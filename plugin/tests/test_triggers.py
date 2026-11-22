"""T-2276: fm triggers counts each trigger's captures by outcome; a trigger whose closed captures are mostly dropped
files at low priority; urgent triggers and triggers with too few closes are left alone."""
import os

from helpers import ForemanTestCase

from sampo import core as c
from sampo import eco


class Triggers(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.n = 0

    def fire(self, trigger, status=None, **kw):
        self.n += 1
        b, new = eco.capture_once(self.p, f"FIX: {trigger} finding {self.n}", "FIX", "discovered", trigger=trigger, **kw)
        self.assertTrue(new)
        if status:
            b.meta["status"] = status
            c.save_brief(self.p, b, touch=False)
        return b

    def rows(self):
        return {r["trigger"]: r for r in self.fm_json("triggers")["triggers"]}

    def test_counts_per_trigger_from_captures_and_their_final_status(self):
        for st in ["done", "done", "done", "dropped", "dropped", None]:
            self.fire("scout", st)
        self.fire("spec", "done")
        r = self.rows()
        self.assertEqual({k: r["scout"][k] for k in ("fired", "done", "dropped", "open")},
                         {"fired": 6, "done": 3, "dropped": 2, "open": 1})
        self.assertAlmostEqual(r["scout"]["precision"], 0.6)
        self.assertEqual(r["spec"]["precision"], 1.0)

    def test_trigger_defaults_to_the_source_and_a_user_capture_is_not_a_trigger(self):
        b, _ = eco.capture_once(self.p, "FIX: from a sweep", "FIX", "cross-project")
        eco.capture_once(self.p, "FIX: typed by hand", "FIX", "user")
        r = self.rows()
        self.assertEqual(r["cross-project"]["fired"], 1)
        self.assertNotIn("user", r)
        self.assertIsNone(r["cross-project"]["precision"])  # nothing closed yet

    def test_mostly_dropped_trigger_files_at_low_priority_with_a_note(self):
        for _ in range(5):
            self.fire("noisy", "dropped")
        b = self.fire("noisy")
        self.assertEqual(b.priority, "low")
        self.assertIn("precision 0.00", b.section("Raw request"))
        self.assertEqual(c.find_brief(self.p, b.id).priority, "low")

    def test_few_closed_captures_or_good_precision_stay_normal(self):
        for _ in range(4):
            self.fire("young", "dropped")
        self.assertEqual(self.fire("young").priority, "normal")
        for st in ["done", "done", "dropped", "dropped", "dropped"]:  # 0.4
            self.fire("mixed", st)
        self.assertEqual(self.fire("mixed").priority, "normal")

    def test_urgent_triggers_are_never_demoted(self):
        for _ in range(6):
            self.fire("baseline", "dropped")
        self.assertEqual(self.fire("baseline").priority, "normal")

    def test_state_names_the_noisiest_trigger_and_a_second_state_reloads_no_briefs(self):
        import unittest.mock as mock

        from sampo import triggers
        for _ in range(5):
            self.fire("noisy", "dropped")
        self.assertEqual(triggers.noisiest(self.p)["trigger"], "noisy")
        real = c.load_briefs
        with mock.patch.object(c, "load_briefs", side_effect=lambda *a, **k: real(*a, **k)) as m:
            self.assertEqual(triggers.noisiest(self.p)["trigger"], "noisy")
            self.assertEqual(m.call_count, 0)
        self.assertIn("Noisiest trigger: noisy", self.fm("state").stdout)

    def test_noisiest_uses_the_rule_that_demotes_the_last_20_closed(self):
        from sampo import triggers
        for _ in range(40):
            self.fire("healed", "dropped")
        for _ in range(10):
            self.fire("healed", "done")
        for _ in range(10):
            self.fire("healed", "dropped")  # last 20: 0.5, though over all 60 it is 0.17
        self.assertIsNone(triggers.noisiest(self.p))
        self.assertEqual(self.fire("healed").priority, "normal")

    def test_a_failing_precision_read_never_loses_the_capture(self):
        import unittest.mock as mock
        with mock.patch("sampo.triggers.noisy", side_effect=RuntimeError("boom")):
            b = self.fire("any")
        self.assertEqual(b.priority, "normal")

    def test_fm_triggers_is_read_only_and_days_is_clamped(self):
        import os
        self.fire("scout", "done")
        r = self.fm_json("triggers", "--days", "0")
        self.assertEqual(r["days"], 1)
        self.assertEqual(self.fm_json("triggers", "--days", "999999")["days"], 3650)
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, "triggers.json")))

    def test_ad_hoc_sources_count_as_triggers(self):
        c.log_event(self.p, "capture", task="T-0099", data={"source": "sentinel"})  # as fm sentinel logs it
        self.fm("capture", "a follow-up", "--source", "followup")
        r = self.rows()
        self.assertEqual((r["sentinel"]["fired"], r["followup"]["fired"]), (1, 1))

    def test_a_recovered_trigger_climbs_back(self):
        for _ in range(5):
            self.fire("flaky", "dropped")
        for _ in range(20):  # the last 20 closed ones decide
            self.fire("flaky", "done")
        self.assertEqual(self.fire("flaky").priority, "normal")


