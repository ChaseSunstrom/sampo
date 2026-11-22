"""T-5472: MASTER.md and docs/map/*.md merge through a key-aware three-way driver (merge=sampo-map), not a union.

A union keeps every line of both sides, so a line the lane carried unchanged from its base comes back beside the
version trunk changed or moved: on 2026-11-21 23 stale MASTER.md bullets returned that way in one live land. The
driver knows a map line by its module key (- `name`), so a lane's unchanged line takes trunk's state, absence
included, and a lane's change replaces trunk's unchanged line wherever trunk put it."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from sampo import mapmerge  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
L4, L8 = "[→ files-lib-4](docs/map/files-lib-4.md)", "[→ files-lib-8](docs/map/files-lib-8.md)"


def text(*lines):
    return "".join(f"{x}\n" for x in lines)


class Resolve(unittest.TestCase):
    def merge(self, base, ours, theirs, **kw):
        out, conflicts = mapmerge.merge(base, ours, theirs, **kw)
        self.assertNotRegex(out, r"(?m)^(<{7}|={7}|>{7})" if not conflicts else r"\A\Z")
        return out, conflicts

    def test_a_line_the_lane_left_at_its_base_takes_trunks_change(self):
        """The 2026-11-21 case: trunk repointed fma's line; the lane added fmn beside its base copy."""
        base = text("# Map", f"- `fma` one {L4}", "- `fmb` two")
        ours = text("# Map", f"- `fma` one {L8}", "- `fmb` two")
        theirs = text("# Map", f"- `fma` one {L4}", "- `fmn` the lane's module", "- `fmb` two")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual(conflicts, 0)
        self.assertEqual(out, text("# Map", f"- `fma` one {L8}", "- `fmn` the lane's module", "- `fmb` two"))

    def test_a_line_trunk_dropped_stays_dropped(self):
        base = text("- `fma` one", "- `fmold` stale", "- `fmb` two")
        ours = text("- `fma` one", "- `fmb` two")
        theirs = text("- `fma` one", "- `fmold` stale", "- `fmn` new", "- `fmb` two")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual((out, conflicts), (text("- `fma` one", "- `fmn` new", "- `fmb` two"), 0))

    def test_a_lane_edit_follows_the_line_trunk_moved(self):
        base = text("- `fma` one", "- `fmx` moved", "- `fmb` two", "- `fmc` three")
        ours = text("- `fma` one", "- `fmb` two", "- `fmc` three", "- `fmx` moved")
        theirs = text("- `fma` one", "- `fmx` moved, now with the lane's words", "- `fmb` two", "- `fmc` three")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual(conflicts, 0)
        self.assertEqual(out, text("- `fma` one", "- `fmb` two", "- `fmc` three",
                                   "- `fmx` moved, now with the lane's words"))

    def test_a_lane_edit_beside_a_trunk_insert(self):
        base = text("- `fma` one", "- `fmb` two")
        ours = text("- `fma` one", "- `fmq` trunk's", "- `fmb` two")
        theirs = text("- `fma` one, edited", "- `fmb` two")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual((out, conflicts), (text("- `fma` one, edited", "- `fmq` trunk's", "- `fmb` two"), 0))

    def test_a_lane_deletion_beside_a_trunk_insert(self):
        base = text("- `fma` one", "- `fmgone` dead", "- `fmb` two")
        ours = text("- `fma` one", "- `fmgone` dead", "- `fmq` trunk's", "- `fmb` two")
        theirs = text("- `fma` one", "- `fmb` two")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual((out, conflicts), (text("- `fma` one", "- `fmq` trunk's", "- `fmb` two"), 0))

    def test_two_sides_adding_beside_one_neighbour_keep_both(self):
        base = text("- `fma` one", "- `fmb` two")
        ours = text("- `fma` one", "- `fmtrunk` t", "- `fmb` two")
        theirs = text("- `fma` one", "- `fmlane` l", "- `fmb` two")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual((out, conflicts), (text("- `fma` one", "- `fmtrunk` t", "- `fmlane` l", "- `fmb` two"), 0))

    def test_both_sides_appending_to_one_line_keep_both_clauses(self):
        """T-4375's case: two lanes each add a sentence to one entry: one line carries both, no copy."""
        base = text("- `fma` one.", "- `fmb` two")
        ours = text("- `fma` one. Trunk adds this.", "- `fmb` two")
        theirs = text("- `fma` one. Lane adds that.", "- `fmb` two")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual((out, conflicts), (text("- `fma` one. Trunk adds this. Lane adds that.", "- `fmb` two"), 0))

    def test_a_repoint_and_a_body_edit_of_one_line_both_survive(self):
        base = text(f"- `fma` one {L4}")
        ours = text(f"- `fma` one {L8}")
        theirs = text(f"- `fma` one (T-9) {L4}")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual((out, conflicts), (text(f"- `fma` one (T-9) {L8}"), 0))

    def test_both_sides_rewriting_one_line_is_a_conflict_or_both_lines_when_asked(self):
        base = text("- `fma` the old words", "- `fmb` two")
        ours = text("- `fma` trunk's new words", "- `fmb` two")
        theirs = text("- `fma` the lane's words", "- `fmb` two")
        out, conflicts = mapmerge.merge(base, ours, theirs)
        self.assertEqual(conflicts, 1)
        self.assertRegex(out, r"(?m)^<{7}")
        out, conflicts = mapmerge.merge(base, ours, theirs, keep_both=True)
        self.assertEqual(conflicts, 1, "kept twice is still counted, so the train asks for a read")
        self.assertEqual(out, text("- `fma` trunk's new words", "- `fma` the lane's words", "- `fmb` two"))

    def test_one_module_both_sides_added_at_two_places_is_a_conflict(self):
        """A stale lane branch adding a module line trunk already added elsewhere: never two lines for one module."""
        base = text("- `fma` one", "- `fmb` two", "- `fmc` three")
        ours = text("- `fma` one", "- `fmnew` trunk's words", "- `fmb` two", "- `fmc` three")
        theirs = text("- `fma` one", "- `fmb` two", "- `fmc` three", "- `fmnew` the lane's words")
        out, conflicts = mapmerge.merge(base, ours, theirs)
        self.assertEqual(conflicts, 1)
        self.assertRegex(out, r"(?m)^<{7}")
        out, conflicts = mapmerge.merge(base, ours, theirs, keep_both=True)
        self.assertEqual(conflicts, 1)
        self.assertEqual(out.count("`fmnew`"), 2)

    def test_a_line_both_sides_moved_to_two_places_comes_out_once(self):
        """T-5472 review: the diff3 walk takes trunk's move and the lane's move as two one-sided stretches."""
        for line in ("prose B", "- `fmb` b"):
            base = text("a", line, "c", "d", "e")
            out, conflicts = self.merge(base, text("a", "c", "d", "e", line), text("a", "c", "d", line, "e"))
            self.assertEqual((out, conflicts), (text("a", "c", "d", "e", line), 0), line)

    def test_one_module_both_sides_added_with_the_same_words_comes_out_once(self):
        base = text("- `fma` one", "- `fmb` two", "- `fmc` three")
        new = "- `fmnew` the same words"
        out, conflicts = self.merge(base, text("- `fma` one", new, "- `fmb` two", "- `fmc` three"),
                                    text("- `fma` one", "- `fmb` two", "- `fmc` three", new))
        self.assertEqual((out.count("`fmnew`"), conflicts), (1, 0))

    def test_a_lanes_code_fences_beside_trunks_are_kept(self):
        """T-5472 review: a keyless line both sides hold more than once (a fence) is never shared between blocks."""
        base = text("h", "m1 the middle line", "t")
        ours = text("h", "```", "x", "```", "m1 the middle line, trunk's", "t")
        theirs = text("h", "m1 the middle line", "```", "n", "```", "t")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual(conflicts, 0)
        self.assertEqual(out.count("```\n"), 4)
        self.assertIn("```\nn\n```\n", out)
        self.assertIn("m1 the middle line, trunk's\n", out)

    def test_crlf_lines_survive(self):
        base = "- `fma` one\r\n- `fmb` two\r\n"
        ours = "- `fma` one, trunk\r\n- `fmb` two\r\n"
        theirs = "- `fma` one\r\n- `fmn` lane\r\n- `fmb` two\r\n"
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual((out, conflicts), ("- `fma` one, trunk\r\n- `fmn` lane\r\n- `fmb` two\r\n", 0))

    def test_the_driver_that_cannot_run_exits_2_and_leaves_ours(self):
        tmp = tempfile.mkdtemp(prefix="fm-mapmerge-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        a = os.path.join(tmp, "ours")
        with open(a, "w", encoding="utf-8") as f:
            f.write("- `fma` trunk's\n")
        code = mapmerge.main(["mapmerge.py", os.path.join(tmp, "missing-base"), a, os.path.join(tmp, "missing")])
        self.assertEqual(code, 2)
        with open(a, encoding="utf-8") as f:
            self.assertEqual(f.read(), "- `fma` trunk's\n")

    def test_a_file_both_sides_added_keeps_one_header(self):
        ours = text("# Shard", "", "- `fma` one")
        theirs = text("# Shard", "", "- `fmb` two")
        out, conflicts = self.merge("", ours, theirs)
        self.assertEqual((out, conflicts), (text("# Shard", "", "- `fma` one", "- `fmb` two"), 0))

    def test_a_continuation_line_trunk_moved_with_its_entry_takes_the_lanes_edit(self):
        base = text("- `fma` one", "  more about fma", "- `fmb` two", "- `fmc` three")
        ours = text("- `fmb` two", "- `fmc` three", "- `fma` one", "  more about fma")
        theirs = text("- `fma` one", "  more about fma, and the lane's", "- `fmb` two", "- `fmc` three")
        out, conflicts = self.merge(base, ours, theirs)
        self.assertEqual((out, conflicts),
                         (text("- `fmb` two", "- `fmc` three", "- `fma` one", "  more about fma, and the lane's"), 0))


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


class Driver(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="fm-mapmerge-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        shutil.copy(os.path.join(ROOT, ".gitattributes"), os.path.join(self.repo, ".gitattributes"))
        git(self.repo, "init", "-q", "-b", "main")

    def put(self, rel, body, msg):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path) or self.repo, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)

    def read(self, rel):
        with open(os.path.join(self.repo, rel), encoding="utf-8") as f:
            return f.read()

    def test_ensure_writes_the_driver_once_and_mends_a_stale_one(self):
        self.assertTrue(mapmerge.ensure(self.repo))
        got = git(self.repo, "config", "--get", "merge.sampo-map.driver").stdout.strip()
        self.assertIn("mapmerge.py", got)
        self.assertIn("%O %A %B", got)
        self.assertFalse(mapmerge.ensure(self.repo), "already right: nothing written")
        git(self.repo, "config", "merge.sampo-map.driver", "python3 /gone/mapmerge.py %O %A %B")
        self.assertTrue(mapmerge.ensure(self.repo))
        self.assertEqual(git(self.repo, "config", "--get", "merge.sampo-map.driver").stdout.strip(), got)

    def test_a_real_git_merge_through_the_driver_keeps_no_stale_line(self):
        for rel in ("MASTER.md", "docs/map/files-lib-1.md"):
            git(self.repo, "checkout", "-q", "main")
            self.put(rel, text("# Map", f"- `fma` one {L4}", "- `fmb` two"), f"base {rel}")
        mapmerge.ensure(self.repo)
        git(self.repo, "checkout", "-qb", "lane")
        for rel in ("MASTER.md", "docs/map/files-lib-1.md"):
            self.put(rel, text("# Map", f"- `fma` one {L4}", "- `fmn` lane", "- `fmb` two"), f"lane {rel}")
        git(self.repo, "checkout", "-q", "main")
        for rel in ("MASTER.md", "docs/map/files-lib-1.md"):
            self.put(rel, text("# Map", f"- `fma` one {L8}", "- `fmb` two"), f"trunk {rel}")
        r = git(self.repo, "merge", "--no-edit", "lane")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rel in ("MASTER.md", "docs/map/files-lib-1.md"):
            self.assertEqual(self.read(rel), text("# Map", f"- `fma` one {L8}", "- `fmn` lane", "- `fmb` two"), rel)

    def test_a_rewrite_on_both_sides_stops_the_git_merge_with_markers(self):
        self.put("MASTER.md", text("- `fma` the old words"), "base")
        mapmerge.ensure(self.repo)
        git(self.repo, "checkout", "-qb", "lane")
        self.put("MASTER.md", text("- `fma` the lane's words"), "lane")
        git(self.repo, "checkout", "-q", "main")
        self.put("MASTER.md", text("- `fma` trunk's new words"), "trunk")
        r = git(self.repo, "merge", "--no-edit", "lane")
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(self.read("MASTER.md"), r"(?m)^<{7}")
        self.assertIn("MASTER.md", git(self.repo, "diff", "--name-only", "--diff-filter=U").stdout)


if __name__ == "__main__":
    unittest.main()
