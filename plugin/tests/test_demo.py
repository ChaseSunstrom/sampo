"""T-0870: fm demo shows Foreman work on a seeded bug in a throwaway repo: no model, no trace left behind."""
import os
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase, read_text

from sampo import demo


class Demo(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = os.path.join(self.tmp, "tmpdir")  # where the demo's temp folder must come and go
        os.makedirs(self.tmpdir)

    def test_runs_end_to_end_in_plain_words_and_leaves_nothing(self):
        before = sorted(os.listdir(self.home))
        p = self.fm("demo", env={"TMPDIR": self.tmpdir}, timeout=20)  # a hang guard, scaled by fm with the load
        out = p.stdout
        for stage in ("no active task", "rm -rf ~", "blocked rm-outside", "T-0001", "red", "green", "not finished",
                      "closed", "nothing was left behind"):
            self.assertIn(stage, out)
        self.assertLess(out.index("red"), out.index("green"))  # the test fails before the fix, passes after
        self.assertEqual(os.listdir(self.tmpdir), [])
        self.assertEqual(sorted(os.listdir(self.home)), before)  # the user's Foreman state is never touched
        self.assertNotIn("claude was called", out + p.stderr)

    def test_pace_and_colour_for_a_recording(self):
        # T-0902: a recording needs time to read each stage and colour to scan it; a plain run stays quick and plain
        self.assertNotIn("\x1b[", self.fm("demo", env={"TMPDIR": self.tmpdir}).stdout)
        t0 = time.time()
        out = self.fm("demo", "--pace", "0.05", env={"TMPDIR": self.tmpdir, "FORCE_COLOR": "1"}).stdout
        self.assertGreater(time.time() - t0, 7 * 0.05)  # at least one pause per stage
        self.assertIn("\x1b[1;31mRefused", out)
        self.assertIn("\x1b[32mgreen", out)
        self.assertIn("nothing was left behind", out)
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_ctrl_c_still_cleans_up(self):
        with mock.patch.object(demo.tempfile, "tempdir", self.tmpdir), \
                mock.patch.object(demo, "_hook", side_effect=KeyboardInterrupt):
            self.assertEqual(demo.cmd_demo(None), 130)
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_listed_where_a_newcomer_starts(self):
        self.assertIn("sampo demo", self.fm("--help").stdout)
        self.assertIn("demo", self.fm("help", "--all").stdout)
        self.assertIn("sampo demo", read_text(os.path.join(os.path.dirname(demo.__file__), "..", "..", "..", "README.md")))


if __name__ == "__main__":
    unittest.main()


class DemoLoadGate(ForemanTestCase):
    # T-2822: the demo, its fences and the release check run their fm with the load gate off, even on a loaded host
    def test_the_demo_fences_and_release_env_turn_the_load_gate_off_and_drop_the_host_stand_in(self):
        with mock.patch.dict(os.environ, {"FOREMAN_HOST": "load=99,cpus=1"}):
            demo_env = demo._env(os.path.join(self.tmp, "demo"))
            fence_env = demo._fence_env(os.path.join(self.tmp, "fence"))
            release = demo.release_env(os.path.join(self.tmp, "release"))
        for env in (demo_env, fence_env, release):
            self.assertEqual(env["FOREMAN_LOAD_GATE"], "0")
            self.assertNotIn("FOREMAN_HOST", env)


class DemoNext(ForemanTestCase):
    # T-0919: run inside a project, the demo ends on that project's own next step, not a generic "fm init"
    def test_inside_a_project_the_demo_ends_with_its_next_step(self):
        tmpdir = os.path.join(self.tmp, "tmpdir")
        os.makedirs(tmpdir)
        out = self.fm("demo", env={"TMPDIR": tmpdir}).stdout
        self.assertIn("sampo init in your repo", out)  # not a project yet
        self.fm("init")
        self.fm("capture", "Add a dark theme")
        out = self.fm("demo", env={"TMPDIR": tmpdir}).stdout
        self.assertIn("Back in this project", out)
        self.assertIn("T-0001", out.split("Back in this project", 1)[1])