class TriggerHeldFixes(ForemanTestCase):
    """T-2351: a held self-fix counts toward its trigger only once fm outcomes shows the task survived 30 days."""
    def test_a_held_fix_counts_only_with_outcomes_survival(self):
        import datetime
        import json
        from unittest import mock
        from sampo import outcomes
        from sampo import trials
        self.fm("init")
        p = c.find_project(self.repo)
        b = eco.capture_once(p, "FIX: CI red\nCONTEXT: u, ", "FIX", "ci", key="CONTEXT: u, ")[0]
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(p.dir, "trials.json"), "w") as f:
            json.dump([{"what": f"Self-fix {b.id}", "kind": "self-fix", "task": b.id, "trigger": "ci",
                        "key": b.meta["trigger_key"], "at": old, "by": 14, "focus": old, "verdict": "held"}], f)
        with mock.patch.object(outcomes, "outcomes", lambda p: {b.id: {"fate": "held", "survival": "settling"}}):
            self.assertEqual(trials.held_fixes(p), {})
        with mock.patch.object(outcomes, "outcomes", lambda p: {b.id: {"fate": "held", "survival": "survived"}}):
            self.assertEqual(trials.held_fixes(p), {"ci": 1})
        n = len(c.ledger_events(p))
        self.fm("triggers")
        self.assertEqual(len(c.ledger_events(p)), n)  # fm triggers stays read-only


