"""T-1877: fm task drop, defer and unblock take id ranges (T-3..T-7, T-3..7) and lists (T-3,T-9); one failure
doesn't stop the rest; unblock moves blocked work back."""
import json
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c


class Bulk(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.fm("task", "new", "A thing", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                "--step", "s", "--focus")
        for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"):  # T-0002..T-0007
            self.fm("capture", f"Idea {word} in its own words", "--anyway")

    def status(self, *ids):
        return [c.find_brief(self.p, t).status for t in ids]

    def test_drop_defer_and_unblock_take_ranges_and_lists(self):
        self.assertIn("3 of 3 deferred.", self.fm("task", "defer", "T-0002..T-0004").stdout)
        self.assertEqual(self.status("T-0002", "T-0003", "T-0004", "T-0001"), ["deferred"] * 3 + ["active"])
        self.assertIn("2 of 2 dropped.", self.fm("task", "drop", "t-5,T-0007", "--reason", "duplicates").stdout)
        self.assertEqual(self.status("T-0005", "T-0006", "T-0007"), ["dropped", "captured", "dropped"])
        self.assertIn("dropped: duplicates", c.find_brief(self.p, "T-0007").section("Log"))
        r = self.fm("task", "drop", "T-0002..4", check=False)  # no reason: refused before any id is touched
        self.assertEqual((r.returncode, "needs a reason" in r.stderr), (1, True), r.stderr)
        self.assertEqual(self.status("T-0002", "T-0003", "T-0004"), ["deferred"] * 3)
        for tid in ("T-0003", "T-0004"):
            self.fm("task", "block", tid, "waits on the API")
        r = self.fm("task", "unblock", "T-0002..T-0004", "the API is back", check=False)
        self.assertEqual(r.returncode, 1, r.stderr)  # one failed, the rest went through
        self.assertIn("sampo: task unblock T-0002: T-0002 is deferred, not blocked", r.stderr)
        self.assertIn("2 of 3 unblocked.", r.stdout)
        self.assertEqual(self.status("T-0002", "T-0003", "T-0004"), ["deferred", "captured", "captured"])
        self.assertIn("unblocked: the API is back", c.find_brief(self.p, "T-0004").section("Log"))
        r = self.fm("task", "defer", "T-0006,T-0099", "--json", check=False)
        d = json.loads(r.stdout)
        self.assertEqual((r.returncode, d["ok"], [f["id"] for f in d["failed"]]), (1, ["T-0006"], ["T-0099"]))
        self.assertIn("no task T-0099", d["failed"][0]["error"])
        self.fm("task", "block", "T-0001", "a wall")
        self.assertIn("T-0001 unblocked: planned.", self.fm("task", "unblock", "T-0001").stdout)  # criteria: planned
        r = self.fm("task", "unblock", "T-0001", check=False)
        self.assertEqual((r.returncode, "T-0001 is planned, not blocked" in r.stderr), (2, True), r.stderr)
        for spec, said in (("T-0050..T-0060", "no task in that range"), ("T-0004..T-0002", "backwards")):
            r = self.fm("task", "drop", spec, "--reason", "x", check=False)
            self.assertEqual((r.returncode, said in r.stderr, "Traceback" in r.stderr), (1, True, False), r.stderr)
        r = self.fm("task", "drop", "T-1..T-" + "9" * 5000, "--reason", "x", check=False)  # never "state looks corrupt"
        self.assertEqual((r.returncode in (1, 2), "corrupt" in r.stderr, "Traceback" in r.stderr), (True, False, False))

    def test_an_unreadable_brief_fails_by_name_and_the_rest_still_run(self):
        # review: a brief that won't parse raised ValueError past the loop, so T-0004 was never tried
        with open(c.find_brief(self.p, "T-0003").path, "w") as f:
            f.write("not a brief\n")
        r = self.fm("task", "defer", "T-0002..T-0004", check=False)
        self.assertEqual((r.returncode, "Traceback" in r.stderr, "2 of 3 deferred." in r.stdout), (1, False, True),
                         r.stdout + r.stderr)
        self.assertIn("sampo: task defer T-0003: ", r.stderr)
        self.assertEqual(self.status("T-0002", "T-0004"), ["deferred"] * 2)

    def test_task_ids_dedupes_keeps_order_and_caps(self):
        self.assertEqual(cli.task_ids(self.p, "T-0003,T-2..3,T-0099"), ["T-0003", "T-0002", "T-0099"])
        with mock.patch.object(cli, "BULK_MAX", 2), self.assertRaises(cli.UsageError):
            cli.task_ids(self.p, "T-1..T-7")  # a typo'd range can't sweep the backlog


if __name__ == "__main__":
    unittest.main()
