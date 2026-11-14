"""T-4597: a Smith run whose work is done and verified never ends blocked at once on a refusal from fm task finish. (a) The
doc-drift check reads a test id written path::Class::test as the file before the first '::' (a file that exists is
proven, one that is missing still a finding). (b) Any finish refusal goes back to the worker as a fix round carrying the
refusal's own text, at most FINISH_FIX_ROUNDS rounds, then finish is tried again; a refusal that survives them blocks the
task with the refusal named and the work kept."""
import json
import os

from helpers import ForemanTestCase, read_text

from sampo import docs, smith, tidy
from test_code import CLOSE, M_PLAN, CodeCase, call, plan
from test_smith_small import DONE, FIX_EDIT

BAD_DOC = "Notes: see `calc.py/missing_note.txt` for the detail.\n"  # a path under a file: no such file
DOC_NOTES = json.dumps({"intent": "calc.add adds, as asked", "adversary": "negative numbers add too",
                        "docs": "README.md: a note", "lesson": "none: a one-character bug",
                        "why_not_caught": "none: test_calc.py is the test that catches it"})
CLOSE_DOCS = {"text": DOC_NOTES}
WRITE_BAD = call("write", file="README.md", content="# app\n\n" + BAD_DOC)
WRITE_GOOD = call("write", file="README.md", content="# app\n\nNotes: the detail is in calc.py.\n")


class TestIdPath(ForemanTestCase):
    """(a) tests/x.py::Class::test is the file tests/x.py."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.repo, "tests"), exist_ok=True)
        with open(os.path.join(self.repo, "tests", "test_backoff.py"), "w") as f:
            f.write("class BusyButQuiet:\n    pass\n")
        with open(os.path.join(self.repo, "FINDINGS.md"), "w") as f:
            f.write("Proven by `tests/test_backoff.py::BusyButQuiet::test_waits` and `tests/test_gone.py::A::test_b`.\n")

    def test_a_test_id_whose_file_exists_is_no_drift_and_one_whose_file_is_missing_still_is(self):
        found = [f["detail"] for f in docs.scan(self.repo) if f["file"] == "FINDINGS.md"]
        self.assertEqual(found, ["tests/test_gone.py::A::test_b"])

    def test_the_claims_read_it_the_same_way(self):
        got, _ = docs.claims(self.repo)
        verdicts = {c["detail"]: c["verdict"] for c in got if c["kind"] == "path" and c["file"] == "FINDINGS.md"}
        self.assertEqual(verdicts, {"tests/test_backoff.py::BusyButQuiet::test_waits": "proven",
                                    "tests/test_gone.py::A::test_b": "failed"})

    def test_a_doc_a_task_updated_is_not_blocked_by_an_existing_files_test_id(self):
        blockers, _ = docs.task_docs(self.repo, "FINDINGS.md: the proof")
        self.assertEqual(len(blockers), 1, blockers)
        self.assertIn("tests/test_gone.py::A::test_b", blockers[0])
        self.assertNotIn("test_backoff", blockers[0])

    def test_the_path_part_is_the_text_before_the_first_double_colon(self):
        self.assertEqual(tidy.path_part("tests/x.py::Class::test"), "tests/x.py")
        self.assertEqual(tidy.path_part("tests/x.py"), "tests/x.py")
        self.assertEqual(tidy.path_part("::odd/path"), "::odd/path", "nothing before it: the token stays whole")


class FinishRefusal(CodeCase):
    """(b) a refusal nothing else answers (here: a doc this task wrote has a dead path) is the worker's to mend."""

    def go(self, *script):
        f = self.serve("anthropic", *script)
        return f, self.code("fix add", "--model", "fake", check=False)

    def asked(self, f):
        return [i for i, s in enumerate(f.seen) if "fm task finish refused to close" in str(s["messages"][-1])]

    def blocked_events(self, r):
        return r.stdout + r.stderr

    def test_the_refusal_text_goes_to_the_worker_and_the_next_finish_closes(self):
        f, r = self.go(plan(M_PLAN), FIX_EDIT, WRITE_BAD, DONE, CLOSE_DOCS, WRITE_GOOD, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task("T-0001")["status"], "done", r.stdout + r.stderr)
        rounds = self.asked(f)
        self.assertEqual(len(rounds), 1, "one round was enough")
        said = f.said(rounds[0])
        self.assertIn("calc.py/missing_note.txt", said, "the refusal's own text is the round's prompt")
        self.assertIn("doc drift in a doc this task updated", said)
        self.assertIn("return a + b", self.calc())
        self.assertIn("the detail is in calc.py", read_text(os.path.join(self.repo, "README.md")))
        self.assertIn("README.md", self.last_commit(), "the mended doc went in the close's commit")

    def test_a_refusal_that_survives_two_rounds_blocks_with_the_refusal_named_and_the_work_kept(self):
        f, r = self.go(plan(M_PLAN), FIX_EDIT, WRITE_BAD, DONE, CLOSE_DOCS, DONE, DONE)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.asked(f)), smith.FINISH_FIX_ROUNDS, "the rounds are bounded")
        self.assertEqual(smith.FINISH_FIX_ROUNDS, 2)
        self.assertEqual(self.task("T-0001")["status"], "blocked", r.stdout + r.stderr)
        out = self.blocked_events(r)
        self.assertIn("fm task finish refused", out)
        self.assertIn("doc drift in a doc this task updated", out, "the refusal is named")
        self.assertIn("return a + b", self.calc(), "the work is kept")

    def test_a_second_round_gets_the_refusal_again_and_a_later_finish_can_close(self):
        f, r = self.go(plan(M_PLAN), FIX_EDIT, WRITE_BAD, DONE, CLOSE_DOCS, DONE, WRITE_GOOD, DONE)
        self.assertEqual(self.task("T-0001")["status"], "done", r.stdout + r.stderr)
        rounds = self.asked(f)
        self.assertEqual(len(rounds), 2)
        self.assertIn("calc.py/missing_note.txt", f.said(rounds[1]))

    def test_the_close_notes_are_not_asked_again_for_each_finish(self):
        f, r = self.go(plan(M_PLAN), FIX_EDIT, WRITE_BAD, DONE, CLOSE_DOCS, WRITE_GOOD, DONE)
        self.assertEqual(self.task("T-0001")["status"], "done", r.stdout + r.stderr)
        self.assertEqual(len(f.seen), 7, "the close notes were asked once: seven calls in all")
