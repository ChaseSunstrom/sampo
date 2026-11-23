"""T-5774: lanes.lane_fn_overlap (T-5725) unpacked _defs as three values after T-5682 made it return four, so `sampo lane
brief` raised ValueError for any task with a code scope. A scoped Python file plus another lane changing one of its defs
gives the warning line."""
# map-links: plugin/lib/sampo/lanes.py
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import types
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes

G = ["git", "-c", "user.name=t", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false"]


class FnOverlap(ForemanTestCase):
    def git(self, cwd, *a):
        return subprocess.run(G + list(a), cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def test_fn_overlap_warns_for_a_def_another_lane_changes_instead_of_raising(self):
        main = os.path.join(self.tmp, "main")
        os.makedirs(main)
        self.git(main, "init", "-q")
        with open(os.path.join(main, "a.py"), "w") as f:
            f.write("def keep():\n    return 1\n\n\ndef frobnicate():\n    return 2\n")
        self.git(main, "add", "-A")
        self.git(main, "commit", "-qm", "base")
        base = self.git(main, "rev-parse", "HEAD")
        lane = os.path.join(self.tmp, "lane")
        self.git(main, "worktree", "add", "-q", "-b", "other", lane)
        with open(os.path.join(lane, "a.py"), "w") as f:
            f.write("def keep():\n    return 1\n\n\ndef frobnicate():\n    return 3\n")
        self.git(lane, "commit", "-qam", "change frobnicate")
        step = types.SimpleNamespace(text="make `frobnicate` faster")
        mine = types.SimpleNamespace(id="T-1", title="Speed up frobnicate", meta={"scope": ["a.py"]}, steps=lambda: [step])
        other = types.SimpleNamespace(id="T-2", title="Other work", status="active", meta={"lane": lane})
        out = lanes.lane_fn_overlap(None, mine, main, base, [mine, other])
        self.assertEqual(len(out), 1, out)
        self.assertIn("frobnicate in a.py", out[0])
        self.assertIn("T-2", out[0])


def real(n):
    """A verify command whose output is a test runner's own ("Ran N tests"): the one kind of run that counts as proof."""
    return f"python3 -c 'print(\"Ran {n} tests in 0.1s\"); print(\"OK\")'"


RED = "python3 -c 'import sys; sys.exit(3)'"


class AlreadyTrue(ForemanTestCase):
    """T-6063: before a brief is written, each criterion's verify runs on a clean checkout of trunk's HEAD. All of them
    real test runs that passed: the task closes with those runs as its evidence and no builder is launched. A mix: the
    brief lists only the criteria not proven. A run that measured nothing (zero tests, a -k that matched nothing, `true`,
    `test -f`, grep, echo) or timed out is never green."""

    ON = {"FOREMAN_BRIEF_VERIFY": "on"}

    def setUp(self):
        super().setUp()
        self.fm("init")

    def task(self, *verifies, tier="S"):
        acs = []
        for i, v in enumerate(verifies, 1):
            acs += ["--ac", f"criterion {i} holds :: {v}"]
        return json.loads(self.fm("task", "new", "Already true work", "--type", "FEATURE", "--tier", tier, *acs,
                                  "--step", "build it", "--json").stdout)["id"]

    def brief(self, tid, env=None, *flags):
        r = self.fm("lane", "brief", tid, *flags, "--json", env={**self.ON, **(env or {})}, check=False)
        return r, (json.loads(r.stdout) if r.stdout.strip().startswith("{") else {})

    def stored(self, tid):
        return c.find_brief(c.find_project(self.repo), tid)

    def test_every_criterion_green_closes_the_task_with_the_runs_as_evidence_and_no_builder(self):
        tid = self.task(real(1), real(2))
        r, out = self.brief(tid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(out.get("closed"), out)
        self.assertFalse(out.get("path"), out)  # no brief file, no Agent call
        b = self.stored(tid)
        self.assertEqual(b.status, "done", r.stderr)
        ev = b.evidence()
        for n in (1, 2):  # each criterion's own run, fm's (never typed), green
            mine = [l for l in ev if l.startswith(f"- (ac {n}) `{real(n)}` → exit 0")]
            self.assertTrue(mine and "[ran]" in mine[0] and "✗" not in mine[0], ev)
        self.assertFalse([l for l in ev if "✗" in l], ev)
        self.assertTrue(all(a.checked for a in b.acceptance()))

    def test_the_close_says_what_happened_no_change_and_the_runs_on_head(self):
        tid = self.task(real(1))
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo, capture_output=True,
                             text=True).stdout.strip()
        r, out = self.brief(tid)
        self.assertTrue(out.get("closed"), (r.stderr, out))
        text = " ".join(self.fm("task", "show", tid).stdout.split())
        self.assertIn("no change was made", text)
        self.assertIn(f"verified on HEAD {sha}", text)

    def test_a_mixed_list_briefs_only_the_red_criteria(self):
        tid = self.task(real(1), RED)
        r, out = self.brief(tid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(out.get("closed"), out)
        self.assertEqual(out.get("red"), [2], out)
        self.assertEqual(out.get("green"), [1], out)
        with open(out["path"]) as f:
            text = f.read()
        crit = text.split("## Acceptance criteria", 1)[1].split("\n## ", 1)[0]
        self.assertIn("sys.exit(3)", crit)
        self.assertNotIn("Ran 1 tests", crit)  # the green criterion is not listed...
        self.assertIn("Criteria 1 already pass on HEAD", crit)  # ...and the brief says so
        b = self.stored(tid)
        self.assertEqual(b.status, "planned")
        self.assertFalse(any(a.checked for a in b.acceptance()))  # nothing is ticked: the builder's close re-runs them
        self.assertFalse(b.evidence(), b.evidence())

    def test_the_criteria_filter_lists_all_when_nothing_passes(self):
        tid = self.task(RED, "python3 -c 'import sys; sys.exit(4)'")
        r, out = self.brief(tid)
        self.assertEqual(out.get("red"), [1, 2], out)
        with open(out["path"]) as f:
            crit = f.read().split("## Acceptance criteria", 1)[1].split("\n## ", 1)[0]
        self.assertIn("sys.exit(3)", crit)
        self.assertIn("sys.exit(4)", crit)
        self.assertNotIn("already pass on HEAD", crit)

    def test_a_run_that_ran_zero_tests_is_red(self):
        tid = self.task("echo 'Ran 0 tests in 0.000s'; echo OK")
        r, out = self.brief(tid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(out.get("closed"), out)
        self.assertEqual(out.get("red"), [1], out)
        self.assertEqual(self.stored(tid).status, "planned")

    def test_a_k_filter_that_matched_nothing_is_red(self):
        tid = self.task("echo 'run.py: no test matches test_x -k Nothing'; exit 1", "echo 'no tests ran in 0.01s'")
        r, out = self.brief(tid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(out.get("closed"), out)
        self.assertEqual(out.get("red"), [1, 2], out)

    def test_a_timeout_is_red(self):
        tid = self.task("sleep 30")
        r, out = self.brief(tid, env={"FOREMAN_BRIEF_VERIFY_S": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(out.get("closed"), out)
        self.assertEqual(out.get("red"), [1], out)
        self.assertEqual(self.stored(tid).status, "planned")

    def test_a_vacuous_verify_never_closes_a_task(self):
        self.fm("lane", "max", "20")  # each brief takes a builder slot
        for cmd in ("true", "test -f README.md", "grep -q . README.md", "echo hello", "echo 'Ran 4 tests in 0.1s'"):
            with self.subTest(cmd=cmd):
                tid = self.task(cmd)
                r, out = self.brief(tid)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(out.get("closed"), out)
                self.assertEqual(out.get("red"), [1], out)
                self.assertEqual(self.stored(tid).status, "planned")

    def test_a_vacuous_criterion_beside_a_real_one_stays_in_the_brief(self):
        tid = self.task(real(1), "true")
        r, out = self.brief(tid)
        self.assertFalse(out.get("closed"), out)
        self.assertEqual((out.get("green"), out.get("red")), ([1], [2]), out)

    def test_an_m_task_all_green_closes_too(self):
        extra = ["--interpretation", "Reading 1: a. Reading 2: b.", "--approach",
                 "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a"]
        tid = json.loads(self.fm("task", "new", "Already true M", "--type", "FEATURE", "--tier", "M", "--ac",
                                 f"it holds :: {real(1)}", "--step", "build it", *extra, "--json").stdout)["id"]
        r, out = self.brief(tid)
        self.assertTrue(out.get("closed"), (r.stderr, out))
        self.assertEqual(self.stored(tid).status, "done")

    def test_the_verifies_run_in_a_clean_checkout_of_head_not_the_main_one(self):
        """An untracked file in the main checkout is not HEAD: a verify that needs it is red."""
        with open(os.path.join(self.repo, "untracked.txt"), "w") as f:
            f.write("x\n")
        tid = self.task("python3 -c 'import os; assert os.path.exists(\"untracked.txt\"); print(\"Ran 2 tests in 0.1s\")'")
        r, out = self.brief(tid)
        self.assertFalse(out.get("closed"), (r.stderr, out))
        self.assertEqual(out.get("red"), [1], out)

    def test_a_changed_tracked_file_in_main_is_not_head_either(self):
        with open(os.path.join(self.repo, "README.md"), "a") as f:
            f.write("changed-in-the-working-tree\n")
        tid = self.task("python3 -c 'assert \"changed-in-the-working-tree\" in open(\"README.md\").read(); print(\"Ran 2 tests in 0.1s\")'")
        r, out = self.brief(tid)
        self.assertFalse(out.get("closed"), (r.stderr, out))
        self.assertTrue(out.get("path"), (r.stderr, out))
        self.assertEqual(out.get("red"), [1], out)

    def test_a_file_a_verify_creates_turns_nothing_red_and_leaves_no_trace(self):
        tid = self.task(f"touch made_by_verify.txt; {real(1)}", real(2))
        r, out = self.brief(tid)
        self.assertTrue(out.get("closed"), (r.stderr, out))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "made_by_verify.txt")))
        wts = subprocess.run(["git", "worktree", "list"], cwd=self.repo, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(len(wts), 1, wts)  # the scratch checkout of HEAD is removed

    def test_a_skipped_check_says_why_on_stderr(self):
        a, b = self.task(real(1)), self.task(real(2))
        host = json.loads(self.fm("batch", a, b, "--json").stdout)["id"]
        for tid, word in ((host, "batch"), (a, "batch")):
            r, out = self.brief(tid)
            self.assertFalse(out.get("closed"), out)
            self.assertIn("not run on HEAD", r.stderr, (tid, r.stderr))
            self.assertIn(word, r.stderr)

    def test_a_plain_s_task_is_checked(self):
        tid = self.task(real(1))
        r, out = self.brief(tid)
        self.assertNotIn("not run on HEAD", r.stderr)
        self.assertTrue(out.get("closed"), out)

    def test_brief_ready_closes_the_true_one_and_briefs_the_other(self):
        done = self.task(real(1))
        todo = self.task(RED)
        r = self.fm("lane", "brief", "--ready", "--no-batch", "--json", env=self.ON)
        out = json.loads(r.stdout)
        self.assertEqual(out["closed"], [done], out)
        self.assertEqual([x["id"] for x in out["briefed"]], [todo], out)
        self.assertEqual(self.stored(done).status, "done")

    def test_the_ready_sweep_stops_checking_when_its_total_is_spent_and_says_which_tasks_were_not_checked(self):
        slow = self.task(f"sleep 3; {real(1)}")
        late = self.task(real(2))
        r = self.fm("lane", "brief", "--ready", "--no-batch", "--json", env={**self.ON, "FOREMAN_BRIEF_SWEEP_S": "1"})
        out = json.loads(r.stdout)
        self.assertEqual(out["unchecked"], [late], out)  # the first one spent the sweep's time; the second is briefed as is
        self.assertEqual(sorted(x["id"] for x in out["briefed"]), sorted([slow, late]), out)
        self.assertEqual(out["closed"], [], out)
        self.assertEqual(self.stored(late).status, "planned")
        self.assertIn(late, r.stdout)

    def test_the_check_is_off_with_the_env_switch_and_with_no_verify(self):
        tid = self.task(real(1))
        r, out = self.brief(tid, env={"FOREMAN_BRIEF_VERIFY": "off"})
        self.assertFalse(out.get("closed"), out)
        self.assertTrue(out.get("path"), out)
        self.assertEqual(self.stored(tid).status, "planned")
        tid2 = self.task(real(1))
        r, out = self.brief(tid2, None, "--no-verify")
        self.assertFalse(out.get("closed"), (r.stderr, out))
        self.assertTrue(out.get("path"), out)

    def test_a_race_is_never_closed_as_already_true(self):
        """lane race briefs every side through builder_brief: a side that closed would leave the race without a brief."""
        tid = self.task(real(1), "test -f win.txt")  # a race wants a check that fails now; the green one is what a close would have used
        r = self.fm("lane", "race", tid, "--json", env=self.ON)
        out = json.loads(r.stdout)
        self.assertEqual(self.stored(tid).status, "planned", (r.stdout, r.stderr))
        self.assertEqual(self.stored(out["race"]).status, "planned")
        for t in (tid, out["race"]):  # each side has its brief file: a builder can be launched
            self.assertTrue(os.path.exists(out["briefs"][t]), out)

    def big(self, *verifies):
        """A task whose criteria are ~1.3k chars each, so eight of them are past the size cap (T-6047)."""
        args = ["task", "new", "Big already true", "--type", "FEATURE", "--tier", "S", "--step", "build it", "--json"]
        for i, v in enumerate(verifies, 1):
            args += ["--ac", f"criterion {i} " + "word " * 250 + f":: {v}"]
        return json.loads(self.fm(*args).stdout)["id"]

    def test_an_all_green_task_past_the_size_cap_closes_and_is_never_size_checked(self):
        tid = self.big(*[real(n) for n in range(1, 9)])
        r, out = self.brief(tid)
        self.assertTrue(out.get("closed"), (r.stderr, out))
        self.assertEqual(self.stored(tid).status, "done")

    def test_the_size_cap_reads_the_reduced_brief_of_a_mixed_task(self):
        tid = self.big(*[real(n) for n in range(1, 8)], RED)  # ~10k chars whole, ~1.4k once the 7 green ones are dropped
        r, out = self.brief(tid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.get("red"), [8], out)
        self.assertTrue(os.path.exists(out["path"]), out)

    def test_a_mixed_task_still_past_the_size_cap_after_the_reduction_is_refused(self):
        tid = self.big(real(1), *[RED] * 7)  # one green one dropped: seven red are still ~9k chars
        r, out = self.brief(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("--oversize", r.stderr)
        self.assertEqual(self.stored(tid).status, "planned")

    def test_a_resume_that_finds_no_branch_briefs_anew_and_is_never_closed_as_already_true(self):
        tid = self.task(real(1))
        path = json.loads(self.fm("lane", "new", tid, "--json").stdout)["path"]
        from sampo import bus
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        b.meta["builder"] = c.now()
        b.meta["builder_proc"] = f"{bus.host_id()}|999999999|1"
        c.save_brief(p, b)
        git = lambda *a, cwd=self.repo: subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, check=True)
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).stdout.strip()
        git("worktree", "remove", "--force", path)
        git("branch", "-D", branch)  # no lane, no branch: resume briefs from HEAD, as for a new task
        r = self.fm("lane", "resume", tid, "--json", env=self.ON)
        self.assertNotEqual(self.stored(tid).status, "done", (r.stdout, r.stderr))
        self.assertNotIn("already true", r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(p.dir, "audits", f"{tid}.builder.md")), (r.stdout, r.stderr))


class SizeCap(ForemanTestCase):
    """T-6047: a builder brief whose task-owned text is past the cap is refused at plan time, with the split printed."""

    def setUp(self):
        super().setUp()
        self.fm("init")

    TITLE = 'Cap "probe" $HOME it\'s `x`'  # what a printed command must quote

    def mk(self, n_ac, width, title=None, steps=3):
        args = ["task", "new", title or self.TITLE, "--type", "FEATURE", "--tier", "S", "--json"]
        for i in range(steps):
            args += ["--step", f"do part {i + 1}"]
        for i in range(n_ac):
            args += ["--ac", f"criterion {i + 1} " + "word " * width + ":: true"]
        return json.loads(self.fm(*args).stdout)["id"]

    def project(self):
        return c.find_project(self.repo)

    def brief_path(self, tid):
        return os.path.join(self.project().dir, "audits", f"{tid}.builder.md")

    def refused(self, tid, *more):
        r = self.fm("lane", "brief", tid, *more, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.brief_path(tid)), "a refused brief is not written")
        return r.stdout + r.stderr

    def test_over_the_cap_is_refused_with_the_split_and_the_measured_reason(self):
        tid = self.mk(8, 250)  # eight criteria of ~1.3k chars: ~10k of the task's own text
        out = self.refused(tid)
        self.assertIn("93%", out)
        self.assertIn("48%", out)
        self.assertIn("part 1/", out)
        self.assertIn("part 2/", out)
        self.assertIn("--depends", out, "the parts are chained")
        self.assertIn("--oversize", out, "the override is named")
        self.assertTrue(int(re.findall(r"part \d+/(\d+)", out)[0]) >= 2, out)
        for size in re.findall(r"~(\d+) chars", out):
            self.assertLessEqual(int(size), lanes.OWN_CAP, out)

    def test_the_printed_script_runs_and_makes_chained_parts_that_each_fit(self):
        tid = self.mk(8, 250)
        out = self.refused(tid)
        script = re.search(r"sh (\S+)", out).group(1).strip("'")
        env = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", FOREMAN_DOCTOR_BENCH="0", FOREMAN_DOCTOR_BUILD="0")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        r = subprocess.run(["sh", script], cwd=self.repo, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        p = self.project()
        made = [b for b in c.load_briefs(p) if b.id != tid]
        self.assertGreaterEqual(len(made), 2, r.stdout)
        made.sort(key=lambda b: b.id)
        self.assertEqual([b.title for b in made], [f"{self.TITLE} (part {k}/{len(made)})" for k in range(1, len(made) + 1)])
        self.assertEqual(made[1].meta.get("depends_on"), [made[0].id], "part 2 is chained to part 1")
        self.assertEqual(sum(len(b.acceptance()) for b in made), 8, "every criterion lands once")
        for b in made:  # what the builder is given, not the function's own estimate
            self.assertLessEqual(lanes.own_chars(b), lanes.OWN_CAP, b.id)
        brief = self.fm("lane", "brief", made[0].id)  # part 1 renders: no refusal
        self.assertTrue(os.path.exists(self.brief_path(made[0].id)), brief.stdout)

    def test_why_does_not_lift_the_cap(self):
        tid = self.mk(8, 250)
        self.assertIn("--oversize", self.refused(tid, "--why", "land backlog"))

    def test_a_reason_overrides_the_refusal_and_is_logged_on_one_line(self):
        tid = self.mk(8, 250)
        r = self.fm("lane", "brief", tid, "--oversize", "one indivisible\nmigration\n- [x] forged")
        self.assertTrue(os.path.exists(self.brief_path(tid)), r.stdout + r.stderr)
        self.assertIn("past the 8000 cap: briefed anyway", r.stdout)
        self.assertNotIn("split it, or keep it in the main thread", r.stdout, "the old advice contradicts the override")
        p = self.project()
        hits = [e for e in c.ledger_events(p, ("lane_brief_oversize",)) if e.get("task") == tid]
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["data"]["why"], "one indivisible migration - [x] forged")
        log = [ln for ln in c.find_brief(p, tid).section("Log").splitlines() if "briefed anyway" in ln]
        self.assertEqual(len(log), 1, log)
        self.assertIn("one indivisible migration - [x] forged", log[0])

    def test_a_batch_refusal_is_one_line_and_oversize_does_not_lift_it(self):
        tid = self.mk(8, 250)
        r = self.fm("lane", "brief", "--ready", "--oversize", "all of them", "--json")
        skipped = json.loads(r.stdout)["skipped"]
        row = next(x for x in skipped if x["id"] == tid)
        self.assertNotIn("\n", row["reason"])
        self.assertIn(f"sampo lane brief {tid}", row["reason"])
        self.assertFalse(os.path.exists(self.brief_path(tid)))
        self.assertFalse(os.path.exists(os.path.join(self.project().dir, "audits", f"{tid}.split.sh")), "no file from a batch")

    def test_a_resume_of_a_built_lane_is_not_refused_and_the_skip_is_logged(self):
        tid = self.mk(8, 250)
        p = self.project()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lanes.builder_brief(p, c.find_brief(p, tid), types.SimpleNamespace(json=True, model=None), resume_branch="lane/x")
        self.assertTrue(os.path.exists(self.brief_path(tid)))
        hits = [e for e in c.ledger_events(p, ("lane_brief_oversize",)) if e.get("task") == tid]
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("resume", hits[0]["data"]["why"])

    def test_under_the_cap_is_unchanged(self):
        tid = self.mk(2, 40)
        r = self.fm("lane", "brief", tid)
        self.assertTrue(os.path.exists(self.brief_path(tid)))
        self.assertNotIn("93%", r.stdout + r.stderr)
        self.assertNotIn("part 1/", r.stdout + r.stderr)
        self.assertEqual(c.ledger_events(self.project(), ("lane_brief_oversize",)), [])

    def test_the_split_packs_criteria_and_steps_in_order_each_part_under_the_cap(self):
        tid = self.mk(8, 250, steps=5)
        parts = lanes.split_criteria(c.find_brief(self.project(), tid), lanes.OWN_CAP)
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual([a.n for g in parts for a in g["acs"]], list(range(1, 9)), "every criterion once, in order")
        self.assertEqual([s.n for g in parts for s in g["steps"]], list(range(1, 6)), "every step once, in order")
        for g in parts:
            self.assertLessEqual(g["chars"], lanes.OWN_CAP)

    def test_no_split_names_what_is_too_big(self):
        one = self.mk(1, 1700)  # one criterion of ~8.5k chars: nothing to split by
        self.assertIn("nothing to split by", self.refused(one))
        shared = self.mk(3, 250, title="Shared heavy")
        self.fm("task", "set", shared, "--section", "Interpretation", "--text", "reading " * 1000)
        out = self.refused(shared)
        self.assertIn("every part repeats them", out)
        self.assertNotIn("nothing to split by", out)


class SplitScriptQuoting(ForemanTestCase):
    """T-6074: the split script quotes already-redacted values and is never redacted again: brief text never runs as shell,
    and a secret next to a quote is still redacted (a pattern run over the quoted script misses password='x')."""

    SECRET = "hunter2hunter2"
    TOKEN = "ghp_" + "Ab1" * 8  # a made-up token shape

    def brief(self):
        sec = {"Raw request": f"don't leak password='{self.SECRET}' or $(touch PWN_RAW)\nsecond line `touch PWN_BT`; touch PWN_SEMI",
               "Interpretation": f"token: \"{self.TOKEN}\" it's \\ done", "Approach (options → choice → why)": "x'; touch PWN_APP; '"}
        return types.SimpleNamespace(
            id="T-9001", title="Fix $(touch PWN_TITLE) `touch PWN_T2`; touch PWN_T3 it's", type="FIX", tier="S",
            meta={"scope": ["a b;touch PWN_SC", "c'd"], "depends_on": ["T-1; touch PWN_DEP"]}, section=lambda name: sec.get(name, ""))

    def parts(self):
        t = types.SimpleNamespace
        key = "-----BEGIN PRIVATE KEY-----"
        return [{"acs": [t(text=f"ac1 password='{self.SECRET}'"), t(text=key), t(text="between $(touch PWN_AC) `touch PWN_AC2`; x"),
                         t(text="-----END PRIVATE KEY-----")], "steps": [t(text="step '; touch PWN_ST; ' \"q\"")]},
                {"acs": [t(text="://u:p'w$(touch PWN_URL)@host and 'tail")], "steps": [t(text="plain step")]}]

    def written(self):
        stub = os.path.join(self.tmp, "plug", "bin")
        os.makedirs(stub)
        log = os.path.join(self.tmp, "argv.log")
        with open(os.path.join(stub, "sampo"), "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\0" "$@" >> {log}\nprintf "\\001" >> {log}\n'
                    f'echo x >> {log}.n\necho "T-90$(wc -l < {log}.n | tr -d " ") made"\n')
        os.chmod(os.path.join(stub, "sampo"), 0o755)
        p = types.SimpleNamespace(slug="proj", dir=os.path.join(self.tmp, "pdir"))
        with mock.patch.object(c, "PLUGIN_ROOT", os.path.join(self.tmp, "plug")):
            return lanes._split_script(p, self.brief(), self.parts()), log

    def test_values_are_redacted_then_quoted_and_the_script_runs_none_of_them(self):
        path, log = self.written()
        with open(path) as f:
            script = f.read()
        self.assertNotIn(self.SECRET, script, "the secret next to a quote is redacted")
        self.assertNotIn(self.TOKEN, script)
        self.assertIn("[REDACTED]", script)
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        r = subprocess.run(["sh", path], cwd=work, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([x for x in os.listdir(work) if x.startswith("PWN")], [], "brief text ran as shell")
        self.assertEqual([x for x in os.listdir(self.tmp) if x.startswith("PWN")], [])
        with open(log, "rb") as f:
            calls = [x.decode().split("\0")[:-1] for x in f.read().split(b"\x01")[:-1]]
        self.assertEqual(len(calls), 2, calls)
        flat = "\n".join("\0".join(x) for x in calls)
        self.assertNotIn(self.SECRET, flat)
        self.assertNotIn(self.TOKEN, flat)
        b, parts = self.brief(), self.parts()
        red = c.redact
        first = calls[0]
        self.assertEqual(first[:5], ["-p", "proj", "task", "new", red(f"{b.title} (part 1/2)")])
        self.assertEqual(first[first.index("--scope") + 1], red("a b;touch PWN_SC"))
        self.assertEqual(first[first.index("--depends") + 1], "T-1; touch PWN_DEP", "part 1 keeps the task's own depends")
        self.assertEqual(first[first.index("--raw") + 1], red(b.section("Raw request").strip()))
        self.assertEqual(first[first.index("--approach") + 1], "x'; touch PWN_APP; '")
        acs = [first[i + 1] for i, x in enumerate(first) if x == "--ac"]
        self.assertEqual(acs, [red(a.text) for a in parts[0]["acs"]], "each field alone: no pattern ran across two of them")
        self.assertEqual([first[i + 1] for i, x in enumerate(first) if x == "--step"], [parts[0]["steps"][0].text])
        second = calls[1]
        self.assertEqual(second[second.index("--depends") + 1], "T-901", "part 2 follows the id part 1 printed")
        self.assertEqual([second[i + 1] for i, x in enumerate(second) if x == "--scope"], ["a b;touch PWN_SC", "c'd"])
        self.assertEqual([second[i + 1] for i, x in enumerate(second) if x == "--ac"], [red(parts[1]["acs"][0].text)])

    def test_every_command_line_reads_back_with_shlex_and_a_misquoted_value_is_never_written(self):
        path, _ = self.written()
        with open(path) as f:
            script = f.read()
        words = shlex.split(script, comments=True)  # raises on an unbalanced quote anywhere
        self.assertIn("exit", words)
        self.assertEqual(script.count("\nO1=$("), 1)
        other = os.path.join(self.tmp, "pdir2")
        p = types.SimpleNamespace(slug="proj", dir=other)
        with mock.patch.object(lanes.shlex, "quote", lambda v: v), self.assertRaises(c.PolicyError):
            lanes._split_script(p, self.brief(), self.parts())
        self.assertFalse(os.path.exists(os.path.join(other, "audits", "T-9001.split.sh")))


if __name__ == "__main__":
    unittest.main()
