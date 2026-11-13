"""T-4566: the survey's SPEC field is read wherever it sits in the summary. Measured 2026-11-13 (exam run cascade10l):
2 of 2 summaries that carried a SPEC field had it inline at the end of one line ('... TESTS: pytest (...). SPEC:
docs/warehouses.md (the design note ...)'); spec_docs read only lines that began with SPEC and needed the rest to be a
path, so run.spec was empty, no 'specification:' note was emitted, and the plan was built from the lossy summary. The
fields are the labels the survey prompt asks for (DOCS, CODE, TESTS, SPEC): a field runs to the next label or the end."""
import json
import unittest

from test_code import CLOSE, PLAN, plan
from test_smith_survey import FIX_EDIT, DONE, ONLY_IN_RULES, READ_ALL, SUMMARY, SurveyCase, call, wants

from sampo import smith

MAP = ["README.md", "docs/rules.md", "docs/notes.md"]
OPENED = {"README.md", "docs/rules.md", "docs/notes.md"}
# the real shape: one line, the SPEC field last, the path followed by a parenthesis
ONE_LINE = ("DOCS: docs/rules.md says calc.add(a, b) returns the sum. CODE: calc.py add subtracts. TESTS: pytest "
            "(project uses pytest per README Development; no dedicated test command file observed). SPEC: "
            "docs/rules.md (the design note specifying add, its commands, and migration 004).")


class SpecField(SurveyCase):
    def named(self, summary, opened=OPENED):
        return smith.spec_docs(self.run_object(opened=set(opened)), summary, MAP)

    def test_inline_at_the_end_of_a_one_line_summary(self):
        self.assertEqual(self.named(ONE_LINE), ["docs/rules.md"])

    def test_path_then_a_parenthesis_or_prose_still_counts(self):
        self.assertEqual(self.named("CODE: calc.py. SPEC: docs/notes.md (the note)"), ["docs/notes.md"])
        self.assertEqual(self.named("SPEC: docs/notes.md is the design note for this change"), ["docs/notes.md"])
        self.assertEqual(self.named("SPEC: docs/notes.md."), ["docs/notes.md"], "a sentence's full stop")

    def test_several_named_in_one_field_come_back_in_the_maps_order(self):
        got = self.named("TESTS: pytest. SPEC: docs/notes.md (first), `docs/rules.md` and ./README.md; CODE: calc.py")
        self.assertEqual(got, ["README.md", "docs/rules.md", "docs/notes.md"])

    def test_the_field_ends_at_the_next_label(self):
        self.assertEqual(self.named("SPEC: docs/notes.md. CODE: docs/rules.md is wrong. TESTS: none"), ["docs/notes.md"])
        self.assertEqual(self.named("SPEC: docs/notes.md\nCODE: docs/rules.md"), ["docs/notes.md"])

    def test_a_path_only_under_docs_or_code_names_nothing(self):
        self.assertEqual(self.named("DOCS: docs/rules.md says add. CODE: docs/notes.md. TESTS: pytest. SPEC: none"), [])
        self.assertEqual(self.named("DOCS: docs/rules.md says add. CODE: calc.py. TESTS: pytest"), [])
        self.assertEqual(self.named("the docs/rules.md file is the spec"), [], "no field, no name")

    def test_one_per_line_and_a_continuation_line_still_work(self):
        self.assertEqual(self.named("SPEC: docs/notes.md\nSPEC: docs/rules.md"), ["docs/rules.md", "docs/notes.md"])
        self.assertEqual(self.named("SPEC: docs/notes.md,\ndocs/rules.md\n\nCODE: calc.py"), ["docs/rules.md", "docs/notes.md"])
        self.assertEqual(self.named("spec: docs/notes.md"), ["docs/notes.md"], "a line's label in any case, as before")

    def test_only_a_listed_opened_document_whole_path_is_named(self):
        self.assertEqual(self.named(ONE_LINE, opened={"README.md"}), [], "docs/rules.md was never opened")
        self.assertEqual(self.named("SPEC: old/docs/rules.md and docs/rules.md.bak and docs/rules.mdx"), [],
                         "a longer path is another path")
        self.assertEqual(self.named("SPEC: docs/zzz.md"), [], "a path off the map is a guess")

    def test_bold_labels(self):
        self.assertEqual(self.named("**DOCS:** x. **SPEC:** docs/notes.md **CODE:** docs/rules.md"), ["docs/notes.md"])


