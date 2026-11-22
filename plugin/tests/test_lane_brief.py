"""T-5774: lanes.lane_fn_overlap (T-5725) unpacked _defs as three values after T-5682 made it return four, so `sampo lane
brief` raised ValueError for any task with a code scope. A scoped Python file plus another lane changing one of its defs
gives the warning line."""
# map-links: plugin/lib/sampo/lanes.py
import os
import subprocess
import types
import unittest

from helpers import ForemanTestCase

from sampo import lanes

G = ["git", "-c", "user.name=t", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false"]


class FnOverlap(ForemanTestCase):
    def git(self, cwd, *a):
        return subprocess.run(G + list(a), cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def test_fn_overlap_warns_for_a_def_another_lane_changes_instead_of_raising(self):
        main = os.path.join(self.tmp, "main")
        os.makedirs(main)
        self.git(main, "init", "-q")
        with open(os.path.join(main, "a.py"), "w") as f:
            f.write("def keep():\n    return 1\n\n\ndef frobnicate():\n    return 2\n")
        self.git(main, "add", "-A")
        self.git(main, "commit", "-qm", "base")
        base = self.git(main, "rev-parse", "HEAD")
        lane = os.path.join(self.tmp, "lane")
        self.git(main, "worktree", "add", "-q", "-b", "other", lane)
        with open(os.path.join(lane, "a.py"), "w") as f:
            f.write("def keep():\n    return 1\n\n\ndef frobnicate():\n    return 3\n")
        self.git(lane, "commit", "-qam", "change frobnicate")
        step = types.SimpleNamespace(text="make `frobnicate` faster")
        mine = types.SimpleNamespace(id="T-1", title="Speed up frobnicate", meta={"scope": ["a.py"]}, steps=lambda: [step])
        other = types.SimpleNamespace(id="T-2", title="Other work", status="active", meta={"lane": lane})
        out = lanes.lane_fn_overlap(None, mine, main, base, [mine, other])
        self.assertEqual(len(out), 1, out)
        self.assertIn("frobnicate in a.py", out[0])
        self.assertIn("T-2", out[0])


if __name__ == "__main__":
    unittest.main()
