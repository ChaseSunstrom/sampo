"""T-5839: each lane's stage times (built, reviewed, accepted, landed, closed) are derived on read from the events the
ledger already holds, and fm lane list shows them as the gap before each stage."""
import json
import os

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes as slanes

DAY = "2026-11-20T"


def event(hms, kind, task, **data):
    return {"ts": f"{DAY}{hms}Z", "session_id": "s", "project": "x", "task": task, "event": kind, "data": data}


class StageTimes(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def write(self, *events, raw=()):
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
            for line in raw:
                f.write(line + "\n")

    def whole_lane(self, tid):
        return [event("10:00:00", "lane_builder", tid, chars=900),
                event("10:12:00", "builder_mark", tid, returned=True, agent="a1"),
                event("10:15:00", "reviewed", tid, tip="abc"),
                event("10:16:00", "accepted", tid, at=1),
                event("10:56:00", "lane_merge", tid, train=[tid]),
                event("10:57:00", "task_done", tid)]

    def test_stage_times_from_the_events_the_ledger_holds(self):
        self.write(*self.whole_lane("T-0001"), event("10:05:00", "builder_mark", "T-0002", returned=True))
        got = slanes.stage_times(self.p, "T-0001")
        self.assertEqual([s["stage"] for s in got], ["built", "reviewed", "accepted", "landed", "closed"])
        self.assertEqual([s["gap_s"] for s in got], [720, 180, 60, 2400, 60])
        self.assertEqual(got[0]["at"], f"{DAY}10:12:00Z")
        self.assertEqual(got[3]["at"], f"{DAY}10:56:00Z")

    def test_stage_times_skips_a_stage_not_reached_yet(self):
        self.write(*self.whole_lane("T-0003")[:3])  # stopped after review
        got = slanes.stage_times(self.p, "T-0003")
        self.assertEqual([(s["stage"], s["gap_s"]) for s in got], [("built", 720), ("reviewed", 180)])
        hand = [event("11:00:00", "builder_mark", "T-0004", returned=True),
                event("11:30:00", "lane_merge", "T-0004", train=["T-0004"])]  # landed by hand: never reviewed or accepted
        self.write(*hand)
        got = slanes.stage_times(self.p, "T-0004")
        self.assertEqual([(s["stage"], s["gap_s"]) for s in got], [("built", None), ("landed", 1800)])

    def test_stage_times_follows_the_latest_return_and_ignores_an_undone_merge(self):
        t = "T-0005"
        self.write(event("10:00:00", "lane_builder", t), event("10:10:00", "builder_mark", t, returned=True),
                   event("10:12:00", "reviewed", t),  # findings: the builder resumed, and returned again
                   event("10:20:00", "builder_mark", t, returned=False), event("10:30:00", "builder_mark", t, returned=True),
                   event("10:40:00", "lane_merge", t, undone=True))
        got = slanes.stage_times(self.p, t)
        self.assertEqual([(s["stage"], s["at"][-9:-1]) for s in got], [("built", "10:30:00")])
        self.assertEqual(got[0]["gap_s"], 1800)

    def test_stage_times_never_raises_on_a_lane_with_no_events_or_odd_ones(self):
        self.assertEqual(slanes.stage_times(self.p, "T-9999"), [])
        self.assertEqual(slanes.stage_times(self.p, ""), [])
        self.assertEqual(slanes.stage_times(self.p, None), [])
        self.write({"ts": "not a time", "event": "builder_mark", "task": "T-0006", "data": "x"},
                   {"ts": None, "event": "reviewed", "task": "T-0006", "data": None},
                   {"event": "accepted", "task": "T-0006"},
                   event("10:00:00", "lane_merge", "T-0006", train="no"), raw=["{broken"])
        got = slanes.stage_times(self.p, "T-0006")
        self.assertEqual([s["stage"] for s in got], ["landed"])
        self.assertIsNone(got[0]["gap_s"])

    def test_stage_times_listed(self):
        tid = json.loads(self.fm("task", "new", "Staged", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        path = json.loads(self.fm("lane", "new", tid, "--json").stdout)["path"]
        self.write(*self.whole_lane(tid)[:3])
        text = self.fm("lane", "list").stdout
        self.assertIn("built 12m, reviewed 3m", text)
        self.assertNotIn("accepted", text, "a stage not reached is not listed")
        rows = json.loads(self.fm("lane", "list", "--json").stdout)["lanes"]
        row = next(r for r in rows if r["path"] == path)
        self.assertEqual([(s["stage"], s["gap_s"]) for s in row["stages"]], [("built", 720), ("reviewed", 180)])
        self.write(*self.whole_lane(tid)[3:5])
        text = self.fm("lane", "list").stdout
        self.assertIn("built 12m, reviewed 3m, accepted 60s, landed 40m", text)
