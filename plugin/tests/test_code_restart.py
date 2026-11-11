"""T-4204: a run that made no model turn leaves no journal (a pause, a load gate or a first call that fails: the next
start is not refused with "an earlier fm code run didn't finish"); --restart sets an unfinished journal aside, kept
beside the new one; and a resume after "used all N turns" gets N new turns, not an instant stop."""
import json
import os
import re

from test_code import FIX, CodeCase, call, plan


def code_dir(case, tid):
    return os.path.join(os.path.dirname(os.path.dirname(case.task(tid)["path"])), "code")


class NoTurn(CodeCase):
    def brief(self):
        a = ["task", "new", "make calc.add add", "--type", "FIX", "--tier", "S", "--json", "--step", "fix calc.add",
             "--ac", "calc.add adds :: python3 test_calc.py"]
        return json.loads(self.fm(*a).stdout)["id"]

    def test_a_run_refused_before_its_first_model_turn_leaves_no_journal(self):
        tid = self.brief()
        self.serve("openai")  # an empty script: the first model call is refused, as a paused or timed-out one is
        r = self.code("--task", tid, "--model", "fake", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(code_dir(self, tid), f"{tid}.jsonl")), r.stdout + r.stderr)

    def test_the_next_start_after_such_a_refusal_is_not_refused(self):
        tid = self.brief()
        self.serve("openai")
        self.code("--task", tid, "--model", "fake", check=False)
        self.serve("openai", *FIX)
        r = self.code("--task", tid, "--model", "fake", check=False)
        self.assertNotIn("didn't finish", r.stderr)
        self.assertEqual(self.task(tid)["status"], "done", r.stdout + r.stderr)

    def test_restart_sets_an_unfinished_journal_aside_and_starts_fresh(self):
        tid = self.brief()
        jd = code_dir(self, tid)
        os.makedirs(jd, exist_ok=True)
        old = os.path.join(jd, f"{tid}.jsonl")
        with open(old, "w") as f:
            f.write(json.dumps({"kind": "start", "task": tid}) + "\n")
        self.serve("openai", *FIX)
        r = self.code("--task", tid, "--model", "fake", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("didn't finish", r.stderr, "without --restart the unfinished run still holds the task")
        r = self.code("--task", tid, "--model", "fake", "--restart", check=False)
        self.assertEqual(self.task(tid)["status"], "done", r.stdout + r.stderr)
        kept = [n for n in os.listdir(jd) if re.fullmatch(rf"{re.escape(tid)}\.\d+\.jsonl", n)]
        self.assertEqual(len(kept), 1, os.listdir(jd))
        with open(os.path.join(jd, kept[0])) as f:
            self.assertEqual(json.loads(f.readline())["kind"], "start", "the set-aside run is kept as it was")
        r = self.code("--resume", check=False)  # the set-aside run is no run to resume
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing to resume", r.stderr)


class Turns(CodeCase):
    def test_a_resume_after_used_all_turns_gets_its_turns_again(self):
        self.serve("openai", plan(), call("read", path="calc.py"), call("read", path="calc.py"))
        r = self.code("fix add", "--model", "fake", "--no-commit", "--max-steps", "2", check=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("used all 2 turns", r.stdout + r.stderr)
        self.serve("openai", *FIX)
        r = self.code("--resume", check=False)  # no --max-steps: the journal's 2 would stop it at once
        self.assertNotIn("used all", r.stdout + r.stderr, "a resume gets its own turns")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
