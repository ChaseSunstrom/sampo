"""Bench family (T-0280): judged cases for work without tests (T-0231), plugin duels (T-0240), replays on an earlier
Foreman (T-0241), the user's steers as judged cases (T-0242) and a shadow-user soak (T-0243)."""
import json
import os
import shutil
import subprocess

from helpers import ForemanTestCase, read_text

from sampo import core as c

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:  # T-1255: run_arm stamps the Claude Code version
    print("2.1.304 (Claude Code)")
    sys.exit(0)
stdin = sys.stdin.read()
system = args[args.index("--append-system-prompt") + 1] if "--append-system-prompt" in args else ""
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "stdin": stdin, "system": system,
                        "window": os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
                        "marathon": os.environ.get("FOREMAN_MARATHON"), "ablate": os.environ.get("FOREMAN_ABLATE"),
                        "claude_md": open("CLAUDE.md").read() if os.path.exists("CLAUDE.md") else None}) + "\n")
if os.environ.get("CLAUDE_CONFIG_DIR") and not os.environ.get("STUB_NO_SID") and (
        "--plugin-dir" in args or args[1:2] and args[1].startswith("Task:")):  # an arm's kept session (T-3364: audited)
    d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "-tmp-replay")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "s-1.jsonl"), "w") as f:  # compacted twice when a window is set
        f.write('{"type":"user"}\n' + '{"type":"system","subtype":"compact_boundary"}\n' * 2
                if "--plugin-dir" in args and os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") else '{"type":"user"}\n')
if "--plugin-dir" not in args and args[1:2] and args[1].startswith("Task:") and os.environ.get("STUB_BARE_FIX"):
    with open("calc.py", "w") as f:  # T-1376: the plain session fixes it
        f.write("def double(x):\n    return x * 2\n")
if "--plugin-dir" in args:  # a replayed session
    if os.environ.get("STUB_PLANT"):  # a session that leaves git config for whoever runs git here next
        import subprocess
        subprocess.run(["git", "config", "core.fsmonitor", "touch " + os.environ["STUB_PLANT"]])
        subprocess.run(["git", "config", "core.hooksPath", os.path.dirname(os.environ["STUB_PLANT"])])
    if os.environ.get("STUB_FAIL"):
        print(json.dumps({"type": "result", "is_error": True, "total_cost_usd": 0.1, "result": "crashed"}))
        sys.exit(1)
    if os.environ.get("STUB_DOCS"):
        with open("README.md", "a") as f:
            f.write("\nUsage: run calc.\n")
    cost = float(os.environ.get("STUB_COST_WITH_RIVAL", 0.4)) if args.count("--plugin-dir") > 1 else 0.4
    sid = None if os.environ.get("STUB_NO_SID") else "s-1"
    print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": cost, "num_turns": 5, "session_id": sid,
                      "result": "Done. Run `make` yourself to finish."}))
elif "You play the user" in system:
    print(json.dumps({"result": "REPEAT: I said never hand me commands to run\nNEXT: run make yourself please",
                      "total_cost_usd": 0.01}))
else:  # the judge
    score = os.environ.get("STUB_SCORE", "4")
    print(json.dumps({"result": f"SCORE: {score}\n- the usage section is clear\n- one example is missing",
                      "total_cost_usd": 0.01, "session_id": None if os.environ.get("STUB_NO_SID") else "s-1"}))
