"""T-4570: a waiting builder's run goes before a queued post-merge-verify class, up to a bound; and every run's record
says how long it ran, waited and held slots, and of which kind, so who held the slots is on disk, not rebuilt."""
import os
import threading
import time
from unittest import mock

from helpers import ForemanTestCase

import run
from sampo import core as c


class LowYieldsToBuilders(ForemanTestCase):
    def setUp(self):
        super().setUp()
        env = {"XDG_RUNTIME_DIR": self.tmp, "SAMPO_SLOTS": "1"}  # one pool slot
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        for k in (run.SLOT_ENV, "FOREMAN_SLOT", "FOREMAN_SLOTS", "SAMPO_SLOT_PRIORITY", "FOREMAN_SLOT_PRIORITY",
                  run.LOW_ENV, "FOREMAN_SLOT_LOW"):
            os.environ.pop(k, None)
        self.addCleanup(self.patch.stop)
        for name, val in (("_SAID", []), ("_LOW", {"until": None})):
            m = mock.patch.object(run, name, val)
            m.start()
            self.addCleanup(m.stop)
        self.folder = run._slot_folder()

    def hold(self):
        """The one pool slot, held by a thread until the returned event is set."""
        held, free = threading.Event(), threading.Event()

        def body():
            with run.cpu_slot(wait=5, poll=0.01):
                held.set()
                free.wait(30)

        t = threading.Thread(target=body, daemon=True)
        t.start()
        self.addCleanup(lambda: (free.set(), t.join(30)))
        self.assertTrue(held.wait(5), "the slot was not free")
        return free

    def markers(self, prefix):
        return [x for x in os.listdir(self.folder) if x.startswith(prefix)]

    def wait_for(self, what, secs=5):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if what():
                return True
            time.sleep(0.01)
        return what()

    def test_a_waiting_builder_takes_a_freed_slot_before_a_queued_low_class(self):
        for _ in range(3):  # without the yield the two poll for it and either may win: three rounds
            free, order, lock = self.hold(), [], threading.Lock()

            def builder():
                with run.cpu_slot(wait=10, poll=0.01):
                    with lock:
                        order.append("builder")
                    time.sleep(0.2)  # holds it: the low class cannot take it meanwhile

            def low():
                with run.cpu_slot(wait=10, poll=0.01, low=True):
                    with lock:
                        order.append("low")

            tb = threading.Thread(target=builder, daemon=True)
            tb.start()
            self.assertTrue(self.wait_for(lambda: self.markers("wait-")), "a waiting builder leaves its marker")
            tl = threading.Thread(target=low, daemon=True)
            tl.start()
            time.sleep(0.1)  # both poll for it
            free.set()
            tb.join(10)
            tl.join(10)
            self.assertEqual(order, ["builder", "low"])

    def test_a_low_class_waits_for_a_builder_no_longer_than_the_bound(self):
        # a builder marker that is never served (a stuck waiter): the verify class must still start
        mark = run._mark(self.folder, "wait-")
        self.addCleanup(lambda: (os.close(mark[0]), os.path.exists(mark[1]) and os.unlink(mark[1])))
        self.assertEqual(run.LOW_MAX, 600.0)
        with mock.patch.object(run, "LOW_MAX", 0.4):
            start = time.monotonic()
            with run.cpu_slot(wait=60, poll=0.01, low=True) as s:
                waited = time.monotonic() - start
                self.assertEqual(s, 0, "the free slot is taken once the bound is past")
        self.assertGreaterEqual(waited, 0.3, "it yielded to the waiting builder first")
        self.assertLess(waited, 10)

    def test_the_bound_is_one_window_per_process_not_one_per_class(self):
        # a class after the window has passed does not yield again: 100 classes cannot each yield LOW_MAX
        mark = run._mark(self.folder, "wait-")
        self.addCleanup(lambda: (os.close(mark[0]), os.path.exists(mark[1]) and os.unlink(mark[1])))
        with mock.patch.object(run, "LOW_MAX", 0.3):
            with run.cpu_slot(wait=60, poll=0.01, low=True):
                pass
            start = time.monotonic()
            with run.cpu_slot(wait=60, poll=0.01, low=True) as s:
                self.assertEqual(s, 0)
        self.assertLess(time.monotonic() - start, 0.2, "the window is spent: the second class takes the slot at once")

    def test_a_normal_run_ignores_a_builder_marker(self):
        mark = run._mark(self.folder, "wait-")
        self.addCleanup(lambda: (os.close(mark[0]), os.path.exists(mark[1]) and os.unlink(mark[1])))
        start = time.monotonic()
        with run.cpu_slot(wait=60, poll=0.01) as s:
            self.assertEqual(s, 0)
        self.assertLess(time.monotonic() - start, 0.2)

    def test_a_waiting_priority_run_still_goes_before_a_builder_and_a_low_class(self):
        mark = run._mark(self.folder, "prio-")
        self.addCleanup(lambda: (os.close(mark[0]), os.path.exists(mark[1]) and os.unlink(mark[1])))
        for low in (False, True):
            start = time.monotonic()
            with mock.patch.object(run, "DEFER_MAX", 0.3):
                with run.cpu_slot(wait=1000, poll=0.01, low=low) as s:
                    self.assertEqual(s, 0)
            self.assertGreaterEqual(time.monotonic() - start, 0.25, "a gate waiting leaves the slot to it first")

    def test_a_process_with_many_waiters_leaves_one_marker_and_removes_it(self):
        free = self.hold()
        done = []

        def waiter():
            with run.cpu_slot(wait=10, poll=0.01):
                time.sleep(0.05)
            done.append(1)

        threads = [threading.Thread(target=waiter, daemon=True) for _ in range(3)]
        for t in threads:
            t.start()
        self.assertTrue(self.wait_for(lambda: self.markers("wait-")))
        time.sleep(0.1)
        self.assertEqual(len(self.markers("wait-")), 1, "one marker for this process, however many classes wait")
        free.set()
        for t in threads:
            t.join(10)
        self.assertEqual(len(done), 3)
        self.assertEqual(self.markers("wait-"), [], "no waiter left: the marker is gone")

    def test_a_dead_builders_marker_is_not_waited_for(self):
        path = os.path.join(self.folder, "wait-1-deadbeef")
        open(path, "w").close()  # nobody holds a lock on it
        self.assertFalse(run._prio_waiting(self.folder, "wait-"))
        self.assertFalse(os.path.exists(path), "swept")

    def test_the_verify_command_is_low_and_a_gate_is_not(self):
        env = {k: v for k, v in os.environ.items() if k not in ("FOREMAN_SLOT_LOW", "SAMPO_SLOT_LOW")}
        code, out = c.run_command(self.tmp, "echo low=$FOREMAN_SLOT_LOW", 30, env=env, low=True)
        self.assertEqual((code, out.strip()), (0, "low=1"))
        code, out = c.run_command(self.tmp, "echo low=$FOREMAN_SLOT_LOW", 30, env=env)
        self.assertEqual((code, out.strip()), (0, "low="))

    def test_the_low_switch_is_an_operator_switch_never_a_tests(self):
        self.assertIn("SLOT_LOW", run.OPERATOR_SWITCHES)


