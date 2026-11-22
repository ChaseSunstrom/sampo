"""T-5179 (child k3 of T-4338, Moons at scale): moonground.ground grades every kept moon new, partial or built against the
done tasks, the open tasks, fm's verbs and the modules' map lines, appending to moons/grounding.jsonl; model-free, one
brief load per run, a moon regraded only when its text or the newest closed task changed, never waiting on the store."""
# map-links: plugin/lib/sampo/moonground.py plugin/lib/sampo/moonscale.py plugin/lib/sampo/recall.py
import fcntl
import json
import os
from unittest import mock

from helpers import ForemanTestCase

BUILT = "Roll back a bad deploy before users notice"
VERB = "Rerun a failing test to see if it is flaky"
OPEN = "Answer every waiting review within the hour"
MODULE = "Mutate parser input to surface crashes before release"
NEW = "Translate every commit message into a haiku"
REJECTED = "Roll back a bad deploy before users notice again"

HELP = {"task prove": (VERB, "task prove " + VERB), "lane list": ("List the lanes", "lane list List the lanes")}
MAP = ("# Map\n\n## File map\n\n- `fmfuzz` (`sampo fuzz`: mutate a parser's input to surface crashes before release, "
       "one corpus per format; T-0099)\n- `fmother` (a shim for something about colours)\n")


def moon(title):
    return {"title": title, "today": "Today you can't tell what broke in your code without a morning of reading.",
            "with_this": "With this, you can ship your release knowing which failures in your project are noise.",
            "what": "It reads each new failure and groups the ones that share a cause.",
            "kill": "Run it on 20 known failures in one repo for a day; if it groups fewer than 15, it is dead.",
            "prior_art": "dashboards", "tier": "high"}


