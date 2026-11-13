"""T-4562: MASTER.md keeps 10 KB of room under its 90000-byte cap, its moved sections stay one link away, and a lane
that grows MASTER.md while under 4 KB of room gets the room named (the T-4202 train went red on 2.1 KB left)."""
import os
import subprocess
import time
import types

from helpers import ForemanTestCase

from sampo import core as c
from sampo import selfcheck

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class MapRoom(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def git(self, *a):
        return subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True, text=True).stdout.strip()

    def trunk_master(self, left):
        with open(os.path.join(self.repo, "MASTER.md"), "w") as f:
            f.write("x" * (c.MAP_FILE_BYTES - left))
        self.git("add", "-A")
        self.git("commit", "-qm", "the map")

    def lane_appends(self, rel, adds):
        self.git("checkout", "-q", "-b", "lane1")
        with open(os.path.join(self.repo, rel), "a") as f:
            f.write("y" * adds)
        self.git("add", "-A")
        self.git("commit", "-qm", "a lane's index line")
        self.git("checkout", "-q", "-")

    def accepted(self, **meta):
        return types.SimpleNamespace(id="T-0007", status="active", meta={"lane_branch": "lane1", **meta})

    def test_the_trunk_master_keeps_10_kb_of_room_under_its_cap(self):
        left = c.MAP_FILE_BYTES - os.path.getsize(os.path.join(ROOT, "MASTER.md"))
        self.assertGreaterEqual(left, 10000, "MASTER.md's room under its 90000-byte cap")
        self.assertIsNone(selfcheck.map_room(types.SimpleNamespace(root=ROOT), [], time.time()))

    def test_the_sections_moved_out_of_master_are_linked_and_present_in_their_shard(self):
        master = read(os.path.join(ROOT, "MASTER.md"))
        shard = read(os.path.join(ROOT, "docs", "map", "guide-1.md"))
        self.assertIn("](docs/map/guide-1.md)", master, "MASTER.md links the shard")
        for heading in ("## 2. How to prompt it", "## 5. Lifecycle walkthrough"):
            self.assertIn(heading, master, "the heading stays in MASTER.md, so its anchor still resolves")
        self.assertIn("### 2. How to prompt it", shard)
        self.assertIn("### 5. Lifecycle walkthrough", shard)
        self.assertIn("You type `FIX: login times out on slow wifi`", shard)
        self.assertNotIn("You type `FIX: login times out on slow wifi`", master, "the bodies moved out of the index")

    def test_a_lane_that_grows_master_under_4_kb_of_room_names_the_room(self):
        self.trunk_master(left=3000)
        self.lane_appends("MASTER.md", 500)
        hit = selfcheck.map_room(self.p, [self.accepted(accepted_at=time.time())], time.time())
        self.assertIn("MASTER.md has 3000 bytes left of its 90000", hit)
        self.assertIn("T-0007 500", hit)
        self.assertIn("2500 bytes left after them", hit)
        self.assertIn("split it", hit)

    def test_a_lane_that_grows_master_at_4_kb_or_more_of_room_is_not_named(self):
        self.trunk_master(left=4500)
        self.lane_appends("MASTER.md", 500)
        self.assertIsNone(selfcheck.map_room(self.p, [self.accepted(accepted_at=time.time())], time.time()))

    def test_a_lane_that_does_not_grow_master_is_not_named_under_4_kb(self):
        self.trunk_master(left=3000)
        self.lane_appends("notes.txt", 500)
        self.assertIsNone(selfcheck.map_room(self.p, [self.accepted(accepted_at=time.time())], time.time()))

    def test_a_lane_not_yet_accepted_is_not_named(self):
        self.trunk_master(left=3000)
        self.lane_appends("MASTER.md", 500)
        self.assertIsNone(selfcheck.map_room(self.p, [self.accepted()], time.time()), "still being built")


if __name__ == "__main__":
    import unittest
    unittest.main()
