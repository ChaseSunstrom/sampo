"""T-5598: a subagent's final report is capped at 1500 characters, and the rest of its work goes to a file the report
names. The reports averaged 3.8k characters (365 a day, ~0.9M input tokens a day re-read by the parent); the cap is
the agent files' report contract, because a SubagentStop hook is async and can't change the text the parent sees."""
import glob
import os
import re
import unittest

from helpers import PLUGIN, read_text
from sampo import lanes as slanes

AGENTS = os.path.join(PLUGIN, "agents")
REPORT_LINE = re.compile(r"(?m)^(?:Report in|Output|Return)\b[^\n]*1500 characters")
WRITES_THE_REST = ("builder", "reproducer")  # the two agents that can write .fm-scratch/ and name it


def _body(path):
    return read_text(path).split("---", 2)[2]


def _report(body):
    """The report paragraph: from the line that caps it to the next blank line."""
    m = REPORT_LINE.search(body)
    if not m:
        return ""
    return body[m.start():].split("\n\n", 1)[0]


class ReportCap(unittest.TestCase):
    def test_every_agent_caps_its_report(self):
        paths = sorted(glob.glob(os.path.join(AGENTS, "fm-*.md")))
        self.assertGreaterEqual(len(paths), 6, paths)
        for path in paths:
            with self.subTest(agent=os.path.basename(path)):
                body = _body(path)
                self.assertRegex(body, REPORT_LINE, "the report line must cap the report at 1500 characters")
                self.assertNotIn("400 words", body, "the old word cap is replaced, not kept beside the new one")

    def test_builder_and_reproducer_return_at_most_six_plain_lines_and_write_no_report_file(self):
        # T-5963: Claude Code refuses a subagent's Write to report.md ("return findings as text"); no rename gets past
        # it. The message is at most 6 lines, evidence lives in state, close lines are trailers on the tip commit.
        for name in WRITES_THE_REST:
            with self.subTest(agent=name):
                body = _body(os.path.join(AGENTS, f"fm-{name}.md"))
                report = _report(body)
                self.assertRegex(report, r"(?:≤|at most) 6 lines", "the message is capped at 6 lines")
                self.assertNotIn(".fm-scratch/report", body, "no report file: the harness refuses the write")
        builder = _body(os.path.join(AGENTS, "fm-builder.md"))
        self.assertIn("trailer", builder.split("\n\nReport in")[0], "the close lines go on the tip commit")
        self.assertIn("sampo task evidence", builder, "the evidence lives in state")

    def test_the_builder_report_paragraph_still_splits_the_contract(self):
        # lanes.contract and the contract tests cut the rules at "\n\nReport in": the cap line must keep that opening
        body = _body(os.path.join(AGENTS, "fm-builder.md"))
        self.assertEqual(body.count("\n\nReport in"), 1)
        self.assertTrue(_report(body).startswith("Report in"))

    def test_the_reviewer_marks_every_lens_it_cut_short(self):
        # the cap may drop finding lines: the finding lines are never cut, and a lens that leaves any out says so
        body = _body(os.path.join(AGENTS, "fm-reviewer.md"))
        self.assertIn("+N more omitted", body)
        self.assertIn("finding lines are never cut", body)


class ReviewerOmitted(unittest.TestCase):
    PROSE = "Context for this lens, said once. " * 40  # a reviewer's prose that fills the cap

    def test_a_report_cut_at_the_cap_with_an_omitted_high_is_not_clean(self):
        # the HIGH was cut at the cap and the lens says so: it must not be recorded as "approve, 0 findings"
        msg = ("**Verdict** — changes needed.\n\n## lens: intent\n- LOW — `f.py:1` — a name — rename — low\n\n"
               f"## lens: adversary\n{self.PROSE}\n+1 more omitted\n")
        self.assertLessEqual(len(self.PROSE), 1500)
        self.assertEqual(slanes.reviewer_findings(msg), [("intent", "LOW", "f.py:1", "a name")])
        self.assertEqual(slanes.reviewer_lenses(msg), {"intent": 1}, "adversary lost a HIGH: left out, not clean")

    def test_every_spelling_of_the_omitted_line_marks_its_lens(self):
        for line in ("- +1 more omitted", "**+2 more omitted**", "+3 more omitted — the rest are LOW"):
            with self.subTest(line=line):
                msg = f"**Verdict** — approve.\n\n## lens: adversary\n{self.PROSE}\n{line}\n\n## lens: intent\nNo findings.\n"
                self.assertEqual(slanes.reviewer_lenses(msg), {"intent": 0})

    def test_a_lens_with_nothing_omitted_is_still_recorded(self):
        msg = f"**Verdict** — approve.\n\n## lens: adversary\n{self.PROSE}\n\n## lens: intent\nNo findings.\n"
        self.assertEqual(slanes.reviewer_lenses(msg), {"adversary": 0, "intent": 0})
