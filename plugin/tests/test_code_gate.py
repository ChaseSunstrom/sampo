"""T-4507: sampo code answers the plan gate's Undo: line itself. A plan whose one-way steps on untracked data fm focus
refuses (no snapshot covers them, so no Undo line can be written) is answered by the run: each step the refusal names
gets its own targeted question, the step again narrowed to what the request needs and carrying STEP_LIMIT, however
many steps one refusal names. A plan never ends the run at focus for this gap while the planner can still narrow it."""
from test_code import CLOSE, M_PLAN, CodeCase, plan
from test_smith_small import DONE, FIX_EDIT

from sampo import smith

# two steps that delete the whole repo (a door no snapshot covers) and a third that rewrites it: one refusal names all three
THREE = dict(M_PLAN, steps=["delete ./", "fix calc.add", "wipe ./ and rewrite it"])


class UndoGate(CodeCase):
    def go(self, *script, request="fix add", kind="anthropic"):
        f = self.serve(kind, *script)  # T-4586: no copy of the tree fits, so the run can't supply the Undo line itself
        return f, self.code(request, "--model", "fake", check=False, env={"FM_DOOR_CAP": "1"})

    def test_undo_every_step_one_refusal_names_gets_its_own_question_and_the_task_starts(self):
        f, r = self.go(plan(THREE), plan(THREE), {"text": "check calc.py against the request"},
                       {"text": "read test_calc.py and keep every other file as it is"},
                       FIX_EDIT, DONE, DONE, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Undo: line on step 1", f.said(2), r.stdout + r.stderr)  # step 1's question quotes its gate line
        self.assertIn("Undo: line on step 3", f.said(3), r.stdout + r.stderr)  # then step 3's, one question each
        brief = self.task("T-0003")
        self.assertEqual(brief["status"], "done", r.stdout + r.stderr)
        steps = [s["text"] if isinstance(s, dict) else s for s in brief.get("steps", [])]
        self.assertTrue(all(smith.STEP_LIMIT in s for s in (steps[0], steps[2])), steps)
        self.assertNotIn(smith.STEP_LIMIT, steps[1], "the step the gate did not name is left as the planner wrote it")

    def test_undo_a_refusal_naming_four_steps_is_not_cut_short_by_its_length(self):
        # the focus refusal is read past its first 500 characters: the fourth step's gap is named last, after the others
        four = dict(M_PLAN, steps=["delete ./", "fix calc.add", "wipe ./ and rewrite it", "delete ./ and recreate it"])
        f, r = self.go(plan(four), plan(four), {"text": "check calc.py against the request"},
                       {"text": "read test_calc.py and keep every other file as it is"},
                       {"text": "list the files in the repo and touch none of them"},
                       FIX_EDIT, DONE, DONE, DONE, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Undo: line on step 4", f.said(4), r.stdout + r.stderr)
        self.assertEqual(self.task("T-0003")["status"], "done", r.stdout + r.stderr)
