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
        with mock.patch.dict(os.environ, {"FOREMAN_SHIP_TIMEOUT_S": "0.3"}):
            self.assertTrue(lanes.ship_yield(self.p))
        self.assertLess(time.monotonic() - t0, 10, "a ship that never ends can't hold the verify past its limit")

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


if __name__ == "__main__":
    unittest.main()
