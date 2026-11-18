"""T-4924: `sampo task show ID --full` prints the brief and its story (what --story prints) in one call. Two sessions
reached for it and found only --story, which prints the ledger alone."""
import json
import unittest

from helpers import ForemanTestCase

from sampo import core as c


class TaskShowFull(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = json.loads(self.fm("task", "new", "Show the whole task", "--type", "FEATURE", "--tier", "S",
                                      "--ac", "shows :: true", "--step", "print it", "--json").stdout)["id"]
        self.fm("focus", self.tid)
        self.fm("task", "log", "a note on the way")

    def test_full_exits_zero_and_holds_the_brief_then_the_story(self):
        r = self.fm("task", "show", self.tid, "--full")
        self.assertEqual(r.returncode, 0, r.stderr)
        brief = self.fm("task", "show", self.tid).stdout
        story = self.fm("task", "show", self.tid, "--story").stdout
        self.assertTrue(brief and story, "both halves exist to compare")
        self.assertTrue(r.stdout.startswith(brief), "the brief comes first, as plain show prints it")
        self.assertTrue(r.stdout.endswith(story), "the story follows, as --story prints it")
        self.assertIn("## Steps", r.stdout, "the brief's sections are in it")
        self.assertIn("a note on the way", r.stdout, "the story's log is in it")

    def test_story_with_full_is_full_not_the_story_alone(self):
        full = self.fm("task", "show", self.tid, "--full").stdout
        self.assertEqual(self.fm("task", "show", self.tid, "--story", "--full").stdout, full)
        self.assertIn("## Steps", full)

    def test_full_json_is_the_brief_detail_with_the_story_under_story(self):
        d = json.loads(self.fm("task", "show", self.tid, "--full", "--json").stdout)
        self.assertEqual(d["id"], self.tid)
        self.assertIn("criteria", d, "the brief's JSON is kept")
        self.assertEqual(d["story"], json.loads(self.fm("task", "show", self.tid, "--story", "--json").stdout))

    def test_full_writes_nothing(self):
        path = c.find_brief(c.find_project(self.repo), self.tid).path
        with open(path) as f:
            before = f.read()
        self.fm("task", "show", self.tid, "--full")
        self.fm("task", "show", self.tid, "--full")
        with open(path) as f:
            self.assertEqual(f.read(), before, "a read: a second run changes nothing")

    def test_full_on_an_unknown_id_fails_like_show(self):
        r = self.fm("task", "show", "T-9999", "--full", check=False)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no task T-9999", r.stderr)

    def test_the_help_says_what_full_adds(self):
        helped = " ".join(self.fm("task", "show", "--help").stdout.split())  # argparse wraps lines; join them
        self.assertIn("--full the brief, then its story", helped)


if __name__ == "__main__":
    unittest.main()
