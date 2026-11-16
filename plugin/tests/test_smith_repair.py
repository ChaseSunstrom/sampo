"""T-4293: a plan repair changes what the refusal named and nothing else, and a criterion that is the request's own words
is refused. Exam goal-c-0b1e05 (off arm, 2026-11-11): a concrete first plan (a Makefile with C11 warnings as errors, test
and docs-check targets) was replaced 170 s later by one whose title and first criterion were the request verbatim, and
that one was taken. The echo rule is a backstop, not a proof that the criterion adds something: it bites only a one-word
verify; the one-field repair and `_lost` are the real protection."""
import json
import os
import unittest
from unittest import mock

from test_code import CLOSE, PLAN, CodeCase, call, plan

from sampo import smith

DONE = call("done", summary="fixed")
FIX_EDIT = call("edit", file="calc.py", old="a - b", new="a + b")
GREEN = "python3 -c 'import calc'"  # passes before any work: the plan gate refuses that criterion's verify
GOOD_VERIFY = "python3 -c \"import calc; assert calc.add(1, 1) == 2\""
REQUEST = "Get these helpers to a finished state I could release; decide what that takes."

FIRST = dict(PLAN, title="Make calc.add add and check it from a docs target",
             criteria=[{"text": "calc.add adds", "verify": "python3 test_calc.py"},
                       {"text": "the docs-check target passes", "verify": GREEN},
                       {"text": "calc imports cleanly", "verify": "python3 -c 'import calc; assert calc.add(2, 1) == 3'"}],
             steps=["fix calc.add, then run the checks"])
# ASSUMPTION: the exam's second plan's verify is not known; BARE ("make") is a guess at a one-word verify. The echo rule
# is a backstop, not a proof that the criterion adds something: it bites only a one-word verify.
BARE = "make"  # a verify that is only a program: it names no file and no argument of its own
ECHO = dict(PLAN, title=REQUEST, criteria=[{"text": REQUEST, "verify": BARE}], steps=["do it"])
ECHO_WHY = "a criterion must name something a command can show"
THIN = dict(FIRST, criteria=[dict(FIRST["criteria"][1], verify=GOOD_VERIFY)])  # one of FIRST's three, repaired
FULL = dict(FIRST, criteria=[FIRST["criteria"][0], dict(FIRST["criteria"][1], verify=GOOD_VERIFY), FIRST["criteria"][2]])


def with_criterion(text, base=PLAN, verify=BARE):
    return dict(base, criteria=[{"text": text, "verify": verify}])


