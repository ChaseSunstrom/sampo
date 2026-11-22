"""T-4718: a train's own background verify never takes the test slots the train's ship needs. Train 1115d (2026-11-15)
started the full-suite `sampo lane verify` and, in the same minute, its ship: the ship's affected run waited 98% of its
wall for slots, was cut at its limit with 20 classes 'not run' and was refused as 'the affected tests failed'.
Now the train leaves the verify for its caller to start once the ship is asked for (or ran in line), the verify waits
while a ship of the project runs (the ship's marker, the ship's claim: never a process name), a ship cut only by its
time limit says it ran out of time waiting for test slots and is retried once, and a real failed class is refused as
before and not retried."""
# map-links: plugin/lib/sampo/train.py, plugin/lib/sampo/lanes.py, plugin/lib/sampo/release.py
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_land_ship import OLD, ShipCase
from test_lane_train import OK, TrainCase

from sampo import cli
from sampo import core as c
from sampo import lanes, release, train

CUT_OUT = ("\nRan 40 tests in 898.0s (12 jobs, 6 at a time; slowest: test_x.X 3s)\n\nINCOMPLETE (stopped at its deadline)\n"
           "not run (deadline): 2\n  not run class: test_late.Late\n  not run class: test_later.Later\n"
           "run.py: stopped at its deadline\n")
RED_OUT = ("FAIL: test_it (test_bad.Bad.test_it)\nAssertionError: False is not true\n\nRan 3 tests in 1.0s\n\n"
           "FAILED (1 classes: test_bad.Bad)\n  failed class: test_bad.Bad\n")
RED_AND_CUT = RED_OUT + "not run (deadline): 1\n  not run class: test_late.Late\n"
HUNG_OUT = ("\n==== test_hung.Hung\nrun.py: still running at the run's deadline (898s in): killed (T-1809)\nThread 0x1 (most "
            "recent call first):\n  File \"x.py\", line 1 in wait\n\nRan 40 tests in 898.0s (12 jobs, 6 at a time)\n\n"
            "INCOMPLETE (stopped at its deadline)\nnot run (deadline): 2\n  not run class: test_hung.Hung\n"
            "  not run class: test_late.Late\nrun.py: stopped at its deadline\n")


class Deferred(TrainCase):
    """The train hands its verify to the caller when asked to: the records are written, nothing is spawned."""

    def setUp(self):
        super().setUp()
        self.suite = os.path.join(self.tmp, "suite-runs")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        files = {"suite.sh": f'echo "${{PWD##*/}} $*" >> {self.suite}\nPYTHONPATH=plugin/tests exec python3 -m unittest "$@"\n',
                 "plugin/tests/test_doctor.py": OK.format("Doctor"),
                 "plugin/tests/test_rename_aliases.py": OK.format("Aliases"), "test_other.py": OK.format("Other")}
        for rel, text in files.items():
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "a suite")
        self.fm("check", "add", "sh suite.sh")
        self.fm("check", "affected", "sh suite.sh {names}")
        self.p = c.find_project(self.repo)

    def run_train(self, **kw):
        a = self.lane(1, "test_a.py", OK.format("A"))
        env = {"FM_POSTMERGE_CMD": "sh suite.sh"}
        with mock.patch.dict(os.environ, env), mock.patch.object(lanes, "pm_spawn") as spawn:
            got = train.train(self.p, self.repo, [a], **kw)
        return got, spawn

    def test_order_a_train_asked_to_defer_records_the_verify_and_starts_none(self):
        got, spawn = self.run_train(defer_verify=True)
        self.assertEqual(spawn.call_count, 0)
        self.assertTrue(got.get("verify_owed"), "it says a verify is owed, for the caller to start")
        self.assertEqual(len(lanes.pm_records(self.p)), 1, "the pending record is there for it")

    def test_order_a_train_not_asked_starts_its_verify_as_before(self):
        got, spawn = self.run_train()
        self.assertEqual(spawn.call_count, 1)
        self.assertFalse(got.get("verify_owed"))

    def test_order_a_train_that_ran_the_full_suite_owes_no_verify(self):
        with mock.patch.dict(os.environ, {"FOREMAN_FULL_GATE": "1"}):
            got, spawn = self.run_train(defer_verify=True)  # the gate ran everything: no record, nothing to verify
        self.assertFalse(got.get("verify_owed"))
        self.assertEqual((spawn.call_count, len(lanes.pm_records(self.p))), (0, 0))


