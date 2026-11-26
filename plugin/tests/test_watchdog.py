"""T-6859: the throughput watchdog (sampo/watchdog.py). Each tick reads one outcome (builder-done to on trunk: the median and
p90 hours, the lanes landed an hour, and the returned lanes not landed, timed as censored hours) and a stage census of the
open work. A regressed outcome names the item stage most items sit in and that stage's fix, and ship beside it. The advice
is checked after a wait: when the action it named ran and the stage did not move, the stage's alarm is raised; an open
alarm is in every tick's note until the count moves. The pure readers are tested on ledger dicts; the tick is tested on a
real project with the lane readers stubbed, and the replay of the 40-lane stall is run through tick()."""
import json
import os
import time
from types import SimpleNamespace
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import watchdog

H = 3600
T0 = 1_800_000_000.0  # a fixed epoch for the pure readers


def ev(kind, task, at, **data):
    return {"event": kind, "ts": c.iso(at), "task": task, "data": data}


def landed(task, at, hours):
    """A lane whose builder returned `hours` before it landed at `at`."""
    return [ev("builder_mark", task, at - hours * H, returned=True), ev("lane_landed", task, at)]


def returned(task, at):
    return ev("builder_mark", task, at, returned=True)


def out(n=5, landed=0, returned_open=0, median_h=3.0, p90_h=10.0):
    return {"n": n, "landed": landed, "returned_open": returned_open, "median_h": median_h, "p90_h": p90_h,
            "per_hour": 0.1, "window_h": 24}


def planned(tid, status="planned"):
    return SimpleNamespace(id=tid, status=status, meta={})


def fake_read(events):
    """A stand-in for watchdog._read: the events of these kinds at or after `since`, as the ledger would give them."""
    def read(p, kinds, since):
        return [e for e in events if e["event"] in kinds and watchdog._at(e["ts"]) >= since]
    return read


class Outcome(ForemanTestCase):
    def test_median_p90_and_landed_an_hour_over_the_window(self):
        evs = []
        for i, h in enumerate((2, 4, 6, 30)):
            evs += landed(f"T-{i}", T0 - 2 * H, h)
        o = watchdog.outcome_of(evs, T0)
        self.assertEqual(o["n"], 4)
        self.assertEqual(o["median_h"], 5.0)
        self.assertEqual(o["p90_h"], 30.0)
        self.assertEqual(o["window_h"], 24)
        self.assertEqual(o["landed"], 4)
        self.assertEqual(round(o["per_hour"], 3), round(4 / 24, 3))

    def test_landed_outside_the_window_is_not_counted(self):
        evs = landed("T-old", T0 - 30 * H, 3) + landed("T-new", T0 - 2 * H, 3)
        o = watchdog.outcome_of(evs, T0)
        self.assertEqual(o["n"], 1)
        self.assertEqual(o["landed"], 1)
        self.assertEqual(o["median_h"], 3.0)

    def test_a_lane_with_no_builder_return_is_not_timed(self):
        o = watchdog.outcome_of([ev("lane_landed", "T-9", T0 - H)], T0)
        self.assertEqual(o["n"], 0)
        self.assertIsNone(o["median_h"])
        self.assertIsNone(o["p90_h"])

    def test_returned_lanes_with_no_landing_count_their_hours_so_far(self):
        # the 40-lane stall: 40 returned 6 h ago, none landed. Their censored hours are the median and p90, not n=0.
        evs = [returned(f"T-{i}", T0 - 6 * H) for i in range(40)]
        o = watchdog.outcome_of(evs, T0)
        self.assertEqual(o["n"], 40)
        self.assertEqual(o["landed"], 0)
        self.assertEqual(o["returned_open"], 40)
        self.assertEqual(o["median_h"], 6.0)
        self.assertEqual(o["p90_h"], 6.0)
        self.assertEqual(o["per_hour"], 0.0)

    def test_a_closed_task_returned_but_not_landed_is_not_timed(self):
        evs = [returned("T-gone", T0 - 6 * H), returned("T-open", T0 - 6 * H)]
        o = watchdog.outcome_of(evs, T0, closed={"T-gone"})
        self.assertEqual(o["n"], 1)
        self.assertEqual(o["returned_open"], 1)

    def test_a_stalled_outcome_is_a_regression_naming_its_stage(self):
        evs = [returned(f"T-{i}", T0 - 6 * H) for i in range(40)]
        o = watchdog.outcome_of(evs, T0)
        v = watchdog.verdict(o, {"parked": 0, "held": 0, "land": 0, "ship": 0, "idle": 5, "queued": 5})
        self.assertEqual(v["stage"], "idle")
        self.assertEqual(v["count"], 5)

    def test_too_few_returned_lanes_to_judge_a_stall(self):
        evs = [returned(f"T-{i}", T0 - 6 * H) for i in range(2)]
        self.assertIsNone(watchdog.verdict(watchdog.outcome_of(evs, T0), {"idle": 5, "queued": 5}))


