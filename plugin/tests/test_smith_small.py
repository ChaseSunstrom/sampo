"""T-2878: Smith for small local models. An action's arguments under another unambiguous name, or nested one level, are
read (mimo-v2.6:9b lost 7.8% of its turns to `write needs file as str`); a missing one is sent back as one line naming
what was sent and the shape wanted. --small (small-model mode) adds the controls for the hollow close, 36% of the local
models' exam failures: a change plan's check must fail before the work, a change request isn't planned as research or
answered, and a change plan's last step can't end with nothing changed."""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from test_code import CLOSE, M_PLAN, PLAN, CodeCase, call, journal_text, plan

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
        recs = [json.loads(ln) for ln in journal_text(self).splitlines()]
        self.assertIs(recs[0]["options"]["small"], True)  # T-3974: a resume stays small
        self.assertEqual(sum(r["kind"] == "edited" for r in recs), 1)  # once, at the first edit that landed

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


M_ONE = dict(M_PLAN, readings=M_PLAN["readings"][:1])  # sound work, one reading and no ruled_out: only the M form fails
RULED = "callers want a difference, so add would be renamed: ruled out, the tests call add for a sum"
LADDER_M_ONE = ("FIX", "M", "Make calc.add add", "calc.add adds two numbers :: python3 test_calc.py", "1. edit calc.py",
                "- the request means calc.add returns a sum",
                "build | change the operator in calc.add | a caller relies on it\n"
                "exists | call the builtin sum over both numbers | inputs aren't numbers\n"
                "less | document that add subtracts | callers keep wrong totals", "1", "85",
                "the failing test names the operator")


