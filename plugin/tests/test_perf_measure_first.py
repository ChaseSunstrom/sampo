"""T-4579: on a speed-up request Smith measures before it plans. Measured 2026-11-13 (exam cascade11l, perf-log-rollup, 27B):
the request said 'It's the parsing, surely: that line regex is enormous and it runs on every single line. Can you make it
fast again?' about a report on a 150,000-line file; the plan rewrote the parser, its own timing check on 1200 parse calls
went green, and the run scored 0 of 4: the slowness the user sees was never timed or profiled. Now the one judge call says
whether the request is a speed-up (a field, never a word list); a bounded measure phase (read, search, run, no edits) times
the scenario the user names end to end at two sizes and profiles it; its SCENARIO and MEASURED fields go whole to the
planner beside the user's cause, labelled a guess; and a plan whose first criterion does not run that scenario is
refused with it named. A scenario that can't be measured leaves a note and the old path. The scripted fake model is
test_code's."""
import json
import os
import subprocess
import unittest
from unittest import mock

from test_code import CLOSE, PLAN, call, plan
from test_smith_small import DONE, FIX_EDIT
from test_smith_survey import READ_ALL, SUMMARY, SurveyCase

from sampo import smith

REQUEST = ("It's the parsing, surely: that line regex is enormous and it runs on every single line. Can you make it "
           "fast again?")
SCENARIO = "python3 test_calc.py"
MEASURED = ("1000 lines: 0.4 s; 10000 lines: 41 s (grows with the square of the lines). cProfile top: "
            "calc.add_all 38.5 s cumulative, 10000 calls; calc.parse_line 1.2 s")
FIELDS = f"SCENARIO: {SCENARIO}\nMEASURED: {MEASURED}"
PERF = dict(PLAN, type="PERFORMANCE", title="Make the report fast",
            criteria=[{"text": "the report runs in time", "verify": "timeout 60 " + SCENARIO}])
PART = dict(PERF, criteria=[{"text": "parse_line is fast", "verify": 'python3 -c "import calc; calc.add(1, 2)"'}])


def judged(speed_up=True, wants_change=True):
    return {"text": json.dumps({"wants_change": wants_change, "whole": False, "find_fix": False, "speed_up": speed_up,
                                "why": "the request is to make it fast"})}


TIME = call("run", cmd=SCENARIO)
PROFILE = call("run", cmd="python3 -m cProfile -s cumtime test_calc.py")
MEASURE_DONE = call("done", summary=FIELDS)
# a PERFORMANCE task is closed with its Measurements: the close asks for them in one more slot
NUMBERS = {"text": json.dumps({"numbers": "report seconds: 41 → 3 (timeout 60 python3 test_calc.py)"})}


