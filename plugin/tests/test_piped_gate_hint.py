"""T-4511: a builder's gate run is never piped. 20 of 23 'piped gate' refusals on the JARVIS box in 38 hours were builders
piping `sampo check --evidence` through tail: the lane brief shows the unpiped command with the reason in one line, and
the refusal names the exact command to run instead (the same command with the pipe removed)."""
import json
import unittest

from helpers import ForemanTestCase, read_text


class BriefShowsTheUnpipedGate(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def test_the_brief_names_the_gate_unpiped_with_the_reason_on_one_line(self):
        tid = json.loads(self.fm("task", "new", "Gate hint", "--type", "FIX", "--tier", "S", "--ac",
                                 "gate hint works :: python3 -c 'print(1)'", "--step", "build it",
                                 "--json").stdout)["id"]
        brief = read_text(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"])
        gate = f"`sampo check --affected --evidence {tid}`"
        lines = [line for line in brief.splitlines() if gate in line]
        self.assertTrue(lines, "the brief shows the gate command unpiped, in backticks")
        self.assertTrue(any("pipe" in line and "exit status" in line for line in lines),
                        "the reason sits on the same line as the command")
        self.assertNotIn(f"--evidence {tid} |", brief, "no brief line runs the gate through a pipe")


class RefusalNamesTheCommand(unittest.TestCase):  # T-4511: the exact command to run, the pipe removed
    def test_the_refusal_names_the_same_command_with_the_pipe_removed(self):
        from sampo import hooks
        cases = {
            "sampo check --evidence T-1 --step 1 2>&1 | tail -8": "sampo check --evidence T-1 --step 1",
            "fm quiet -- fm check | tail -3": "fm quiet -- fm check",
            "cd x && fm check --evidence T-1 --step 2 | grep ✓": "fm check --evidence T-1 --step 2",
            "sampo check --affected --evidence T-1 | tail -2": "sampo check --affected --evidence T-1",
            "sampo check --evidence T-1 --run 'a | b' | tail -1": "sampo check --evidence T-1 --run 'a | b'",
        }
        for cmd, want in cases.items():
            d = hooks._gate_pipe_deny({"tool_name": "Bash", "tool_input": {"command": cmd}})
            self.assertEqual(d[0], "deny", cmd)
            self.assertIn(f"`{want}`", d[1], cmd)
            self.assertIn("drop the pipe (its summary is short) or start with set -o pipefail;", d[1], cmd)


if __name__ == "__main__":
    unittest.main()
