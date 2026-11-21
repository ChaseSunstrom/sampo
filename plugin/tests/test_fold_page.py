"""T-5020 (slice of T-4325): sampo fold set records one verdict per task in fold.json, sampo fold page lists every
captured and planned task with its verdict (or 'unfolded'), sampo fold --json counts them and the standalone ones
(stays plus unfolded). Run one group with -k: set, page, counts, check."""
import json
import os
import unittest

from helpers import ForemanTestCase

from sampo import core as c


class FoldBase(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def new(self, title, status="captured"):
        """A task in the inbox (captured) or the queue (planned)."""
        if status == "captured":
            self.fm("capture", title, "--type", "FEATURE", "--tier", "M", "--anyway")
            return max((b.id for b in c.load_briefs(self.p) if b.title == title))
        tid = json.loads(self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        return tid

    def ledger(self):
        with open(os.path.join(self.p.dir, "fold.json"), encoding="utf-8") as f:
            return json.load(f)

    def counts(self, *args):
        return self.fm_json("fold", *args)


class FoldSet(FoldBase):
    def test_set_records_row_in_pinned_schema(self):
        a, b = self.new("Alpha thing"), self.new("Beta thing")
        self.fm("fold", "set", a, "joined", "--to", b, "--why", "same work")
        row = self.ledger()["rows"][a]
        self.assertEqual(row, {"verdict": "joined", "to": b, "evidence": "", "why": "same work", "checked": None})

    def test_set_is_case_tolerant_and_replaces_the_old_row(self):
        a, b = self.new("Alpha thing"), self.new("Beta thing")
        self.fm("fold", "set", a.lower(), "STAYS", "--why", "its own work")
        self.assertEqual(self.ledger()["rows"][a]["verdict"], "stays")
        self.fm("fold", "set", a, "merged", "--to", b)
        rows = self.ledger()["rows"]
        self.assertEqual(list(rows), [a])
        self.assertEqual((rows[a]["verdict"], rows[a]["to"], rows[a]["why"]), ("merged", b, ""))

    def test_a_new_verdict_clears_an_earlier_check(self):
        a = self.new("Alpha thing")
        self.fm("fold", "set", a, "built", "--evidence", "plugin/lib/sampo/core.py:12")
        path = os.path.join(self.p.dir, "fold.json")
        d = self.ledger()
        d["rows"][a]["checked"] = "ok"  # what T-5022's fold check writes
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.fm("fold", "set", a, "built", "--evidence", "plugin/lib/sampo/core.py:99")
        self.assertIsNone(self.ledger()["rows"][a]["checked"])

    def test_set_refuses_bad_input_and_writes_nothing(self):
        a = self.new("Alpha thing")
        bad = [("fold", "set", a, "maybe"),                              # not a verdict
               ("fold", "set", "T-9999", "stays"),                       # no such task
               ("fold", "set", "not-an-id", "stays"),
               ("fold", "set", a, "joined"),                             # joined needs --to
               ("fold", "set", a, "merged", "--to", a),                  # into itself
               ("fold", "set", a, "joined", "--to", "T-9999"),           # into nothing
               ("fold", "set", a, "built"),                              # built needs evidence
               ("fold", "set", a, "built", "--evidence", "no line here"),
               ("fold", "set", a, "stays", "--to", a),                   # --to only for joined or merged
               ("fold", "set", a),
               ("fold", "set")]
        for argv in bad:
            r = self.fm(*argv, check=False)
            self.assertNotEqual(r.returncode, 0, argv)
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, "fold.json")))

    def test_why_is_one_capped_line(self):
        a = self.new("Alpha thing")
        self.fm("fold", "set", a, "dropped", "--why", "line one\nline two " + "x" * 600)
        why = self.ledger()["rows"][a]["why"]
        self.assertNotIn("\n", why)
        self.assertLessEqual(len(why), 300)

    def test_a_corrupt_ledger_is_refused_not_overwritten(self):
        a = self.new("Alpha thing")
        path = os.path.join(self.p.dir, "fold.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        r = self.fm("fold", "set", a, "stays", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fold.json", r.stderr)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")


class FoldPage(FoldBase):
    def test_page_lists_every_captured_and_planned_task_with_its_verdict(self):
        inbox1, inbox2 = self.new("Inbox one"), self.new("Inbox two")
        queued = self.new("Queued one", "planned")
        self.fm("fold", "set", inbox1, "joined", "--to", queued, "--why", "same")
        self.fm("fold", "set", queued, "stays")
        page = self.fm("fold", "page").stdout
        for tid, title in ((inbox1, "Inbox one"), (inbox2, "Inbox two"), (queued, "Queued one")):
            line = next(x for x in page.splitlines() if tid in x and title in x)
            self.assertTrue(line.startswith("- "), line)
        line = lambda tid: next(x for x in page.splitlines() if x.startswith(f"- {tid} "))
        self.assertIn("joined", line(inbox1))
        self.assertIn(queued, line(inbox1))
        self.assertIn("same", line(inbox1))
        self.assertIn("stays", line(queued))
        self.assertIn("unfolded", line(inbox2))
        self.assertLess(page.index("Inbox"), page.index("Queue"))

    def test_page_of_an_empty_project_still_prints(self):
        r = self.fm("fold", "page")
        self.assertIn("0 open", r.stdout)

    def test_a_verdict_on_a_task_no_longer_open_is_kept_and_named(self):
        a, b = self.new("Alpha thing"), self.new("Beta thing")
        self.fm("fold", "set", a, "dropped", "--why", "not wanted")
        self.fm("task", "drop", a, "not wanted", "--mistake")
        page = self.fm("fold", "page").stdout
        self.assertIn("no longer open", page)
        self.assertIn(a, page.split("no longer open", 1)[1])
        self.assertIn(a, self.ledger()["rows"])
        self.assertNotIn(a, page.split("no longer open", 1)[0])
        self.assertIn(b, page)


