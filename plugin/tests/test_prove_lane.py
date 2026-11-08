"""T-3940: sampo task prove reads the changed tests from the task's lane and runs green there while the lane's branch
is unmerged; a merged or laneless task proves from the main tree as before."""
import json
import os
import subprocess

from helpers import ForemanTestCase

TEST = "import sys\nsys.exit(0 if open('app.txt').read().strip() == 'new' else 1)\n"


class ProveLane(ForemanTestCase):
    def git(self, *a, cwd=None):
        return subprocess.run(["git", "-C", cwd or self.repo, *a], check=True, capture_output=True, text=True).stdout

    def put(self, root, rel, text):
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.put(self.repo, "app.txt", "old\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.lane = os.path.join(self.tmp, "lane")
        self.git("worktree", "add", "-q", "-b", "foreman/lane", self.lane)
        self.tid = json.loads(self.fm("task", "new", "fix app", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                                      "--step", "s", "--json", "--focus", cwd=self.lane).stdout)["id"]
        self.put(self.lane, "test_app.py", TEST)
        self.put(self.lane, "app.txt", "new\n")
        self.git("add", "test_app.py", "app.txt", cwd=self.lane)
        self.git("commit", "-qm", "fix", cwd=self.lane)

    def prove(self):
        return self.fm("task", "prove", self.tid, "--run", "python3 test_app.py", check=False)

    def test_an_unmerged_lane_proves_red_then_green_from_the_lane(self):
        r = self.prove()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("proved: fails without the change", r.stdout)

    def test_a_dirty_lane_is_refused(self):
        self.put(self.lane, "app.txt", "wip\n")
        r = self.prove()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("uncommitted", r.stdout + r.stderr)

    def test_a_merged_lane_proves_from_the_main_tree(self):
        self.git("merge", "--ff-only", "foreman/lane")
        r = self.prove()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("proved: fails without the change", r.stdout)


if __name__ == "__main__":
    import unittest
    unittest.main()