class MeasureFields(unittest.TestCase):
    """measure_fields reads the SCENARIO and MEASURED fields wherever they sit, as survey_fields reads the survey's."""

    def test_a_line_field_with_a_multi_line_profile_and_a_blank_line_inside(self):
        text = f"SCENARIO: `{SCENARIO}`\nMEASURED: 1000 lines 0.4 s\n10000 lines 41 s\n\ncalc.add_all 38 s\ncalc.parse 1 s"
        cmd, measured = smith.measure_fields(text)
        self.assertEqual(cmd, SCENARIO, "no backticks around it")
        self.assertIn("calc.add_all 38 s", measured, "a profile's blank line does not end the field")
        self.assertIn("10000 lines 41 s", measured)

    def test_inline_in_one_line_and_bold_labels(self):
        cmd, measured = smith.measure_fields(f"I ran it. SCENARIO: {SCENARIO} MEASURED: {MEASURED}")
        self.assertEqual((cmd, measured), (SCENARIO, MEASURED))
        cmd, measured = smith.measure_fields(f"**SCENARIO:** {SCENARIO}\n**MEASURED:** {MEASURED}")
        self.assertEqual(cmd, SCENARIO)
        self.assertTrue(measured.startswith("**") or measured.startswith("1000"), measured)

    def test_a_command_keeps_its_own_quotes(self):
        cmd, _ = smith.measure_fields('SCENARIO: python3 report.py "day 1.log"\nMEASURED: 3 s')
        self.assertEqual(cmd, 'python3 report.py "day 1.log"')
        cmd, _ = smith.measure_fields("SCENARIO: 'python3 report.py'\nMEASURED: 3 s")
        self.assertEqual(cmd, "python3 report.py", "a pair around the whole command comes off")

    def test_none_unknown_absent_and_a_half_field_measure_nothing(self):
        for text in ("SCENARIO: none\nMEASURED: none: it needs a database", "SCENARIO: n/a\nMEASURED: n/a", "",
                     None, "DOCS: x\nTESTS: pytest", f"SCENARIO: {SCENARIO}", f"MEASURED: {MEASURED}",
                     f"SCENARIO: {SCENARIO}\nMEASURED: none: could not time it", "SCENARIO: unknown\nMEASURED: 3 s"):
            self.assertEqual(smith.measure_fields(text), (None, None), repr(text))

    def test_the_surveys_own_fields_are_not_changed_by_the_measure_labels(self):
        self.assertEqual([l for l, _ in smith.survey_fields("DOCS: a\nSCENARIO: x\nTESTS: t")], ["DOCS", "TESTS"])
        self.assertEqual(smith.tests_line("TESTS: pytest\nMEASURED: 3 s"), "pytest")

    def test_the_text_is_bounded(self):
        _, measured = smith.measure_fields(f"SCENARIO: {SCENARIO}\nMEASURED: " + "x" * 20000)
        self.assertLessEqual(len(measured), smith.MEASURE_CHARS)


class SpeedUpJudgment(SurveyCase):
    """The request judge's one call carries the speed_up field; nothing else decides."""

    def ask(self, reply):
        real = self.run_object()
        real.__dict__.pop("wants", None)
        real.request = REQUEST
        with mock.patch.object(smith, "_ask", return_value={"text": json.dumps(reply)}) as asked:
            smith.wants_change(real, {"kind": "openai"})
        return real, asked

    def test_the_prompt_asks_for_the_field_and_names_no_word_to_match(self):
        self.assertIn('"speed_up"', smith.WANTS_PROMPT)
        self.assertLessEqual(smith.JUDGE_TOKENS, 256, "the judge's reply stays small")

    def test_a_true_field_is_the_runs_speed_up_and_is_in_the_note(self):
        run, asked = self.ask({"wants_change": True, "speed_up": True, "why": "fast again"})
        self.assertTrue(run.speed_up)
        self.assertEqual(asked.call_count, 1, "one call, no second one")

    def test_absent_or_odd_means_not_a_speed_up(self):
        for reply in ({"wants_change": True, "why": "x"}, {"wants_change": True, "speed_up": "yes", "why": "x"},
                      {"wants_change": True, "speed_up": 1, "why": "x"}):
            with self.subTest(reply=reply):
                self.assertFalse(self.ask(reply)[0].speed_up)

    def test_no_valid_verdict_is_never_a_speed_up_however_the_request_reads(self):
        real = self.run_object()
        real.__dict__.pop("wants", None)
        real.request = "make it faster, optimise the performance"
        with mock.patch.object(smith, "_ask", return_value={"text": "no json"}):
            smith.wants_change(real, {"kind": "openai"})
        self.assertFalse(real.speed_up, "the fallback word lists decide wants_change only, never this")


