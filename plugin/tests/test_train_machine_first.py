"""T-4652: a merge train checks the machine before it merges or tests anything. At doctor's disk fail level (under 5 GB
free) it refuses at once, naming the free space and the verb that frees it. A gate red only on doctor's disk line is a
machine condition, never a red base: 2026-11-14 a train ran its whole base gate for about 95 minutes, then called the
base red for 2 GB free.
"""
# map-links: plugin/lib/sampo/train.py
import os
import unittest

from test_lane_train import TrainCase

from sampo import train

DISK_LINE = "FAIL  disk                 2 GB free (0%) on the disk holding Sampo state: hooks and builders fail when it fills"


class MachineFirst(TrainCase):
    def test_at_the_fail_level_the_train_refuses_before_any_merge_or_gate(self):
        a = self.lane(1, "a.txt")
        head = self.git("rev-parse", "HEAD")
        r = self.train(a, check=False, env={"FOREMAN_DISK_FREE": "3e9"})
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("3 GB free", out)
        self.assertIn("sampo tidy", out)
        self.assertNotIn("is red", out)
        self.assertEqual(self.gate_runs(), 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertFalse(self.meta(a).get("lane_stop", "").startswith("merged"))

    def test_at_the_warn_level_the_train_runs_as_before(self):
        a = self.lane(1, "a.txt")
        r = self.train(a, env={"FOREMAN_DISK_FREE": "20e9/100e9"})
        self.assertIn("landed", r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertGreaterEqual(self.gate_runs(), 1)

    def test_at_the_ok_level_the_train_runs_as_before(self):
        a = self.lane(1, "a.txt")
        r = self.train(a, env={"FOREMAN_DISK_FREE": "500e9/1000e9"})
        self.assertIn("landed", r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertGreaterEqual(self.gate_runs(), 1)

    def test_a_gate_red_only_on_the_disk_line_is_a_machine_condition_not_a_red_base(self):
        self.fm("check", "add", f"echo '{DISK_LINE}'; exit 1")
        a = self.lane(1, "a.txt")
        r = self.train(a, check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("machine", out)
        self.assertNotIn("is red", out)
        self.assertNotIn("left out", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertFalse(self.meta(a).get("lane_stop", "").startswith("merged"))


class MachineLine(unittest.TestCase):
    def test_only_a_disk_fail_line_reads_as_a_machine_condition(self):
        self.assertTrue(train._machine_only(DISK_LINE + "\n"))
        self.assertTrue(train._machine_only("ok    hooks                fine\n" + DISK_LINE + "\n"))
        self.assertFalse(train._machine_only(DISK_LINE + "\nFAIL: test_it (mod.Cls.test_it)\n"))
        self.assertFalse(train._machine_only("FAIL  hooks                broken\n" + DISK_LINE + "\n"))
        self.assertFalse(train._machine_only("WARN  disk                 20 GB free\n"))
        self.assertFalse(train._machine_only(""))


if __name__ == "__main__":
    unittest.main()
