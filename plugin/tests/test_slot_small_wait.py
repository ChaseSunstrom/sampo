"""T-4536: a small run (a few classes) never queues for minutes behind full suites: it takes an express slot or, after
SMALL_WAIT seconds, runs anyway; a run that waits for a slot says so once, with the seconds it has waited so far."""
import contextlib
import io
import os
import re
import threading
import time
from unittest import mock

from helpers import ForemanTestCase

import run


class SmallWait(ForemanTestCase):
    def setUp(self):
        super().setUp()
        env = {"XDG_RUNTIME_DIR": self.tmp, "SAMPO_SLOTS": "1"}  # one pool slot, both express slots free
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        for k in (run.SLOT_ENV, "FOREMAN_SLOT", "FOREMAN_SLOTS", "SAMPO_SLOT_PRIORITY", "FOREMAN_SLOT_PRIORITY"):
            os.environ.pop(k, None)
        self.addCleanup(self.patch.stop)
        said = mock.patch.object(run, "_SAID", [])  # the wait line is said once per process: each test starts unsaid
        said.start()
        self.addCleanup(said.stop)

    def hold(self, small=False):
        """A slot held by a thread until the returned event is set: (its name, the event)."""
        held, free, box = threading.Event(), threading.Event(), []

        def body():
            with run.cpu_slot(wait=5, poll=0.01, small=small) as s:
                box.append(s)
                held.set()
                free.wait(30)

        t = threading.Thread(target=body, daemon=True)
        t.start()
        self.addCleanup(lambda: (free.set(), t.join(30)))
        self.assertTrue(held.wait(5), "the slot was not free")
        return box[0], free

    def hold_all(self):
        """The machine's whole pool (one slot here) and both express slots: nothing is free."""
        self.assertEqual(self.hold()[0], 0)
        self.assertEqual(self.hold(small=True)[0], "x0")
        self.assertEqual(self.hold(small=True)[0], "x1")

    def test_the_bound_is_about_twenty_seconds_and_the_line_comes_at_five(self):
        self.assertEqual(run.SMALL_WAIT, 20.0)
        self.assertEqual(run.SAY_WAIT, 5.0)

    def test_a_small_run_runs_anyway_after_the_short_bound_when_every_slot_is_held(self):
        # T-4536: 13 per-test jobs of one 14 s module queued for the pool behind full suites: 786 s, zero tests run
        self.hold_all()
        out, start = io.StringIO(), time.monotonic()
        with mock.patch.object(run, "SMALL_WAIT", 0.5), mock.patch.object(run, "SAY_WAIT", 0.1), \
                contextlib.redirect_stderr(out):
            with run.cpu_slot(wait=8, poll=0.01, small=True) as s:
                waited = time.monotonic() - start
                self.assertIsNone(s, "every slot is held: it runs with none rather than wait out its wait")
        self.assertGreaterEqual(waited, 0.4)
        self.assertLess(waited, 5)
        self.assertRegex(out.getvalue(), r"^run\.py: waiting for a CPU slot, \d+ s$", out.getvalue())

    def test_a_small_run_takes_a_pool_slot_freed_within_the_bound(self):
        _, free_pool = self.hold()
        self.hold(small=True)
        self.hold(small=True)
        threading.Timer(0.3, free_pool.set).start()
        with mock.patch.object(run, "SMALL_WAIT", 5.0):
            with run.cpu_slot(wait=60, poll=0.01, small=True) as s:
                self.assertEqual(s, 0, "the pool slot freed inside the bound is taken")

    def test_a_large_run_still_queues_for_the_pool_past_the_small_bound(self):
        # full suites stay queued: the bound is for a small run only
        _, free_pool = self.hold()
        threading.Timer(0.8, free_pool.set).start()
        with mock.patch.object(run, "SMALL_WAIT", 0.2):
            with run.cpu_slot(wait=10, poll=0.01) as s:
                self.assertEqual(s, 0, "a run of more classes waits for the pool, past the small bound")

    def test_a_run_that_waits_says_once_how_long_it_has_waited(self):
        # two classes of one run.py wait together: one line, not one per class
        _, free_pool = self.hold()
        out, got = io.StringIO(), []

        def waiter():
            with run.cpu_slot(wait=10, poll=0.01) as s:
                got.append(s)

        with mock.patch.object(run, "SAY_WAIT", 0.3), contextlib.redirect_stderr(out):
            threads = [threading.Thread(target=waiter) for _ in range(2)]
            for t in threads:
                t.start()
            time.sleep(1.2)
            free_pool.set()
            for t in threads:
                t.join(10)
        self.assertEqual(len(got), 2, got)
        lines = re.findall(r"^run\.py: waiting for a CPU slot, (\d+) s$", out.getvalue(), re.M)
        self.assertEqual(len(lines), 1, out.getvalue())
        self.assertGreaterEqual(int(lines[0]), 0)

    def test_a_run_that_gets_a_slot_at_once_says_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out), run.cpu_slot(wait=10, poll=0.01) as s:
            self.assertEqual(s, 0)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    import unittest
    unittest.main()
