"""T-4586: a Smith run never dead-ends at fm focus for a missing Undo: line. The refusal is answered where Smith calls
focus: a one-way step with no way back gets the Undo from the run's own copy of the tree (kept under the project's
door-snapshots, verified byte for byte, bounded by the same cap focus uses) and focus is asked again; when no copy can be
kept the refusal goes to the planner once, as feedback, and only a second refusal ends the run, with the reason shown."""
import os
import shlex

from test_code import CLOSE, M_PLAN, CodeCase, plan
from test_smith_small import DONE, FIX_EDIT

# a step that deletes the repo itself: a door focus's own snapshot never covers (it names the repo and everything above it)
HARD = dict(M_PLAN, steps=["delete ./", "fix calc.add"])
NO_COPY = {"FM_DOOR_CAP": "1"}  # one byte: no copy of the tree fits, so the run can't supply an Undo line


class FocusRefusal(CodeCase):
    def go(self, *script, env=None, request="fix add"):
        f = self.serve("anthropic", *script)
        return f, self.code(request, "--model", "fake", check=False, env=env)

    def steps(self, tid="T-0001"):
        return [s["text"] if isinstance(s, dict) else s for s in self.task(tid).get("steps", [])]

    def asked_about_undo(self, f):
        return [i for i, s in enumerate(f.seen) if "Undo: line on step" in str(s["messages"][-1])]

    def test_a_missing_undo_is_supplied_from_the_runs_own_tree_copy_and_the_task_starts(self):
        f, r = self.go(plan(HARD), FIX_EDIT, DONE, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task("T-0001")["status"], "done", r.stdout + r.stderr)
        self.assertEqual(self.asked_about_undo(f), [], "the planner is not asked: the run had what the gate wanted")
        step = self.steps()[0]
        self.assertIn("Undo: cp -a", step)
        copy = shlex.split(step.split("Undo:", 1)[1])[2]  # cp -a <copy>/. <repo>/
        self.assertTrue(os.path.isfile(os.path.join(copy[:-2], "calc.py")), f"the copy named in the Undo holds the tree: {copy}")
        self.assertNotIn("Undo:", self.steps()[1], "the step the gate did not name is left as the planner wrote it")
        self.assertIn("kept a copy of the tree", r.stdout)

    def test_no_copy_to_keep_sends_the_refusal_to_the_planner_once_and_the_new_plan_starts(self):
        f, r = self.go(plan(HARD), plan(M_PLAN), FIX_EDIT, DONE, CLOSE, env=NO_COPY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.asked_about_undo(f), [1], "the second call carries the refusal, once")
        self.assertEqual([self.task(t)["status"] for t in ("T-0001", "T-0002")], ["dropped", "done"], r.stdout)

    def test_a_second_refusal_ends_the_run_with_the_reason_in_the_reply(self):
        f, r = self.go(plan(HARD), plan(HARD), {"text": "delete ./"}, env=NO_COPY)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Undo: line on step 1", r.stdout + r.stderr, "the refusal's own words end the run, not a bare error")
        self.assertIn("refused", r.stdout + r.stderr)

    def test_a_tree_with_a_name_the_copy_would_expand_is_not_copied_half(self):
        # the copy is made by name, and a shell would read $ in a name: such a tree gets no copy, never a partial one
        with open(os.path.join(self.repo, "a$HOME.txt"), "w") as fh:
            fh.write("x\n")
        f, r = self.go(plan(HARD), plan(M_PLAN), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.asked_about_undo(f), [1])
        self.assertEqual(self.task("T-0001")["status"], "dropped")
