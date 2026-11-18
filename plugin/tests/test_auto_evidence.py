"""T-3501: auto-evidence (R6, T-0063) matches the same check run another way: a cd to the root, nice, timeout or
env prefix, or a run naming a superset of the criterion's test modules. A narrower run records nothing."""
import json
import unittest

from test_hooks import HookCase

from sampo import core as c

OK = {"stdout": "Ran 3 tests in 0.1s\n\nOK", "stderr": "", "interrupted": False}
RUN = "python3 plugin/tests/run.py"


class SameCheckRunAnotherWay(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = self.task()  # its criterion 1 is `pytest`

    def ac(self, verify):
        self.fm("task", "ac", self.tid, "add", "suite passes", "--verify", verify)
        return len(c.find_brief(self.project(), self.tid).acceptance())

    def recorded(self, cmd, n, resp=OK):
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_response": resp})
        return [l for l in c.find_brief(self.project(), self.tid).evidence() if l.startswith(f"- (ac {n}) ")]

    def test_a_prefixed_run_of_the_verify_command_records_it(self):
        n = self.ac(f"{RUN} test_a")
        for cmd in (f"cd {self.repo} && nice -n 10 {RUN} test_a",
                    f"cd . ; timeout -s KILL --foreground 300 env FOO=1 BAR='x y' {RUN} test_a",
                    f"nice -5 timeout 60s FOREMAN_X=1 {RUN} test_a"):
            before = len(self.recorded("true", n))
            got = self.recorded(cmd, n)
            self.assertEqual(len(got), before + 1, cmd)
            self.assertIn("[ran]", got[-1])

    def test_a_superset_of_modules_records_it_and_fewer_or_a_narrowed_run_does_not(self):
        n = self.ac(f"{RUN} test_a test_b")
        self.assertEqual(len(self.recorded(f"cd {self.repo} && nice -n 10 {RUN} test_b test_c test_a", n)), 1)
        for cmd in (f"{RUN} test_a", f"{RUN} test_a test_b -k slow", f"{RUN} test_a.Case.test_x test_b",
                    f"{RUN} test_a test_b -x", f"{RUN} -p test_a test_b", f"python3 other/run.py test_a test_b",
                    f"cd /tmp && {RUN} test_a test_b"):
            self.assertEqual(len(self.recorded(cmd, n)), 1, cmd)

    def test_a_failing_superset_run_records_nothing_but_a_failing_prefixed_run_does(self):
        n = self.ac(f"{RUN} test_a")
        fail = lambda cmd: self.hook("PostToolUseFailure", {"tool_name": "Bash", "tool_input": {"command": cmd},
                                                            "error": "Exit code 1\nFAILED (failures=1)"})
        fail(f"{RUN} test_a test_b")
        self.assertEqual(self.recorded("true", n), [], "test_b may be the one failing")
        fail(f"nice -n 10 {RUN} test_a")
        self.assertIn("✗ exit 1", self.recorded("true", n)[-1])

    def test_a_run_with_a_second_line_is_never_a_superset(self):
        """T-3501 review: newlines were collapsed to spaces, so `<verify>` then `echo done` on the next line read as a
        superset of modules (echo, done), and its exit (echo's 0) recorded the check as passed."""
        n = self.ac(f"{RUN} test_a")
        crashed = {"stdout": "Segmentation fault\ndone", "stderr": "", "interrupted": False}
        self.assertEqual(self.recorded(f"{RUN} test_a\necho done", n, crashed), [])
        self.assertEqual(len(self.recorded(f"{RUN} test_a\n", n)), 1, "a trailing newline is still the one command")

    def test_a_criterion_with_no_modules_matches_only_itself(self):
        self.assertEqual(self.recorded("pytest test_a", 1), [], "a run of one module is narrower than all of them")
        self.assertEqual(len(self.recorded("nice pytest", 1)), 1)

    def test_a_criterion_prefix_is_not_dropped_from_the_criterion(self):
        n = self.ac(f"FOREMAN_SLOW=1 {RUN} test_a")
        self.assertEqual(self.recorded(f"{RUN} test_a", n), [], "the run lacks the criterion's own env")
        self.assertEqual(len(self.recorded(f"cd {self.repo} && FOREMAN_SLOW=1 {RUN} test_a test_b", n)), 1)

    def test_a_run_that_cds_into_a_lane_from_the_main_checkout_is_the_lanes_tasks_evidence(self):
        # a builder subagent's hook reports the main checkout's cwd while its run cds into its own lane
        lane_tid = self.task("Lane work", focus=False)  # its criterion 1 is `pytest`
        self.fm("task", "ac", lane_tid, "add", "suite passes", "--verify", f"{RUN} test_a")
        path = json.loads(self.fm("lane", "new", lane_tid, "--json").stdout)["path"]
        self.fm("focus", lane_tid, cwd=path)
        main_n = self.ac(f"{RUN} test_a")
        self.recorded(f"cd {path} && nice -n 10 {RUN} test_a test_b", main_n)
        lane_ev = [l for l in c.find_brief(self.project(), lane_tid).evidence() if l.startswith("- (ac 2) ")]
        self.assertEqual(len(lane_ev), 1, "the lane's active task gets it")
        self.assertEqual(self.recorded("true", main_n), [], "not the main checkout's task: its files didn't run")

    def test_a_piped_superset_run_is_judged_by_its_summary(self):  # T-1256 still holds
        n = self.ac(f"{RUN} test_a")
        self.assertEqual(self.recorded(f"nice {RUN} test_a test_b 2>&1 | tail -3", n,
                                       {"stdout": "no summary", "stderr": ""}), [])
        got = self.recorded(f"nice {RUN} test_a test_b 2>&1 | tail -3", n)
        self.assertEqual(len(got), 1)
        self.assertIn("exit 0", got[0])


if __name__ == "__main__":
    unittest.main()
