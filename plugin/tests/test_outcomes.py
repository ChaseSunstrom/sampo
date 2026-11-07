"""T-0689 (Intelligence — learning from outcomes): each finished task's fate (reverted, fixed later, held), lessons with
ids and a record of when they were shown, which audit lenses and passes find anything, a track record by type and tier,
bench cases that never discriminate, blocked tasks reopened when what they waited on changed, and process mining."""
import json
import os
import re
import subprocess
import time

from helpers import ForemanTestCase

from sampo import core as c


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True).stdout

    def commit(self, msg, name="x.py", text=None):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text if text is not None else msg + "\n")
        self.git("add", "-A")
        self.git("commit", "-qm", msg)

    def done(self, title, typ="FEATURE", run="true", *extra):
        self.fm("task", "new", title, "--type", typ, "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        tid = c.load_briefs(self.p)[-1].id
        self.fm("task", "finish", tid, "--audit", "self", "--run", run, *extra)
        return tid

    def age(self, tid, days):
        """Backdate a brief (its updated stamp and log) by days."""
        b = c.find_brief(self.p, tid)
        old = c.iso(time.time() - days * 86400)
        with open(b.path, encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r"(?m)^updated: .*$", f"updated: {old}", text)
        text = re.sub(r"(?m)^- \d{4}-\d\d-\d\dT[\d:]+Z done$", f"- {old} done", text)
        with open(b.path, "w", encoding="utf-8") as f:
            f.write(text)


class Outcomes(Base):
    def test_a_reverted_task_and_one_a_later_fix_names_get_their_fate(self):
        a = self.done("Add session cache")
        self.commit(f"Add session cache ({a})")
        self.git("revert", "--no-edit", "HEAD")
        b = self.done("Add export button")
        self.fm("capture", f"Export button crashes on empty table (from {b})", "--type", "FIX", "--tier", "S")
        held = self.done("Rename module")
        out = self.fm("outcomes").stdout
        self.assertRegex(out, rf"{a}\b.*reverted")
        self.assertRegex(out, rf"{b}\b.*fixed later.*T-0003")
        self.assertNotRegex(out, rf"{held}\b.*(reverted|fixed later)")
        data = json.loads(self.fm("outcomes", "--json").stdout)
        self.assertEqual(data["tasks"][held]["fate"], "held")


class KeptReverts(Base):
    """T-3692: reverts' answer is kept while git's refs stand still (core.git_stamp), so a focus's three asks run no
    git; a new Revert commit, another n or another checkout's root asks again."""

    def gits(self, *args):
        from unittest import mock
        from sampo import outcomes
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as run:
            got = outcomes.reverts(*args)
        return got, sum(1 for call in run.call_args_list if call.args and call.args[0][:1] == ["git"])

    def test_reverts_are_kept_until_the_refs_move(self):
        self.commit("Add session cache (T-0009)")
        self.git("revert", "--no-edit", "HEAD")
        first, ran = self.gits(self.p)
        self.assertEqual(ran, 1)
        self.assertEqual([(r[2], r[3]) for r in first], [("Add session cache (T-0009)", {"T-0009"})])
        again, ran = self.gits(self.p)
        self.assertEqual((again, ran), (first, 0))
        self.assertIs(type(again[0][3]), set)
        self.assertEqual(self.gits(self.p, 1), (first, 1))  # another n is its own answer
        self.assertEqual(self.gits(self.p, 1), (first, 0))
        self.commit("Add export (T-0010)", name="y.py")
        self.git("revert", "--no-edit", "HEAD")
        moved, ran = self.gits(self.p)
        self.assertEqual(ran, 1)
        self.assertEqual([r[3] for r in moved], [{"T-0010"}, {"T-0009"}])

    def test_reverts_key_on_the_checkout_root_and_survive_a_bad_kept_file(self):
        import types
        self.commit("Base")
        wt = os.path.join(self.tmp, "wt")
        self.git("worktree", "add", "-q", "--detach", wt, "HEAD")
        self.commit("Add session cache (T-0009)")
        self.git("revert", "--no-edit", "HEAD")
        self.assertEqual(len(self.gits(self.p)[0]), 1)
        other = types.SimpleNamespace(root=wt, dir=self.p.dir)
        self.assertEqual(self.gits(other), ([], 1))  # the same stamp, but the other checkout's HEAD has no revert
        kept = os.path.join(self.p.dir, "cache", "reverts.json")
        for bad in ("{", "[]", json.dumps({"stamp": c.git_stamp(self.repo), "answers": {f"{self.repo}\0200": [[1]]}})):
            with open(kept, "w") as f:
                f.write(bad)
            got, ran = self.gits(self.p)
            self.assertEqual((len(got), ran), (1, 1), bad)
        outside = types.SimpleNamespace(root=os.path.join(self.tmp, "nogit"), dir=self.p.dir)
        os.makedirs(outside.root)
        self.assertEqual(self.gits(outside)[0], [])


class Survival(Base):
    """T-1060 (wild C1): each close graded by a 30-day window: survived, lost (and why, when), settling (days left);
    a settle event per grade, the digest's line, the pane's history, and the reports that read the grades."""
    def created(self, tid, days):
        b = c.find_brief(self.p, tid)
        old = c.iso(time.time() - days * 86400)
        with open(b.path, encoding="utf-8") as f:
            text = f.read()
        with open(b.path, "w", encoding="utf-8") as f:
            f.write(re.sub(r"(?m)^created: .*$", f"created: {old}", text))

    def setup_five(self):
        ok = self.done("Old and fine")
        self.age(ok, 40)
        late = self.done("Old, fixed late")
        self.age(late, 40)
        self.fm("capture", f"Late fix for {late}", "--type", "FIX", "--tier", "S")  # 40 days on: outside the window
        early = self.done("Old, fixed early")
        self.age(early, 40)
        self.fm("capture", f"Crash after {early}", "--type", "FIX", "--tier", "S")
        self.created(c.load_briefs(self.p)[-1].id, 35)  # 5 days after its close: inside
        fresh = self.done("Recent and fine")
        gone = self.done("Recent then reverted")
        self.commit(f"Recent then reverted ({gone})")
        self.git("revert", "--no-edit", "HEAD")
        return ok, late, early, fresh, gone

    def test_the_window_grades_each_close(self):
        ok, late, early, fresh, gone = self.setup_five()
        t = json.loads(self.fm("outcomes", "--json").stdout)["tasks"]
        self.assertEqual(t[ok]["survival"], "survived")
        self.assertEqual(t[late]["survival"], "survived")  # a fix 40 days on is a fate, not a loss in the window
        self.assertEqual(t[late]["fate"], "fixed later")
        self.assertEqual(t[early]["survival"], "lost")
        self.assertEqual(t[gone]["survival"], "lost")
        self.assertEqual(t[fresh]["survival"], "settling")
        self.assertGreater(t[fresh]["days_left"], 25)
        out = self.fm("outcomes").stdout
        # T-2037: a loss lands mid-window, a survival only after it; T-2584: so the rate counts finished windows only
        self.assertIn("Survival (30 days): of 5 closes, 3 finished the window: 1 lost (33%), 2 survived; "
                      "2 still in it (1 lost so far)", out)
        self.assertIn("days since a lost task: 0", out)

    def test_settling_logs_one_grade_per_task_and_shows_in_digest_and_history(self):
        ok, late, early, fresh, gone = self.setup_five()
        self.assertIn("4 graded", self.fm("outcomes", "--settle").stdout)
        self.assertIn("0 graded", self.fm("outcomes", "--settle").stdout)  # once per task
        graded = [e for e in c.ledger_tail(self.p, 2000) if e.get("event") == "outcome"]
        self.assertEqual(sorted(e["task"] for e in graded), sorted([ok, late, early, gone]))
        self.assertIn("Survival (30 days)", self.fm("digest").stdout)
        hist = self.fm_json("ui")["history"]
        self.assertTrue(any(h["text"] in ("survived 30 days", "lost") for h in hist), hist[-6:])

    def test_grades_read_the_archived_ledger_and_smoke_is_no_loss(self):
        # review: corrections and earlier grades rolled into archive/ledger-YYYY-MM.jsonl still count; a failing smoke
        # run (it covers the whole UI) never marks a task lost
        ok, late, early, fresh, gone = self.setup_five()
        c.log_event(self.p, "correction", task=ok, data={"text": "no, that broke the parser"})
        c.log_event(self.p, "smoke", data={"defects": 2})
        live = os.path.join(self.p.dir, "ledger.jsonl")
        os.makedirs(os.path.join(self.p.dir, "archive"), exist_ok=True)
        with open(live) as f:
            lines = f.readlines()
        for e in map(json.loads, lines[-2:]):  # backdate them into the window, then roll them out
            e["ts"] = c.iso(time.time() - 38 * 86400)
            with open(os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl"), "a") as f:
                f.write(json.dumps(e) + "\n")
        with open(live, "w") as f:
            f.writelines(lines[:-2])
        t = json.loads(self.fm("outcomes", "--json").stdout)["tasks"]
        self.assertEqual(t[ok]["survival"], "lost")
        self.assertEqual(t[ok]["lost_by"], "corrected")
        self.assertEqual(t[fresh]["survival"], "settling", "smoke is no loss")
        self.assertIn("4 graded", self.fm("outcomes", "--settle").stdout)
        with open(live) as f:
            settled = [ln for ln in f if '"outcome"' in ln]
        with open(os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl"), "a") as f:
            f.writelines(settled)
        with open(live) as f:
            rest = [ln for ln in f if '"outcome"' not in ln]
        with open(live, "w") as f:
            f.writelines(rest)
        self.assertIn("0 graded", self.fm("outcomes", "--settle").stdout, "a rolled grade still counts as settled")

    def test_history_shows_only_losses(self):
        self.setup_five()
        self.fm("outcomes", "--settle")
        texts = [h["text"] for h in self.fm_json("ui")["history"]]
        self.assertIn("lost", texts)
        self.assertNotIn("survived 30 days", texts)

    def test_empty_claims_say_so(self):
        self.assertIn("nothing graded yet", self.fm("outcomes", "--claims").stdout)

    def test_reports_read_the_grades(self):
        ok, late, early, fresh, gone = self.setup_five()
        self.fm("task", "drop", self.fm_json("task", "new", "Abandoned", "--type", "FEATURE", "--tier", "S")["id"], "no")
        self.assertIn("Survival by close grade", self.fm("outcomes", "--claims").stdout)
        regret = self.fm("outcomes", "--regret").stdout
        self.assertIn(early, regret)
        grave = self.fm("outcomes", "--graveyard").stdout
        self.assertIn(gone, grave)
        self.assertIn("Abandoned", grave)
        self.assertIn("corrected during the task", self.fm("outcomes", "--seen").stdout)


class Lessons(Base):
    def test_lessons_get_ids_shown_counts_and_flags(self):
        a = self.done("Quote shell paths in the deploy script", "FEATURE", "true", "--lesson", "quote every path")
        b = self.done("Cache the parser tables", "FEATURE", "true", "--lesson", "invalidate on schema change")
        self.age(b, 30)
        with open(os.path.join(self.p.dir, "failures.jsonl"), "a") as f:
            f.write(json.dumps({"sig": "keyerror: 'col'", "task": b, "at": c.iso(time.time() - 40 * 86400)}) + "\n")
            f.write(json.dumps({"sig": "keyerror: 'col'", "task": "T-0009", "at": c.now()}) + "\n")
        self.fm("task", "new", "Quote shell paths in the backup script", "--type", "FEATURE", "--tier", "S",
                "--ac", "ok :: true", "--step", "s")
        self.fm("focus", "T-0003")
        out = self.fm("recall", "--lessons").stdout
        self.assertRegex(out, rf"{a}\.1\b.*shown 1")
        self.assertRegex(out, rf"never recalled.*{b}\.1")
        self.assertRegex(out, rf"recurred.*{b}\.1.*T-0009")


class Yield(Base):
    def test_usage_agents_reports_what_each_lens_and_pass_found(self):
        self.fm("task", "new", "Parser", "--type", "FEATURE", "--tier", "M", "--ac", "ok :: true", "--step", "s")
        self.fm("task", "audit", "T-0001", "edge", "review", "fixed: empty input crashed the parser")
        self.fm("task", "audit", "T-0001", "intent", "self", "no findings")
        self.fm("task", "audit", "T-0001", "adversary", "review", "1 false positive: flagged eval, it is ast.literal_eval")
        self.fm("task", "set", "T-0001", "--section", "Plan review", "--text", "second plan by sonnet")
        self.fm("task", "set", "T-0001", "--section", "Dissent", "--text", "- [ ] the cache can go stale")
        c.log_event(self.p, "second_session", data={"read": 3, "captured": 1})
        c.log_event(self.p, "second_session", data={"read": 2, "captured": 0})
        out = self.fm("usage", "--agents").stdout
        self.assertRegex(out, r"edge: 1 of 1 found something")
        self.assertRegex(out, r"intent: 0 of 1")
        self.assertRegex(out, r"adversary: .*1 false positive")
        self.assertRegex(out, r"second plan: 1 of 1 left dissent")
        self.assertRegex(out, r"second session: 1 of 2 captured")


class TrackRecord(Base):
    def test_a_close_still_in_its_window_is_neither_held_nor_in_the_loss_rate(self):  # T-2584
        old, fresh = self.done("Old"), self.done("Fresh")
        self.age(old, 31)
        outs = outcomes.outcomes(self.p)
        self.assertEqual(outcomes._row(outcomes.track(self.p, outs)[("FEATURE", "S")]).split(", ", 1)[1],
                         "1 of 1 held, 1 still in the 30-day window")
        line = outcomes.survival_lines(self.p, outs)[0]
        self.assertIn("of 2 closes, 1 finished the window: 0 lost (0%), 1 survived; 1 still in it (0 lost so far)", line)

    def test_track_record_and_would_do_differently_in_digest_and_focus(self):
        self.done("First", "FEATURE", "true", "--differently", "write the failing test before the parser")
        self.fm("task", "new", "Second", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        self.fm("task", "finish", "T-0002", "--audit", "self", "--run", "false", check=False)
        self.fm("task", "finish", "T-0002", "--audit", "self", "--run", "true")
        self.assertIn("write the failing test", c.find_brief(self.p, "T-0001").section("Would do differently"))
        out = self.fm("digest").stdout
        self.assertRegex(out, r"Track record.*FEATURE S: 1 of 2 closed on the first finish, 0 of 0 held, 2 still in the 30-day window")
        self.assertIn("write the failing test", out)
        self.fm("task", "new", "Third", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.assertRegex(self.fm("focus", "T-0003").stdout, r"Track record for FEATURE S here: 1 of 2 closed")


class Predictions(Base):
    """T-2290: focus predicts minutes and attempts from the closes of the same tier and type; the close scores them."""

    def seed(self, n, minutes=10, attempts=1, typ="FEATURE", tier="S"):
        from sampo import outcomes
        for i in range(n):
            outcomes.record_close(self.p, dict(type=typ, tier=tier, minutes=minutes + i, attempts=attempts))

    def new(self, title="Work", **kw):
        tid = self.fm_json("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")["id"]
        for k, v in kw.items():
            self.fm("task", "set", tid, f"{k}={v}")
        return tid

    def test_focus_predicts_the_median_of_same_tier_and_type(self):
        self.seed(5, minutes=10)  # 10..14: median 12
        self.seed(9, minutes=500, typ="FIX")  # another type never counts
        t = self.new()
        self.fm("focus", t)
        m = c.find_brief(self.p, t).meta
        self.assertEqual((int(m["predict_minutes"]), int(m["predict_attempts"])), (12, 1))

    def test_under_five_samples_predicts_nothing_and_a_typed_one_stays(self):
        self.seed(4)
        a, b = self.new("A"), self.new("B", predict_minutes=7)
        self.fm("focus", a)
        self.assertIsNone(c.find_brief(self.p, a).meta.get("predict_minutes"))
        self.seed(1)
        self.fm("focus", b)
        self.assertEqual(int(c.find_brief(self.p, b).meta["predict_minutes"]), 7)

    def test_the_close_logs_its_stats_and_the_prediction_scored(self):
        self.seed(5)
        t = self.new()
        self.fm("focus", t)
        self.fm("task", "finish", t, "--audit", "self", "--run", "true")
        d = next(e["data"] for e in c.ledger_events(self.p, ("task_done",)) if e["task"] == t)
        self.assertEqual((d["predict_minutes"], d["minutes"], d["attempts"]), (12, 0, 1))
        from sampo import outcomes
        self.assertEqual(len(outcomes.history(self.p, "FEATURE", "S")), 6, "the close joins the history")

    def test_calibration_prints_the_median_ratio_per_tier(self):
        from sampo import outcomes
        for actual in (10, 20, 40):
            outcomes.record_close(self.p, dict(type="FEATURE", tier="S", minutes=actual, predict_minutes=10))
        out = self.fm("outcomes", "--calibration").stdout
        self.assertRegex(out, r"S: median actual/predicted 2\.0x over 3")

    def test_next_notes_a_task_past_three_times_its_prediction(self):
        t = self.new(predict_minutes=5)
        self.fm("focus", t)
        self.assertNotIn("3x", self.fm("next").stdout)
        live = os.path.join(self.p.dir, "ledger.jsonl")  # the focus happened 25 minutes ago
        with open(live) as f:
            evs = [json.loads(ln) for ln in f]
        for e in evs:
            if e.get("event") == "focus" and e.get("task") == t:
                e["ts"] = c.iso(time.time() - 1500)
        with open(live, "w") as f:
            f.writelines(json.dumps(e) + "\n" for e in evs)
        self.assertEqual(self.fm("next").stdout.count("3x its prediction"), 1)
        self.assertTrue(c.find_brief(self.p, t).meta.get("predict_noted"))
        self.assertNotIn("3x its prediction", self.fm("next").stdout, "said once")

    def test_first_use_backfills_the_history_from_task_done_events(self):
        from sampo import outcomes
        ids = [self.new(f"Old {i}") for i in range(5)]
        for i, tid in enumerate(ids):  # closes logged before closes.jsonl existed: no type or tier in the event
            c.log_event(self.p, "task_done", task=tid, data={"minutes": 10 + i, "attempts": 2})
        c.log_event(self.p, "task_done", task=ids[0], data={"attempts": 2})  # no minutes: skipped
        path = os.path.join(self.p.dir, "closes.jsonl")
        self.assertFalse(os.path.exists(path))
        t = self.new("Fresh")
        self.fm("focus", t)
        m = c.find_brief(self.p, t).meta
        self.assertEqual((int(m["predict_minutes"]), int(m["predict_attempts"])), (12, 2))
        self.assertEqual(len(outcomes.history(self.p, "FEATURE", "S")), 5)
        self.assertTrue(os.path.exists(path), "written once")


class Hygiene(Base):
    def test_bench_lists_cases_that_never_discriminate(self):
        d = os.path.join(self.p.dir, "bench", "results")
        os.makedirs(d)
        for label, c2 in (("v1", True), ("v2", False)):
            with open(os.path.join(d, label + ".json"), "w") as f:
                json.dump({"label": label, "cases": [{"id": "same-case", "pass": True}, {"id": "split-case", "pass": c2}]}, f)
        out = self.fm("bench", "hygiene").stdout
        self.assertRegex(out, r"same-case.*passed in all 2 runs")
        self.assertNotIn("split-case", out)


class Reopen(Base):
    def test_tidy_reopens_a_blocked_task_once_what_it_waited_on_changed(self):
        os.makedirs(os.path.join(self.repo, "lib"))
        self.commit("lib", "lib/shim.py", "X = 1\n")
        self.fm("task", "new", "Upstream", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.fm("task", "new", "Downstream", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.fm("task", "set", "T-0002", "depends_on=T-0001")
        self.fm("task", "block", "T-0002", "waits for T-0001")
        self.fm("task", "new", "Waits on shim", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.fm("task", "block", "T-0003", "lib/shim.py returns the wrong type until upstream fixes it")
        self.fm("task", "new", "Waits on a person", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.fm("task", "block", "T-0004", "needs the owner's phone")
        self.fm("task", "drop", "T-0001", "done elsewhere")
        time.sleep(1.1)
        self.commit("shim fixed upstream", "lib/shim.py", "X = 2\n")  # T-1106: a real change, not a new mtime
        self.assertRegex(self.fm("next").stdout, r"2 blocked task\(s\) waited on changed.*sampo tidy --apply")
        self.fm("tidy", "--apply")
        self.assertEqual(c.find_brief(self.p, "T-0002").status, "planned")
        self.assertEqual(c.find_brief(self.p, "T-0003").status, "planned")
        self.assertEqual(c.find_brief(self.p, "T-0004").status, "blocked")
        self.assertIn("lib/shim.py changed", c.find_brief(self.p, "T-0003").section("Log"))

    def test_reverted_commits_come_back_in_recall_as_wrong_turns(self):
        self.commit("Use a global lock for the session cache")
        self.git("revert", "--no-edit", "HEAD")
        self.assertRegex(self.fm("recall", "session", "cache", "lock").stdout,
                         r"wrong turn.*global lock for the session cache")


class Mining(Base):
    def test_friction_mines_paths_smooth_unverified_and_give_up_points(self):
        def ev(event, task, **data):
            c.log_event(self.p, event, task=task, data=data)
        ev("focus", "T-0005")
        ev("task_done", "T-0005", verified="weak", type="FEATURE", tier="S")
        ev("focus", "T-0006")
        for _ in range(3):
            ev("evidence", "T-0006", cmd="pytest", result=c.run_result(1, "2 failed"))  # the real format
        ev("task_block", "T-0006", reason="can't reproduce")
        out = self.fm("friction").stdout
        self.assertRegex(out, r"smooth but unverified[^\n]*\n\s+- T-0005")
        self.assertRegex(out, r"give-up points[^\n]*\n\s+- [^\n]*after 3 failed run\(s\)[^\n]*T-0006")
        self.assertRegex(out, r"common paths[^\n]*\n\s+- focus → done")


class ModelRouter(Base):
    """T-2291: the builder model follows how past lanes' tasks held up per tier and model."""
    def seed(self, model, n, bad):
        """n closed S tasks built by `model`; the first `bad` get a later FIX naming them."""
        for i in range(n):
            t = self.done(f"{model} task {i}")
            b = c.find_brief(self.p, t)
            with open(b.path, encoding="utf-8") as f:
                text = f.read()
            with open(b.path, "w", encoding="utf-8") as f:
                f.write(re.sub(r"(?m)^updated: .*$", lambda m: f"{m.group(0)}\nbuilder_model: {model}", text, 1))
            if i < bad:
                self.fm("capture", f"{t} broke the export", "--type", "FIX", "--tier", "S")

    def route(self):
        from sampo import lanes
        return lanes.model_route(c.find_project(self.repo), "S")

    def test_a_cheaper_model_that_keeps_needing_rework_escalates_to_opus(self):
        self.seed("sonnet", 6, 4)
        self.seed("opus", 5, 0)
        model, line = self.route()
        self.assertEqual(model, "opus")
        self.assertIn("4/6", line)
        self.assertIn("0/5", line)

    def test_too_few_samples_or_a_small_gap_keeps_sonnet_and_says_so(self):
        self.seed("sonnet", 6, 4)
        self.seed("opus", 4, 0)
        model, line = self.route()
        self.assertEqual(model, "sonnet")
        self.assertIn("too little history", line)


# --- T-2337 (Mission 5, #8): the calibration ledger -------------------------------------------------------------
import datetime

from sampo import outcomes
from sampo import trials as strials


class CalBase(Base):
    def rows(self):
        path = os.path.join(self.p.dir, "calibration.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def bets(self, kind=None):
        return [r for r in self.rows() if "kind" in r and (kind is None or r["kind"] == kind)]

    def planned(self, title="Cache", tier="M", conf=None, assume=(), choice="Choice 1 (80%): a (80%)"):
        """A planned task: a stated choice, assumptions and an optional task confidence, not yet focused."""
        from helpers import valid_m_plan
        reading, _ = valid_m_plan()  # T-2335's plan gate: two readings, outcomes, use-what-exists and do-less
        self.fm("task", "new", title, "--type", "FEATURE", "--tier", tier, "--interpretation", reading, "--approach",
                f"1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z {choice}",
                "--ac", "ok :: true", "--step", "s", "--scope", "lib/a.py")
        tid = c.load_briefs(self.p)[-1].id
        for text in assume:
            self.fm("task", "assume", tid, "add", text)
        if conf is not None:
            self.fm("task", "set", tid, f"confidence={conf}")
        return tid

    def seed(self, kind, stated, held, n, base=None, tier="S", typ="FEATURE", prefix="s", tasks=False, src=None):
        """n scored bets of one kind and stated value, `held` of them held (rows as fm writes them). tasks: the bets
        belong to tasks X-0.. (one close each, whatever its kinds); src: what scored them (a task or choice bet is
        scored by survival, the fm outcomes signal, unless said otherwise)."""
        path = os.path.join(self.p.dir, "calibration.jsonl")
        src = src or ("survival" if kind in ("task", "choice") else "mark")
        with open(path, "a", encoding="utf-8") as f:
            for i in range(n):
                bid = f"{prefix}{kind}{stated}-{i}"
                f.write(json.dumps({"ts": c.now(), "id": bid, "kind": kind, "stated": stated, "base": base,
                                    "task": f"X-{i}" if tasks else None, "key": bid, "files": [], "tree": None,
                                    "tier": tier, "type": typ}) + "\n")
                f.write(json.dumps({"ts": c.now(), "of": bid, "outcome": "held" if i < held else "missed",
                                    "by": "seeded", "src": src}) + "\n")

    def cal(self, *args):
        return json.loads(self.fm("outcomes", "--judgment", "--json", *args).stdout)

    def race(self, tid):
        return json.loads(self.fm("lane", "race", tid, "--confidence", "70", "--json").stdout)["race"]

    def plain(self, title="Race me"):
        self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "it wins :: test -f win.txt",
                "--step", "s")  # T-2339: a race refuses a check that already passes
        return c.load_briefs(self.p)[-1].id


class CalibrationLedgerBets(CalBase):  # not CalibrationLedger: test_planning has one
    def test_a_stated_confidence_writes_one_write_once_row_per_kind(self):
        first = self.done("Earlier 0")  # earlier closes: the base rate is their held-on-first-finish share (2 of 3)
        for tid in (first, self.done("Earlier 1"), self.done("Earlier 2")):
            self.age(tid, 31)  # T-2610: only finished windows pool into the base rate
        self.fm("capture", f"Crash in what {first} added", "--type", "FIX", "--tier", "S")
        tid = self.planned(conf=90, assume=["the cache is read-only (70%)", "no network (75%) in tests"])
        self.assertEqual(self.bets(), [], "nothing is a bet before first focus")
        self.fm("focus", tid)
        got = {(r["kind"], r["key"]): r for r in self.bets() if r["task"] == tid}
        self.assertEqual({k: r["stated"] for k, r in got.items()},
                         {("task", "confidence"): 90, ("choice", "choice"): 80, ("assumption", "A1"): 70,
                          ("assumption", "A2"): 75})
        row = got[("choice", "choice")]
        self.assertEqual((row["base"], row["files"], row["tier"], row["type"]), (67, ["lib/a.py"], "M", "FEATURE"))
        self.assertTrue(row["tree"] and row["id"] and row["ts"], row)
        self.fm("focus", tid)  # a second focus (a resume) writes nothing new
        self.assertEqual(len([r for r in self.bets() if r["task"] == tid]), 4)
        self.fm("decide", "Use a file cache", "--why", "simple", "--confidence", "85", "--task", tid)
        self.assertEqual([(r["stated"], r["task"]) for r in self.bets("decision")], [(85, tid)])
        self.fm("decide", "No number given")
        self.assertEqual(len(self.bets("decision")), 1, "a decision without --confidence is no bet")
        self.assertNotEqual(self.fm("decide", "Too sure", "--confidence", "150", check=False).returncode, 0)
        self.assertNotEqual(self.fm("decide", "NaN", "--confidence", "x", check=False).returncode, 0)

    def test_a_race_side_is_a_bet(self):
        tid = self.plain()
        other = self.race(tid)
        (row,) = self.bets("race")
        self.assertEqual((row["stated"], row["task"], row["key"]), (70, tid, f"{tid}>{other}"))

    def test_a_stated_value_cannot_be_edited_or_cleared_once_work_started(self):
        tid = self.planned(conf=90)
        self.fm("task", "set", tid, "confidence=80")  # before first focus it is still a plan
        self.fm("focus", tid)
        for edit in ("confidence=50", "confidence="):
            r = self.fm("task", "set", tid, edit, check=False)
            self.assertNotEqual(r.returncode, 0, edit)
            self.assertIn("bet", r.stderr + r.stdout)
        self.assertEqual([r["stated"] for r in self.bets("task")], [80])
        self.assertEqual(c.find_brief(self.p, tid).meta["confidence"], "80")
        self.fm("task", "set", tid, "confidence=80")  # the same value is no edit

    def test_a_confidence_stated_after_focus_is_recorded_when_set(self):
        self.fm("task", "new", "Late", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        tid = c.load_briefs(self.p)[-1].id
        self.fm("task", "set", tid, "confidence=75")
        self.assertEqual([(r["task"], r["stated"]) for r in self.bets("task")], [(tid, 75)])

    def test_outcomes_score_each_row_pending_until_the_window_closes(self):
        held, broke, fresh = self.done("Held"), self.done("Broke"), self.done("Fresh")
        for tid in (held, broke, fresh):
            outcomes.bet(self.p, "task", 90, task=tid, key="confidence", tier="S", typ="FEATURE")
        self.age(held, 31)
        self.age(broke, 10)
        self.fm("capture", f"Crash in what {broke} added", "--type", "FIX", "--tier", "S")
        data = self.cal()
        by = {r["task"]: r for r in outcomes.scored(self.p)}
        self.assertEqual({t: by[t]["outcome"] for t in (held, broke, fresh)},
                         {held: "held", broke: None, fresh: None}, "settling is pending, never held")
        self.assertEqual(by[broke]["early"], "missed", "T-2610: a loss inside the window waits for it too")
        self.assertEqual((data["scored"], data["pending"], data["open_missed"]), (1, 2, 1))
        (b,) = [x for x in data["kinds"]["task"] if x["lo"] == 80]
        self.assertEqual((b["n"], b["held"], b["gap"]), (1, 1, -10))  # stated 90, held 100%
        self.assertEqual(sorted(r["outcome"] for r in self.rows() if "outcome" in r), ["held", "missed"])
        self.fm("outcomes", "--judgment")
        self.assertEqual(len([r for r in self.rows() if "outcome" in r]), 2, "settling twice adds no second grade")

    def test_a_first_finish_that_failed_is_a_miss_at_once(self):
        self.fm("task", "new", "Red", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        tid = c.load_briefs(self.p)[-1].id
        c.log_event(self.p, "finish", task=tid, data={"failed": ["x"]})
        outcomes.bet(self.p, "task", 95, task=tid, key="confidence", tier="S", typ="FEATURE")
        self.assertEqual([r["outcome"] for r in outcomes.scored(self.p)], ["missed"])

    def test_assumption_marks_and_reversed_decisions_score(self):
        tid = self.planned(assume=["it is fast (80%)", "it is small (75%)"], choice="Choice 1 (80%): a")
        self.fm("focus", tid)
        self.fm("task", "assume", tid, "verify", "1", "--run", "true")
        self.fm("task", "assume", tid, "verify", "2", "--run", "false")
        self.fm("decide", "Use Redis for sessions", "--confidence", "80")
        self.fm("decide", "Use Redis for rate limits", "--confidence", "90")
        self.fm("decide", "Use Postgres for sessions instead", "--reverses", "Use Redis for sessions")
        data = self.cal()
        got = {(r["kind"], r["key"]): r["outcome"] for r in outcomes.scored(self.p)}
        self.assertEqual(got[("assumption", "A1")], "held")
        self.assertEqual(got[("assumption", "A2")], "missed")
        self.assertEqual(got[("decision", "use redis for sessions")], "missed")
        self.assertIsNone(got[("decision", "use redis for rate limits")], "an unreversed decision waits out the window")
        self.assertEqual(data["pending"], 2)  # the unreversed decision and the choice (80%) the plan gate now requires

    def test_a_race_result_scores_the_race_side(self):
        tid = self.plain()
        other = self.race(tid)
        self.assertEqual([r["outcome"] for r in outcomes.scored(self.p)], [None])
        c.log_event(self.p, "race_pick", task=other, data={"winner": other, "loser": tid})
        self.assertEqual([r["outcome"] for r in outcomes.scored(self.p)], ["missed"])

    def test_the_base_rate_is_the_pairs_held_share_or_all_closes_under_twenty(self):
        outs, first = {}, {}
        for i in range(20):  # 20 FEATURE S closes, 10 held
            outs[f"T-{i}"] = {"type": "FEATURE", "tier": "S", "fate": "held" if i < 10 else "reverted"}
        for i in range(5):  # 5 FIX M closes, all held
            outs[f"F-{i}"] = {"type": "FIX", "tier": "M", "fate": "held"}
        first["T-0"], first["T-1"] = False, False  # two of the held FEATURE S closes failed their first finish
        self.assertEqual(outcomes.base_rate(outs, first, "FEATURE", "S"), 40, "8 of 20: the pair has 20")
        self.assertEqual(outcomes.base_rate(outs, first, "FIX", "M"), 52, "5 closes: all 25 count, 13 held first")
        self.assertIsNone(outcomes.base_rate({}, {}, "FIX", "M"), "no history is no forecast")

    def test_the_old_fmoutcomes_calibration_is_gone(self):
        self.assertFalse(hasattr(outcomes, "calibration"))
        self.assertFalse(hasattr(outcomes, "calibration_lines"))

    def test_a_bet_stated_after_the_task_closed_is_never_recorded(self):
        tid = self.done("Closed already")
        self.fm("task", "set", tid, "confidence=90", check=False)
        self.assertEqual(self.bets(), [], "a hindsight bet can't count")
        self.fm("task", "assume", tid, "add", "it was fine (90%)", check=False)
        self.assertEqual(self.bets(), [])

    def test_a_bet_stated_after_the_first_finish_of_an_open_task_is_never_recorded(self):
        self.fm("task", "new", "Failing", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        tid = c.load_briefs(self.p)[-1].id
        c.log_event(self.p, "finish", task=tid, data={"failed": ["x"]})
        self.fm("task", "set", tid, "confidence=90")
        self.assertEqual(self.bets(), [], "it knew the first finish failed")

    def test_a_decision_is_not_scored_against_its_own_reversal_event(self):
        self.fm("decide", "Use Redis for sessions v2", "--reverses", "use redis for sessions", "--confidence", "80")
        got = {r["key"]: r["outcome"] for r in outcomes.scored(self.p)}
        self.assertIsNone(got["use redis for sessions v2"], "its own --reverses words are not a later reversal")

    def test_a_restated_decision_says_its_bet_was_ignored(self):
        first = self.fm("decide", "Use Redis for sessions", "--confidence", "80")
        self.assertNotIn("ignored", first.stdout + first.stderr)
        again = self.fm("decide", "Use Redis for sessions", "--confidence", "60")
        self.assertIn("ignored", again.stdout + again.stderr)
        self.assertEqual([r["stated"] for r in self.bets("decision")], [80])

    def test_record_stated_computes_nothing_when_nothing_is_stated(self):
        from unittest import mock
        self.fm("task", "new", "Silent", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        b = c.load_briefs(self.p)[-1]
        with mock.patch.object(outcomes, "outcomes", side_effect=AssertionError("outcomes() ran")), \
                mock.patch.object(outcomes, "_first_finish", side_effect=AssertionError("ran")), \
                mock.patch.object(outcomes, "_tree", side_effect=AssertionError("ran")):
            outcomes.record_stated(self.p, b)  # swallows errors, so look at the mocks' own calls
            for name in ("outcomes", "_first_finish", "_tree"):
                self.assertFalse(getattr(outcomes, name).called, name)

    def test_record_stated_skips_the_work_when_every_key_is_already_a_bet(self):
        from unittest import mock
        tid = self.planned(conf=90)
        self.fm("focus", tid)
        b = c.find_brief(self.p, tid)
        with mock.patch.object(outcomes, "outcomes") as outs:
            outcomes.record_stated(self.p, b)
        outs.assert_not_called()
        self.assertEqual(len(self.bets("task")), 1)


class CalibrationHarness(CalBase):  # T-2500: a bet records the harness that stated it; --by harness splits them
    def test_a_bet_records_its_harness_and_by_harness_splits_them(self):
        a, b = self.planned("A"), self.planned("B")
        self.fm("focus", a, env={"FOREMAN_HARNESS": "fm-code", "FOREMAN_BENCH": ""})
        self.fm("focus", b, env={"FOREMAN_HARNESS": "", "FOREMAN_BENCH": "", "CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual({r["task"]: r["harness"] for r in self.bets("choice")}, {a: "fm-code", b: "claude-code"})
        with open(os.path.join(self.p.dir, "calibration.jsonl"), "a", encoding="utf-8") as f:  # a row from before
            f.write(json.dumps({"ts": c.now(), "id": "old-1", "kind": "decision", "stated": 70, "base": None,
                                "task": None, "key": "old", "files": [], "tree": None}) + "\n")
        self.assertEqual(sorted(self.cal("--by", "harness")["by"]), ["claude-code", "fm-code", "unknown"])

    def test_the_harness_marker_is_read_from_the_environment(self):
        from unittest import mock
        for env, want in (({"FOREMAN_HARNESS": "fm-code"}, "fm-code"), ({"FOREMAN_BENCH": "1"}, "exam"),
                          ({"CLAUDECODE": "1"}, "claude-code"), ({}, "unknown"),
                          ({"FOREMAN_HARNESS": "Bad Name!"}, "unknown")):
            clean = {k: v for k, v in os.environ.items()
                     if k not in ("FOREMAN_HARNESS", "FOREMAN_BENCH", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID")}
            with mock.patch.dict(os.environ, dict(clean, **env), clear=True):
                self.assertEqual(c.harness(), want, env)


class CalibrationTrails(CalBase):  # T-2499: the digest names the kind whose Brier trails its base rate most
    def test_the_digest_names_the_kind_whose_brier_trails_its_base_rate_most(self):
        self.seed("choice", 90, 6, 12, base=60)  # stated 90, held half: Brier 0.41 against the base rate's 0.26
        self.seed("task", 70, 8, 12, base=70, prefix="t")  # stated equals the base rate: no trail
        self.assertIn("trails its base rate most: choice (Brier stated 0.41 vs base 0.26, n 12)",
                      outcomes.ledger_lines(self.p)[0])

    def test_under_twenty_scored_says_too_few(self):
        self.seed("choice", 90, 6, 10, base=60)
        self.assertIn("too few", outcomes.ledger_lines(self.p)[0])


class CalibrationInterim(CalBase):  # T-2498: a week-old close reads "held so far", labeled, never scored
    def test_a_pending_bet_closed_a_week_ago_reads_held_so_far_and_scores_nothing(self):
        rows = [{"kind": "choice", "stated": 75, "outcome": None, "task": "T-1"},
                {"kind": "choice", "stated": 70, "outcome": None, "task": "T-2"},
                {"kind": "task", "stated": 90, "outcome": None, "task": "T-3"},   # closed 3 days ago: not yet
                {"kind": "choice", "stated": 75, "outcome": "held", "task": "T-4"},  # scored: the bins, not interim
                {"kind": "decision", "stated": 80, "outcome": None, "task": "T-1"}]  # not a task bet
        outs = {"T-1": {"survival": "settling", "days_left": 22, "fate": "held"},
                "T-2": {"survival": "settling", "days_left": 20, "fate": "corrected"},
                "T-3": {"survival": "settling", "days_left": 27, "fate": "held"},
                "T-4": {"survival": "survived", "fate": "held"}}
        got = outcomes.interim(rows, outs)
        self.assertEqual(got, {"choice": [{"lo": 60, "hi": 79, "n": 2, "held": 1, "pct": 50}]})
        self.assertEqual(outcomes._interim_line({"interim": got}),
                         "Interim (closed 7+ days ago, not scored): choice 60–79%: 1 of 2 held so far")

    def test_the_judgment_json_carries_interim(self):
        d = json.loads(self.fm("outcomes", "--judgment", "--json").stdout)
        self.assertEqual(d["interim"], {})


class CalibrationBars(CalBase):
    def meta_bars(self):
        return c.read_meta(self.p).get("calibration_bars") or {}

    def test_under_twenty_scored_rows_says_too_few_and_moves_nothing(self):
        self.seed("race", 90, 5, 19)
        out = self.fm("outcomes", "--judgment").stdout
        self.assertIn("too few", out)
        self.assertNotIn("Brier", out)
        self.assertEqual(self.cal()["bars"], {"research": 70, "race": 55})

    def test_buckets_and_both_briers_print_from_twenty_rows(self):
        self.seed("task", 90, 14, 20, base=70)
        out = self.fm("outcomes", "--judgment").stdout
        self.assertRegex(out, r"task 80–94%: n 20, held 14 \(70%\), gap \+20")
        self.assertRegex(out, r"Brier: stated 0\.25, base rate 0\.21")
        self.assertRegex(out, r"Bars: research 80 · race 55", "a task bet 20 points over raises the research bar")

    def test_a_bar_only_rises_as_a_decide_row_and_a_registered_trial(self):
        self.seed("choice", 90, 10, 20)  # stated 90, held 50% (by survival): 40 points over
        self.assertEqual(self.cal()["bars"], {"research": 80, "race": 55})
        self.assertEqual(self.meta_bars()["research"], 80)
        decisions = open(os.path.join(self.p.dir, "decisions.md"), encoding="utf-8").read()
        self.assertRegex(decisions, r"research bar 70 → 80")
        trials = strials.load(self.p)
        self.assertEqual([t["moves"] for t in trials], ["brier"])
        self.assertIn("research bar", trials[0]["what"])
        self.fm("outcomes", "--judgment")  # the same rows raise nothing twice
        self.assertEqual((self.meta_bars()["research"], len(strials.load(self.p))), (80, 1))
        self.seed("choice", 90, 10, 20, prefix="more")  # twenty fresh rows, still over: one more step
        self.assertEqual(self.cal()["bars"]["research"], 90)

    def test_a_bar_is_raised_at_task_finish_without_fm_calibration_run(self):
        self.seed("choice", 90, 10, 20)
        self.assertEqual(self.meta_bars(), {}, "nothing has looked yet")
        self.done("Close that triggers the check")
        self.assertEqual(self.meta_bars().get("research"), 80)
        self.assertRegex(open(os.path.join(self.p.dir, "decisions.md"), encoding="utf-8").read(),
                         r"research bar 70 → 80")
        self.assertEqual(len(strials.load(self.p)), 1)

    def test_only_survival_signals_move_a_bar_foremans_own_marks_only_score(self):
        self.seed("race", 90, 10, 20)  # 40 points over, scored by Foreman's own race pick
        self.seed("assumption", 90, 10, 20)
        self.seed("decision", 90, 10, 20)
        self.seed("task", 90, 10, 20, src="first_finish")  # a failed first finish: Foreman's own gate
        data = self.cal()
        self.assertEqual(data["bars"], {"research": 70, "race": 55}, "none of these raise a bar")
        self.assertEqual(self.meta_bars(), {})
        self.assertEqual(strials.load(self.p), [])
        self.assertEqual(data["scored"], 80, "they still score in the report")
        self.assertRegex(self.fm("outcomes", "--judgment").stdout, r"race 80–94%: n 20")
        self.seed("task", 90, 10, 20, src="survival", prefix="surv")
        self.assertEqual(self.cal()["bars"]["research"], 80, "a survival-scored task bet does")

    def test_one_close_counts_once_toward_the_twenty_row_threshold(self):
        self.seed("task", 90, 10, 20, tasks=True)  # twenty closes, each with a task bet ...
        self.seed("choice", 90, 10, 20, tasks=True)  # ... and a choice bet: 40 rows, 20 closes
        self.seed("task", 90, 10, 20, tasks=True, prefix="b")  # twenty more rows of the same twenty closes
        self.assertEqual(self.cal()["bars"]["research"], 80)
        self.assertEqual(self.cal()["bars"]["research"], 80, "ten closes can't raise it twice")
        self.assertEqual(len(strials.load(self.p)), 1)

    def test_a_bar_stops_at_ninety_and_a_small_gap_raises_nothing(self):
        c.update_meta(self.p, calibration_bars={"research": 85})
        self.seed("choice", 90, 10, 20)
        self.assertEqual(self.cal()["bars"]["research"], 90, "at most 10 points, at most 90")
        self.seed("task", 70, 12, 20)  # stated 70, held 60%: 10 points, under the 15
        self.assertEqual(self.cal()["bars"]["research"], 90)
        self.assertEqual(len(strials.load(self.p)), 1)

    def test_a_hand_set_bar_above_ninety_is_kept(self):
        c.update_meta(self.p, calibration_bars={"research": 95})
        self.assertEqual(self.cal()["bars"]["research"], 95)
        self.seed("choice", 90, 10, 20)
        self.assertEqual(self.cal()["bars"]["research"], 95, "never lowered, never raised past the cap")

    def test_a_failed_trial_add_leaves_no_raise_behind(self):
        from unittest import mock
        self.seed("choice", 90, 10, 20)
        with mock.patch.object(strials, "add", side_effect=OSError("disk full")):
            outcomes.adjust_bars(self.p, outcomes.bar_rows(outcomes.scored(self.p)))
        self.assertEqual(self.meta_bars(), {})
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, "decisions.md"))
                         and "research bar" in open(os.path.join(self.p.dir, "decisions.md"), encoding="utf-8").read())
        self.assertEqual(self.cal()["bars"]["research"], 80, "the next look raises it, with its trial")
        self.assertEqual(len(strials.load(self.p)), 1)

    def test_an_underconfident_bucket_is_reported_once_for_the_final_report_and_the_bar_stays(self):
        self.seed("choice", 70, 20, 20)  # stated 70, held 100%
        self.assertIn("underconfident", self.fm("outcomes", "--judgment").stdout)
        self.assertEqual(self.cal()["bars"], {"research": 70, "race": 55}, "Foreman never lowers a bar")
        review = self.fm("decide", "--review").stdout
        self.assertIn("underconfident", review)
        self.assertIn("lower a bar", review)
        self.fm("outcomes", "--judgment")
        self.assertEqual(self.fm("decide", "--review").stdout.count("underconfident"), 1, "said once")

    def test_the_base_rate_routes_where_the_stated_numbers_score_worse(self):
        self.seed("task", 90, 14, 20, base=70)  # stated Brier .25, base .21
        self.seed("task", 70, 14, 20, base=50, tier="M", prefix="m")  # stated .21, base .25: stated stays
        self.assertEqual(self.cal()["routes"], {"FEATURE S": 70})
        self.assertIn("FEATURE S: the base rate's 70% replaces the stated numbers", self.fm("outcomes", "--judgment").stdout)
        self.assertFalse(hasattr(outcomes, "routed"), "routes() in the report is the only reader; no dead twin")

    def test_digest_reads_the_ledger_and_cost_still_runs(self):
        self.seed("task", 90, 14, 20, base=70)
        self.assertRegex(self.fm("digest").stdout, r"(?s)Calibration.*task 80–94%: n 20, held 14")
        with open(os.path.join(os.path.dirname(outcomes.__file__), "cost.py"), encoding="utf-8") as f:
            self.assertNotIn("calibration_lines", f.read(), "fm cost reads the ledger, not the removed function")

    def test_a_trial_for_a_bar_grades_on_brier_and_reverts_when_nothing_improved(self):
        self.seed("choice", 90, 10, 20)
        self.cal()
        t = strials.load(self.p)[0]
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=t["by"] + 1 + outcomes.SURVIVE_DAYS)
        verdict, detail = strials.grade(self.p, t, now=later)  # T-2610: past the windows, whichever side the seeds fell
        self.assertEqual(verdict, "revert")
        self.assertIn("Brier", detail)

    def test_a_brier_trial_waits_while_the_bets_since_the_move_are_inside_their_windows(self):  # T-2610
        t = {"what": "research bar 70 → 75", "moves": "brier", "by": 30, "at": c.now()}
        due = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=31)
        verdict, detail = strials.grade(self.p, t, now=due)
        self.assertEqual(verdict, "pending", detail)
        self.assertIn("30-day windows", detail)
        verdict, detail = strials.grade(self.p, t, now=due + datetime.timedelta(days=outcomes.SURVIVE_DAYS))
        self.assertEqual(verdict, "revert", "the wait is capped: no bet scored by then is no movement")


class CensoredReads(CalBase):  # T-2610: a miss lands mid-window, a hold only after it; reads count finished windows
    def lost_now(self, conf=None):
        """A planned task closed now, then a crash fix captured against it: lost inside its still-open window."""
        tid = self.planned(tier="S", conf=conf)
        self.fm("focus", tid)
        self.fm("task", "finish", tid, "--audit", "self", "--run", "true")
        self.fm("capture", f"Crash after {tid}", "--type", "FIX", "--tier", "S")
        return tid

    def test_the_track_record_counts_a_close_only_once_its_window_finished(self):
        old = self.done("Old")
        self.age(old, 31)
        fresh = self.done("Fresh")
        self.fm("capture", f"Crash after {fresh}", "--type", "FIX", "--tier", "S")
        outs = outcomes.outcomes(self.p)
        self.assertEqual(outs[fresh]["survival"], "lost")
        self.assertEqual(outcomes._row(outcomes.track(self.p, outs)[("FEATURE", "S")]).split(", ", 1)[1],
                         "1 of 1 held, 1 still in the 30-day window", "the fresh loss waits with the fresh holds")

    def test_a_bet_missed_inside_an_open_window_is_pending_and_named_apart(self):
        tid = self.lost_now(conf=90)
        rows = [r for r in outcomes.scored(self.p) if r["task"] == tid]
        self.assertEqual({r["kind"] for r in rows}, {"task", "choice"})
        self.assertTrue(all(r["outcome"] is None and r["early"] == "missed" for r in rows), rows)
        self.assertEqual(outcomes.bar_rows(outcomes.scored(self.p)), [], "the bar check waits for the window")
        d = self.cal()
        self.assertEqual((d["scored"], d["kinds"], d["open_missed"]), (0, {}, 2))
        self.assertIn("2 task or choice bet(s) missed inside a 30-day window still open: scored when it ends",
                      self.fm("outcomes", "--judgment").stdout)
        self.assertEqual(len([r for r in self.rows() if r.get("of")]), 2, "the grade rows are still written at once")

    def test_a_seeded_bet_with_no_window_on_record_still_counts(self):
        self.seed("task", 90, 14, 20, base=70)
        self.assertEqual((self.cal()["scored"], self.cal()["open_missed"]), (20, 0))

    def test_the_base_rate_pools_only_finished_or_unknown_windows(self):
        outs = {"A": {"type": "FEATURE", "tier": "S", "fate": "reverted", "window_done": False},
                "B": {"type": "FEATURE", "tier": "S", "fate": "held", "window_done": True},
                "C": {"type": "FEATURE", "tier": "S", "fate": "held"}}
        self.assertEqual(outcomes.base_rate(outs, {}, "FEATURE", "S"), 100, "A's window is still open")
        self.assertIsNone(outcomes.base_rate({"A": outs["A"]}, {}, "FEATURE", "S"), "only open windows: no forecast")

    def test_the_interim_read_counts_an_early_loss_as_not_held_so_far(self):
        rows = [{"kind": "choice", "stated": 75, "outcome": None, "early": "missed", "task": "T-1"},
                {"kind": "choice", "stated": 75, "outcome": None, "task": "T-2"}]
        outs = {"T-1": {"survival": "lost", "days_left": 22, "fate": "fixed later", "window_done": False},
                "T-2": {"survival": "settling", "days_left": 22, "fate": "held", "window_done": False}}
        self.assertEqual(outcomes.interim(rows, outs), {"choice": [{"lo": 60, "hi": 79, "n": 2, "held": 1, "pct": 50}]})

    def test_a_loss_inside_an_open_window_carries_its_days_left(self):
        fresh = self.done("Fresh")
        self.fm("capture", f"Crash after {fresh}", "--type", "FIX", "--tier", "S")
        self.assertGreater(outcomes.outcomes(self.p)[fresh]["days_left"], 25)
