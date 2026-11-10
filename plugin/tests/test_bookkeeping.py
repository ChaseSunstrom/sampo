"""T-0704 (Frontier 04, first slice): bookkeeping that takes no model turns. Over 14 days 618 fm task evidence calls
re-ran a command the session had just run and 135 finishes were refused and retried; a handoff is trusted only while
its failure still reproduces."""
import os
import subprocess

from helpers import ForemanTestCase
from test_hooks import HookCase

from sampo import core as c

OK = {"stdout": "3 passed", "stderr": "", "interrupted": False}


class VerifierGate(ForemanTestCase):
    def test_close_holds_verifier_is_shadow_until_auc_over_three_projects_passes_065(self):  # T-3073
        from sampo import verifier as v
        at = lambda auc, n: v.gate_open({"trained": {"auc": auc, "projects": n}})  # noqa: E731
        self.assertFalse(at(0.55, 5))
        self.assertFalse(at(0.9, 2))
        self.assertFalse(at(0.65, 3))
        self.assertTrue(at(0.66, 3))
        self.assertFalse(v.gate_open({}))
        self.assertIn("pooled held-out AUC 0.55", v.auc_note({"trained": {"auc": 0.55}}))


class StepEvidence(HookCase):
    def ran(self, cmd, env=None, out=OK):
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_response": out}, env=env)
        return c.find_brief(self.project(), self.tid)

    def test_a_piped_test_run_counts_when_its_output_shows_the_runners_own_summary(self):
        # T-1256: long sessions piped every run to tail, then spent 5 to 12 calls recording evidence by hand
        self.fm("init")
        self.tid = self.task(steps=("build it", "wire it"))
        self.fm("task", "ac", self.tid, "add", "all pass", "--verify", "python3 tests/run.py -p all")
        said = lambda text: {"stdout": text, "stderr": "", "interrupted": False}
        green, red = said("Ran 3 tests in 0.1s\n\nOK"), said("Ran 3 tests in 0.1s\n\nFAILED (failures=1)")
        self.assertFalse(self.ran("python3 tests/run.py -k one 2>&1 | tail -3", out=said("ok")).has_evidence(step=1),
                         "no summary: tail's exit says nothing")
        self.assertFalse(self.ran("python3 tests/run.py -k one | tail -3", out=red).has_evidence(step=1))
        self.assertFalse(self.ran("python3 tests/run.py -k one 2>&1 | tail -n 40", out=green).has_evidence(step=1))
        b = self.ran("python3 tests/run.py -p all 2>&1 | tail -n 40", out=green)
        self.assertTrue(b.has_evidence(step=1), b.evidence())
        b = self.ran("python3 tests/run.py -p all 2>&1 | grep -E 'OK|FAIL'", out=said("OK"))
        self.assertTrue(any("(ac 2)" in l and "[ran]" in l and "✗" not in l for l in b.evidence()), b.evidence())
        for summary in ("===== 4 passed in 0.31s =====", "4 passed in 0.31s", "Tests:       4 passed, 4 total",
                        "test result: ok. 4 passed; 0 failed", "  4 passing (20ms)", "PASS"):
            self.assertTrue(c.passed_summary(summary), summary)
        for not_one in ("passed the ball", "4 passed", "OK then", "FAILED (failures=1)\nOK"):
            self.assertFalse(c.passed_summary(not_one), not_one)

    def test_a_one_off_session_records_nothing_on_the_task(self):
        # T-1004: the UI harness (FOREMAN_ONEOFF=1) ran the suite and closed the working session's step 3
        self.fm("init")
        self.tid = self.task(steps=("build it", "wire it"))
        self.fm("task", "ac", self.tid, "add", "all pass", "--verify", "python3 tests/run.py -p all")
        for env in ({"FOREMAN_ONEOFF": "1"}, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "FOREMAN_DRIVE_TASK": ""}):
            for cmd in ("python3 tests/run.py -p all", "python3 tests/run.py -k one"):  # the criterion's check, its runner
                self.assertEqual(self.ran(cmd, env=env).evidence(), [], (env, cmd))
        b = self.ran("python3 tests/run.py -p all")
        self.assertTrue(b.has_evidence(step=1) and b.has_evidence(ac=2), "the working session still does")

    def test_a_passing_run_through_a_criterion_runner_verifies_the_current_step(self):
        self.fm("init")
        self.tid = self.task(steps=("build it", "wire it"))
        self.fm("task", "ac", self.tid, "add", "all pass", "--verify", "python3 tests/run.py -p all")
        self.assertFalse(self.ran("ls").has_evidence(step=1), "not a test run")
        self.assertFalse(self.ran("python3 tests/run.py -k one | tail -3").has_evidence(step=1), "a pipe hides its exit")
        self.assertFalse(self.ran("cd /x && python3 tests/run.py -k one").has_evidence(step=1),
                         "T-2911: same runner, other tests: not this step's check")
        b = self.ran("cd /x && python3 tests/run.py   -p all")  # the criterion's own check, normalised
        self.assertTrue(b.has_evidence(step=1))
        self.assertTrue(any("(step 1)" in l and "[ran]" in l for l in b.evidence()), b.evidence())
        self.assertFalse(b.has_evidence(step=2), "one run verifies one step")

    def test_a_step_about_failing_tests_is_not_verified_by_a_pass(self):
        self.fm("init")
        self.tid = self.task(steps=("failing tests first", "fix"))
        self.fm("task", "ac", self.tid, "add", "all pass", "--verify", "pytest tests")
        self.assertFalse(self.ran("pytest -k login").has_evidence(step=1))


