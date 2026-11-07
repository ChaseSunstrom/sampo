"""T-2878: Smith for small local models. An action's arguments under another unambiguous name, or nested one level, are
read (mimo-v2.6:9b lost 7.8% of its turns to `write needs file as str`); a missing one is sent back as one line naming
what was sent and the shape wanted. --small (small-model mode) adds the controls for the hollow close, 36% of the local
models' exam failures: a change plan's check must fail before the work, a change request isn't planned as research or
answered, and a change plan's last step can't end with nothing changed."""
import json
import os
import tempfile
import unittest
from unittest import mock

from test_code import CLOSE, PLAN, CodeCase, call, journal_text, plan

from sampo import louhi, models, smith, smithbench

DONE = call("done", summary="fixed")


class ArgAliases(CodeCase):
    def calc(self):
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            return f.read()

    def test_an_edit_with_path_for_file_lands(self):
        self.serve("openai", plan(PLAN), call("edit", path="calc.py", old="a - b", new="a + b"), DONE)
        r = self.code("fix add", "--model", "fake")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a + b", self.calc())
        self.assertEqual(self.task()["status"], "done")

    def test_arguments_nested_one_level_are_read(self):
        self.serve("openai", plan(PLAN), call("edit", arguments={"file_path": "calc.py", "old_string": "a - b",
                                                                   "new_string": "a + b"}), DONE)
        r = self.code("fix add", "--model", "fake")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a + b", self.calc())

    def test_a_missing_argument_comes_back_as_one_line_naming_what_was_sent(self):
        f = self.serve("openai", plan(PLAN), call("write", content="x\n"),
                       call("edit", file="calc.py", old="a - b", new="a + b"), DONE)
        self.code("fix add", "--model", "fake")
        back = f.said(2)
        self.assertIn("write needs file as str (you sent: content)", back)
        self.assertIn('{\\"action\\": \\"write\\", \\"file\\"', back)  # the shape wanted, in the tool result

    def test_two_aliases_are_ambiguous_and_neither_is_guessed(self):
        f = self.serve("openai", plan(PLAN), call("edit", path="calc.py", filename="other.py", old="a - b", new="a + b"),
                       call("edit", file="calc.py", old="a - b", new="a + b"), DONE)
        self.code("fix add", "--model", "fake")
        self.assertIn("you sent: filename, new, old, path", f.said(2))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "other.py")))

    def test_two_aliases_with_one_value_are_one_argument(self):
        self.assertEqual(smith.canon_args("edit", {"path": "x.py", "file_path": "x.py", "old": "a", "new": "b"})["file"],
                         "x.py")
        self.assertNotIn("file", smith.canon_args("edit", {"path": "x.py", "file_path": "y.py", "old": "a", "new": "b"}))


class AsksChange(unittest.TestCase):
    def test_a_question_asks_for_no_change(self):
        for q in ("Why does the cache update twice?", "Explain how handle works", "what does calc.add do",
                  "Can you add a test?", "is the fix in?", "Summarize the failing tests", "list the open bugs",
                  "Describe the slow paths"):
            with self.subTest(q=q):
                self.assertIsNone(smith.asks_change(q))

    def test_an_imperative_does(self):
        for r, word in (("fix add", "fix"), ("Something is off in the paging; find and fix it.", "fix"),
                        ("make this faster", "make"), ("The tests won't run; fix it.", "fix"),
                        # T-3688: a find-and-fix request; 285 of T-3676's 339 missed fixes were answered read-only
                        ("Check the docs are right.", "check"), ("Check the test coverage.", "check"),
                        ("Review this for security problems.", "review"),
                        ("CI fails now and then; find out why.", "fails"), ("", "find and fix")):
            with self.subTest(r=r):
                self.assertEqual(smith.asks_change(r), word)


