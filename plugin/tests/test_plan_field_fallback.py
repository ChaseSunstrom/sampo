"""T-4594: the field-by-field plan fallback is not the end of a run. A missing or invalid type is asked alone once more
with the allowed values, then taken from the verdict the run's own judge call gave (find_fix judged by the model is a FIX,
a judged speed-up is PERFORMANCE); a criterion still without its verify after its own question is set aside as not
proven (the T-4386 set-aside) and the run starts with the sound ones. A plan with no sound criterion, or a type nothing
decides, still ends the run with the same reason. A scripted model, no word lists."""
import argparse
import json
import os
import sys
import unittest

import test_code
from test_code import CodeCase, FIX

from helpers import PLUGIN

sys.path.insert(0, os.path.join(PLUGIN, "lib"))
from sampo import smith  # noqa: E402

NOPE = ["nope"] * 3  # the three JSON tries (the reply and its two repairs) that fail
CRITERIA = "calc.add adds :: python3 test_calc.py\ncalc.add keeps zero ::"  # the second has text and no verify
SOUND = "calc.add adds :: python3 test_calc.py\ncalc.add keeps zero :: python3 test_calc.py"


def fields(type_="FIX", criteria=SOUND):
    return [type_, "S", "Make calc.add add", criteria, "1. fix calc.add"]


class Scripted(CodeCase):
    """slot() against a scripted model: each _ask takes the next reply and keeps the question it was sent."""

    def go(self, replies, **verdict):
        run = smith.Run(argparse.Namespace(), self.repo, os.path.join(self.tmp, "code"))
        run.guard = lambda tool, args: None
        for k, x in verdict.items():
            setattr(run, k, x)
        self.asked, left, sent = [], list(replies), []

        def ask(run_, profile, messages, what, **kw):
            sent.append(messages[-1]["content"])
            if not left:
                raise smith.m.ModelError("script exhausted")
            return {"text": left.pop(0), "tool_calls": [], "usage": {}}
        old, smith._ask = smith._ask, ask
        self.addCleanup(setattr, smith, "_ask", old)
        msgs = [{"role": "system", "content": "plan"}, {"role": "user", "content": "fix add"}]
        try:
            out = smith.slot(run, {"name": "fake"}, msgs, smith.check_plan, "plan", smith.PLAN_FIELDS)
        except smith.NoValidSlot as e:
            out = e
        self.asked, self.left = sent, left
        return run, out

    def notes(self, run):
        return [e.get("text", "") for e in run.kept]  # no journal here: the run keeps its events


class TypeIsAskedAloneThenJudged(Scripted):
    def test_a_type_that_is_no_type_is_asked_alone_once_more_with_the_allowed_values(self):
        run, out = self.go(NOPE + fields("banana") + ["fix"])
        self.assertIsInstance(out, dict, out)
        self.assertEqual(out["type"], "FIX")
        last = self.asked[-1]
        for t in smith.TYPES:
            self.assertIn(t, last)
        self.assertEqual(len(self.asked), 3 + 5 + 1, "one extra question, for the type alone")
        self.assertEqual(self.left, [])

    def test_a_valid_type_costs_no_extra_question(self):
        run, out = self.go(NOPE + fields("FIX"))
        self.assertEqual(out["type"], "FIX")
        self.assertEqual(len(self.asked), 3 + 5)

    def test_a_type_still_invalid_is_taken_from_the_judged_find_fix(self):
        run, out = self.go(NOPE + fields("banana") + ["still no"], wants=(True, "judged"), find_fix=True,
                           find_fix_judged=True)
        self.assertIsInstance(out, dict, out)
        self.assertEqual(out["type"], "FIX")
        self.assertTrue(any("judged verdict" in n for n in self.notes(run)), self.notes(run))

    def test_a_type_still_invalid_is_taken_from_a_judged_speed_up(self):
        run, out = self.go(NOPE + fields("banana") + ["still no"], wants=(True, "judged"), find_fix=False,
                           find_fix_judged=True, speed_up=True)
        self.assertEqual(out["type"], "PERFORMANCE")

    def test_a_find_fix_the_word_lists_gave_decides_nothing(self):
        run, out = self.go(NOPE + fields("banana") + ["still no"], wants=(True, "fallback"), find_fix=True)
        self.assertIsInstance(out, smith.NoValidSlot)
        self.assertIn("nor field by field", str(out))
        self.assertIn("type must be one of", str(out))

    def test_no_verdict_yet_decides_nothing(self):
        run, out = self.go(NOPE + fields("banana") + ["still no"], find_fix=True, find_fix_judged=True)
        self.assertIsInstance(out, smith.NoValidSlot, "the run was never judged: wants is unset")

    def test_a_judged_not_find_fix_decides_nothing(self):
        run, out = self.go(NOPE + fields("banana") + ["still no"], wants=(True, "judged"), find_fix=False,
                           find_fix_judged=True)
        self.assertIsInstance(out, smith.NoValidSlot)

    def test_a_model_that_dies_on_the_type_question_leaves_the_end_as_it_was(self):
        run, out = self.go(NOPE + fields("banana"))  # the script ends: the extra question fails at the model
        self.assertIsInstance(out, smith.NoValidSlot)
        self.assertIn("nor field by field", str(out))