class Verdict(ForemanTestCase):
    def test_regressed_outcome_names_the_stage_and_its_fix(self):
        v = watchdog.verdict(out(n=5, landed=2, median_h=40.0, p90_h=80.0),
                             {"parked": 1, "held": 0, "land": 4, "ship": 2, "idle": 0})
        self.assertEqual(v["stage"], "land")
        self.assertEqual(v["count"], 4)
        self.assertEqual(v["fix"], watchdog.FIXES["land"])

    def test_p90_past_its_bar_is_a_regression(self):
        v = watchdog.verdict(out(median_h=5.0, p90_h=80.0), {"land": 2})
        self.assertIsNotNone(v)
        self.assertEqual(v["stage"], "land")

    def test_ship_is_named_beside_the_stage_never_as_it(self):
        v = watchdog.verdict(out(median_h=40.0), {"parked": 0, "held": 0, "land": 0, "ship": 3, "idle": 0, "queued": 0})
        self.assertIsNone(v["stage"])
        self.assertEqual(v["ship"], 3)

    def test_idle_is_named_only_when_tasks_are_queued(self):
        stalled = out(landed=0, returned_open=5, median_h=30.0, p90_h=30.0)
        self.assertIsNone(watchdog.verdict(stalled, {"idle": 2, "queued": 0})["stage"])
        self.assertEqual(watchdog.verdict(stalled, {"idle": 2, "queued": 2})["stage"], "idle")

    def test_an_item_stage_beats_idle(self):
        stalled = out(landed=0, returned_open=5, median_h=30.0, p90_h=30.0)
        v = watchdog.verdict(stalled, {"parked": 1, "idle": 2, "queued": 2})
        self.assertEqual(v["stage"], "parked")

    def test_within_the_bars_gives_no_verdict(self):
        self.assertIsNone(watchdog.verdict(out(median_h=3.0, p90_h=10.0),
                                           {"parked": 3, "held": 0, "land": 0, "ship": 0, "idle": 2, "queued": 2}))

    def test_too_few_lanes_to_judge_gives_no_verdict(self):
        self.assertIsNone(watchdog.verdict(out(n=1, median_h=90.0, p90_h=90.0), {"land": 1}))


class Stubs(ForemanTestCase):
    def patch(self, target, **kw):
        m = mock.patch(target, **kw)
        self.addCleanup(m.stop)
        return m.start()


class Census(Stubs):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def test_idle_is_the_cap_less_the_working_slots(self):
        self.patch("sampo.lanes.builders_cap", return_value=6)
        self.patch("sampo.lanes._slots", return_value=([object()], [], []))
        self.patch("sampo.recover.held", return_value={})
        self.patch("sampo.selfcheck.unshipped_read", return_value=None)
        cen = watchdog.census(self.p, T0, briefs=[])
        self.assertEqual(cen["idle"], 5)

    def test_held_counts_only_open_tasks(self):
        self.patch("sampo.lanes.builders_cap", return_value=2)
        self.patch("sampo.lanes._slots", return_value=([], [], []))
        self.patch("sampo.recover.held", return_value={"T-1": "why", "T-2": "why"})
        self.patch("sampo.lanes.awaiting_merge", return_value=[])
        self.patch("sampo.selfcheck.unshipped_read", return_value=None)
        briefs = [planned("T-1", status="done"), planned("T-2")]
        self.assertEqual(watchdog.census(self.p, T0, briefs=briefs)["held"], 1)

    def test_queued_counts_planned_tasks_with_no_builder(self):
        self.patch("sampo.lanes.builders_cap", return_value=2)
        self.patch("sampo.lanes._slots", return_value=([], [], []))
        self.patch("sampo.recover.held", return_value={})
        self.patch("sampo.lanes.awaiting_merge", return_value=[])
        self.patch("sampo.selfcheck.unshipped_read", return_value=None)
        briefs = [planned("T-1"), planned("T-2", status="done"), planned("T-3")]
        self.assertEqual(watchdog.census(self.p, T0, briefs=briefs)["queued"], 2)


