"""T-5381: a train merge that takes a docs/map shard or MASTER.md over its cap (MAP_FILE_BYTES) no longer leaves the lane
out when the lane only added: its new whole bullets move to the shard with the most room (or a new one), their
MASTER.md index links follow, and an over-long MASTER.md gets the lane's own index lines shortened. An edit of an
existing bullet is never split from its original: that lane is left out as before (T-4980)."""
# map-links: plugin/lib/sampo/train.py
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts plugin/lib on the path)
from sampo import core as c
from sampo import train

CAP = 3000  # bytes: the cap these tests stand on, so a few bullets cross it

HEAD = ("# Sampo map: File map, plugin/lib modules\n\n"
        "Full entries for [MASTER.md](../../MASTER.md) section 3 (File map).\n\n"
        "### plugin/lib modules, fmaaa to fmzzz\n\n<details>\n<summary>every module, one line each</summary>\n\n")
FOOT = "\n</details>\n"


def bullet(name, n=100, extra=""):
    return f"- `{name}` ({'x' * n})\n{extra}"


def shard(*bullets):
    return HEAD + "".join(bullets) + FOOT


def index(name, shard_name, words=10):
    return f"- `{name}` (T-1: {'word ' * words}…) [→ {shard_name}](docs/map/{shard_name}.md)\n"


class MapCap(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.wt = os.path.join(self._tmp.name, "wt")
        os.makedirs(os.path.join(self.wt, "docs", "map"))
        self.run_git("init", "-q", "-b", "main")
        cap = mock.patch.object(c, "MAP_FILE_BYTES", CAP)
        cap.start()
        self.addCleanup(cap.stop)

    def run_git(self, *args):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", self.wt, *args],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def write(self, rel, text):
        path = os.path.join(self.wt, rel)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.wt, rel), encoding="utf-8") as f:
            return f.read()

    def commit(self, files, msg, links=None):
        for rel, text in files.items():
            self.write(rel, text)
        for rel, target in (links or {}).items():  # a path the lane commits as a symlink (T-5381 fix round)
            path = os.path.join(self.wt, rel)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            elif os.path.lexists(path):
                os.remove(path)
            os.symlink(target, path)
        self.run_git("add", "-A")
        self.run_git("commit", "-q", "-m", msg)

    def merge(self, branch_files, trunk_files=None, links=None):
        """The base on main, a lane branch that changes `branch_files`, optionally another lane merged first (trunk_files);
        then the lane's merge as the train makes it. (before sha, the lane record)"""
        self.run_git("checkout", "-q", "-b", "lane")
        self.commit(branch_files, "lane", links)
        self.run_git("checkout", "-q", "main")
        if trunk_files:
            self.commit(trunk_files, "earlier lane")
        before = self.run_git("rev-parse", "HEAD")
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", self.wt, "merge", "--no-ff",
                            "-m", "Merge T-9: lane", "lane"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return before, types.SimpleNamespace(id="T-9", title="lane")

    def base(self, **extra):
        files = {
            "MASTER.md": "# M\n\n" + index("fmold", "files-lib-1") + index("fmother", "files-lib-2"),
            "docs/map/files-lib-1.md": shard(bullet("fmold", 1500)),
            "docs/map/files-lib-2.md": shard(bullet("fmother", 300)),
        }
        files.update(extra)
        self.commit(files, "base")

    def over(self, before, b):
        log, moved = [], {}
        sha = train._over_cap(self.wt, b, before, log, moved)
        return sha, log, moved


