"""fm bench (T-0212): finished tasks replayed as benchmark cases — the repo at the task commit's parent, the original
request, a fresh Foreman state, the candidate plugin; graded by the task's own tests and verify commands."""
import glob
import json
import os
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase, read_text

from sampo import bench
from sampo import core as c

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.304 (Claude Code)")
    sys.exit(0)
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd(), "state": os.environ.get("FOREMAN_STATE"),
                        "path": os.environ.get("PATH", "").split(os.pathsep)[0],
                        "creds": [k for k in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY") if k in os.environ],
                        "login": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
                        "cpus": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
                        "cpu_count": os.environ.get("PYTHON_CPU_COUNT"), "nice": os.nice(0)}) + "\n")
if os.environ.get("STUB_CHILD"):  # what the session starts: its CPUs as a child of its own sees them
    import subprocess
    seen = subprocess.run([sys.executable, "-c", "import os; print(sorted(os.sched_getaffinity(0)))"],
                          capture_output=True, text=True).stdout
    with open(os.environ["STUB_LOG"], "a") as f:
        f.write(json.dumps({"child": json.loads(seen)}) + "\n")
if os.environ.get("STUB_DIAG"):  # behave a little like a Foreman session: a task, and one guard block
    import subprocess
    subprocess.run(["fm", "task", "new", "Fix double", "--type", "FIX", "--tier", "S"], capture_output=True)
    with open(os.path.join(os.environ["FOREMAN_STATE"], "events.jsonl"), "a") as f:
        f.write(json.dumps({"kind": "guard_block", "category": "rm-outside", "tool": "Bash"}) + "\n")
if os.environ.get("STUB_ALTERNATE"):  # solve every other attempt: a flaky candidate
    n = os.path.join(os.path.dirname(os.environ["STUB_LOG"]), "attempts")
    k = int(open(n).read()) if os.path.exists(n) else 0
    open(n, "w").write(str(k + 1))
    if k % 2 == 0:
        os.environ["STUB_SOLVE"] = "1"
if os.environ.get("STUB_GUT"):  # does the new work, breaks the old, and guts the existing test that would say so
    with open("calc.py", "w") as f:
        f.write("def half(x):\n    return x\n\n\ndef third(x):\n    return x / 3\n")
    with open("tests/test_half.py", "w") as f:
        f.write("print('ok')\n")
if os.environ.get("STUB_EARLY"):  # T-3365: stop early (no claim of done) this many turns, then finish
    n = os.path.join(os.path.dirname(os.environ["STUB_LOG"]), "early")
    k = int(open(n).read()) if os.path.exists(n) else 0
    open(n, "w").write(str(k + 1))
    sid = next((args[i + 1] for i, a in enumerate(args) if a in ("--resume", "--session-id")), "s-0")
    if k < int(os.environ["STUB_EARLY"]):
        if os.environ.get("CLAUDE_CONFIG_DIR") and not os.environ.get("STUB_NO_TRANSCRIPT"):  # T-3364: a run with no
            d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "p")  # transcript is unaudited: keep one
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, sid + ".jsonl"), "a") as f:
                if k == 0 and os.environ.get("STUB_ASKTOOL"):  # the session's transcript: one AskUserQuestion call
                    f.write(json.dumps({"type": "assistant", "message": {"content": [
                        {"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion", "input": {}}]}}) + "\n")
                else:
                    f.write(json.dumps({"type": "user", "message": {"content": "ok"}}) + "\n")
        if os.environ.get("STUB_SOLVE"):
            with open("calc.py", "w") as f:
                f.write("def double(x):\n    return 2 * x\n")
        text = ["I made a start. Should I use tabs or spaces?", "Still working through it."][k % 2]
        print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.1 * (k + 1), "num_turns": 3,
                          "session_id": sid, "result": text}))  # claude's cost is the session's total so far
        sys.exit(0)
    print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.42 + 0.1 * k, "num_turns": 7,
                      "session_id": sid,
                      "result": "All done: the work is complete."}))
    sys.exit(0)
model = args[args.index("--model") + 1] if "--model" in args else ""
if os.environ.get("STUB_SOLVE") or model and model in os.environ.get("STUB_SOLVE_MODELS", "").split(","):
    with open("calc.py", "w") as f:
        f.write("def double(x):\n    return 2 * x\n")
cost = {"haiku": 0.05, "sonnet": 0.3, "opus": 1.2}.get(model, 0.42)
sid = "stub-session"
if os.environ.get("STUB_COMMIT"):  # an arm that commits its work, as Sampo's rules make it
    import subprocess
    subprocess.run(["git", "add", "-A"], capture_output=True)
    subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "work"], capture_output=True)
if os.environ.get("CLAUDE_CONFIG_DIR") and not os.environ.get("STUB_NO_TRANSCRIPT"):  # the kept transcript
    d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "p")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".jsonl"), "w") as f:  # STUB_LEAK: a tool call that reaches for the answers
        f.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "cat " + os.environ.get("STUB_LEAK", "x")}}]}}) + "\n")
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": cost, "num_turns": 7, "duration_ms": 1234,
                  "result": "done", "session_id": sid}))