class Ticks(Stubs):
    """The tick on a real project. The lane readers are stubbed so each test sets the census it needs."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def stub(self, working=1, cap=6, held=None, merge=(), briefs=(), ship=0):
        self.patch("sampo.lanes._slots", return_value=([object()] * working, [], []))
        self.patch("sampo.lanes.builders_cap", return_value=cap)
        self.patch("sampo.recover.held", return_value=dict(held or {}))
        self.patch("sampo.lanes.awaiting_merge", side_effect=lambda *a, **k: list(merge))
        self.patch("sampo.core.load_briefs", side_effect=lambda *a, **k: list(briefs))
        self.patch("sampo.selfcheck.unshipped_read", return_value=(ship, T0) if ship else None)

    def reads(self, events=()):
        self.patch("sampo.watchdog._read", side_effect=fake_read(list(events)))

    def seed(self, st):
        with open(os.path.join(self.p.dir, "watchdog.json"), "w", encoding="utf-8") as f:
            json.dump(st, f)

    def test_replay_40_returned_lanes_unlanded_6h_names_idle(self):
        # today's shape: 40 lanes returned and none landed for 6 h, cap 6 with 1 working, 5 tasks queued
        for i in range(40):
            c.log_event(self.p, "builder_mark", task=f"T-{i}", data={"returned": True})
        self.stub(working=1, cap=6, briefs=[planned(f"T-P{i}") for i in range(5)])
        st = watchdog.tick(self.p, time.time() + 6 * H)
        self.assertEqual(st["outcome"]["landed"], 0)
        self.assertEqual(st["outcome"]["returned_open"], 40)
        self.assertAlmostEqual(st["outcome"]["median_h"], 6.0, delta=0.01)
        self.assertIn("5 item(s) at idle", " ".join(st["note"]))
        self.assertEqual(st["census"]["idle"], 5)

    def test_returns_older_than_48_hours_still_time_the_p90(self):
        # HIGH 2: three lanes landed an hour ago, returned 62, 76 and 92 h before the tick. A 48 h read drops the returns.
        evs = []
        for i, h in enumerate((61, 75, 91)):
            evs += landed(f"T-{i}", T0 - H, h)
        self.reads(evs)
        self.stub(working=1, cap=6)
        st = watchdog.tick(self.p, T0)
        self.assertEqual(st["outcome"]["n"], 3)
        self.assertEqual(st["outcome"]["p90_h"], 91.0)
        self.assertEqual(st["outcome"]["median_h"], 75.0)
        self.assertIn("regressed", " ".join(st["note"]))

    def test_ship_is_named_beside_the_stage_and_idle_is_not_without_queued_tasks(self):
        self.reads([returned(f"T-{i}", T0 - 6 * H) for i in range(3)])
        merged = [object()] * 4
        self.stub(working=1, cap=6, merge=merged, ship=2)
        note = " ".join(watchdog.tick(self.p, T0)["note"])
        self.assertIn("4 item(s) at land", note)
        self.assertIn("2 item(s) at ship", note)
        self.assertNotIn("at idle", note)

    def test_the_regressed_note_for_advice_still_waiting_says_so(self):
        self.reads([returned(f"T-{i}", T0 - 6 * H) for i in range(3)])
        self.stub(working=1, cap=6, briefs=[planned(f"T-P{i}") for i in range(5)])
        first = " ".join(watchdog.tick(self.p, T0)["note"])
        second = " ".join(watchdog.tick(self.p, T0 + watchdog.GAP_S + 1)["note"])
        self.assertIn("5 item(s) at idle", first)
        self.assertIn("watching whether its count moves", second)
        self.assertNotIn("already", second)

    def test_an_open_alarm_is_in_every_tick_until_it_clears(self):
        self.seed({"at": None, "advice": {}, "alarm": {"land": {"count": 3, "at": T0}}, "note": []})
        merged = [object()] * 3
        self.reads([])
        self.stub(merge=merged)
        st1 = watchdog.tick(self.p, T0 + 10)
        self.assertTrue(any("the land alarm is open" in n for n in st1["note"]))
        st2 = watchdog.tick(self.p, T0 + 10 + watchdog.GAP_S)
        self.assertTrue(any("the land alarm is open" in n for n in st2["note"]))
        merged[:] = [object()]
        st3 = watchdog.tick(self.p, T0 + 10 + 2 * watchdog.GAP_S)
        self.assertEqual(st3["note"], [])
        self.assertEqual(st3["alarm"], {})

    def test_a_corrupt_state_file_heals_instead_of_raising_every_tick(self):
        self.seed({"at": "soon", "advice": {"land": {"count": "x", "at": 1}, "bogus": {"count": 1, "at": 1},
                                             "idle": {"count": 2, "at": 5}}, "alarm": "nope", "note": []})
        self.reads([])
        self.stub(working=1, cap=6)
        st = watchdog.tick(self.p, T0)
        self.assertEqual(set(st["advice"]), {"idle"})
        self.assertEqual(st["alarm"], {})

    def test_an_unreadable_state_file_is_empty_state(self):
        with open(os.path.join(self.p.dir, "watchdog.json"), "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertEqual(watchdog.note(self.p, T0), "")
        self.reads([])
        self.stub()
        self.assertIsNotNone(watchdog.tick(self.p, T0))

    def test_tick_writes_the_outcome_and_the_census(self):
        now = time.time()
        self.assertEqual(set(watchdog.tick(self.p, now)["census"]), set(watchdog.STAGES) | {"queued"})
        with open(os.path.join(self.p.dir, "watchdog.json"), encoding="utf-8") as f:
            st = json.load(f)
        self.assertIn("outcome", st)
        self.assertEqual(set(st["census"]), set(watchdog.STAGES) | {"queued"})

    def test_a_tick_inside_the_gap_counts_nothing(self):
        now = time.time()
        watchdog.tick(self.p, now)
        self.assertIsNone(watchdog.tick(self.p, now + 60))


class Advice(Stubs):
    def test_no_action_yet_waits(self):
        st = {"advice": {"land": {"count": 3, "at": T0}}, "alarm": {}}
        self.assertIsNone(watchdog.check_advice(st, {"land": 3}, {"land": False}, T0 + 600))
        self.assertIn("land", st["advice"])

    def test_action_ran_and_the_count_did_not_move_raises_the_alarm(self):
        st = {"advice": {"land": {"count": 3, "at": T0}}, "alarm": {}}
        watchdog.check_advice(st, {"land": 3}, {"land": True}, T0 + watchdog.WAIT_S + 1)
        self.assertEqual(st["alarm"]["land"]["count"], 3)
        self.assertNotIn("land", st["advice"])

    def test_the_count_moving_clears_the_advice_and_no_alarm(self):
        st = {"advice": {"land": {"count": 3, "at": T0}}, "alarm": {}}
        watchdog.check_advice(st, {"land": 2}, {"land": True}, T0 + 600)
        self.assertNotIn("land", st["advice"])
        self.assertNotIn("land", st["alarm"])

    def test_an_open_alarm_is_not_repeated_as_advice(self):
        st = {"advice": {}, "alarm": {"land": {"count": 3, "at": T0}}}
        self.assertIsNone(watchdog.advise(st, "land", 3, T0 + 10 * watchdog.WAIT_S))
        self.assertNotIn("land", st["advice"])

    def test_advice_is_given_once_while_it_waits(self):
        st = {"advice": {}, "alarm": {}}
        self.assertEqual(watchdog.advise(st, "ship", 2, T0), watchdog.FIXES["ship"])
        self.assertIsNone(watchdog.advise(st, "ship", 2, T0 + 60))

    def test_the_alarm_clears_once_the_stage_moves(self):
        st = {"advice": {}, "alarm": {"land": {"count": 3, "at": T0}}}
        watchdog.check_advice(st, {"land": 1}, {"land": False}, T0 + 9 * watchdog.WAIT_S)
        self.assertNotIn("land", st["alarm"])

    def test_advice_that_did_not_run_is_kept_and_given_once(self):
        # LOW 9: no action ran after the advice; it stays in state and is not given again
        st = {"advice": {"land": {"count": 3, "at": T0}}, "alarm": {}}
        watchdog.check_advice(st, {"land": 3}, {"land": False}, T0 + watchdog.WAIT_S + 1)
        self.assertIn("land", st["advice"])
        self.assertNotIn("land", st["alarm"])
        self.assertIsNone(watchdog.advise(st, "land", 3, T0 + 10 * watchdog.WAIT_S))

    def test_an_action_is_judged_against_its_own_stage_since(self):
        # LOW 8: a lane_builder after idle's advice does not count for parked, whose advice came later
        ledger = [ev("lane_builder", "T-1", T0 + 500)]
        self.patch("sampo.watchdog._read", side_effect=fake_read(ledger))
        acted = watchdog._acted(None, {"parked": T0 + 600, "idle": T0 + 400})
        self.assertFalse(acted["parked"])
        self.assertTrue(acted["idle"])