class FinishGaps(HookCase):
    def test_one_refusal_names_every_gap(self):
        self.fm("init")
        tid = self.task(type_="FEATURE", tier="M", steps=("build it",))
        r = self.fm("task", "finish", tid, "--audit", "self review", "--lens", "edge: fine", check=False)
        self.assertNotEqual(r.returncode, 0)
        for gap in ("step 1", "intent", "docs", "lesson"):
            self.assertIn(gap, r.stderr + r.stdout)

    def test_a_refused_field_naming_a_section_says_how_to_fill_it(self):
        # T-1925: `fm task set ID interpretation=…` listed the editable fields and nothing else
        self.fm("init")
        tid = self.task(type_="FIX", tier="S", steps=("fix",))
        for field, name in (("interpretation", "Interpretation"), ("risks_and_rollback", "Risks and rollback")):
            r = self.fm("task", "set", tid, field + "=x", check=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(f"--section '{name}' --text", r.stderr + r.stdout)
        r = self.fm("task", "set", tid, "colour=x", check=False)
        self.assertNotIn("brief section", r.stderr + r.stdout)
        self.assertIn("task set", self.fm("which", "interpretation").stdout)

    def test_a_red_step_names_its_failed_run_and_the_command_while_a_bare_one_keeps_the_old_message(self):
        # T-1272: 'no verify command or evidence' was wrong for a step that had a (failed) run
        self.fm("init")
        tid = self.task(type_="FEATURE", tier="M", steps=("build it", "wire it"))
        e = self.fm("task", "evidence", tid, "--step", "1", "--run", "false", check=False)
        self.assertNotEqual(e.returncode, 0, e.stdout + e.stderr)
        r = self.fm("task", "finish", tid, "--audit", "self review", check=False)
        out = r.stderr + r.stdout
        self.assertNotIn("its last run failed", out)  # T-1844: the close re-runs it, so it's no gap
        self.assertIn("step 2 (wire it) has no verify command or evidence of its own", out)
        self.assertNotIn("step 1 (build it) has no verify", out)

    def test_a_red_step_is_re_run_at_close_and_closes_once_its_command_passes(self):
        # T-1844: a test-first step's deliberate red run refused the close until it was re-recorded (T-1669, T-1841)
        self.fm("init")
        self.fm("task", "new", "t", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "red first", "--focus")
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "test -f ok", check=False)
        self.fm("task", "set", "T-0001", "--section", "Regression test", "--text", "none: a test of the close itself")
        close = ["task", "finish", "T-0001", "--audit", "self", "--lens", "intent: ok"]
        r = self.fm(*close, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("test -f ok", r.stdout + r.stderr)  # it ran, still red, and says so
        open(os.path.join(self.repo, "ok"), "w").close()
        r = self.fm(*close, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(c.find_brief(c.find_project(self.repo), "T-0001").status, "done")

    def test_a_criterion_whose_runs_matched_no_test_can_be_fixed_and_a_real_failure_cannot(self):  # T-2606
        self.fm("init")
        self.fm("task", "new", "t", "--type", "FIX", "--tier", "S", "--ac", "ok :: python3 plugin/tests/run.py test_nope",
                "--step", "s", "--focus")
        p = c.find_project(self.repo)
        b = c.find_brief(p, "T-0001")
        b.add_evidence("python3 plugin/tests/run.py test_nope", c.run_result(1, "run.py: no test matches test_nope"),
                       ac=1, ran=True)
        c.save_brief(p, b)
        self.fm("task", "ac", "T-0001", "edit", "1", "--verify", "python3 plugin/tests/run.py test_real")
        self.assertIn("test_real", c.find_brief(p, "T-0001").section("Acceptance criteria"))
        b = c.find_brief(p, "T-0001")
        b.add_evidence("python3 plugin/tests/run.py test_real", c.run_result(1, "AssertionError: 1 != 2"), ac=1, ran=True)
        c.save_brief(p, b)
        r = self.fm("task", "ac", "T-0001", "edit", "1", "--verify", "true", check=False)
        self.assertNotEqual(r.returncode, 0, "a check that ran and failed stays as it is (T-0342)")

    def test_measured_nothing_reads_the_criterions_own_command_and_allows_only_a_check_already_run(self):  # T-4113
        # T-4111: the criterion named test_louhi_loo (no such module); the right command's passes were recorded under it
        self.fm("init")
        self.fm("task", "new", "t", "--type", "FIX", "--tier", "S", "--ac", "ok :: python3 plugin/tests/run.py test_nope",
                "--step", "s", "--focus")
        p = c.find_project(self.repo)
        b = c.find_brief(p, "T-0001")
        b.add_evidence("python3 plugin/tests/run.py test_nope", c.run_result(1, "run.py: no test matches test_nope"),
                       ac=1, ran=True)
        b.add_evidence("python3 plugin/tests/run.py test_real", c.run_result(0, "Ran 1 tests in 0.1s\nOK"), ac=1, ran=True)
        c.save_brief(p, b)
        r = self.fm("task", "ac", "T-0001", "edit", "1", "--verify", "true", check=False)
        self.assertNotEqual(r.returncode, 0, "only a check already run under it may replace it")
        self.fm("task", "ac", "T-0001", "edit", "1", "--verify", "python3 plugin/tests/run.py test_real")
        self.assertIn("test_real", c.find_brief(p, "T-0001").section("Acceptance criteria"))

    def test_a_failed_affected_run_reruns_its_command_not_its_label(self):  # T-2596
        self.fm("init")
        self.fm("task", "new", "t", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        b = c.find_brief(c.find_project(self.repo), "T-0001")
        b.add_evidence("fm check --affected (affected tests only): test -f ok", c.run_result(1, "no"), step=1, ran=True)
        self.assertEqual(b.failed_cmd(1), "test -f ok")


class Reproduce(ForemanTestCase):
    def test_a_handoff_is_accepted_only_while_its_failure_reproduces(self):
        self.fm("init")
        with open(os.path.join(self.repo, "check.py"), "w") as f:
            f.write("raise SystemExit(1)\n")
        self.fm("task", "new", "Fix check", "--type", "FIX", "--tier", "S", "--ac", "check passes :: python3 check.py",
                "--step", "fix", "--focus")
        self.fm("task", "evidence", "T-0001", "--run", "python3 check.py", check=False)
        self.fm("task", "packet", "T-0001")
        with open(os.path.join(self.home, "state", "projects", c.find_project(self.repo).slug, "handoffs",
                               "T-0001.md")) as f:
            packet = f.read()
        self.assertIn("## Reproduce", packet)
        self.assertIn("python3 check.py", packet.split("## Reproduce")[1])
        r = self.fm("task", "packet", "T-0001", "--check")
        self.assertIn("reproduces", r.stdout)
        with open(os.path.join(self.repo, "check.py"), "w") as f:
            f.write("pass\n")
        r = self.fm("task", "packet", "T-0001", "--check", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stale", r.stdout + r.stderr)


class UnprovenTrailer(HookCase):  # T-2392: a closed task in the unproven rollup says so in its commit and its PR
    def test_unproven_trailer_names_the_kinds_and_fm_pr_lists_them(self):
        from sampo import core as c
        self.fm("init")
        self.fm("task", "new", "Claimed", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        path = os.path.join(self.repo, "a.py")
        with open(path, "w") as f:
            f.write("A = 1\n")
        self.hook("PostToolUse", {"tool_name": "Write", "tool_input": {"file_path": path}, "tool_response": {}})
        p = self.project()
        b = c.find_brief(p, "T-0001")
        b.meta["unproven_claims"] = ["perf"]  # what the Stop hook records for a kept, unbacked claim
        c.save_brief(p, b)
        self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--commit", "Claimed")
        body = subprocess.run(["git", "-C", self.repo, "log", "-1", "--format=%B"], capture_output=True, text=True).stdout
        self.assertIn("Unproven: claimed", body)
        pr = self.fm("pr", "T-0001").stdout
        self.assertIn("### Unproven", pr)
        self.assertIn("claimed perf", pr)
        self.assertIn("--bench", pr)  # with the command that proves it
        self.fm("task", "new", "Proven", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        with open(path, "w") as f:
            f.write("A = 2\n")
        self.hook("PostToolUse", {"tool_name": "Write", "tool_input": {"file_path": path}, "tool_response": {}})
        self.fm("task", "finish", "T-0002", "--audit", "self", "--run", "true", "--commit", "Proven")
        body = subprocess.run(["git", "-C", self.repo, "log", "-1", "--format=%B"], capture_output=True, text=True).stdout
        self.assertNotIn("Unproven:", body)


class SessionTrailer(HookCase):  # T-2447: commits carry no attribution line, whatever session made them (T-2443 undone)
    def test_session_trailer_is_never_added_whatever_the_bridge_session_id(self):
        self.fm("init")
        path = os.path.join(self.repo, "a.py")
        for n, sid in ((1, "session_Abc123"), (2, "")):
            self.fm("task", "new", f"t{n}", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                    "--focus")
            with open(path, "w") as f:
                f.write(f"A = {n}\n")
            self.hook("PostToolUse", {"tool_name": "Write", "tool_input": {"file_path": path}, "tool_response": {}})
            self.fm("task", "finish", f"T-000{n}", "--audit", "self", "--run", "true", "--commit", f"t{n}",
                    env={"CLAUDE_CODE_BRIDGE_SESSION_ID": sid})
            body = subprocess.run(["git", "-C", self.repo, "log", "-1", "--format=%B"], capture_output=True,
                                  text=True).stdout
            self.assertNotIn("Claude-Session", body)


class CommitOwnFiles(HookCase):
    """T-0738: T-0706's commit took T-0707's new files: both edited neighbouring lines of shared files, so re-basing
    T-0706's start snapshot failed and its commit fell back to everything changed since; T-0707's then had nothing."""

    def edit(self, rel, text):
        path = os.path.join(self.repo, rel)
        with open(path, "w") as f:
            f.write(text)
        self.hook("PostToolUse", {"tool_name": "Write", "tool_input": {"file_path": path}, "tool_response": {}})

    def setUp(self):
        super().setUp()
        self.fm("init")
        with open(os.path.join(self.repo, "shared.py"), "w") as f:
            f.write("X = 0\nY = 0\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "shared"], check=True)
        self.fm("task", "new", "First", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        self.edit("shared.py", "X = 1\nY = 0\n")
        self.fm("task", "new", "Second", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        self.edit("shared.py", "X = 1\nY = 2\n")  # the next line: the paused task's snapshot can't be re-based

    def shown(self):
        return subprocess.run(["git", "-C", self.repo, "show", "--stat", "--format=", "HEAD"], capture_output=True,
                              text=True).stdout

    def test_a_file_only_another_task_edited_stays_out(self):
        self.edit("theirs.py", "B = 1\n")
        self.fm("focus", "T-0001")
        self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--commit", "First")
        self.assertIn("shared.py", self.shown())
        self.assertNotIn("theirs.py", self.shown())

    def test_a_shared_file_names_the_open_task_whose_edits_it_takes_along(self):
        # T-1711: T-0001's commit took shared.py whole, T-0002's line too, without a word
        self.fm("focus", "T-0001")
        r = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--commit", "First")
        self.assertIn("shared.py", self.shown())
        self.assertIn("shared.py was also edited by T-0002 since this task's focus (committed with it)", r.stdout)

    def test_a_commit_with_nothing_left_says_so(self):
        self.fm("focus", "T-0001")
        self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--commit", "First")
        self.fm("focus", "T-0002")
        r = self.fm("task", "finish", "T-0002", "--audit", "self", "--run", "true", "--commit", "Second", check=False)
        self.assertIn("nothing to commit", r.stdout + r.stderr)


class CloseUndo(HookCase):
    # T-0918: the commit line says how to take it back; every criterion proved by its own check says so
    def test_a_close_names_its_undo_and_when_nothing_is_needed_from_you(self):
        self.fm("init")
        self.fm("task", "new", "First", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        path = os.path.join(self.repo, "a.py")
        with open(path, "w") as f:
            f.write("A = 1\n")
        self.hook("PostToolUse", {"tool_name": "Write", "tool_input": {"file_path": path}, "tool_response": {}})
        self.fm("task", "evidence", "T-0001", "--ac", "1", "--run", "true")
        out = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--commit", "First").stdout
        sha = subprocess.run(["git", "-C", self.repo, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertIn(f"Undo: git revert --no-edit {sha}", out)
        self.assertIn("Nothing needed from you", out)
        self.assertNotIn("Not verified", out)


class CommitAfterFocus(HookCase):
    def test_a_file_another_task_edited_before_this_one_started_is_still_committed(self):
        # T-0739: T-0684's own shell edits of CHANGELOG.md were left out because T-0683 had edited it after T-0684 was
        # captured (but before it was focused)
        self.fm("init")
        self.fm("capture", "Later work")  # T-0001, captured long before it is worked
        self.fm("task", "new", "First", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        notes = os.path.join(self.repo, "notes.md")
        with open(notes, "w") as f:
            f.write("one\n")
        self.hook("PostToolUse", {"tool_name": "Write", "tool_input": {"file_path": notes}, "tool_response": {}})
        self.fm("task", "finish", "T-0002", "--audit", "self", "--run", "true", "--commit", "First")
        self.fm("task", "new", "Later work", "--from", "T-0001", "--type", "FEATURE", "--tier", "S")
        self.fm("task", "ac", "T-0001", "add", "ok", "--verify", "true")
        self.fm("task", "step", "T-0001", "add", "s")
        self.fm("focus", "T-0001")
        with open(notes, "a") as f:  # through the shell: no touched event of its own
            f.write("two\n")
        self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--commit", "Later")
        shown = subprocess.run(["git", "-C", self.repo, "show", "--stat", "--format=%s", "HEAD"], capture_output=True,
                               text=True).stdout
        self.assertTrue(shown.startswith("Later"), shown)  # its own commit, not the one before
        self.assertIn("notes.md", shown)

    def test_a_file_it_untracked_and_deleted_is_committed_as_gone(self):
        # T-0837: T-0836 ran git rm --cached on a stray cache and deleted it; git add -A -- <it> failed the pathspec
        self.fm("init")
        git = lambda *a: subprocess.run(["git", "-C", self.repo, *a], capture_output=True, text=True)
        for name in ("cache.json", ".gitignore"):
            with open(os.path.join(self.repo, name), "w") as f:
                f.write("{}\n" if name == "cache.json" else "*.bak\n")
        git("add", "cache.json", ".gitignore")
        git("commit", "-qm", "stray cache")
        self.fm("task", "new", "Untrack", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        with open(os.path.join(self.repo, ".gitignore"), "a") as f:
            f.write("/cache.json\n")
        git("rm", "-q", "--cached", "cache.json")
        os.remove(os.path.join(self.repo, "cache.json"))
        r = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--commit", "Untrack", check=False)
        self.assertIn("committed", r.stdout + r.stderr, r.stdout + r.stderr)
        shown = git("show", "--name-status", "--format=%s", "HEAD").stdout
        self.assertTrue(shown.startswith("Untrack"), shown)
        self.assertIn("D\tcache.json", shown)
        self.assertIn(".gitignore", shown)

    def test_build_artefacts_stay_out_and_a_vanished_one_doesnt_break_the_commit(self):
        # T-1320: an fm code run's checks left __pycache__/*.pyc, which went into its commit; one that was there at the
        # focus and gone by the close failed git commit's pathspec, so the task was done and nothing committed
        self.fm("init")
        git = lambda *a: subprocess.run(["git", "-C", self.repo, *a], capture_output=True, text=True)
        cache = os.path.join(self.repo, "__pycache__")
        with open(os.path.join(self.repo, "a.py"), "w") as f:
            f.write("x = 1\n")
        git("add", "a.py")
        git("commit", "-qm", "start")
        os.makedirs(cache)
        with open(os.path.join(cache, "a.cpython-314.pyc"), "w") as f:
            f.write("old\n")
        self.fm("task", "new", "Two", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        with open(os.path.join(self.repo, "a.py"), "w") as f:
            f.write("x = 2\n")
        os.remove(os.path.join(cache, "a.cpython-314.pyc"))
        for name in ("b.cpython-314.pyc", "c.pyc"):
            with open(os.path.join(cache, name), "w") as f:
                f.write("new\n")
        os.makedirs(os.path.join(self.repo, "node_modules", "x"))
        with open(os.path.join(self.repo, "node_modules", "x", "index.js"), "w") as f:
            f.write("1\n")
        r = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--commit", "Two", check=False)
        self.assertIn("committed", r.stdout, r.stdout + r.stderr)
        self.assertIn("build artefact", r.stdout, "what was left out is said")
        self.assertEqual(git("show", "--name-only", "--format=", "HEAD").stdout.split(), ["a.py"])
        self.assertNotIn("__pycache__", git("diff", "--cached", "--name-only").stdout, "nothing left staged")


class FinalRun(ForemanTestCase):
    def test_run_is_the_final_check_even_when_every_step_has_evidence(self):
        # T-0742: T-0686's close skipped --run (every step had evidence), so the full suite never ran at the close
        self.fm("init")
        self.fm("task", "new", "Two steps", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                "--step", "a", "--focus")
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true")
        r = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "false", check=False)
        self.assertNotEqual(r.returncode, 0, "a failing final check stops the close")
        self.assertNotEqual(c.find_brief(c.find_project(self.repo), "T-0001").status, "done")
        self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true")
        self.assertTrue(any("`true` → exit 0" in l and "[ran]" in l
                            for l in c.find_brief(c.find_project(self.repo), "T-0001").evidence()))
