"""T-5227: a blocked task is freed within minutes of its last dependency closing. The review records each kept task's open
waits; due() then reads the ledger since the review and says so at once when a waited-on task closes or a task is
blocked, instead of waiting out the hour. An unchanged block keeps the hourly cadence and asks no model again. The
model is replaced by a stub here: blocked._ask is the one place a model is asked."""
import os
from unittest import mock

from helpers import ForemanTestCase

from sampo import blocked
from sampo import core as c
from sampo import night

T0 = "2026-11-01T00:00:00Z"  # when the blocks are written
REVIEW = "2026-11-01T02:00:00Z"  # the review that records the waits
SOON = "2026-11-01T02:20:00Z"  # 20 minutes on: well inside the hour


class Stub:
    """blocked._ask's stand-in: answers a block's reason with `holds`, and counts the questions."""

    def __init__(self, holds):
        self.holds, self.asked = holds, []

    def __call__(self, p, kind, prompt, choices, **kw):
        self.asked.append(kind)
        if kind != "block-holds":
            return None
        return {"choice": self.holds, "probability": 0.9, "cleared": True}


class BlockedSoon(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        for t in ("Build the parser", "Wire the parser in", "Ship the docs", "Ask about the key"):
            self.fm("task", "new", t, "--type", "FEATURE", "--tier", "S", "--ac", "works :: true", "--step", "do it")
        self.p = c.find_project(self.repo)
        self.fm("task", "set", "T-0002", "depends_on+=T-0001")
        self.at(T0, "task", "block", "T-0002", "")  # held only by T-0001, which is open

    def at(self, now, *args):
        return self.fm(*args, env={"FOREMAN_NOW": now})

    def review(self, now, stub=None):
        with mock.patch.object(blocked, "_ask", stub or Stub("tasks")), mock.patch.dict(os.environ, {"FOREMAN_NOW": now}):
            return blocked.review(self.p)

    def due(self, now):
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": now}):
            return blocked.due(self.p)

    def status(self, tid):
        return c.find_brief(self.p, tid).status

    def test_the_review_records_each_kept_tasks_open_waits(self):
        self.review(REVIEW)
        self.assertEqual(blocked.load(self.p)["waits"], {"T-0002": ["T-0001"]})

    def test_closing_the_waited_task_makes_the_review_due_at_once(self):
        self.review(REVIEW)
        self.assertFalse(self.due(SOON))  # nothing has happened: the hourly cadence stands
        self.at("2026-11-01T02:21:00Z", "task", "drop", "T-0001", "done elsewhere")
        self.assertTrue(self.due("2026-11-01T02:22:00Z"))  # the waited-on task closed: due now, not at 03:00
        res = self.review("2026-11-01T02:22:00Z")
        self.assertEqual([x["id"] for x in res["freed"]], ["T-0002"])
        self.assertEqual(self.status("T-0002"), "planned")

    def test_the_sweep_tick_frees_the_task_as_soon_as_its_wait_closes(self):
        self.review(REVIEW)
        self.at("2026-11-01T02:21:00Z", "task", "drop", "T-0001", "done elsewhere")
        with mock.patch.object(blocked, "_ask", Stub("tasks")), \
                mock.patch.dict(os.environ, {"FOREMAN_NOW": "2026-11-01T02:22:00Z"}):
            res = night.tick(self.p, fetch_ci=False)
        self.assertEqual(res.get("unblocked"), ["T-0002"])
        self.assertEqual(self.status("T-0002"), "planned")

    def test_closing_an_unrelated_task_keeps_the_hourly_cadence(self):
        self.review(REVIEW)
        self.at(SOON, "task", "drop", "T-0004", "not needed")
        self.assertFalse(self.due("2026-11-01T02:21:00Z"))

    def test_a_new_block_makes_the_review_due_at_once(self):
        self.review(REVIEW)
        self.at(SOON, "task", "block", "T-0003", "waits for the user's API key")
        self.assertTrue(self.due("2026-11-01T02:21:00Z"))

    def test_a_task_done_in_another_task_counts_as_a_close(self):
        self.review(REVIEW)
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": SOON}):  # sampo task drop --done-in writes this event
            c.log_event(self.p, "task_done_in", task="T-0001", data={"host": "T-0003", "reason": ""})
        self.assertTrue(self.due("2026-11-01T02:21:00Z"))

    def test_an_unchanged_block_asks_no_model_again(self):
        self.at(T0, "task", "block", "T-0003", "waits for T-0001 to land")
        first = Stub("tasks")
        self.review(REVIEW, first)
        self.assertEqual(first.asked, ["block-holds"])  # the reason is read once, and kept
        self.assertFalse(self.due("2026-11-01T02:30:00Z"))
        self.at("2026-11-01T02:31:00Z", "task", "drop", "T-0001", "done elsewhere")
        second = Stub("tasks")
        res = self.review("2026-11-01T02:32:00Z", second)
        self.assertEqual(sorted(x["id"] for x in res["freed"]), ["T-0002", "T-0003"])
        self.assertEqual(second.asked, [])  # the cached verdict frees it: no model asked again