class _Ground(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        from sampo import cli, moonground, moonscale, recall
        from sampo import core as c
        self.c, self.cli, self.mg, self.ms, self.recall = c, cli, moonground, moonscale, recall
        self.p = c.find_project(self.repo)
        with open(os.path.join(self.repo, "MASTER.md"), "w", encoding="utf-8") as f:
            f.write("# Master\n")
        os.makedirs(os.path.join(self.repo, "docs", "map"), exist_ok=True)
        with open(os.path.join(self.repo, "docs", "map", "files-1.md"), "w", encoding="utf-8") as f:
            f.write(MAP)
        with c.lock(self.p.dir):
            self.cli._create(self.p, BUILT + " automatically", "FEATURE", "S", "done", raw=BUILT)
            self.cli._create(self.p, "Add an export button", "FEATURE", "S", "done", raw="Add an export button")
            self.cli._create(self.p, "Answer a waiting review within the hour", "FEATURE", "S", "planned", raw=OPEN)
        self.docs = mock.patch.object(recall_(), "verb_docs", return_value=HELP)
        self.docs.start()
        self.addCleanup(self.docs.stop)

    def seed(self, *titles):
        cell = {"problem": "slow tests", "source": "pain", "kind": "bugs", "mechanism": "biology", "scale": "file",
                "surface": "terminal"}
        ids = [i for i, _, _ in self.ms.add(self.p, [moon(t) for t in titles], cell, "haiku", 1)]
        lines = [json.dumps({"id": i, "at": "2026-11-03T00:00:00Z", "stage": "novelty", "verdict": "kept", "reason": "",
                             "model": "jarvis", "tier": "high", "wild": 3}) for i, t in zip(ids, titles)
                 if t != REJECTED]
        self.c.append_line(os.path.join(self.ms._dir(self.p), self.ms.FUNNEL), "\n".join(lines))
        return ids

    def lines(self):
        try:
            with open(os.path.join(self.ms._dir(self.p), "grounding.jsonl"), encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []


def recall_():
    from sampo import recall
    return recall


class Grade(_Ground):
    def test_each_kept_moon_is_graded_against_tasks_verbs_and_modules(self):
        ids = self.seed(BUILT, VERB, OPEN, MODULE, NEW)
        r = self.mg.ground(self.p)
        st = self.mg.state(self.p)
        self.assertEqual([st[i]["state"] for i in ids], ["built", "built", "partial", "partial", "new"])
        self.assertIn("T-0001", st[ids[0]]["evidence"], "a done task a moon restates is named")
        self.assertIn("task prove", st[ids[1]]["evidence"], "a verb whose help a moon restates is named")
        self.assertIn("T-0003", st[ids[2]]["evidence"], "an open task near it is named")
        self.assertIn("fmfuzz", st[ids[3]]["evidence"], "a module's map line near it is named")
        self.assertEqual(st[ids[4]]["evidence"], "")
        for i in ids:
            self.assertEqual(sorted(k for k in ("id", "state", "evidence", "digest", "at") if k in st[i]),
                             ["at", "digest", "evidence", "id", "state"])
        self.assertEqual((r["kept"], r["grounded"], r["new"], r["partial"], r["built"]), (5, 5, 1, 2, 2))

    def test_a_moon_that_is_not_kept_is_not_graded(self):
        ids = self.seed(BUILT, REJECTED)
        self.ms.add(self.p, [moon(NEW)], {"kind": "bugs"}, "haiku", 1)  # written, never funnelled: not kept
        self.mg.ground(self.p)
        self.assertEqual(sorted(self.mg.state(self.p)), [ids[0]])

    def test_no_moons_grade_nothing_and_still_log(self):
        r = self.mg.ground(self.p)
        self.assertEqual((r["kept"], r["grounded"]), (0, 0))
        self.assertEqual(self.mg.state(self.p), {})


class Cost(_Ground):
    def test_briefs_load_once_per_run_not_once_per_moon(self):
        self.seed(BUILT, VERB, OPEN, MODULE, NEW)
        with mock.patch.object(self.c, "load_briefs", wraps=self.c.load_briefs) as load:
            self.mg.ground(self.p)
        self.assertEqual(load.call_count, 1)

    def test_a_moon_is_regraded_only_when_its_text_or_the_newest_closed_task_changed(self):
        ids = self.seed(BUILT, NEW)
        self.mg.ground(self.p)
        n = len(self.lines())
        r = self.mg.ground(self.p)
        self.assertEqual((r["grounded"], len(self.lines())), (0, n), "nothing changed: nothing written")
        path = os.path.join(self.ms._dir(self.p), self.ms.CANDIDATES)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(NEW, NEW + " with pictures"))
        r = self.mg.ground(self.p)
        self.assertEqual((r["grounded"], len(self.lines())), (1, n + 1), "its text changed: it alone is graded again")
        self.assertEqual(self.lines()[-1]["id"], ids[1])
        with self.c.lock(self.p.dir):
            self.cli._create(self.p, "Add a dark theme", "FEATURE", "S", "done", raw="Add a dark theme")
        r = self.mg.ground(self.p)
        self.assertEqual(r["grounded"], 2, "a task closed since: every kept moon is graded again")

    def test_a_run_completes_while_another_process_holds_the_store_lock(self):
        self.seed(BUILT, NEW)
        os.makedirs(self.ms._dir(self.p), exist_ok=True)
        fd = os.open(os.path.join(self.ms._dir(self.p), ".funnel.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(self.ms.Busy):
                with self.ms.store_lock(self.p):
                    pass
            with mock.patch.object(self.ms, "store_lock", side_effect=AssertionError("ground took the lock")):
                r = self.mg.ground(self.p)
            self.assertEqual(r["grounded"], 2)
        finally:
            os.close(fd)

    def test_the_counts_are_logged_and_no_model_is_called(self):
        from sampo import models
        self.seed(BUILT, VERB, OPEN, MODULE, NEW)
        boom = AssertionError("a model was called")
        with mock.patch.object(self.ms, "_ask", side_effect=boom), mock.patch.object(models, "embed", side_effect=boom), \
                mock.patch.object(self.ms, "_local", side_effect=boom), \
                mock.patch("urllib.request.urlopen", side_effect=boom):
            self.mg.ground(self.p)
        ev = [e for e in self.c.ledger_events(self.p) if e.get("event") == "moons_ground"]
        self.assertEqual(len(ev), 1)
        self.assertEqual({k: ev[0]["data"][k] for k in ("kept", "grounded", "new", "partial", "built")},
                         {"kept": 5, "grounded": 5, "new": 1, "partial": 2, "built": 2})

    def test_a_torn_or_foreign_line_in_the_grounding_file_is_skipped(self):
        ids = self.seed(NEW)
        os.makedirs(self.ms._dir(self.p), exist_ok=True)
        self.c.append_line(os.path.join(self.ms._dir(self.p), "grounding.jsonl"), '{"id": "C-000001", "state"')
        self.c.append_line(os.path.join(self.ms._dir(self.p), "grounding.jsonl"), '[1, 2]')
        self.assertEqual(self.mg.state(self.p), {})
        self.assertEqual(self.mg.ground(self.p)["grounded"], 1)
        self.assertEqual(self.mg.state(self.p)[ids[0]]["state"], "new")