class ShardOverflow(MapCap):
    def test_new_bullets_move_to_the_shard_with_the_most_room_and_their_index_links_follow(self):
        self.base()
        new = bullet("fmnew", 800, "  continued line of fmnew\n") + bullet("fmnew2", 500)
        lane = {"docs/map/files-lib-1.md": shard(bullet("fmold", 1500), new),
                "MASTER.md": "# M\n\n" + index("fmold", "files-lib-1") + index("fmother", "files-lib-2")
                + index("fmnew", "files-lib-1") + index("fmnew2", "files-lib-1")}
        self.assertGreater(len(lane["docs/map/files-lib-1.md"].encode()), CAP)
        before, b = self.merge(lane)
        sha, log, moved = self.over(before, b)
        self.assertEqual(sha, self.run_git("rev-parse", "HEAD"), "the lane lands: the new head is the amended merge")
        self.assertEqual(log, [])
        self.assertEqual(self.read("docs/map/files-lib-1.md"), shard(bullet("fmold", 1500)), "left as it was on the base")
        two = self.read("docs/map/files-lib-2.md")
        self.assertIn(new, two, "the added bullets word for word, continuation line with them")
        self.assertEqual(two, shard(bullet("fmother", 300), new))
        master = self.read("MASTER.md")
        self.assertIn("[→ files-lib-2](docs/map/files-lib-2.md)", master.split("`fmnew`")[1].splitlines()[0])
        self.assertIn("[→ files-lib-2](docs/map/files-lib-2.md)", master.split("`fmnew2`")[1].splitlines()[0])
        self.assertIn("[→ files-lib-1](docs/map/files-lib-1.md)", master.split("`fmold`")[1].splitlines()[0])
        self.assertEqual(self.run_git("rev-list", "--parents", "-n", "1", "HEAD").count(" "), 2, "still a merge commit")
        self.assertEqual(self.run_git("status", "--porcelain"), "")
        self.assertIn("fmnew", moved["T-9"][0])
        self.assertIn("files-lib-2", moved["T-9"][0])

    def test_a_shard_with_room_beats_one_with_less(self):
        self.base(**{"docs/map/files-lib-3.md": shard(bullet("fmthird", 50))})
        new = bullet("fmnew", 700)
        lane = {"docs/map/files-lib-1.md": shard(bullet("fmold", 1500), new, bullet("fmnew2", 900)),
                "MASTER.md": "# M\n\n" + index("fmold", "files-lib-1") + index("fmother", "files-lib-2")
                + index("fmnew", "files-lib-1") + index("fmnew2", "files-lib-1")}
        before, b = self.merge(lane)
        sha, log, _ = self.over(before, b)
        self.assertTrue(sha, log)
        self.assertIn("`fmnew`", self.read("docs/map/files-lib-3.md"), "files-lib-3 has the most room")
        self.assertNotIn("`fmnew`", self.read("docs/map/files-lib-2.md"))

    def test_no_shard_with_room_makes_a_new_one_with_the_same_header_shape(self):
        full = shard(bullet("fmother", 2300))
        self.base(**{"docs/map/files-lib-2.md": full})
        new = bullet("fmnew", 1400, "  more\n")
        lane = {"docs/map/files-lib-1.md": shard(bullet("fmold", 1500), new),
                "MASTER.md": "# M\n\n" + index("fmold", "files-lib-1") + index("fmother", "files-lib-2")
                + index("fmnew", "files-lib-1")}
        before, b = self.merge(lane)
        sha, log, moved = self.over(before, b)
        self.assertTrue(sha, log)
        made = self.read("docs/map/files-lib-3.md")
        self.assertTrue(made.startswith("# Sampo map: File map, plugin/lib modules\n\nFull entries for [MASTER.md]"), made)
        self.assertIn("### plugin/lib modules, fmnew to fmnew\n\n<details>\n<summary>every module, one line each</summary>\n\n"
                      + new + "\n</details>\n", made)
        self.assertLessEqual(len(made.encode()), CAP)
        self.assertIn("[→ files-lib-3](docs/map/files-lib-3.md)", self.read("MASTER.md").split("`fmnew`")[1].splitlines()[0])
        self.assertEqual(self.run_git("status", "--porcelain"), "")
        self.assertIn("new shard", moved["T-9"][0])
        self.assertEqual(self.run_git("ls-tree", "--name-only", "HEAD", "docs/map/files-lib-3.md"),
                         "docs/map/files-lib-3.md", "the new shard is in the merge commit")

    def test_an_edit_of_an_existing_bullet_is_never_moved_and_the_lane_is_left_out(self):
        self.base()
        edited = bullet("fmold", 1500, "  an edited continuation\n")
        lane = {"docs/map/files-lib-1.md": shard(edited, bullet("fmnew", 1500)),
                "MASTER.md": "# M\n\n" + index("fmold", "files-lib-1") + index("fmother", "files-lib-2")
                + index("fmnew", "files-lib-1")}
        before, b = self.merge(lane)
        sha, log, moved = self.over(before, b)
        self.assertIsNone(sha)
        self.assertEqual(self.run_git("rev-parse", "HEAD"), before, "the merge is undone")
        self.assertIn("files-lib-1.md", log[0][1])
        self.assertIn("cap", log[0][1])
        self.assertEqual(moved, {})

    def test_a_new_bullet_with_no_index_line_in_master_is_left_out(self):
        self.base()
        lane = {"docs/map/files-lib-1.md": shard(bullet("fmold", 1500), bullet("fmnew", 1400))}
        before, b = self.merge(lane)
        sha, log, _ = self.over(before, b)
        self.assertIsNone(sha, "no index line to repoint: the lane is not moved blind")
        self.assertEqual(self.run_git("rev-parse", "HEAD"), before)

    def test_a_shard_over_its_cap_on_the_base_blames_no_lane(self):
        self.base(**{"docs/map/files-lib-1.md": shard(bullet("fmold", 3200))})
        lane = {"docs/map/files-lib-1.md": shard(bullet("fmold", 3200), bullet("fmnew", 10)),
                "MASTER.md": "# M\n\n" + index("fmold", "files-lib-1") + index("fmother", "files-lib-2")
                + index("fmnew", "files-lib-1")}
        before, b = self.merge(lane)
        sha, log, moved = self.over(before, b)
        self.assertTrue(sha)
        self.assertEqual((log, moved), ([], {}))

    def test_a_lane_that_fits_is_untouched(self):
        self.base()
        lane = {"docs/map/files-lib-1.md": shard(bullet("fmold", 1500), bullet("fmnew", 100)),
                "MASTER.md": "# M\n\n" + index("fmold", "files-lib-1") + index("fmother", "files-lib-2")
                + index("fmnew", "files-lib-1")}
        before, b = self.merge(lane)
        merged = self.run_git("rev-parse", "HEAD")
        sha, log, moved = self.over(before, b)
        self.assertEqual(sha, merged, "the merge commit is not rewritten")
        self.assertEqual((log, moved), ([], {}))

    def test_an_ops_shard_is_left_out_as_before(self):
        ops = ("# Sampo map: Operations (part ops-1)\n\n### Dev loop\n\n<details>\n<summary>notes</summary>\n\n"
               + bullet("note", 1500) + "\n</details>\n")
        self.base(**{"docs/map/ops-1.md": ops})
        lane = {"docs/map/ops-1.md": ops.replace("\n</details>", bullet("note2", 2000) + "\n</details>")}
        before, b = self.merge(lane)
        sha, log, _ = self.over(before, b)
        self.assertIsNone(sha)
        self.assertIn("ops-1.md", log[0][1])


