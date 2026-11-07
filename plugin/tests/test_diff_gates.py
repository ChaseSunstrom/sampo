"""T-0071 round A: what the diff itself tells fm — scope drift, security-sensitive changes, weak verify commands,
outlines of big files and a mechanical pre-audit."""
import json
import os
import subprocess
import sys
import time

from helpers import ForemanTestCase

from sampo import core as c


class DiffGates(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def task(self, *extra):
        self.fm("task", "new", "change it", "--type", "FEATURE", "--tier", "S", "--step", "do it", "--ac", "works",
                *extra, "--focus")

    def touch(self, rel, text="x = 1\n"):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a") as f:  # as the PostToolUse hook records an Edit
            f.write(json.dumps({"ts": c.now(), "task": "T-0001", "event": "touched",
                                "data": {"file": path, "tool": "Edit"}}) + "\n")

    def finish(self, *lenses):
        self.fm("task", "evidence", "T-0001", "--ac", "1", "pytest", "ok")
        self.fm("task", "ac", "T-0001", "check", "1")
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "pytest", "ok")
        for lens in ("self",) + lenses:
            self.fm("task", "audit", "T-0001", lens, "checked", "ok")
        return self.fm("task", "done", "T-0001", check=False)

    def test_edits_outside_scope_need_a_reason(self):
        self.task("--scope", "src/**")
        self.touch("src/app.py")
        self.touch("setup.cfg")
        p = self.finish()
        self.assertEqual(p.returncode, 2)
        self.assertIn("edited outside scope [src/**]: setup.cfg", p.stderr)
        self.assertNotIn("src/app.py", p.stderr)
        self.fm("task", "log", "T-0001", "scope: the build config names the new module")
        time.sleep(1.1)  # one-second timestamps: the next edit comes after the reason
        self.touch("tox.ini")
        p = self.fm("task", "done", "T-0001", check=False)
        self.assertIn("tox.ini", p.stderr, "an earlier reason doesn't cover a later out-of-scope edit")
        self.assertIn("after your last edit of tox.ini", p.stderr)  # T-0168: and it says so
        self.fm("task", "log", "T-0001", "scope: the tox.ini builds it")
        self.fm("task", "audit", "T-0001", "self", "rechecked after the tox.ini edit", "ok")
        self.assertEqual(self.fm("task", "done", "T-0001", check=False).returncode, 0)

    def test_finish_refuses_an_out_of_scope_edit_before_any_check_runs(self):
        # T-3550: T-3163's close ran its gates, then done() refused it for one file outside scope
        self.task("--scope", "src/**")
        self.touch("src/app.py")
        self.touch("setup.cfg")
        p = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", check=False)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertIn("nothing ran", p.stderr)
        self.assertIn("edited outside scope [src/**]: setup.cfg", p.stderr)
        self.assertEqual(c.find_brief(self.p, "T-0001").evidence(), [])

    def test_an_untracked_file_removed_since_the_start_is_not_this_tasks_edit(self):
        # T-3550: removed by another task or a shell rm, it read as touched "now", so no scope reason could cover it
        self.write("docs/old.md")  # tracked: its removal is still an edit
        subprocess.run(["git", "-C", self.repo, "add", "docs/old.md"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "old"], check=True)
        self.write("notes/scratch.md", "draft\n")  # untracked when the task starts
        self.task("--scope", "src/**")
        self.touch("src/app.py")
        os.remove(os.path.join(self.repo, "notes/scratch.md"))
        os.remove(os.path.join(self.repo, "docs/old.md"))
        p = self.finish()
        self.assertIn("edited outside scope [src/**]: docs/old.md", p.stderr)
        self.assertNotIn("scratch", p.stderr)
        self.fm("task", "set", "T-0001", "scope+=docs/old.md")  # a reason can't cover a deletion: it reads as touched now
        p = self.fm("task", "done", "T-0001")
        self.assertIn("1 untracked file gone since the start (not counted as this task's edits): notes/scratch.md",
                      p.stderr)  # left out, never silently

    def test_security_sensitive_change_needs_the_adversary_lens(self):
        self.task()
        self.touch("auth/login.py")
        p = self.finish()
        self.assertEqual(p.returncode, 2)
        self.assertIn("audit missing: adversary", p.stderr)
        self.assertIn("security-sensitive: auth/login.py", p.stderr)
        self.fm("task", "audit", "T-0001", "adversary", "abuse cases", "ok")
        self.assertEqual(self.fm("task", "done", "T-0001", check=False).returncode, 0)

    def write(self, rel, text="x = 1\n"):  # edited before or outside Foreman: no ledger record
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def test_uncommitted_work_from_before_the_task_is_not_its_change(self):
        # T-0078/T-0079: work in progress (or a never-committed repo) made every task own every file
        self.write("auth/login.py")
        self.write("poetry.lock", "pinned = 1\n")
        self.task("--scope", "src/**")
        self.touch("src/app.py")
        p = self.finish()
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_repo_with_no_commits_still_judges_the_task_by_its_own_changes(self):
        # T-0079: no start commit, so before this nothing was judged at all, or everything was
        import shutil
        import subprocess
        shutil.rmtree(os.path.join(self.repo, ".git"))
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        self.write("auth/login.py")
        self.task("--scope", "src/**")
        self.touch("src/app.py")
        self.assertEqual(self.finish().returncode, 0)
        self.fm("task", "new", "two", "--type", "FEATURE", "--tier", "S", "--step", "do it", "--ac", "works", "--focus")
        self.write("auth/session.py")  # through the shell: no ledger record, the snapshot still sees it
        self.fm("task", "evidence", "T-0002", "--ac", "1", "pytest", "ok")
        self.fm("task", "ac", "T-0002", "check", "1")
        self.fm("task", "step", "T-0002", "done", "1", "--evidence", "pytest", "ok")
        self.fm("task", "audit", "T-0002", "self", "checked", "ok")
        p = self.fm("task", "done", "T-0002", check=False)
        self.assertIn("security-sensitive: auth/session.py", p.stderr)

    def test_a_diff_git_cannot_produce_fails_closed(self):
        # T-0078 review: an unreadable file made git add fail, the diff came back "" and eval( went unchecked
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        self.task()
        self.touch("src/util.py", "x = eval(y)\n")  # fixture text for the detector; never run
        locked = os.path.join(self.repo, "locked.bin")
        self.write("locked.bin")
        os.chmod(locked, 0)
        try:
            p = self.finish()
        finally:
            os.chmod(locked, 0o644)
        self.assertEqual(p.returncode, 2)
        self.assertIn("diff unavailable", p.stderr)

    def test_its_own_edit_to_an_already_dirty_sensitive_file_still_counts(self):
        self.write("auth/token.py", "a = 1\n")
        self.task()
        self.touch("auth/token.py", "a = 1\nb = eval(x)\n")  # fixture text for the detector; never run
        p = self.finish()
        self.assertEqual(p.returncode, 2)
        self.assertIn("security-sensitive: auth/token.py", p.stderr)

    def test_plain_change_needs_only_the_tier_audits(self):
        self.task()
        self.touch("docs/notes.py")
        self.assertEqual(self.finish().returncode, 0)

    def test_weak_verify_commands_are_flagged_when_written(self):
        p = self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--ac", "a :: echo ok",
                    "--ac", "b :: pytest -q | tail -3", "--ac", "c :: nosuchtool-xyz --run", "--ac", "d :: git status")
        self.assertIn("`echo ok`: it can't fail", p.stderr)
        self.assertIn("last piped program's", p.stderr)
        self.assertIn("nosuchtool-xyz isn't on PATH", p.stderr)
        self.assertNotIn("git status", p.stderr)
        p = self.fm("task", "ac", "T-0001", "add", "e :: true")
        self.assertIn("can't fail", p.stderr)
        self.assertEqual(c.lint_verify("set -o pipefail; git log | head -1", self.repo), [])
        self.assertEqual(c.lint_verify("cd sub && FOO=1 git status", self.repo), [])
        # T-0357: Foreman's run.py -k is a substring, so a pattern with a space matches nothing
        self.assertTrue(any("repeat -k" in x for x in c.lint_verify(
            "python3 plugin/tests/run.py -k 'test_guard or test_cli'", self.repo)))
        self.assertFalse(any("repeat -k" in x for x in c.lint_verify("pytest -k 'a or b'", self.repo)))
        run = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
        p = subprocess.run([sys.executable, run, "-k", "test_cli or test_guard"], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(p.returncode, 2)  # a usage error, before anything runs
        self.assertIn("repeat -k", p.stderr)

    def test_lint_names_run_py_modules_and_k_filters_that_match_nothing(self):
        # T-2027: a criterion naming a test module that doesn't exist, or a -k no test in it matches, fails every run,
        # and once it has evidence it can't be edited: say so at fm task new, from the files, without importing them
        tests = os.path.join(self.repo, "plugin", "tests")
        os.makedirs(tests, exist_ok=True)
        with open(os.path.join(tests, "test_widget.py"), "w") as f:
            f.write("class Widget(Base):\n    def test_spins(self):\n        pass\n\n    def helper(self):\n        pass\n")
        lint = lambda cmd: c.lint_verify(cmd, self.repo)  # noqa: E731
        self.assertEqual(lint("python3 plugin/tests/run.py test_widget -k test_spins"), [])
        self.assertEqual(lint("python3 plugin/tests/run.py test_widget -k Widget"), [], "-k matches the whole id")
        self.assertEqual(lint("python3 plugin/tests/run.py test_wid"), [], "a name is a substring too")
        got = lint("python3 plugin/tests/run.py test_widget -k test_wobbles")
        self.assertTrue(any("test_widget" in x and "-k test_wobbles" in x for x in got), got)
        self.assertTrue(lint("python3 plugin/tests/run.py test_widget -k helper"), "helper isn't a test")
        got = lint("python3 plugin/tests/run.py test_gadget test_widget")
        self.assertTrue(any("test_gadget" in x and "no test module" in x for x in got), got)
        self.assertFalse(any("test_widget" in x for x in got), got)
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        t = time.process_time()
        self.assertEqual(c.lint_verify("python3 plugin/tests/run.py test_diff_gates -k test_lint_names", root), [])
        self.assertEqual(c.lint_verify("python3 plugin/tests/run.py test_tail_rows_with_keep 2>&1", root), [],
                         "a test's name, found across every module; 2>&1 isn't a name")
        self.assertLess(time.process_time() - t, 1.0)  # CPU: lint_verify reads files in this process

    def test_run_py_repeats_p(self):
        # T-0389: `run.py -p a.py -p b.py` kept only the last -p and ran b.py alone, silently
        run = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
        # T-3572: no CPU slot pool: outside run.py (no slot to nest in) it waited 70-88 s for one on a busy host
        p = subprocess.run([sys.executable, run, "-p", "test_mission.py", "-p", "test_help.py", "-k", "test_a_cli",
                            "-k", "test_every_command_once"], capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, SAMPO_SLOTS="0"))
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertIn("Ran 2 tests", p.stderr)  # one from each module

    def test_outline_lists_definitions_with_line_ranges(self):
        py = os.path.join(self.repo, "m.py")
        with open(py, "w") as f:
            f.write("import os\n\n\nclass A:\n    def f(self):\n        return 1\n\n\ndef g():\n    pass\n")
        data = self.fm_json("outline", py)
        self.assertEqual([(d["start"], d["end"], d["depth"], d["name"]) for d in data["defs"]],
                         [(4, 6, 0, "class A"), (5, 6, 1, "def f"), (9, 10, 0, "def g")])
        js = os.path.join(self.repo, "m.js")
        with open(js, "w") as f:
            f.write("export function a() {\n  return 1;\n}\n\nclass B {\n}\n")
        out = self.fm("outline", js).stdout
        self.assertIn("1-3", out)
        self.assertIn("function a", out)
        self.assertIn("class B", out)
        self.assertEqual(self.fm("outline", "nope.py", check=False).returncode, 1)

    def test_audit_prep_lists_mechanical_findings_and_adds_adversary_for_risky_small_changes(self):
        self.task()
        self.touch("auth/token.py", "def check(t):\n    breakpoint()\n    # TODO tighten\n    return eval(t)\n")
        out = self.fm_json("audit", "prep", "T-0001")
        joined = "\n".join(out["pre_audit"])
        for want in ("debug leftover in auth/token.py", "new TODO in auth/token.py",
                     "source changed with no test changed", "security-sensitive"):
            self.assertIn(want, joined)
        self.assertEqual(out["lenses"], ["self", "adversary"])
        with open(out["brief"]) as f:
            self.assertIn("Pre-audit (mechanical", f.read())

    def test_whole_read_of_a_big_file_gets_one_outline_note(self):
        big = os.path.join(self.repo, "big.py")
        with open(big, "w") as f:
            f.write("x = 1\n" * 700)

        def read(**extra):
            r = self.hook("PostToolUse", {"tool_name": "Read", "tool_input": dict(file_path=big, **extra)})
            return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""
        self.assertEqual(read(offset=1, limit=50), "")
        self.assertIn("sampo outline", read())
        self.assertEqual(read(), "", "once per session")


class LiveVerify(ForemanTestCase):
    """T-0943: a verify command that reaches a live service is a manual check, not an unattended criterion."""

    def test_live_service_commands_are_named_and_local_ones_are_not(self):
        live = ["aws iam simulate-principal-policy --policy-source-arn x", "gcloud run services list",
                "az group list", "kubectl get pods -n prod", "psql -h db.example.com -c 'select 1'",
                "mysql --host=db -e 'select 1'", "terraform apply -auto-approve", "helm upgrade app ./chart",
                "set -o pipefail; kubectl get ns | grep prod"]
        for cmd in live:
            self.assertTrue(any("live service" in x for x in c.lint_verify(cmd, self.repo)), cmd)
        for cmd in ["terraform validate", "terraform plan -out p", "psql -c 'select 1'", "helm lint ./chart",
                    "kubectl kustomize overlays/prod", "python3 -m pytest -q", "aws --version"]:
            self.assertFalse(any("live service" in x for x in c.lint_verify(cmd, self.repo)), cmd)
        p = self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--ac", "a :: kubectl get pods")
        self.assertIn("live service", p.stderr)  # a warning: the criterion is still recorded
        self.assertIn("kubectl get pods", self.fm("task", "show", "T-0001").stdout)
