"""T-4386: a plan with sound criteria and one whose verify is still refused after the repairs, the field ladder and the
re-plan (exam s8-049 goal-c-ab86e2: `... ; exit 0` can't fail) starts with the sound ones. The refused one is not met
by anything: it is carried as an unproven part, which the worker is told of and the run's end says by name. A plan with
no sound criterion, and a plan the user wrote, end as before. Every model reply is scripted."""
import json
import os
import subprocess
import sys
from unittest import mock

from helpers import FM
from sampo import smith
from test_code import CLOSE, FIX, HANG, M_PLAN, PLAN, CodeCase, call, journal_text, plan
from test_smith_budget import SRC_EDIT, VERIFY
from test_smith_small import DONE, FIX_EDIT

SOUND = {"text": "calc.add adds", "verify": "python3 test_calc.py"}
BAD = {"text": "nothing outside the repo is touched", "verify": "rm -rf ~"}  # the guard refuses it
CANT_FAIL = {"text": "the run is done", "verify": "python3 test_calc.py; exit 0"}  # check_plan: it can't fail
MIXED = dict(PLAN, criteria=[SOUND, BAD])


def records(case, kind, tid="T-0001"):
    return [x for x in (json.loads(l) for l in journal_text(case, tid).splitlines()) if x.get("kind") == kind]


