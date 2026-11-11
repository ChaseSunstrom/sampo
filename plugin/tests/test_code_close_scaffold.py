"""T-2937: a verified task isn't refused at finish for a Decision, Measurements or Regression test the model never
wrote: the close asks for each missing one in its own slot, with a worked example (fakes only)."""
import json
import os
import unittest

from test_code import CLOSE, M_PLAN, PLAN, CodeCase, call, plan
from helpers import read_text

EDIT = (call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
GAPS = {"decision": "Recommendation: change the operator; would change if: callers want a difference",
        "numbers": "add calls per second: 9 → 14 (timeit over 500 runs)",
        "regression": "test_add.py failed before the fix and passes now"}


def reply(**keys):
    return {"text": json.dumps(keys)}


class CloseGaps(CodeCase):
    def closes(self, p, *script):
        asked = [{"text": json.dumps({"wants_change": False, "why": "research asks for findings"})}] \
            if p.get("type") == "RESEARCH" else []  # T-4284: a RESEARCH plan's request is judged once
        f = self.serve("anthropic", plan(p), *asked, *EDIT, *script)
        r = self.code("research add", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        return f

    def test_research_s_sets_the_decision_before_finish(self):
        f = self.closes(dict(PLAN, type="RESEARCH"), reply(decision=GAPS["decision"]))
        self.assertIn("would change if: callers", read_text(self.task()["path"]))
        self.assertEqual(len(f.seen), 5)  # plan, the judgment, edit, done, the one gap slot: no M close notes for S

    def test_research_m_asks_close_notes_then_the_decision(self):
        f = self.closes(dict(M_PLAN, type="RESEARCH"), CLOSE, reply(decision=GAPS["decision"]))
        self.assertIn("would change if: callers", read_text(self.task()["path"]))
        self.assertEqual(len(f.seen), 6)

    def test_performance_numbers_and_fix_regression_ride_the_same_slot(self):
        self.closes(dict(PLAN, type="PERFORMANCE"), reply(numbers=GAPS["numbers"]))
        self.assertIn("9 → 14", read_text(self.task()["path"]))

    def test_a_copied_example_is_asked_again(self):  # audit: the example is a claim the model never made
        from sampo import smith
        copied = smith.GAP_FIELDS[1][1].split("(for example: ", 1)[1][:-1]
        f = self.closes(dict(PLAN, type="PERFORMANCE"), reply(numbers=copied), reply(numbers=GAPS["numbers"]))
        self.assertIn("copies the example", f.said(4))
        self.assertIn("9 → 14", read_text(self.task()["path"]))

    def test_a_fix_with_a_red_run_asks_nothing_more(self):
        f = self.closes(PLAN)
        self.assertEqual(len(f.seen), 3)

    def test_a_resumed_run_restores_its_gap_notes_and_does_not_ask_again(self):  # T-2948, like close_notes
        from test_code import Resume
        Resume.kill_mid_run(self, dict(PLAN, type="RESEARCH"))
        jr = os.path.join(os.path.dirname(os.path.dirname(self.task()["path"])), "code", "T-0001.jsonl")
        with open(jr, "a") as f:
            f.write(json.dumps({"kind": "gap_notes", "notes": {"decision": GAPS["decision"]}}) + "\n")
        f = self.serve("openai", *EDIT)  # no gap reply scripted: asking again would find the script empty
        r = self.code("--resume", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("would change if: callers", read_text(self.task()["path"]))
        self.assertEqual(len(f.seen), 2)

    def test_the_prompt_carries_a_worked_example_for_each_key(self):
        from sampo import smith
        for key, q, *_ in smith.CLOSE_FIELDS + smith.GAP_FIELDS:
            self.assertIn("for example", q, key)
        for key in smith.CLOSE_KEYS:
            self.assertIn(f'"{key}"', smith.CLOSE_PROMPT)
        self.assertIn("For example", smith.CLOSE_PROMPT)

    def test_the_gap_prompt_sent_shows_the_example(self):
        f = self.closes(dict(PLAN, type="RESEARCH"), reply(decision=GAPS["decision"]))
        self.assertIn("Recommendation:", f.said(4))
        self.assertIn("For example", f.said(4))


if __name__ == "__main__":
    unittest.main()
