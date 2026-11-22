"""The flow report in `sampo digest` (T-5503): request-to-done by stage, tokens and turns per agent type, test runs by
origin, guard blocks by category, tasks made and closed per day; each number shown against the last earlier day's saved
snapshot, one snapshot a day. A scratch project, hand-written ledger events at pinned times and fake transcripts."""
import json
import os

from helpers import ForemanTestCase

from sampo import core as c
from sampo import flowreport

DAY1 = "2026-11-20T18:00:00Z"
DAY2 = "2026-11-21T18:00:00Z"


def usage(**kw):
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, **kw}


class FlowReport(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.cc = os.path.join(self.tmp, "cc")
        self.addCleanup(lambda: os.environ.pop("FOREMAN_NOW", None))

    def ev(self, ts, event, task=None, data=None):
        os.environ["FOREMAN_NOW"] = ts
        try:
            c.log_event(self.p, event, task=task, data=data or {})
        finally:
            os.environ.pop("FOREMAN_NOW", None)

    def lane(self, tid, made, *gaps):
        """One closed task whose stages last `gaps` seconds: wait, build, review, land, close, from `made` (epoch)."""
        t, stamps = made, []
        for g in (0,) + gaps:
            t += g
            stamps.append(c.iso(t))
        names = ("task_new", "focus", "builder_mark", "accepted", "lane_landed", "task_done")
        for name, ts in zip(names, stamps):
            self.ev(ts, name, tid, {"returned": True} if name == "builder_mark" else {})

    def transcript(self, name, *messages):
        folder = os.path.join(self.cc, "projects", "-scratch", "subagents")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w") as f:
            for i, u in enumerate(messages):
                f.write(json.dumps({"type": "assistant", "timestamp": "2026-11-20T10:00:00.000Z",
                                    "message": {"id": f"{name}-{i}", "usage": u}}) + "\n")
        return path

    def seed(self):
        base = c.parse_ts("2026-11-20T01:00:00Z").timestamp()
        self.lane("T-0001", base, 7200, 3600, 1800, 3600, 600)
        self.lane("T-0002", base, 3600, 7200, 3600, 3600, 1800)
        for sec in ("system", "system", "brief"):
            self.ev("2026-11-20T11:00:00Z", "guard_block", "T-0001", {"category": sec, "detail": "x"})
        self.ev("2026-11-20T11:00:00Z", "check_run", "T-0001", {"results": [{"cmd": "t", "exit": 0, "s": 100.0}]})
        self.ev("2026-11-20T11:30:00Z", "check_run", None, {"train": ["T-0001"], "results": [{"cmd": "t", "exit": 0, "s": 400.0}]})
        self.ev("2026-11-20T11:40:00Z", "ambient_run", "T-0001", {"tests": 2, "exit": 0})
        self.ev("2026-11-20T11:50:00Z", "ship_test", None, {"cmd": "t", "exit": 0, "s": 50.0})
        builder = self.transcript("b.jsonl", usage(input_tokens=100, output_tokens=10), usage(cache_read_input_tokens=1000))
        rev = self.transcript("r.jsonl", usage(output_tokens=20))
        outside = os.path.join(self.tmp, "elsewhere.jsonl")
        with open(outside, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"id": "z", "usage": usage(output_tokens=999)}}) + "\n")
        for kind, path in (("sampo:fm-builder", builder), ("sampo:fm-reviewer", rev), ("sampo:fm-reviewer", outside)):
            self.ev("2026-11-20T12:00:00Z", "subagent", "T-0001", {"agent_type": kind, "agent_id": "a", "transcript": path})

    def digest(self, now, *args):
        got = self.fm("digest", *args, env={"FOREMAN_NOW": now, "CLAUDE_CONFIG_DIR": self.cc})
        return got.stdout

    def baseline_lines(self):
        try:
            with open(os.path.join(self.p.dir, "flow-baseline.jsonl")) as f:
                return [x for x in f.read().splitlines() if x.strip()]
        except OSError:
            return []

    def test_digest_json_has_flow_sections(self):
        self.seed()
        flow = json.loads(self.digest(DAY1, "--json"))["flow"]
        st = flow["stages"]
        self.assertEqual([st[k]["n"] for k in ("wait", "build", "review", "land", "close")], [2] * 5)
        self.assertEqual([st[k]["median_s"] for k in ("wait", "build", "review", "land", "close")],
                         [5400.0, 5400.0, 2700.0, 3600.0, 1200.0])
        self.assertEqual(flow["agents"]["sampo:fm-builder"], {"runs": 1, "read": 1, "turns": 2, "tokens": 250.0})
        self.assertEqual(flow["agents"]["sampo:fm-reviewer"]["runs"], 2)
        self.assertEqual(flow["agents"]["sampo:fm-reviewer"]["read"], 1)  # the one outside the projects folder is not read
        self.assertEqual(flow["agents"]["sampo:fm-reviewer"]["tokens"], 100.0)
        self.assertEqual(flow["tests"]["train"], {"runs": 1, "seconds": 400.0})
        self.assertEqual(flow["tests"]["task"], {"runs": 1, "seconds": 100.0})
        self.assertEqual(flow["tests"]["ambient"]["runs"], 1)
        self.assertEqual(flow["tests"]["ship"], {"runs": 1, "seconds": 50.0})
        self.assertEqual(flow["guard"], {"system": 2, "brief": 1})
        self.assertEqual(flow["made_closed"], [{"day": "2026-11-20", "made": 2, "closed": 2}])
        self.assertIsNone(flow["baseline"])

    def test_baseline_once_a_day_and_delta_shown(self):
        self.seed()
        first = self.digest(DAY1)
        self.assertIn("Flow", first)
        self.assertIn("first snapshot", first)
        self.assertEqual(len(self.baseline_lines()), 1)
        self.digest(DAY1)  # the same day again: nothing new is kept
        self.assertEqual(len(self.baseline_lines()), 1)
        self.ev("2026-11-21T09:00:00Z", "guard_block", "T-0001", {"category": "system", "detail": "y"})
        self.ev("2026-11-21T09:00:00Z", "guard_block", "T-0001", {"category": "core", "detail": "y"})
        second = self.digest(DAY2)
        self.assertIn("system 3 (+1)", second)
        self.assertIn("core 1 (new)", second)
        self.assertIn("against 2026-", second)
        self.assertEqual(len(self.baseline_lines()), 2)
        self.digest(DAY2)
        self.assertEqual(len(self.baseline_lines()), 2)
        again = json.loads(self.digest(DAY2, "--json"))["flow"]
        self.assertEqual(again["baseline"]["guard"]["system"], 2)  # the delta is against the earlier day, not today's own

    def test_biggest_number_first(self):
        self.seed()
        text = self.digest(DAY1)
        self.assertLess(text.index("system 2"), text.index("brief 1"))

    def test_a_task_with_no_stage_events_is_counted_closed_and_its_stages_unread(self):
        self.ev("2026-11-20T10:00:00Z", "task_done", "T-0007", {})
        flow = json.loads(self.digest(DAY1, "--json"))["flow"]
        self.assertEqual(flow["stages"]["build"]["n"], 0)
        self.assertIsNone(flow["stages"]["build"]["median_s"])
        self.assertEqual(flow["made_closed"], [{"day": "2026-11-20", "made": 0, "closed": 1}])
        self.assertIn("unread", self.digest(DAY1))

    def test_a_damaged_baseline_line_is_skipped_and_never_fails_the_digest(self):
        self.seed()
        self.digest(DAY1)
        with open(os.path.join(self.p.dir, "flow-baseline.jsonl"), "a") as f:
            f.write("not json\n[1,2]\n")
        self.assertIn("system 2", self.digest(DAY2))
        self.assertEqual(flowreport.last_before(self.p, "2026-11-21", 7.0)["day"], "2026-11-20")

    def test_a_different_window_does_not_use_another_windows_snapshot(self):
        self.seed()
        self.digest(DAY1)
        text = self.digest(DAY2, "--days", "3")
        self.assertIn("first snapshot", text)
        self.assertEqual(len(self.baseline_lines()), 2)