class Unproven(CodeCase):
    def test_unproven_starts_with_the_sound_criteria_and_the_journal_names_the_one_left_out(self):
        self.serve("openai", plan(MIXED), plan(MIXED), *FIX)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        brief = open(self.task()["path"], encoding="utf-8").read()
        self.assertIn("calc.add adds", brief)
        self.assertNotIn("nothing outside the repo is touched", brief, "the refused criterion is no criterion of the task")
        self.assertNotIn("rm -rf ~", brief)
        self.assertTrue(os.path.exists(os.path.join(self.fake_home, "keep")), "the refused verify never ran")
        got = records(self, "unproven")
        self.assertEqual(len(got), 1, "one journal record")
        self.assertEqual(got[0]["items"][0]["text"], BAD["text"])
        self.assertIn("refused", got[0]["items"][0]["why"])
        self.assertIn("criterion 2", got[0]["items"][0]["why"], "the gate's reason names it")
        self.assertIn("set aside", r.stdout, "the output says it when it happens")

    def test_unproven_starts_with_the_sound_criteria_after_the_field_ladder(self):
        # the exam shape: three bad JSON replies and the field ladder end in a plan whose second verify can't fail
        ladder = [{"text": "nope"}] * 3 + [{"text": "Type: FIX."}, {"text": "S"}, {"text": "Make calc.add add"},
                                          {"text": f"- {SOUND['text']} :: `{SOUND['verify']}`\n"
                                                   f"- {CANT_FAIL['text']} :: `{CANT_FAIL['verify']}`"},
                                          {"text": "1. fix calc.add"}]
        look = call("done", summary="calc.add subtracts (calc.py:2): a - b should be a + b")  # the look before the plan
        self.serve("openai", look, *ladder, SRC_EDIT, VERIFY)
        r = self.code("fix add", "--model", "fake", "--small", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        brief = open(self.task()["path"], encoding="utf-8").read()
        self.assertNotIn("exit 0;", brief.replace("exit 0\n", ""), "the can't-fail verify is no criterion")
        self.assertNotIn(CANT_FAIL["text"], brief)
        got = records(self, "unproven")
        self.assertEqual([x["text"] for x in got[0]["items"]], [CANT_FAIL["text"]])
        self.assertIn("can't fail", got[0]["items"][0]["why"])

    def test_unproven_said_to_the_worker_and_at_the_end_of_the_run(self):
        f = self.serve("openai", plan(MIXED), plan(MIXED), *FIX)
        r = self.code("fix add", "--model", "fake", "--json", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        step = next(x for x in f.seen if "Now do step 1" in json.dumps(x["messages"]))
        sent = json.dumps(step["messages"])
        self.assertIn("with no check", sent)
        self.assertIn("nothing verifies it", sent)
        self.assertIn(BAD["text"], sent)
        self.assertNotIn("rm -rf ~", sent, "the refused command is not handed to the worker")
        evs = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
        end = [e for e in evs if e["event"] == "closed"][0]
        self.assertIn("not proven: ", end["text"])
        self.assertIn(BAD["text"], end["text"])
        self.assertTrue(any(BAD["text"] in x and x.startswith("not proven: ") for x in end["unproven"]), end["unproven"])

    def test_unproven_a_plan_with_no_refusal_says_nothing_of_it(self):
        f = self.serve("openai", plan(), *FIX)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertNotIn("not proven", r.stdout + r.stderr)
        self.assertNotIn("no check", json.dumps(f.seen))
        self.assertEqual(records(self, "unproven"), [])

    def test_unproven_all_refused_ends_with_the_gates_reason_and_makes_no_task(self):
        bad = dict(PLAN, criteria=[BAD, dict(BAD, text="home is whole", verify="rm -rf ~/x")])
        self.serve("openai", plan(bad), plan(bad))
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused by Smith's guard", r.stdout + r.stderr)
        self.assertNotIn("set aside", r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.fake_home, "keep")))
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0)

    def test_unproven_own_plan_untrimmed_a_verify_the_user_gave_is_never_set_aside(self):
        f = self.serve("openai")
        r = self.code("fix add", "--model", "fake", "--verify", "rm -rf ~", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--verify", r.stdout + r.stderr)
        self.assertNotIn("set aside", r.stdout + r.stderr)
        self.assertEqual(f.seen, [], "no model asked")

    def test_unproven_own_plan_untrimmed_in_the_trimmer_itself(self):
        from sampo import smith
        run = mock.Mock(own_plan=True, root=self.repo, small=False)
        self.assertIsNone(smith._set_aside(run, MIXED, smith.check_plan))
        run.own_plan = False  # a plan whose every criterion is refused has nothing sound to start with
        run.guard.side_effect = lambda tool, args: "Ward: blocked: outside" if "rm" in args["command"] else None
        self.assertIsNone(smith._set_aside(run, dict(PLAN, criteria=[BAD, BAD]), smith.check_plan))

    def test_unproven_a_resumed_run_still_tells_the_worker_and_the_end(self):
        first =self.serve("openai", plan(MIXED), plan(MIXED), call("read", path="calc.py"), HANG)
        p = subprocess.Popen([sys.executable, FM, "code", "fix add", "--model", "fake"], cwd=self.repo, env=self.env(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(first.hung.wait(30), "fm code never reached the hanging request")
            p.kill()
        finally:
            p.communicate(timeout=30)
        second = self.serve("openai", *FIX)
        r = self.code("--resume", "--model", "fake", "--json")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("nothing verifies it", json.dumps(second.seen[0]["messages"]), "the resumed prompt carries it")
        evs = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
        self.assertIn("not proven: ", [e for e in evs if e["event"] == "closed"][0]["text"])


ASK = "fix add, and keep the tests as they are"  # the gate counts it one clause and no criterion of the M plan checks it
CLAUSE = ASK
HMM = [{"text": "hmm"}, {"text": "nope"}]  # the two replies to the mapping question that fill nothing


class ClauseUnproven(CodeCase):
    """The same rule at the other door: the plan gate (fm focus) can tie a clause of the request to no criterion even
    after the mapping question and the re-plan (exam s8-051: three of twelve goal runs ended with nothing done)."""

    def go(self, *script, request=ASK, args=("--json",)):
        f = self.serve("anthropic", *script)
        return f, self.code(request, "--model", "fake", *args, check=False)

    def test_clause_refusal_starts_unproven_with_the_clause_named_and_said_at_the_end(self):
        f, r = self.go(plan(M_PLAN), *HMM, plan(M_PLAN), *HMM, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([self.task(t)["status"] for t in ("T-0001", "T-0002", "T-0003")], ["dropped", "dropped", "done"])
        with open(self.task("T-0003")["path"], encoding="utf-8") as fh:
            brief = fh.read()
        self.assertIn(f"- {smith._unproven_line(CLAUSE)}", brief, "the Non-goals line the gate reads")
        self.assertNotIn("none of this is to be made", brief, "the part is to be done, only unproven")
        self.assertEqual(len(f.seen), 6 + 3, "no model call for the way out")
        got = records(self, "unproven", "T-0003")
        self.assertEqual([x["text"] for x in got[0]["items"]], [CLAUSE])
        self.assertIn("plan gate", got[0]["items"][0]["why"])
        sent = json.dumps(f.seen[6:])
        self.assertIn(CLAUSE, sent)
        self.assertIn("nothing verifies it", sent, "the worker is told")
        evs = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
        end = [e for e in evs if e["event"] == "closed"][0]
        self.assertIn(f"not proven: {CLAUSE}", end["text"])
        self.assertTrue(any(x.startswith("not proven: ") and CLAUSE in x for x in end["unproven"]), end["unproven"])
        self.assertTrue(any("set aside" in e.get("text", "") and CLAUSE in e.get("text", "") for e in evs), "said when it happens")

    def test_clause_refusal_starts_unproven_with_every_clause_the_gate_names(self):
        two = "Get these helpers to a finished state I could release; decide what that takes."
        helpers = dict(M_PLAN, title="Bring the helpers in app.py to a release-ready state and cover them in test_app.py",
                       criteria=[{"text": "app.py helpers are complete with no TODO/stub/placeholder behavior left",
                                  "verify": "! grep -q 'a - b' calc.py"},
                                 {"text": "Every helper in app.py has a passing test in test_app.py",
                                  "verify": "python3 test_calc.py"}])
        f, r = self.go(plan(helpers), *HMM, plan(helpers), *HMM, FIX_EDIT, DONE, CLOSE, request=two)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = [x["text"] for x in records(self, "unproven", "T-0003")[0]["items"]]
        self.assertEqual(len(got), 2, got)
        with open(self.task("T-0003")["path"], encoding="utf-8") as fh:
            brief = fh.read()
        for x in got:
            self.assertIn(smith._unproven_line(x), brief)
        evs = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
        self.assertEqual(sum(x.startswith("not proven: ") for x in [e for e in evs if e["event"] == "closed"][0]["unproven"]), 2)

    def test_clause_refusal_after_the_one_try_ends_as_before(self):
        run = mock.Mock(own_plan=False, plan_msgs=[{"role": "user", "content": "x"}], unproven=[], clause_aside=True)
        run.plan = dict(M_PLAN)
        why = f'fm focus refused the plan: a criterion for "{CLAUSE}" (sampo task ac T-0001 add ...)'
        with self.assertRaises(smith.CodeError):
            smith._replan(run, mock.Mock(), "T-0002", why, 0, 1)
        run.fm.assert_not_called()
        run.emit.assert_not_called()

    def test_clause_refusal_a_plan_without_a_criterion_or_a_clause_ends_as_before(self):
        why = f'fm focus refused the plan: a criterion for "{CLAUSE}" (sampo task ac T-0001 add ...)'
        for plan_, text in ((dict(M_PLAN, criteria=[]), why), (dict(M_PLAN), "fm focus refused the plan: a step"),
                            (dict(M_PLAN, tier="S"), why)):
            run = mock.Mock(own_plan=False, unproven=[], clause_aside=False)
            self.assertFalse(smith._clause_aside(run, text, plan_), text)
            self.assertEqual(run.unproven, [])

    def test_clause_refusal_own_plan_untrimmed_in_the_door_itself(self):
        run = mock.Mock(own_plan=True, plan_msgs=[{"role": "user", "content": "x"}], unproven=[], unproven_lines=[])
        run.plan = dict(M_PLAN)
        why = f'fm focus refused the plan: a criterion for "{CLAUSE}" (sampo task ac T-0001 add ...)'
        for repaired in (2, 1, 0):
            with self.assertRaises(smith.CodeError):
                smith._replan(run, mock.Mock(), "T-0001", why, repaired, 1)
        self.assertEqual((run.unproven, run.unproven_lines), ([], []))
        run.fm.assert_not_called()
