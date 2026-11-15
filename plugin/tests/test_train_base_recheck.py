"""T-4700: a base test that turns red with time (outputs ageing, a date rolling over) after a train's start check never
leaves a lane out. Before a lane is left out for a class, the class runs on the bare base as it is now (T-4676); one red
there is named the base's, excluded from the blame (the lane stays in the batch for the other classes' verdict), and
filed once as a FIX capture; a lane that breaks a class green on the base is still left out."""
# map-links: plugin/lib/sampo/train.py, plugin/tests/test_train_flaky_narrow.py
import os

import test_train_flaky as ttf
from test_lane_train import OK

# green on the first run of the suite in the train (its start check) and red from the second on: the base's own test,
# turning red while the train runs; it names no lane's file
AGED = ("# map-links: a.txt\nimport unittest\n\nSUITE = {0!r}\n\n\nclass Aged(unittest.TestCase):\n    def test_it(self):\n"
        "        runs = len(open(SUITE).read().splitlines())\n        self.assertFalse(runs >= 2, 'the outputs aged')\n")
# green on the base; red once lane b's file is in the tree, and names it: a lane's own break
BREAK = ("# map-links: b.txt\nimport os, unittest\n\n\nclass Brk(unittest.TestCase):\n    def test_it(self):\n"
         "        self.assertFalse(os.path.exists('b' + '.txt'), 'the tree holds b.txt')\n")


class Recheck(ttf.Gate):
    def setUp(self):
        super().setUp()
        self.base("plugin/tests/test_aged.py", AGED.format(self.suite))
        self.base("plugin/tests/test_brk.py", BREAK)

    def captures(self):
        from sampo import core as c
        return [b for b in c.load_briefs(self.p) if "red on the bare base" in b.title]

    def test_a_base_class_that_turned_red_after_the_start_check_leaves_no_lane_out(self):
        a, b = self.lane(1, "a.txt"), self.lane(2, "c.txt")
        r = self.train(a, b, check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("test_aged.Aged", out)
        self.assertIn("is red", out)
        self.assertNotIn("left out", out)
        for tid in (a, b):
            self.assertNotIn("left out of a merge train", self.fm("task", "show", tid).stdout, tid)
        self.assertFalse(self.landed("a.txt") or self.landed("c.txt"))
        got = self.captures()
        self.assertEqual(len(got), 1, [x.title for x in got])
        self.assertEqual((got[0].type, got[0].meta.get("source")), ("FIX", "discovered"))
        self.assertIn("test_aged.Aged", got[0].title)

    def test_the_capture_is_filed_once_across_trains(self):
        a = self.lane(1, "a.txt")
        self.train(a, check=False)
        self.green_env()  # no start check: the second train reaches the same verdict by its own recheck
        self.train(a, check=False)
        self.assertEqual(len(self.captures()), 1)

    def test_a_lane_that_breaks_a_class_green_on_the_base_is_still_left_out_beside_a_base_red_one(self):
        a, b = self.lane(1, "a.txt"), self.lane(2, "b.txt")
        r = self.train(a, b, check=False)
        out = r.stdout + r.stderr
        self.assertIn(f"left out {b}: its merge turns the gates red", out)
        self.assertIn("test_brk.Brk", out)
        self.assertNotIn(f"left out {a}", out)
        self.assertNotIn("failing test_aged.Aged", out, "the lane's reason names only what it broke")
        self.assertIn("test_aged.Aged", out, "the base's class is named")
        self.assertNotIn("left out of a merge train", self.fm("task", "show", a).stdout)
        self.assertIn("left out of a merge train", self.fm("task", "show", b).stdout)
        self.assertTrue(self.landed("a.txt") and not self.landed("b.txt"), out)
        self.assertEqual(len(self.captures()), 1)
        self.assertIn("test_aged.Aged", self.captures()[0].title)

    def test_a_class_red_only_with_a_lane_is_the_lanes_when_the_base_is_green(self):
        r = self.train(self.lane(1, "b.txt"), self.lane(2, "c.txt"))
        self.assertIn("left out", r.stdout)
        self.assertIn("test_brk.Brk", r.stdout)
        self.assertTrue(self.landed("c.txt") and not self.landed("b.txt"))
        self.assertEqual(self.captures(), [], "the base was green: nothing to file")
