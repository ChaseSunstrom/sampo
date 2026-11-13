"""T-4514: a builder's own runs are never piped. 20 of 23 'piped gate' refusals on the JARVIS box in 38 hours were
builders piping a check through tail: the lane brief shows the builder's own command unpiped with the reason in one
line. (T-4511's refusal text, which named the command to run instead, was reverted after the merge and is not tested
here.)"""
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


if __name__ == "__main__":
    unittest.main()
