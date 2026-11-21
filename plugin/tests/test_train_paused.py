"""T-5044: a paused machine (sampo pause, or the project's own pause) refuses the gate runs, which the train read as a red
base. The train and the land check the pause at their entry, as they check the disk (T-4652): one line says the pause
stops the gates and to resume first, nothing is merged, parked, dropped or tested."""
# map-links: plugin/lib/sampo/train.py
import os
import unittest

from test_lane_land import OLD, LandCase
from test_lane_train import TrainCase

from sampo import core as c
from sampo import train


class PausedTrain(TrainCase):
    def test_a_paused_machine_refuses_the_train_before_any_merge_or_gate_and_never_calls_the_base_red(self):
        a = self.lane(1, "a.txt")
        head = self.git("rev-parse", "HEAD")
        self.fm("pause", "on")  # the test's own FOREMAN_HOME: no other session is paused by it
        r = self.train(a, check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("paused", out)
        self.assertIn("sampo pause off", out)
        self.assertIn("gate", out)
        self.assertNotIn("is red", out)
        self.assertNotIn("left out", out)
        self.assertEqual(self.gate_runs(), 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertFalse(self.meta(a).get("lane_stop", "").startswith("merged"))

    def test_a_projects_own_pause_refuses_the_train_too(self):
        a = self.lane(1, "a.txt")
        p = c.find_project(self.repo)
        c.write_meta(p, {**c.read_meta(p), "paused": True})
        r = self.train(a, check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("paused", out)
        self.assertNotIn("is red", out)
        self.assertEqual(self.gate_runs(), 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_resumed_the_train_runs_as_before(self):
        a = self.lane(1, "a.txt")
        self.fm("pause", "on")
        self.fm("pause", "off")
        r = self.train(a)
        self.assertIn("landed", r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertGreaterEqual(self.gate_runs(), 1)

    def test_paused_stop_is_empty_when_not_paused_and_names_the_resume_when_paused(self):
        p = c.find_project(self.repo)
        self.assertEqual(train.paused_stop(p), "")
        self.fm("pause", "on")
        why = train.paused_stop(p)
        self.assertIn("paused", why)
        self.assertIn("sampo pause off", why)
        self.assertNotIn("red", why)


class PausedLand(LandCase):
    def test_a_paused_machine_refuses_the_land_and_parks_and_merges_nothing(self):
        tid = self.lane(1, "a.txt", when=OLD)
        self.audited(tid)
        self.returned(tid, "Lane 1")
        head = self.git("rev-parse", "HEAD")
        self.fm("pause", "on")
        r = self.land(check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("paused", out)
        self.assertIn("sampo pause off", out)
        self.assertNotIn("is red", out)
        self.assertEqual(self.gate_runs(), 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertNotEqual(self.brief(tid).status, "done", out)
        self.assertFalse(self.meta(tid).get("lane_stop"), "nothing parked for a pause")

    def test_land_merged_refuses_a_paused_machine_before_its_gate_and_closes_nothing(self):
        tid = self.lane(1, "a.txt", when=OLD)
        self.audited(tid)
        self.returned(tid, "Lane 1")
        self.git("merge", "--no-ff", "-q", "-m", "merge lane 1", "foreman/lane1")
        self.fm("pause", "on")
        r = self.fm("lane", "land", "--merged", check=False, timeout=120)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("sampo pause off", out)
        self.assertNotIn("is red", out)
        self.assertEqual(self.gate_runs(), 0, out)
        self.assertNotEqual(self.brief(tid).status, "done", out)

    def test_lane_merge_under_the_projects_own_pause_names_that_hold_not_the_gates(self):
        tid = self.lane(1, "a.txt", when=OLD)
        self.audited(tid)
        self.returned(tid, "Lane 1")
        p = c.find_project(self.repo)
        c.write_meta(p, {**c.read_meta(p), "paused": True})
        r = self.fm("lane", "merge", tid, check=False, timeout=120)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("this project", out)
        self.assertNotIn("stops the gates", out)
        self.assertEqual(self.gate_runs(), 0, out)

    def test_the_machine_wide_pause_still_says_it_stops_the_gates(self):
        self.fm("pause", "on")
        self.assertIn("stops the gates", train.paused_stop(c.find_project(self.repo)))


if __name__ == "__main__":
    unittest.main()
