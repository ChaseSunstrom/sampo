"""T-4291: a guided run that runs out of turns while it is still progressing carries on in steps, and a run that
repeats itself or changes nothing stops at the cap as before. Every model reply is scripted turn by turn."""
import types
import unittest

from test_code import call
from test_smith_budget import SRC_EDIT, VERIFY, Budget, looks

from sampo import smith  # noqa: E402  (test_code puts plugin/lib on the path)


def writes(n, start=0):
    """n writes to one file, each with content not written before: each one is a change to the tree."""
    return [call("write", file="work.txt", content=f"draft {start + i}") for i in range(n)]


def back_and_forth(n):
    """n writes that flip one file between two contents: after the first two, every write returns to a seen state."""
    return [call("write", file="work.txt", content="A" if i % 2 == 0 else "B") for i in range(n)]


class Progressing(Budget):
    def test_progressing_writes_that_keep_changing_carry_past_the_cap_with_a_note(self):
        # the plan is turn 1; eight new drafts (turns 2-9) pass the cap of 6; the edit and the verify close the step
        f, r = self.go(*writes(8), SRC_EDIT, VERIFY, extra=("--max-steps", "6", "--deadline", "600"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertEqual(out.count("carrying on"), 1,"one carry: the run closes at turn 11, inside the new cap of 26")
        self.assertIn("6 -> 26", out, "the note names the cap before and after")
        self.assertIn("file changed", out, "the note says why the run carries on")
        self.assertIn("s left", out, "the note says how much deadline is left")
        self.assertEqual(self.stats(r)["carried"], 1)
        self.assertEqual(self.task()["status"], "done")
        self.assertIn("return a + b", self.calc())

    def test_progressing_a_run_that_keeps_changing_carries_again_and_closes_before_the_next_cap(self):
        # sixty drafts (turns 2-61) pass the first cap at 6 and the second at 26 and the third at 46; the edit at 62
        # and the verify at 63 close the step before the fourth cap at 66
        f, r = self.go(*writes(60), SRC_EDIT, VERIFY, extra=("--max-steps", "6", "--deadline", "600"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertEqual(self.stats(r)["carried"], 3)
        self.assertIn("26 -> 46", out)
        self.assertIn("46 -> 66", out)
        self.assertEqual(self.task()["status"], "done")

    def test_progressing_a_run_past_the_fourth_carry_stops_at_the_limit_and_says_carries(self):
        # one hundred drafts: four carries take the cap to 86, and the run stops there with the carries named. The
        # history of 86 turns is over 2000 estimated tokens, so each reply reports the prompt it read (in: 50000) as a
        # server does; the fake's default of 5 would read as a truncated prompt
        big = [dict(s, **{"in": 50000}) for s in writes(100)]
        f, r = self.go(*big, SRC_EDIT, VERIFY, extra=("--max-steps", "6"))
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertEqual(out.count("carrying on"), 4, "CARRY_MAX is four")
        self.assertEqual(self.stats(r)["carried"], 4)
        self.assertIn("used all 86 turns", out)
        self.assertIn("carries", out, "the stop names the carries used up")

    def test_progressing_an_edit_between_runs_of_the_same_red_test_is_no_repeat(self):
        # review of T-4291: edit, test, edit, test: the test is red each time with the same count, but the file changed
        # between its runs, so the run is working, not repeating itself
        turns = [x for i in range(4) for x in (*writes(1, start=i), call("run", cmd="python3 test_calc.py"))]
        f, r = self.go(*turns, SRC_EDIT, VERIFY, extra=("--max-steps", "6", "--deadline", "600"))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("repeated", out)
        self.assertEqual(self.stats(r)["carried"], 1)
        self.assertEqual(self.task()["status"], "done")

    def test_progressing_with_the_deadline_nearly_spent_the_run_stops_and_says_deadline(self):
        # 60 s of deadline is under the 90 s needed to carry on: the cap holds and the stop says the deadline did
        f, r = self.go(*writes(8), SRC_EDIT, VERIFY, extra=("--max-steps", "6", "--deadline", "60"))
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertNotIn("carrying on", out)
        self.assertEqual(self.stats(r)["carried"], 0)
        self.assertIn("deadline", out)


class Stalled(Budget):
    def test_stalled_looks_only_stop_at_the_cap_and_say_no_file_changed(self):
        f, r = self.go(*looks(8), extra=("--max-steps", "6"))
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertNotIn("carrying on", out)
        self.assertEqual(self.stats(r)["carried"], 0)
        self.assertIn("no file changed", out, "the stop says why: nothing changed in the last 12 turns")

    def test_stalled_one_action_repeated_three_times_stops_at_the_cap_and_says_repeated(self):
        # two new drafts make the window progressing, then the same look three times: the repeat overrides progress
        f, r = self.go(*writes(2), *[call("run", cmd="echo same")] * 8, extra=("--max-steps", "6"))
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertNotIn("carrying on", out)
        self.assertEqual(self.stats(r)["carried"], 0)
        self.assertIn("repeated", out, "the stop says the same action repeated")

    def test_stalled_a_back_and_forth_edit_carries_once_then_stops_as_repeated(self):
        # turns 2 and 3 are new contents (progress); the flips after them return to seen contents, so the window
        # at turn 26 holds only repeats and the second cap holds
        f, r = self.go(*back_and_forth(40), extra=("--max-steps", "6"))
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertEqual(out.count("carrying on"), 1, "once: the flips after the first carry are not progress")
        self.assertEqual(self.stats(r)["carried"], 1)
        self.assertIn("repeated", out)


class StepFinished(unittest.TestCase):
    """T-4471: hard exam, cascade run: the worker did three steps' work in one, closed each of the next three on a
    look, and was stopped at its cap as 'no file changed' with eighteen minutes left. A finished step is movement."""

    def run_at(self, turns, phase_turn):
        looks_ = [{"turn": t, "key": f"read:f{t}", "moved": None, "space": None, "state": None}
                  for t in range(turns - smith.PROGRESS_WINDOW + 1, turns + 1)]
        return types.SimpleNamespace(window=looks_, turns=turns, ends=None, carries=0, phase_turn=phase_turn)

    def test_step_finished_inside_the_window_carries_on(self):
        self.assertEqual(smith.Run.carry_check(self.run_at(140, 135)), (True, "a step finished"))

    def test_step_finished_before_the_window_or_never_still_stops_as_no_change(self):
        for at in (140 - smith.PROGRESS_WINDOW, None):
            ok, why = smith.Run.carry_check(self.run_at(140, at))
            self.assertFalse(ok)
            self.assertIn("no file changed", why)

    def test_step_finished_does_not_excuse_one_action_repeated(self):
        run = self.run_at(140, 139)
        for r in run.window[-3:]:
            r["key"] = "read:same"
        ok, why = smith.Run.carry_check(run)
        self.assertFalse(ok)
        self.assertIn("repeated", why)
