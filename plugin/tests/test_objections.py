"""T-3276: sampo audit ahead ID lists the objections past reviews raised on this task's diff, before the review: the same
files first, then their folders, then the defect classes whose cue words the added lines carry. Rejected findings and
this task's own are left out; at most 8 rows; one 'none on record' line with nothing. audit prep ends with a hint to it."""
import json
import os

from helpers import ForemanTestCase

from sampo import core as c
from sampo import objections


class Objections(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def finding(self, text, verdict="confirmed", task="T-9000", lens="edge"):
        c.log_event(self.p, "finding", task=task, data={"lens": lens, "verdict": verdict, "text": text})

    def test_the_same_file_first_then_its_folder_then_the_class_the_added_lines_cue(self):
        self.finding("lib/app.py: the retry loop has no cap")
        self.finding("lib/sibling.py: the parser drops the last line", task="T-9002")
        self.finding("docs/guide.md: the write lock is never released", task="T-9003")
        self.finding("other/far.py: a stale cache entry is read", task="T-9004")  # nothing in the diff cues it
        rows = objections.likely(self.p, ["lib/app.py"], "with self.lock:\n    x = 1\n", "T-9999")
        self.assertEqual([r["file"] for r in rows], ["lib/app.py", "lib/sibling.py", "docs/guide.md"])
        self.assertEqual(rows[0]["why"], "same file")
        self.assertEqual(rows[1]["why"], "same directory")
        self.assertIn("race / lock", rows[2]["why"])
        self.assertEqual((rows[0]["lens"], rows[0]["task"], rows[0]["text"]),
                         ("edge", "T-9000", "the retry loop has no cap"))

    def test_rejected_and_this_tasks_own_findings_are_left_out(self):
        self.finding("lib/app.py: a rejected nit about the app", verdict="rejected")
        self.finding("lib/app.py: this task's own nit about the app", task="T-9999")
        self.assertEqual(objections.likely(self.p, ["lib/app.py"], "", "T-9999"), [])

    def test_at_most_eight_rows_newest_first(self):
        for i in range(12):
            self.finding(f"lib/app.py: nit number {i} on the app")
        rows = objections.likely(self.p, ["lib/app.py"], "", "T-9999")
        self.assertEqual(len(rows), objections.CAP)
        self.assertEqual(objections.CAP, 8)
        self.assertTrue(rows[0]["text"].startswith("nit number 11"), rows[0])

    def test_a_cue_must_start_a_word(self):
        self.finding("docs/guide.md: the write lock is never released")
        self.assertEqual(objections.likely(self.p, ["other/x.py"], "unblock the blockade\n", "T-9999"), [])

    def test_nothing_on_record_is_an_empty_list(self):
        self.assertEqual(objections.likely(self.p, ["lib/app.py"], "x = 1\n", "T-9999"), [])

    def test_a_bare_filename_is_not_in_every_folder(self):  # review: a root file's folder is "", which matched all
        self.finding("other.py: a note about a file at the root")
        self.finding("docs/guide.md: a note about a doc page")
        self.assertEqual(objections.likely(self.p, ["app.py"], "", "T-9999"), [])

    def test_a_class_takes_two_rows_and_the_class_with_more_cue_hits_ranks_first(self):  # review: one cue can't fill it
        for i in range(6):
            self.finding(f"lib/far{i}.py: a lock is held across the call {i}")
        self.finding("lib/stale.py: a stale entry is read back")
        rows = objections.likely(self.p, ["other/x.py"], "with self.lock:\n    self.race = 1\n# stale cache\n", "T-9999")
        self.assertEqual([r["why"] for r in rows],
                         ["class: race / lock (a cue in the diff)"] * 2
                         + ["class: wrong signal or stale state (a cue in the diff)"])


class AheadCommand(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = json.loads(self.fm("task", "new", "Parse the last line", "--type", "FIX", "--tier", "S", "--ac",
                                      "done :: true", "--step", "fix the parser", "--json").stdout)["id"]
        self.fm("focus", self.tid)
        os.makedirs(os.path.join(self.repo, "lib"), exist_ok=True)
        with open(os.path.join(self.repo, "lib", "app.py"), "w") as f:
            f.write("def parse(s):\n    return s.split()[:-1]\n")
        self.p = c.find_project(self.repo)

    def test_ahead_lists_the_objections_on_the_diffs_files_and_one_line_each(self):
        c.log_event(self.p, "finding", task="T-9000", data={"lens": "edge", "verdict": "confirmed",
                                                            "text": "lib/app.py: the parser drops the last line"})
        out = self.fm("audit", "ahead", self.tid).stdout
        self.assertIn("lib/app.py: the parser drops the last line", out)
        self.assertIn("same file", out)
        self.assertIn("edge, T-9000", out)
        self.assertNotIn("none on record", out)

    def test_ahead_sees_the_uncommitted_edits_beside_a_lane_branch_commit(self):  # review: the pair prep adds too
        import subprocess
        g = lambda *a: subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True)  # noqa: E731
        g("checkout", "-q", "-b", "foreman/lane")
        with open(os.path.join(self.repo, "CHANGELOG.md"), "w") as f:
            f.write("- a line\n")
        g("add", "CHANGELOG.md")
        g("commit", "-qm", "changelog")
        g("checkout", "-q", "-")
        b = next(x for x in c.load_briefs(self.p) if x.id == self.tid)
        b.meta["lane_branch"] = "foreman/lane"
        c.save_brief(self.p, b)
        c.log_event(self.p, "finding", task="T-9000", data={"lens": "edge", "verdict": "confirmed",
                                                            "text": "lib/app.py: the parser drops the last line"})
        out = self.fm("audit", "ahead", self.tid).stdout
        self.assertIn("(2 files", out, "the committed CHANGELOG.md and the untracked lib/app.py")
        self.assertIn("lib/app.py: the parser drops the last line", out)

    def test_ahead_with_nothing_on_record_prints_one_none_on_record_line(self):
        out = self.fm("audit", "ahead", self.tid).stdout
        self.assertEqual(len([ln for ln in out.splitlines() if "none on record" in ln]), 1, out)

    def test_audit_prep_ends_with_a_hint_to_run_ahead_first(self):
        out = self.fm("audit", "prep", self.tid).stdout
        self.assertIn("sampo audit ahead", out.strip().splitlines()[-1], out)


if __name__ == "__main__":
    import unittest
    unittest.main()
