"""T-4676: a train whose gate is red only in classes that pass alone confirms them with one narrowed run (the classes'
modules and the modules map-linked to them, uncached, on the merged tree), never a second whole gate; the cap is reached
only by the same class red twice (green alone each time, green in between); a class red in the narrowed confirm is a
lane's as before; the result names the minutes the confirming runs took. And before a lane is dropped for a set of
classes they run once, uncached, on the bare base in the same train: red there is the base's or the machine's, and the
train stops saying no lane was dropped (the 2026-11-14 train dropped three lanes for a class red on the bare base)."""
# map-links: plugin/lib/sampo/train.py, plugin/tests/test_train_flaky.py
import os
import unittest

import test_train_flaky as ttf
from test_lane_train import OK

FLAKY_L = "# map-links: shared.txt\n" + ttf.FLAKY
NEIGHBOUR = "# map-links: shared.txt\n" + OK.format("Nb")
# red in whole-gate-style runs 1 and 3 (a run that names no class with a dot) and green in run 2; Fb is red in run 2 only
SAME = ("# map-links: a.txt\nimport unittest\n\nSUITE = {0!r}\n\n\ndef red_in(*ks):\n"
        "    lines = open(SUITE).read().splitlines()\n    whole = [x for x in lines if x.startswith('sampo-train-') and '.' not in x.split(' ', 1)[1]]\n"
        "    return '.' not in lines[-1].split(' ', 1)[1] and len(whole) in ks\n\n\n"
        "class Fa(unittest.TestCase):\n    def test_it(self):\n        self.assertFalse(red_in(1, 3), 'load')\n\n\n"
        "class Fb(unittest.TestCase):\n    def test_it(self):\n        self.assertFalse(red_in(2), 'load')\n")
# red only while c.txt is in the tree, and says so: a strong blame (its failure names the file); green on the base
NAMED = ("# map-links: c.txt\nimport os, unittest\n\n\nclass Named(unittest.TestCase):\n    def test_it(self):\n"
         "        self.assertFalse(os.path.exists('c' + '.txt'), 'the tree holds c.txt')\n")