PROSE = "pytest (project uses pytest per README Development; no dedicated test command file observed)"
WAREHOUSES = ("DOCS: README says stock is per warehouse. CODE: inventory/cli.py has no warehouses command. TESTS: "
              + PROSE + ". SPEC: docs/warehouses.md (the design note specifying stock-per-warehouse).")


class TestsField(SurveyCase):
    """T-4566 follow-up: the TESTS field is read through survey_fields too; what is done with its text is unchanged."""

    def test_an_inline_field_in_a_one_line_summary_is_taken(self):
        self.assertEqual(smith.tests_line(WAREHOUSES), PROSE + ".", "the field's text as it stands, a full stop and all")
        self.assertEqual(smith.tests_line("DOCS: x. TESTS: python3 -m unittest discover -s tests -q SPEC: none"),
                         "python3 -m unittest discover -s tests -q")

    def test_a_line_field_is_read_as_before(self):
        self.assertEqual(smith.tests_line("DOCS: x\nTESTS: `python3 test_ok.py`\nSPEC: none"), "python3 test_ok.py")
        self.assertEqual(smith.tests_line("tests: 'make test'\ncode: x"), "make test", "a line's label in any case")
        self.assertEqual(smith.tests_line("TESTS: a\nTESTS: b"), "a", "the first one")
        self.assertEqual(smith.tests_line("TESTS: make test\nsome prose after"), "make test", "its first line only")

    def test_none_unknown_and_absent_are_no_command(self):
        for text in ("TESTS: none", "TESTS: none. SPEC: docs/a.md", "TESTS: N/A", "TESTS: unknown", "TESTS:", "", None,
                     "DOCS: pytest", "TESTS pytest"):
            self.assertIsNone(smith.tests_line(text), repr(text))
        self.assertEqual(len(smith.tests_line("TESTS: " + "x" * 900)), 300, "bounded")

    def test_prose_after_a_command_is_never_a_keep_check(self):
        """A value like 'pytest (project uses ...)' is not valid shell: it is red before the work, so the suite prove
        finds is tried after it and the keep check is never the prose."""
        run = self.run_object(survey_tests=smith.tests_line(WAREHOUSES))
        smith._keep_begin(run, {})
        self.assertNotEqual(run.keep_cmd, PROSE)
        self.assertIn(PROSE, run.keep_why, "the stats say the survey's line was tried and why it is no check")
        self.assertIn("the suite prove finds", run.keep_why, "and the suite prove finds was tried after it")


class InlineEndToEnd(SurveyCase):
    def test_the_note_is_emitted_and_the_planner_is_handed_the_document(self):
        f, r, ev = self.go(wants(True), *READ_ALL, call("done", summary=ONE_LINE), plan(PLAN), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(ONLY_IN_RULES, f.said(self.planner(f)), "the whole document, not the summary's line about it")
        note = self.notes(ev, "spec")
        self.assertEqual([e["spec"] for e in note], [["docs/rules.md"]])
        self.assertIn("specification:", note[0]["text"] if "text" in note[0] else json.dumps(note[0]))

    def test_the_wrap_path_asks_for_the_spec_field_too(self):
        self.assertIn("SPEC", smith.WRAP_PROMPT, "the survey that ran out of turns is asked for every field")
        self.assertIn("DOCS", smith.WRAP_PROMPT)


if __name__ == "__main__":
    unittest.main()