class MeasureFirst(SurveyCase):
    def sent(self, f, marker):
        return [i for i, s in enumerate(f.seen) if marker in json.dumps(s["messages"][0])]

    def measured(self, f):
        return [i for i, s in enumerate(f.seen) if "Before any plan, measure" in json.dumps(s["messages"])]

    def test_a_speed_up_is_measured_before_the_plan_and_the_planner_is_handed_the_facts(self):
        f, r, ev = self.go(judged(), *READ_ALL, call("done", summary=SUMMARY), TIME, PROFILE, MEASURE_DONE, plan(PERF),
                           FIX_EDIT, DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        turns = self.measured(f)
        self.assertEqual(len(turns), 3, "two runs and the done, then the plan")
        names = {t["function"]["name"] for t in f.seen[turns[0]]["tools"]}
        self.assertTrue({"read", "search", "run", "done"} <= names, names)
        self.assertFalse(names & {"edit", "write"}, "it can run, never edit or write")
        at = self.planner(f)
        self.assertGreater(at, turns[-1], "the plan comes after the measure")
        said = f.said(at)
        self.assertIn(MEASURED, said, "the measured field, whole")
        self.assertIn(SCENARIO, said)
        self.assertIn("guess", said, "the user's cause is labelled a guess beside the facts")
        self.assertIn("first criterion", said)
        note = self.notes(ev, "measure")
        self.assertEqual([e["measure"] for e in note], ["start", "summary"])
        self.assertEqual(note[-1]["scenario"], SCENARIO)
        self.assertEqual(self.task()["status"], "done")

    def test_no_edit_is_offered_while_it_measures(self):
        f, r, ev = self.go(judged(), *READ_ALL, call("done", summary=SUMMARY), TIME, MEASURE_DONE, plan(PERF), FIX_EDIT,
                           DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for i in self.measured(f):
            names = {t["function"]["name"] for t in f.seen[i]["tools"]}
            self.assertFalse(names & {"edit", "write"}, names)

    def test_a_request_that_is_not_a_speed_up_is_not_measured(self):
        f, r, ev = self.go(judged(speed_up=False), *READ_ALL, call("done", summary=SUMMARY), plan(PLAN), FIX_EDIT, DONE,
                           CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.measured(f), [])
        self.assertEqual(self.notes(ev, "measure"), [])
        self.assertNotIn("guess", f.said(self.planner(f)))

    def test_a_plan_whose_first_criterion_times_a_part_is_sent_back_with_the_scenario_named(self):
        f, r, ev = self.go(judged(), *READ_ALL, call("done", summary=SUMMARY), TIME, MEASURE_DONE, plan(PART), plan(PERF),
                           FIX_EDIT, DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        back = f.said(self.planner(f) + 1)
        self.assertIn(SCENARIO, back, "the refusal names the measured scenario")
        self.assertIn("first criterion", back)
        self.assertEqual(self.task()["status"], "done")

    def test_a_plan_that_still_times_a_part_after_the_second_ask_is_refused(self):
        f, r, ev = self.go(judged(), *READ_ALL, call("done", summary=SUMMARY), TIME, MEASURE_DONE, plan(PART), plan(PART),
                           request=REQUEST)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("refused", r.stdout + r.stderr)
        self.assertIn(SCENARIO, r.stdout + r.stderr, "and the scenario it should have timed")

    def test_an_unmeasurable_scenario_leaves_a_note_and_the_old_path(self):
        gave_up = call("done", summary="SCENARIO: none\nMEASURED: none: the report needs a database that is not here")
        f, r, ev = self.go(judged(), *READ_ALL, call("done", summary=SUMMARY), gave_up, plan(PART), FIX_EDIT, DONE, CLOSE,
                           NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kinds = [e["measure"] for e in self.notes(ev, "measure")]
        self.assertEqual(kinds, ["start", "none"])
        self.assertIn("database", json.dumps(self.notes(ev, "measure")[-1]), "the note says why")
        said = f.said(self.planner(f))
        self.assertNotIn("guess", said)
        self.assertNotIn("Measured by running", said)
        self.assertEqual(self.task()["status"], "done", "a plan that times a part is taken as before")

    def test_a_measure_out_of_turns_is_asked_for_its_fields_alone(self):
        runs = [call("run", cmd=f"echo {n}") for n in range(smith.MEASURE_TURNS)]
        f, r, ev = self.go(judged(), *READ_ALL, call("done", summary=SUMMARY), *runs, MEASURE_DONE, plan(PERF), FIX_EDIT,
                           DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kinds = [e["measure"] for e in self.notes(ev, "measure")]
        self.assertEqual(kinds, ["start", "wrap", "summary"])
        wrap = [i for i in self.measured(f) if smith.MEASURE_WRAP[:30] in f.said(i)]
        self.assertTrue(wrap, "the wrap-up request was sent")
        self.assertEqual({t["function"]["name"] for t in f.seen[wrap[-1]]["tools"]}, {"done"}, "the done tool alone")
        self.assertIn(MEASURED, f.said(self.planner(f)))

    def test_a_measure_that_wrote_a_file_in_the_project_is_put_back_and_its_facts_are_not_trusted(self):
        # the guard refuses a write the command names; a script that writes its own log is what slips past it
        with open(os.path.join(self.repo, "scenario.py"), "w", encoding="utf-8") as h:
            h.write("open('big.log', 'w').write('x' * 100)\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "-m", "a scenario that logs"], check=True)
        wrote = call("run", cmd="python3 scenario.py")
        f, r, ev = self.go(judged(), *READ_ALL, call("done", summary=SUMMARY), wrote, MEASURE_DONE, plan(PART), FIX_EDIT,
                           DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "big.log")), "what the measure made is removed")
        last = self.notes(ev, "measure")[-1]
        self.assertEqual(last["measure"], "dirty")
        self.assertIn("big.log", json.dumps(last))
        self.assertNotIn("Measured by running", f.said(self.planner(f)))

    def test_a_project_with_no_documents_pays_no_judge_call_for_the_measure(self):
        # the judge is asked by the survey, which needs documents: with none, the measure adds no model call of its own
        for rel in ("README.md", "docs/rules.md", "docs/notes.md"):
            subprocess.run(["git", "-C", self.repo, "rm", "-q", "-f", rel], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "-m", "no docs"], check=True)
        f, r, ev = self.go(plan(PERF), FIX_EDIT, DONE, CLOSE, NUMBERS, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.notes(ev, "measure"), [], "not judged, not measured")
        self.assertEqual(self.notes(ev, "survey"), [], "no documents, no survey")

    def test_verify_and_ask_runs_are_never_measured(self):
        for why, run in (("--verify", self.run_object(verify_cmd="python3 test_calc.py", speed_up=True)),
                         ("--ask", self.run_object(ask=True, speed_up=True)),
                         ("a worker CLI", self.run_object(worker="claude", speed_up=True)),
                         ("not a speed-up", self.run_object(speed_up=False))):
            with self.subTest(why):
                self.assertIsNotNone(smith.measure_skip(run, {"kind": "openai"}))
        self.assertIsNotNone(smith.measure_skip(self.run_object(speed_up=True), {"kind": "cli"}), "a CLI planner")
        self.assertIsNone(smith.measure_skip(self.run_object(speed_up=True), {"kind": "openai"}))


class ScenarioRule(SurveyCase):
    """_plan_problem: the first criterion of a change plan runs the measured scenario, not a part of it."""

    def problem(self, plan_, cmd=SCENARIO, **kw):
        run = self.run_object(measure_cmd=cmd, measured=FIELDS, **kw)
        run.plan = plan_
        return smith._plan_problem(run)

    def test_the_scenario_inside_a_timing_wrapper_passes(self):
        self.assertIsNone(self.problem(PERF))
        for verify in ("time " + SCENARIO, f"python3 -c 'import time; t=time.time()' ; {SCENARIO}",
                       "timeout 20 python3   test_calc.py"):
            with self.subTest(verify):
                self.assertIsNone(self.problem(dict(PERF, criteria=[{"text": "t", "verify": verify}])))

    def test_a_part_of_it_is_refused_naming_the_scenario_and_the_first_criterion(self):
        why = self.problem(PART)
        self.assertIn("refused", why)
        self.assertIn(SCENARIO, why)
        self.assertIn("first criterion", why)

    def test_only_the_first_criterion_must_run_it(self):
        two = dict(PERF, criteria=[PERF["criteria"][0], {"text": "parse is fast", "verify": "python3 -c 'print(1)'"}])
        self.assertIsNone(self.problem(two))
        swapped = dict(PERF, criteria=[two["criteria"][1], PERF["criteria"][0]])
        self.assertIn("first criterion", self.problem(swapped))

    def test_no_measured_scenario_is_no_rule(self):
        self.assertIsNone(self.problem(PART, cmd=None))

    def test_a_research_plan_is_not_held_to_it(self):
        run = self.run_object(measure_cmd=SCENARIO, measured=FIELDS)
        run.plan = dict(PART, type="RESEARCH")
        self.assertIsNone(smith._plan_problem(run))


if __name__ == "__main__":
    unittest.main()
