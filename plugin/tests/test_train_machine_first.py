"""T-4652: a merge train checks the machine before it merges or tests anything. At doctor's disk fail level (under 5 GB
free) it refuses at once, naming the free space and the verb that frees it. A gate red only on doctor's disk line is a
machine condition, never a red base: 2026-11-14 a train ran its whole base gate for about 95 minutes, then called the
base red for 2 GB free.
"""
# map-links: plugin/lib/sampo/train.py
import collections
import inspect
import os
import re
import time
import unittest
import unittest.mock

from test_lane_land import OLD, LandCase
from test_lane_train import TrainCase

from sampo import core as c
from sampo import doctor as d
from sampo import lanes
from sampo import train

Usage = collections.namedtuple("Usage", "total used free")
DISK_LINE = "FAIL  disk                 2 GB free (0%) on the disk holding Sampo's state: hooks and builders fail when it fills"


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
        self.fm("check", "add", f'echo "{DISK_LINE}"; exit 1')
        a = self.lane(1, "a.txt")
        r = self.train(a, check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("machine", out)
        self.assertNotIn("is red", out)
        self.assertNotIn("left out", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertFalse(self.meta(a).get("lane_stop", "").startswith("merged"))


class SweepFirst(LandCase):
    """T-4658: `sampo lane land --merged` checks the machine before its gate, as `sampo lane land` and the train do: on a
    full disk it refuses before any gate runs, and the task of a lane already on the trunk stays active."""

    def test_land_merged_refuses_a_full_disk_before_its_gate_and_closes_nothing(self):
        tid = self.lane(1, "a.txt", when=OLD)
        self.audited(tid)
        self.returned(tid, "Lane 1")
        self.git("merge", "--no-ff", "-q", "-m", "merge lane 1", "foreman/lane1")
        r = self.fm("lane", "land", "--merged", check=False, env={"FOREMAN_DISK_FREE": "3e9"}, timeout=120)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("machine", out)
        self.assertIn("3 GB free", out)
        self.assertIn("sampo tidy", out)
        self.assertEqual(self.gate_runs(), 0, out)
        self.assertNotEqual(self.brief(tid).status, "done", out)


class MachineStopVerdict(TrainCase):
    """T-4658: the train's refusal needs the free space only. It never sizes what Sampo could free (up to 10 s on every
    refusal); `sampo doctor` still names it."""

    def test_the_refusal_names_the_free_space_and_sizes_nothing(self):
        p = c.find_project(self.repo)
        slow = unittest.mock.Mock(side_effect=lambda *a, **kw: (time.sleep(1.5), [])[1])
        with unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": "3e9"}), \
                unittest.mock.patch.object(c, "all_projects", return_value=[(p, {})]), \
                unittest.mock.patch.object(lanes, "reclaimables", slow):
            t0 = time.monotonic()
            why = train.machine_stop(p)
            took = time.monotonic() - t0
        self.assertIn("3 GB free", why)
        self.assertIn("sampo tidy", why)
        self.assertEqual(slow.call_count, 0, "a refusal sizes nothing")
        self.assertLess(took, 1.0, why)


class MachineLine(unittest.TestCase):
    def test_only_a_disk_fail_line_reads_as_a_machine_condition(self):
        self.assertTrue(train._machine_only(DISK_LINE + "\n"))
        self.assertTrue(train._machine_only("ok    hooks                fine\n" + DISK_LINE + "\n"))
        self.assertFalse(train._machine_only(DISK_LINE + "\nFAIL: test_it (mod.Cls.test_it)\n"))
        self.assertFalse(train._machine_only("FAIL  hooks                broken\n" + DISK_LINE + "\n"))
        self.assertFalse(train._machine_only("WARN  disk                 20 GB free\n"))
        self.assertFalse(train._machine_only(""))

    def test_the_matcher_reads_the_line_doctor_renders_for_a_real_fail(self):
        r = d.check_disk(None, lambda _: Usage(900e9, 898e9, 2e9), [])
        self.assertEqual(r.status, "FAIL")
        line = d.result_line(r)
        self.assertEqual(line, DISK_LINE, "the pinned example is doctor's own rendering")
        self.assertTrue(train._machine_only(line + "\n"))
        self.assertFalse(train._machine_only(d.result_line(d.Result("disk", "WARN", "20 GB free")) + "\n"))
        self.assertFalse(train._machine_only(d.result_line(d.Result("hooks", "FAIL", "broken")) + "\n"))

    def test_the_matcher_and_doctor_share_one_rendering(self):
        """A drift between the printed line and the matcher fails here: the matcher's head is doctor's line, and the
        report prints every line through that function."""
        head = d.result_line(d.Result(d.DISK, "FAIL", "")).rstrip()
        fail, _ = train._machine_patterns()
        self.assertIsNotNone(fail.search(head + " 2 GB free (0%) on the disk"))
        self.assertEqual(fail.pattern, "^" + re.escape(head) + r"\b.*$", "built from doctor's own head")
        self.assertIn("result_line(r)", inspect.getsource(d.cmd_doctor))


if __name__ == "__main__":
    unittest.main()