class TriggerTable(ForemanTestCase):
    """T-2355: one table routes every trigger's captures (inbox, queue, lane, muted) with a tier cap, a daily budget and
    a burst window; Foreman moves a route only on outcomes it didn't author, each move an fm decide row."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.n = 0

    def fire(self, trigger, tier_text="", source="discovered"):
        self.n += 1
        return eco.capture_once(self.p, f"FIX: {trigger} finding {self.n}{tier_text}", "FIX", source, trigger=trigger)

    def test_every_trigger_has_a_row_and_the_defaults_change_nothing(self):
        from sampo import triggers
        self.fire("scout")
        row = triggers.table(self.p)["scout"]
        self.assertEqual({k: row[k] for k in ("route", "tier_cap", "budget", "burst")},
                         {"route": "inbox", "tier_cap": "M", "budget": None, "burst": 0})
        rows = {r["trigger"]: r for r in self.fm_json("triggers")["triggers"]}
        self.assertEqual(rows["scout"]["route"], "inbox")
        for _ in range(12):  # no budget, no burst: every distinct capture files
            self.assertTrue(self.fire("scout")[1])

    def test_past_its_daily_budget_or_inside_its_burst_a_trigger_adds_to_its_open_capture(self):
        self.fm("triggers", "set", "scout", "--budget", "2")
        first, second = self.fire("scout")[0], self.fire("scout")[0]
        b, new = self.fire("scout")
        self.assertFalse(new)
        self.assertEqual(b.id, second.id)  # the newest open capture of that trigger takes the firing
        self.assertIn("fired again", b.section("Log"))
        self.fm("triggers", "set", "lint", "--burst", "600")
        self.assertTrue(self.fire("lint")[1])
        self.assertFalse(self.fire("lint")[1])  # within 600 s of the last filing

    def test_urgent_triggers_have_no_budget_and_are_never_muted(self):
        from sampo import triggers
        self.fm("triggers", "set", "ci-red", "--budget", "1")
        self.assertTrue(all(self.fire("ci-red")[1] for _ in range(3)))
        self.assertNotEqual(triggers.decide_route(self.p, "ci-red", closed=["dropped"] * 12, held=0)[0], "muted")

    def test_the_route_rules_mute_demote_promote_in_order(self):
        from sampo import triggers
        r = triggers.decide_route
        self.assertEqual(r(self.p, "x", closed=["dropped"] * 10 + ["done"] * 0, held=0)[0], "muted")  # under 0.1 over 10+
        self.assertEqual(r(self.p, "x", closed=["dropped"] * 4 + ["done"], held=0)[0], "inbox-low")  # under 0.3 over 5+
        self.assertEqual(r(self.p, "x", closed=["done"] * 10, held=7)[0], "queue")  # 0.7 of 10+ closed S/M held
        self.assertEqual(r(self.p, "x", closed=["done"] * 10, held=6)[0], "inbox")
        self.assertEqual(r(self.p, "x", closed=["done"] * 9, held=9)[0], "inbox")  # under 10 closed

    def test_a_route_move_is_a_decide_row_once_and_a_queued_capture_is_lane_only(self):
        from sampo import triggers
        from unittest import mock
        caps = [self.fire("scout")[0] for _ in range(10)]
        for b in caps:
            b.meta["status"] = "done"
            c.save_brief(self.p, b, touch=False)
        outs = {b.id: {"fate": "held", "survival": "survived"} for b in caps}
        with mock.patch("sampo.outcomes.outcomes", lambda p: outs):
            moved = triggers.settle_routes(self.p)
            self.assertEqual(moved, {"scout": "queue"})
            self.assertEqual(triggers.settle_routes(self.p), {})  # nothing new: no second row
        rows = [t for _, _, t in c._decision_rows(self.p) if "scout" in t]
        self.assertEqual(len(rows), 1, rows)
        b, new = self.fire("scout")
        self.assertTrue(new)
        self.assertTrue(b.meta.get("lane_only"))
        big, _ = self.fire("scout", " (L: a cross-cutting rewrite)")
        big.meta["tier"] = "L"
        self.assertTrue(triggers.lane_only(self.p, "scout", "L", "FIX") is False)  # an L stays in the inbox
        self.assertFalse(triggers.lane_only(self.p, "scout", "S", "SECURITY"))  # so does SECURITY
        r = self.fm("focus", b.id, check=False)  # on main: refused, it starts in a lane
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"sampo lane brief {b.id}", r.stderr)
        b = next(x for x in c.load_briefs(self.p) if x.id == b.id)
        b.meta["lane"] = os.path.join(self.repo, "lane")  # after fm lane brief: main focuses it to merge and close
        c.save_brief(self.p, b, touch=False)
        r = self.fm("focus", b.id, check=False)
        self.assertNotIn("lane brief", r.stderr)  # the lane gate passes (the plan gate still wants criteria)

    def test_a_muted_trigger_adds_to_one_open_capture(self):
        import json
        import os
        with open(os.path.join(self.p.dir, "trigger-routes.json"), "w") as f:
            json.dump({"noise": "muted"}, f)
        first, new = self.fire("noise")
        self.assertTrue(new)  # the one capture a muted trigger keeps, so its firings stay counted
        b, new = self.fire("noise")
        self.assertFalse(new)
        self.assertEqual(b.id, first.id)
        first.meta["status"] = "dropped"  # closed: a muted trigger still files nothing new
        c.save_brief(self.p, first, touch=False)
        b, new = self.fire("noise")
        self.assertFalse(new)
        self.assertEqual(b.id, first.id)
        self.assertEqual(sum(1 for x in c.load_briefs(self.p) if x.meta.get("trigger") == "noise"), 1)


class UnlimitedBudget(ForemanTestCase):
    """T-5897: --budget unlimited is a user's budget with no cap: allowance() is None, nothing is held or folded for it."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.n = 0

    def fire(self, trigger):
        self.n += 1
        return eco.capture_once(self.p, f"FIX: {trigger} finding {self.n}", "FIX", "discovered", trigger=trigger)

    def test_unlimited_is_stored_and_allowance_is_none(self):
        from sampo import triggers
        self.fm("triggers", "set", "discovered", "--budget", "unlimited")
        self.assertEqual(triggers.row(self.p, "discovered")["budget"], "unlimited")
        self.assertIsNone(triggers.allowance(self.p, "discovered"))
        self.assertIsNone(triggers._room(self.p, "discovered", triggers._local_now()))
        self.assertIn("unlimited", self.fm("triggers", "set", "discovered", "--budget", "unlimited").stdout)

    def test_an_unlimited_trigger_never_files_into_its_open_capture(self):
        self.fm("triggers", "set", "scout", "--budget", "unlimited")
        self.assertTrue(all(self.fire("scout")[1] for _ in range(6)))

    def test_held_says_unlimited_not_none(self):
        import types
        from unittest import mock
        from sampo import triggers
        self.fm("triggers", "set", "discovered", "--budget", "unlimited")
        shown = []
        cli = types.SimpleNamespace(out=lambda args, data, text: shown.append(text))
        item = {"source": "discovered", "type": "FIX", "text": "a held finding", "held_at": "2026-01-01T00:00:00Z"}
        with mock.patch.object(triggers, "held", lambda p: [item]):
            triggers.cmd_held(self.p, types.SimpleNamespace(), cli)
        self.assertIn("discovered unlimited", shown[0])
        self.assertNotIn("None", shown[0])

    def test_a_number_still_caps_and_zero_still_clears(self):
        from sampo import triggers
        self.fm("triggers", "set", "discovered", "--budget", "7")
        self.assertEqual(triggers.allowance(self.p, "discovered"), 7)
        self.fm("triggers", "set", "discovered", "--budget", "unlimited")
        self.fm("triggers", "set", "discovered", "--budget", "0")
        self.assertEqual(triggers.allowance(self.p, "discovered"), triggers.DAILY_MAX)

    def test_a_bad_budget_word_is_refused(self):
        r = self.fm("triggers", "set", "discovered", "--budget", "lots", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_a_hand_edited_row_with_another_word_is_dropped(self):
        from sampo import triggers
        self.assertNotIn("budget", triggers._override({"budget": "lots"}))
        self.assertEqual(triggers._override({"budget": "unlimited"})["budget"], "unlimited")


class MutedTriggerDoctor(ForemanTestCase):
    def test_fm_doctor_names_each_muted_trigger_with_its_unmute_command(self):  # T-2355
        import json
        import os
        from sampo import doctor
        self.fm("init")
        p = c.find_project(self.repo)
        self.assertEqual(doctor.check_muted_triggers(p).status, "PASS")
        with open(os.path.join(p.dir, "trigger-routes.json"), "w") as f:
            json.dump({"noise": "muted"}, f)
        r = doctor.check_muted_triggers(p)
        self.assertEqual(r.status, "WARN")
        self.assertIn("fm triggers set noise --route inbox", r.detail)