class MasterOverflow(MapCap):
    def master(self, *lines):
        return "# M\n\n" + "".join(lines)

    def test_master_over_its_cap_has_the_lanes_index_lines_shortened(self):
        old = [index("fmold", "files-lib-1", 5)] + [index(f"fmx{i}", "files-lib-1", 5) for i in range(30)]
        self.base(**{"MASTER.md": self.master(*old)})
        size = len(self.read("MASTER.md").encode())
        long_line = index("fmnew", "files-lib-1", int((CAP - size) / 5) + 40)
        lane = {"MASTER.md": self.master(*old, long_line)}
        before, b = self.merge(lane)
        self.assertGreater(len(self.read("MASTER.md").encode()), CAP)
        sha, log, moved = self.over(before, b)
        self.assertTrue(sha, log)
        master = self.read("MASTER.md")
        self.assertLessEqual(len(master.encode()), CAP)
        self.assertIn("- `fmnew` [→ files-lib-1](docs/map/files-lib-1.md)\n", master, "the short form: name and link")
        for line in old:
            self.assertIn(line, master, "a line the lane did not add is never touched")
        self.assertEqual(self.run_git("status", "--porcelain"), "")

    def test_only_enough_of_the_lanes_lines_are_shortened(self):
        old = [index(f"fmx{i}", "files-lib-1", 5) for i in range(20)]
        self.base(**{"MASTER.md": self.master(*old)})
        a, bb = index("fma", "files-lib-1", 140), index("fmb", "files-lib-1", 130)
        size = len(self.master(*old, a, bb).encode())
        self.assertGreater(size, CAP, "both lines cross the cap")
        self.assertLess(size - len(a) + 70, CAP, "shortening the longer one is enough")
        before, b = self.merge({"MASTER.md": self.master(*old, a, bb)})
        sha, log, _ = self.over(before, b)
        self.assertTrue(sha, log)
        master = self.read("MASTER.md")
        self.assertIn("- `fma` [→ files-lib-1]", master)
        self.assertIn(bb, master, "the second line was not needed")

    def test_still_over_after_the_shortening_the_lane_is_left_out(self):
        old = [index(f"fmx{i}", "files-lib-1", 5) for i in range(10)]
        self.base(**{"MASTER.md": self.master(*old, "x" * 1000 + "\n")})
        # the lane adds a long line that is no index line, which the short form cannot shrink
        before, b = self.merge({"MASTER.md": self.master(*old, "x" * 1000 + "\n", "prose " * 300 + "\n")})
        sha, log, _ = self.over(before, b)
        self.assertIsNone(sha)
        self.assertEqual(self.run_git("rev-parse", "HEAD"), before)
        self.assertIn("MASTER.md", log[0][1])

    def test_lines_the_base_already_had_are_not_the_lanes_to_shorten(self):
        old = [index(f"fmx{i}", "files-lib-1", 60) for i in range(8)]
        self.base(**{"MASTER.md": self.master(*old)})
        before, b = self.merge({"MASTER.md": self.master(*old, "prose " * 300 + "\n")})
        sha, log, _ = self.over(before, b)
        self.assertIsNone(sha)
        self.assertEqual(self.read("MASTER.md"), self.master(*old), "undone, nothing shortened on the way")


