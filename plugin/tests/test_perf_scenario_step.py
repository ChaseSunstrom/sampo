"""T-4583: the measured-scenario rule of a PERFORMANCE plan (T-4579) holds only when the first criterion's verify RUNS the
scenario's command as a step of its own. It was a substring test: `echo 'python3 test_calc.py'`, a comment, or the command
quoted as an argument to another program passed it without timing the scenario. Now the verify and the scenario are both
split into their shell steps by the guard's own tokenizer (quotes, comments and separators read as a shell reads them), a
leading env assignment, time or timeout wrapper is set aside, and the scenario's steps must appear among the verify's."""
import unittest

from test_perf_measure_first import FIELDS, PERF, SCENARIO
from test_smith_survey import SurveyCase

from sampo import smith


class ScenarioStep(SurveyCase):
    def problem(self, verify, cmd=SCENARIO):
        run = self.run_object(measure_cmd=cmd, measured=FIELDS)
        run.plan = dict(PERF, criteria=[{"text": "the report runs in time", "verify": verify}])
        return smith._plan_problem(run)

    def refused(self, verify, cmd=SCENARIO):
        why = self.problem(verify, cmd)
        self.assertIsNotNone(why, verify)
        self.assertIn("first criterion", why)
        self.assertIn(cmd, why, "the refusal names the scenario")
        self.assertIn("own step", why, "and says what is wanted: the command run as a step of its own")

    def test_a_mention_is_not_a_run(self):
        for verify in (f"echo '{SCENARIO}'", f'echo "ran {SCENARIO}"', f"echo {SCENARIO!r} && python3 -c 'print(1)'",
                       f"python3 -c 'print(1)' # {SCENARIO}", f"grep -c '{SCENARIO}' notes.txt",
                       f"python3 -c \"import sys; assert '{SCENARIO}'\"", f"printf '%s' \"{SCENARIO}\""):
            with self.subTest(verify):
                self.refused(verify)

    def test_a_different_command_with_the_same_start_is_not_it(self):
        self.refused("python3 test_calc.py --small")
        self.refused("python3 test_calc.pyc")
        self.refused("python3 other.py test_calc.py")

    def test_the_command_as_a_step_of_its_own_passes(self):
        for verify in (SCENARIO, f"timeout 60 {SCENARIO}", f"time {SCENARIO}", f"TZ=UTC {SCENARIO}",
                       f"timeout -k 5 60 {SCENARIO}", f"{SCENARIO} > /dev/null", f"{SCENARIO}   ",
                       "python3   test_calc.py"):
            with self.subTest(verify):
                self.assertIsNone(self.problem(verify))

    def test_an_assertion_step_may_come_before_or_after(self):
        for verify in (f"timeout 60 {SCENARIO} && python3 -c 'import calc; assert calc.add(1, 2) == 3'",
                       f"python3 -c 'import calc' ; timeout 60 {SCENARIO}", f"echo start && {SCENARIO}",
                       f"{SCENARIO}\npython3 -c 'print(1)'"):
            with self.subTest(verify):
                self.assertIsNone(self.problem(verify))

    def test_a_timed_scenario_the_measure_wrote_with_a_wrapper_is_matched_without_it(self):
        self.assertIsNone(self.problem(SCENARIO, cmd=f"timeout 90 {SCENARIO}"))
        self.assertIsNone(self.problem(f"time {SCENARIO}", cmd=f"/usr/bin/time -v {SCENARIO}"))

    def test_a_compound_scenario_needs_every_step_in_order(self):
        cmd = "python3 gen.py 10000 && python3 test_calc.py"
        self.assertIsNone(self.problem("python3 gen.py 10000 && timeout 60 python3 test_calc.py", cmd=cmd))
        self.refused("python3 test_calc.py", cmd=cmd)
        self.refused("python3 test_calc.py && python3 gen.py 10000", cmd=cmd)
        self.refused(f"echo '{cmd}'", cmd=cmd)

    def test_a_quoted_argument_of_the_scenario_itself_is_kept_whole(self):
        cmd = 'python3 report.py "day 1.log"'
        self.assertIsNone(self.problem("timeout 60 python3 report.py 'day 1.log'", cmd=cmd))
        self.refused("python3 report.py day 1.log", cmd=cmd)

    def test_only_the_first_criterion_and_only_a_change_plan(self):
        run = self.run_object(measure_cmd=SCENARIO, measured=FIELDS)
        run.plan = dict(PERF, criteria=[PERF["criteria"][0], {"text": "t", "verify": f"echo '{SCENARIO}'"}])
        self.assertIsNone(smith._plan_problem(run), "the second criterion is free")
        run.plan = dict(PERF, type="RESEARCH", criteria=[{"text": "t", "verify": f"echo '{SCENARIO}'"}])
        self.assertIsNone(smith._plan_problem(run), "a research plan is not held to it")


if __name__ == "__main__":
    unittest.main()
