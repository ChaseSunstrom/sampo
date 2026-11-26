"""T-5839: each lane's stage times (built, reviewed, accepted, landed, closed) are derived on read from the events the
ledger already holds, and fm lane list shows them as the gap before each stage."""
import json
import os
import subprocess
import time

from helpers import ForemanTestCase
from test_lane_land import CLEAN_CMD, report
from test_lanepilot import PilotCase

from sampo import core as c
from sampo import lanes as slanes
from sampo import train

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


class LiveFlow(PilotCase):
    """T-5840: in a live project a builder's return runs the whole chain with no main-thread call: the detached pilot
    reviews, a clean lane is accepted, the accept spawns the land, and the land closes the task. Findings stop it at the
    pilot. A gated project still runs its check stage before anything queues."""

    def setUp(self):
        super().setUp()
        self.fm("lane", "mode", "live")
        # the hook matches a lane by its agent-<id> path, which this test lane does not have: the worktree moves there, as a
        # real folder (T-6227: the pilot opens the lane root O_NOFOLLOW, so a link at the lane path is refused), and the old
        # path links to it for the helpers that still name lane1
        self.link = os.path.join(self.tmp, "agent-a1b2c3d4")
        old = os.path.join(self.tmp, "lane1")
        subprocess.run(["git", "-C", self.repo, "worktree", "move", old, self.link], check=True, capture_output=True)
        os.symlink(self.link, old)
        x = self.brief()
        x.meta["lane"] = self.link
        # PilotCase stamped the return itself: the hook marks a return only when the stamp changes, so clear it here and let
        # the hook's own SubagentStop write the builder_mark the stages read
        x.meta.pop("builder_returned", None)
        c.save_brief(c.find_project(self.repo), x)

    def background_on(self):
        os.environ.pop("FOREMAN_NO_BACKGROUND", None)  # PilotCase's patch.dict restores it at cleanup

    def hook_return(self):
        """The builder's own stop. spar.py's payload names the agent bare (fm-builder), which the namespace check of
        pilotsig.returned never matches; Claude Code sends the namespaced name, so that is the one sent here."""
        r = self.hook("SubagentStop", {"session_id": "spar", "agent_type": "sampo:fm-builder", "stop_hook_active": False,
                                       "agent_id": "a1b2c3d4", "last_assistant_message": report(self.tid, "Lane 1")})
        self.assertEqual(r.returncode, 0, r.stderr)

    def wait(self, done, what, limit=240):
        end = time.monotonic() + limit
        while not done():
            if time.monotonic() > end:
                b = self.brief()
                log = os.path.join(c.find_project(self.repo).dir, "pilot.log")
                tail = open(log, encoding="utf-8", errors="replace").read()[-1500:] if os.path.exists(log) else "no log"
                self.fail(f"{what} within {limit} s: status {b.status}, stages {self.stages()}, "
                          f"pilot pid {b.meta.get('pilot_pid')}, pilot.log: {tail}")
            time.sleep(0.5)

    def stage_list(self):
        return slanes.stage_times(c.find_project(self.repo), self.tid)

    def stages(self):
        return [s["stage"] for s in self.stage_list()]

    def test_clean_return_reviews_lands_closes(self):
        self.background_on()
        t0 = time.monotonic()
        self.hook_return()
        self.wait(lambda: self.brief().status in c.CLOSED, "the lane closes")
        chain = time.monotonic() - t0
        self.assertEqual(self.brief().meta.get("pilot_outcome"), "accepted")
        self.assertLessEqual({"adversary", "intent"}, set(self.audits()), "the clean lenses are recorded on the lane")
        self.assertTrue(self.accepted())
        self.assertEqual(self.stages(), ["built", "reviewed", "accepted", "landed", "closed"])
        ats = [s["at"] for s in self.stage_list()]
        self.assertEqual(ats, sorted(ats), "the stages are in the order they happened")
        self.assertLess(chain, 240, "the chain's wall time per return")

    def test_findings_stop_flow(self):
        self.background_on()
        os.environ["STUB_BAD_LENS"] = "adversary"  # that lens reports a HIGH finding on every round
        self.addCleanup(os.environ.pop, "STUB_BAD_LENS", None)
        self.hook_return()
        self.wait(lambda: self.brief().meta.get("pilot_done_for"), "the pilot reports")
        b = self.brief()
        self.assertNotEqual(b.meta.get("pilot_outcome"), "accepted", b.meta)
        self.assertEqual(b.meta.get("changes_asked"), "lane-pilot", "the lane is marked for its builder")
        self.assertFalse(self.accepted(), "no accept ran")
        self.assertNotIn("accepted", self.stages())
        self.assertNotIn("landed", self.stages(), "no land was spawned")
        self.assertNotIn("closed", self.stages())
        self.assertNotIn(b.status, c.CLOSED)

    def test_gated_project_unchanged(self):
        self.fm("lane", "mode", "gated")  # setUp says live: this project is the gated one
        self.assertFalse(train.live_mode(c.find_project(self.repo)))
        # a newer failing run at the tip replaces the helper's clean one (T-4286: a clean run at the tip lets any accept skip
        # the check, gated or live): with none, a gated accept must go to its check stage, where a live one queues
        p = c.find_project(self.repo)
        x = self.brief()
        x.add_evidence(c.AFFECTED_LABEL + CLEAN_CMD, "✗ 1 failed", step=1, ran=True,
                       ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60)))
        c.save_brief(p, x)
        tip = subprocess.run(["git", "-C", self.link, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        self.assertFalse(train._clean_run(self.brief(), tip, self.repo), "the newest run at the tip is a red one")
        self.hook_return()  # FOREMAN_NO_BACKGROUND is on: no detached pilot starts, the return is stamped only
        self.assertEqual(self.stages(), ["built"])
        self.pilot()  # the lane pilot verb, in process: the reviewers run, then accept goes to its check stage
        self.assertEqual(self.audits(), ["adversary", "intent"], "the pilot still reviews a gated lane")
        b = self.brief()
        self.assertFalse(self.accepted(), f"the gated accept waits on its check (not started here: no background), {b.meta}")
        self.assertNotIn("check_state", b.meta, "a check that did not start gives its claim back")
        self.assertNotIn("accepted", self.stages(), "nothing queues before the check")
        self.assertNotIn("landed", self.stages())
        self.assertNotIn("closed", self.stages())
        self.assertNotIn(b.status, c.CLOSED)
