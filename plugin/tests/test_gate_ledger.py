"""T-5716 (T-5704 part d): `sampo outcomes --gates`, the gate ledger. Per gate and process tier: the runs, seconds and
catches over a window (14 days), read from the close, finding, panel and refusal events the ledger already holds. A gate
with no writer is named as not recorded; a gate with runs and no catch is a skip candidate, and nothing is skipped."""
import time

from helpers import ForemanTestCase

from sampo import core as c
from sampo import outcomes


class GateLedger(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def brief(self, title, tier):
        self.fm("task", "new", title, "--type", "FEATURE", "--tier", tier, "--ac", "ok :: true", "--step", "s")
        return c.load_briefs(self.p)[-1].id

    def event(self, kind, tid, **data):
        c.log_event(self.p, kind, task=tid, data=data)

    def ptier(self, tid):
        return c.ptier_now(c.find_brief(self.p, tid))[0]

    def seed(self, a, b):
        """Task a closes at tier S with five clean affected runs and one confirmed and one rejected finding; task b
        closes at M with one red affected run, an unadopted panel, a plan refusal and a close refusal by gap kind."""
        self.event("close_steps", a, s=100.0, steps={"verify": 60.0, "affected": 30.0, "advice": 2.0, "done": 1.0,
                                                     "commit": 2.0, "other": 5.0}, notes={})
        for _ in range(5):
            self.event("close_affected", a, cmd="pytest", exit=0, failing=[], s=6.0)
        self.event("finding", a, lens="adversary", verdict="confirmed", text="x")
        self.event("finding", a, lens="adversary", verdict="rejected", text="y")
        self.event("panel_outcome", a, items=2, adopted=1, tier="S")
        self.event("close_steps", b, s=50.0, steps={"verify": 40.0, "affected": 5.0, "other": 5.0}, notes={})
        self.event("close_affected", b, cmd="pytest", exit=1, failing=["t"], s=5.0)
        self.event("panel_outcome", b, items=1, adopted=0, tier="M")
        self.event("plan_gate_refused", b, kinds=["criteria"], where="focus")
        self.event("gate_refused", b, gate="close:lesson", where="finish", why="no LESSON line")

    def test_the_gate_rows_carry_runs_seconds_and_catches_per_process_tier(self):
        a = self.brief("Add session cache", "S")
        b = self.brief("Rewrite the export module", "M")
        self.assertNotEqual(self.ptier(a), self.ptier(b), "the two tasks must sit in two process tiers")
        self.seed(a, b)
        rep = outcomes.gate_report(self.p)
        rows = {(r["gate"], r["ptier"]): r for r in rep["rows"]}
        pa, pb = self.ptier(a), self.ptier(b)
        self.assertEqual((rows[("verify", pa)]["runs"], rows[("verify", pa)]["seconds"]), (1, 60.0))
        self.assertEqual((rows[("affected", pb)]["runs"], rows[("affected", pb)]["catches"]), (1, 1))
        self.assertEqual((rows[("affected", pa)]["runs"], rows[("affected", pa)]["catches"]), (5, 0))
        self.assertEqual((rows[("audit", pa)]["runs"], rows[("audit", pa)]["catches"]), (2, 1))
        self.assertEqual((rows[("panel", pb)]["runs"], rows[("panel", pb)]["catches"]), (1, 0))
        self.assertEqual((rows[("plan", pb)]["runs"], rows[("plan", pb)]["catches"]), (1, 1))
        self.assertEqual((rows[("close:lesson", pb)]["runs"], rows[("close:lesson", pb)]["catches"]), (1, 1))

    def test_a_waived_reused_or_unjudged_close_or_finding_is_no_run(self):  # T-5716 review: no fake runs or skip candidates
        a = self.brief("Add session cache", "S")
        self.event("close_affected", a, cmd="pytest", waived="flaky")
        self.event("close_affected", a, cmd="pytest", exit=0, failing=[], s=0.0, cached=True)
        self.event("close_affected", a, cmd="pytest", exit=0, reused="T-0001")
        self.event("close_affected", a, cmd="pytest", sized="too big")
        self.event("close_affected", a, cmd="pytest", exit=0, failing=[], s=6.0)  # the one real run
        self.event("finding", a, lens="adversary", verdict="reported", text="maybe")
        c.log_event(self.p, "finding", task=a, data={"kind": "tripwire", "verdict": "reported", "flag": "x"})
        c.log_event(self.p, "finding", task=a, data={"kind": "tripwire", "verdict": "confirmed", "lens": "edge"})
        self.event("finding", a, lens="edge", verdict="confirmed", text="y")
        rows = {(r["gate"], r["ptier"]): r for r in outcomes.gate_report(self.p)["rows"]}
        pa = self.ptier(a)
        self.assertEqual((rows[("affected", pa)]["runs"], rows[("affected", pa)]["catches"]), (1, 0))
        self.assertEqual((rows[("audit", pa)]["runs"], rows[("audit", pa)]["catches"]), (1, 1))

    def test_a_clean_lens_audit_is_an_audit_run_with_a_recorded_zero_catch(self):  # T-5716 review: ok_audit's lines count
        a = self.brief("Add session cache", "M")
        self.event("audit", a, lens="intent", how="fm audit run", result="ok")
        self.event("audit", a, lens="edge", how="fm audit run", result="changes needed")  # a finding event counts instead
        rows = {(r["gate"], r["ptier"]): r for r in outcomes.gate_report(self.p)["rows"]}
        pa = self.ptier(a)
        self.assertEqual((rows[("audit", pa)]["runs"], rows[("audit", pa)]["catches"]), (1, 0))

    def test_a_gate_with_no_writer_is_named_not_recorded_and_a_catch_never_reads_as_zero(self):
        a = self.brief("Add session cache", "S")
        b = self.brief("Rewrite the export module", "M")
        self.seed(a, b)
        rep = outcomes.gate_report(self.p)
        self.assertIn("rival read: not recorded (no writer)", "\n".join(rep["lines"]))
        verify = [r for r in rep["rows"] if r["gate"] == "verify"]
        self.assertTrue(verify)
        for r in verify:  # a close step has no catch writer: its catches read as not recorded, never as zero
            self.assertIsNone(r["catches"])

    def test_a_gate_with_runs_and_no_catch_is_a_skip_candidate_and_nothing_is_skipped(self):
        a = self.brief("Add session cache", "S")
        b = self.brief("Rewrite the export module", "M")
        self.seed(a, b)
        text = "\n".join(outcomes.gate_report(self.p)["lines"])
        self.assertIn("no catch in 5 runs over 14 days: a skip candidate (nothing is skipped)", text)
        out = self.fm("outcomes", "--gates").stdout
        self.assertIn("no catch in 5 runs over 14 days", out)
        self.assertIn("nothing is skipped", out)

    def test_the_window_leaves_out_events_older_than_14_days(self):
        from unittest import mock
        a = self.brief("Add session cache", "S")
        b = self.brief("Rewrite the export module", "M")
        self.seed(a, b)
        old = c.iso(time.time() - 20 * 86400)
        with mock.patch.object(c, "now", return_value=old):  # a red run stamped 20 days back
            c.log_event(self.p, "close_affected", task=a, data={"cmd": "pytest", "exit": 1, "failing": [], "s": 1.0})
        rows = {(r["gate"], r["ptier"]): r for r in outcomes.gate_report(self.p)["rows"]}
        self.assertEqual((rows[("affected", self.ptier(a))]["runs"], rows[("affected", self.ptier(a))]["catches"]),
                         (5, 0))
        self.assertEqual(outcomes.gate_report(self.p, days=30)["window_days"], 30)

    def test_a_plan_refusal_logs_one_gate_event(self):
        tid = self.brief("Add session cache", "S")
        from unittest import mock
        from sampo import cli
        with mock.patch.object(c, "plan_gaps", return_value=["step"]):
            with self.assertRaises(c.PolicyError) as refused:
                cli._focus_refusal(self.p, c.find_brief(self.p, tid))
        self.assertIn("isn't planned enough to start", str(refused.exception))
        ev = c.ledger_events(self.p, ("plan_gate_refused",))
        self.assertEqual([(e.get("task"), e["data"]["where"], e["data"]["kinds"]) for e in ev],
                         [(tid, "focus", [c.gap_kind("step")])])

    def test_a_lane_brief_plan_refusal_logs_one_gate_event(self):
        self.fm("task", "new", "Add session cache", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true")
        tid = c.load_briefs(self.p)[-1].id  # no --step: the plan has a gap the lane brief refuses
        r = self.fm("lane", "brief", tid, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        ev = c.ledger_events(self.p, ("plan_gate_refused",))
        self.assertEqual([(e.get("task"), e["data"]["where"]) for e in ev], [(tid, "lane")])

    def test_a_close_refusal_logs_one_gate_event_per_gap_kind(self):
        self.fm("task", "new", "Add session cache", "--type", "FEATURE", "--tier", "S", "--ac", "ok", "--step", "s")
        tid = c.load_briefs(self.p)[-1].id  # a criterion with no verify command: a missing close line
        self.fm("focus", tid, check=False)
        r = self.fm("task", "finish", tid, "--audit", "self", check=False)  # no --run: the criterion has no verify
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        gates = [e["data"]["gate"] for e in c.ledger_events(self.p, ("gate_refused",)) if e.get("task") == tid]
        self.assertTrue(gates, "a refused close logs its gate")
        self.assertTrue(all(g.startswith("close:") for g in gates), gates)
        self.assertEqual(len(gates), len(set(gates)), "one event per gap kind, not per gap")

    def test_an_accept_refusal_logs_one_gate_event(self):
        from types import SimpleNamespace
        from unittest import mock
        from sampo import train
        tid = self.brief("Add session cache", "S")
        b = c.find_brief(self.p, tid)
        b.meta["lane"] = self.repo  # any existing dir: the gaps are stubbed
        args = SimpleNamespace(no_check=False, audit=[], json=False)
        with mock.patch.object(train, "_audit_rows", return_value=[]), \
                mock.patch.object(train, "_lane_tip", return_value="abc"), \
                mock.patch.object(train, "_clean_run", return_value=True), \
                mock.patch.object(train, "_counted_lenses", return_value=set()), \
                mock.patch.object(train, "_accept_gaps", return_value=["no audit"]):
            with self.assertRaises(Exception):
                train.accept(self.p, b, self.repo, args)
        ev = c.ledger_events(self.p, ("gate_refused",))
        self.assertEqual([(e.get("task"), e["data"]["gate"], e["data"]["where"]) for e in ev],
                         [(tid, "accept:gaps", "accept")])

    def test_an_accept_with_no_lane_logs_one_gate_event(self):
        from types import SimpleNamespace
        from sampo import train
        tid = self.brief("Add session cache", "S")
        b = c.find_brief(self.p, tid)  # no lane bound: accept has nothing to review
        with self.assertRaises(c.PolicyError):
            train.accept(self.p, b, self.repo, SimpleNamespace(no_check=False, audit=[], json=False))
        ev = c.ledger_events(self.p, ("gate_refused",))
        self.assertEqual([(e.get("task"), e["data"]["gate"], e["data"]["where"]) for e in ev],
                         [(tid, "accept:no-lane", "accept")])

    def test_an_accept_no_check_without_a_clean_run_logs_one_gate_event(self):
        from types import SimpleNamespace
        from unittest import mock
        from sampo import train
        tid = self.brief("Add session cache", "S")
        b = c.find_brief(self.p, tid)
        b.meta["lane"] = self.repo  # any existing dir: the run is stubbed as not clean
        args = SimpleNamespace(no_check=True, audit=[], json=False)
        with mock.patch.object(train, "_audit_rows", return_value=[]), \
                mock.patch.object(train, "_lane_tip", return_value="abc"), \
                mock.patch.object(train, "_clean_run", return_value=False):
            with self.assertRaises(c.PolicyError):
                train.accept(self.p, b, self.repo, args)
        ev = c.ledger_events(self.p, ("gate_refused",))
        self.assertEqual([(e.get("task"), e["data"]["gate"], e["data"]["where"]) for e in ev],
                         [(tid, "accept:no-check", "accept")])

    def test_an_accept_that_queues_nothing_logs_one_gate_event(self):  # T-5716 review: the not-queued path is a refusal too
        from types import SimpleNamespace
        from unittest import mock
        from sampo import lanes, train
        tid = self.brief("Add session cache", "S")
        b = c.find_brief(self.p, tid)
        b.meta["lane"] = self.repo  # an existing dir, but no awaiting merge: the accept queues nothing
        args = SimpleNamespace(no_check=False, audit=[], json=True)
        with mock.patch.object(train, "_audit_rows", return_value=[]), \
                mock.patch.object(train, "_lane_tip", return_value="abc"), \
                mock.patch.object(train, "_close_gaps", return_value=[]), \
                mock.patch.object(lanes, "awaiting_merge", return_value=[]):
            train.accept(self.p, b, self.repo, args)
        ev = c.ledger_events(self.p, ("gate_refused",))
        self.assertEqual([(e.get("task"), e["data"]["gate"], e["data"]["where"]) for e in ev],
                         [(tid, "accept:not-queued", "accept")])

    def test_an_accept_with_a_mistyped_audit_logs_one_gate_event(self):  # T-5716 review: _audit_rows refuses first
        from types import SimpleNamespace
        from sampo import cli, train
        tid = self.brief("Add session cache", "S")
        b = c.find_brief(self.p, tid)
        b.meta["lane"] = self.repo
        args = SimpleNamespace(no_check=False, audit=[("intent", "--no-check", "ok")], more=[], json=False)
        with self.assertRaises(cli.UsageError):
            train.accept(self.p, b, self.repo, args)
        ev = c.ledger_events(self.p, ("gate_refused",))
        self.assertEqual([(e.get("task"), e["data"]["gate"], e["data"]["where"]) for e in ev],
                         [(tid, "accept:audit", "accept")])

    def test_outcomes_gates_days_sets_the_window_through_the_cli(self):
        from unittest import mock
        a = self.brief("Add session cache", "S")
        b = self.brief("Rewrite the export module", "M")
        self.seed(a, b)
        old = c.iso(time.time() - 20 * 86400)
        with mock.patch.object(c, "now", return_value=old):  # a red run 20 days back: outside 14, inside 30
            c.log_event(self.p, "close_affected", task=a, data={"cmd": "pytest", "exit": 1, "failing": [], "s": 1.0})
        row = f"affected · {self.ptier(a)}: runs "
        narrow = self.fm("outcomes", "--gates").stdout
        wide = self.fm("outcomes", "--gates", "--days", "30").stdout
        self.assertIn(row + "5, ", narrow)
        self.assertIn("Gates over 30 days", wide)
        self.assertIn(row + "6, ", wide)
        self.assertNotEqual(self.fm("outcomes", "--gates", "--days", "0", check=False).returncode, 0)

    def test_a_ledger_line_whose_data_is_not_an_object_is_skipped(self):
        from unittest import mock
        a = self.brief("Add session cache", "S")
        self.event("close_affected", a, cmd="pytest", exit=1, failing=[], s=1.0)
        junk = [{"event": "close_affected", "task": a, "data": ["x"]},
                {"event": "close_steps", "task": a, "data": "text"},
                {"event": "gate_refused", "task": a, "data": 7},
                {"event": "finding", "task": a, "data": "confirmed"}]
        real = c.ledger_events
        with mock.patch.object(c, "ledger_events", side_effect=lambda p, kinds, **kw: [*junk, *real(p, kinds, **kw)]):
            rows = {(r["gate"], r["ptier"]): r for r in outcomes.gate_report(self.p)["rows"]}
        self.assertEqual((rows[("affected", self.ptier(a))]["runs"], rows[("affected", self.ptier(a))]["catches"]),
                         (1, 1))

    def test_a_task_counts_in_the_tier_its_close_stamped_and_works_with_the_tier_off(self):
        import os
        from unittest import mock
        from sampo import cli
        a = self.brief("Add session cache", "S")
        b = self.brief("Rewrite the export module", "M")
        self.seed(a, b)
        with mock.patch.dict(os.environ):  # the harness runs with the tier off, which stamps nothing: stamp with it on
            os.environ.pop("FOREMAN_PROCESS_TIER", None)
            cli.mutate(self.p, a, lambda x: c.stamp_ptier(x, "risky", ["stamped by its close"]), "ptier", {"ptier": "risky"})
        rows = {(r["gate"], r["ptier"]): r for r in outcomes.gate_report(self.p)["rows"]}
        self.assertIn(("affected", "risky"), rows, sorted(rows))
        self.assertEqual((rows[("affected", "risky")]["runs"], rows[("affected", "risky")]["catches"]), (5, 0))
        with mock.patch.dict(os.environ, {"FOREMAN_PROCESS_TIER": "off"}):
            off = outcomes.gate_report(self.p)
        self.assertTrue(all(t in c.PTIERS or t in ("?", "-") for _, t in ((r["gate"], r["ptier"]) for r in off["rows"])))
        self.assertIn("affected", "\n".join(off["lines"]))

    def test_the_outcomes_gates_flag_prints_the_report_beside_panel(self):
        a = self.brief("Add session cache", "S")
        self.seed(a, a)
        out = self.fm("outcomes", "--gates").stdout
        self.assertIn("verify", out)
        self.assertIn("runs", out)
        self.assertIn("catches", out)