class HeldSlotsAreRecorded(ForemanTestCase):
    def setUp(self):
        super().setUp()
        env = {"XDG_RUNTIME_DIR": self.tmp, "SAMPO_SLOTS": "2"}
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        for k in (run.SLOT_ENV, "FOREMAN_SLOT", "FOREMAN_SLOTS", "SAMPO_SLOT_PRIORITY", "FOREMAN_SLOT_PRIORITY",
                  run.LOW_ENV, "FOREMAN_SLOT_LOW"):
            os.environ.pop(k, None)
        self.addCleanup(self.patch.stop)

    def test_a_class_that_ran_in_a_slot_has_its_held_seconds_kept(self):
        with mock.patch.object(run, "HELD", {}), mock.patch.object(run, "WAITED", {}), \
                mock.patch.object(run, "_run_one", lambda *a: (time.sleep(0.2), ("cls", 1, "", 1, 0.2))[1]):
            res = run.run_one("cls", None)
            self.assertEqual(res[0], "cls")
            self.assertGreaterEqual(run.HELD["cls"], 0.19)
            self.assertLess(run.HELD["cls"], 5)
            self.assertIn("cls", run.WAITED)

    def test_a_class_run_with_no_slot_holds_none(self):
        os.environ["SAMPO_SLOTS"] = "0"
        with mock.patch.object(run, "HELD", {}), mock.patch.object(run, "WAITED", {}), \
                mock.patch.object(run, "_run_one", lambda *a: ("cls", 1, "", 1, 0.0)):
            run.run_one("cls", None)
            self.assertEqual(run.HELD.get("cls", 0), 0)

    def test_the_kind_of_a_run_is_its_priority_class(self):
        self.assertEqual(run.slot_kind(), "normal")
        with mock.patch.dict(os.environ, {"SAMPO_SLOT_PRIORITY": "1"}):
            self.assertEqual(run.slot_kind(), "priority")
        with mock.patch.dict(os.environ, {"SAMPO_SLOT_LOW": "1"}):
            self.assertEqual(run.slot_kind(), "low")

    def test_the_record_carries_started_seconds_waited_held_and_kind(self):
        with mock.patch.object(run, "HELD", {"a": 3.0, "b": 4.5}), mock.patch.object(run, "WAITED", {"a": 10.0, "b": 2.5}):
            got = run.slot_fields(100.0, 160.0)
        self.assertEqual(got, {"started": 100.0, "seconds": 60.0, "waited_s": 12.5, "held_s": 7.5, "kind": "normal"})

    def test_held_by_kind_adds_up_a_days_records_and_counts_the_unrecorded(self):
        recs = [{"t": 1_700_000_000, "kind": "low", "held_s": 100.0, "waited_s": 5.0, "seconds": 20.0},
                {"t": 1_700_000_100, "kind": "low", "held_s": 50.0, "waited_s": 0.0, "seconds": 10.0},
                {"t": 1_700_000_200, "kind": "priority", "held_s": 400.0, "waited_s": 1.0, "seconds": 60.0},
                {"t": 1_700_000_300},  # before the record had these fields
                {"t": 1_600_000_000, "kind": "low", "held_s": 9999.0}]  # another day
        got, unrecorded = run.held_by_kind(recs, "2023-11-14")
        self.assertEqual(got["low"], {"runs": 2, "held_s": 150.0, "waited_s": 5.0, "seconds": 30.0})
        self.assertEqual(got["priority"]["held_s"], 400.0)
        self.assertEqual(unrecorded, 1)
