"""T-4576: a request to go through existing code and make it safe, ready or correct for a use was judged one named change
(whole false, find_fix false), so no finding pass ran, the plan was one small fix and the run left the rest. Two parts:
the judge's prompt names that case by its meaning (the verdict stays the model's: no word decides it), and a change run
the judge called whole closes with the closing look even when the plan has one criterion, the look being shown what the
run's own closing notes said could still break it, so a defect those notes list becomes the next task, within the look's
round limit. The fake model answers from a script; the real model's verdicts are measured by hand and reported."""
import json
import os

from test_code import M_PLAN, PLAN, CodeCase, call, plan
from test_smith_look import FIRST, GOAL, MUL, MUL_STEPS, SURVEY, Goal, closed, looks, reply
from test_smith_record import sent
from test_smith_small import wants

from sampo import smith

HARDEN = ("Cabinet has only ever run on our office network, and next week we're putting it on the internet so the other "
          "offices and a few clients can use it. Can you go through it and make it safe to put out there? Everything "
          "people use today has to keep working.")


def whole(find_fix=False):
    """A verdict of the whole code: find_fix false here, so the function-by-function review (its own calls) stays out of
    the scripted replies; the routing under test is the closing look's."""
    return {"text": json.dumps({"wants_change": True, "whole": True, "find_fix": find_fix, "why": "make it right"})}


ONE = dict(PLAN, type="FIX", title="Make calc.add add")  # one criterion: one named change to the plan's own fields
LEFT = "calc.mul overflows on large input and nothing refuses it"
NOTED = {"text": json.dumps({"intent": "calc.add adds, as asked", "adversary": LEFT,
                             "docs": "none: internal fix", "lesson": "none: a one-character bug",
                             "why_not_caught": "none: test_calc.py is the test that catches it"})}


class Prompt(CodeCase):
    def test_prompt_names_going_through_the_code_to_make_it_right_as_whole_and_find_fix(self):
        p = smith.WANTS_PROMPT
        self.assertRegex(p, r"(?i)go(?:es|ing)? through")
        self.assertRegex(p, r"(?i)safe, ready")
        self.assertRegex(p, r"(?i)what is wrong has to be found")
        self.assertIn('"whole"', p)
        self.assertIn('"find_fix"', p)

    def test_prompt_still_calls_a_feature_and_a_migration_not_find_fix(self):
        p = smith.WANTS_PROMPT
        self.assertIn("false for building a feature, a migration, a speed-up", p)
        self.assertIn("one named change", p)

    def test_prompt_stays_inside_the_judges_token_room(self):
        self.assertLessEqual(smith.JUDGE_TOKENS, 256)
        self.assertLess(len(smith.WANTS_PROMPT), 2400, "a short prompt: a 27B reads all of it")


class Routing(Goal):
    """A goal run whose plan has ONE criterion: before T-4576 it never looked, whatever the judge said."""

    def one(self, verdict, *script):
        f = self.serve("openai", verdict, *SURVEY, plan(ONE), *FIRST, *script)
        return f, self.code(GOAL, "--model", "fake", "--json", check=False)

    def test_routing_one_criterion_run_judged_whole_gets_the_closing_look(self):
        f, r = self.one(whole(), reply(MUL), *MUL_STEPS, reply())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(f.script, [], "every scripted reply was asked for")
        self.assertEqual(len(looks(f)), 2)
        self.assertEqual(self.task("T-0002")["status"], "done", "the gap the look found is the next task")
        self.assertEqual([x["round"] for x in closed(r)[-1]["looks"]], [1, 2])

    def test_routing_one_criterion_run_not_judged_whole_gets_no_look(self):
        f, r = self.one(wants(True))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(looks(f), [])
        self.assertNotIn("looks", closed(r)[-1])

    def test_routing_a_run_judged_whole_is_bound_by_the_looks_round_limit(self):
        def gap(n):
            return {"gap": f"a flag file is missing ({n})", "file": "calc.py", "check": f"test -f flag{n}.txt"}

        def flag(n):
            return [call("write", file=f"flag{n}.txt", content="x\n"), call("done", summary=f"flag{n}.txt is written")]

        f, r = self.one(whole(), reply(gap(1)), *flag(1), reply(gap(2)), *flag(2), reply(gap(3)), *flag(3))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(looks(f)), smith.LOOKBACK_ROUNDS)
        self.assertIn(f"stopped at the bound of {smith.LOOKBACK_ROUNDS} rounds", closed(r)[-1]["text"])

    def test_routing_no_look_flag_still_wins_over_a_whole_verdict(self):
        f = self.serve("openai", whole(), *SURVEY, plan(ONE), *FIRST)
        r = self.code(GOAL, "--model", "fake", "--json", "--no-look", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(looks(f), [])

    def test_routing_a_whole_verdict_costs_no_second_judge_call(self):
        f, r = self.one(whole(), reply())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        judged = [b for b in f.seen if str(b["messages"][0].get("content", "")).startswith("Judge one request")]
        self.assertEqual(len(judged), 1, "one judged call per run")


class Closing(Goal):
    """The run's own closing review (an M plan's close notes) reaches the look, and what it lists becomes work."""

    def noted(self, *script):
        m = dict(M_PLAN, title="Make calc.add add",
                 criteria=[{"text": "make calc complete as its README promises: calc.add adds",
                            "verify": "python3 test_calc.py"}])
        f = self.serve("openai", whole(), *SURVEY, plan(m), *FIRST, NOTED, *script)
        return f, self.code(GOAL, "--model", "fake", "--json", check=False)

    def test_closing_a_defect_the_close_notes_list_is_shown_to_the_look_and_taken_as_work(self):
        f, r = self.noted(reply(MUL), *MUL_STEPS, reply())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(f.script, [], str([str(b["messages"][-1].get("content", ""))[:100] for b in f.seen[-5:]]))
        (first, second) = looks(f)
        self.assertIn(LEFT, sent(f, first), "the run's own closing notes ride the look")
        self.assertEqual(self.task("T-0002")["status"], "done")

    def test_closing_the_notes_ride_only_as_fenced_data(self):
        f, r = self.noted(reply())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (i,) = looks(f)
        user = sent(f, i)
        self.assertNotIn(LEFT, f.seen[i]["messages"][0]["content"], "never in the instructions")
        at = user.index(LEFT)
        self.assertLess(user.rfind("<data-", 0, at), at)
        self.assertGreater(user.index("</data-", at), at, "inside an envelope")

    def test_closing_no_notes_no_section(self):
        f, r = self.one_plain()
        (i,) = looks(f)
        self.assertNotIn("closing notes", sent(f, i))

    def one_plain(self):
        f = self.serve("openai", whole(), *SURVEY, plan(ONE), *FIRST, reply())
        return f, self.code(GOAL, "--model", "fake", "--json", check=False)
