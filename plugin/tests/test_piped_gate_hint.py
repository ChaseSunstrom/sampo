"""T-4514: a builder's own runs are never piped. 20 of 23 'piped gate' refusals on the JARVIS box in 38 hours were
builders piping a check through tail: the lane brief shows the builder's own command unpiped with the reason in one
line. T-4511 (re-landed by T-4553): the refusal names the same command with the pipe removed, spelled sampo."""
import json
import unittest

from helpers import ForemanTestCase, read_text


class BriefShowsTheUnpipedGate(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def test_the_brief_names_the_builders_own_command_unpiped_with_the_reason_on_one_line(self):
        # T-4514: a builder never runs the wide check; the no-pipe rule stays for the commands it does run
        tid = json.loads(self.fm("task", "new", "Gate hint", "--type", "FIX", "--tier", "S", "--ac",
                                 "gate hint works :: python3 -c 'print(1)'", "--step", "build it",
                                 "--json").stdout)["id"]
        brief = read_text(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"])
        own = f'`sampo task evidence {tid} --step N --run "<cmd>"`'
        lines = [line for line in brief.splitlines() if own in line]
        self.assertTrue(lines, "the brief shows the builder's own evidence command unpiped, in backticks")
        self.assertTrue(any("pipe" in line and "exit status" in line for line in lines),
                        "the reason sits on the same line as the command")
        for line in brief.splitlines():
            if "`sampo check --affected" in line:  # a command shown in backticks, as opposed to prose
                self.assertIn("Never run", line, "no brief line tells a builder to run the wide check")
        self.assertNotIn(f"--evidence {tid}", brief, "no brief line runs a gate with --evidence")
        for line in brief.splitlines():
            self.assertNotRegex(line, r"`(?:sampo|python3)[^`]*\|[^`]*`",
                                "no piped form is shown as the command to run")


class RefusalNamesTheCommand(unittest.TestCase):  # T-4511 (re-landed): the exact command to run, the pipe removed
    def test_the_refusal_names_the_same_command_with_the_pipe_removed_and_spelled_sampo(self):
        from sampo import hooks
        cases = {
            "sampo check --evidence T-1 --step 1 2>&1 | tail -8": "sampo check --evidence T-1 --step 1",
            "fm check --evidence T-1 --step 1 | tail -8": "sampo check --evidence T-1 --step 1",
            "fm quiet -- fm check | tail -3": "sampo quiet -- sampo check",
            "sampo quiet -- fm check --evidence T-1 | tail -1": "sampo quiet -- sampo check --evidence T-1",
            "cd x && fm check --evidence T-1 --step 2 | grep ✓": "sampo check --evidence T-1 --step 2",
            "FOO=1 fm check --evidence T-1 | tail -1": "FOO=1 sampo check --evidence T-1",
            "sampo check --affected --evidence T-1 | tail -2": "sampo check --affected --evidence T-1",
            "sampo check --evidence T-1 --run 'a | b' | tail -1": "sampo check --evidence T-1 --run 'a | b'",
            "sampo check --evidence T-1 --run 'echo fm check' | tail -1": "sampo check --evidence T-1 --run 'echo fm check'",
            "fm check --run 'fm next' | tail -1": "sampo check --run 'fm next'",
        }
        for cmd, want in cases.items():
            d = hooks._gate_pipe_deny({"tool_name": "Bash", "tool_input": {"command": cmd}})
            self.assertEqual(d[0], "deny", cmd)
            self.assertIn(f"`{want}`", d[1], cmd)
            self.assertIn("drop the pipe (its summary is short) or start with set -o pipefail;", d[1], cmd)

    def test_a_very_long_gate_line_is_refused_without_echoing_it(self):  # the refusal's size is bounded
        from sampo import hooks
        long_cmd = "fm check --run '" + "x" * 5000 + "' | tail -1"
        d = hooks._gate_pipe_deny({"tool_name": "Bash", "tool_input": {"command": long_cmd}})
        self.assertEqual(d[0], "deny")
        self.assertNotIn("xxxxx", d[1])
        self.assertLess(len(d[1]), 1000)


if __name__ == "__main__":
    unittest.main()