'''


def glob_transcripts(config_dir):
    import glob
    return glob.glob(os.path.join(config_dir, "projects", "*", "*.jsonl"))


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


class Bench(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log,
                    "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "cc")}  # T-3364: where the stub keeps its transcript
        self.fm("init")
        self.fm("claude-bench", "on")  # T-5584: a benchmark calls a Claude model only once the user turns it on
        os.environ["FOREMAN_PROJECT"] = c.find_project(self.repo).slug  # T-5584: run_case runs in this process
        self.write("calc.py", "def double(x):\n    return x + x + 1\n")
        git(self.repo, "add", "calc.py")
        git(self.repo, "commit", "-qm", "calc")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.tid = self.finished_task("double() is off by one", "PYTHONPATH=. python3 tests/test_calc.py")
        self.write("calc.py", "def double(x):\n    return x * 2\n")
        self.write("tests/test_calc.py", "from calc import double\nassert double(3) == 6, double(3)\nprint('ok')\n")
        git(self.repo, "add", "-A", "calc.py", "tests")
        git(self.repo, "commit", "-qm", f"double() is right ({self.tid})")

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def finished_task(self, title, verify):
        tid = json.loads(self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--ac", f"fixed :: {verify}",
                                 "--step", "fix it", "--json").stdout)["id"]
        self.fm("task", "set", tid, "--section", "Raw request", "--text", f"> {title}; fix it")
        self.fm("focus", tid)
        self.fm("task", "step", tid, "done", "1", "--evidence", "x", "ok")
        self.fm("task", "ac", tid, "check", "1", "--evidence", "x", "ok")
        self.fm("task", "audit", tid, "self", "x", "ok")
        self.fm("task", "set", tid, "--section", "Regression test", "--text", "none: fixture")
        self.fm("task", "done", tid)
        return tid

    def calls(self):
        return [json.loads(l) for l in read_text(self.log).splitlines()]

    def test_build_keeps_cases_that_fail_before_and_pass_after(self):
        res = json.loads(self.fm("bench", "build", "--json", env=self.env).stdout)
        self.assertEqual([x["id"] for x in res["cases"]], [self.tid])
        case = res["cases"][0]
        self.assertEqual(case["base"], self.base)
        self.assertEqual(case["tests"], ["tests/test_calc.py"])
        self.assertEqual(case["verify"], ["PYTHONPATH=. python3 tests/test_calc.py"])
        self.assertIn("off by one", case["prompt"])
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0, "validation worktrees are removed")

    def test_a_case_that_passes_without_the_change_is_dropped(self):
        tid = self.finished_task("add a readme", "true")
        self.write("README.md", "hi\n")
        self.write("tests/test_readme.py", "print('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", f"readme ({tid})")
        res = json.loads(self.fm("bench", "build", "--json", env=self.env).stdout)
        self.assertNotIn(tid, [x["id"] for x in res["cases"]])
        self.assertIn(tid, " ".join(res["skipped"]))

    def test_tests_the_commit_deleted_are_not_hidden_tests(self):
        # T-0212 review: a deleted test made `git checkout` fail and the grade ran without the hidden tests
        self.write("tests/test_old.py", "print('old')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "old test")
        tid = self.finished_task("triple() exists", "PYTHONPATH=. python3 tests/test_triple.py")
        os.remove(os.path.join(self.repo, "tests", "test_old.py"))
        self.write("calc.py", "def double(x):\n    return x * 2\n\n\ndef triple(x):\n    return 3 * x\n")
        self.write("tests/test_triple.py", "from calc import triple\nassert triple(2) == 6\nprint('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", f"triple ({tid})")
        res = json.loads(self.fm("bench", "build", "--ids", tid, "--json", env=self.env).stdout)
        self.assertEqual(res["cases"][0]["tests"], ["tests/test_triple.py"])
        self.fm("bench", "build", "--ids", self.tid, env=self.env)
        cases = json.loads(self.fm("bench", "list", "--json", env=self.env).stdout)["cases"]
        self.assertEqual(sorted(x["id"] for x in cases), sorted([tid, self.tid]), "--ids adds, it doesn't replace")

    def test_a_crashing_case_keeps_the_others_results(self):
        self.fm("bench", "build", env=self.env)
        broken = os.path.join(self.tmp, "broken")
        os.makedirs(os.path.join(broken, ".claude-plugin"))
        os.makedirs(os.path.join(broken, "hooks"))
        os.makedirs(os.path.join(broken, "lib"))
        for rel, text in ((".claude-plugin/plugin.json", '{"name": "broken"}'), ("lib/fmguard.py", ""),
                          ("hooks/hooks.json", '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}')):
            with open(os.path.join(broken, rel), "w") as f:
                f.write(text)
        out = self.fm("bench", "run", "--plugin", broken, "--label", "broken", "--json", env=self.env, check=False)
        res = json.loads(out.stdout)
        self.assertFalse(res["cases"][0]["pass"])
        self.assertTrue(res["cases"][0]["error"], "no bin/fm: recorded as the case's error, not a crash")
        self.assertIn("broken", self.fm("bench", "list", env=self.env).stdout, "the results file was written")
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0)

    def test_a_replay_says_what_its_foreman_did(self):
        # T-0220: a failure pinned to a stage: did it plan, verify, get blocked?
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "run", "--label", "diag", "--json", env=dict(self.env, STUB_DIAG="1"),
                                 check=False).stdout)
        diag = res["cases"][0]["diag"]
        self.assertEqual(list(diag["tasks"].values()), ["FIX S planned"])
        self.assertEqual(diag["guard_blocks"], {"rm-outside": 1})
        self.assertGreaterEqual(diag["ledger"].get("capture", 0) + diag["ledger"].get("task_new", 0), 1)
        out = self.fm("bench", "show", "diag", env=self.env).stdout
        self.assertIn("FIX S planned", out)
        self.assertIn("rm-outside", out)

    def test_a_result_names_the_claude_code_it_ran_on(self):
        # T-1255: results from different Claude Code releases weren't comparable without it
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "run", "--label", "ver", "--json", env=self.env, check=False).stdout)
        self.assertEqual(res["claude"], "2.1.304")

    def test_repeats_give_a_pass_rate(self):
        # T-0221: one run per arm is noise; a pass rate is a measurement
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "run", "--runs", "2", "--label", "flaky", "--json",
                                 env=dict(self.env, STUB_ALTERNATE="1"), check=False).stdout)
        case = res["cases"][0]
        self.assertEqual((case["runs"], case["pass_rate"]), (2, 0.5))
        self.assertEqual(case["cost_usd"], 0.42)
        self.assertEqual(len(case["attempts"]), 2)

    def run_early(self, early, label, **env):
        cc = os.path.join(self.tmp, "cc")
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "run", "--label", label, "--json", check=False, timeout=120,
                                 env=dict(self.env, STUB_EARLY=str(early), STUB_SOLVE="1", CLAUDE_CONFIG_DIR=cc,
                                          **env)).stdout)
        return res["cases"][0], cc

    def prompts(self):
        return [x["args"][x["args"].index("-p") + 1] for x in self.calls()]

    def test_unattended_a_session_that_stops_early_twice_gets_two_nudges_and_the_run_records_them(self):
        # T-3365: the user is away; a stop without a claim of done is answered with a fixed "use your defaults"
        case, cc = self.run_early(2, "early", STUB_ASKTOOL="1")
        calls = self.calls()
        self.assertEqual(len(calls), 3, "the first turn and two nudges")
        self.assertEqual(self.prompts()[1:], [bench.NUDGE, bench.NUDGE])
        self.assertIn("away", bench.NUDGE)
        self.assertIn("defaults", bench.NUDGE)
        self.assertNotIn("--resume", calls[0]["args"])
        sids = [x["args"][x["args"].index("--resume") + 1] for x in calls[1:]]
        self.assertEqual(sids, [calls[0]["args"][calls[0]["args"].index("--session-id") + 1]] * 2,
                         "the same session each time")
        self.assertNotIn("--no-session-persistence", calls[0]["args"], "a nudge resumes the kept session")
        self.assertEqual(case["unattended"], {"interventions": 2, "stalls": 2, "questions": 2, "ask_tool": 1,
                                              "ask_reply": 1})
        self.assertTrue(case["pass"], case)
        self.assertEqual((case["cost_usd"], case["turns"]), (0.62, 13), "the session's total cost, every turn's turns")
        self.assertEqual(glob_transcripts(cc), [], "the kept session's transcript is removed after")

    def test_unattended_a_session_that_claims_done_is_not_nudged(self):
        case, _ = self.run_early(0, "doneearly")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(case["unattended"], {"interventions": 0, "stalls": 0, "questions": 0, "ask_tool": 0,
                                              "ask_reply": 0})

    def test_unattended_at_most_three_nudges(self):
        case, _ = self.run_early(9, "stuck")
        self.assertEqual(len(self.calls()), 4, "the first turn and three nudges, then it is left")
        self.assertEqual((case["unattended"]["interventions"], case["unattended"]["stalls"]), (3, 4))
        self.assertIsNone(case["error"], "the end of the nudges is a finding, not a crash")

    def test_unattended_claims_of_done(self):
        for text, want in (("All done.", True), ("The work is complete.", True), ("Finished: tests pass.", True),
                           ("I made a start.", False), ("Not done yet; the tests still fail.", False),
                           ("It isn't finished.", False), ("incomplete", False), ("", False), (None, False),
                           ("Done\n\n```\nfoo?\n```", True),
                           ("The analysis is complete. Shall I implement it?", False),
                           ("All done. Anything else?", False), ("**not** done", False), ("It isn\u2019t done.", False),
                           ("I couldn't get it done.", False), ("Not yet fully done.", False),
                           ("The tests did not fail and it is done.", True), ("I can't stop; it's all done.", True)):
            self.assertEqual(bench.claims_done(text), want, text)

    def test_unattended_a_question_is_a_reply_that_ends_asking(self):
        for text, want in (("Should I use tabs?", True), ("Plan below.\n\nShall I go ahead?\n", True),
                           ("Shall I go ahead? (yes/no)", False), ("Done.", False), (None, False),
                           ("Fixed.\n```\nx = a ? b : c?\n```", False), ("Which one?**", True)):
            self.assertEqual(bench.ends_in_question(text), want, text)

    def test_unattended_the_time_budget_ends_the_nudges(self):
        # the run's timeout is for every turn together: no time left, no nudge
        ticks = [0.0]  # the clock starts at 0, and every later look says an hour went
        turn = ({"total_cost_usd": 0.1, "result": "Still going.", "session_id": "s"}, None)
        with mock.patch.object(bench, "_turn", return_value=turn) as t, \
                mock.patch.object(bench.time, "monotonic", side_effect=lambda: ticks.pop() if ticks else 61 * 60.0):
            res, error = bench._session(self.repo, {}, ["plugin"], "go", None, None, 60, nudges=3)
        self.assertEqual(t.call_count, 1)
        self.assertEqual((res["unattended"]["interventions"], res["unattended"]["stalls"]), (0, 1))
        self.assertIsNone(error)

    @staticmethod
    def clock(*seconds):
        """A monotonic clock that reads these, then the last again."""
        vals = list(seconds)
        return lambda: vals.pop(0) if len(vals) > 1 else vals[0]

    def test_unattended_a_nudge_needs_a_tenth_of_the_timeout_left_and_at_least_a_minute(self):
        turn = ({"total_cost_usd": 0.1, "result": "Still going.", "session_id": "s"}, None)
        for timeout, elapsed, nudged in ((30, 28 * 60.0, False), (30, 26 * 60.0, True), (5, 4.5 * 60, False),
                                         (5, 3.5 * 60, True), (600, 541 * 60.0, False), (600, 530 * 60.0, True)):
            with mock.patch.object(bench, "_turn", return_value=turn) as t, \
                    mock.patch.object(bench.time, "monotonic", self.clock(0.0, elapsed, elapsed)):
                res, _ = bench._session(self.repo, {}, ["plugin"], "go", None, None, timeout, nudges=1)
            self.assertEqual((t.call_count, res["unattended"]["interventions"]), (2, 1) if nudged else (1, 0),
                             (timeout, elapsed))
            self.assertEqual(res["unattended"]["stalls"], 2 if nudged else 1)

    def test_unattended_a_timed_out_last_turn_keeps_the_session_and_the_last_result(self):
        # run_case counts compactions and removes the transcript by the result's session id
        stall = ({"total_cost_usd": 0.3, "result": "Still going.", "session_id": "s-1", "num_turns": 4}, None)
        out = ({}, "timed out after 5 min")
        with mock.patch.object(bench, "_turn", side_effect=[stall, out]) as t:
            res, error = bench._session(self.repo, {}, ["plugin"], "go", None, 1.0, 30, nudges=3)
        self.assertEqual((res["session_id"], res["result"], error), ("s-1", "Still going.", "timed out after 5 min"))
        self.assertEqual((res["total_cost_usd"], res["num_turns"], res["unattended"]["interventions"]), (0.3, 4, 1))
        first = ({}, "timed out after 30 min")  # not even the first turn answered: the id chosen for it stays
        with mock.patch.object(bench, "_turn", return_value=first) as t:
            res, _ = bench._session(self.repo, {}, ["plugin"], "go", None, 1.0, 30, nudges=3)
        self.assertEqual(res["session_id"], t.call_args.kwargs["sid"])

    def test_unattended_claude_s_cost_is_the_session_s_total_so_far_and_each_turn_gets_the_whole_budget(self):
        # measured on a real resumed haiku session: the second turn's total_cost_usd was the first's plus its own
        turns = [({"total_cost_usd": 0.1, "result": "Going.", "session_id": "s", "num_turns": 2}, None),
                 ({"total_cost_usd": 0.25, "result": "Done.", "session_id": "s", "num_turns": 3}, None)]
        with mock.patch.object(bench, "_turn", side_effect=turns) as t:
            res, error = bench._session(self.repo, {}, ["plugin"], "go", None, 1.0, 30, nudges=3)
        self.assertEqual((res["total_cost_usd"], res["num_turns"], error), (0.25, 5, None))
        self.assertEqual([x.args[5] for x in t.call_args_list], [1.0, 1.0], "the whole budget each turn")
        over = [({"total_cost_usd": 0.6, "result": "Going.", "session_id": "s"}, None),
                ({"total_cost_usd": 1.2, "result": "Going.", "session_id": "s"}, None)]
        with mock.patch.object(bench, "_turn", side_effect=over) as t:
            res, error = bench._session(self.repo, {}, ["plugin"], "go", None, 1.0, 30, nudges=3)
        self.assertEqual(t.call_count, 2, "no nudge once the budget is spent")
        self.assertIn("$1 budget cap", error, "the whole budget is named")

    def test_unattended_the_session_name_is_checked_before_it_is_a_glob(self):
        cc = os.path.join(self.tmp, "cc")
        os.makedirs(os.path.join(cc, "projects", "p"))
        path = os.path.join(cc, "projects", "p", "keep.jsonl")
        with open(path, "w") as f:
            f.write('{"subtype": "compact_boundary"}\n')
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cc}):
            for bad in ("*", "../p/keep", "ke?p", "k" * 81, "", None, "a/b"):
                self.assertEqual(bench._compactions(bad), 0, bad)
                self.assertTrue(os.path.exists(path), bad)
            self.assertEqual(bench._compactions("keep"), 1)
        self.assertFalse(os.path.exists(path))

    def test_unattended_both_arms_are_told_to_say_done_and_the_run_records_the_nudges_setting(self):
        self.fm("bench", "build", env=self.env)
        case = bench._load_cases(c.find_project(self.repo))[0]
        seen = []

        def fake(wt, env, plugins, prompt, model, budget, timeout, **kw):
            seen.append((prompt, kw.get("nudges")))
            return {"total_cost_usd": 0.1, "num_turns": 1, "result": "done"}, None
        p = c.find_project(self.repo)
        with mock.patch.object(bench, "_session", fake):
            for bare in (False, True):
                bench.run_case(p, case, c.PLUGIN_ROOT, bare=bare)
        self.assertEqual(len(seen), 2)
        for prompt, nudges in seen:
            self.assertIn("say 'done' when", prompt)
            self.assertEqual(nudges, bench.MAX_NUDGES)
        with mock.patch.object(bench, "_session", fake):
            bench.run_case(p, case, c.PLUGIN_ROOT, bare=True, nudges=0)
        self.assertEqual(seen[-1][1], 0)

    def test_unattended_nudges_zero_is_an_un_nudged_baseline_and_the_default_is_recorded(self):
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "run", "--nudges", "0", "--label", "plain", "--json",
                                 env=dict(self.env, STUB_EARLY="2", CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "cc")),
                                 check=False).stdout)  # T-3364: a transcript to audit
        self.assertEqual(res["nudges"], 0)
        self.assertEqual(len(self.calls()), 1, "one turn, nobody nudged")
        self.assertNotIn("unattended", res["cases"][0])
        res = json.loads(self.fm("bench", "run", "--label", "dflt", "--json", env=self.env, check=False).stdout)
        self.assertEqual(res["nudges"], bench.MAX_NUDGES)

    def test_unattended_counts_show_in_run_show_compare_and_gate(self):
        self.fm("bench", "build", env=self.env)
        un = {"interventions": 2, "stalls": 2, "questions": 3, "ask_tool": 1, "ask_reply": 2}
        for label, extra in (("ua", {"unattended": un}), ("ub", {})):
            folder = os.path.join(c.find_project(self.repo).dir, "bench", "results")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, label + ".json"), "w") as f:
                json.dump({"label": label, "cases": [dict(id="X-1", pass_rate=1.0, cost_usd=0.5, turns=3, runs=1,
                                                          seconds=9, error=None, **{"pass": True}, **extra)]}, f)
        want = "2 nudge(s) \u00b7 2 stall(s) \u00b7 3 question(s)"
        self.assertIn(want, self.fm("bench", "show", "ua").stdout)
        out = self.fm("bench", "compare", "ua", "ub").stdout
        self.assertIn(want, out)
        self.assertEqual(bench.unattended_text([{}]), "", "an old result has none to show")
        ok, lines = bench.gate(*(json.load(open(os.path.join(c.find_project(self.repo).dir, "bench", "results", x + ".json")))
                                 for x in ("ua", "ub")))
        self.assertTrue(any(want in x for x in lines), lines)

    def test_unattended_the_repeats_sum_their_counts(self):
        one = {"id": "X-1", "pass": True, "cost_usd": 0.1, "turns": 1, "seconds": 1, "error": None, "tests": None,
               "unattended": {"interventions": 1, "stalls": 2, "questions": 3, "ask_tool": 1, "ask_reply": 2}}
        with mock.patch.object(bench, "run_case", return_value=one):
            res = bench.run_case_n(None, {"id": "X-1"}, "plugin", runs=3)
        self.assertEqual(res["unattended"], {"interventions": 3, "stalls": 6, "questions": 9, "ask_tool": 3,
                                             "ask_reply": 6})
        self.assertEqual(len(res["attempts"]), 3)
        self.assertEqual(res["attempts"][0]["unattended"]["stalls"], 2, "each attempt keeps its own")

    def test_unattended_an_errored_turn_is_not_nudged(self):
        turn = ({}, "timed out after 1 min")
        with mock.patch.object(bench, "_turn", return_value=turn) as t:
            res, error = bench._session(self.repo, {}, ["plugin"], "go", None, None, 1, nudges=3)
        self.assertEqual((t.call_count, error), (1, "timed out after 1 min"))
        self.assertEqual(res["unattended"]["interventions"], 0)

    def test_unattended_without_nudges_a_session_is_one_plain_turn(self):
        turn = ({"result": "Still going."}, None)
        with mock.patch.object(bench, "_turn", return_value=turn) as t:
            res, _ = bench._session(self.repo, {}, ["plugin"], "go", None, None, 1)
        self.assertEqual(t.call_count, 1)
        self.assertNotIn("unattended", res, "the other callers' results are untouched")

    def write_result(self, label, cases, runs=1):
        folder = os.path.join(c.find_project(self.repo).dir, "bench", "results")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, label + ".json"), "w") as f:
            json.dump({"label": label, "cases": [dict(id=i, pass_rate=r, cost_usd=cost, turns=10, runs=runs,
                                                      **{"pass": r >= 0.5}) for i, r, cost in cases]}, f)

    def write_attempts(self, label, cases):
        """cases: (id, [(passed, cost)] per attempt) — a result as fm bench run --runs N saves it."""
        folder = os.path.join(c.find_project(self.repo).dir, "bench", "results")
        os.makedirs(folder, exist_ok=True)
        rows = []
        for i, tries in cases:
            rate = sum(p for p, _ in tries) / len(tries)
            rows.append({"id": i, "runs": len(tries), "pass_rate": rate, "pass": rate >= 0.5, "turns": 10,
                         "cost_usd": round(sum(x for _, x in tries) / len(tries), 4),
                         "attempts": [{"pass": p, "cost_usd": x} for p, x in tries]})
        with open(os.path.join(folder, label + ".json"), "w") as f:
            json.dump({"label": label, "cases": rows}, f)

    def test_the_gate_lets_through_only_no_worse_candidates(self):
        # single runs (T-1260: unchanged): any lost pass fails, and a flip is flagged as possible noise
        self.write_result("live", [("T-1", 1.0, 1.0), ("T-2", 0.0, 1.0)])
        self.write_result("same", [("T-1", 1.0, 1.1), ("T-2", 0.0, 1.0)])
        self.write_result("worse", [("T-1", 0.0, 1.0), ("T-2", 0.0, 1.0)])
        self.write_result("pricey", [("T-1", 1.0, 1.5), ("T-2", 1.0, 1.5)])
        self.assertEqual(self.fm("bench", "gate", "live", "same", env=self.env, check=False).returncode, 0)
        worse = self.fm("bench", "gate", "live", "worse", env=self.env, check=False)
        self.assertEqual(worse.returncode, 1)
        self.assertIn("T-1", worse.stdout)
        self.assertIn("may be noise", worse.stdout)
        self.assertNotIn("±", worse.stdout)
        self.assertNotIn("within noise", worse.stdout)
        pricey = self.fm("bench", "gate", "live", "pricey", env=self.env, check=False)
        self.assertEqual(pricey.returncode, 1, "more passes don't buy 50% more cost")
        self.assertIn("cost", pricey.stdout)

    def test_the_gate_calls_a_drop_inside_the_noise_band_noise(self):
        # T-1260: with --runs 3 a lost attempt and a dearer one are inside the spread the attempts show
        # T-1464: T-3 flips on both sides, so the lost attempt is under 1 SE (over it reads inconclusive)
        self.write_attempts("live", [("T-1", [(True, 1.0), (True, 1.0), (True, 1.0)]),
                                     ("T-2", [(True, 1.0), (True, 1.0), (True, 1.0)]),
                                     ("T-3", [(True, 1.0), (False, 1.0), (True, 1.0)])])
        self.write_attempts("noisy", [("T-1", [(True, 1.0), (False, 1.6), (True, 1.0)]),
                                      ("T-2", [(True, 1.0), (True, 1.6), (True, 1.0)]),
                                      ("T-3", [(True, 1.0), (False, 1.6), (True, 1.0)])])
        r = self.fm("bench", "gate", "live", "noisy", env=self.env, check=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        head = r.stdout.splitlines()[0]
        self.assertRegex(head, r"passes 2\.67 → 2\.33 \(-0\.33 ± [\d.]+\)")
        self.assertRegex(head, r"cost/case \$1\.00 → \$1\.20 \(\+\$0\.20 ± \$[\d.]+\)")
        self.assertIn("fewer passes within noise", r.stdout)
        self.assertIn("cost per case up 20% within noise", r.stdout)
        self.assertNotIn("may be noise", r.stdout)

    def test_a_loss_between_one_and_two_se_is_inconclusive(self):
        # T-1464: 3 clean cases at 3 runs, one drops to 2/3: 0.33 is over 1 SE (0.27), inside 2 SE (0.54)
        self.write_attempts("live", [("T-1", [(True, 1.0)] * 3), ("T-2", [(True, 1.0)] * 3)])
        self.write_attempts("cand", [("T-1", [(True, 1.0), (False, 1.0), (True, 1.0)]), ("T-2", [(True, 1.0)] * 3)])
        r = self.fm("bench", "gate", "live", "cand", env=self.env, check=False)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("rerun T-1 with --runs 10", r.stdout)
        self.assertIn("few runs (3): a real loss of about 2 passes is caught about half the time", r.stdout)
        self.assertIn("✗ not proposed", r.stdout)
        # over 2 SE still fails outright; 5+ runs drop the power line
        flip = [(True, 1.0)] * 3 + [(False, 1.0)] * 2
        self.write_attempts("big", [("T-1", [(True, 1.0)] * 5), ("T-2", flip)])
        self.write_attempts("bigworse", [("T-1", [(False, 1.0)] * 5), ("T-2", flip)])
        w = self.fm("bench", "gate", "big", "bigworse", env=self.env, check=False)
        self.assertIn("outside noise", w.stdout)
        self.assertNotIn("few runs", w.stdout)

    def test_the_gate_fails_a_drop_outside_the_noise_band(self):
        self.write_attempts("live", [("T-1", [(True, 1.0)] * 3), ("T-2", [(True, 1.0)] * 3),
                                     ("T-3", [(True, 1.0), (False, 1.0), (True, 1.0)])])
        self.write_attempts("broken", [("T-1", [(False, 1.0)] * 3), ("T-2", [(False, 1.0)] * 3),
                                       ("T-3", [(True, 1.0), (False, 1.0), (True, 1.0)])])
        self.write_attempts("dear", [("T-1", [(True, 2.0), (True, 2.2), (True, 1.8)]), ("T-2", [(True, 2.0)] * 3),
                                     ("T-3", [(True, 2.0), (False, 2.0), (True, 2.0)])])
        broken = self.fm("bench", "gate", "live", "broken", env=self.env, check=False)
        self.assertEqual(broken.returncode, 1, broken.stdout)
        self.assertIn("✗ fewer passes", broken.stdout)
        self.assertIn("outside noise", broken.stdout)
        self.assertIn("±", broken.stdout)
        dear = self.fm("bench", "gate", "live", "dear", env=self.env, check=False)
        self.assertEqual(dear.returncode, 1, dear.stdout)
        self.assertIn("✗ cost per case up 100%", dear.stdout)

    def test_old_repeated_results_without_attempt_costs_keep_the_cost_tolerance(self):
        # results saved before per-attempt costs: a pass band from runs and pass rate, no cost band
        self.write_result("live", [("T-1", 1.0, 1.0)], runs=3)
        self.write_result("cand", [("T-1", 2 / 3, 1.3)], runs=3)
        r = self.fm("bench", "gate", "live", "cand", env=self.env, check=False)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("likely a loss: rerun T-1", r.stdout)  # T-1464: 0.33 is over 1 SE of 0.54/2
        self.assertIn("✗ cost per case up 30% (over 15%)", r.stdout)

    def test_grading_restores_the_reference_tests_an_arm_edited(self):
        # T-1237 review: only the commit's own test files were restored, so gutting another existing test passed
        self.write("calc.py", "def half(x):\n    return x / 2\n")
        self.write("tests/test_half.py", "from calc import half\nassert half(4) == 2\nprint('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "half")
        self.write("calc.py", "def half(x):\n    return x / 2\n\n\ndef third(x):\n    return x / 3\n")
        self.write("tests/test_third.py", "from calc import third\nassert third(9) == 3\nprint('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Add third()\n\nthird(x) divides by three; half() keeps working.")
        sha = git(self.repo, "rev-parse", "HEAD")
        self.fm("bench", "build", "--commits", "HEAD~1..HEAD", "--verify",
                "PYTHONPATH=. python3 tests/test_half.py && PYTHONPATH=. python3 {tests}", env=self.env)
        res = json.loads(self.fm("bench", "run", "--ids", f"C-{sha[:10]}", "--label", "gut", "--json",
                                 env=dict(self.env, STUB_GUT="1"), check=False).stdout)
        self.assertFalse(res["cases"][0]["pass"], "the reference test_half.py runs, not the gutted one")

    def test_any_commit_with_a_test_and_a_fix_is_a_case(self):
        # T-0222: bench from a repo's own history, no Foreman brief needed
        self.write("calc.py", "def double(x):\n    return x * 2\n\n\ndef half(x):\n    return x / 2\n")
        self.write("tests/test_half.py", "from calc import half\nassert half(4) == 2\nprint('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Add half()\n\nhalf(x) divides by two.\n- half(0) is 0")
        sha = git(self.repo, "rev-parse", "HEAD")
        self.write("tests/test_more.py", "print('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "more tests")
        res = json.loads(self.fm("bench", "build", "--commits", "HEAD~2..HEAD", "--verify",
                                 "PYTHONPATH=. python3 {tests}", "--json", env=self.env).stdout)
        self.assertEqual([x["id"] for x in res["cases"]], [f"C-{sha[:10]}"])
        case = res["cases"][0]
        self.assertEqual(case["verify"], ["PYTHONPATH=. python3 tests/test_half.py"])
        self.assertIn("half(x) divides by two", case["prompt"])
        self.assertIn("two.\n- half(0)", case["prompt"], "T-1231: a spec keeps its lines (it was one run-on line)")
        self.assertIn("tests only", " ".join(res["skipped"]))
        self.fm("bench", "build", env=self.env)
        ids = [x["id"] for x in json.loads(self.fm("bench", "list", "--json", env=self.env).stdout)["cases"]]
        self.assertIn(f"C-{sha[:10]}", ids, "a brief build keeps the commit cases")
        self.assertIn(self.tid, ids)

    def test_together_makes_one_case_asking_for_a_chain_of_requests(self):
        # T-1242: a long session measured as one: every request of a range, graded by all their hidden tests
        start = git(self.repo, "rev-parse", "HEAD")
        self.write("calc.py", "def double(x):\n    return x * 2\n\n\ndef half(x):\n    return x / 2\n")
        self.write("tests/test_half.py", "from calc import half\nassert half(4) == 2\nprint('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Add half()\n\nhalf(x) divides by two.")
        self.write("calc.py", "def double(x):\n    return x * 2\n\n\ndef half(x):\n    return x / 2\n\n\n"
                              "def third(x):\n    return x / 3\n")
        self.write("tests/test_third.py", "from calc import third\nassert third(9) == 3\nprint('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Add third()\n\nthird(x) divides by three.")
        last = git(self.repo, "rev-parse", "HEAD")
        verify = "for t in {tests}; do PYTHONPATH=. python3 $t || exit 1; done"
        res = json.loads(self.fm("bench", "build", "--commits", "HEAD~2..HEAD", "--verify", verify, "--together",
                                 "--json", env=self.env).stdout)
        [case] = [x for x in res["cases"] if x["id"].startswith("M-")]
        self.assertEqual((case["id"], case["base"], case["commit"]), (f"M-{last[:10]}", start, last))
        self.assertEqual(case["tests"], ["tests/test_half.py", "tests/test_third.py"])
        p = case["prompt"]
        self.assertLess(p.index("## Request 1"), p.index("divides by two"))
        self.assertLess(p.index("divides by two"), p.index("## Request 2"))
        self.assertLess(p.index("## Request 2"), p.index("divides by three"))
        self.assertEqual(len(case["requests"]), 2)
        self.write("tests/test_more.py", "print('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "more tests")
        r = self.fm("bench", "build", "--commits", "HEAD~3..HEAD", "--verify", verify, "--together", env=self.env,
                    check=False)
        self.assertNotEqual(r.returncode, 0, "a commit that isn't a case would leave its change unasked")
        self.assertIn("every commit", r.stderr)

    def test_test_counts_read_unittest_and_pytest_summaries(self):
        from sampo import bench
        self.assertEqual(bench.count_tests(["Ran 12 tests in 0.1s / FAILED (failures=2, errors=1)",
                                              "3 failed, 9 passed in 0.2s"]), (18, 24))
        self.assertEqual(bench.count_tests(["Ran 5 tests in 0.0s / OK"]), (5, 5))
        self.assertIsNone(bench.count_tests(["ok"]))

    def test_compactions_are_counted_from_entries_not_quotes(self):
        # T-1242 review: a transcript line quoting "compact_boundary" (reading fmbench.py, say) counted as one
        from sampo import bench
        cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(os.path.join(cfg, "projects", "p"))
        with open(os.path.join(cfg, "projects", "p", "s-9.jsonl"), "w") as f:
            f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": 'it counts "compact_boundary" lines'}}) + "\n")
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        try:
            self.assertEqual(bench._compactions("s-9"), 1)
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR") if old is None else os.environ.update(CLAUDE_CONFIG_DIR=old)

    def test_failures_are_counted_per_request(self):
        from sampo import bench
        case = {"requests": [{"title": "INI", "tests": ["tests/test_ini.py"]},
                             {"title": "Invoice", "tests": ["tests/test_invoice.py", "tests/test_text.py"]}]}
        self.assertEqual(bench.per_request(case, ["test_ini.ParseTest.test_a", "tests.test_ini.ParseTest.test_b",
                                                    "test_text.WrapTest.test_c"]),
                         [{"title": "INI", "failed": 2}, {"title": "Invoice", "failed": 1}])

    def test_grading_names_the_hidden_tests_that_failed(self):
        from sampo import bench
        from sampo import core as c
        out = ("FAIL: test_split (test_lexer.LexerTest.test_split)\nERROR: test_x (tests.test_ini.IniTest)\n"
               "FAILED tests/test_a.py::test_b - assert 1 == 2\n")
        failed, orig = [], c.run_command
        c.run_command = lambda *a, **k: (1, out)
        try:
            bench._safe_git, safe = (lambda *a: subprocess.CompletedProcess([], 0, "", "")), bench._safe_git
            try:
                bench._grade(self.repo, {"tests": [], "commit": "HEAD", "verify": ["x"]}, {}, failed=failed)
            finally:
                bench._safe_git = safe
        finally:
            c.run_command = orig
        self.assertEqual(failed, ["test_lexer.LexerTest.test_split", "tests.test_ini.IniTest.test_x",
                                  "tests/test_a.py::test_b"])

    def test_models_recommends_the_cheapest_that_passes(self):
        # T-0223: which model each tier needs, measured on the project's own work
        self.fm("bench", "build", env=self.env)
        res = json.loads(self.fm("bench", "models", "--models", "haiku,sonnet,opus", "--json",
                                 env=dict(self.env, STUB_SOLVE_MODELS="sonnet,opus")).stdout)
        self.assertEqual(res["recommend"], {"S": "sonnet"}, res)
        self.fm("bench", "models", "--models", "haiku,sonnet", "--save", env=dict(self.env, STUB_SOLVE_MODELS="haiku,sonnet"))
        self.assertEqual(c.read_meta(c.find_project(self.repo)).get("run_models", {}).get("S"), "haiku")

    def test_a_plugin_without_the_guard_or_credentials_never_runs(self):
        # security review: replays run in bypass mode like the user's sessions, so the guard must come with them
        self.fm("bench", "build", env=self.env)
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(os.path.join(bare, ".claude-plugin"))
        with open(os.path.join(bare, ".claude-plugin", "plugin.json"), "w") as f:
            f.write('{"name": "bare"}')
        out = self.fm("bench", "run", "--plugin", bare, env=self.env, check=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("guard", out.stdout + out.stderr)
        self.assertFalse(os.path.exists(self.log), "no session started")
        self.fm("bench", "run", "--label", "creds", env=dict(self.env, GITHUB_TOKEN="ghp_x", AWS_SECRET_ACCESS_KEY="y",
                                                              CLAUDE_CODE_OAUTH_TOKEN="keep", STUB_SOLVE="1"))
        self.assertEqual(self.calls()[-1]["creds"], [], "cloud and forge credentials stay behind")
        self.assertEqual(self.calls()[-1]["login"], "keep", "claude's own login goes along")

    def test_run_replays_in_isolation_and_grades_by_hidden_tests(self):
        self.fm("bench", "build", env=self.env)
        fail = json.loads(self.fm("bench", "run", "--label", "nofix", "--json", env=self.env, check=False).stdout)
        self.assertFalse(fail["cases"][0]["pass"], "the stub changed nothing: the hidden test fails")
        good = json.loads(self.fm("bench", "run", "--label", "fix", "--json",
                                  env=dict(self.env, STUB_SOLVE="1")).stdout)
        case = good["cases"][0]
        self.assertTrue(case["pass"], case)
        self.assertEqual((case["cost_usd"], case["turns"]), (0.42, 7))
        call = self.calls()[-1]
        self.assertIn("--plugin-dir", call["args"])
        snap = call["args"][call["args"].index("--plugin-dir") + 1]
        self.assertIn("fm-bench-plugin-", snap, "T-1231: a snapshot of this Foreman, not the live folder")
        self.assertIn("off by one", " ".join(call["args"]))
        self.assertNotEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.repo))
        self.assertTrue(call["state"] and not call["state"].startswith(c.state_dir()), "a fresh Foreman state")
        self.assertEqual(call["path"], os.path.join(snap, "bin"), "the candidate's fm comes first")
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0, "the replay worktree is removed")
        self.assertEqual(read_text(os.path.join(self.repo, "calc.py")), "def double(x):\n    return x * 2\n")
        out = self.fm("bench", "compare", "nofix", "fix", env=self.env).stdout
        self.assertIn("0/1", out)
        self.assertIn("1/1", out)

    def test_the_case_commit_is_unreachable_from_the_arms_tree(self):
        # T-3364: a linked worktree shares the repo's objects: git show <case commit> gave the answers away
        from sampo import bench
        commit = git(self.repo, "rev-parse", "HEAD")
        with bench._fresh(self.repo, self.base) as (tmp, wt, base):
            self.assertEqual(git(wt, "log", "--format=%H").count("\n"), 0, "one commit")
            self.assertEqual(git(wt, "remote"), "", "no remote")
            probe = subprocess.run(["git", "-C", wt, "cat-file", "-e", commit], capture_output=True)
            self.assertNotEqual(probe.returncode, 0)
            self.assertIn("x + x + 1", read_text(os.path.join(wt, "calc.py")))
            # an arm that commits leaves HEAD past the base: the judge's diff must start at the base sha
            self.assertEqual(base, git(wt, "rev-parse", "HEAD"))
            with open(os.path.join(wt, "calc.py"), "w") as f:
                f.write("def double(x):\n    return 2 * x\n")
            git(wt, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qam", "work")
            self.assertEqual(git(wt, "diff", "HEAD"), "")
            self.assertIn("2 * x", git(wt, "diff", base))

    def test_the_judge_diffs_from_the_base_sha_after_an_arm_commits(self):
        from unittest import mock
        from sampo import bench
        seen = []
        case = {"id": self.tid, "base": self.base, "prompt": "fix", "judge": "rubric", "tests": [], "verify": []}
        env = dict(self.env, STUB_COMMIT="1", STUB_SOLVE="1")
        with mock.patch.dict(os.environ, env), mock.patch.object(
                bench, "_judge", lambda p_, wt, cs, reply: (seen.append((cs["base"], wt)) or (5, [], None))):
            res = bench.run_case(c.find_project(self.repo), case, c.PLUGIN_ROOT)
        self.assertEqual(len(seen), 1, res)
        self.assertNotIn(seen[0][0], ("HEAD", self.base), "the fresh tree's own base sha, not HEAD")

    def test_a_transcript_touching_the_answers_is_flagged(self):
        from types import SimpleNamespace
        from sampo import bench
        p = SimpleNamespace(root=self.repo, dir=os.path.join(self.tmp, "state"))
        needles = bench._needles(p, {"id": "T-1"})
        path = os.path.join(self.tmp, "s.jsonl")
        for cmd, hit in ((f"cat {bench._cases_path(p)}", True), (f"ls {self.repo}/bench/spec-suite/T-1", True),
                         (f"git -C {self.repo} show HEAD", True), ("cat ~/.claude/projects/x/s.jsonl", True),
                         ("cat $HOME/.claude/foreman/MASTER.md", True),
                         ("ls bench/spec-suite/x", False), ("python3 -m unittest", False)):
            with open(path, "w") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}) + "\n")
            self.assertEqual(bool(bench._leaks(path, needles)), hit, cmd)

    def test_the_arms_own_plugin_under_the_source_root_is_not_a_leak(self):
        from types import SimpleNamespace
        p = SimpleNamespace(root=self.repo, dir=os.path.join(self.tmp, "state"))
        plugin = os.path.join(self.repo, "plugin")
        needles = bench._needles(p, {"id": "T-1"}, allow=[plugin])
        path = os.path.join(self.tmp, "s.jsonl")
        for cmd, hit in ((f"python3 {plugin}/bin/fm status", False), (f"ls {self.repo}/bench/spec-suite/T-1", True),
                         (f"cat {plugin}/x {self.repo}/bench/cases.json", True)):
            with open(path, "w") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}) + "\n")
            self.assertEqual(bool(bench._leaks(path, needles, allow=[plugin])), hit, cmd)

    def test_audit_ignores_a_session_name_with_glob_characters(self):
        cc = os.path.join(self.tmp, "cc2")
        os.makedirs(os.path.join(cc, "projects", "q"))
        with open(os.path.join(cc, "projects", "q", "real.jsonl"), "w") as f:
            f.write("{}\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cc}):
            self.assertEqual(bench._audit("re*", ["x"]), ([], False))
            self.assertEqual(bench._audit("real", ["x"]), ([], True))

    def test_a_kept_session_keeps_its_subagent_transcripts(self):
        cc, keep = os.path.join(self.tmp, "cc3"), os.path.join(self.tmp, "keep")
        os.makedirs(os.path.join(cc, "projects", "q", "sid1", "subagents"))
        os.makedirs(keep)
        with open(os.path.join(cc, "projects", "q", "sid1.jsonl"), "w") as f:
            f.write("{}\n")
        with open(os.path.join(cc, "projects", "q", "sid1", "subagents", "a.jsonl"), "w") as f:
            f.write("{}\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cc, "FOREMAN_BENCH_SESSIONS": keep}):
            bench._compactions("sid1")
        self.assertTrue(os.path.exists(os.path.join(keep, "sid1", "subagents", "a.jsonl")))

    def test_runs_n_shows_a_leaked_attempt(self):
        tries = [{"pass": True, "cost_usd": 1, "turns": 1, "seconds": 1},
                 {"pass": False, "cost_usd": 1, "turns": 1, "seconds": 1, "status": "leak"}]
        with mock.patch.object(bench, "run_case", side_effect=tries):
            got = bench.run_case_n(None, {}, "plug", runs=2)
        self.assertEqual(got.get("status"), "leak")
        self.assertEqual(got["attempts"][1].get("status"), "leak")

    def test_a_leaking_run_is_voided(self):
        env = dict(self.env, STUB_SOLVE="1", STUB_LEAK="~/.claude/projects/x")
        self.fm("bench", "build", env=env)
        case = json.loads(self.fm("bench", "run", "--label", "leak", "--json", env=env, check=False).stdout)["cases"][0]
        self.assertFalse(case["pass"])
        self.assertEqual(case.get("status"), "leak")
        self.assertFalse(glob.glob(os.path.join(self.tmp, "cc", "projects", "*", "*.jsonl")), "transcript removed")

    def test_a_run_with_no_transcript_is_unaudited_not_clean(self):
        env = dict(self.env, STUB_SOLVE="1", STUB_NO_TRANSCRIPT="1")
        self.fm("bench", "build", env=env)
        case = json.loads(self.fm("bench", "run", "--label", "un", "--json", env=env, check=False).stdout)["cases"][0]
        self.assertFalse(case["pass"])
        self.assertEqual(case.get("status"), "unaudited")
        self.assertEqual(case.get("mode"), "fresh")

    def test_compare_warns_when_the_runs_trees_differ(self):
        self.write_result("old", [("T-1", 1.0, 0.1)])  # no mode stamp: a run from before T-3364, in a worktree
        self.write_result("new", [("T-1", 1.0, 0.1)])
        path = os.path.join(c.find_project(self.repo).dir, "bench", "results", "new.json")
        with open(path, "w") as f:
            json.dump({"label": "new", "mode": "fresh", "cases": [{"id": "T-1", "pass": True, "cost_usd": 0.1, "turns": 10}]}, f)
        self.assertIn("not comparable", self.fm("bench", "compare", "old", "new", env=self.env).stdout)
        self.assertNotIn("not comparable", self.fm("bench", "compare", "new", "new", env=self.env).stdout)


@unittest.skipUnless(hasattr(os, "sched_getaffinity"), "no CPU affinity on this system")
class Cpus(ForemanTestCase):
    """T-4276: a headless replay session is held to a few CPUs at a low priority, with everything it starts: a nightly
    benchmark's sessions ran two whole test suites beside a land's gate (27 of 40 runnable processes were theirs)."""

    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {**os.environ, "PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log}
        self.env.pop("PYTHON_CPU_COUNT", None)
        self.mine = sorted(os.sched_getaffinity(0))
        self.fm("init")
        self.fm("claude-bench", "on")  # T-5584: the session runs in this process, so it names its project
        os.environ["FOREMAN_PROJECT"] = c.find_project(self.repo).slug

    def session(self, cpus, **env):
        with mock.patch.object(bench, "replay_cpus", return_value=cpus):
            got = bench._session(self.repo, {**self.env, **env}, ["plugin"], "hi", None, None, 1)
        return got, [json.loads(l) for l in read_text(self.log).splitlines()] if os.path.exists(self.log) else []

    def test_cpus_a_session_runs_on_its_cpus_at_a_low_priority_and_is_told_their_count(self):
        (res, error), calls = self.session(self.mine[-1:])
        self.assertIsNone(error)
        self.assertEqual(res.get("result"), "done")
        self.assertEqual(calls[0]["cpus"], self.mine[-1:])
        self.assertEqual(calls[0]["cpu_count"], "1")  # Python's os.cpu_count() in everything the session starts
        self.assertEqual(calls[0]["nice"], max(os.nice(0), bench.REPLAY_NICE))
        self.assertEqual(calls[0]["args"][:2], ["-p", "hi"], "claude's own arguments are untouched")

    def test_cpus_what_the_session_starts_stays_on_them(self):
        _, calls = self.session(self.mine[-1:], STUB_CHILD="1")
        self.assertEqual(calls[1], {"child": self.mine[-1:]})

    def test_cpus_a_small_machine_confines_nothing(self):
        mine, nice = set(os.sched_getaffinity(0)), os.nice(0)
        (_, error), calls = self.session(None)
        self.assertIsNone(error)
        self.assertEqual((set(calls[0]["cpus"]), calls[0]["cpu_count"], calls[0]["nice"]), (mine, None, nice))

    def test_cpus_a_missing_claude_is_still_cant_start(self):
        for cpus in (None, self.mine[-1:]):
            (res, error), calls = self.session(cpus, PATH=os.path.join(self.tmp, "none"))
            self.assertEqual((res, calls), ({}, []))
            self.assertTrue(error.startswith("can't start claude: "), error)

    def test_cpus_the_launcher_loads_no_code_from_the_session_s_environment(self):
        # a replay's environment and worktree are the replayed tree's: the launcher (python -c) must not import from them
        planted, seen = os.path.join(self.tmp, "planted"), os.path.join(self.tmp, "seen")
        os.makedirs(planted)
        with open(os.path.join(planted, "sitecustomize.py"), "w") as f:
            f.write(f"import json, sys\nwith open({seen!r}, 'a') as f:\n    f.write(json.dumps(sys.orig_argv[:3]) + '\\n')\n")
        (_, error), calls = self.session(self.mine[-1:], PYTHONPATH=planted)
        self.assertIsNone(error)
        self.assertEqual(calls[0]["cpus"], self.mine[-1:])
        ran = [json.loads(l) for l in read_text(seen).splitlines()]
        self.assertTrue(ran, "the planted module is live: the stub claude, a plain Python, loaded it")
        self.assertEqual([a for a in ran if "-c" in a], [], "the launcher ran code planted for it")

    def test_cpus_the_highest_quarter_at_least_two_and_none_below_eight(self):
        for have, want in ((range(16), [12, 13, 14, 15]), (range(8), [6, 7]), (range(0, 32, 2), [24, 26, 28, 30]),
                           (range(11), [9, 10]), (range(7), None), (range(1), None)):
            with mock.patch.object(os, "sched_getaffinity", return_value=set(have)):
                self.assertEqual(bench.replay_cpus(), want, list(have))
        with mock.patch.object(os, "sched_getaffinity", side_effect=OSError("no")):
            self.assertIsNone(bench.replay_cpus())
