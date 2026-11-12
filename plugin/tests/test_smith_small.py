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

import test_code
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
        f, r = self.run_small(False, answer, wants(False, "it asks for a check, findings only"),  # T-4284: the model judges
                              call("done", summary="calc.add subtracts (calc.py:2)"), request="Check calc.add.")
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
        f, r = self.run_small(True, answer, wants(False, "it asks how the code works"),  # T-4284
                              call("done", summary="calc.add subtracts (calc.py:2)"), request="what does calc.add do?")
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
        self.assertEqual({t["function"]["name"] for t in f.seen[0]["tools"]}, {"read", "search", "done", "lib"},
                         "the look can't change a file (the lib tool, T-4443, only reads a stack card)")
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


def wants(yes, why="the request states an outcome"):
    return {"text": json.dumps({"wants_change": yes, "why": why})}


ANSWERED = {"text": json.dumps({"answer": "calc.add subtracts; the fix is to change - to +"})}
RELEASE = "Get these helpers to a finished state I could release; decide what that takes."
RESEARCH_S = {"type": "RESEARCH", "tier": "S", "title": "Read the helpers to determine finished state",
              "criteria": [{"text": "the state of the helpers is known", "verify": "test -f calc.py"}],
              "steps": ["cat app.rs", "cat test_app.rs", "cat README.md"]}