class CriterionIsAskedAloneThenSetAside(Scripted):
    def test_a_missing_verify_is_asked_alone_and_a_good_answer_keeps_the_criterion(self):
        run, out = self.go(NOPE + fields(criteria=CRITERIA) + ["`python3 test_calc.py`"])
        self.assertIsInstance(out, dict, out)
        self.assertEqual([c["verify"] for c in out["criteria"]], ["python3 test_calc.py"] * 2)
        self.assertIn("calc.add keeps zero", self.asked[-1], "the question names the criterion it is about")
        self.assertEqual(len(self.asked), 3 + 5 + 1)

    def test_a_verify_still_missing_ends_the_slot_on_the_criterion_for_the_set_aside(self):
        run, out = self.go(NOPE + fields(criteria=CRITERIA) + [""])
        self.assertIsInstance(out, smith.NoValidSlot)
        self.assertIn("criterion 2 needs both text and verify", str(out))
        got = smith._set_aside(run, out.candidate, smith.check_plan, dry=True)
        self.assertIsNotNone(got)
        left, items = got
        self.assertEqual([c["text"] for c in left["criteria"]], ["calc.add adds"])
        self.assertEqual([x["text"] for x in items], ["calc.add keeps zero"])

    def test_a_missing_text_is_asked_alone_too(self):
        run, out = self.go(NOPE + fields(criteria="calc.add adds :: python3 test_calc.py\n :: python3 test_calc.py")
                           + ["calc.add keeps zero"])
        self.assertIsInstance(out, dict, out)
        self.assertEqual(out["criteria"][1]["text"], "calc.add keeps zero")

    def test_a_plan_with_no_sound_criterion_still_ends_the_run_with_the_reason(self):
        run, out = self.go(NOPE + fields(criteria="calc.add adds ::") + [""])
        self.assertIsInstance(out, smith.NoValidSlot)
        self.assertIn("nor field by field", str(out))
        self.assertIsNone(smith._set_aside(run, out.candidate, smith.check_plan, dry=True))

    def test_a_sound_plan_is_asked_nothing_more(self):
        run, out = self.go(NOPE + fields())
        self.assertEqual(len(self.asked), 3 + 5)

    def test_every_criterion_is_asked_for_its_own_verify_at_most_once(self):
        crit = "\n".join(f"thing {i} ::" for i in range(1, 4)) + "\nthing 4 :: python3 test_calc.py"
        run, out = self.go(NOPE + fields(criteria=crit) + ["", "", ""])
        self.assertEqual(len(self.asked), 3 + 5 + 3)
        self.assertIsInstance(out, smith.NoValidSlot)


class Runs(CodeCase):
    def test_a_run_starts_with_the_sound_criterion_and_says_the_other_was_carried_unproven(self):
        # three JSON tries, the five field answers (criterion 2 has no verify), its own question answered with nothing
        replies = [{"text": "nope"}] * 3 + [{"text": x} for x in fields(criteria=CRITERIA)] + [{"text": " "}]
        f = self.serve("openai", *replies, *FIX)
        r = self.code("fix add", "--model", "fake", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("set aside", r.stdout)
        self.assertIn("calc.add keeps zero", r.stdout, "the carried criterion is named at the start")
        with open(self.task()["path"], encoding="utf-8") as fh:
            brief = fh.read()
        self.assertIn("calc.add adds", brief)
        self.assertNotIn("calc.add keeps zero ::", brief)
        evs = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        end = [e for e in evs if e["event"] == "closed"][0]
        self.assertIn("not proven: calc.add keeps zero", end["text"], "never reported done on it")
        self.assertTrue(any(x.startswith("not proven: calc.add keeps zero") for x in end["unproven"]), end["unproven"])

    def test_the_old_ending_stays_for_a_model_that_never_answers_a_plan(self):
        self.serve("openai", *[{"text": "nope"}] * 8)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no valid plan after 2 repairs, nor field by field", r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main()
