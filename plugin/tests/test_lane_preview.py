"""T-3268: fm lane list trial-merges every open lane branch against main and against each other (git merge-tree, no
checkout) and names the files each clashing pair would conflict on, so a clash shows before a lane merge does. Clean
pairs say nothing; a git failure or a timeout is no clash, never an exception."""
import os
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import lane_preview as lp


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout


def write(root, rel, text):
    with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
        f.write(text)


class Clashes(ForemanTestCase):
    """clashes(main, branches) on a repo: main has f.txt (lines 1..5) and g.txt; lanes branch from its first commit."""

    def setUp(self):
        super().setUp()
        write(self.repo, "f.txt", "1\n2\n3\n4\n5\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")

    def lane(self, name, edits):
        """A lane: a worktree on foreman/<name> from main's HEAD, with one commit of the edits {file: text}."""
        path = os.path.join(self.tmp, "lanes", name)
        git(self.repo, "worktree", "add", "-q", "-b", f"foreman/{name}", path)
        for rel, text in edits.items():
            write(path, rel, text)
        git(path, "add", "-A")
        git(path, "commit", "-qm", name)
        return f"foreman/{name}"

    def main_moves(self, edits):
        for rel, text in edits.items():
            write(self.repo, rel, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "main moves on")

    def test_same_line_clash_clean_pair_and_clash_with_main(self):
        a = self.lane("T-A", {"f.txt": "1\nA2\n3\n4\n5\n"})  # line 2 ...
        b = self.lane("T-B", {"f.txt": "1\nB2\n3\n4\n5\n"})  # ... and line 2 again: a clash between two lanes
        c = self.lane("T-C", {"g.txt": "y\n"})  # a clean lane
        d = self.lane("T-D", {"f.txt": "1\n2\n3\n4\nD5\n"})  # line 5, which main changes below
        self.main_moves({"f.txt": "1\n2\n3\n4\nM5\n"})
        before = (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"),
                  git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"))
        got = lp.clashes(self.repo, [a, b, c, d])
        after = (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"),
                 git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"))
        self.assertEqual(got, {("main", d): ["f.txt"], (a, b): ["f.txt"]})  # clean pairs are omitted
        self.assertEqual(before, after, "a trial merge checks nothing out and moves no ref")

    def test_clean_branches_have_no_clash(self):
        c = self.lane("T-C", {"g.txt": "y\n"})
        self.assertEqual(lp.clashes(self.repo, [c]), {})
        self.assertEqual(lp.clashes(self.repo, []), {})

    def test_a_missing_branch_or_a_git_failure_is_no_clash(self):
        self.assertEqual(lp.clashes(self.repo, ["foreman/nope"]), {})  # merge-tree exits 1 with no tree: not a conflict
        a = self.lane("T-A", {"f.txt": "1\nA2\n3\n4\n5\n"})
        timed_out = subprocess.CompletedProcess([], 124, "", "timed out after 10 s")
        with mock.patch.object(lp, "_git", return_value=timed_out):
            self.assertEqual(lp.clashes(self.repo, [a]), {})
        with mock.patch.object(lp, "_git", return_value=subprocess.CompletedProcess([], 129, "", "usage")):
            self.assertEqual(lp.clashes(self.repo, [a]), {})  # an old git without --write-tree

    def test_pairs_capped_at_twelve_branches(self):
        names = [f"foreman/T-{i:02d}" for i in range(13)]  # 13 branches: the first 12 plus main
        calls = []

        def clean(root, *args, **kw):
            calls.append(args)
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(lp, "_git", side_effect=clean):
            self.assertEqual(lp.clashes(self.repo, names), {})
        merges = [a for a in calls if a[0] == "merge-tree"]  # the one rev-parse is main's label, not a pair
        self.assertEqual(len(merges), 13 * 12 // 2, "one merge-tree per pair of the 12 branches and main")

    def test_the_time_budget_stops_further_pairs(self):
        a = self.lane("T-A", {"f.txt": "1\nA2\n3\n4\n5\n"})
        with mock.patch.object(lp, "BUDGET_S", 0):
            self.assertEqual(lp.clashes(self.repo, [a]), {}, "no pair is tried once the budget is spent")
            got = lp.scan(self.repo, [a])
        self.assertEqual((got.checked, got.total), (0, 1), "the spent budget is said, not silent")
        self.assertIn("budget", got.note)

    def test_a_mid_run_timeout_is_a_partial_result_and_says_so(self):
        a = self.lane("T-A", {"f.txt": "1\nA2\n3\n4\n5\n"})
        b = self.lane("T-B", {"f.txt": "1\nB2\n3\n4\n5\n"})
        c = self.lane("T-C", {"g.txt": "y\n"})
        d = self.lane("T-D", {"f.txt": "1\n2\n3\n4\nD5\n"})
        self.main_moves({"f.txt": "1\n2\n3\n4\nM5\n"})
        real = lp._git

        def slow_for_b(root, *args, **kw):  # every trial merge that names T-B times out, the rest run
            if b in args:
                return subprocess.CompletedProcess([], 124, "", "timed out after 10 s")
            return real(root, *args, **kw)

        with mock.patch.object(lp, "_git", side_effect=slow_for_b):
            got = lp.scan(self.repo, [a, b, c, d])
        self.assertEqual(got.pairs, {("main", d): ["f.txt"]}, "the (a, b) clash is missed while T-B's merges time out")
        self.assertEqual((got.checked, got.total), (6, 10), "four of main and four lanes' ten pairs timed out")
        self.assertIn("timed out", got.note)

    def test_a_conflicted_name_with_a_space_or_non_ascii_comes_through_whole(self):
        name = "sp ace é.txt"
        a = self.lane("T-A", {name: "a\n"})  # both lanes add the same new file: an add/add clash
        b = self.lane("T-B", {name: "b\n"})
        self.assertEqual(lp.clashes(self.repo, [a, b]), {(a, b): [name]})

    def test_main_is_labelled_with_the_main_checkouts_branch(self):
        d = self.lane("T-D", {"f.txt": "1\n2\n3\n4\nD5\n"})
        self.main_moves({"f.txt": "1\n2\n3\n4\nM5\n"})
        git(self.repo, "branch", "-m", "trunk")
        self.assertEqual(lp.clashes(self.repo, [d]), {("trunk", d): ["f.txt"]})

    def test_the_twelve_branch_cap_says_how_many_pairs_were_not_tried(self):
        names = [f"foreman/T-{i:02d}" for i in range(13)]

        def clean(root, *args, **kw):
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(lp, "_git", side_effect=clean):
            got = lp.scan(self.repo, names)
        self.assertEqual((got.checked, got.total), (78, 91), "C(14, 2) pairs of main and 13 branches, 78 of them tried")
        self.assertIn("more than 12 branches", got.note)

    def test_an_old_git_is_named_in_the_note(self):
        a = self.lane("T-A", {"f.txt": "1\nA2\n3\n4\n5\n"})
        with mock.patch.object(lp, "_git", return_value=subprocess.CompletedProcess([], 129, "", "usage")):
            got = lp.scan(self.repo, [a])
        self.assertEqual((got.pairs, got.checked, got.total), ({}, 0, 1))
        self.assertIn("2.38", got.note)

    def test_a_call_gets_no_more_time_than_the_budget_left(self):
        a = "foreman/T-A"
        seen = []

        def clean(root, *args, **kw):
            if "merge-tree" in args:
                seen.append(kw["timeout"])
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(lp, "BUDGET_S", 3), mock.patch.object(lp, "_git", side_effect=clean):
            lp.scan(self.repo, [a])
        self.assertTrue(seen)
        self.assertTrue(all(t <= 3 for t in seen), "a call's timeout is the time left, under PAIR_S: " + repr(seen))

    def test_a_timeout_cut_short_by_the_budget_says_budget_spent(self):
        a = "foreman/T-A"
        timed_out = subprocess.CompletedProcess([], 124, "", "timed out")
        with mock.patch.object(lp, "BUDGET_S", 3), mock.patch.object(lp, "_git", return_value=timed_out):
            got = lp.scan(self.repo, [a])
        self.assertIn("budget spent", got.note)
        self.assertNotIn("timed out", got.note, "a call the budget cut short did not time out on its own")

    def test_past_twelve_the_newest_are_tried_and_the_oldest_are_named(self):
        names = [f"foreman/T-{i:02d}" for i in range(13)]  # T-12 has the newest tip, T-00 the oldest
        dates = "".join(f"{1000 + i}\t{n}\n" for i, n in enumerate(names))
        tried = []

        def fake(root, *args, **kw):
            if args[0] == "for-each-ref":
                return subprocess.CompletedProcess([], 0, dates, "")
            if args[0] == "rev-parse":
                return subprocess.CompletedProcess([], 0, "main\n", "")
            tried.extend(args)
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(lp, "_git", side_effect=fake):
            got = lp.scan(self.repo, names)
        self.assertIn("foreman/T-12", tried)
        self.assertNotIn("foreman/T-00", tried, "the oldest branch is the one left out")
        self.assertIn("not tried: foreman/T-00", got.note)
        self.assertEqual((got.checked, got.total), (78, 91))


class LaneListPreview(ForemanTestCase):
    """fm lane list names the clashing pairs in text (a 'Will conflict:' line each) and in json ('conflicts')."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        write(self.repo, "f.txt", "1\n2\n3\n4\n5\n")
        write(self.repo, "g.txt", "x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")

    def lane(self, name, edits, branch=None):
        path = os.path.join(self.tmp, "lanes", name)
        branch = branch or f"foreman/{name}"
        git(self.repo, "worktree", "add", "-q", "-b", branch, path)
        for rel, text in edits.items():
            write(path, rel, text)
        git(path, "add", "-A")
        git(path, "commit", "-qm", name)
        return branch

    def main_moves(self, edits):
        for rel, text in edits.items():
            write(self.repo, rel, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "main moves on")

    def pairs(self, rows):
        return {tuple(sorted(r["pair"])): r["files"] for r in rows}

    def test_clashes_are_named_in_text_and_json(self):
        self.lane("T-A", {"f.txt": "1\nA2\n3\n4\n5\n"})
        self.lane("T-B", {"f.txt": "1\nB2\n3\n4\n5\n"})
        self.lane("T-C", {"g.txt": "y\n"})
        self.lane("T-D", {"f.txt": "1\n2\n3\n4\nD5\n"})
        self.main_moves({"f.txt": "1\n2\n3\n4\nM5\n"})
        text = self.fm("lane", "list").stdout
        clash = [ln for ln in text.splitlines() if ln.startswith("Will conflict:")]
        self.assertEqual(len(clash), 2, text)
        self.assertTrue(any("foreman/T-A" in ln and "foreman/T-B" in ln and "f.txt" in ln for ln in clash), clash)
        self.assertTrue(any("main" in ln and "foreman/T-D" in ln and "f.txt" in ln for ln in clash), clash)
        js = self.fm_json("lane", "list")
        self.assertEqual(self.pairs(js["conflicts"]),
                         {("foreman/T-A", "foreman/T-B"): ["f.txt"], ("foreman/T-D", "main"): ["f.txt"]})
        self.assertEqual(len(js["lanes"]), 4, "the four lanes are still listed as before (main's checkout is not a row)")

    def test_a_clean_list_prints_no_clash_line(self):
        self.lane("T-C", {"g.txt": "y\n"})
        text = self.fm("lane", "list").stdout
        self.assertNotIn("Will conflict", text)
        self.assertEqual(self.fm_json("lane", "list")["conflicts"], [])

    def test_ten_pairs_and_three_files_at_most_in_text(self):
        for i in range(5):  # five lanes that each rewrite line 2 of f.txt, and main rewrites it too: every pair clashes
            self.lane(f"T-{i}", {"f.txt": f"1\nL{i}\n3\n4\n5\n"})
        self.main_moves({"f.txt": "1\nM\n3\n4\n5\n"})
        text = self.fm("lane", "list").stdout
        clash = [ln for ln in text.splitlines() if ln.startswith("Will conflict:")]
        self.assertEqual(len(clash), 10, text)
        self.assertEqual(len(self.fm_json("lane", "list")["conflicts"]), 15, "json keeps every pair: C(6, 2)")
        self.assertIn("more clashing pairs", text)

    def test_files_capped_in_text(self):
        many = {f"h{k}.txt": f"lane one {k}\n" for k in range(5)}
        self.lane("T-1", many)
        self.lane("T-2", {k: f"lane two {k}\n" for k in many})
        text = self.fm("lane", "list").stdout
        clash = [ln for ln in text.splitlines() if ln.startswith("Will conflict:")]
        self.assertEqual(len(clash), 1, text)
        self.assertIn("+2 more", clash[0])
        self.assertEqual(len(self.fm_json("lane", "list")["conflicts"][0]["files"]), 5)

    def test_a_builder_lane_on_a_worktree_branch_is_previewed(self):
        self.lane("agent-a1", {"f.txt": "1\nBUILDER\n3\n4\n5\n"}, branch="worktree-agent-a1")
        self.main_moves({"f.txt": "1\nM\n3\n4\n5\n"})
        js = self.fm_json("lane", "list")
        self.assertEqual(self.pairs(js["conflicts"]), {("main", "worktree-agent-a1"): ["f.txt"]})

    def test_no_preview_skips_the_trial_merges(self):
        self.lane("T-A", {"f.txt": "1\nA2\n3\n4\n5\n"})
        self.lane("T-B", {"f.txt": "1\nB2\n3\n4\n5\n"})
        self.main_moves({"f.txt": "1\n2\n3\n4\nM5\n"})
        text = self.fm("lane", "list", "--no-preview").stdout
        self.assertNotIn("Will conflict", text)
        self.assertNotIn("preview incomplete", text, "a skipped preview is not an incomplete one")
        js = self.fm_json("lane", "list", "--no-preview")
        self.assertEqual(js["conflicts"], [])
        self.assertTrue(js["preview"]["skipped"])
        self.assertEqual(len(js["lanes"]), 2)

    def test_a_full_preview_reports_its_count_in_json(self):
        self.lane("T-A", {"f.txt": "1\nA2\n3\n4\n5\n"})
        self.lane("T-B", {"f.txt": "1\nB2\n3\n4\n5\n"})
        preview = self.fm_json("lane", "list")["preview"]
        self.assertEqual((preview["checked"], preview["total"], preview["complete"]), (3, 3, True))

    def test_more_than_twelve_branches_says_the_preview_is_incomplete(self):
        for i in range(13):  # each lane adds its own file, so no pair clashes; 91 pairs, 78 tried
            self.lane(f"T-{i:02d}", {f"x{i:02d}.txt": "x\n"})
        slow = {"SAMPO_LANE_PREVIEW_BUDGET_S": "600"}  # 78 real merge-trees: the 20 s default fails under load
        text = self.fm("lane", "list", env=slow).stdout
        self.assertIn("(preview incomplete: 78 of 91 pairs checked: more than 12 branches", text)
        preview = self.fm_json("lane", "list", env=slow)["preview"]
        self.assertEqual((preview["checked"], preview["total"], preview["complete"]), (78, 91, False))


if __name__ == "__main__":
    unittest.main()
