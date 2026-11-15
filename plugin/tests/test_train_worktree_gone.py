"""T-4728: a merge train whose scratch worktree is removed by something else says so. On 2026-11-15 a train's
/tmp/sampo-train-* folder vanished before its base probe and the train stopped with "base probe inconclusive ... exit 127 ...
No such file or directory", naming no cause. Now the train sees the tree is gone, rebuilds it once at the commit it had
reached and goes on; a second loss stops it, naming the cause, with nothing landed."""
# map-links: plugin/lib/sampo/train.py
import os

import test_lane_train as tlt
from test_lane_train import TrainCase

SAID = "the train's scratch worktree was removed by something else"

# a gate that removes the folder it runs in (the train's own scratch worktree, named sampo-train-*, made by the train in
# this test's temp folder) on its first LOSSES runs: what the incident did to the train from outside
EATER = ("n=$(cat {count} 2>/dev/null || echo 0)\nn=$((n+1))\necho $n > {count}\n"
         "if [ \"$n\" -le {losses} ]; then\n  case \"$(basename \"$PWD\")\" in sampo-train-*) rm -rf \"$PWD\" ;; esac\nfi\n")


class WorktreeGone(TrainCase):
    def eater(self, losses):
        count = os.path.join(self.tmp, "eater-runs")
        script = os.path.join(self.tmp, "eater.sh")
        with open(script, "w") as f:
            f.write(EATER.format(count=count, losses=losses))
        self.fm("check", "add", f"sh {script}")
        return count

    def eaten(self, count):
        return int(open(count).read())

    def test_a_tree_lost_once_is_rebuilt_and_the_train_goes_on(self):
        count = self.eater(1)
        a, b = self.lane(1, "a.txt"), self.lane(2, "b.txt")
        r = self.train(a, b)
        out = r.stdout + r.stderr
        self.assertIn(SAID, out)
        self.assertNotIn("inconclusive", out)
        self.assertNotIn("No such file", out)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")) and os.path.exists(os.path.join(self.repo, "b.txt")), out)
        self.assertEqual(self.eaten(count), 2, "the gate the lost tree ended is run again on the rebuilt one")
        for tid in (a, b):
            self.assertTrue(self.meta(tid)["lane_stop"].startswith("merged"), self.meta(tid))
        self.assertNotIn("sampo-train-", self.git("worktree", "list"), "no scratch worktree is left behind")

    def test_a_second_loss_stops_the_train_with_nothing_landed(self):
        count = self.eater(2)
        a = self.lane(1, "a.txt")
        before = self.git("rev-parse", "HEAD")
        r = self.train(a, check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn(SAID, out)
        self.assertIn("again", out, "the second loss is said as a second")
        self.assertEqual(self.eaten(count), 2, "one rebuild, never a third try")
        self.assertEqual(self.git("rev-parse", "HEAD"), before, "nothing landed")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertNotIn("sampo-train-", self.git("worktree", "list"), "no scratch worktree is left behind")
        self.assertFalse(self.meta(a).get("lane_stop", "").startswith("merged"))

    def test_a_tree_that_was_never_lost_says_nothing_of_it(self):
        self.eater(0)
        r = self.train(self.lane(1, "a.txt"))
        self.assertNotIn(SAID, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))


# a suite that removes the scratch tree it runs in when that tree is of the kind named (sampo-probe-* or sampo-base-*), on
# its first LOSSES runs there, and then ends as a run in a vanished folder does (red); anywhere else it runs the tests
SUITE = ('case "${{PWD##*/}}" in {kind}-*)\n  n=$(cat {count} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {count}\n'
         '  if [ "$n" -le {losses} ]; then rm -rf "$PWD"; echo "boom"; exit 1; fi;;\nesac\n'
         'PYTHONPATH=plugin/tests exec python3 -m unittest "$@"\n')


class SideTrees(tlt.AffectedGate):
    """The incident's own path: the BASE probe (sampo-base-*) and the per-lane PROBE (sampo-probe-*) ran in a folder that was
    gone, ended 127 and were read as a red base or a guilty lane. A removed tree is a loss, not a red: it is rebuilt once and
    run again, and a second loss stops the train with the cause, no lane left out and the base not called red."""
    def eating(self, kind, losses):
        count = os.path.join(self.tmp, f"{kind}-runs")
        with open(os.path.join(self.repo, "suite.sh"), "w") as f:
            f.write(SUITE.format(kind=kind, count=count, losses=losses))
        self.git("add", "suite.sh")
        self.git("commit", "-qm", f"a {kind} eater")
        return count

    def count_of(self, count):
        return int(open(count).read()) if os.path.exists(count) else 0

    def test_a_base_probe_tree_lost_once_is_rebuilt_and_the_base_is_not_called_red(self):
        count = self.eating("sampo-base", 1)
        a = self.lane(1, "test_a.py", tlt.OK.format("A"))
        r = self.train(a)
        out = r.stdout + r.stderr
        self.assertIn(SAID, out)
        self.assertIn("base probe", out)
        self.assertNotIn("inconclusive", out)
        self.assertNotIn("is red", out)
        self.assertEqual(self.count_of(count), 2, "the base probe runs again on the rebuilt tree")
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), self.meta(a))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "test_a.py")))

    def test_a_base_probe_tree_lost_twice_stops_the_train_calling_nothing_red(self):
        count = self.eating("sampo-base", 2)
        a = self.lane(1, "test_a.py", tlt.OK.format("A"))
        r = self.train(a, check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn(SAID, out)
        self.assertIn("again", out)
        self.assertNotIn("is red", out)
        self.assertNotIn("left out", out)
        self.assertEqual(self.count_of(count), 2, "one rebuild, never a third try")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "test_a.py")), "nothing landed")
        self.assertNotIn("sampo-base-", self.git("worktree", "list"), "no scratch tree is left registered")

    def test_a_probe_tree_lost_once_is_rebuilt_and_the_lane_is_not_blamed(self):
        count = self.eating("sampo-probe", 1)
        a = self.lane(1, "test_a.py", tlt.OK.format("A"))
        r = self.train(a, env={"FOREMAN_TRAIN_PROBES": "1"})
        out = r.stdout + r.stderr
        self.assertIn(SAID, out)
        self.assertIn(f"the probe of {a}", out)
        self.assertNotIn("left out", out)
        self.assertEqual(self.count_of(count), 2, "the probe runs again on the rebuilt tree")
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), self.meta(a))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "test_a.py")))

    def test_a_probe_tree_lost_twice_stops_the_train_and_leaves_the_lane_in(self):
        count = self.eating("sampo-probe", 2)
        a = self.lane(1, "test_a.py", tlt.OK.format("A"))
        r = self.train(a, check=False, env={"FOREMAN_TRAIN_PROBES": "1"})
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn(SAID, out)
        self.assertIn("again", out)
        self.assertNotIn("left out", out, "the lane is not blamed for a loss")
        self.assertNotIn("its affected tests fail", out)
        self.assertEqual(self.count_of(count), 2, "one rebuild, never a third try")
        self.assertNotIn("left out of a merge train", self.fm("task", "show", a).stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "test_a.py")), "nothing landed")
        self.assertNotIn("sampo-probe-", self.git("worktree", "list"), "no scratch tree is left registered")
