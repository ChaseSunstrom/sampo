"""T-4191: three avoidable steers of one root, or one concern, in 7 days file one meta-task: why did the mechanism that
should have caught it not act. The mechanism comes from plugin/data/steer-mechanisms.json, its last firings from the
ledger; one task per group per week; no model is called. Rows are written as the meter books them (steers.jsonl)."""
import datetime
import json
import os
from unittest import mock

from helpers import ForemanTestCase

from sampo import concerns as cn
from sampo import core as c
from sampo import eco
from sampo import steerrecur as sr


def stamp(days=0.0, hours=0.0):
    t = c.clock() - datetime.timedelta(days=days, hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Recur(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.n = 0

    def book(self, root="R7", days=1.0, avoidable=True, concern=None, repeat_of=None, session="s1", kind="steer"):
        """One row as steermeter books it; each has its own words (the twin rule) and, unless named, its own concern."""
        self.n += 1
        r = {"session": session, "uuid": f"u{self.n}", "at": stamp(days=days, hours=self.n * 0.01), "model": "fake",
             "text": f"message number {self.n} about something", "kind": kind, "avoidable": avoidable,
             "root": root if avoidable else "none", "missed_thought": f"check the live page {self.n}", "why": "it said done",
             "concern": concern or f"concern {self.n}", "surface": "the page"}
        if repeat_of:
            r["repeat_of"] = repeat_of
        c.append_line(os.path.join(self.p.dir, "steers.jsonl"), json.dumps(r))
        return r

    def fire(self, kind, n=1, **data):
        for i in range(n):
            c.log_event(self.p, kind, task=f"T-{9000 + i}", data=dict(data, i=i))

    def tasks(self):
        return [b for b in c.load_briefs(self.p) if "steer-recur" in b.section("Raw request")]


class Root(Recur):
    def test_three_avoidable_steers_of_one_root_in_a_week_file_one_task_naming_the_mechanism_and_its_last_firings(self):
        for i in range(5):  # five firings: only the last three are shown
            c.log_event(self.p, "unproven_claim", task=f"T-90{i}", data={"kind": f"claim{i}"})
        for d in (1, 2, 3):
            self.book("R7", days=d)
        got = sr.tick(self.p)
        self.assertEqual([(x["group"], x["new"]) for x in got], [("R7", True)])
        (b,) = self.tasks()
        self.assertEqual(b.type, "FIX")
        self.assertIn("unproven-claim check", b.title)
        self.assertIn("runs is not done", b.title)
        raw = b.section("Raw request")
        for i in (2, 3, 4):
            self.assertIn(f"T-90{i}", raw)
        for i in (0, 1):
            self.assertNotIn(f"T-90{i}", raw)
        self.assertIn("never a new one", raw)
        self.assertIn("check the live page", raw)  # the missed thoughts of the three steers

    def test_two_steers_file_none_and_rows_older_than_a_week_do_not_count(self):
        self.book("R7", days=1)
        self.book("R7", days=2)
        self.assertEqual(sr.tick(self.p), [])
        self.book("R7", days=8)
        self.book("R7", days=9)
        self.assertEqual(sr.tick(self.p), [])
        self.assertEqual(self.tasks(), [])

    def test_steers_that_were_not_avoidable_or_fit_no_root_do_not_count(self):
        self.book("R7", days=1)
        self.book("R7", days=1, avoidable=False)
        self.book("new", days=1)
        self.book("new", days=1)
        self.book("new", days=1)
        self.assertEqual(sr.tick(self.p), [])

    def test_a_second_run_files_nothing_and_an_open_one_stands_in_for_the_next_week(self):
        for d in (1, 2, 3):
            self.book("R7", days=d)
        self.assertEqual(len(sr.tick(self.p)), 1)
        again = sr.tick(self.p)
        self.assertEqual([x["new"] for x in again], [False])
        self.assertEqual(len(self.tasks()), 1)

    def test_the_next_week_while_the_task_is_open_files_no_second_one_and_once_closed_it_files_again(self):
        wed, mon = "2026-11-18T12:00:00Z", "2026-11-23T12:00:00Z"  # ISO weeks 47 and 48
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": wed}):
            for d in (1, 2, 3):
                self.book("R7", days=d)
            self.assertEqual([x["new"] for x in sr.tick(self.p)], [True])
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": mon}):
            for d in (1, 2, 3):
                self.book("R7", days=d)
            self.assertEqual([x["new"] for x in sr.tick(self.p)], [False])
            self.assertEqual(len(self.tasks()), 1)
            (b,) = self.tasks()
            self.fm("task", "drop", b.id, "test", "--force")
            self.assertEqual([x["new"] for x in sr.tick(self.p)], [True])
        self.assertEqual(len(self.tasks()), 2)

    def test_two_roots_each_with_three_file_two_tasks(self):
        for d in (1, 2, 3):
            self.book("R7", days=d)
            self.book("R4", days=d)
        self.assertEqual(sorted(x["group"] for x in sr.tick(self.p)), ["R4", "R7"])
        self.assertEqual(len(self.tasks()), 2)

    def test_a_mechanism_that_never_fired_says_so(self):
        for d in (1, 2, 3):
            self.book("R5", days=d)
        sr.tick(self.p)
        (b,) = self.tasks()
        self.assertIn("no firing in the last", b.section("Raw request"))

    def test_a_root_the_table_does_not_know_is_left_alone(self):
        for d in (1, 2, 3):
            self.book("R99", days=d)
        self.assertEqual(sr.tick(self.p), [])

    def test_the_words_of_a_firing_are_redacted(self):
        token = "ghp_" + "a1B2c3D4" * 4
        c.log_event(self.p, "unproven_claim", task="T-901", data={"kind": f"used {token}"})
        for d in (1, 2, 3):
            self.book("R7", days=d)
        sr.tick(self.p)
        (b,) = self.tasks()
        self.assertNotIn(token, b.section("Raw request"))

    def test_steers_already_filed_for_are_not_filed_again_in_the_next_week(self):
        wed, mon = "2026-11-18T12:00:00Z", "2026-11-23T12:00:00Z"  # ISO weeks 47 and 48
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": wed}):
            for d in (1, 2, 3):
                self.book("R7", days=d)
            self.assertEqual([x["new"] for x in sr.tick(self.p)], [True])
            (b,) = self.tasks()
            self.fm("task", "drop", b.id, "test", "--force")
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": mon}):
            self.assertEqual(sr.tick(self.p), [])  # the same three steers are still in the 7 days
            self.assertEqual(len(self.tasks()), 1)

    def test_a_muted_discovered_trigger_does_not_stop_it_and_a_muted_own_trigger_files_no_false_task(self):
        routes = os.path.join(self.p.dir, "trigger-routes.json")
        with open(routes, "w") as f:
            json.dump({"discovered": "muted"}, f)
        eco.capture_once(self.p, "an unrelated finding", "FIX", "discovered", key="other-key")
        for d in (1, 2, 3):
            self.book("R7", days=d)
        self.assertEqual([(x["group"], x["new"]) for x in sr.tick(self.p)], [("R7", True)])  # its own trigger
        (b,) = self.tasks()
        self.assertEqual(b.meta.get("trigger"), "steer-recur")
        with open(routes, "w") as f:
            json.dump({"steer-recur": "muted"}, f)
        self.fm("task", "drop", b.id, "test", "--force")
        for d in (1, 2, 3):
            self.book("R2", days=d)
        got = sr.tick(self.p)  # R2's filing is joined onto R7's task by the mute: no task is named for R2
        self.assertEqual([x for x in got if x["group"] == "R2"], [])
        self.assertEqual(len(self.tasks()), 1)


class Concern(Recur):
    def test_one_concern_said_three_times_in_a_week_files_one_task_even_under_three_roots(self):
        first = self.book("R1", days=3, concern="the page must work")
        kid = cn.concern_id(first["session"], first["uuid"])
        self.book("R2", days=2, concern="the page must work", repeat_of=kid)
        self.book("R3", days=1, concern="the page must work", repeat_of=kid)
        got = sr.tick(self.p)
        self.assertEqual([(x["group"], x["new"]) for x in got], [(kid, True)])
        (b,) = self.tasks()
        self.assertIn("concerns repeat pass", b.title)
        self.assertIn(kid, b.section("Raw request"))
        self.assertEqual([x["new"] for x in sr.tick(self.p)], [False])
        self.assertEqual(len(self.tasks()), 1)

    def test_the_meta_task_is_not_one_of_the_concerns_own_tasks(self):
        first = self.book("R1", days=3, concern="the page must work")
        kid = cn.concern_id(first["session"], first["uuid"])
        self.book("R2", days=2, concern="the page must work", repeat_of=kid)
        self.book("R3", days=1, concern="the page must work", repeat_of=kid)
        sr.tick(self.p)
        (b,) = self.tasks()
        self.assertEqual(cn._briefs_naming(self.p, [b]), {})

    def test_two_said_in_the_week_and_one_older_is_not_three(self):
        first = self.book("R1", days=20, concern="the page must work")
        kid = cn.concern_id(first["session"], first["uuid"])
        self.book("R2", days=2, concern="the page must work", repeat_of=kid)
        self.book("R3", days=1, concern="the page must work", repeat_of=kid)
        self.assertEqual(sr.tick(self.p), [])

    def test_a_root_and_its_concern_that_both_recur_file_both(self):
        first = self.book("R7", days=3, concern="done means works")
        kid = cn.concern_id(first["session"], first["uuid"])
        self.book("R7", days=2, concern="done means works", repeat_of=kid)
        self.book("R7", days=1, concern="done means works", repeat_of=kid)
        self.assertEqual(sorted(x["group"] for x in sr.tick(self.p)), sorted([kid, "R7"]))


class Safe(Recur):
    def test_a_failure_is_logged_and_returns_nothing(self):
        with mock.patch.object(sr, "tick", side_effect=RuntimeError("boom")):
            self.assertEqual(sr.safe_tick(self.p), [])
