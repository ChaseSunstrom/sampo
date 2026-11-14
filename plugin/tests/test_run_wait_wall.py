"""T-4582: run.py's summary says the wall-clock time the run spent with a job waiting for a slot, and its share of the
run, before the figure summed over its worker threads (which, with -j 8, adds up to about 8 times the wall)."""
import contextlib
import threading
import time
from unittest import mock

from helpers import ForemanTestCase

import run


class WaitWall(ForemanTestCase):
    def test_two_jobs_waiting_at_once_sum_twice_and_count_once_in_wall(self):
        spans = [(10.0, 110.0), (10.0, 110.0)]
        self.assertEqual(run.wall_wait(spans), 100.0)
        waits = {"a": 100.0, "b": 100.0}
        self.assertEqual(sum(waits.values()), 200.0)
        note = run.waited_note(waits, spans, 400.0)
        self.assertIn("waited 100 s of 400 s wall for a slot (25%)", note)
        self.assertIn("200 s summed over jobs", note)

    def test_the_union_joins_overlapping_and_keeps_apart_the_disjoint(self):
        self.assertEqual(run.wall_wait([]), 0.0)
        self.assertEqual(run.wall_wait([(0.0, 10.0), (5.0, 20.0), (30.0, 40.0)]), 30.0)
        self.assertEqual(run.wall_wait([(30.0, 40.0), (0.0, 10.0), (5.0, 20.0)]), 30.0)  # order does not matter
        self.assertEqual(run.wall_wait([(0.0, 50.0), (10.0, 20.0)]), 50.0)  # one span inside another
        self.assertEqual(run.wall_wait([(5.0, 5.0), (9.0, 3.0)]), 0.0)  # empty or backwards: nothing

    def test_the_wall_figure_comes_before_the_summed_one(self):
        note = run.waited_note({"a": 300.0, "b": 300.0}, [(0.0, 300.0), (0.0, 300.0)], 600.0)
        self.assertLess(note.index("wall for a slot"), note.index("summed over jobs"))
        self.assertTrue(note.startswith("; waited 300 s of 600 s wall for a slot (50%)"))

    def test_a_run_that_waited_little_in_wall_says_nothing(self):
        self.assertEqual(run.waited_note({"a": 4.0}, [(0.0, 4.0)], 100.0), "")
        self.assertEqual(run.waited_note({}, [], 100.0), "")
        # many threads, a short wall: the summed figure alone is no reason to speak
        self.assertEqual(run.waited_note({c: 3.0 for c in "abcdefgh"}, [(0.0, 3.0)] * 8, 100.0), "")

    def test_a_wait_without_any_run_wall_still_names_both_figures(self):
        note = run.waited_note({"a": 10.0}, [(0.0, 10.0)], 0.0)  # a zero wall must not divide by zero
        self.assertIn("waited 10 s", note)
        self.assertIn("for a slot", note)

    def test_the_share_never_reads_over_a_hundred_percent(self):
        note = run.waited_note({"a": 60.0}, [(0.0, 60.0)], 50.0)  # a clock skew between t0 and the spans
        self.assertIn("(100%)", note)

    def test_run_one_keeps_each_class_wait_as_a_span_and_two_at_once_overlap(self):
        @contextlib.contextmanager
        def slot(wait, small=False, low=False):
            time.sleep(0.2)  # both threads queue at the same moment, as on a pool with no free slot
            yield 0

        with mock.patch.object(run, "WAIT_SPANS", []), mock.patch.object(run, "WAITED", {}), \
                mock.patch.object(run, "cpu_slot", slot), \
                mock.patch.object(run, "_run_one", lambda cid, k, t, d, s: (cid, 1, "", 1, 0.0)):
            ts = [threading.Thread(target=run.run_one, args=(c, [])) for c in ("t.A", "t.B")]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            summed, wall = sum(run.WAITED.values()), run.wall_wait(run.WAIT_SPANS)
            self.assertEqual(len(run.WAIT_SPANS), 2)
        self.assertGreaterEqual(summed, 0.38)
        self.assertGreaterEqual(wall, 0.19)
        self.assertLess(wall, summed * 0.75)  # about half: the two waits overlapped

    def test_the_record_carries_the_wall_wait_beside_the_summed_one(self):
        with mock.patch.object(run, "WAITED", {"a": 10.0, "b": 10.0}), mock.patch.object(run, "HELD", {}), \
                mock.patch.object(run, "WAIT_SPANS", [(1.0, 11.0), (1.0, 11.0)]):
            got = run.slot_fields(100.0, 160.0)
        self.assertEqual(got["waited_s"], 20.0)
        self.assertEqual(got["waited_wall_s"], 10.0)