class RepairKeepsFirst(CodeCase):
    def go(self, *script, request="fix add"):
        f = self.serve("openai", *script)
        return f, self.code(request, "--model", "fake", check=False)

    def test_keeps_first_plan_with_only_the_refused_field_changed_and_a_note_naming_it(self):
        f, r = self.go(plan(FIRST), {"text": json.dumps({"verify": GOOD_VERIFY})}, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        t = self.task()
        self.assertEqual(t["title"], FIRST["title"])
        brief = json.dumps(t)
        for cr in FIRST["criteria"]:
            self.assertIn(cr["text"], brief)
        self.assertIn("python3 test_calc.py", brief)
        self.assertIn("assert calc.add(1, 1) == 2", brief)
        self.assertNotIn(GREEN, brief, "the refused verify is the one thing that changed")
        self.assertIn("criterion 2's verify was repaired alone", r.stdout)

    def test_keeps_first_question_asks_for_the_refused_field_only_and_quotes_the_plan_and_the_refusal(self):
        f, r = self.go(plan(FIRST), {"text": json.dumps({"verify": GOOD_VERIFY})}, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        asked = json.dumps(f.seen[1]["messages"][-1])
        self.assertIn("criterion 2", asked)
        self.assertIn("with verify for", asked, "it asks for the one key")
        self.assertIn("already passes before any work", asked)
        self.assertNotIn("corrected plan as a JSON object", asked, "no whole-plan repair where a field is named")

    def test_keeps_first_the_run_of_the_exam_never_takes_the_request_as_a_criterion(self):
        # the exam's second plan, the request verbatim, answers the question: it is refused with its reason, told to the
        # planner, and the plan that goes on keeps the first plan's criteria
        fixed = dict(FIRST, criteria=[FIRST["criteria"][0], dict(FIRST["criteria"][1], verify=GOOD_VERIFY),
                                      FIRST["criteria"][2]])
        f, r = self.go(plan(FIRST), plan(ECHO), plan(fixed), FIX_EDIT, DONE, CLOSE, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(ECHO_WHY, f.said(2), "the planner is told why the plan with the request's words was refused")
        t = self.task()
        self.assertEqual(t["title"], FIRST["title"])
        self.assertIn(FIRST["criteria"][1]["text"], json.dumps(t))

    def test_keeps_first_a_whole_replacement_that_loses_a_criterion_nobody_named_is_sent_back_once(self):
        thin = dict(FIRST, criteria=[dict(FIRST["criteria"][1], verify=GOOD_VERIFY)])
        full = dict(FIRST, criteria=[FIRST["criteria"][0], dict(FIRST["criteria"][1], verify=GOOD_VERIFY),
                                     FIRST["criteria"][2]])
        # the refusal (an unreadable reply to the field question) falls back to the whole plan; the thin one drops two
        f, r = self.go(plan(FIRST), {"text": "I do not know"}, plan(thin), plan(full), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("drops", f.said(3))
        self.assertIn(FIRST["criteria"][2]["text"], f.said(3))
        self.assertIn(FIRST["criteria"][2]["text"], json.dumps(self.task()))

    def test_keeps_first_a_second_lossy_replacement_is_taken_as_before_and_says_what_stayed_dropped(self):
        f, r = self.go(plan(FIRST), {"text": "I do not know"}, plan(THIN), plan(THIN), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(FIRST["criteria"][2]["text"], json.dumps(self.task()))
        self.assertIn("stayed dropped", r.stdout, "the run says it took a plan that lost criteria nobody named")
        self.assertIn(FIRST["criteria"][2]["text"], r.stdout.split("stayed dropped", 1)[1])
        self.assertNotIn("asked once more to keep them", r.stdout, "it did not keep them: no such claim")

    def test_keeps_first_a_second_ask_that_is_an_answer_leaves_the_first_replacement_and_says_what_it_dropped(self):
        f, r = self.go(plan(FIRST), {"text": "I do not know"}, plan(THIN), {"text": json.dumps({"answer": "ok"})},
                       FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("stayed dropped", r.stdout)

    def test_keeps_first_a_second_ask_that_gives_no_valid_plan_leaves_no_turn_behind(self):
        msgs, calls = [{"role": "user", "content": "q"}], []

        def fake_slot(run, profile, messages, check, what, fields=(), given=None):
            messages.append({"role": "assistant", "content": "reply"})
            if calls:
                raise smith.NoValidSlot("no valid plan", then=None)
            calls.append(1)
            return THIN
        run = mock.Mock()
        with mock.patch.object(smith, "slot", fake_slot), mock.patch.object(smith, "run_friction"):
            got = smith._whole(run, {}, msgs, smith.check_plan, FIRST)
        self.assertIs(got, THIN)
        self.assertEqual([x["role"] for x in msgs], ["user", "assistant"], "it ends on the plan that stands")

    def test_keeps_first_a_second_replacement_that_keeps_them_all_says_so_once(self):
        f, r = self.go(plan(FIRST), {"text": "I do not know"}, plan(THIN), plan(FULL), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("asked once more to keep them", r.stdout)
        self.assertNotIn("stayed dropped", r.stdout)

    def test_keeps_first_a_criterion_with_its_text_kept_and_a_weaker_verify_counts_as_changed(self):
        weak = dict(FIRST, criteria=[dict(FIRST["criteria"][0], verify="python3 -c 'pass'"), FIRST["criteria"][1],
                                     FIRST["criteria"][2]])
        self.assertEqual(smith._lost(FIRST, weak), [FIRST["criteria"][0]["text"]])
        self.assertEqual(smith._lost(FIRST, FULL, named=(2,)), [], "criterion 2 is the one the refusal named")
        self.assertEqual(smith._lost(FIRST, FIRST), [])
        self.assertEqual(smith._lost(FIRST, {"answer": "x"}), [])
        reworded = dict(FIRST, criteria=[dict(FIRST["criteria"][0], text="add adds"), *FIRST["criteria"][1:]])
        self.assertEqual(smith._lost(FIRST, reworded), [FIRST["criteria"][0]["text"]], "the pair is what is kept")

    def test_keeps_first_a_plan_trimmed_by_a_refusal_and_repaired_by_one_field_still_reports_what_was_set_aside(self):
        # the field ladder ends on a plan whose second verify can't fail: it is set aside; then the harness refuses the
        # third (it passes before any work) and that one is repaired alone, in a new plan object
        cant = {"text": "the run is done", "verify": "python3 test_calc.py; exit 0"}
        ladder = [{"text": "nope"}] * 3 + [{"text": "Type: FIX."}, {"text": "S"}, {"text": "Make calc.add add"},
                                          {"text": f"- calc.add adds :: `python3 test_calc.py`\n"
                                                   f"- {cant['text']} :: `{cant['verify']}`\n"
                                                   f"- the docs-check target passes :: `{GREEN}`"},
                                          {"text": "1. fix calc.add"}]
        f, r = self.go(*ladder, {"text": json.dumps({"verify": GOOD_VERIFY})}, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("criterion 2's verify was repaired alone", r.stdout)
        self.assertIn("set aside, to start without it: the run is done", r.stdout)
        self.assertIn("not proven", r.stdout, "the run's end says nothing proved it")

    def test_keeps_first_a_model_failure_on_the_field_question_leaves_the_conversation_in_turn(self):
        f, r = self.go(plan(FIRST), {}, plan(FULL), FIX_EDIT, DONE, CLOSE)  # {} is an error reply
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        roles = [x["role"] for x in f.seen[2]["messages"] if x["role"] != "system"]
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), roles)
        self.assertEqual(self.task()["title"], FIRST["title"])

    def test_keeps_first_a_patch_whose_verify_can_not_fail_is_refused_and_the_whole_plan_asked(self):
        f, r = self.go(plan(FIRST), {"text": json.dumps({"verify": "true"})}, plan(FULL), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("can't fail", f.said(2))
        self.assertIn("did not settle it", f.said(2))
        self.assertEqual([c["verify"] for c in self.task()["criteria"]][1], GOOD_VERIFY)

    def test_keeps_first_extra_keys_in_a_patch_change_nothing_and_the_exam_shape_keeps_the_plan(self):
        # a patch that also offers the request as text and a new title: only the verify the refusal named is taken
        patch = {"verify": GOOD_VERIFY, "text": REQUEST, "title": REQUEST}
        f, r = self.go(plan(FIRST), {"text": json.dumps(patch)}, FIX_EDIT, DONE, CLOSE, request=REQUEST)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        t = self.task()
        self.assertEqual(t["title"], FIRST["title"])
        brief = json.dumps(t)
        for cr in FIRST["criteria"]:
            self.assertIn(cr["text"], brief)
        self.assertNotIn(REQUEST, brief)

    def test_keeps_first_a_whole_replacement_that_answers_is_not_taken_as_the_plan(self):
        f, r = self.go(plan(FIRST), {"text": "I do not know"}, {"text": json.dumps({"answer": "no change needed"})},
                       FIX_EDIT, DONE, CLOSE)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertNotIn("KeyError", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class RequestWords(CodeCase):
    def test_request_words_a_criterion_equal_to_the_request_is_refused_with_the_reason(self):
        why = smith.check_plan(with_criterion(REQUEST), None, REQUEST)
        self.assertIn(ECHO_WHY, why)
        self.assertEqual((why.criterion, why.field), (1, ("criterion", 1, ("text",))))

    def test_request_words_case_space_and_punctuation_are_folded(self):
        for text in (REQUEST.upper(), "  " + REQUEST.replace(" ", "   ") + "  ", REQUEST.rstrip("."),
                     "get these helpers to a finished state i could release decide what that takes"):
            with self.subTest(text=text):
                self.assertIn(ECHO_WHY, smith.check_plan(with_criterion(text), None, REQUEST) or "")

    def test_request_words_most_of_the_request_is_refused_and_a_few_quoted_words_are_not(self):
        most = "Get these helpers to a finished state I could release; decide what"  # 80%+ of the request's length
        self.assertIn(ECHO_WHY, smith.check_plan(with_criterion(most), None, REQUEST) or "")
        for text in ("decide what that takes", "a finished state: python3 test_calc.py exits 0 for add(2, 3)",
                     "the helpers, got to a finished state I could release, are tested by test_calc.py",
                     "Get these helpers"):
            with self.subTest(text=text):
                self.assertIsNone(smith.check_plan(with_criterion(text), None, REQUEST))

    def test_request_words_a_criterion_whose_verify_has_a_file_or_an_argument_of_its_own_is_no_echo(self):
        for verify in ("python3 test_calc.py", "make test", "python3 -m pytest -q", "./check.sh"):
            with self.subTest(verify=verify):
                self.assertIsNone(smith.check_plan(with_criterion(REQUEST, verify=verify), None, REQUEST))

    def test_request_words_a_criterion_whose_verify_is_a_bare_program_adds_nothing_and_is_refused(self):
        for verify in ("make", "pytest"):
            with self.subTest(verify=verify):
                self.assertIn(ECHO_WHY, smith.check_plan(with_criterion(REQUEST, verify=verify), None, REQUEST) or "")

    def test_request_words_a_short_request_restated_with_a_real_verify_is_run(self):
        short = "make the tests pass"
        self.serve("openai", plan(with_criterion(short, verify="python3 test_calc.py")), FIX_EDIT, DONE, CLOSE)
        r = self.code(short, "--model", "fake", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(short, json.dumps(self.task()))

    def test_request_words_no_request_no_rule(self):
        self.assertIsNone(smith.check_plan(with_criterion(REQUEST)))
        self.assertIsNone(smith.check_plan(with_criterion(REQUEST), None, ""))

    def test_request_words_only_the_criterion_text_counts_never_the_title(self):
        self.assertIsNone(smith.check_plan(dict(with_criterion("python3 test_calc.py exits 0"), title=REQUEST), None,
                                           REQUEST))

    def test_request_words_a_plan_with_the_request_as_a_criterion_is_sent_back_and_the_good_one_is_taken(self):
        f = self.serve("openai", plan(ECHO), plan(PLAN), FIX_EDIT, DONE, CLOSE)
        r = self.code(REQUEST, "--model", "fake", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        asked = json.dumps(f.seen[1]["messages"][-1])
        self.assertIn(ECHO_WHY, asked)
        self.assertIn("Criterion 1's text", asked, "the refusal names the criterion and the field")
        self.assertEqual(self.task()["title"], PLAN["title"])


if __name__ == "__main__":
    unittest.main()