class FixPlanCheck(unittest.TestCase):
    """T-3688: for a request that asks for a change, small-model mode's plan check refuses what can't change or prove
    anything: an answer, a RESEARCH plan, a verify that can't fail (none-python's 'echo task received')."""

    @staticmethod
    def verify(cmd):
        return dict(PLAN, criteria=[{"text": "calc.add adds", "verify": cmd}])

    def test_an_answer_and_a_research_plan_are_refused(self):
        answer = {"answer": "README names page() but the code has paginate()"}
        self.assertIsNone(smith.check_plan(answer), "a question's answer stands")
        self.assertIn("read-only answer", smith.check_plan(answer, change="check"))
        self.assertIn("RESEARCH plan", smith.check_plan(dict(PLAN, type="RESEARCH"), change="check"))
        self.assertIsNone(smith.check_plan(PLAN, change="check"))

    def test_a_verify_that_cant_fail_is_refused(self):
        for cmd in ("echo 'task received'", "true", ":", "exit 0", "python3 test_calc.py || true",
                    "python3 test_calc.py; echo done", "python3 -m pytest -q | tail -5",
                    "sh -c 'python3 test_calc.py || exit 0'", "printf ok"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(smith.check_plan(self.verify(cmd)), "outside a fix request, as before")
                self.assertIn("can't fail", smith.check_plan(self.verify(cmd), change="check"))

    def test_a_verify_that_can_fail_stands(self):
        for cmd in ("python3 test_calc.py", "python3 test_calc.py && echo ok", "grep -q paginate README.md",
                    "! grep -q 'page(' README.md", "python3 -c \"import calc; print('ok')\"", "cargo test 2>&1",
                    "node --test || exit 1"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(smith.check_plan(self.verify(cmd), change="check"))


GREEN ="python3 -c 'import calc'"  # passes before any work
FIX_EDIT = call("edit", file="calc.py", old="a - b", new="a + b")
LOOK = call("done", summary="calc.add subtracts (calc.py:2): a - b should be a + b")  # T-3688: the look before the plan


class SmallMode(CodeCase):
    def run_small(self, small, *script, request="fix add"):
        f = self.serve("openai", *script)
        r = self.code(request, "--model", "fake", *(["--small"] if small else []), check=False)
        return f, r

    def test_a_perf_plan_whose_check_already_passes_goes_back_once(self):
        perf = dict(PLAN, type="PERFORMANCE", criteria=[{"text": "calc imports", "verify": GREEN}])
        for small in (True, False):
            with self.subTest(small=small):
                f, r = self.run_small(small, *[LOOK] * small, plan(perf), plan(PLAN), FIX_EDIT,
                                      call("done", summary="fixed"), CLOSE)
                sent_back = "already passes before any work" in f.said(1 + small)
                self.assertEqual(sent_back, small, r.stdout + r.stderr)
                self.tearDown()
                self.setUp()

    def test_a_change_request_planned_as_research_goes_back_to_be_typed(self):
        research = dict(PLAN, type="RESEARCH")
        f, r = self.run_small(True, LOOK, plan(research), plan(PLAN), FIX_EDIT, call("done", summary="fixed"), CLOSE)
        self.assertIn("asks for a change", f.said(2))
        self.assertIn("RESEARCH", f.said(2))
        self.assertEqual(self.task()["type"], "FIX")

    def test_a_change_request_answered_goes_back_for_a_plan(self):
        answer = {"text": json.dumps({"answer": "calc.add subtracts; change - to +"})}
        f, r = self.run_small(True, LOOK, answer, plan(PLAN), FIX_EDIT, call("done", summary="fixed"), CLOSE)
        self.assertIn("asks for a change", f.said(2))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done")

    def test_a_find_and_fix_request_answered_twice_still_gets_a_plan_and_an_edit(self):
        # T-3688: docs-rust-3d91ae's planner named the defect in an answer and nothing was edited
        answer = {"text": json.dumps({"answer": "calc.add subtracts; change - to +"})}
        f, r = self.run_small(True, LOOK, answer, answer, plan(PLAN), FIX_EDIT, DONE, CLOSE, request="Check calc.add.")
        self.assertIn("read-only answer", f.said(2))
        self.assertIn("read-only answer", f.said(3), "the second answer is refused too, not taken")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as h:
            self.assertIn("a + b", h.read())
        self.assertEqual(self.task()["status"], "done")

    def test_a_find_and_fix_request_outside_small_mode_is_still_answered(self):
        answer = {"text": json.dumps({"answer": "calc.add subtracts"})}
        f, r = self.run_small(False, answer, call("done", summary="calc.add subtracts (calc.py:2)"),
                              request="Check calc.add.")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("read-only answer", json.dumps(f.seen))

    def test_a_fix_round_quotes_the_visible_verify_and_nothing_else(self):
        # T-3688: the retry hears its own failing check's output; an exam's hidden check is never in the tree it runs in
        f, r = self.run_small(True, LOOK, plan(PLAN), call("write", file="notes.txt", content="x\n"), DONE, FIX_EDIT,
                              DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        fix = next(json.dumps(s) for s in f.seen if "fix round 1" in json.dumps(s))
        self.assertIn("python3 test_calc.py", fix)
        self.assertIn("AssertionError: -1", fix, "the failing check's own output")

    def test_a_question_is_still_answered(self):
        answer = {"text": json.dumps({"answer": "calc.add subtracts"})}
        f, r = self.run_small(True, answer, call("done", summary="calc.add subtracts (calc.py:2)"),
                              request="what does calc.add do?")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("asks for a change", json.dumps(f.seen))
        self.assertNotIn("Before any plan", json.dumps(f.seen), "a question has no look before its plan")

    def test_a_change_request_looks_before_it_plans(self):
        # T-3688: the planner sees only the file list; s31-039's blind fix plans found 5 defects of 24 where the
        # read-only answers had found 11. A read-only look reads first, and the plan hears what it found.
        f, r = self.run_small(True, call("read", file="calc.py"), LOOK, plan(PLAN), FIX_EDIT, DONE, CLOSE,
                              request="Check calc.add.")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Before any plan", f.said(0))
        self.assertEqual({t["function"]["name"] for t in f.seen[0]["tools"]}, {"read", "search", "done"},
                         "the look can't change a file")
        self.assertIn("a - b should be a + b", f.said(2), "the planner hears what the look found")
        self.assertEqual(self.task()["status"], "done")

    def test_a_look_that_never_ends_stops_at_its_own_turns_and_the_work_keeps_its_own(self):
        looks = [call("search", pattern=f"x{i}") for i in range(smith.LOOK_TURNS)]
        f = self.serve("openai", *looks, plan(PLAN), FIX_EDIT, DONE, CLOSE)
        r = self.code("fix add", "--model", "fake", "--small", "--max-steps", "3", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("What a read-only look", f.said(smith.LOOK_TURNS), "no summary: nothing to hear")
        self.assertEqual(self.task()["status"], "done")

    def test_the_last_step_of_a_change_plan_cant_end_with_nothing_changed(self):
        green = dict(PLAN, criteria=[{"text": "calc imports", "verify": GREEN}])
        close = {"text": json.dumps(dict(json.loads(CLOSE["text"]), regression="none: the check was green before"))}
        f, r = self.run_small(True, LOOK, plan(green), plan(green), call("done", summary="nothing to do"), FIX_EDIT,
                              call("done", summary="fixed"), close)  # a green-before check: the close asks why
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        refused = [json.loads(ln) for ln in journal_text(self).splitlines() if '"early_done"' in ln]
        self.assertEqual(len(refused), 1)
        self.assertIn("no file has changed", f.said(4))

    def test_the_exam_arm_passes_small_and_records_it(self):
        script = "import json,sys\nprint(json.dumps({'event':'answer','text':' '.join(sys.argv)}))\n"
        with tempfile.TemporaryDirectory() as tmp:
            base, work = os.path.join(tmp, "base"), os.path.join(tmp, "work")
            os.makedirs(os.path.join(base, "repo"))
            fm = os.path.join(tmp, "fm.py")
            with open(fm, "w") as h:
                h.write(script)
            with mock.patch.object(smithbench, "FM", fm), mock.patch.object(models, "resolve", lambda m: {"name": m}), \
                    mock.patch.object(models, "complete", lambda *a, **k: {"text": "{}", "tool_calls": [], "usage": {}}):
                ans = louhi._smith_arm(base, {"request": "x", "kind": "vuln"}, work, "fake", None,
                                       os.path.join(tmp, "e.jsonl"), small=True)
            self.assertIn("--small", ans["notes"][0])
            self.assertTrue(ans["small"])


if __name__ == "__main__":  # T-3688: run as a script, as the brief's verify does, it ran nothing before
    unittest.main()