class WantsChange(CodeCase):
    """T-4284: whether a request wants files changed is one cached model call's judgment, made lazily after an answer or
    a RESEARCH plan (the word lists only stand in when the call gives nothing). T-2878's guard needs no --small."""

    def go(self, request, *script, args=()):
        f = self.serve("openai", *script)
        r = self.code(request, "--model", "fake", "--json", *args, check=False)
        return f, r, [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]

    def judged(self, f):
        return [i for i, s in enumerate(f.seen) if "wants_change" in json.dumps(s["messages"][0])]

    def test_wants_change_an_answer_to_an_outcome_goes_back_for_a_plan_and_the_work_is_done(self):
        for request in ("The code needs these fixes.", "Gap analysis of calc.py: the three real bugs"):
            with self.subTest(request=request):
                f, r, ev = self.go(request, ANSWERED, wants(True), plan(PLAN), FIX_EDIT, DONE, CLOSE)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.judged(f), [1])
                self.assertIn("names fixes the user wants made", f.said(2))
                self.assertIn("change - to +", f.said(2), "the answer, fenced, seeds the plan")
                self.assertIn("<data-", f.said(2))
                self.assertIn("a + b", self.calc())
                self.assertEqual(self.task()["status"], "done")
                self.tearDown()
                self.setUp()

    def test_wants_change_a_second_answer_is_refused(self):
        f, r, ev = self.go("The code needs these fixes.", ANSWERED, wants(True), ANSWERED, plan(PLAN), FIX_EDIT, DONE,
                           CLOSE)
        self.assertIn("read-only answer", f.said(3))
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)

    def test_wants_change_a_question_the_judgment_says_wants_none_stays_an_answer(self):
        f, r, ev = self.go("what does calc.add do?", ANSWERED, wants(False, "it asks what the code does"),
                           call("done", summary="calc.add returns a - b (calc.py line 2)"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([e["event"] for e in ev if e["event"] == "answer"], ["answer"])
        self.assertIn("return a - b", self.calc())
        self.assertNotIn("task", [e["event"] for e in ev])
        self.assertEqual(len(f.seen), 3, "the answer, the judgment and the answering phase: no re-plan")

    def test_wants_change_ask_is_unchanged_and_asks_nothing(self):
        f, r, ev = self.go("The code needs these fixes.", ANSWERED, call("done", summary="it subtracts (calc.py)"),
                           args=("--ask",))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.judged(f), [])
        self.assertIn("return a - b", self.calc())

    def test_wants_change_a_first_plan_that_changes_something_asks_nothing(self):
        f, r, ev = self.go("The code needs these fixes.", plan(PLAN), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.judged(f), [], "no answer, no RESEARCH plan: no call")
        self.assertEqual(len(f.seen), 3, "the plan, the edit, the done: the fake model's own calls and no extra one")

    def test_judgment_is_one_call_with_thinking_off_and_the_request_fenced(self):
        f, r, ev = self.go(RELEASE, plan(RESEARCH_S), wants(True), plan(RESEARCH_S), plan(PLAN), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(self.judged(f), [1], "refused again by the check, never judged again")
        asked = f.seen[1]
        self.assertIn("<data-", json.dumps(asked["messages"]))
        self.assertIn(RELEASE, json.dumps(asked["messages"][-1]))
        self.assertTrue(0 < asked.get("max_tokens", 0) <= 256, asked.keys())

    def test_judgment_verdict_and_reason_are_an_event_of_the_run(self):
        f, r, ev = self.go("The code needs these fixes.", ANSWERED, wants(True, "it lists fixes to make"), plan(PLAN),
                           FIX_EDIT, DONE, CLOSE)
        got = [e for e in ev if "wants_change" in e]
        self.assertEqual(len(got), 1, r.stdout)
        self.assertIs(got[0]["wants_change"], True)
        self.assertEqual(got[0]["why"], "it lists fixes to make")
        self.assertEqual(got[0]["event"], "note")

    def test_judgment_that_fails_falls_back_to_the_word_lists_and_never_fails_the_run(self):
        bad = {"text": "maybe, hard to say"}, {"text": ""}, {"text": json.dumps({"wants_change": "yes"})}, \
            {"text": json.dumps({"why": "no verdict"})}
        for reply in bad:
            with self.subTest(reply=reply["text"]):
                f, r, ev = self.go("Fix calc.add", ANSWERED, reply, plan(PLAN), FIX_EDIT, DONE, CLOSE)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.task()["status"], "done", "asks_change says a change: back for a plan")
                got = [e for e in ev if "wants_change" in e]
                self.assertEqual([e["wants_change"] for e in got], [True])
                self.assertIn("fallback", got[0]["why"])
                self.tearDown()
                self.setUp()

    def test_judgment_that_fails_for_a_question_leaves_the_answer(self):
        f, r, ev = self.go("what does calc.add do?", ANSWERED, {"text": "no idea"},
                           call("done", summary="calc.add returns a - b (calc.py line 2)"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len([e for e in ev if e["event"] == "answer"]), 1)
        self.assertIn("return a - b", self.calc())

    def test_research_plan_for_an_outcome_goes_back_once_for_a_plan_that_changes_files(self):
        f, r, ev = self.go(RELEASE, plan(RESEARCH_S), wants(True), plan(RESEARCH_S), plan(PLAN), FIX_EDIT, DONE, CLOSE)
        seed = f.said(2)
        self.assertIn("you planned to look and report; the user wants the work done: plan the look as step 1 and the "
                      "changes after it", seed)
        for text in ("Read the helpers to determine finished state", "cat app.rs", "cat README.md"):
            self.assertIn(text, seed, "the RESEARCH plan's own title and steps seed the second ask")
        self.assertIn("<data-", seed)
        self.assertIn("RESEARCH plan", f.said(3), "check_plan(v, change) refuses a second RESEARCH plan")
        self.assertEqual(self.task()["type"], "FIX")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)

    def test_research_plan_for_a_request_that_wants_only_findings_keeps_it(self):
        f, r, ev = self.go(RELEASE, plan(RESEARCH_S), wants(False, "it asks what is left to do"), call("run", cmd="true"),
                           DONE, CLOSE)
        self.assertEqual(self.judged(f), [1])
        self.assertNotIn("you planned to look", json.dumps(f.seen))
        self.assertEqual([e["plan"]["type"] for e in ev if e["event"] == "plan"], ["RESEARCH"])
        self.assertIn("return a - b", self.calc())

    def test_research_plan_under_ask_is_left_alone(self):
        f, r, ev = self.go(RELEASE, plan(RESEARCH_S), args=("--ask",))
        self.assertEqual(self.judged(f), [])

    # T-4284 round 2: exam s8-048 goal-c-1764a8. The judgment said yes, the second ask gave a sound CLEAN M plan after
    # the ladder, and check_plan(v, change) threw it away: its first verify ended `&& exit 1 || exit 0`, which can fail
    # (exit 1 ends the shell) but matched _CANT_FAIL's `|| exit 0`. A bare except then said only "kept the first".

    def test_a_clean_plan_whose_verify_can_exit_1_is_a_change_plan(self):
        verify = "grep -q 'a - b' calc.py && exit 1 || exit 0"
        self.assertIsNone(smith.check_plan(dict(M_PLAN, type="CLEAN", criteria=[{"text": "t", "verify": verify}]),
                                           "an outcome stated as a fact"))
        for hollow in ("echo ok", "true", "python3 test_calc.py || exit 0", "grep -q x calc.py; exit 0"):
            with self.subTest(verify=hollow):
                why = smith.check_plan(dict(PLAN, criteria=[{"text": "t", "verify": hollow}]), "fix")
                self.assertIn("can't fail", why or "")
        self.assertIn("CLEAN", smith.SMALL_CHANGE)

    def test_a_second_plan_typed_clean_with_that_verify_is_taken_and_the_work_is_done(self):
        clean = dict(PLAN, type="CLEAN", title="Bring calc to a finished state",
                     criteria=[{"text": "calc.add is fixed", "verify": "grep -q 'a - b' calc.py && exit 1 || exit 0"}])
        f, r, ev = self.go(RELEASE, plan(RESEARCH_S), wants(True), plan(clean), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("kept the first", r.stdout)
        self.assertEqual((self.task()["type"], self.task()["status"]), ("CLEAN", "done"))
        self.assertIn("a + b", self.calc())

    def test_a_second_plan_gets_the_same_repairs_a_first_plan_gets(self):
        hollow = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "echo ok"}])
        f, r, ev = self.go(RELEASE, plan(RESEARCH_S), wants(True), plan(hollow), plan(PLAN), FIX_EDIT, DONE, CLOSE)
        self.assertIn("can't fail", f.said(3), "the check's own words go back to the model")
        self.assertEqual((self.task()["type"], self.task()["status"]), ("FIX", "done"), r.stdout + r.stderr)

    def test_a_second_plan_whose_verify_passes_already_goes_through_the_replan_round(self):
        green = dict(PLAN, criteria=[{"text": "calc imports", "verify": GREEN}])
        f, r, ev = self.go(RELEASE, plan(RESEARCH_S), wants(True), plan(green), plan(PLAN), FIX_EDIT, DONE, CLOSE)
        self.assertIn("already passes before any work", f.said(3))
        self.assertEqual((self.task()["type"], self.task()["status"]), ("FIX", "done"), r.stdout + r.stderr)

    HOLLOW = dict(PLAN, criteria=[{"text": "calc is done", "verify": "echo ok"}])
    LADDER_HOLLOW = ({"text": "FIX"}, {"text": "S"}, {"text": "Make it right"}, {"text": "calc is done :: echo ok"},
                     {"text": "1. edit calc.py"})

    def test_no_change_plan_after_the_ladder_says_why_in_one_line_and_that_the_change_went_unmade(self):
        one = dict(RESEARCH_S, steps=["cat calc.py"])
        decision = {"text": json.dumps({"decision": "Recommendation: change the operator; would change if: callers differ"})}
        f, r, ev = self.go(RELEASE, plan(one), wants(True), *[plan(self.HOLLOW)] * 3, *self.LADDER_HOLLOW,
                           call("run", cmd="true"), DONE, decision)
        notes =[e["text"] for e in ev if e["event"] == "note" and "unmade" in e["text"]]
        self.assertEqual(len(notes), 1, r.stdout + r.stderr)
        self.assertNotIn("\n", notes[0])
        self.assertIn("can't fail", notes[0], "the check's own words")
        self.assertNotIn("kept the first", notes[0])
        self.assertIn("RESEARCH", [e["plan"]["type"] for e in ev if e["event"] == "plan"][0])
        ended = [e for e in ev if e["event"] == "closed"]
        self.assertTrue(ended and any("unmade" in x for x in ended[-1]["unproven"]), r.stdout + r.stderr)

    def test_an_answer_that_no_change_plan_followed_says_the_change_went_unmade(self):
        f, r, ev = self.go("The code needs these fixes.", ANSWERED, wants(True), *[ANSWERED] * 3, *self.LADDER_HOLLOW,
                           call("done", summary="calc.add subtracts (calc.py line 2)"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        answer = [e for e in ev if e["event"] == "answer"]
        self.assertEqual(len(answer), 1)
        self.assertIn("unmade", answer[0]["text"])
        self.assertIn("return a - b", self.calc())

    def test_a_spend_cap_in_the_second_ask_stops_the_run_and_is_not_kept_as_the_first_plan(self):
        self.serve("openai", plan(RESEARCH_S), wants(True), plan(RESEARCH_S), plan(RESEARCH_S))
        test_code.Cost.priced(self)  # $0.07 a call: the plan, the judgment, the second ask, and the cap is met before its repair
        r = self.code(RELEASE, "--model", "fake", "--max-usd", "0.15", check=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("of its $0.15 cap", r.stderr)
        self.assertNotIn("kept the first", r.stdout)

    def test_the_gate_asks_nothing_of_a_cli_or_cloud_planner_after_either_an_answer_or_a_research_plan(self):
        def run(plan_, guided, kind):
            r = types.SimpleNamespace(plan=plan_, ask=False, guided=guided)
            with mock.patch.object(smith, "wants_change", side_effect=AssertionError("judged")):
                return smith._wants_work(r, {"name": "p", "kind": kind}, [], None)
        for what in ({"answer": "fix the sum"}, dict(RESEARCH_S)):
            for guided, kind in ((True, "cli"), (False, "openai"), (False, "cli")):
                with self.subTest(plan=sorted(what)[0], guided=guided, kind=kind):
                    self.assertIsNone(run(what, guided, kind))

    def test_the_reason_is_one_printable_line_of_bounded_length(self):
        f, r, ev = self.go("The code needs these fixes.", ANSWERED, wants(True, "ok\x1b[31m red\x07\nnext\x00 " + "y" * 500),
                           plan(PLAN), FIX_EDIT, DONE, CLOSE)
        got = [e for e in ev if "wants_change" in e][0]
        self.assertTrue(got["why"].isprintable(), repr(got["why"]))
        self.assertLessEqual(len(got["why"]), 200)
        self.assertNotIn("\x1b", r.stdout)


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
        # T-4363: the order is the mapping question first (two replies that fill nothing), the whole re-plan next, and
        # on its refusal the mapping question again: its reply, a new criterion, starts the task
        f, r = self.go(plan(M_PLAN), {"text": "hmm"}, {"text": "nope"}, plan(M_PLAN),
                       {"text": "0\nthe test file is unchanged :: python3 test_calc.py"},
                       FIX_EDIT, DONE, CLOSE, request="fix add, and keep the tests as they are", kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a criterion for", f.said(4))
        self.assertIn("keep the tests as they are", f.said(4))
        self.assertEqual([self.task(t)["status"] for t in ("T-0001", "T-0002", "T-0003")],
                         ["dropped", "dropped", "done"], r.stdout + r.stderr)
        self.assertIn("the test file is unchanged", json.dumps(self.task("T-0003")))

    def test_focus_refusal_whose_targeted_reply_fills_nothing_starts_with_the_part_set_aside(self):
        # T-4386: it ended with the refusal; now the clause no reply could tie to a check is carried as unproven (the
        # tests in test_smith_unproven.py hold the rest), after the same six requests and no more
        f, r = self.go(plan(M_PLAN), {"text": "hmm"}, {"text": "nope"}, plan(M_PLAN), {"text": "hmm"},
                       {"text": "nope"}, FIX_EDIT, DONE, CLOSE, request="fix add, and keep the tests as they are",
                       kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("set aside, to start without it", r.stdout)
        self.assertEqual(self.task("T-0002")["status"], "dropped")
        self.assertEqual(self.task("T-0003")["status"], "done")
        self.assertIn("unproven by this run", self.brief("T-0003"))

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

    # T-4363: the plan gate's clause refusal is answered by a mapping the planner gives -----------------------------------
    TWO = "Get these helpers to a finished state I could release; decide what that takes."
    TWO_PLAN = dict(M_PLAN, title="Bring the helpers in app.py to a release-ready state and cover them in test_app.py",
                    criteria=[{"text": "app.py helpers are complete with no TODO/stub/placeholder behavior left",
                               "verify": "! grep -q 'a - b' calc.py"},
                              {"text": "Every helper in app.py has a passing test in test_app.py",
                               "verify": "python3 test_calc.py"}])

    def brief(self, tid):
        with open(self.task(tid)["path"], encoding="utf-8") as fh:
            return fh.read()

    def test_clause_map_one_and_none_start_the_task_with_both_criteria_and_a_non_goals_line(self):
        # exam s8-048 goal-python-14283a: the plan is sound, the gate reads words; the planner says what covers what
        f, r = self.go(plan(self.TWO_PLAN), {"text": "1"}, {"text": "none"}, FIX_EDIT, DONE, CLOSE, request=self.TWO,
                       kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Get these helpers to a finished state I could release", f.said(1))
        self.assertIn("decide what that takes", f.said(2))
        self.assertIn("which of your criteria delivers this part", f.said(1))
        # no whole JSON re-plan (the plan was sound); counted as what was asked, not the run's total, which a closing
        # look before the close may lengthen
        self.assertFalse(any("The plan gate refused this plan" in f.said(i) for i in range(len(f.seen))), f.said(2))
        self.assertEqual([self.task(t)["status"] for t in ("T-0001", "T-0002")], ["dropped", "done"], r.stdout)
        text = self.brief("T-0002")
        self.assertIn("Get these helpers to a finished state I could release: app.py helpers are complete with no "
                      "TODO/stub/placeholder behavior left", text)
        self.assertIn("! grep -q 'a - b' calc.py", text)  # its check is untouched
        self.assertIn("Every helper in app.py has a passing test in test_app.py", text)
        self.assertEqual(text.count("verify with"), 2, text)
        self.assertIn(f"- {smith._none_line('decide what that takes')}", text)
        # (b) the clause that rests on another criterion's check is recorded in the task and in one note
        self.assertIn("Clause map: 'Get these helpers to a finished state I could release' rests on criterion 1's "
                      "check, not one of its own", text)
        self.assertIn("plan gate: 'Get these helpers to a finished state I could release' -> criterion 1", r.stdout)

    def test_clause_map_question_lists_the_criteria_numbered_with_their_checks(self):
        f, r = self.go(plan(self.TWO_PLAN), {"text": "1"}, {"text": "none"}, FIX_EDIT, DONE, CLOSE, request=self.TWO,
                       kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        asked = json.dumps(f.seen[1]["messages"][-1])
        self.assertIn("1. app.py helpers are complete with no TODO/stub/placeholder behavior left :: ! grep -q", asked)
        self.assertIn("2. Every helper in app.py has a passing test in test_app.py :: python3 test_calc.py", asked)

    def test_clause_map_numbers_past_the_bound_get_the_old_question_with_a_new_check(self):
        # one clause of two may rest on another criterion's check (MAPPED_ONE_IN): the second number is not read
        self.assertEqual(smith.MAPPED_ONE_IN, 2)
        f, r = self.go(plan(self.TWO_PLAN), {"text": "2."}, {"text": "1"},
                       {"text": "what that takes is decided :: python3 test_calc.py"}, FIX_EDIT, DONE, CLOSE,
                       request=self.TWO, kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("what that part means when it is done", f.said(3), "past the bound: the old question")
        text = self.brief("T-0002")
        self.assertIn("Get these helpers to a finished state I could release: Every helper in app.py", text)
        self.assertIn("decide what that takes: what that takes is decided", text)
        self.assertEqual(text.count("verify with"), 3, text)
        self.assertEqual(text.count("rests on criterion"), 1, text)

    def planner_non_goals(self, bad):
        # a planner's own non_goals (a list excusing every clause, a number, a string) is dropped, not written
        f, r = self.go(plan(dict(self.TWO_PLAN, non_goals=bad)), {"text": "1"}, {"text": "none"}, FIX_EDIT, DONE, CLOSE,
                       request=self.TWO, kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("which of your criteria delivers this part", f.said(1), "the gate still refused it")
        text = self.brief("T-0002")
        self.assertEqual(text.count(smith._none_line("decide what that takes")), 1, text)
        self.assertNotIn("- Get these helpers to a finished state I could release\n", text)
        self.assertEqual(text.count("none of this is to be made"), 1, text)

    def test_clause_map_a_planner_non_goals_list_never_reaches_the_brief(self):
        self.planner_non_goals(["Get these helpers to a finished state I could release", "decide what that takes"])

    def test_clause_map_a_planner_non_goals_number_never_reaches_the_brief(self):
        self.planner_non_goals(5)

    def test_clause_map_a_planner_non_goals_string_never_reaches_the_brief(self):
        self.planner_non_goals("decide what that takes")

    def test_clause_map_a_number_leaves_a_plan_whose_other_criterion_stays_as_it_was(self):
        f, r = self.go(plan(self.TWO_PLAN), {"text": "2."}, {"text": "0\nwhat that takes is decided :: python3 test_calc.py"},
                       FIX_EDIT, DONE, CLOSE, request=self.TWO, kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = self.brief("T-0002")
        self.assertIn("Get these helpers to a finished state I could release: Every helper in app.py", text)
        self.assertIn("app.py helpers are complete", text)
        self.assertNotIn("decide what that takes: app.py helpers", text)
        self.assertNotIn(smith._none_line("decide what that takes"), text)

    def test_clause_map_zero_with_a_line_adds_one_new_criterion(self):
        f, r = self.go(plan(M_PLAN), {"text": "0\nthe test file is unchanged :: python3 test_calc.py"}, FIX_EDIT, DONE,
                       CLOSE, request="fix add, and keep the tests as they are", kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("which of your criteria delivers this part", f.said(1))  # what was asked up to the task's start
        self.assertFalse(any("The plan gate refused this plan" in f.said(i) for i in range(len(f.seen))))
        self.assertEqual([self.task(t)["status"] for t in ("T-0001", "T-0002")], ["dropped", "done"], r.stdout)
        text = self.brief("T-0002")
        self.assertIn("keep the tests as they are: the test file is unchanged", text)
        self.assertIn("calc.add adds", text, "the plan's own criterion stays")

    def test_clause_map_unreadable_reply_asks_the_old_question_once(self):
        f, r = self.go(plan(M_PLAN), {"text": "I am not sure"}, {"text": "the test file is unchanged :: python3 test_calc.py"},
                       FIX_EDIT, DONE, CLOSE, request="fix add, and keep the tests as they are", kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("which of your criteria delivers this part", f.said(1))
        self.assertIn("what that part means when it is done", f.said(2))
        self.assertIn("keep the tests as they are: the test file is unchanged", self.brief("T-0002"))

    def test_clause_map_a_reply_that_fills_nothing_twice_goes_to_the_whole_replan_without_its_own_words_hint(self):
        again = dict(M_PLAN, criteria=M_PLAN["criteria"] + [
            {"text": "keep the tests as they are: test_calc.py is unchanged", "verify": "python3 test_calc.py"}])
        f, r = self.go(plan(M_PLAN), {"text": "hmm"}, {"text": "nope"}, plan(again), FIX_EDIT, DONE, CLOSE,
                       request="fix add, and keep the tests as they are", kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("The plan gate refused this plan", f.said(3))
        self.assertNotIn("own words", f.said(3))
        self.assertIn("a criterion for", f.said(3))
        self.assertEqual(self.task("T-0002")["status"], "done", r.stdout + r.stderr)

    def test_clause_map_none_for_the_only_clause_is_an_unreadable_reply(self):
        f, r = self.go(plan(M_PLAN), {"text": "none"}, {"text": "the test file is unchanged :: python3 test_calc.py"},
                       FIX_EDIT, DONE, CLOSE, request="fix add, and keep the tests as they are", kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("what that part means when it is done", f.said(2), "asked the old question instead")
        self.assertNotIn(smith._none_line("keep the tests as they are"), self.brief("T-0002"))

    def test_clause_map_a_second_none_is_an_unreadable_reply(self):
        f, r = self.go(plan(self.TWO_PLAN), {"text": "none"}, {"text": "none"},
                       {"text": "what that takes is decided :: python3 test_calc.py"}, FIX_EDIT, DONE, CLOSE,
                       request=self.TWO, kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("what that part means when it is done", f.said(3), "the second none gets the old question")
        text = self.brief("T-0002")
        self.assertEqual(text.count("none of this is to be made"), 1, text)
        self.assertIn("what that takes is decided", text)

    def test_clause_map_zero_with_a_check_that_passes_now_goes_to_the_replan_with_that_problem(self):
        again = dict(M_PLAN, criteria=M_PLAN["criteria"] + [
            {"text": "keep the tests as they are: test_calc.py is unchanged", "verify": "python3 test_calc.py"}])
        f, r = self.go(plan(M_PLAN), {"text": "0\nthe module loads :: python3 -c 'import calc'"}, plan(again), FIX_EDIT,
                       DONE, CLOSE, request="fix add, and keep the tests as they are", kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already passes before any work", f.said(2))
        self.assertEqual(self.task("T-0002")["status"], "done", r.stdout + r.stderr)

    # T-4363: no plan refusal ends the run while a repair round is left -------------------------------------------------
    def test_replan_round_a_ladder_plan_whose_check_passes_goes_back_before_the_task_is_made(self):
        # the field-by-field plan meets _plan_problem in _plan like any other: a green-before verify never reaches task new
        ladder = ("FIX", "S", "Make calc.add add", f"calc.add adds two numbers :: {GREEN}", "1. edit calc.py")
        f, r = self.go(*[{"text": "nope"}] * 3, *[{"text": a} for a in ladder], plan(PLAN), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("came field by field", r.stdout)
        self.assertIn("already passes before any work", f.said(3 + len(ladder)))
        self.assertEqual(self.task()["status"], "done", "the first task made is the corrected plan's")

    HARD = dict(M_PLAN, steps=["delete ./", "fix calc.add"])
    GREEN_M = dict(M_PLAN, criteria=[{"text": "calc.add adds", "verify": GREEN}])

    def test_replan_round_a_replan_the_check_refuses_gets_one_more_round_with_the_problem(self):
        f, r = self.go(plan(self.HARD), plan(self.GREEN_M), plan(M_PLAN), FIX_EDIT, DONE, CLOSE, kind="anthropic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already passes before any work", f.said(2))
        self.assertIn("fails the harness's own check", json.dumps(f.seen[2]["messages"][-1]))  # the last message, not the first re-plan's
        self.assertEqual([self.task(t)["status"] for t in ("T-0001", "T-0002")], ["dropped", "done"], r.stdout)

    def test_replan_round_the_extra_round_is_the_last_one(self):
        f, r = self.go(plan(self.HARD), plan(self.GREEN_M), plan(self.GREEN_M), kind="anthropic")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already passes before any work", r.stderr + r.stdout)
        self.assertEqual(len(f.seen), 3)
        self.assertEqual(self.task("T-0001")["status"], "planned")  # it stays to be looked at

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


class ClauseMapParse(unittest.TestCase):  # T-4363 review: what a reply to the mapping question is read as
    def test_a_number_counts_only_as_the_whole_first_line(self):
        for text in ("2", " 2. ", "Criterion 2", "criterion #2.", "#2", "2\nit covers the second part"):
            with self.subTest(text=text):
                self.assertEqual(smith._clause_map(text, 3), ("crit", 2))
        for text in ("2 of them cover it", "1 and 3", "1, 2", "-1", "I think 2", "2)", "number 2", "1.0", "4", "7"):
            with self.subTest(text=text):
                self.assertIsNone(smith._clause_map(text, 3), "unreadable: the old question is asked")

    def test_none_is_the_whole_first_line_too(self):
        for text in ("none", "None.", "none\nit asks for nothing to be made"):
            self.assertEqual(smith._clause_map(text, 3), ("none",), text)
        for text in ("none of them", "None cover it", "nothing", "none, I think"):
            self.assertIsNone(smith._clause_map(text, 3), text)

    def test_zero_takes_only_the_one_line_holding_the_separator(self):
        self.assertEqual(smith._clause_map("0\nNone cover it.\nwhat it means :: python3 t.py\nHope this helps", 2),
                         ("new", "what it means", "python3 t.py"))
        self.assertEqual(smith._clause_map("0\n`what it means :: python3 t.py`", 2),
                         ("new", "what it means", "python3 t.py"))
        for text in ("0", "0\nno line with both halves", "0 :: x", "0: what :: check", "0\n :: check", "0\nwhat :: "):
            self.assertIsNone(smith._clause_map(text, 2), text)

    def test_a_run_of_digits_is_unreadable_not_an_error(self):
        for text in ("9" * 4301, "0" * 4301, "1" * 4):
            self.assertIsNone(smith._clause_map(text, 3))
        self.assertIsNone(smith._clause_map("", 3))
        self.assertIsNone(smith._clause_map(None, 3))

    def test_the_non_goals_line_carries_the_clause_and_stop_words_only(self):
        from sampo import recall
        line = smith._none_line("decide what that takes")
        self.assertEqual(set(recall._tokens(line)), set(recall._tokens("decide what that takes")))
        other = "keep the decision criteria next to the release notes"  # shared stems with a wordier boilerplate
        self.assertFalse(set(recall._tokens(line)) & set(recall._tokens(other)))

    def test_the_gate_counts_a_request_without_its_context_lines(self):
        from sampo import recall
        for ctx in ("CONTEXT: calc.py is in the root", "> CONTEXT: calc.py is in the root"):
            req = f"fix add, and keep the tests as they are\n{ctx}"
            self.assertEqual(len(recall.clauses(req)), 2, "recall.clauses alone counts the context line")
            self.assertEqual(len(smith._gate_clauses(req)), 1, ctx)

    def make(self, plan, fm):
        return types.SimpleNamespace(request="fix add", plan=plan, fm=fm, plan_msgs=None, root=".")

    def test_a_failed_task_set_ends_the_run_with_the_refused_task_kept(self):
        calls = []

        def fm(*a):
            calls.append(a)
            return (0, json.dumps({"id": "T-0009"})) if a[:2] == ("task", "new") else (1, "no such section")
        pl = smith._ClauseMap(M_PLAN)
        pl.none_lines = [smith._none_line("keep it")]
        with self.assertRaises(smith.CodeError) as cm:
            smith._make_task(self.make(pl, fm), None)
        self.assertIn("Non-goals", str(cm.exception))
        self.assertEqual(cm.exception.then, "fm task show T-0009")
        self.assertEqual([c[:2] for c in calls], [("task", "new"), ("task", "set")])
        self.assertIn("- keep it: none of this is to be made", calls[1][-1])

    def test_a_plan_the_planner_wrote_never_runs_task_set(self):
        calls = []

        def fm(*a):
            calls.append(a)
            return (0, json.dumps({"id": "T-0009"})) if a[:2] == ("task", "new") else (1, "stop here")
        for bad in (["everything"], 5, "text"):
            with self.subTest(bad=bad), mock.patch.object(smith.core, "find_project", return_value=None), \
                    self.assertRaises(smith.CodeError):
                smith._make_task(self.make(dict(M_PLAN, non_goals=bad), fm), None)
        self.assertNotIn(("task", "set"), [c[:2] for c in calls])


class PlanFormRecord(CodeCase):
    """Review of T-4282: through the second ask of T-4294 (items on record the plan doesn't name) the downgrade follows
    the plan that stands."""
    repo_seed = ("seed record", CodeCase.repo_seed[1] + (("PLAN.md", "# Plan\n\n- [ ] Make parse_line() handle tabs\n"),))

    def dry(self, *script):
        # T-4425: PLAN.md is a document, so a model-planned request surveys first: the change verdict, the survey's done
        # (PLAN.md was never opened: named back once, a second done), then the plan asks
        self.serve("openai", wants(True), DONE, DONE, *script)
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


class VerifyShape(CodeCase):
    """T-4445: on the hard exam a field-by-field plan's verify was `python3 -c "` (the first line of a command that
    spanned five): the work was done and the run blocked on a check bash could not parse. A verify whose quote is still
    open takes the lines that follow, as written; one bash cannot parse is refused at the plan, with bash's reason."""

    def test_multi_line_verify_of_a_field_by_field_plan_is_kept_whole(self):
        text = ('- totals match :: python3 -c "\nimport calc\n\nif True:\n    assert calc.add(2, 3) == 5\n"\n'
                "- docs say so :: grep -q sum README.md\nno sep")
        got = smith._criteria(text)
        self.assertEqual(len(got), 2, got)
        self.assertEqual(got[0], {"text": "totals match",
                                  "verify": 'python3 -c "\nimport calc\n\nif True:\n    assert calc.add(2, 3) == 5\n"'})
        self.assertEqual(got[1], {"text": "docs say so", "verify": "grep -q sum README.md"})
        self.assertEqual(smith._criteria("1. a :: `x`\nno sep"), [{"text": "a", "verify": "x"}],
                         "a verify that is whole takes no line after it")
        self.assertEqual(smith._criteria("- a :: `sh -c 'echo\n  ok'`"), [{"text": "a", "verify": "sh -c 'echo\n  ok'"}])

    def test_unparsable_verify_goes_back_to_the_planner_with_bashs_reason(self):
        self.assertIsNone(smith._shell_syntax("python3 -c 'print(1)' && echo ok"))
        self.assertIn("EOF", smith._shell_syntax('python3 -c "'))
        cut = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": 'python3 -c "'}])
        f = self.serve("openai", plan(cut), plan(PLAN), FIX_EDIT, DONE, CLOSE)
        r = self.code("fix add", "--model", "fake", "--json", check=False)
        said = [f.said(i) for i in range(len(f.seen))]
        self.assertTrue(any("not a complete shell command" in s for s in said), r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)


if __name__ == "__main__":  # T-3688: run as a script, as the brief's verify does, it ran nothing before
    unittest.main()
