"""T-4588: four time losses of one long run. (a) A plan call that spent its whole output thinking taught the run nothing
(T-4358), so each later plan call lost the same minutes: within a run, a later decides call of that size or larger now
goes out with a bounded think budget. (b) Planning has a share of the run's clock; past it an optional re-plan or
re-ask is skipped, with a note, and the plan in hand is used. (c) A fix round with no clock left is not counted, and
the stop names the clock. (d) A step whose plan names a file it creates, and that ends with none, is told once and says
so at its end. A scripted model and a fake clock: nothing here calls a real model or sleeps."""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import helpers  # noqa: F401 (puts plugin/lib on the import path before sampo)
from sampo import core, models, second, smith

LOCAL = {"name": "qwen-local", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen"}
CLI = {"name": "claude", "kind": "cli", "base_url": "http://127.0.0.1:11434/v1"}
MSG = [{"role": "user", "content": "plan it"}]
TEXT = {"text": "{}", "tool_calls": [], "usage": {}, "stop": "stop"}


def spent():
    return models.ModelError("qwen-local: reasoning used the whole budget: no text", reasoning=True)


class Fake:
    """models.complete stand-in: each call records the keywords it was sent, then gives the next scripted reply."""

    def __init__(self, *script):
        self.script, self.sent = list(script), []

    def __call__(self, profile, messages, **kw):
        self.sent.append(kw)
        got = self.script.pop(0) if self.script else TEXT
        if isinstance(got, Exception):
            raise got
        return got


class Clock:
    """A monotonic clock that moves only when told."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        patch = mock.patch.dict(os.environ, {"FOREMAN_STATE": os.path.join(tmp.name, "state")})
        patch.start()
        self.addCleanup(patch.stop)
        self.clock = Clock()
        clock = mock.patch.object(smith.time, "monotonic", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.notes = []

    def fresh(self, reason="native", deadline=None):
        run = smith.Run(types.SimpleNamespace(escalate=None, max_steps=None, reason=reason, deadline=deadline),
                        self.root, self.root)
        run.emit = lambda event, text, quiet=False, **data: self.notes.append((event, text, data))
        return run

    def said(self, event="note"):
        return [t for e, t, _ in self.notes if e == event]


class DecidesBudget(Base):
    def calls(self, fake, run, profile, *sizes, **kw):
        start = len(fake.sent)
        with mock.patch.object(models, "complete", fake):
            for size in sizes:
                smith.complete_retrying(run, profile, MSG, decides=True, **dict(kw, max_tokens=size))
        return fake.sent[start:]

    def test_the_next_decides_call_after_one_that_spent_its_thinking_is_bounded(self):
        run, fake = self.fresh(), Fake(spent(), TEXT, TEXT, TEXT)
        self.calls(fake, run, LOCAL, 8192)  # the first: spends it, is asked again with thinking off
        self.assertEqual(fake.sent[1].get("think"), False)
        later = self.calls(fake, run, LOCAL, 8192, 8192)
        for kw in later:
            self.assertEqual(kw.get("think_budget"), smith.DECIDE_THINK_BUDGET, kw)
            self.assertIs(kw.get("think"), True, "thinking is kept, never turned off for the plan")
        self.assertLess(smith.DECIDE_THINK_BUDGET + models.ANSWER_ALLOWANCE, 8192, "room for the text is guaranteed")

    def test_said_once_in_the_run(self):
        run, fake = self.fresh(), Fake(spent(), TEXT, TEXT, TEXT)
        self.calls(fake, run, LOCAL, 8192, 8192, 8192)
        notes = [t for t in self.said() if "think at most" in t]
        self.assertEqual(len(notes), 1, self.said())

    def test_a_larger_call_is_bounded_and_a_smaller_one_is_as_before(self):
        run, fake = self.fresh(), Fake(spent(), TEXT, TEXT, TEXT)
        self.calls(fake, run, LOCAL, 8192)
        bigger, smaller = self.calls(fake, run, LOCAL, 16000), None
        self.assertEqual(bigger[0].get("think_budget"), smith.DECIDE_THINK_BUDGET)
        smaller = self.calls(fake, run, LOCAL, 4000)
        self.assertNotIn("think_budget", smaller[0])
        self.assertNotIn("think", smaller[0], "a smaller plan call is left as the profile has it")

    def test_a_run_that_never_spent_it_is_as_before(self):
        run, fake = self.fresh(), Fake()
        for kw in self.calls(fake, run, LOCAL, 8192, 8192):
            self.assertNotIn("think_budget", kw)
            self.assertNotIn("think", kw)

    def test_the_default_calls_learn_nothing_from_a_plan_call(self):
        # T-4358, unchanged: a plan's exhausted thinking leaves no floor for the calls that start off by default
        run, fake = self.fresh(), Fake(spent(), TEXT, TEXT)
        self.calls(fake, run, LOCAL, 8192)
        self.assertEqual(run.think_floor, {})
        with mock.patch.object(models, "complete", fake):
            smith.complete_retrying(run, LOCAL, MSG, max_tokens=8192)  # a default (not decides) call of that size
        self.assertNotIn("think_budget", fake.sent[-1])
        self.assertNotIn("think", fake.sent[-1])

    def test_a_callers_own_think_or_budget_and_a_cli_profile_are_left(self):
        run, fake = self.fresh(), Fake(spent(), TEXT, TEXT, TEXT, TEXT)
        self.calls(fake, run, LOCAL, 8192)
        self.assertEqual(self.calls(fake, run, LOCAL, 8192, think_budget=512)[0]["think_budget"], 512)
        self.assertNotIn("think_budget", self.calls(fake, run, CLI, 8192)[0])

    def test_the_budget_call_that_spends_it_is_not_asked_again_by_the_retry(self):
        # models._reasoned closes a spent budget itself; a ModelError that still says reasoning is raised, not looped
        run, fake = self.fresh(), Fake(spent(), TEXT, spent())
        self.calls(fake, run, LOCAL, 8192)
        with mock.patch.object(models, "complete", fake), self.assertRaises(models.ModelError):
            smith.complete_retrying(run, LOCAL, MSG, decides=True, max_tokens=8192)
        self.assertEqual(len(fake.sent), 3)


class PlanningShare(Base):
    def test_the_share_is_a_named_fraction_of_the_time_limit(self):
        self.assertTrue(0 < smith.PLAN_SHARE < 1)
        run = self.fresh(deadline=900)
        self.assertFalse(smith._plan_spent(run))
        self.clock.now += 900 * smith.PLAN_SHARE - 1
        self.assertFalse(smith._plan_spent(run))
        self.clock.now += 2
        self.assertTrue(smith._plan_spent(run))

    def test_a_run_with_no_time_limit_never_runs_out_of_planning_clock(self):
        run = self.fresh()
        self.clock.now += 10 ** 6
        self.assertFalse(smith._plan_spent(run))
        self.assertFalse(smith._plan_late(run, "a second ask"))
        self.assertEqual(self.notes, [])

    def test_past_the_share_an_optional_step_is_skipped_with_a_note(self):
        run = self.fresh(deadline=900)
        self.assertFalse(smith._plan_late(run, "the re-ask"))
        self.clock.now += 400
        self.assertTrue(smith._plan_late(run, "the re-ask"))
        self.assertTrue(any("the re-ask" in t and "plan in hand" in t for t in self.said()), self.said())

    def brief(self, lines):
        return types.SimpleNamespace(meta={"rival_at": "now"}, section=lambda name: "\n".join(lines))

    def test_the_rival_lines_are_not_put_to_the_planner_past_the_share(self):
        run = self.fresh(deadline=900)
        run.plan_msgs = [{"role": "user", "content": "x"}]
        run.fm = lambda *a: (0, "")
        b = self.brief(["- [ ] rival reading: a ledger", "- [ ] rival approach: use sqlite"])
        asked = []
        with mock.patch.object(smith.core, "find_brief", return_value=b), \
                mock.patch.object(smith.core, "rival_gap", return_value=["2 open rival lines"]), \
                mock.patch.object(smith, "_ask", side_effect=lambda *a, **k: asked.append(a)):
            self.clock.now += 400
            self.assertTrue(smith._rival(run, LOCAL, "P", "T-0001"))
        self.assertEqual(asked, [], "past the share the plan's author is not asked")

    def test_the_rival_lines_are_put_to_the_planner_inside_the_share(self):
        run = self.fresh(deadline=900)
        run.plan_msgs = [{"role": "user", "content": "x"}]
        run.fm = lambda *a: (0, "")
        b = self.brief(["- [ ] rival reading: a ledger"])
        asked = []
        with mock.patch.object(smith.core, "find_brief", return_value=b), \
                mock.patch.object(smith.core, "rival_gap", return_value=["1 open rival line"]), \
                mock.patch.object(smith, "_ask", side_effect=lambda *a, **k: asked.append(a) or {"text": ""}):
            smith._rival(run, LOCAL, "P", "T-0001")
        self.assertEqual(len(asked), 1)

    def make(self, deadline=900):
        """_make_task on a fake fm whose first focus is refused for open rival lines."""
        run = self.fresh(deadline=deadline)
        run.plan = {"type": "FIX", "tier": "M", "title": "t", "criteria": [{"text": "c", "verify": "v"}], "steps": ["s"],
                    "readings": ["a", "b"], "options": [], "choice": 1, "confidence": 60, "why": "w"}
        run.after_tid = run.before_task = None
        run.unproven_lines, run.worker, run.request = [], None, "fix it"
        self.calls = []
        focus = [(1, "plan gate: 2 open rival lines"), (1, "plan gate: 2 open rival lines"), (0, "focused")]

        def fm(*a):
            self.calls.append(a)
            if a[:2] == ("task", "new"):
                return 0, json.dumps({"id": "T-0001"})
            if a[0] == "focus":
                return focus.pop(0)
            return 0, ""
        run.fm = fm
        return run

    def drive(self, run, elapsed):
        b = self.brief(["- [ ] rival reading: a ledger", "- [ ] rival approach: use sqlite", "- [x] other: done"])
        self.clock.now += elapsed
        with mock.patch.object(smith.core, "find_project", return_value="P"), \
                mock.patch.object(smith.core, "find_brief", return_value=b), \
                mock.patch.object(smith, "_rival", return_value=True), \
                mock.patch.object(smith, "_hold"), mock.patch.object(smith, "_journal_start"), \
                mock.patch.object(smith, "_replan", return_value="replanned") as replan:
            out = smith._make_task(run, LOCAL)
        return out, replan

    def test_open_rival_lines_past_the_share_are_waived_and_the_plan_in_hand_is_focused(self):
        run = self.make()
        out, replan = self.drive(run, 400)
        replan.assert_not_called()
        resolved = [c for c in self.calls if c[:3] == ("task", "dissent", "T-0001")]
        self.assertEqual([c[4] for c in resolved], ["1", "2"], "each open rival line, by its number, none other")
        self.assertTrue(all(c[3] == "resolve" and c[5].startswith("rejected:") for c in resolved), resolved)
        self.assertEqual([c[0] for c in self.calls if c[0] == "focus"], ["focus"] * 3, "refused, refused, then taken")
        self.assertEqual(run.tid, "T-0001")
        self.assertTrue(any("plan in hand" in t for t in self.said()), self.said())

    def test_inside_the_share_a_refused_focus_is_re_planned_as_before(self):
        run = self.make()
        out, replan = self.drive(run, 100)
        replan.assert_called_once()
        self.assertEqual([c for c in self.calls if c[:2] == ("task", "dissent")], [])

    def test_a_refusal_that_is_not_the_rival_lines_is_still_re_planned_past_the_share(self):
        run = self.make()
        b = self.brief(["- [x] rival reading: answered"])
        self.clock.now += 400
        with mock.patch.object(smith.core, "find_project", return_value="P"), \
                mock.patch.object(smith.core, "find_brief", return_value=b), \
                mock.patch.object(smith, "_rival", return_value=True), \
                mock.patch.object(smith, "_replan", return_value="replanned") as replan:
            smith._make_task(run, LOCAL)
        replan.assert_called_once()


class Stub:
    """The parts of a run that _drive touches: a scripted verify, a phase that may move the clock or write a file."""

    def __init__(self, root, clock, plan, deadline=None, fails=1):
        self.root, self.clock, self.plan, self.deadline = root, clock, plan, deadline
        self.ends = clock.now + deadline if deadline else None
        self.keep_cmd = self.keep_state = self.spec = self.harness_text = self.escalate = self.worker = None
        self.keep_logged = self.spec_logged = self.own_plan = False
        self.baseline, self.done, self.steps_done, self.repairable, self.tid = None, set(), False, True, "T-0001"
        self.repairs = {"seen": {}, "asked": set(), "doubt": {}}
        self.fails, self.phases, self.events, self.cost = fails, [], [], {}
        self.fix_cost, self.on_phase = 0, {}

    def log(self, **kw):
        pass

    def verify(self, when):
        return [(1, "check", "boom")] if when == "verify" and self.fails else []

    def harness(self, profile, failed):
        pass

    def red_scaffold(self, step):
        return ""

    def carried(self, n, profile):
        return ""

    def fence(self, text):
        return text

    def leave_out_built(self):
        pass

    def snapshot(self):
        pass

    def emit(self, event, text, quiet=False, **data):
        self.events.append((event, text, data))

    def phase(self, name, profile, prompt):
        self.phases.append((name, prompt))
        self.clock.now += self.cost.get(name.split(":")[0], 0)
        if name.startswith("fix:"):
            self.clock.now += self.fix_cost
        act = self.on_phase.get(name)
        if act:
            act()
        self.done.add(name)

    work = phase


class Drive(Base):
    """A run of _drive over the Stub run, with the git and finishing parts stubbed."""

    def setUp(self):
        super().setUp()
        for name, ret in (("_mend", False), ("_keep_end", None), ("_finish", None), ("_dirty", []), ("_numstat", {})):
            p = mock.patch.object(smith, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(smith, "_task_text", return_value="the task")
        p.start()
        self.addCleanup(p.stop)

    base_plan = {"steps": ["do it"], "criteria": [{"text": "c", "verify": "check"}], "tier": "S", "type": "FIX",
                 "title": "t"}

    def stub(self, **kw):
        return Stub(self.root, self.clock, dict(self.base_plan, **kw.pop("plan", {})), **kw)


class Rounds(Drive):
    def test_a_round_with_no_clock_left_is_not_counted_and_the_stop_names_the_clock(self):
        run = self.stub(deadline=900)
        run.cost["step"] = 880  # the step ends with 20 s of 900 left, under a tenth
        with self.assertRaises(smith.CodeError) as cm:
            smith._drive(run, LOCAL)
        text = str(cm.exception)
        self.assertIn("out of time", text)
        self.assertIn("--deadline 900", text)
        self.assertNotIn("still fails after 3", text)
        self.assertEqual([n for n, _ in run.phases if n.startswith("fix:")], [], "no round ran, none was counted")
        self.assertIn("0 fix rounds", text)
        self.assertEqual(cm.exception.then, "fm code --resume T-0001")

    def test_rounds_that_ran_are_counted_and_the_round_the_clock_cut_is_not(self):
        run = self.stub(deadline=900)
        run.fix_cost = 430  # each fix round takes 430 s: the second starts with 470 s left, the third with 40: not run
        with self.assertRaises(smith.CodeError) as cm:
            smith._drive(run, LOCAL)
        self.assertEqual([n for n, _ in run.phases if n.startswith("fix:")], ["fix:1", "fix:2"])
        self.assertIn("after 2 fix rounds", str(cm.exception))
        self.assertIn("out of time", str(cm.exception))

    def test_with_clock_to_spare_the_rounds_run_out_as_before(self):
        run = self.stub(deadline=900)
        with self.assertRaises(smith.Blocked) as cm:
            smith._drive(run, LOCAL)
        self.assertIn("still fails after 3 fix rounds", str(cm.exception))
        run = self.stub()  # no time limit at all
        with self.assertRaises(smith.Blocked):
            smith._drive(run, LOCAL)


class Creates(Drive):
    def plan(self, creates):
        return dict(self.base_plan, steps=["write cal.py", "wire it"], creates=creates)

    def changed(self, run):
        return [d for e, _, d in run.events if e == "changed"]

    def test_a_step_that_leaves_the_file_it_names_is_told_once(self):
        run = self.stub(plan=self.plan([["cal.py"], []]), fails=0)
        smith._drive(run, LOCAL)
        files = [(n, p) for n, p in run.phases if n == "step:1:files"]
        self.assertEqual(len(files), 1)
        self.assertIn("cal.py", files[0][1])
        self.assertEqual([n for n, _ in run.phases if n.endswith(":files")], ["step:1:files"], "step 2 names none")
        self.assertEqual(self.changed(run)[0].get("missing"), ["cal.py"], "still missing: said in the step's end event")
        self.assertTrue(any("cal.py" in t for e, t, _ in run.events if e == "note"))
        self.assertNotIn("missing", self.changed(run)[1])

    def test_the_file_made_after_the_telling_ends_the_step_plainly(self):
        run = self.stub(plan=self.plan([["cal.py"], []]), fails=0)

        def make():
            with open(os.path.join(self.root, "cal.py"), "w") as f:
                f.write("x = 1\n")
        run.on_phase["step:1:files"] = make
        smith._drive(run, LOCAL)
        self.assertNotIn("missing", self.changed(run)[0])

    def test_a_file_made_by_the_step_is_never_asked_for(self):
        run = self.stub(plan=self.plan([["cal.py"], []]), fails=0)

        def make():
            with open(os.path.join(self.root, "cal.py"), "w") as f:
                f.write("x = 1\n")
        run.on_phase["step:1"] = make
        smith._drive(run, LOCAL)
        self.assertEqual([n for n, _ in run.phases if n.endswith(":files")], [])

    def test_a_plan_with_no_creates_changes_nothing(self):
        for plan in (dict(self.base_plan, steps=["a", "b"]), self.plan("cal.py"), self.plan([[5], None]), self.plan([])):
            run = self.stub(plan=plan, fails=0)
            smith._drive(run, LOCAL)
            self.assertEqual([n for n, _ in run.phases], ["step:1", "step:2"], plan.get("creates"))
            self.assertTrue(all("missing" not in d for d in self.changed(run)))

    def test_only_paths_inside_the_project_count(self):
        run = self.stub(plan=self.plan([["../out.py", "/etc/x", "a\nb", "", "x" * 400], []]))
        self.assertEqual(smith.step_creates(run, 1), [])
        run = self.stub(plan=self.plan([["pkg/cal.py", "./cal.py", "pkg/cal.py"], []]))
        self.assertEqual(smith.step_creates(run, 1), ["pkg/cal.py", "cal.py"])

    def test_steps_folded_into_the_last_carry_their_files_with_it(self):
        run = self.stub(plan=self.plan([[], ["a.py"], ["b.py"]]))
        self.assertEqual(smith.step_creates(run, 2), ["a.py", "b.py"])
        self.assertEqual(smith.step_creates(run, 1), [])

    def test_the_follow_up_has_its_own_turns_and_gives_back_the_cap(self):
        run = self.stub(plan=self.plan([["cal.py"], []]), fails=0)
        run.max_steps, run.turns, run.turn_base = 5, 5, 0
        seen = []
        run.on_phase["step:1:files"] = lambda: seen.append(run.max_steps)
        smith._drive(run, LOCAL)
        self.assertGreater(seen[0] - (run.turns - run.turn_base), 0, "turns of its own on top of what was used")
        self.assertEqual(run.max_steps, 5, "no turns were used: the cap is as it was")

    def test_the_plan_prompt_asks_for_the_optional_field(self):
        self.assertIn('"creates"', smith.PLAN_PROMPT)


if __name__ == "__main__":
    unittest.main()
