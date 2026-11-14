"""T-4595: a speed-up request on a project with no documents is measured before its first edit, judged at no extra model
call. T-4579 measures only where the survey already asked the judge (the survey needs documents), so on a project with
none a request to make something faster was planned with no before-measure. The plan reply itself now carries an
optional "speed_up" field (true or false) the planner fills in the call it makes anyway; when it is true, no judge has
spoken and nothing is measured yet, Smith takes the same measure then (T-4579's phase) and puts the facts to the planner
once, in a re-plan that must run the measured scenario first. A reply without the field, or with any value but the JSON
true, behaves as before. Nothing reads the request's words: the model's field decides. The scripted fake model is
test_code's."""
import json
import subprocess
import unittest
from unittest import mock

from test_code import CLOSE, call, plan
from test_perf_measure_first import (MEASURE_DONE, MEASURED, NUMBERS, PART, PERF, REQUEST, SCENARIO, PROFILE, TIME,
                                     judged)
from test_smith_small import DONE, FIX_EDIT
from test_smith_survey import READ_ALL, SUMMARY, SurveyCase

from sampo import smith


def says_speed_up(p, value=True):
    return dict(p, speed_up=value)


class NoDocuments(SurveyCase):
    def setUp(self):
        super().setUp()
        for rel in ("README.md", "docs/rules.md", "docs/notes.md"):
            subprocess.run(["git", "-C", self.repo, "rm", "-q", "-f", rel], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "-m", "no docs"], check=True)

    def measured(self, f):
        return [i for i, s in enumerate(f.seen) if "Before any plan, measure" in json.dumps(s["messages"])]

    def judge_calls(self, f):
        return [i for i, s in enumerate(f.seen) if smith.WANTS_PROMPT[:60] in json.dumps(s["messages"])]

    def first_edit(self, f):
        return next(i for i, s in enumerate(f.seen)
                    if "edit" in {t["function"]["name"] for t in s.get("tools") or []})

    def test_a_plan_reply_with_speed_up_true_is_measured_before_the_first_edit_with_no_judge_call(self):
        f, r, ev = self.go(plan(says_speed_up(PART)), TIME, PROFILE, MEASURE_DONE, plan(PERF), FIX_EDIT, DONE, CLOSE,
                           NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        turns = self.measured(f)
        self.assertEqual(len(turns), 3, "two runs and the done")
        self.assertGreater(self.first_edit(f), turns[-1], "the measure comes before the first edit")
        self.assertEqual(self.judge_calls(f), [], "no model call of the judge's, on any run")
        kinds = [e["measure"] for e in self.notes(ev, "measure")]
        self.assertEqual(kinds, ["start", "summary"])
        self.assertEqual(self.task()["status"], "done")

    def test_the_planner_is_handed_the_facts_once_and_the_scenario_is_the_first_criterion(self):
        f, r, ev = self.go(plan(says_speed_up(PART)), TIME, PROFILE, MEASURE_DONE, plan(PERF), FIX_EDIT, DONE, CLOSE,
                           NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        asks = [i for i, s in enumerate(f.seen) if smith.PLAN_PROMPT[:40] in json.dumps(s["messages"][0])]
        self.assertEqual(len(asks), 2, "the plan and one re-plan, no more")
        said = f.said(asks[-1])
        self.assertIn(MEASURED, said)
        self.assertIn(SCENARIO, said)
        self.assertIn("guess", said)
        self.assertIn("first criterion", said)
        self.assertNotIn("Measured by running", f.said(asks[0]), "the first plan was made before the measure")
        plan_note = [e for e in ev if e["event"] == "plan"][-1]
        self.assertIn(SCENARIO, plan_note["plan"]["criteria"][0]["verify"], "the plan that stands proves the scenario")

    def test_a_reply_without_the_field_behaves_as_before(self):
        f, r, ev = self.go(plan(PERF), FIX_EDIT, DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.measured(f), [])
        self.assertEqual(self.notes(ev, "measure"), [])
        self.assertEqual(self.judge_calls(f), [])

    def test_only_the_json_true_counts(self):
        for value in (False, "true", 1, "yes", None):
            with self.subTest(value=value):
                f, r, ev = self.go(plan(says_speed_up(PERF, value)), FIX_EDIT, DONE, CLOSE, NUMBERS, request=REQUEST)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.measured(f), [])
                self.assertEqual(self.notes(ev, "measure"), [])

    def test_a_scenario_that_cannot_be_run_leaves_a_note_and_the_first_plan_stands(self):
        gave_up = call("done", summary="SCENARIO: none\nMEASURED: none: the report needs a database that is not here")
        f, r, ev = self.go(plan(says_speed_up(PART)), gave_up, FIX_EDIT, DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([e["measure"] for e in self.notes(ev, "measure")], ["start", "none"])
        asks = [i for i, s in enumerate(f.seen) if smith.PLAN_PROMPT[:40] in json.dumps(s["messages"][0])]
        self.assertEqual(len(asks), 1, "nothing was measured: no re-plan")

    def test_the_prompt_names_the_field_and_no_word_to_match(self):
        self.assertIn('"speed_up"', smith.PLAN_PROMPT)
        self.assertIn("faster", smith.PLAN_PROMPT)

    def test_the_run_that_asks_nothing_changes_is_never_measured_whatever_the_reply_says(self):
        for why, kw, profile in (("--verify", dict(verify_cmd="python3 test_calc.py"), {"kind": "openai"}),
                                 ("--ask", dict(ask=True), {"kind": "openai"}),
                                 ("a worker CLI", dict(worker="claude"), {"kind": "openai"}),
                                 ("a CLI planner", {}, {"kind": "cli"})):
            with self.subTest(why):
                run = self.run_object(**kw)
                run.wants = None
                self.assertIsNotNone(smith.measure_skip(run, profile, plan_says=True))
        run = self.run_object()
        run.wants = None
        self.assertIsNone(smith.measure_skip(run, {"kind": "openai"}, plan_says=True))
        self.assertIsNotNone(smith.measure_skip(run, {"kind": "openai"}, dry=True, plan_says=True), "--dry-plan")


class ReplanFails(SurveyCase):
    def go_unit(self, replan):
        run = self.run_object()
        run.wants = None
        run.plan, msgs = dict(PART, speed_up=True), [{"role": "user", "content": "x"}]
        with mock.patch.object(smith, "_measure", return_value=(SCENARIO, MEASURED)), \
                mock.patch.object(smith, "slot", side_effect=replan):
            smith._plan_measure(run, {"kind": "openai"}, msgs, smith.check_plan, False)
        return run, msgs

    def test_a_replan_with_no_valid_plan_keeps_the_first_and_leaves_the_messages_as_they_were(self):
        run, msgs = self.go_unit(smith.NoValidSlot("no valid plan"))
        self.assertEqual(run.plan["criteria"], PART["criteria"])
        self.assertEqual(len(msgs), 1, "the facts message is taken back out")

    def test_a_replan_that_answers_keeps_the_first_plan(self):
        run, msgs = self.go_unit([{"answer": "it is slow because of the regex"}])
        self.assertEqual(run.plan["criteria"], PART["criteria"])

    def test_a_replan_replaces_the_plan(self):
        run, msgs = self.go_unit([PERF])
        self.assertEqual(run.plan, PERF)


class JudgedRuns(SurveyCase):
    """A run where the survey already asked the judge is not changed: its verdict stands, the plan's field is not read."""

    def test_the_judge_said_not_a_speed_up_so_the_plans_field_starts_no_measure(self):
        f, r, ev = self.go(judged(speed_up=False), *READ_ALL, call("done", summary=SUMMARY),
                           plan(says_speed_up(PERF)), FIX_EDIT, DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.notes(ev, "measure"), [])
        self.assertFalse([1 for s in f.seen if "Before any plan, measure" in json.dumps(s["messages"])])

    def test_the_judge_said_a_speed_up_is_measured_once_before_the_plan_as_in_t_4579(self):
        f, r, ev = self.go(judged(), *READ_ALL, call("done", summary=SUMMARY), TIME, PROFILE, MEASURE_DONE,
                           plan(says_speed_up(PERF)), FIX_EDIT, DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([e["measure"] for e in self.notes(ev, "measure")], ["start", "summary"], "once")
        asks = [i for i, s in enumerate(f.seen) if smith.PLAN_PROMPT[:40] in json.dumps(s["messages"][0])]
        self.assertEqual(len(asks), 1, "no re-plan: the planner was handed the facts at its first ask")


if __name__ == "__main__":
    unittest.main()