class Symlinks(MapCap):
    """A lane can commit a map file as a symlink: the train never reads or writes through one (security review of T-5381).
    The outside file stays byte for byte as it was and the lane is left out as before."""

    def setUp(self):
        super().setUp()
        self.outside_dir = os.path.join(self._tmp.name, "outside")
        os.makedirs(self.outside_dir)
        self.outside = os.path.join(self.outside_dir, "target.md")

    def outside_has(self, text):
        with open(self.outside, "w", encoding="utf-8") as f:
            f.write(text)
        return text

    def left_out(self, lane, links, before_text):
        before, b = self.merge(lane, links=links)
        merged = self.run_git("rev-parse", "HEAD")
        sha, log, moved = self.over(before, b)
        self.assertIsNone(sha, "the lane is left out as before")
        self.assertEqual(self.run_git("rev-parse", "HEAD"), before)
        self.assertNotEqual(before, merged)
        with open(self.outside, encoding="utf-8") as f:
            self.assertEqual(f.read(), before_text, "the file the link points at is untouched")
        self.assertEqual(os.listdir(self.outside_dir), ["target.md"], "nothing new written beside it either")
        self.assertEqual(moved, {})
        self.assertIn("cap", log[0][1])

    def master_for(self, *names):
        return "# M\n\n" + index("fmold", "files-lib-1") + index("fmother", "files-lib-2") + "".join(
            index(n, "files-lib-1") for n in names)

    def test_a_shard_that_is_a_link_to_a_file_outside_is_never_written_through(self):
        self.base()
        text = self.outside_has(shard(bullet("fmold", 1500), bullet("fmnew", 1400)))  # base + a bullet: a pure addition
        self.left_out({"MASTER.md": self.master_for("fmnew")}, {"docs/map/files-lib-1.md": self.outside}, text)

    def test_the_shard_a_bullet_would_move_to_being_a_link_is_never_written_through(self):
        self.base()
        text = self.outside_has(shard(bullet("fmother", 300)))
        lane = {"docs/map/files-lib-1.md": shard(bullet("fmold", 1500), bullet("fmnew", 1400)),
                "MASTER.md": self.master_for("fmnew")}
        self.left_out(lane, {"docs/map/files-lib-2.md": self.outside}, text)

    def test_master_that_is_a_link_to_a_file_outside_is_never_written_through(self):
        self.base()
        text = self.outside_has(self.master_for("fmnew"))
        lane = {"docs/map/files-lib-1.md": shard(bullet("fmold", 1500), bullet("fmnew", 1400))}
        self.left_out(lane, {"MASTER.md": self.outside}, text)

    def test_a_relative_link_that_leaves_the_tree_is_refused_too(self):
        self.base()
        text = self.outside_has(shard(bullet("fmold", 1500), bullet("fmnew", 1400)))
        rel = os.path.relpath(self.outside, os.path.join(self.wt, "docs", "map"))
        self.left_out({"MASTER.md": self.master_for("fmnew")}, {"docs/map/files-lib-1.md": rel}, text)

    def test_docs_map_being_a_link_to_a_directory_outside_is_never_written_through(self):
        self.base()
        for n in (1, 2):
            shutil.copy(os.path.join(self.wt, "docs", "map", f"files-lib-{n}.md"), self.outside_dir)
        text = self.outside_has("untouched\n")
        os.remove(os.path.join(self.outside_dir, "files-lib-1.md"))
        with open(os.path.join(self.outside_dir, "files-lib-1.md"), "w", encoding="utf-8") as f:
            f.write(shard(bullet("fmold", 1500), bullet("fmnew", 1400)))
        before, b = self.merge({"MASTER.md": self.master_for("fmnew")}, links={"docs/map": self.outside_dir})
        sha, log, moved = self.over(before, b)  # whether the lane lands is not the point: nothing goes through the link
        self.assertEqual(moved, {})
        for n, want in ((2, shard(bullet("fmother", 300))), (1, shard(bullet("fmold", 1500), bullet("fmnew", 1400)))):
            with open(os.path.join(self.outside_dir, f"files-lib-{n}.md"), encoding="utf-8") as f:
                self.assertEqual(f.read(), want)
        self.assertEqual(sorted(os.listdir(self.outside_dir)), ["files-lib-1.md", "files-lib-2.md", "target.md"])

    def test_the_writer_refuses_a_link_it_is_handed(self):
        self.base()
        self.outside_has("kept\n")
        os.symlink(self.outside, os.path.join(self.wt, "docs", "map", "files-lib-9.md"))
        with self.assertRaises(OSError):
            train._write_map(self.wt, "docs/map/files-lib-9.md", "pwned\n")
        with open(self.outside, encoding="utf-8") as f:
            self.assertEqual(f.read(), "kept\n")


class Reported(MapCap):
    def test_the_train_report_names_each_move(self):
        got = {"merged": ["T-9"], "branch": "main", "tip": "a" * 40, "gate_runs": 1, "probes": 0, "left_out": [],
               "notes": [], "map_merged": [], "minutes": {"probe": 0, "gate": 0, "land": 0}, "probes_skipped": 0,
               "lanes_probed": 1, "affected": None, "map_moved": [("T-9", "moved fmnew to docs/map/files-lib-2.md")]}
        lines = train._move_lines(got)
        self.assertEqual(lines, ["  moved T-9: moved fmnew to docs/map/files-lib-2.md"])


if __name__ == "__main__":
    unittest.main()