'''


def git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if p.returncode:  # T-2019: a bare CalledProcessError hid "No space left on device"
        raise AssertionError(f"git {' '.join(args)} exited {p.returncode}: {p.stderr.strip()}")
    return p.stdout.strip()


class _Bench(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log,
                    "CLAUDE_CONFIG_DIR": self.safe_config_dir()}  # T-3364: where the stub keeps its transcript; T-6020: not under /tmp
        self.fm("init")
        self.fm("claude-bench", "on")  # T-5584: a benchmark calls a Claude model only once the user turns it on
        self.write("calc.py", "def double(x):\n    return x + x + 1\n")
        self.write("README.md", "# calc\n")
        git(self.repo, "add", "calc.py", "README.md")
        git(self.repo, "commit", "-qm", "calc")
        self.calc = self.finished("double() is off by one", "PYTHONPATH=. python3 tests/test_calc.py")
        self.write("calc.py", "def double(x):\n    return x * 2\n")
        self.write("tests/test_calc.py", "from calc import double\nassert double(3) == 6, double(3)\nprint('ok')\n")
        git(self.repo, "add", "-A", "calc.py", "tests")
        git(self.repo, "commit", "-qm", f"double() is right ({self.calc})")
        self.docs = self.finished("Document how to run calc", None, steer="never hand the user commands to run")
        self.write("README.md", "# calc\n\nUsage: python3 -c 'from calc import double'\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", f"calc usage ({self.docs})")

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def finished(self, title, verify, steer=None):
        ac = ["--ac", f"fixed :: {verify}"] if verify else ["--ac", "the README says how to run calc"]
        tid = json.loads(self.fm("task", "new", title, "--type", "FIX", "--tier", "S", *ac, "--step", "do it",
                                 "--json").stdout)["id"]
        self.fm("task", "set", tid, "--section", "Raw request", "--text", f"> {title}")
        self.fm("focus", tid)
        if steer:
            self.fm("task", "log", tid, f"steer: {steer}")
        self.fm("task", "step", tid, "done", "1", "--evidence", "x", "ok")
        self.fm("task", "ac", tid, "check", "1", "--evidence", "x", "ok")
        self.fm("task", "audit", tid, "self", "x", "ok")
        self.fm("task", "set", tid, "--section", "Regression test", "--text", "none: fixture")
        self.fm("task", "done", tid)
        return tid

    def calls(self):
        return [json.loads(x) for x in read_text(self.log).splitlines()]

    def sessions(self):
        return [x for x in self.calls() if "--plugin-dir" in x["args"]]


class Judged(_Bench):
    def test_a_docs_only_task_is_judged(self):
        built = json.loads(self.fm("bench", "build", "--judged", "--json", env=self.env).stdout)
        case = next(x for x in built["cases"] if x["id"] == self.docs)
        self.assertTrue(case["judge"])
        self.assertIn("the README says how to run calc", " ".join(case["rubric"]))
        self.assertIn(self.calc, [x["id"] for x in built["cases"]])  # tested work stays a tested case
        res = json.loads(self.fm("bench", "run", "--ids", self.docs, "--label", "j", "--json",
                                 env=dict(self.env, STUB_DOCS="1")).stdout)
        got = res["cases"][0]
        self.assertEqual((got["score"], got["pass"]), (4, True))
        judge = [x for x in self.calls() if "--plugin-dir" not in x["args"]][-1]
        self.assertIn("Usage: run calc", judge["stdin"])  # the replay's own change is what's judged
        self.assertIn("Run `make` yourself", judge["stdin"])  # and its final reply
        self.assertIn("never hand the user commands", judge["stdin"])  # the user's taste is in the rubric
        shown = self.fm("bench", "show", "j", env=self.env).stdout
        self.assertIn("score 4/5", shown)
        self.assertIn("judge: the usage section is clear", shown)
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["bench"]["last"]["score"], 4.0)
        low = json.loads(self.fm("bench", "run", "--ids", self.docs, "--label", "low", "--json",
                                 env=dict(self.env, STUB_SCORE="2"), check=False).stdout)
        self.assertEqual((low["cases"][0]["score"], low["cases"][0]["pass"]), (2, False))  # below 4 fails

    def test_a_run_stopped_at_its_budget_is_inconclusive_and_not_judged(self):
        # T-0309: the court's R-T-0205 spent $0.5055 of $0.50 — cut off mid-task, then judged as if it had finished
        self.fm("bench", "build", "--judged", env=self.env)
        res = json.loads(self.fm("bench", "run", "--ids", self.docs, "--label", "cap", "--budget", "0.3", "--json",
                                 env=dict(self.env, STUB_DOCS="1"), check=False).stdout)
        got = res["cases"][0]
        self.assertFalse(got["pass"])
        self.assertIn("budget cap", got["error"])
        self.assertFalse([x for x in self.calls() if "--plugin-dir" not in x["args"]])  # no judge paid
        self.assertIn("budget cap", self.fm("bench", "show", "cap", env=self.env).stdout)

    def test_without_judged_a_docs_task_is_skipped(self):
        built = json.loads(self.fm("bench", "build", "--json", env=self.env).stdout)
        self.assertNotIn(self.docs, [x["id"] for x in built["cases"]])
        self.fm("bench", "build", "--judged", env=self.env)
        again = json.loads(self.fm("bench", "build", "--json", env=self.env).stdout)  # review: a plain rebuild keeps them
        self.assertIn(self.docs, [x["id"] for x in again["cases"]])

    def test_git_config_a_session_leaves_runs_nothing(self):
        # review: the judge's git add/diff ran in the worktree a bypass-mode session had configured
        self.fm("bench", "build", "--judged", env=self.env)
        marker = os.path.join(self.tmp, "planted", "ran")
        os.makedirs(os.path.dirname(marker))
        for hook in ("pre-commit", "post-checkout", "fsmonitor-watchman"):
            with open(os.path.join(self.tmp, "planted", hook), "w") as f:
                f.write(f"#!/bin/sh\ntouch {marker}\n")
            os.chmod(os.path.join(self.tmp, "planted", hook), 0o755)
        try:
            self.fm("bench", "run", "--ids", self.docs, "--label", "p", env=dict(self.env, STUB_PLANT=marker), check=False)
            self.fm("bench", "run", "--ids", self.calc, "--label", "q", env=dict(self.env, STUB_PLANT=marker), check=False)
        finally:
            for key in ("core.fsmonitor", "core.hooksPath"):  # T-3364: an arm's own repo takes the config now, not this one
                subprocess.run(["git", "-C", self.repo, "config", "--unset", key], capture_output=True)
        self.assertFalse(os.path.exists(marker))

    def test_the_change_is_fenced_and_a_failed_session_isnt_judged(self):
        self.fm("bench", "build", "--judged", env=self.env)
        self.fm("bench", "run", "--ids", self.docs, "--label", "j", env=dict(self.env, STUB_DOCS="1"))
        judge = [x for x in self.calls() if "--plugin-dir" not in x["args"]][-1]
        self.assertIn("<diff>", judge["stdin"])
        self.assertIn("</reply>", judge["stdin"])
        n = len(self.calls())
        res = json.loads(self.fm("bench", "run", "--ids", self.docs, "--label", "k", "--json",
                                 env=dict(self.env, STUB_FAIL="1"), check=False).stdout)
        self.assertFalse(res["cases"][0]["pass"])
        self.assertEqual(len(self.calls()), n + 1)  # the session only: no judge paid for a crashed run


class Duel(_Bench):
    def setUp(self):
        super().setUp()
        rival = os.path.join(self.tmp, "rival")  # T-0294: a duel's rival must be an installed plugin
        cc = self.safe_config_dir()  # T-6020: not under /tmp
        os.makedirs(os.path.join(cc, "plugins"))
        with open(os.path.join(cc, "plugins", "installed_plugins.json"), "w") as f:
            json.dump({"version": 2, "plugins": {"rival@m": [{"scope": "user", "installPath": rival}]}}, f)
        self.env = dict(self.env, CLAUDE_CONFIG_DIR=cc)

    def test_both_arms_and_the_gate(self):
        self.fm("bench", "build", env=self.env)
        rival = os.path.join(self.tmp, "rival")
        os.makedirs(os.path.join(rival, ".claude-plugin"))
        with open(os.path.join(rival, ".claude-plugin", "plugin.json"), "w") as f:
            f.write('{"name": "rival"}')
        res = json.loads(self.fm("bench", "duel", rival, "--json", env=self.env).stdout)
        dirs = [[a[i + 1] for i, x in enumerate(a) if x == "--plugin-dir"] for a in (s["args"] for s in self.sessions())]
        snap = dirs[0][0]  # T-1231: a snapshot of this Foreman, not the live folder
        self.assertIn("fm-bench-plugin-", snap)
        self.assertEqual(dirs, [[snap], [snap, rival]])
        self.assertTrue(res["gate"]["lines"][0].startswith(res["without"]))
        for label in (res["without"], res["with"]):
            self.assertTrue(os.path.exists(os.path.join(c.find_project(self.repo).dir, "bench", "results",
                                                        label + ".json")))
        with_ = json.loads(read_text(os.path.join(c.find_project(self.repo).dir, "bench", "results",
                                                  res["with"] + ".json")))
        self.assertEqual(with_["extra"], [rival])  # the results say what ran beside Foreman
        self.assertIn("no difference", self.fm("bench", "duel", rival, env=self.env).stdout)
        view = json.loads(self.fm("ui", "--json").stdout)
        self.assertIn("no difference", view["bench"]["verdicts"][0]["verdict"])  # the pane's bench card
        self.assertEqual(view["bench"]["verdicts"][0]["kind"], "duel")

    def test_a_losing_contender_says_why(self):
        self.fm("bench", "build", env=self.env)
        rival = os.path.join(self.tmp, "rival")
        os.makedirs(os.path.join(rival, ".claude-plugin"))
        with open(os.path.join(rival, ".claude-plugin", "plugin.json"), "w") as f:
            f.write('{"name": "rival"}')
        out = self.fm("bench", "duel", rival, "--json", env=dict(self.env, STUB_COST_WITH_RIVAL="2.0")).stdout
        self.assertIn("rival hurts here: cost per case up 400%", json.loads(out)["verdict"])

    def test_a_folder_that_is_not_a_plugin_is_refused(self):
        self.fm("bench", "build", env=self.env)
        p = self.fm("bench", "duel", self.tmp, env=self.env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(self.log))


class Versions(_Bench):
    def test_an_earlier_foreman_and_this_one(self):
        self.fm("bench", "build", env=self.env)
        src = os.path.join(self.tmp, "src")
        shutil.copytree(c.PLUGIN_ROOT, os.path.join(src, "plugin"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "evals"))
        git(self.tmp, "init", "-q", src)
        git(src, "add", "-A")
        git(src, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "old foreman")
        rev = git(src, "rev-parse", "HEAD")
        res = json.loads(self.fm("bench", "versions", "HEAD", "--source", src, "--json", env=self.env).stdout)
        old, new = (s["args"][s["args"].index("--plugin-dir") + 1] for s in self.sessions())
        self.assertTrue(old.endswith(os.sep + "plugin") and "fm-bench-" in old, old)
        self.assertIn("fm-bench-plugin-", new, "this Foreman, as a snapshot")
        self.assertIn(rev[:10], res["old"])
        self.assertIn("→", res["gate"]["lines"][0])
        self.assertEqual(git(src, "worktree", "list").count("\n"), 0)  # the old version's worktree is gone


class Court(_Bench):
    def test_steers_become_judged_cases(self):
        res = json.loads(self.fm("bench", "court", "--json", env=self.env).stdout)
        case = res["cases"][0]
        self.assertEqual(case["score"], 4)
        cases = json.loads(read_text(os.path.join(c.find_project(self.repo).dir, "bench", "cases.json")))
        court = [x for x in cases if x["id"].startswith("R-")]
        self.assertEqual(len(court), 1)
        self.assertIn("never hand the user commands", court[0]["rubric"][0])
        self.assertEqual(court[0]["prompt"], "Document how to run calc")
        judge = [x for x in self.calls() if "--plugin-dir" not in x["args"]][-1]
        self.assertIn("never hand the user commands", judge["stdin"])


class Soak(_Bench):
    def test_a_two_turn_conversation_with_the_shadow_user(self):
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "soak", "--ids", self.calc, "--turns", "2", "--json", env=self.env).stdout)
        got = res["cases"][0]
        self.assertIn("never hand me commands", " ".join(got["complaints"]))
        first, second = self.sessions()
        self.assertNotIn("--no-session-persistence", first["args"])  # a conversation needs its session kept
        self.assertEqual(second["args"][second["args"].index("--resume") + 1], "s-1")
        self.assertIn("run make yourself please", " ".join(second["args"]))
        shadow = [x for x in self.calls() if "You play the user" in x["system"]]
        self.assertEqual(len(shadow), 2)
        self.assertIn("never hand the user commands", shadow[0]["stdin"])  # the user's own steers
        self.assertIn("Run `make` yourself", shadow[0]["stdin"])
        self.assertTrue(os.path.exists(res["path"]))
        shown = self.fm("bench", "show", res["label"], env=self.env).stdout
        self.assertIn("had to repeat: I said never hand me commands", shown)
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["bench"]["last"]["repeats"], 2)  # once per turn

    def test_a_soak_runs_in_a_fresh_repo_not_a_linked_worktree(self):
        # T-3364: a linked worktree shares the repo's objects, so the arm could git show the answers
        from unittest import mock

        from sampo import bench
        from helpers import PLUGIN
        case = json.loads(self.fm("bench", "build", "--json", env=self.env).stdout)["cases"][0]
        seen = []

        def session(wt, *a, **k):
            seen.append(os.path.isdir(os.path.join(wt, ".git")))
            return {}, "stop"
        with mock.patch.object(bench, "_worktree", side_effect=AssertionError("linked worktree")), \
                mock.patch.object(bench, "_session", session):
            bench.soak_case(c.find_project(self.repo), case, PLUGIN)
        self.assertEqual(seen, [True])

    def test_a_soak_needs_the_guard_a_session_id_and_sane_numbers(self):
        self.fm("bench", "build", env=self.env)
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(os.path.join(bare, ".claude-plugin"))
        with open(os.path.join(bare, ".claude-plugin", "plugin.json"), "w") as f:
            f.write('{"name": "bare"}')
        from sampo import bench
        with self.assertRaises(ValueError):  # refused inside soak_case, whoever calls it
            bench.soak_case(c.find_project(self.repo), {"id": "x", "base": "HEAD", "prompt": "x"}, bare)
        self.assertFalse(os.path.exists(self.log))
        for bad in (["--budget", "nan"], ["--max", "0"], ["--turns", "0"]):
            p = self.fm("bench", "soak", *bad, env=self.env, check=False)
            self.assertNotEqual(p.returncode, 0, bad)
        res = json.loads(self.fm("bench", "soak", "--ids", self.calc, "--json", env=dict(self.env, STUB_NO_SID="1")).stdout)
        self.assertIn("session id", res["cases"][0]["error"])
        self.assertFalse(res["cases"][0]["pass"])


class Snippet(_Bench):  # T-1264
    def test_a_snippet_duel_gives_claude_with_no_plugins_the_one_file_rules_as_claude_md(self):
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "duel", "--vs", "snippet", "--ids", self.calc, "--json", env=self.env).stdout)
        self.assertTrue(res["with"].startswith("duel-snippet-bare-"), res)
        rules = read_text(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates",
                                       "CLAUDE.lite.md"))
        tasks = [s for s in self.calls() if s["args"][:1] == ["-p"] and s["args"][1].startswith("Task:")]
        self.assertEqual(len(tasks), 2)
        bare = [s for s in tasks if "--plugin-dir" not in s["args"]]
        self.assertEqual(len(bare), 1)
        self.assertIn(rules.strip(), bare[0]["claude_md"] or "", "Claude with no plugins reads the rules")
        self.assertIsNone(next(s for s in tasks if "--plugin-dir" in s["args"])["claude_md"], "Foreman's side doesn't")
        self.assertIn("Claude with Sampo's rules as one CLAUDE.md", json.dumps(res))
        p = self.fm("bench", "duel", "some-plugin", "--vs", "snippet", "--ids", self.calc, env=self.env, check=False)
        self.assertNotEqual(p.returncode, 0)


class Impossible(_Bench):  # T-1377
    def setUp(self):
        super().setUp()
        self.write("vault.py", "import acme_vault\n")
        self.write("tests/test_vault.py", "import vault  # needs acme_vault, which isn't installed\n")
        git(self.repo, "add", "-A", "vault.py", "tests")
        git(self.repo, "commit", "-qm", "vault: secrets through the company's acme_vault client")
        self.verify = "PYTHONPATH=. python3 {tests}"

    def build(self, *more):
        return json.loads(self.fm("bench", "build", "--commits", "HEAD~3..HEAD", "--verify", self.verify, *more,
                                  "--json", env=self.env).stdout)

    def test_impossible_marks_commits_whose_tests_fail_at_them_too(self):
        plain = self.build()
        self.assertTrue(any("fail at its own commit" in s for s in plain["skipped"]), plain["skipped"])
        built = self.build("--impossible")
        case = next(x for x in built["cases"] if x["title"].startswith("vault:"))
        self.assertEqual((case["impossible"], case["judge"], case["type"]), (True, True, "IMPOSSIBLE"))
        self.assertIn("cannot be done", " ".join(case["rubric"]))
        self.assertTrue(any("it can be done" in s for s in built["skipped"]), "the calc fix is possible")
        p = self.fm("bench", "build", "--impossible", "--json", env=self.env, check=False)
        self.assertNotEqual(p.returncode, 0, "--impossible goes with --commits")

    def test_the_duel_counts_false_dones(self):
        cid = next(x["id"] for x in self.build("--impossible")["cases"] if x.get("impossible"))
        res = json.loads(self.fm("bench", "duel", "--vs", "none", "--ids", cid, "--json",
                                 env=dict(self.env, STUB_SCORE="1")).stdout)
        self.assertEqual(res["false_done"], {"foreman": 1, "rival": 1, "cases": 1})
        self.assertIn("false done: Sampo 1/1, Claude with no plugins 1/1", res["gate"]["lines"])
        text = self.fm("bench", "duel", "--vs", "none", "--ids", cid, env=dict(self.env, STUB_SCORE="5")).stdout
        self.assertIn("false done: Sampo 0/1, Claude with no plugins 0/1", text)


class Ablation(_Bench):  # T-1375, T-1376
    def results(self):
        return os.path.join(c.find_project(self.repo).dir, "bench", "results")

    def test_ablate_runs_full_foreman_and_one_arm_per_feature_off(self):
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "duel", "--ablate", "scope,lessons", "--ids", self.calc, "--json",
                                 env=self.env).stdout)
        tasks = [s for s in self.calls() if s["args"][:1] == ["-p"] and s["args"][1].startswith("Task:")]
        self.assertEqual(sorted(str(s["ablate"]) for s in tasks), ["None", "lessons", "scope"])
        self.assertEqual(sorted(res["arms"]), ["lessons", "scope"])
        self.assertTrue(res["full"].startswith("duel-ablate-full-"))
        text = self.fm("bench", "duel", "--ablate", "scope", "--ids", self.calc, env=self.env).stdout
        self.assertRegex(text, r"scope off: \d/1 passed \(full \d/1\)")
        for bad in (["--ablate", "guard"], ["--ablate", "scope", "--vs", "none"]):
            p = self.fm("bench", "duel", *bad, "--ids", self.calc, env=self.env, check=False)
            self.assertNotEqual(p.returncode, 0, bad)

    def test_a_feature_whose_off_arm_ties_full_foreman_files_one_subtraction_candidate(self):  # T-2536
        self.fm("bench", "build", env=self.env)
        self.fm("bench", "duel", "--ablate", "scope", "--ids", self.calc, env=self.env)
        briefs = lambda: c.load_briefs(c.find_project(self.repo), include_archive=True)  # noqa: E731
        cands = [b for b in briefs() if "ablate" in b.title.lower() and "scope" in b.title]
        self.assertEqual(len(cands), 1, [b.title for b in briefs()])
        raw = open(cands[0].path, encoding="utf-8").read()
        self.assertIn("duel-ablate-scope-", raw, "names the run")
        self.assertIn("long-horizon", raw, "a tie on duel cases is a candidate, never a cut")
        self.fm("bench", "duel", "--ablate", "scope", "--ids", self.calc, env=self.env)
        self.assertEqual(len([b for b in briefs() if "ablate" in b.title.lower() and "scope" in b.title]), 1)

    def test_equal_spend_retries_the_plain_arm_up_to_foremans_spend(self):
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "duel", "--vs", "none", "--equal-spend", "--ids", self.calc, "--json",
                                 env=self.env).stdout)
        self.assertTrue(res["with"].startswith("duel-spend-bare-"), res)
        bare = json.loads(read_text(os.path.join(self.results(), res["with"] + ".json")))["cases"][0]
        self.assertEqual(bare["tries"], 5, "0.01 a try never reaches 0.4: five at most")
        res = json.loads(self.fm("bench", "duel", "--vs", "none", "--equal-spend", "--ids", self.calc, "--json",
                                 env=dict(self.env, STUB_BARE_FIX="1")).stdout)
        bare = json.loads(read_text(os.path.join(self.results(), res["with"] + ".json")))["cases"][0]
        self.assertEqual((bare["tries"], bare["pass"]), (1, True), "a pass stops it")
        p = self.fm("bench", "duel", "--equal-spend", "--ids", self.calc, env=self.env, check=False)
        self.assertNotEqual(p.returncode, 0, "--equal-spend goes with --vs none")


class Repro(_Bench):  # T-1261
    def test_repro_reruns_one_duel_row_on_both_sides_with_its_saved_settings(self):
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "duel", "--vs", "none", "--ids", self.calc, "--json", env=self.env).stdout)
        tasks = lambda: [s for s in self.calls()  # noqa: E731  both sides' task sessions (the bare one has no plugin)
                         if s["args"][:1] == ["-p"] and s["args"][1].startswith("Task:")]
        before = len(tasks())
        out = self.fm("bench", "repro", res["without"], self.calc, env=self.env).stdout
        self.assertIn(f"fm bench repro {res['without']} {self.calc}", out)
        self.assertRegex(out, r"Sampo: then [✓✗].*, now [✓✗]")
        self.assertRegex(out, r"Claude Code: then [✓✗].*, now [✓✗]")
        self.assertEqual(len(tasks()) - before, 2, "one session a side")
        for bad in ((res["with"], self.calc), (res["without"], "nope"), ("duel-none-foreman-missing", self.calc)):
            p = self.fm("bench", "repro", *bad, env=self.env, check=False)
            self.assertNotEqual(p.returncode, 0, bad)


class SoakStorm(_Bench):  # T-1259
    def test_a_soak_storm_compacts_early_and_a_sweep_runs_once_per_window(self):
        cfg = self.safe_config_dir()  # T-6020: not under /tmp
        env = dict(self.env, CLAUDE_CONFIG_DIR=cfg)
        self.fm("bench", "build", env=env)
        res = json.loads(self.fm("bench", "soak", "--ids", self.calc, "--turns", "1", "--compact-at", "20000",
                                 "--json", env=env).stdout)
        self.assertEqual((res["cases"][0]["compactions"], res["compact_at"]), (2, 20000))
        self.assertEqual({s["window"] for s in self.sessions()}, {"20000"}, "every soak session compacts early")
        self.assertFalse(os.path.exists(os.path.join(cfg, "projects", "-tmp-replay")), "the kept session is removed")
        out = json.loads(self.fm("bench", "soak", "--ids", self.calc, "--turns", "1", "--sweep", "20000,40000",
                                 "--json", env=env).stdout)
        self.assertEqual([w["compact_at"] for w in out["windows"]], [20000, 40000])
        self.assertTrue(all(w["cases"][0]["compactions"] == 2 for w in out["windows"]))
        text = self.fm("bench", "soak", "--ids", self.calc, "--turns", "1", "--sweep", "30000", env=env).stdout
        self.assertRegex(text, r"window 30000: \d/1 passed · \d+ repeat\(s\) · 2 compaction\(s\)")
        for bad in (["--sweep", "10"], ["--sweep", "a,b"], ["--compact-at", "20000", "--sweep", "30000"],
                    ["--compact-at", "0"]):
            p = self.fm("bench", "soak", "--ids", self.calc, *bad, env=env, check=False)
            self.assertNotEqual(p.returncode, 0, bad)


class Marathon(_Bench):
    """T-1242: a long session measured as one: both arms forced to compact early, the compactions counted."""

    def test_a_duel_can_force_early_compaction_on_both_arms_and_counts_it(self):
        cfg = self.safe_config_dir()  # T-6020: not under /tmp
        env = dict(self.env, CLAUDE_CONFIG_DIR=cfg)
        self.fm("bench", "build", env=env)
        res = json.loads(self.fm("bench", "duel", "--vs", "none", "--compact-at", "40000", "--max", "1", "--json",
                                 env=env).stdout)
        tasks = [s for s in self.calls() if s["args"][:1] == ["-p"] and s["args"][1].startswith("Task:")]
        self.assertEqual([s["window"] for s in tasks], ["40000", "40000"], "both arms")
        self.assertTrue(all("--no-session-persistence" not in s["args"] for s in tasks), "kept, to count compactions")
        # --vs none: "without" is Foreman's arm (the bare one is the reference it has to beat)
        arm = json.loads(read_text(os.path.join(c.find_project(self.repo).dir, "bench", "results",
                                                res["without"] + ".json")))
        self.assertEqual(arm["cases"][0]["compactions"], 2)
        self.assertEqual(arm["compact_at"], 40000)
        self.assertFalse(os.path.exists(os.path.join(cfg, "projects", "-tmp-replay")), "the kept session is removed")

    def test_a_marathon_duel_lets_foremans_arm_run_to_its_cap_and_still_grades_it(self):
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "duel", "--vs", "none", "--marathon", "3", "--budget", "0.4", "--max", "1",
                                 "--json", env=self.env, check=False).stdout)
        tasks = [s for s in self.calls() if s["args"][:1] == ["-p"] and s["args"][1].startswith("Task:")]
        self.assertEqual([s["marathon"] for s in tasks], ["3", None], "Foreman's arm only")
        arm = json.loads(read_text(os.path.join(c.find_project(self.repo).dir, "bench", "results",
                                                res["without"] + ".json")))
        x = arm["cases"][0]
        self.assertEqual((x["capped"], x["error"]), (True, None), "the cap ends a marathon; it isn't a crash")
        self.assertIn("titles", x["diag"])

    def test_a_run_can_force_early_compaction_too(self):
        cfg = self.safe_config_dir()  # T-6020: not under /tmp
        env = dict(self.env, CLAUDE_CONFIG_DIR=cfg)
        self.fm("bench", "build", env=env)
        res = json.loads(self.fm("bench", "run", "--compact-at", "30000", "--max", "1", "--json", env=env,
                                 check=False).stdout)
        tasks = [s for s in self.calls() if s["args"][:1] == ["-p"] and s["args"][1].startswith("Task:")]
        self.assertEqual([s["window"] for s in tasks], ["30000"])
        self.assertEqual(res["cases"][0]["compactions"], 2)
