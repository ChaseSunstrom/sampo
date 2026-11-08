"""T-3285 (moon C-005835): two open builder lanes that add the same function, or touch the same file, overlap. The lead
sees one OVERLAP line per pair in fm lane list, so one of them is merged or stopped before the other conflicts. Review
fixes: uncommitted and untracked work counts, hub files every lane edits don't pair two lanes, generic and test names
aren't defs, a lane git can't read is named (OVERLAP ?), and a long name list is cut before the advice."""
# map-links: plugin/lib/sampo/lane_overlap.py plugin/lib/sampo/lanes.py
import json
import os
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import lane_overlap as ov

G = ["git", "-c", "user.name=t", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false"]


class Overlap(unittest.TestCase):
    def lanes(self):
        return {
            "T-1": {"files": ["plugin/lib/a.py"], "defs": ["shared_helper", "only_one"]},
            "T-2": {"files": ["plugin/lib/b.py"], "defs": ["shared_helper"]},  # same new function as T-1
            "T-3": {"files": ["plugin/lib/a.py"], "defs": ["other"]},  # same file as T-1
            "T-4": {"files": ["docs/x.md"], "defs": ["unrelated"]},  # shares nothing
        }

    def test_a_shared_def_or_a_shared_file_pairs_two_lanes_and_an_unrelated_lane_stays_out(self):
        rows = ov.overlap(self.lanes())
        self.assertEqual({(r["a"], r["b"]) for r in rows}, {("T-1", "T-2"), ("T-1", "T-3")})
        self.assertEqual((rows[0]["a"], rows[0]["b"]), ("T-1", "T-2"), "a shared new function ranks above a shared file")
        self.assertEqual(rows[0]["defs"], ["shared_helper"])
        self.assertEqual(rows[0]["files"], [])
        self.assertEqual(rows[1]["files"], ["plugin/lib/a.py"])

    def test_the_line_names_both_tasks_and_what_they_share(self):
        lines = ov.lines({"overlaps": ov.overlap(self.lanes()), "unscanned": []})
        self.assertTrue(lines[0].startswith("OVERLAP T-1 and T-2"), lines)
        self.assertIn("shared_helper", lines[0])
        self.assertIn("merge", lines[0].lower(), "the advice says what to do with the pair")
        self.assertIn("plugin/lib/a.py", lines[1])
        self.assertNotIn("T-4", "\n".join(lines))

    def test_no_shared_def_or_file_prints_nothing(self):
        self.assertEqual(ov.overlap({"T-1": {"files": ["a.py"], "defs": ["x"]},
                                     "T-2": {"files": ["b.py"], "defs": ["y"]}}), [])
        self.assertEqual(ov.lines({"overlaps": [], "unscanned": []}), [])
        self.assertEqual(ov.overlap({}), [])

    def test_hub_files_every_lane_edits_never_pair_two_lanes_but_a_shared_code_file_still_does(self):
        hub = {"files": ["CHANGELOG.md", "MASTER.md", "docs/map/files-lib-2.md"], "defs": []}
        self.assertEqual(ov.overlap({"T-1": dict(hub), "T-2": dict(hub)}), [])
        rows = ov.overlap({"T-1": {"files": ["CHANGELOG.md", "a.py"], "defs": []},
                           "T-2": {"files": ["a.py", "CHANGELOG.md"], "defs": []}})
        self.assertEqual(rows[0]["files"], ["a.py"], "the hub file drops out of the shared list")

    def test_pairs_are_capped_at_MAX_PAIRS(self):
        rows = ov.overlap({f"T-{i}": {"files": [], "defs": ["x"]} for i in range(10)})
        self.assertEqual(len(rows), ov.MAX_PAIRS, "45 pairs share x; the line list stops at MAX_PAIRS")

    def test_a_long_name_list_is_cut_and_the_advice_tail_stays(self):
        names = [f"plugin/lib/some_long_module_name_{i}_with_a_long_tail.py" for i in range(4)]
        rows = ov.overlap({"T-1": {"files": names, "defs": []}, "T-2": {"files": names, "defs": []}})
        line = ov.lines({"overlaps": rows, "unscanned": []})[0]
        self.assertLessEqual(len(line), 240, line)
        self.assertTrue(line.endswith("merge one first; the other rebases on it"), line)

    def test_a_lane_that_could_not_be_scanned_prints_a_question_note(self):
        self.assertEqual(ov.lines({"overlaps": [], "unscanned": ["T-9"]}), ["OVERLAP ?: T-9 not scanned"])


class Repo(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.write(self.repo, "plugin/lib/a.py", "def old():\n    return 1\n\ndef gone():\n    return 2\n")
        self.git("add", "-A", cwd=self.repo)
        self.git("commit", "-qm", "base", cwd=self.repo)
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *a, cwd=None):
        return subprocess.run(G + list(a), cwd=cwd or self.repo, capture_output=True, text=True, check=True).stdout

    def write(self, root, rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def branch(self, name, files):
        """A lane folder on a new branch with files committed; returns the folder."""
        path = os.path.join(self.tmp, name.replace("/", "-"))
        self.git("worktree", "add", "-q", "-b", name, path, cwd=self.repo)
        for rel, text in files.items():
            self.write(path, rel, text)
        self.git("add", "-A", cwd=path)
        self.git("commit", "-qm", name, cwd=path)
        return path


class Facts(Repo):
    def test_facts_list_the_files_and_the_defs_a_branch_adds_and_not_the_context_or_removed_ones(self):
        path = self.branch("foreman/T-1", {"plugin/lib/a.py": "def old():\n    return 3\n\ndef shared_helper():\n    return 4\n",
                                           "plugin/lib/new.py": "class Thing:\n    pass\n"})
        got = ov.facts(path, self.base)
        self.assertEqual(got["files"], ["plugin/lib/a.py", "plugin/lib/new.py"])
        self.assertEqual(got["defs"], ["Thing", "shared_helper"], "old is only context, gone is removed")
        self.assertTrue(got["scanned"])

    def test_uncommitted_edits_and_untracked_files_in_the_lane_folder_count(self):
        path = self.branch("foreman/T-2", {"plugin/lib/a.py": "def old():\n    return 1\n"})
        self.write(path, "plugin/lib/a.py", "def old():\n    return 1\n\ndef pending_helper():\n    return 5\n")
        self.write(path, "plugin/lib/fresh.py", "def untracked_helper():\n    return 6\n")
        got = ov.facts(path, self.base)
        self.assertEqual(got["files"], ["plugin/lib/a.py", "plugin/lib/fresh.py"], "a live builder's uncommitted work")
        self.assertEqual(got["defs"], ["pending_helper", "untracked_helper"])

    def test_prose_that_starts_with_a_def_keyword_is_not_a_def(self):
        path = self.branch("foreman/T-3", {"docs/notes.md": "function returns the total here\nsub total is computed\n"})
        got = ov.facts(path, self.base)
        self.assertEqual(got["files"], ["docs/notes.md"])
        self.assertEqual(got["defs"], [], "a word after the keyword with no '(', ':', '{' or '=' is prose")

    def test_generic_and_test_names_are_not_defs(self):
        path = self.branch("foreman/T-4", {"plugin/lib/g.py": "def main():\n    pass\n\ndef setUp(self):\n    pass\n\n"
                                                              "def test_x():\n    pass\n\ndef unique_name():\n    pass\n"})
        self.assertEqual(ov.facts(path, self.base)["defs"], ["unique_name"], "main, setUp and test_* are everywhere")

    def test_a_lane_with_nothing_new_is_scanned_and_empty(self):
        self.assertEqual(ov.facts(self.repo, "HEAD"), {"files": [], "defs": [], "scanned": True})

    def test_a_folder_git_cannot_read_is_unscanned_not_empty(self):
        self.assertEqual(ov.facts(os.path.join(self.tmp, "nowhere"), self.base),
                         {"files": [], "defs": [], "scanned": False})

    def test_a_diff_over_the_line_cap_is_unscanned(self):
        path = self.branch("foreman/T-5", {"plugin/lib/big.py": "x = 1\n" * 10})
        with mock.patch.object(ov, "MAX_LINES", 3):
            self.assertFalse(ov.facts(path, self.base)["scanned"])

    def test_a_git_call_past_the_timeout_is_unscanned(self):
        path = self.branch("foreman/T-6", {"plugin/lib/t.py": "def t():\n    pass\n"})
        with mock.patch.object(ov, "TIMEOUT", 0.000001):
            self.assertFalse(ov.facts(path, self.base)["scanned"])


class Scan(Repo):
    def row(self, tid, status, branch, path, missing=False):
        return {"tasks": [{"id": tid, "status": status, "title": ""}], "branch": branch, "path": path, "missing": missing}

    def test_two_open_lanes_that_share_a_def_pair_and_nothing_is_unscanned(self):
        p1 = self.branch("foreman/T-1", {"plugin/lib/one.py": "def shared_helper():\n    return 1\n"})
        p2 = self.branch("foreman/T-2", {"plugin/lib/two.py": "def shared_helper():\n    return 2\n"})
        got = ov.scan(self.repo, [self.row("T-1", "doing", "foreman/T-1", p1), self.row("T-2", "doing", "foreman/T-2", p2)])
        self.assertEqual([(r["a"], r["b"]) for r in got["overlaps"]], [("T-1", "T-2")])
        self.assertEqual(got["unscanned"], [])

    def test_a_closed_lane_and_a_missing_lane_are_not_scanned(self):
        p1 = self.branch("foreman/T-1", {"plugin/lib/one.py": "def shared_helper():\n    return 1\n"})
        p2 = self.branch("foreman/T-2", {"plugin/lib/two.py": "def shared_helper():\n    return 2\n"})
        rows = [self.row("T-1", "doing", "foreman/T-1", p1), self.row("T-2", "done", "foreman/T-2", p2),
                self.row("T-3", "doing", "foreman/T-2", p2, missing=True)]
        self.assertEqual(ov.scan(self.repo, rows), {"overlaps": [], "unscanned": []})

    def test_an_open_lane_whose_folder_git_cannot_read_is_unscanned_and_paired_with_nothing(self):
        p1 = self.branch("foreman/T-1", {"plugin/lib/one.py": "def only_here():\n    return 1\n"})
        gone = os.path.join(self.tmp, "nowhere")
        got = ov.scan(self.repo, [self.row("T-1", "doing", "foreman/T-1", p1),
                                  self.row("T-2", "doing", "foreman/T-2", gone)])
        self.assertEqual(got, {"overlaps": [], "unscanned": ["T-2"]})

    def test_a_repo_git_cannot_read_leaves_every_open_lane_unscanned(self):
        p1 = self.branch("foreman/T-1", {"plugin/lib/one.py": "def only_here():\n    return 1\n"})
        got = ov.scan(os.path.join(self.tmp, "nowhere"), [self.row("T-1", "doing", "foreman/T-1", p1)])
        self.assertEqual(got, {"overlaps": [], "unscanned": ["T-1"]})


class LaneList(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def lane(self, title, files, commit=True):
        args = ["task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--json"]
        tid = json.JSONDecoder().raw_decode(self.fm(*args).stdout)[0]["id"]
        data = json.loads(self.fm("lane", "new", tid, "--json").stdout)
        for rel, text in files.items():
            path = os.path.join(data["path"], rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        if commit:
            subprocess.run(G + ["add", "-A"], cwd=data["path"], check=True, capture_output=True)
            subprocess.run(G + ["commit", "-qm", title], cwd=data["path"], check=True, capture_output=True)
        return tid

    def test_fm_lane_list_prints_an_overlap_line_for_a_pair_sharing_a_def_and_none_for_an_unrelated_lane(self):
        t1 = self.lane("First", {"plugin/lib/one.py": "def shared_helper():\n    return 1\n"})
        t2 = self.lane("Second", {"plugin/lib/two.py": "def shared_helper():\n    return 2\n"})
        t3 = self.lane("Third", {"docs/three.md": "text\n"})
        out = self.fm("lane", "list").stdout
        lines = [x for x in out.splitlines() if x.startswith("OVERLAP ")]
        self.assertEqual(len(lines), 1, out)
        self.assertIn(t1, lines[0])
        self.assertIn(t2, lines[0])
        self.assertIn("shared_helper", lines[0])
        self.assertNotIn(t3, "\n".join(lines))
        js = json.loads(self.fm("lane", "list", "--json").stdout)
        self.assertEqual([(r["a"], r["b"]) for r in js["overlaps"]], [(t1, t2)])
        self.assertEqual(js["unscanned"], [])

    def test_fm_lane_list_prints_no_overlap_line_when_no_two_lanes_share_anything(self):
        self.lane("Alone", {"plugin/lib/alone.py": "def only_here():\n    return 1\n"})
        self.lane("Apart", {"plugin/lib/apart.py": "def elsewhere():\n    return 2\n"})
        self.assertNotIn("OVERLAP", self.fm("lane", "list").stdout)
        self.assertEqual(json.loads(self.fm("lane", "list", "--json").stdout)["overlaps"], [])

    def test_uncommitted_lanes_overlap_too(self):
        t1 = self.lane("Wip A", {"plugin/lib/wa.py": "def wip_shared():\n    return 1\n"}, commit=False)
        t2 = self.lane("Wip B", {"plugin/lib/wb.py": "def wip_shared():\n    return 2\n"}, commit=False)
        lines = [x for x in self.fm("lane", "list").stdout.splitlines() if x.startswith("OVERLAP ")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(t1, lines[0])
        self.assertIn(t2, lines[0])
        self.assertIn("wip_shared", lines[0])

    def test_lanes_that_only_share_hub_files_do_not_overlap(self):
        self.lane("Hub A", {"CHANGELOG.md": "a\n", "MASTER.md": "a\n"})
        self.lane("Hub B", {"CHANGELOG.md": "b\n", "MASTER.md": "b\n"})
        self.assertNotIn("OVERLAP", self.fm("lane", "list").stdout)
