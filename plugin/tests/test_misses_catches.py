"""T-5712: `sampo misses` counts every avoidable steer steermeter booked (a steer note() already logged counts once); a steer
whose words match a task Sampo filed 10+ minutes earlier is a catch; a steer whose words match only an earlier self_tell
is told-not-acted; the summary prints both and catches per week. The clock is pinned (FOREMAN_NOW), so every window and
every filing time is exact: minute 0 is T0, a negative minute is before it."""
import datetime
import json
import os
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c

T0 = datetime.datetime(2026, 11, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)
STALLED = "why is the graph look or whatever on the local live site not done yet"


def at(minute):
    return (T0 + datetime.timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo, create=False)
        self.enterContext(mock.patch.dict(os.environ, {"FOREMAN_NOW": at(0)}))

    def pin(self, minute):
        return mock.patch.dict(os.environ, {"FOREMAN_NOW": at(minute)})

    def steer(self, sid, uuid, text, minute, root="R3", avoidable=True, unread=False):
        """A row as steermeter books it (steers.jsonl, one line per message read)."""
        r = {"session": sid, "uuid": uuid, "at": at(minute), "model": "fake", "text": text, "kind": "steer",
             "avoidable": avoidable, "root": root, "missed_thought": "check it", "why": "it was not checked",
             "concern": "the page", "surface": "the push"}
        if unread:
            r["unread"] = True
        c.append_line(os.path.join(self.p.dir, "steers.jsonl"), json.dumps(r, ensure_ascii=False))

    def file(self, text, source, minute, type_="FEATURE"):
        """A task Sampo filed (capture_once stamps its created time with the pinned clock)."""
        from sampo import eco
        with self.pin(minute):
            b, _new = eco.capture_once(self.p, text, type_, source)
        return b

    def tell(self, check, text, minute):
        with self.pin(minute):
            c.log_event(self.p, "self_tell", data={"check": check, "text": text})

    def note(self, text, sid, minute):
        from sampo import selfaware
        with self.pin(minute):
            return selfaware.note(self.p, text, session=sid)

    def done(self, b):
        """A task that reached done: catches_per_week counts only the catches whose task is done (T-5712 review)."""
        b.meta["status"] = "done"
        c.save_brief(self.p, b)


class Union(Base):
    def test_every_avoidable_steer_counts_and_a_steer_note_already_logged_counts_once(self):
        from sampo import selfaware
        self.assertEqual(self.note(STALLED, "s1", -30), "stalled")
        self.steer("s1", "u1", STALLED, -30, root="R2")  # the same message, booked by the tick
        self.steer("s2", "u2", "the push said done but the page is still empty", -20, root="R5")  # no pattern: steer only
        self.steer("s2", "u3", "thanks", -15, root="none", avoidable=False)  # not avoidable: never a miss
        self.steer("s2", "u4", "the login page is broken again", -10, root="R7", unread=True)  # not read yet: not counted
        r = selfaware.report(self.p)
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["by_class"], {"stalled": 1, "R5": 1})
        self.assertEqual(r["by_root"], {"R2": 1, "R5": 1})
        self.assertEqual(r["by_day"], {"2026-11-20": 2})
        self.assertEqual(r["steers_read"], 3)

    def test_misses_prints_the_union_by_day_and_the_root_of_each_steer(self):
        self.note(STALLED, "s1", -30)
        self.steer("s2", "u2", "the push said done but the page is still empty", -20, root="R5")
        self.steer("s3", "u3", "the export button does nothing", -2880, root="R2")  # two days earlier
        out = self.fm("misses").stdout
        self.assertIn("3 misses", out)
        self.assertIn("2026-11-18  1", out)
        data = json.loads(self.fm("misses", "--json").stdout)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["by_day"], {"2026-11-18": 1, "2026-11-20": 2})
        self.assertEqual(data["by_root"], {"unrooted": 1, "R5": 1, "R2": 1})
        self.assertEqual(data["steers_read"], 2)


    def test_a_long_message_is_one_miss_when_the_note_and_the_steer_row_keep_different_lengths(self):
        """A miss keeps 300 characters and a steer row 160 (with an ellipsis): one message of 400+ characters is one miss,
        even when the steer row is booked minutes after the note (the 60 s rule cannot pair them)."""
        from sampo import selfaware, steermeter
        text = STALLED + " " + "the page still shows nothing on the local live site " * 8
        self.assertGreater(len(text), 400)
        self.assertEqual(self.note(text, "s1", -30), "stalled")
        self.steer("s1", "u1", steermeter._text160(text), -25, root="R2")
        r = selfaware.report(self.p)
        self.assertEqual((r["total"], r["by_class"], r["by_root"], r["steers_read"]), (1, {"stalled": 1}, {"R2": 1}, 1))

    def test_two_messages_a_moment_apart_with_no_shared_word_are_two_misses(self):
        """The 60 s rule pairs a miss and a steer only with a shared word: two unrelated messages of one session are two."""
        from sampo import selfaware
        self.note(STALLED, "s1", -30)
        self.steer("s1", "u1", "the export button does nothing", -30, root="R5")
        self.assertEqual(selfaware.report(self.p)["total"], 2)


