"""T-0960: small verbs from the board: fm oops, fm round close, fm show, fm release-check, fm sensitive suggest and
fm outcomes --readme."""
import json
import os
import subprocess
import sys

from helpers import FM, ForemanTestCase

from sampo import core as c


class BoardVerbs(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True).stdout

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def done(self, title):
        self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        tid = c.load_briefs(self.p)[-1].id
        self.fm("task", "finish", tid, "--audit", "self", "--run", "true")
        return tid

    def test_oops_names_the_newest_snapshot_and_restores_only_with_apply(self):
        self.assertIn("No snapshots", self.fm("oops").stdout)
        self.write("README.md", "# app\nmy unsaved paragraph\n")
        self.assertTrue(c.snapshot(self.repo, "git checkout -- ."))
        self.git("checkout", "-q", "--", ".")
        out = self.fm("oops").stdout
        self.assertIn("git checkout -- .", out)
        self.assertIn("restore --source=", out)
        self.assertIn("--apply", out)
        with open(os.path.join(self.repo, "README.md")) as f:
            self.assertNotIn("my unsaved paragraph", f.read(), "without --apply nothing is restored")
        self.fm("oops", "--apply")
        with open(os.path.join(self.repo, "README.md")) as f:
            self.assertIn("my unsaved paragraph", f.read())

    def test_round_close_tags_head_and_lists_the_tasks_closed_since_the_last_round(self):
        a = self.done("Add cache")
        self.write("a.py", "a\n")
        self.git("add", "-A")
        self.git("commit", "-qm", f"Add cache ({a})")
        out = self.fm("round", "close").stdout
        self.assertIn("round-1", out)
        self.assertIn(a, out)
        self.assertEqual(self.git("rev-parse", "round-1^{commit}"), self.git("rev-parse", "HEAD"))
        b = self.done("Add export")
        self.write("b.py", "b\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "Export", "-m", f"Foreman-Task: {b}")
        out = self.fm("round", "close").stdout
        self.assertIn("round-2", out)
        self.assertIn(b, out)
        self.assertNotIn(a, out)

    def test_round_close_lists_the_newest_20_and_counts_the_rest(self):
        # T-1200: the first round of 1.2.34 printed 744 task lines into the release output
        ids = []
        for i in range(25):
            self.fm("task", "new", f"Task {i}", "--type", "FIX", "--tier", "S")
            b = c.load_briefs(self.p)[-1]
            b.meta["status"] = "done"
            c.save_brief(self.p, b)
            ids.append(b.id)
        self.write("a.py", "a\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "Batch (" + ", ".join(ids) + ")")
        out = self.fm("round", "close").stdout
        self.assertEqual([i for i in ids if i in out], ids[5:], "the newest 20")
        self.assertIn("5 earlier", out)
        self.write("b.py", "b\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "Again (" + ", ".join(ids) + ")")
        self.assertEqual(json.loads(self.fm("round", "close", "--json").stdout)["tasks"], ids)

    def test_show_routes_an_id_to_task_show_a_path_to_why_and_words_to_recall(self):
        self.fm("task", "new", "Fix login timeout", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.assertEqual(self.fm("show", "T-0001").stdout, self.fm("task", "show", "T-0001").stdout)
        self.assertEqual(self.fm("show", "README.md:1").stdout, self.fm("why", "README.md:1").stdout)
        self.assertEqual(self.fm("show", "login", "timeout").stdout, self.fm("recall", "login", "timeout").stdout)
        os.makedirs(os.path.join(self.repo, "tests"), exist_ok=True)  # a folder, or a path with more words: recall
        self.assertEqual(self.fm("show", "tests", "failing").stdout, self.fm("recall", "tests", "failing").stdout)

    def test_release_check_runs_doctor_and_demo_in_a_temporary_home(self):
        before = sorted(os.listdir(self.home))
        stub = os.path.join(self.tmp, "bin")  # claude plugin validate passes; no model is ever called
        os.makedirs(stub)
        with open(os.path.join(stub, "claude"), "w") as f:
            f.write("#!/bin/sh\necho '9.9.9 (Claude Code)'\n")
        os.chmod(os.path.join(stub, "claude"), 0o755)
        e = dict(os.environ, FOREMAN_HOME=self.home, PATH=stub + os.pathsep + os.environ["PATH"],
                 FOREMAN_DOCTOR_BENCH="0")  # T-1829: no hook timings under the parallel suite's load
        p = subprocess.run([sys.executable, FM, "release-check"], cwd=self.repo, env=e, capture_output=True, text=True,
                           timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("sampo doctor", p.stdout)
        self.assertIn("sampo demo", p.stdout)
        self.assertEqual(sorted(os.listdir(self.home)), before, "the user's Foreman home is never touched")

    def test_release_check_child_env_keeps_only_the_bench_switch(self):
        from sampo import demo
        old = dict(os.environ)
        try:
            os.environ.update(FOREMAN_DOCTOR_BENCH="0", FOREMAN_INHIBITED="1", CLAUDE_X="1", GIT_DIR="/x")
            env = demo.release_env(self.tmp)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(env.get("FOREMAN_DOCTOR_BENCH"), "0")
        self.assertNotIn("FOREMAN_INHIBITED", env)
        self.assertNotIn("CLAUDE_X", env)
        self.assertNotIn("GIT_DIR", env)
        self.assertEqual(env["HOME"], os.path.join(self.tmp, "home"))

    def test_sensitive_suggest_prints_signals_and_sets_nothing(self):
        self.assertIn("No signals", self.fm("sensitive", "suggest").stdout)
        self.write(".env.production", "KEY=x\n")
        self.write("certs/server.pem", "x\n")
        self.write("config/secrets.yml", "x\n")
        self.write(".env.example", "KEY=\n")
        self.write("product/list.py", "x\n")
        out = self.fm("sensitive", "suggest").stdout
        for sig in (".env.production", "certs/server.pem", "config/secrets.yml"):
            self.assertIn(sig, out)
        self.assertNotIn(".env.example", out)
        self.assertNotIn("product/list.py", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "settings.local.json")))
        self.assertFalse(c.read_meta(self.p).get("sensitive"))

    def test_outcomes_readme_is_one_sentence_with_held_total_and_ungraded(self):
        self.done("Add cache")
        self.done("Add export")
        b = c.load_briefs(self.p)[-1]
        b.meta.pop("verified", None)
        c.save_brief(self.p, b)
        out = self.fm("outcomes", "--readme").stdout.strip()
        self.assertEqual(out, "0 of 0 settled closes held; 2 still in the 30-day window; 1 ungraded.")  # T-3067
        self.assertEqual(json.loads(self.fm("outcomes", "--readme", "--json").stdout),
                         {"held": 0, "settled": 0, "closed": 2, "in_window": 2, "ungraded": 1})