class PlanForm(CodeCase):
    """T-4282: a plan that is sound work in an imperfect form never ends the run (exam s8-041 goal-rust-ea4f63: 50,288
    tokens and nothing built for 'Interpretation names 1 distinct reading'; s8-042 goal-ruby-f44b59 and s8-043
    goal-c-e61e7e: 'fm focus refused the plan' for one named missing piece)."""

    def go(self, *script, request="fix add", kind="openai"):
        f = self.serve(kind, *script)
        return f, self.code(request, "--model", "fake", check=False)

    def test_m_form_one_reading_gets_one_targeted_question_and_the_plan_stays_m(self):
        f, r = self.go(*[plan(M_ONE)] * 3, {"text": RULED}, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("one reading needs ruled_out", f.said(3))  # the gap is the question
        self.assertIn(M_ONE["readings"][0], f.said(3))
        self.assertNotIn("the task's type", f.said(3))  # no field ladder for a plan that parsed
        self.assertEqual((self.task()["tier"], self.task()["status"]), ("M", "done"), r.stdout + r.stderr)
        self.assertIn("completed by one targeted question", r.stdout)

    def test_m_form_after_the_field_ladder_gets_the_same_one_question(self):
        f, r = self.go(*[{"text": "nope"}] * 3, *[{"text": a} for a in LADDER_M_ONE], {"text": RULED}, FIX_EDIT, DONE,
                       CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("one reading needs ruled_out", f.said(3 + len(LADDER_M_ONE)))
        self.assertEqual((self.task()["tier"], self.task()["status"]), ("M", "done"), r.stdout + r.stderr)

    def test_m_form_still_unmet_files_the_plan_as_s_with_the_gap_and_the_run_goes_on(self):
        f, r = self.go(*[plan(M_ONE)] * 3, {"text": "nope"}, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.task()["tier"], self.task()["status"]), ("S", "done"), r.stdout + r.stderr)
        self.assertIn("plan filed as S: its M form was not met: Interpretation names 1 distinct reading", r.stdout)
        self.assertIn("return a + b", self.calc())
        down = [e for e in map(json.loads, journal_events(self)) if e.get("downgrade")]
        self.assertEqual(len(down), 1, down)

    def test_m_form_question_that_fails_at_the_model_still_files_the_plan_as_s(self):
        # past its script the fake answers 400: a dead targeted question is not a dead run
        f = self.serve("openai", *[plan(M_ONE)] * 3)
        r = self.code("fix add", "--model", "fake", "--dry-plan", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("plan filed as S", r.stdout)
        self.assertEqual(len(f.seen), 4)

    def test_m_form_rescue_leaves_a_plan_that_fails_an_s_rule_to_end_as_before(self):
        no_verify = dict(M_ONE, criteria=[{"text": "calc.add adds", "verify": ""}])
        f, r = self.go(*[plan(no_verify)] * 3, *[{"text": "nope"}] * 5)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no valid plan after 2 repairs, nor field by field", r.stderr + r.stdout)
        self.assertNotIn("plan filed as S", r.stdout)
        self.assertEqual(len(f.seen), 8)  # three JSON tries and five field questions, no targeted one

    def test_focus_refusal_twice_for_a_clause_gets_one_targeted_question_and_the_task_starts(self):
        f, r = self.go(plan(M_PLAN), plan(M_PLAN), {"text": "the test file is unchanged :: python3 test_calc.py"},
                       FIX_EDIT, DONE, CLOSE, request="fix add, and keep the tests as they are", kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a criterion for", f.said(2))
        self.assertIn("keep the tests as they are", f.said(2))
        self.assertEqual([self.task(t)["status"] for t in ("T-0001", "T-0002", "T-0003")],
                         ["dropped", "dropped", "done"], r.stdout + r.stderr)
        self.assertIn("the test file is unchanged", json.dumps(self.task("T-0003")))

    def test_focus_refusal_whose_targeted_reply_fills_nothing_ends_with_the_refusal(self):
        f, r = self.go(plan(M_PLAN), plan(M_PLAN), {"text": "nope"},
                       request="fix add, and keep the tests as they are", kind="anthropic")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused the plan", r.stderr)
        self.assertEqual(len(f.seen), 3)
        self.assertEqual(self.task("T-0002")["status"], "planned")  # it stays to be looked at

    def test_focus_refusal_for_a_one_way_step_asks_for_that_step_again(self):
        # the repo itself is a door no snapshot covers, so focus can't write the Undo line on its own (doors.gate)
        hard = dict(M_PLAN, steps=["delete ./", "fix calc.add"])
        f, r = self.go(plan(hard), plan(hard), {"text": "read calc.py and leave every file where it is"},
                       FIX_EDIT, DONE, DONE, CLOSE, kind="anthropic")
        self.assertIn("Undo: line on step 1", f.said(2), r.stdout + r.stderr)
        self.assertIn("delete ./", f.said(2))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task("T-0003")["status"], "done", r.stdout + r.stderr)

    # review of T-4282 ------------------------------------------------------------------------------------------------
    def test_focus_refusal_for_a_one_way_step_quotes_the_gate_in_the_question_and_the_new_step_keeps_the_limit(self):
        hard = dict(M_PLAN, steps=["delete ./", "fix calc.add"])
        f, r = self.go(plan(hard), plan(hard), {"text": "read calc.py and leave every file where it is"},
                       FIX_EDIT, DONE, DONE, CLOSE, kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        asked = json.dumps(f.seen[2]["messages"][-1])  # the targeted question itself, not the refusal before it
        self.assertIn("an Undo: line on step 1 (one-way, untracked data", asked)
        self.assertIn("delete ./", asked)
        self.assertIn("no Undo line can be written", asked, "the model is not led to invent a snapshot")
        brief = json.dumps(self.task("T-0003"))
        self.assertIn("read calc.py and leave every file where it is", brief)
        self.assertIn(smith.STEP_LIMIT, brief, "the limit the gate asked for stays in the step the worker reads")
        self.assertIn("step 1 reworded for the plan gate", r.stdout)

    def downgrades(self, tid="T-0001"):
        return [e for e in map(json.loads, journal_events(self, tid)) if e.get("downgrade")]

    def test_m_form_downgrade_of_a_plan_the_replan_replaced_is_not_reported(self):
        # the first plan is cut to S in the planner's hands; its verify already passes, so it is planned again, whole:
        # the plan that stands was never downgraded, and no note, event or friction line says it was
        passes = dict(M_ONE, criteria=[{"text": "calc.add adds two numbers", "verify": "true"}])
        f, r = self.go(*[plan(passes)] * 3, {"text": "nope"}, plan(M_PLAN), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.task()["tier"], self.task()["status"]), ("M", "done"), r.stdout + r.stderr)
        self.assertNotIn("plan filed as S", r.stdout)
        self.assertEqual(self.downgrades(), [])

    def test_m_form_downgrade_twice_over_a_replan_is_reported_once_for_the_plan_that_stands(self):
        passes = dict(M_ONE, criteria=[{"text": "calc.add adds two numbers", "verify": "true"}])
        f, r = self.go(*[plan(passes)] * 3, {"text": "nope"}, *[plan(M_ONE)] * 3, {"text": "nope"}, FIX_EDIT, DONE,
                       CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.task()["tier"], self.task()["status"]), ("S", "done"), r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("plan filed as S"), 1, r.stdout)
        self.assertEqual(len(self.downgrades()), 1)


NAMES_ITEM = dict(M_PLAN, title="Make parse_line() handle tabs", steps=["handle tabs in parse_line()"],
                  criteria=[{"text": "parse_line() handles tabs", "verify": "python3 test_calc.py"}])


class PlanFormRecord(CodeCase):
    """Review of T-4282: through the second ask of T-4294 (items on record the plan doesn't name) the downgrade follows
    the plan that stands."""
    repo_seed = ("seed record", CodeCase.repo_seed[1] + (("PLAN.md", "# Plan\n\n- [ ] Make parse_line() handle tabs\n"),))

    def dry(self, *script):
        self.serve("openai", *script)
        return self.code("continue", "--dry-plan", "--model", "fake", check=False)

    def test_m_form_downgrade_of_a_plan_the_second_ask_replaced_is_not_reported(self):
        r = self.dry(*[plan(M_ONE)] * 3, {"text": "nope"}, plan(NAMES_ITEM))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("FIX M Make parse_line() handle tabs", r.stdout)
        self.assertNotIn("plan filed as S", r.stdout)

    def test_m_form_downgrade_is_still_reported_when_the_second_ask_gives_no_plan(self):
        r = self.dry(*[plan(M_ONE)] * 3, {"text": "nope"}, *[{"text": "nope"}] * 12)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("kept the first plan", r.stdout)
        self.assertEqual(r.stdout.count("plan filed as S"), 1, r.stdout)


def journal_events(case, tid="T-0001"):
    """The run's kept events, one JSON line each (code/<task>.events.jsonl beside the project's tasks)."""
    path = os.path.join(os.path.dirname(os.path.dirname(case.task(tid)["path"])), "code", f"{tid}.events.jsonl")
    with open(path, encoding="utf-8") as fh:
        return [x for x in fh.read().splitlines() if x.strip()]


class Resume(unittest.TestCase):  # T-3974: a resumed --small run kept neither small nor an edit made before the crash
    def load(self, *recs):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "T-0001.jsonl"), "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(r) + "\n" for r in recs))
            run = smith.Run(types.SimpleNamespace(escalate=None, max_steps=None), tmp, tmp)
            smith._load(run, "T-0001")
            return run

    def test_a_resumed_small_run_is_small_and_an_edit_before_the_crash_counts(self):
        start = {"kind": "start", "task": "T-0001", "plan": {}, "request": "fix add", "options": {"small": True}}
        run = self.load(start, {"kind": "edited"})
        self.assertEqual((run.small, run.edited), (True, True))
        run = self.load(dict(start, options={}))  # an older journal: neither
        self.assertEqual((run.small, run.edited), (False, False))


if __name__ == "__main__":  # T-3688: run as a script, as the brief's verify does, it ran nothing before
    unittest.main()