class Catch(Base):
    def test_a_task_filed_before_the_window_is_not_a_catch(self):
        """A task filed 50 days back is outside the 30-day window: a steer matching it is not a catch."""
        from sampo import selfaware
        self.file("dashboard chart for merge queue wait times", "discovered", -50 * 1440)
        self.steer("s5", "u7", "add a dashboard chart of the merge queue wait times please", -5)
        self.assertEqual(selfaware.report(self.p)["catches"], 0)

    def test_catches_per_week_counts_only_the_catches_whose_task_is_done(self):
        from sampo import selfaware
        b = self.file("dashboard chart for merge queue wait times", "discovered", -20)
        self.steer("s5", "u7", "add a dashboard chart of the merge queue wait times please", -5)
        r = selfaware.report(self.p)
        self.assertEqual((r["catches"], r["catches_done"], r["catches_per_week"]), (1, 0, 0.0))
        self.done(b)
        r = selfaware.report(self.p)
        self.assertEqual((r["catches"], r["catches_done"], r["catches_per_week"]), (1, 1, 0.2))

    def test_a_steer_matching_a_task_filed_ten_minutes_or_more_earlier_is_a_catch(self):
        from sampo import selfaware
        self.file("dashboard chart for merge queue wait times", "discovered", -20)
        self.steer("s5", "u7", "add a dashboard chart of the merge queue wait times please", -5, root="R1")
        r = selfaware.report(self.p)
        self.assertEqual((r["total"], r["catches"], r["told_not_acted"]), (1, 1, 0))

    def test_a_steer_exactly_ten_minutes_after_the_filing_is_a_catch(self):
        from sampo import selfaware
        self.file("dashboard chart for merge queue wait times", "predicted", -15)
        self.steer("s5", "u7", "add a dashboard chart of the merge queue wait times please", -5)
        self.assertEqual(selfaware.report(self.p)["catches"], 1)

    def test_a_steer_under_ten_minutes_after_the_filing_is_not_a_catch(self):
        from sampo import selfaware
        self.file("dashboard chart for merge queue wait times", "discovered", -5)
        self.steer("s5", "u7", "add a dashboard chart of the merge queue wait times please", -2)
        r = selfaware.report(self.p)
        self.assertEqual((r["total"], r["catches"]), (1, 0))

    def test_only_a_task_sampo_filed_is_a_catch_predicted_discovered_or_self(self):
        from sampo import selfaware
        self.file("dashboard chart for merge queue wait times", "predicted", -60)
        self.file("retry flaky deploy script", "self", -60)
        self.file("remove stale lanes folder", "user", -60)
        self.steer("s6", "u8", "add a dashboard chart of the merge queue wait times", -5)
        self.steer("s6", "u9", "please retry the flaky deploy script", -4)
        self.steer("s6", "u10", "remove the stale lanes folder", -3)
        r = selfaware.report(self.p)
        self.assertEqual((r["total"], r["catches"]), (3, 2))

    def test_a_steer_sharing_one_word_with_a_task_is_not_a_catch(self):
        from sampo import selfaware
        self.file("retry flaky deploy script", "self", -60)
        self.steer("s6", "u8", "do the retry work", -5)
        self.assertEqual(selfaware.report(self.p)["catches"], 0)

    def test_a_predicted_catch_and_a_steer_of_the_same_words_are_one_catch(self):
        from sampo import selfaware
        b = self.file("dashboard chart for merge queue wait times", "predicted", -60)
        text = "add a dashboard chart of the merge queue wait times please"
        with self.pin(-5):
            c.log_event(self.p, "predicted_catch", task=b.id, data={"text": text})
        self.steer("s6", "u8", text, -5)
        self.assertEqual(selfaware.report(self.p)["catches"], 1)

    def test_catches_per_week_is_the_window_rate_and_misses_prints_it(self):
        from sampo import selfaware
        for text, source in (("dashboard chart for merge queue wait times", "self"),
                             ("retry flaky deploy script", "self"), ("remove stale lanes folder", "discovered")):
            self.done(self.file(text, source, -40000))
        self.steer("s6", "u8", "add a dashboard chart of the merge queue wait times", -28800)
        self.steer("s6", "u9", "please retry the flaky deploy script", -20000)
        self.steer("s6", "u10", "remove the stale lanes folder", -5000)
        r = selfaware.report(self.p)
        self.assertEqual((r["catches"], r["catches_per_week"]), (3, 0.7))
        out = self.fm("misses").stdout
        self.assertIn("3 catches", out)
        self.assertIn("0.7 a week", out)