class FoldCounts(FoldBase):
    def test_json_counts_verdicts_and_the_standalone(self):
        ids = [self.new(f"Task number {n}") for n in range(5)]
        queued = self.new("Queued one", "planned")
        self.fm("fold", "set", ids[0], "stays")
        self.fm("fold", "set", ids[1], "joined", "--to", queued)
        self.fm("fold", "set", ids[2], "merged", "--to", queued)
        self.fm("fold", "set", ids[3], "dropped")
        self.fm("fold", "set", queued, "built", "--evidence", "plugin/lib/sampo/core.py:1")
        d = self.counts("--json")
        self.assertEqual(d["open"], 6)
        self.assertEqual(d["captured"], 5)
        self.assertEqual(d["planned"], 1)
        self.assertEqual(d["verdicts"], {"joined": 1, "merged": 1, "dropped": 1, "built": 1, "stays": 1, "unfolded": 1})
        self.assertEqual(d["standalone"], 2)  # the one that stays plus the one nobody ruled on
        self.assertEqual(d["gone"], 0)
        self.assertEqual({t["id"]: t["verdict"] for t in d["tasks"]}[ids[4]], "unfolded")

    def test_fold_without_an_action_prints_the_counts(self):
        a = self.new("Alpha thing")
        text = self.fm("fold").stdout
        self.assertIn("1 open", text)
        self.assertIn("standalone 1", text)
        self.fm("fold", "set", a, "dropped")
        self.assertIn("standalone 0", self.fm("fold").stdout)

    def test_page_json_carries_the_same_counts(self):
        self.new("Alpha thing")
        self.assertEqual(self.counts("page", "--json")["standalone"], 1)

    def test_every_ledger_row_counts_once_whatever_its_shape(self):
        a = self.new("Alpha thing")
        with open(os.path.join(self.p.dir, "fold.json"), "w", encoding="utf-8") as f:
            json.dump({"rows": {a: "junk", "T-4444": {"verdict": "stays"}}}, f)
        d = self.counts("--json")
        self.assertEqual(d["verdicts"]["unfolded"], 1)  # a malformed row is no verdict
        self.assertEqual(d["gone"], 1)


class FoldIsolation(FoldBase):
    def test_a_verdict_in_one_project_is_not_another_projects(self):
        from helpers import git_repo
        repo_b = git_repo(self.tmp, name="app-b")
        self.fm("init", cwd=repo_b)
        a = self.new("Alpha thing")
        self.fm("fold", "set", a, "stays", "--why", "only in A")
        self.fm("capture", "Other thing", "--type", "FEATURE", "--tier", "M", "--anyway", cwd=repo_b)
        d = json.loads(self.fm("fold", "page", "--json", cwd=repo_b).stdout)
        self.assertEqual(d["gone"], 0)
        self.assertEqual(d["verdicts"]["unfolded"], 1)
        self.assertEqual(d["tasks"][0]["verdict"], "unfolded")
        page = self.fm("fold", "page", cwd=repo_b).stdout
        self.assertNotIn("only in A", page)
        self.assertNotIn("no longer open", page)


class FoldControlText(FoldBase):
    def test_control_characters_never_reach_the_ledger_or_page(self):
        a, b = self.new("Alpha thing"), self.new("Beta thing")
        self.fm("fold", "set", a, "dropped", "--why", "x\x1b]0;t\x07y")
        self.assertNotIn("\x1b", json.dumps(self.ledger()))
        self.assertNotIn("\x1b", self.fm("fold", "page").stdout)
        # a ledger written by hand (or an older run) is cleaned on the way out too
        d = self.ledger()
        d["rows"][a].update(why="w\x1b[2Jz", to="T-1\x1b[2J", evidence="e\x1b[2J:1")
        d["rows"][b] = {"verdict": "stays", "why": "q\x1b]0;t\x07", "to": "", "evidence": "", "checked": None}
        d["rows"]["T-9999"] = {"verdict": "joined", "to": "T-1\x1b[2J", "why": "g\x1b[2J"}
        with open(os.path.join(self.p.dir, "fold.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.assertNotIn("\x1b", self.fm("fold", "page").stdout)


class FoldCheck(FoldBase):
    def test_check_without_its_module_says_not_built(self):
        if os.path.exists(os.path.join(os.path.dirname(c.__file__), "foldcheck.py")):
            self.skipTest("T-5022's foldcheck is built")
        r = self.fm("fold", "check", check=False)
        self.assertIn("not built", r.stdout + r.stderr)

    def test_unknown_action_is_refused(self):
        self.assertNotEqual(self.fm("fold", "frobnicate", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