class LandOrder(ShipCase):
    """sampo lane land: the verify starts after the ship was asked for (or ran in line), never before it."""

    def setUp(self):
        super().setUp()
        self.calls = []
        real = self.real = train.train

        def owing(p, main, ids=None, **kw):  # the gate here has no affected command: the verify is owed by the flag
            got = real(p, main, ids, **kw)
            got["verify_owed"] = bool(kw.get("defer_verify"))
            return got
        for patch in (mock.patch.object(train, "train", side_effect=owing),
                      mock.patch.object(lanes, "pm_spawn", side_effect=lambda *a, **k: self.calls.append("verify"))):
            patch.start()
            self.addCleanup(patch.stop)

    def land_here(self):
        out, err = io.StringIO(), io.StringIO()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            train.cmd_land(self.p, argparse.Namespace(batch=1, first=None, bg=False, json=False), self.repo)
        return out.getvalue() + err.getvalue()

    def ask(self, result):
        def request(p, main, final):
            self.calls.append("ship")
            return result
        return mock.patch.object(train, "_ship_request", side_effect=request)

    def test_order_the_land_starts_the_verify_after_it_asked_for_the_ship(self):
        self.lanes_ready(1)
        started = ("started", {"pid": 1, "log": "x.log"})
        with self.ask(started):
            self.land_here()
        self.assertEqual(self.calls, ["ship", "verify", "ship"], "per train: ship then verify; the final ship after")

    def test_order_a_land_with_no_grant_still_starts_the_verify(self):
        self.lanes_ready(1, grant=False)
        with self.ask(None):
            self.land_here()
        self.assertIn("verify", self.calls)
        self.assertEqual(self.calls.count("verify"), 1)

    def test_order_an_in_line_ship_ends_before_the_verify_starts(self):
        self.lanes_ready(1)
        os.environ["FOREMAN_NO_BACKGROUND"] = "1"
        self.addCleanup(os.environ.pop, "FOREMAN_NO_BACKGROUND", None)
        with mock.patch.object(train, "_ship", side_effect=lambda *a, **k: self.calls.append("ship") or None):
            self.land_here()
        self.assertEqual(self.calls[:2], ["ship", "verify"])

    def test_order_a_land_that_fails_mid_way_still_starts_the_verify_it_owes(self):
        self.lanes_ready(1)
        with mock.patch.object(train, "_ship_request", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.land_here()
        self.assertEqual(self.calls, ["verify"], "the records are pending: a verify must run for them")

    def test_order_a_train_that_raises_after_its_records_were_written_still_gets_a_verify(self):
        self.lanes_ready(1)

        def killed(p, main, ids=None, **kw):  # the records are on disk, the flag never reaches the land
            self.real(p, main, ids, **kw)
            with mock.patch.dict(os.environ, {"FM_POSTMERGE_CMD": "true"}):
                lanes.pm_record(p, "a" * 40, "T-1", main, spawn=False)
            raise RuntimeError("killed during its closes")
        with mock.patch.object(train, "train", side_effect=killed), self.ask(None):
            with self.assertRaises(RuntimeError):
                self.land_here()
        self.assertEqual(self.calls, ["verify"], "a pending record with no runner is a verify to start")

    def test_order_no_owed_flag_and_no_pending_record_starts_no_verify(self):
        train.start_verify(self.p, self.repo, [False])
        self.assertEqual(self.calls, [])

    def test_order_a_pending_record_alone_starts_the_verify_once(self):
        with mock.patch.dict(os.environ, {"FM_POSTMERGE_CMD": "true"}):
            lanes.pm_record(self.p, "b" * 40, "T-2", self.repo, spawn=False)
        owed = [False]
        train.start_verify(self.p, self.repo, owed)
        self.assertEqual(self.calls, ["verify"])

    def test_order_a_two_train_land_starts_the_verify_after_the_last_ship_of_its_trains(self):
        """The verify's suite run is one long run with no per-class boundary to yield at: so it starts only once no
        further train of the land is queued, never between train 1's ship and train 2's."""
        self.lanes_ready(2)
        with self.ask(("started", {"pid": 1, "log": "x.log"})):
            self.land_here()
        self.assertEqual(self.calls.count("verify"), 1)
        self.assertEqual(self.calls[:3], ["ship", "ship", "verify"], "both trains' ships are asked before it starts")


class Yield(TrainCase):
    """The verify's runner waits while a ship of the project runs, bounded by the ship's own limit."""

    def setUp(self):
        super().setUp()
        self.p = c.find_project(self.repo)
        self.addCleanup(lambda: [os.path.exists(f) and os.remove(f) for f in [train._ship_marker(self.p)]])
        for patch in (mock.patch.object(lanes, "SHIP_YIELD_POLL", 0.02),):
            patch.start()
            self.addCleanup(patch.stop)

    def marker(self, pid=None, ticks=None):
        path = train._ship_marker(self.p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        me = os.getpid() if pid is None else pid
        c.write_atomic(path, json.dumps({"pid": me, "ticks": train._ticks(me) if ticks is None else ticks,
                                         "started": time.time(), "want": "x", "ship_head": "", "again": False,
                                         "final": False, "grant": "T-0001", "log": "x"}))

    def claim(self, pid=None):
        me = os.getpid() if pid is None else pid
        with c.lock(self.p.dir):
            meta = c.read_meta(self.p)
            meta["ship_claim"] = {"pid": me, "ticks": train._ticks(me), "started": round(time.time(), 1), "at": "", "head": "h",
                                  "picks": [], "task": "T-0001"}
            c.write_meta(self.p, meta)

    def test_yield_no_ship_is_no_wait(self):
        t0 = time.monotonic()
        self.assertFalse(lanes.ship_yield(self.p))
        self.assertLess(time.monotonic() - t0, 2)

    def test_yield_a_dead_ships_marker_is_no_wait(self):
        self.marker(pid=2 ** 22 + 7, ticks=123)
        self.assertFalse(lanes.ship_yield(self.p))

    def test_yield_a_running_ships_marker_holds_the_verify_until_it_ends(self):
        self.marker()
        threading.Timer(0.4, lambda: os.remove(train._ship_marker(self.p))).start()
        t0 = time.monotonic()
        with mock.patch.dict(os.environ, {"FOREMAN_SHIP_TIMEOUT_S": "60"}):
            self.assertTrue(lanes.ship_yield(self.p))
        self.assertGreaterEqual(time.monotonic() - t0, 0.35)
        self.assertLess(time.monotonic() - t0, 10)

    def test_yield_a_running_ships_claim_holds_the_verify_too(self):  # a hand `sampo ship` has no marker, only its claim
        self.claim()

        def drop():
            with c.lock(self.p.dir):
                meta = c.read_meta(self.p)
                meta.pop("ship_claim")
                c.write_meta(self.p, meta)
        threading.Timer(0.4, drop).start()
        t0 = time.monotonic()
        with mock.patch.dict(os.environ, {"FOREMAN_SHIP_TIMEOUT_S": "60"}):
            self.assertTrue(lanes.ship_yield(self.p))
        self.assertGreaterEqual(time.monotonic() - t0, 0.35)

    def test_yield_the_wait_ends_when_the_ships_limit_has_passed(self):
        self.marker()
        t0 = time.monotonic()
        with mock.patch.object(release, "ship_life", return_value=0.3):  # T-5985: a ship's whole life (waits included), not its suite's cap
            self.assertTrue(lanes.ship_yield(self.p))
        self.assertLess(time.monotonic() - t0, 10, "a ship that never ends can't hold the verify past its limit")
        with mock.patch.dict(os.environ, {"FOREMAN_SHIP_TIMEOUT_S": "0.3"}):
            self.assertGreater(release.ship_life(), 3000, "the suite's cap alone is no longer the verify's limit")

    def test_yield_the_runner_yields_before_each_suite_run(self):
        order = []
        recs = {"a" * 40: {"state": "pending", "task": "T-1", "n": 1}}
        with mock.patch.object(lanes, "ship_yield", side_effect=lambda p, cap=None: order.append("yield")), \
                mock.patch.object(lanes, "pm_records", return_value=recs), \
                mock.patch.object(lanes, "pm_verify", side_effect=lambda *a: order.append("verify") or False):
            lanes.pm_run(self.p, self.repo)
        self.assertEqual(order, ["yield", "verify"])


class Cut(unittest.TestCase):
    """The ship's affected run: cut by its time limit with only classes not run is a wait, not a failure."""

    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="ship-order-")
        os.makedirs(os.path.join(self.wt, "plugin", "tests"))
        open(os.path.join(self.wt, "plugin", "tests", "run.py"), "w").close()
        self.state = tempfile.mkdtemp(prefix="ship-order-state-")  # its own state folder, apart from the working tree
        self.p = c.Project("x", self.wt, self.state)
        for d in (self.wt, self.state):
            self.addCleanup(shutil.rmtree, d, True)
        self.runs = []
        for patch in (mock.patch.object(cli, "affected", return_value=("python3 plugin/tests/run.py", ["a", "b"], None)),
                      mock.patch.object(c, "wait_for_load", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def affected(self, *results):
        it = iter(results)

        def run(root, cmd, *a, **k):
            self.runs.append(cmd)
            return next(it)
        with mock.patch.object(c, "run_command", side_effect=run), contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                got = release._affected_first(self.p, self.wt, "base", {})
                return got, "", out.getvalue()
            except cli.UsageError as e:
                return None, str(e), out.getvalue()

    def test_cut_a_run_cut_only_by_its_limit_is_retried_once_on_the_classes_not_run(self):
        got, err, said = self.affected((124, CUT_OUT), (0, "OK\n"))
        self.assertEqual(err, "")
        self.assertEqual(got, ["a", "b"])
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(self.runs[1], "python3 plugin/tests/run.py test_late.Late test_later.Later")
        self.assertIn("ran out of time waiting for test slots (2 classes not run)", said)

    def test_cut_a_second_cut_is_a_wait_not_failed_tests_and_not_retried_again(self):
        got, err, _ = self.affected((124, CUT_OUT), (124, CUT_OUT))
        self.assertEqual(len(self.runs), 2, "one retry")
        self.assertIn("ran out of time waiting for test slots (2 classes not run)", err)
        self.assertIn("nothing was pushed", err)
        self.assertNotIn("tests failed", err)

    def test_cut_a_retry_that_fails_for_real_is_a_failure(self):
        got, err, _ = self.affected((124, CUT_OUT), (1, RED_OUT), (1, RED_OUT))
        self.assertIn("the affected tests failed in the scratch tree: test_bad.Bad", err)

    def test_cut_a_real_failed_class_is_refused_as_before_and_never_retried_as_a_cut(self):
        got, err, said = self.affected((1, RED_OUT), (1, RED_OUT))  # the second: the flaky check's own rerun alone
        self.assertIn("the affected tests failed in the scratch tree: test_bad.Bad", err)
        self.assertNotIn("ran out of time", err + said)
        self.assertEqual(self.runs[1:], ["python3 plugin/tests/run.py test_bad.Bad"], "only the flaky rerun, as today")

    def test_cut_a_red_class_beside_classes_not_run_is_a_failure(self):
        got, err, said = self.affected((124, RED_AND_CUT), (1, RED_OUT))
        self.assertIn("the affected tests failed", err)
        self.assertNotIn("ran out of time", err + said)

    def test_cut_a_class_still_running_at_the_deadline_is_hung_not_a_slot_wait(self):
        self.assertEqual(release._cut_only(124, HUNG_OUT), [], "a class killed at the deadline is not a wait")
        got, err, said = self.affected((124, HUNG_OUT), (124, HUNG_OUT))
        self.assertEqual(len(self.runs), 1, "no retry: it would hang again")
        self.assertIn("test_hung.Hung", err, "the hung class is named")
        self.assertIn("still running at the run's deadline", err)
        self.assertIn("nothing was pushed", err)
        self.assertNotIn("ran out of time", err + said)

    def test_cut_a_retry_that_hangs_names_the_hung_class_not_the_slots(self):
        got, err, _ = self.affected((124, CUT_OUT), (124, HUNG_OUT))
        self.assertEqual(len(self.runs), 2)
        self.assertIn("test_hung.Hung", err)
        self.assertNotIn("waiting for test slots", err)

    def test_cut_a_green_run_runs_once(self):
        got, err, _ = self.affected((0, "OK\n"))
        self.assertEqual((got, err, len(self.runs)), (["a", "b"], "", 1))

    def test_cut_the_ship_record_names_no_failing_test_for_a_wait(self):
        out = "refused: " + release.SLOTS_CUT + " (2 classes not run)\n" + CUT_OUT
        line, data = train._ship_end(self.p, "T-1", "h", 1, out, 0.0, 1.0)
        self.assertEqual(data["red"], [])
        self.assertNotIn("failing", line)
        self.assertIn(release.SLOTS_CUT, line)


# T-4788: ship-1116a: 208 affected files (6429 tests, 3846 CPU s) at load 13-16 hit the 900 s limit with two healthy
# classes 10 s into their run, and the ship blamed them as hung. A cut run is worded from its own totals.
HEALTHY_CUT = ("\n==== test_slow.Slow\nrun.py: still running at the run's deadline (10s in): killed (T-1809)\n"
               "Thread 0x1 (most recent call first):\n  File \"x.py\", line 1 in wait\n\n"
               "==== test_slow2.Slow2\nrun.py: still running at the run's deadline (11s in): killed (T-1809)\n\n"
               "Ran 6429 tests in 880.0s (20 jobs, 6 at a time; slowest: test_x.X 3s; cpu 3846.0 s)\n\n"
               "INCOMPLETE (stopped at its deadline)\nnot run (deadline): 5\n  not run class: test_slow.Slow\n"
               "  not run class: test_slow2.Slow2\n  not run class: test_a.A\n  not run class: test_b.B\n"
               "  not run class: test_c.C\nrun.py: stopped at its deadline\n")
SUITE = {f"test_m{i}" for i in range(30)}


def suite_times(mine, theirs, mine_n=3):
    """Recorded class times: test_m0..test_m<mine_n-1> take `mine` s each, the other modules `theirs` s each."""
    return {f"test_m{i}.C": float(mine if i < mine_n else theirs) for i in range(30)}


class Scaled(unittest.TestCase):
    """T-4788: the affected stage's limit follows the set's recorded time and the machine's free CPUs; a set too big for
    it is skipped (the full check covers it); a run cut with healthy classes running goes on to the full check."""
    setUp = Cut.setUp
    limit = None

    def affected(self, *results):
        it = iter(results)

        def run(root, cmd, timeout=600, **k):
            self.runs.append(cmd)
            self.limit = timeout
            return next(it)
        with mock.patch.object(c, "run_command", side_effect=run), contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                return release._affected_first(self.p, self.wt, "base", {}), "", out.getvalue()
            except cli.UsageError as e:
                return None, str(e), out.getvalue()

    def plan(self, times, load=4, cpus=16, tests=("plugin/tests/test_m0.py", "plugin/tests/test_m1.py",
                                                     "plugin/tests/test_m2.py"), suite=SUITE, jobs=0):
        patches = (mock.patch.object(cli, "affected", return_value=("python3 plugin/tests/run.py", list(tests), None)),
                   mock.patch.object(release, "_run_times", return_value=(times, set(suite), jobs)),
                   mock.patch.object(c, "host_load", return_value=(load, cpus)))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_scaled_a_set_over_a_third_of_the_suite_is_skipped_with_one_line_naming_the_counts(self):
        self.plan(suite_times(2000, 10), tests=[f"plugin/tests/test_m{i}.py" for i in range(4)])
        got, err, said = self.affected()
        self.assertEqual((got, err, self.runs), ([], "", []), "no run: the full check covers it")
        self.assertEqual(len(said.strip().splitlines()), 1, said)
        self.assertIn("affected set is 4 of 30 test files", said)
        self.assertIn("the full check covers it", said)

    def test_scaled_the_208_file_case_is_skipped_by_share_and_by_time(self):
        suite = {f"test_n{i}" for i in range(587)}
        times = {f"test_n{i}.C": 3846 / 208 if i < 208 else 7.0 for i in range(587)}
        self.plan(times, load=14, tests=[f"plugin/tests/test_n{i}.py" for i in range(208)], suite=suite)
        got, err, said = self.affected()
        self.assertEqual(self.runs, [])
        self.assertIn("affected set is 208 of 587 test files", said)
        self.assertIn("3846", said)
        self.assertIn("the full check covers it", said)

    def test_scaled_a_smaller_set_gets_a_limit_from_its_recorded_cpu_time_and_the_free_cpus(self):
        self.plan(suite_times(400, 200), load=14)  # 1200 CPU s over 2 free CPUs: 600 s, times 1.5, plus 60
        got, err, said = self.affected((0, "OK\n"))
        self.assertEqual(err, "")
        self.assertEqual(self.limit, 960)
        self.assertIn("1200 recorded s", said)
        self.assertNotIn("CPU s", said, "the record is wall seconds per class, not CPU seconds")

    def test_scaled_the_limit_has_a_floor_on_a_quiet_machine(self):
        self.plan(suite_times(400, 200), load=1)  # 1200 CPU s over 15 free CPUs: 80 s: the floor
        self.affected((0, "OK\n"))
        self.assertEqual(self.limit, release.AFFECTED_FLOOR)

    def test_scaled_a_set_that_cannot_finish_under_the_ceiling_is_skipped(self):
        self.plan(suite_times(1500, 600), load=14)  # 4500 of 20100 CPU s: under a third, but 3435 s at 2 CPUs
        got, err, said = self.affected()
        self.assertEqual(self.runs, [])
        self.assertIn("would take", said)
        self.assertIn("the full check covers it", said)

    def test_scaled_no_record_falls_back_to_the_file_count_and_says_so(self):
        self.plan({}, tests=[f"plugin/tests/test_m{i}.py" for i in range(12)])
        got, err, said = self.affected()
        self.assertEqual(self.runs, [])
        self.assertIn("by file count", said)
        self.assertIn("12 of 30", said)

    def test_scaled_no_record_and_a_small_set_keeps_the_old_limit(self):
        self.plan({})
        self.affected((0, "OK\n"))
        self.assertEqual(self.limit, 900)

    def test_scaled_a_run_cut_with_healthy_classes_running_blames_none_and_goes_on(self):
        self.plan({"test_slow.Slow": 3.0, "test_slow2.Slow2": 4.0})
        got, err, said = self.affected((124, HEALTHY_CUT))
        self.assertEqual((err, len(self.runs)), ("", 1), "no retry, no refusal")
        self.assertEqual(got, [])
        self.assertIn("ran out of time", said)
        self.assertIn("15 of 20 classes done", said)
        self.assertIn("3 not started", said)
        self.assertIn("2 running", said)
        self.assertIn("test_slow.Slow 10 s in", said)
        self.assertIn("no test failed", said)
        self.assertIn("the full check", said)
        self.assertNotIn("hung", said)

    def test_scaled_a_class_with_no_record_a_few_seconds_in_is_not_called_hung(self):
        self.plan({})
        got, err, said = self.affected((124, HEALTHY_CUT))
        self.assertEqual(err, "")
        self.assertNotIn("hung", said)

    def test_scaled_a_class_far_over_its_recorded_time_is_still_hung(self):
        out = HEALTHY_CUT.replace("(10s in)", "(700s in)")
        self.plan({"test_slow.Slow": 3.0, "test_slow2.Slow2": 4.0})
        got, err, said = self.affected((124, out))
        self.assertEqual(len(self.runs), 1)
        self.assertIn("test_slow.Slow", err)
        self.assertNotIn("test_slow2.Slow2", err, "only the class far over its time is named")
        self.assertIn("hung", err)
        self.assertIn("nothing was pushed", err)

    def test_scaled_a_real_failure_beside_the_running_classes_still_stops_the_ship(self):
        self.plan({"test_slow.Slow": 3.0, "test_slow2.Slow2": 4.0})
        got, err, said = self.affected((124, RED_OUT + HEALTHY_CUT), (1, RED_OUT))
        self.assertIn("the affected tests failed in the scratch tree: test_bad.Bad", err)
        self.assertNotIn("ran out of time", said)

    def test_scaled_a_cut_of_a_files_only_run_is_not_a_pass_the_full_run_follows(self):
        self.plan({"test_slow.Slow": 3.0, "test_slow2.Slow2": 4.0})
        with mock.patch.object(c, "run_command", side_effect=lambda *a, **k: (124, HEALTHY_CUT)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(release._affected_first(self.p, self.wt, "base", {}, only=["plugin/x.py"]))

    def test_scaled_a_hung_class_the_stage_let_through_is_refused_by_the_full_check_and_kept_in_its_output(self):
        line = release._full_refusal(self.p, HUNG_OUT, 124, [], [], "python3 plugin/tests/run.py")
        self.assertIn("nothing was pushed", line)
        path = release._keep_output(self.p, HUNG_OUT, "x")
        with open(path, encoding="utf-8") as f:
            self.assertIn("test_hung.Hung", f.read())

    def test_scaled_the_times_come_from_the_trees_own_run_py(self):
        tree = tempfile.mkdtemp(prefix="ship-times-")
        self.addCleanup(shutil.rmtree, tree, True)
        tests = os.path.join(tree, "plugin", "tests")
        os.makedirs(tests)
        for n in ("run.py", "test_a.py", "test_b.py", "helper.py"):
            open(os.path.join(tests, n), "w").close()
        with open(os.path.join(tests, "run.py"), "w") as f:
            f.write("def load_times():\n    return {'test_a.A': 2.5, 'test_a.A.test_x': 1.0}\n")
        times, suite, _ = release._run_times(tree, "python3 plugin/tests/run.py a")
        self.assertEqual((times, suite), ({"test_a.A": 2.5, "test_a.A.test_x": 1.0}, {"test_a", "test_b"}))
        with open(os.path.join(tests, "run.py"), "w") as f:
            f.write("raise SystemExit(3)\n")
        self.assertEqual(release._run_times(tree, "python3 plugin/tests/run.py a"), ({}, {"test_a", "test_b"}, 0))
        self.assertEqual(release._run_times(tree, "python3 -m pytest -q a"), ({}, set(), 0), "another runner: no record")

    def stub_tree(self, run_py, names=("run.py", "test_a.py")):
        tree = tempfile.mkdtemp(prefix="ship-times-")
        self.addCleanup(shutil.rmtree, tree, True)
        tests = os.path.join(tree, "plugin", "tests")
        os.makedirs(tests)
        for n in names:
            open(os.path.join(tests, n), "w").close()
        with open(os.path.join(tests, "run.py"), "w") as f:
            f.write(run_py)
        return tree

    # --- T-4788 review: sizing never crashes a ship, and the record is trusted only as far as it is sane
    def test_scaled_a_record_of_all_zeros_is_no_record_not_a_division_by_zero(self):
        self.plan(suite_times(0, 0))
        got, err, _ = self.affected((0, "OK\n"))
        self.assertEqual((err, self.limit), ("", release.AFFECTED_OLD), "no usable record: the old limit")
        self.plan(suite_times(0, 0), tests=[f"plugin/tests/test_m{i}.py" for i in range(12)])
        got, err, said = self.affected()
        self.assertIn("by file count", said)

    def test_scaled_a_host_with_no_cpus_or_no_reading_does_not_crash_the_sizing(self):
        for host in ((3, 0), None):
            with self.subTest(host=host):
                self.plan(suite_times(400, 200))
                with mock.patch.object(c, "host_load", return_value=host):
                    got, err, said = self.affected((0, "OK\n"))
                self.assertEqual(err, "")
                self.assertGreaterEqual(self.limit, release.AFFECTED_FLOOR)

    def test_scaled_any_error_in_the_sizing_falls_back_to_the_old_limit(self):
        self.plan(suite_times(400, 200))
        with mock.patch.object(release, "_affected_limit", side_effect=ZeroDivisionError("x")):
            got, err, said = self.affected((0, "OK\n"))
        self.assertEqual((err, self.limit), ("", release.AFFECTED_OLD))
        self.plan(suite_times(400, 200))
        with mock.patch.object(release, "_run_times", side_effect=RuntimeError("x")):
            got, err, said = self.affected((0, "OK\n"))
        self.assertEqual((err, self.limit), ("", release.AFFECTED_OLD))

    def test_scaled_the_record_keeps_only_finite_non_negative_seconds(self):
        tree = self.stub_tree("def load_times():\n    return {'test_a.A': float('nan'), 'test_a.B': -3.0, "
                              "'test_a.C': float('inf'), 'test_a.D': 2.0, 'test_a.E': True, 'test_a.F': 0.0}\n")
        times, _, _ = release._run_times(tree, "python3 plugin/tests/run.py a")
        self.assertEqual(times, {"test_a.D": 2.0, "test_a.F": 0.0})

    def test_scaled_the_times_child_gets_the_ships_env_not_the_hooks_git_vars(self):
        seen = os.path.join(tempfile.mkdtemp(prefix="ship-env-"), "seen")
        self.addCleanup(shutil.rmtree, os.path.dirname(seen), True)
        tree = self.stub_tree("import os\ndef load_times():\n    open(%r, 'w').write(repr((os.environ.get('GIT_DIR'), "
                              "os.environ.get('SHIP_MARK'))))\n    return {}\n" % seen)
        with mock.patch.dict(os.environ, {"GIT_DIR": "/not/the/repo"}):
            release._run_times(tree, "python3 plugin/tests/run.py a", {"SHIP_MARK": "yes", "PATH": os.environ["PATH"]})
        with open(seen) as f:
            self.assertEqual(f.read(), repr((None, "yes")))
        with mock.patch.dict(os.environ, {"GIT_DIR": "/not/the/repo"}):  # no env given: the process's, minus GIT_*
            release._run_times(tree, "python3 plugin/tests/run.py a")
        with open(seen) as f:
            self.assertEqual(f.read(), repr((None, None)))

    def test_scaled_a_times_child_that_hangs_is_killed_with_everything_it_started(self):
        pidfile = os.path.join(tempfile.mkdtemp(prefix="ship-hang-"), "pid")
        self.addCleanup(shutil.rmtree, os.path.dirname(pidfile), True)
        tree = self.stub_tree("import subprocess, sys, time\ndef load_times():\n    p = subprocess.Popen([sys.executable, "
                              "'-c', 'import time; time.sleep(120)'])\n    open(%r, 'w').write(str(p.pid))\n"
                              "    time.sleep(120)\n" % pidfile)
        t0 = time.monotonic()
        got = release._run_times(tree, "python3 plugin/tests/run.py a", wait=2)
        self.assertEqual(got, ({}, {"test_a"}, 0), "no record when the reader hangs")
        self.assertLess(time.monotonic() - t0, 60)
        with open(pidfile) as f:
            pid = int(f.read())
        for _ in range(100):  # the kill has landed; init reaps the orphan
            try:
                os.kill(pid, 0)
                with open(f"/proc/{pid}/stat") as f:
                    if f.read().rpartition(")")[2].split()[0] == "Z":
                        break
            except (OSError, ValueError):
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            self.fail("the reader's own child was left running")

    def test_scaled_a_reader_that_prints_an_object_or_junk_is_no_record_not_a_crash(self):
        for shown in ("{'a': 1}", "'text'", "[]", "[1]", "[{}, 'x']", "None"):
            with self.subTest(shown=shown):
                tree = self.stub_tree(f"import json\nprint(json.dumps({shown}))\nraise SystemExit(0)\n")
                self.assertEqual(release._run_times(tree, "python3 plugin/tests/run.py a"), ({}, {"test_a"}, 0))

    def test_scaled_a_child_the_reader_left_running_after_a_clean_exit_is_ended_too(self):
        pidfile = os.path.join(tempfile.mkdtemp(prefix="ship-left-"), "pid")
        self.addCleanup(shutil.rmtree, os.path.dirname(pidfile), True)
        tree = self.stub_tree("import subprocess, sys\ndef load_times():\n    p = subprocess.Popen([sys.executable, "
                              "'-c', 'import time; time.sleep(120)'], stdout=subprocess.DEVNULL)\n"
                              "    open(%r, 'w').write(str(p.pid))\n    return {'test_a.A': 1.0}\n" % pidfile)
        got = release._run_times(tree, "python3 plugin/tests/run.py a")
        self.assertEqual(got[0], {"test_a.A": 1.0}, "the reader itself ended cleanly with its record")
        with open(pidfile) as f:
            pid = int(f.read())
        for _ in range(100):
            try:
                os.kill(pid, 0)
                with open(f"/proc/{pid}/stat") as f:
                    if f.read().rpartition(")")[2].split()[0] == "Z":
                        break
            except (OSError, ValueError):
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            self.fail("the reader's child outlived the read")

    def test_scaled_the_divisor_is_capped_at_the_jobs_the_run_will_use(self):
        self.plan(suite_times(400, 200), load=1, jobs=4)  # 1200 s over min(15 free, 4 jobs): 300 s, times 1.5, plus 60
        self.affected((0, "OK\n"))
        self.assertEqual(self.limit, 510)
        self.plan(suite_times(400, 200), load=1, jobs=0)  # no -j known: the free CPUs alone
        self.affected((0, "OK\n"))
        self.assertEqual(self.limit, release.AFFECTED_FLOOR)

    def test_scaled_the_jobs_are_the_commands_own_j_else_run_pys_default(self):
        tree = self.stub_tree("def load_times():\n    return {}\ndef slot_count():\n    return 6\n"
                              "def usable_cpus(folder=None):\n    return 5\ndef _cgroup_dir():\n    return None\n")
        self.assertEqual(release._run_times(tree, "python3 plugin/tests/run.py a")[2], 5)
        self.assertEqual(release._run_times(tree, "python3 plugin/tests/run.py -j 2 a")[2], 2)
        self.assertEqual(release._run_times(tree, "python3 plugin/tests/run.py -j3 a")[2], 3)

    def test_scaled_the_real_run_py_is_read_once_through_its_own_load_times(self):
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(os.path.dirname(here))
        rec = os.path.join(tempfile.mkdtemp(prefix="ship-rec-"), "times.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(rec), True)
        with open(rec, "w") as f:
            json.dump({"test_a.A": 1.5, "test_a.A.test_x": 0.5}, f)
        times, suite, jobs = release._run_times(root, "python3 plugin/tests/run.py a",
                                                dict(os.environ, FM_TEST_TIMES=rec))
        self.assertEqual(times, {"test_a.A": 1.5, "test_a.A.test_x": 0.5})
        self.assertIn("test_train_ship_order", suite)
        self.assertGreaterEqual(jobs, 1, "run.py's own -j default")

    # --- T-4788 review: a files-only run that did not run everything is not a pass
    def test_scaled_only_a_real_fail_beside_a_killed_class_is_not_a_pass_when_the_failed_one_is_green_alone(self):
        self.plan({"test_slow.Slow": 3.0})
        out = RED_OUT + HEALTHY_CUT
        with mock.patch.object(c, "run_command", side_effect=[(1, out), (0, "OK\n")]), \
                contextlib.redirect_stdout(io.StringIO()):
            got = release._affected_first(self.p, self.wt, "base", {}, only=["plugin/x.py"])
        self.assertIsNone(got, "the killed and not-run classes never passed: the suite runs")

    def test_scaled_only_a_real_fail_beside_a_not_run_class_is_not_a_pass_either(self):
        self.plan({})
        out = RED_AND_CUT
        with mock.patch.object(c, "run_command", side_effect=[(1, out), (0, "OK\n")]), \
                contextlib.redirect_stdout(io.StringIO()):
            got = release._affected_first(self.p, self.wt, "base", {}, only=["plugin/x.py"])
        self.assertIsNone(got)

    def test_scaled_the_range_run_with_the_same_flake_still_passes_the_full_check_follows(self):
        self.plan({"test_slow.Slow": 3.0})
        got, err, said = self.affected((1, RED_OUT + HEALTHY_CUT), (0, "OK\n"))
        self.assertEqual((got, err), (["plugin/tests/test_m0.py", "plugin/tests/test_m1.py", "plugin/tests/test_m2.py"], ""))

    def test_scaled_the_ship_runs_the_suite_when_the_files_only_run_was_cut(self):
        with mock.patch.object(release, "_affected_first", return_value=None) as run:
            hit, ran = release._reuse_after_affected(self.p, self.wt, "base", {}, "the gates' pass (x)", ["f.md"])
        self.assertEqual((hit, ran), (None, None), "no reuse: the suite runs")
        self.assertEqual(run.call_args.kwargs, {"only": ["f.md"]})
        with mock.patch.object(release, "_affected_first", return_value=["t.py"]):
            self.assertEqual(release._reuse_after_affected(self.p, self.wt, "base", {}, "pass", ["f.md"]), ("pass", ["t.py"]))
        with mock.patch.object(release, "_affected_first") as run:
            self.assertEqual(release._reuse_after_affected(self.p, self.wt, "base", {}, "pass", []), ("pass", []))
            self.assertEqual(release._reuse_after_affected(self.p, self.wt, "base", {}, None, ["f.md"]), (None, []))
        run.assert_not_called()

    def test_scaled_a_cut_files_only_run_through_the_real_stage_sends_the_ship_to_the_suite(self):
        self.plan({"test_slow.Slow": 3.0, "test_slow2.Slow2": 4.0})
        with mock.patch.object(c, "run_command", side_effect=lambda *a, **k: (124, HEALTHY_CUT)), \
                contextlib.redirect_stdout(io.StringIO()):
            hit, ran = release._reuse_after_affected(self.p, self.wt, "base", {}, "the gates' pass (x)", ["plugin/x.py"])
        self.assertIsNone(hit)


if __name__ == "__main__":
    unittest.main()