class Narrow(ttf.Gate):
    def test_a_flaky_class_is_confirmed_by_one_narrowed_run_of_its_module_never_a_second_whole_gate(self):
        a = self.lane(1, "test_flaky.py", ttf.FLAKY.format(self.marker))
        b = self.lane(2, "test_b.py", OK.format("B"))
        r = self.train(a, b)
        args = self.gate_args()
        self.assertEqual(len(args), 3, args)
        self.assertIn("test_b", args[0].split(), "the first gate runs the whole batch")
        self.assertEqual(args[1], "test_flaky.Flaky", "the red class alone")
        self.assertEqual(args[2].split(), ["test_flaky"], f"then its module, not the batch: {args}")
        self.assertIn("flaky under load: test_flaky.Flaky", r.stdout)
        self.assertTrue(self.landed("test_flaky.py") and self.landed("test_b.py"))
        self.assertNotIn("left out", r.stdout)

    def test_the_confirm_names_the_modules_map_linked_to_the_flaky_one_and_no_others(self):
        self.base("plugin/tests/test_nb.py", NEIGHBOUR)
        self.base("plugin/tests/test_far.py", OK.format("Far"))
        a = self.lane(1, "plugin/tests/test_flaky.py", FLAKY_L.format(self.marker))
        self.train(a)
        args = self.gate_args()
        self.assertEqual(len(args), 3, args)
        self.assertEqual(sorted(args[2].split()), ["test_flaky", "test_nb"], args)

    def test_the_result_names_the_minutes_the_confirming_runs_took(self):
        r = self.train(self.lane(1, "test_flaky.py", ttf.FLAKY.format(self.marker)))
        line = next(x for x in r.stdout.splitlines() if x.startswith("Minutes:"))
        self.assertRegex(line, r"confirm \d+(\.\d+)?,", line)
        r2 = self.train(self.lane(2, "b.txt"))
        self.assertRegex(next(x for x in r2.stdout.splitlines() if x.startswith("Minutes:")), r"confirm 0(\.0)?,")

    def test_a_class_red_again_in_the_narrowed_confirm_is_a_lanes_as_before(self):
        self.green_env()
        self.base("plugin/tests/test_victim.py", ttf.VICTIM.format(self.suite))
        a, b = self.lane(1, "a.txt"), self.lane(2, "poll.txt")
        r = self.train(a, b)
        self.assertIn(f"left out {b}: its merge turns the gates red", r.stdout)
        self.assertTrue(self.landed("a.txt") and not self.landed("poll.txt"))
        args = self.gate_args()
        self.assertEqual(args[:3], [args[0], "test_victim.Victim", "test_victim"],
                         f"alone, then the narrowed confirm of its module, not a second whole gate: {args}")
        self.assertNotIn("flaky under load: test_victim", r.stdout)

    def test_different_classes_red_in_successive_rounds_do_not_reach_the_cap(self):
        self.green_env()
        self.base("plugin/tests/test_cap.py", ttf.CAP.format(self.suite))
        r = self.train(self.lane(1, "a.txt"))
        self.assertTrue(self.landed("a.txt"), r.stdout)
        self.assertNotIn("cap reached", r.stdout + r.stderr)
        for k in (1, 2, 3, 4):
            self.assertIn(f"test_cap.F{k}", r.stdout)
        self.assertEqual(self.events("lane_train_flaky_cap"), [])

    def test_the_same_class_red_twice_reaches_the_cap_and_drops_no_lane(self):
        self.green_env()
        self.base("plugin/tests/test_same.py", SAME.format(self.suite))
        r = self.train(self.lane(1, "a.txt"), check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("flaky, cap reached", out)
        self.assertIn("test_same.Fa", out)
        self.assertNotIn("left out", out)
        self.assertFalse(self.landed("a.txt"))
        self.assertEqual([e["classes"] for e in self.events("lane_train_flaky_cap")], [["test_same.Fa"]])
        args = self.gate_args()
        self.assertTrue(all(a.split()[0].startswith("test_same") or a.startswith("test_same") for a in args[1:]),
                        f"after the first gate every run was narrowed: {args}")


class BaseFirst(ttf.Gate):
    """Part 5: the classes about to cost a lane run once, uncached, on the bare base first."""
    def setUp(self):
        super().setUp()
        self.red = {"GATE_ENV_RED": "1"}  # red on the base too, in the gate's own environment
        self.green_env()  # the base's green is cached from before the machine changed: _base_red passes

    def test_a_lane_that_changed_the_test_red_on_the_bare_base_is_left_out_for_it(self):
        """T-5982: the base's red excuses only a test no lane touched: a lane's own edit of it is the lane's."""
        self.base("plugin/tests/test_env.py", ttf.ENV.format(""))
        a = self.lane(1, "plugin/tests/test_env.py", ttf.ENV.format("") + "# a lane's edit\n")
        b = self.lane(2, "b.txt")
        r = self.train(a, b, check=False, env=self.red)
        out = r.stdout + r.stderr
        log = self.fm("task", "show", a).stdout
        self.assertIn("left out of a merge train", log, out)
        self.assertIn("test_env.Env", log)
        self.assertNotIn("put back", log)

    def test_a_class_red_on_the_bare_base_drops_no_lane_when_none_changed_its_test(self):
        self.base("plugin/tests/test_env.py", ttf.ENV.format(""))
        a = self.lane(1, "a.txt")
        b = self.lane(2, "b.txt")
        r = self.train(a, b, check=False, env=self.red)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("the base", out)
        self.assertIn("is red", out)
        self.assertIn("test_env.Env", out)
        self.assertIn("no lane dropped", out)
        for tid in (a, b):
            log = self.fm("task", "show", tid).stdout
            self.assertNotIn("left out of a merge train", log, tid)
            self.assertNotIn("put back", log, f"{tid}: it was never dropped, so nothing to put back")
        self.assertFalse(self.landed("b.txt"))
        self.assertEqual([(e["classes"], e["restored"]) for e in self.events("lane_train_base_red")], [(["test_env.Env"], [])])

    def test_a_class_red_only_with_one_lane_costs_that_lane_and_one_narrowed_base_run(self):
        self.base("plugin/tests/test_named.py", NAMED)
        a, b = self.lane(1, "c.txt"), self.lane(2, "d.txt")
        r = self.train(a, b)
        self.assertIn(f"left out {a}: its merge turns the gates red", r.stdout)
        self.assertTrue(self.landed("d.txt") and not self.landed("c.txt"))
        args = self.gate_args()
        self.assertEqual(args[1:3], ["test_named.Named"] * 2, f"alone on the merged tree, then on the bare base: {args}")
        self.assertEqual(len(args), 4, f"then the rebuilt tree's gate, no more: {args}")
        self.assertEqual(self.events("lane_train_base_red"), [])


if __name__ == "__main__":
    unittest.main()