class ToldNotActed(Base):
    def test_a_steer_repeating_an_earlier_self_tell_is_told_not_acted_and_names_the_check(self):
        from sampo import selfaware
        self.tell("usage_burn", "usage is burning three times the budget on parallel sampo lanes, cut the lanes", -30)
        self.steer("s7", "u9", "cut the parallel lanes the usage is burning", -10, root="R4")
        r = selfaware.report(self.p)
        self.assertEqual((r["total"], r["catches"], r["told_not_acted"]), (1, 0, 1))
        self.assertEqual(r["told_checks"], {"usage_burn": 1})
        self.assertEqual(r["told_examples"][0]["check"], "usage_burn")
        self.assertIn("usage_burn", self.fm("misses").stdout)

    def test_a_self_tell_older_than_seven_days_does_not_make_a_steer_told(self):
        from sampo import selfaware
        self.tell("usage_burn", "usage is burning three times the budget on parallel sampo lanes", -9 * 1440)
        self.steer("s7", "u9", "cut the parallel lanes the usage is burning", -1440)
        self.assertEqual(selfaware.report(self.p)["told_not_acted"], 0)

    def test_a_self_tell_after_the_steer_does_not_make_it_told(self):
        from sampo import selfaware
        self.steer("s7", "u9", "cut the parallel lanes the usage is burning", -30)
        self.tell("usage_burn", "usage is burning three times the budget on parallel sampo lanes", -10)
        self.assertEqual(selfaware.report(self.p)["told_not_acted"], 0)

    def test_a_steer_a_task_already_caught_is_not_also_told(self):
        from sampo import selfaware
        self.file("dashboard chart for merge queue wait times", "self", -60)
        self.tell("usage_burn", "dashboard chart for merge queue wait times is still missing", -30)
        self.steer("s8", "u10", "add a dashboard chart of the merge queue wait times", -5)
        r = selfaware.report(self.p)
        self.assertEqual((r["catches"], r["told_not_acted"]), (1, 0))

    def test_one_shared_word_with_a_self_tell_is_not_told(self):
        from sampo import selfaware
        self.tell("usage_burn", "usage is burning three times the budget", -30)
        self.steer("s9", "u11", "the usage was fine yesterday", -10)
        self.assertEqual(selfaware.report(self.p)["told_not_acted"], 0)


    def test_a_check_name_is_printed_through_plain(self):
        """A check's name comes from the ledger: its keys print through c.plain, so no escape sequence reaches a terminal."""
        from sampo import selfaware
        raw = "usage_burn\x1b[2J"
        self.tell(raw, "usage is burning three times the budget on parallel sampo lanes", -30)
        self.steer("s7", "u9", "cut the parallel lanes the usage is burning", -10)
        r = selfaware.report(self.p)
        self.assertEqual(list(r["told_checks"]), [c.plain(raw)])
        self.assertNotIn("\x1b", r["told_examples"][0]["check"])
        self.assertNotIn("\x1b", self.fm("misses").stdout)


class Summary(Base):
    def test_the_misses_summary_and_the_status_line_print_the_same_counts(self):
        from sampo import selfaware
        self.file("dashboard chart for merge queue wait times", "self", -60)
        self.steer("s5", "u7", "add a dashboard chart of the merge queue wait times please", -5, root="R1")
        line = selfaware.status_line(self.p)
        self.assertIsNotNone(line)
        summary = selfaware.summary(selfaware.report(self.p))
        self.assertIn("1 catch", summary)
        self.assertIn("0 told but not acted", summary)
        self.assertIn(summary, line)  # the status line carries the misses summary's words
        self.assertIn(summary, self.fm("misses").stdout)
        self.assertIn(summary, self.fm("status").stdout)

    def test_the_status_line_is_absent_with_no_misses(self):
        from sampo import selfaware
        self.assertIsNone(selfaware.status_line(self.p))

    def test_a_second_status_with_unchanged_inputs_does_not_reload_briefs(self):
        """The report is kept under the inputs it read (steers.jsonl, the ledger, every brief's stamp): a second status with
        nothing changed reads that copy and loads no brief (T-5712 review)."""
        from sampo import selfaware
        self.file("dashboard chart for merge queue wait times", "self", -60)
        self.steer("s5", "u7", "add a dashboard chart of the merge queue wait times please", -5, root="R1")
        loads, real = [], c.load_briefs
        with mock.patch.object(c, "load_briefs", lambda *a, **k: loads.append(a) or real(*a, **k)):
            first = selfaware.status_line(self.p)
            after_first = len(loads)
            second = selfaware.status_line(self.p)
        self.assertGreater(after_first, 0)
        self.assertEqual(len(loads), after_first)
        self.assertEqual(first, second)

    def test_a_new_steer_is_seen_at_the_next_status(self):
        from sampo import selfaware
        self.steer("s5", "u7", "the export button does nothing", -10)
        self.assertIn("Misses (30 days): 1 ", selfaware.status_line(self.p))
        self.steer("s6", "u8", "the login page is broken again", -5)
        self.assertIn("Misses (30 days): 2 ", selfaware.status_line(self.p))
