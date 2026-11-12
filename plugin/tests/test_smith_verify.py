"""T-4362: a plan criterion whose verify command can never pass (ruby given `--name /clamp/`, `test -f /dev/null`, pytest
not installed) no longer ends the run blocked after 3 fix rounds with the work done. At plan time a verify whose first
run fails on its own (exit 126/127, or a failure the model judges to be the command's) goes back to the planner once; in
a fix round a verify that fails with the same output twice gets one repair ask, and the repair replaces the check only
when it fails at the task's base commit (a throwaway worktree) and passes on the work tree. Models advise, proof decides."""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import test_code
from test_code import CodeCase, call, plan

from helpers import PLUGIN

sys.path.insert(0, os.path.join(PLUGIN, "lib"))
from sampo import smith  # noqa: E402

OWN, SAME = "check command's own failure", "keeps failing the same way"  # test_code.ASIDES: the prompts' own words
FIX = test_code.FIX
NOTHING = [call("done", summary="nothing more")] * 6
# the work is right (calc.add adds) but the check expects 3 for 1 + 1: it can never pass, whatever the work does
WRONG = dict(test_code.PLAN, criteria=[{"text": "calc.add adds", "verify":
                                         'python3 -c "import calc; assert calc.add(1, 1) == 3"'}])
RIGHT = 'python3 -c "import calc; assert calc.add(1, 1) == 2"'
LOCAL = {"name": "m", "kind": "openai", "base_url": "http://127.0.0.1:1/v1"}  # a profile whose calls cost nothing


def own(*numbers, why="the command's own usage error"):
    return json.dumps({"own": list(numbers), "why": why})


class Verbs(CodeCase):
    """The run's events and what the task and the repo hold afterwards."""

    def go(self, f, request="fix add", *args):
        r = self.code(request, "--model", "fake", "--json", *args, check=False)
        return r, [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]

    def worktrees(self):
        out = subprocess.run(["git", "-C", self.repo, "worktree", "list", "--porcelain"], capture_output=True,
                             text=True).stdout
        return [ln for ln in out.splitlines() if ln.startswith("worktree ")]


class OwnFailure(Verbs):
    def test_own_failure_exit_127_goes_back_to_the_planner_once_with_its_output_and_asks_no_model(self):
        gone = dict(test_code.PLAN, criteria=[{"text": "calc.add adds", "verify": "./no_such_runner.sh"}])
        f = self.serve("openai", plan(gone), plan(), *FIX)
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("no_such_runner.sh: No such file or directory", f.said(1))  # its output, as it ran
        self.assertIn("fails on its own", f.said(1))
        self.assertEqual(f.asked[OWN], [])  # 127 is a fact: nothing is judged
        self.assertEqual(self.task()["criteria"][0]["verify"], "python3 test_calc.py")

    def test_own_failure_a_failure_the_model_judges_to_be_the_commands_goes_back_with_the_output(self):
        usage = dict(test_code.PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 -m unittest --no-such-flag"}])
        f = self.serve("openai", plan(usage), plan(), *FIX)
        f.asides[OWN] = [own(1)]
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertEqual(len(f.asked[OWN]), 1)
        self.assertIn("unrecognized arguments", json.dumps(f.asked[OWN][0]))  # the judge saw the output, fenced
        self.assertIn("unrecognized arguments: --no-such-flag", f.said(1))  # and so did the planner
        self.assertIn("fails on its own", f.said(1))
        self.assertTrue(any("sent back" in e.get("text", "") for e in ev if e.get("event") == "note"), ev)

    def test_own_failure_a_real_red_of_the_code_under_work_is_left_alone(self):
        f = self.serve("openai", plan(), *FIX)  # python3 test_calc.py fails on the bug: the check is fine
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertEqual(len(f.asked[OWN]), 1)  # judged once for the plan
        self.assertNotIn("fails on its own", f.said(1))
        self.assertIn("calc.add adds", self.task()["criteria"][0]["text"])

    def test_own_failure_a_judgment_that_names_no_check_or_none_that_exists_leaves_the_plan_alone(self):
        for reply in ("maybe, hard to say", own(), own(7), json.dumps({"own": "1"}), json.dumps({"own": [True]})):
            with self.subTest(reply=reply):
                f = self.serve("openai", plan(), *FIX)
                f.asides[OWN] = [reply]
                self.code("fix add", "--model", "fake", check=False)
                self.assertNotIn("fails on its own", f.said(1))
                self.fm("task", "drop", "T-0001", "--reason", "next", check=False)
                self.reset()

    def test_own_failure_only_the_first_plan_is_sent_back(self):
        gone = dict(test_code.PLAN, criteria=[{"text": "calc.add adds", "verify": "./no_such_runner.sh"}])
        f = self.serve("openai", plan(gone), plan(gone), *FIX)
        r, ev = self.go(f)
        self.assertEqual(len([s for s in f.seen if "fails on its own" in json.dumps(s)]), 1)  # asked once
        self.assertEqual(f.asked[OWN], [])

    def reset(self):
        """A second run in the same repo: the first run's task is dropped, its tree put back."""
        subprocess.run(["git", "-C", self.repo, "checkout", "--", "."], capture_output=True)

    def plan_problem(self, verify, profile, **kw):
        run = mock.Mock(plan=dict(test_code.PLAN, criteria=[{"text": "x", "verify": verify}]), root=self.repo,
                        small=False, guided=kw.get("guided", True))
        run.guard.return_value = None
        run.env.return_value = dict(os.environ)
        run.confine.return_value = None
        run.fence.side_effect = lambda s: f"<data-1>\n{s}\n</data-1>"
        run.redact.side_effect = lambda s: s
        return run, smith._plan_problem(run, profile)

    def test_own_failure_a_cloud_or_cli_or_priced_profile_is_asked_nothing_but_exit_127_is_still_sent_back(self):
        for prof in ({"name": "cloud", "kind": "openai", "base_url": "https://api.example.com/v1"},
                     {"name": "claude", "kind": "cli"}, dict(LOCAL, price={"in": 1, "out": 1})):  # T-4362: paid is paid
            with self.subTest(profile=prof["name"]), mock.patch.object(smith, "_ask",
                                                                       side_effect=AssertionError("judged")):
                guided = prof["kind"] == "cli"  # a CLI run can be guided: its kind alone is no judge's
                run, why = self.plan_problem("python3 -m unittest --no-such-flag", prof, guided=guided)
                self.assertIsNone(why)  # not judged: it is left to the fix rounds
                run, why = self.plan_problem("./no_such_runner.sh", prof, guided=guided)
                self.assertIn("fails on its own", why)
                self.assertIn("exit 127", why)

    def test_own_failure_without_a_profile_the_second_pass_and_a_replan_read_no_failure_as_the_commands(self):
        run, why = self.plan_problem("./no_such_runner.sh", None)
        self.assertIsNone(why)

    def test_own_failure_a_failed_judgment_call_leaves_the_plan_alone(self):
        from sampo import models
        with mock.patch.object(smith, "_ask", side_effect=models.ModelError("down")):
            run, why = self.plan_problem("python3 -m unittest --no-such-flag", LOCAL)
        self.assertIsNone(why)

    def test_own_failure_the_output_sent_is_bounded_and_fenced_as_data(self):
        loud = "python3 -c \"import sys; sys.stderr.write('x' * 100000 + 'END'); sys.exit(1)\""
        with mock.patch.object(smith, "_ask", return_value={"text": own(1)}) as ask:
            run, why = self.plan_problem(loud, LOCAL)
        self.assertIn("fails on its own", why)
        self.assertLess(len(why), 3000)
        self.assertIn("<data-1>", why)
        self.assertLess(len(json.dumps(ask.call_args[0][2])), 4000)

    def test_own_failure_a_verify_that_waits_for_its_task_is_neither_run_nor_judged(self):  # T-4371 stays
        run = mock.Mock(plan=dict(test_code.PLAN, criteria=[{"text": "x", "verify": "rm -f data.db && ./nosuch.sh"}]),
                        root=self.repo, small=False, guided=True)
        run.guard.return_value = "Sampo: no active task in this project, so data.db can't be edited yet."
        with mock.patch.object(smith, "run_bounded", return_value=(127, "")) as ran, \
                mock.patch.object(smith, "_ask", side_effect=AssertionError("judged")):
            self.assertIsNone(smith._plan_problem(run, {"name": "m", "kind": "openai"}))
            ran.assert_not_called()


class RepairProven(Verbs):
    def test_repair_proven_the_check_the_planner_corrects_replaces_it_and_the_run_closes(self):
        f = self.serve("openai", plan(WRONG), *FIX, NOTHING[0], test_code.CLOSE)
        f.asides[OWN] = [own(), own(1)]  # the plan's judgment (none), then the fix round's: the check's own failure
        f.asides[SAME] = [json.dumps({"verify": RIGHT})]
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertEqual(self.task()["criteria"][0]["verify"], RIGHT)  # the brief holds the repaired check
        self.assertEqual(len(f.asked[SAME]), 1)  # one ask for the criterion, in a run of fix rounds
        shown = self.fm("task", "show", "T-0001").stdout
        self.assertIn("Smith's own run", shown)  # the red is labelled as the harness's, at the base commit
        self.assertIn("not one made by `sampo task evidence`", shown)
        ask = json.dumps(f.asked[SAME][0])
        self.assertIn("calc.add(1, 1) == 3", ask)  # the verify
        self.assertIn("AssertionError", ask)  # its output
        self.assertIn("calc.py", ask)  # the diff's file names
        log = self.fm("task", "show", "T-0001").stdout
        self.assertIn("repaired", log)  # logged on the task: from, to, and the proof
        self.assertIn("== 3", log)
        self.assertTrue(any("repaired" in e.get("text", "") for e in ev if e.get("event") == "note"), ev)  # in the run
        self.assertEqual(len(self.worktrees()), 1)  # the throwaway worktree is gone
        self.assertEqual([l for l in self.status().splitlines() if "__pycache__" not in l and "calc.py" not in l], [])  # nothing of it left but the work

    def test_repair_proven_the_throwaway_worktree_is_removed_when_the_run_raises_in_it(self):
        base = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        run = mock.Mock(root=self.repo)
        run.env.return_value, run.confine.return_value = dict(os.environ), None
        seen = []

        def boom(cmd, cwd, *a, **k):
            seen.append(cwd)
            self.assertTrue(os.path.isfile(os.path.join(cwd, "calc.py")))  # the base commit's files are there
            raise RuntimeError("killed")

        with mock.patch.object(smith, "run_bounded", boom), self.assertRaises(RuntimeError):
            smith._at_base(run, base, "python3 test_calc.py")
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0]))
        self.assertEqual(len(self.worktrees()), 1)

    def test_repair_proven_a_repair_is_asked_only_after_the_same_output_twice(self):
        # a different failure each round is the work still moving: nothing is asked
        f = self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a * b"), call("done", summary="x"),
                       call("edit", file="calc.py", old="a * b", new="a + b"), call("done", summary="x"))
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertEqual(f.asked[SAME], [])

    def test_repair_proven_a_plan_the_user_wrote_is_never_repaired(self):
        f = self.serve("openai", *NOTHING)
        f.asides[SAME] = [json.dumps({"verify": RIGHT})]
        r, ev = self.go(f, "fix add", "--verify", 'python3 -c "import calc; assert calc.add(1, 1) == 3"')
        self.assertEqual(f.asked[SAME], [])


class RepairRefused(Verbs):
    def refused(self, repair, why, blame=True):
        f = self.serve("openai", plan(WRONG), *FIX, *NOTHING)
        f.asides[OWN] = [own(), own(1)]
        f.asides[SAME] = [json.dumps({"verify": repair})]
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "blocked", r.stdout + r.stderr)
        self.assertEqual(len(f.asked[SAME]), 1)  # asked once: the rounds that follow ask nothing more
        reason = next(e["reason"] for e in ev if e.get("event") == "blocked")
        if blame:  # the check was judged its own failure and no valid repair came
            self.assertIn("suspected wrong", reason)  # the block names the check
        else:  # the repair was bad (the guard refused it, it fails on the work): the check is not blamed for that
            self.assertNotIn("suspected wrong", reason)
            self.assertIn("a repair of it was tried", reason)
        self.assertIn("calc.add(1, 1) == 3", reason)
        self.assertIn("AssertionError", reason)  # and its output
        self.assertIn(why, reason)  # and why the repair was not taken
        self.assertEqual(self.task()["criteria"][0]["verify"], WRONG["criteria"][0]["verify"])
        self.assertEqual(len(self.worktrees()), 1)
        return reason

    def test_repair_refused_one_that_passes_at_the_base_commit_proves_nothing(self):
        self.refused('python3 -c "import calc"', "passes at the task's base commit")

    def test_repair_refused_one_that_fails_on_the_work_tree(self):
        self.refused('python3 -c "import calc; assert calc.add(1, 1) == 4"', "fails on the work", blame=False)

    def test_repair_refused_one_that_cannot_fail_at_all(self):
        self.refused("echo ok", "can't fail")

    def test_repair_refused_one_the_guard_refuses(self):
        self.refused("rm -rf ~", "guard", blame=False)
        self.assertTrue(os.path.exists(os.path.join(self.fake_home, "keep")))

    def test_repair_refused_one_that_fails_at_base_only_because_its_tool_is_not_there_is_no_red(self):
        script = os.path.join(self.repo, "check.sh")  # the work's own, untracked: the base commit has no such file
        with open(script, "w") as fh:
            fh.write('python3 -c "import calc; assert calc.add(1, 1) == 2"\n')
        os.chmod(script, 0o755)
        reason = self.refused("./check.sh", "no red")
        self.assertIn("exit 127", reason)

    def test_repair_refused_a_planner_that_keeps_the_check_leaves_the_block_as_it_was(self):
        f = self.serve("openai", plan(WRONG), *FIX, *NOTHING)  # the default aside: {"keep": true}
        f.asides[OWN] = [own(), own(1)]
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "blocked", r.stdout + r.stderr)
        reason = next(e["reason"] for e in ev if e.get("event") == "blocked")
        self.assertNotIn("suspected wrong", reason)  # the planner said the work is what is wrong
        self.assertIn("still fails after", reason)
        self.assertEqual(len(f.asked[SAME]), 1)

    def test_repair_refused_a_reply_that_is_no_command_is_no_repair(self):
        for reply in ("not json", json.dumps({"verify": ""}), json.dumps({"verify": 3}),
                      json.dumps({"verify": WRONG["criteria"][0]["verify"]}), json.dumps({"verify": "a\nb"})):
            with self.subTest(reply=reply):
                run = mock.Mock(plan=dict(WRONG, criteria=[dict(WRONG["criteria"][0])]), root=self.repo, own_plan=False,
                                tid="T-0001", guided=True)
                run.fence.side_effect = lambda s: s
                run.redact.side_effect = lambda s: s
                with mock.patch.object(smith, "_ask", return_value={"text": reply}), \
                        mock.patch.object(smith, "_at_base", side_effect=AssertionError("proved")):
                    got = smith._repair(run, {"name": "m", "kind": "openai"}, 1,
                                        WRONG["criteria"][0]["verify"], "AssertionError")
                self.assertEqual(got, (False, None, False))  # nothing taken, nothing refused: no suspicion either


class RepairGated(Verbs):
    """A repair is asked only for a check that failed on its own; the judge is a free profile's, the planner's own."""

    def work(self, repair):
        f = self.serve("openai", plan(WRONG), *FIX, *NOTHING)
        f.asides[OWN] = [own(), json.dumps({"own": [], "why": "the assertion is about the work"})]
        f.asides[SAME] = [json.dumps({"verify": repair})]
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "blocked", r.stdout + r.stderr)
        self.assertEqual(f.asked[SAME], [])  # judged the work's failure: no repair is asked
        self.assertEqual(len(f.asked[OWN]), 2)  # the plan's judgment and the fix round's, once each
        self.assertEqual(self.task()["criteria"][0]["verify"], WRONG["criteria"][0]["verify"])  # unchanged
        reason = next(e["reason"] for e in ev if e.get("event") == "blocked")
        self.assertIn("still fails after", reason)  # the fix rounds went on, and the check is not blamed
        self.assertNotIn("suspected wrong", reason)

    def test_repair_gated_a_failure_judged_the_works_asks_the_planner_nothing(self):
        self.work(RIGHT)

    def test_repair_gated_a_grep_style_repair_after_a_judged_work_failure_is_never_taken(self):
        self.work('grep -q "def add" calc.py')  # it would pass: it checks nothing of what the work does

    def test_repair_gated_a_planner_that_is_paid_is_never_judged_though_the_model_is_local(self):
        big = self.serve("openai", plan(WRONG), name="big")  # its calls cost: a loopback with a price
        small = self.serve("openai", *FIX, *NOTHING, name="small")
        path = os.path.join(self.home, "models.json")
        with open(path) as fh:
            conf = json.load(fh)
        conf["profiles"]["big"]["price"] = {"in": 1, "out": 1}
        with open(path, "w") as fh:
            json.dump(conf, fh)
        r = self.code("fix add", "--model", "small", "--planner", "big", "--json", check=False)
        self.assertEqual(self.task()["status"], "blocked", r.stdout + r.stderr)
        for f in (big, small):  # the worker's locality is not where the judge's call goes: nobody is asked
            self.assertEqual(f.asked[OWN], [])
            self.assertEqual(f.asked[SAME], [])


class Resume(Verbs):
    """T-4362: what a run asked and what it repaired is on its journal: a resume goes on from it."""

    def stopped(self, repair, *script):
        """A run that dies (its script ends) just after the second identical failure was handled."""
        f = self.serve("openai", plan(WRONG), *FIX, NOTHING[0], *script)
        f.asides[OWN] = [own(), own(1)]
        f.asides[SAME] = [json.dumps({"verify": repair})]
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "active", r.stdout + r.stderr)  # resumable
        self.assertEqual(len(f.asked[SAME]), 1)
        return f

    def test_resume_after_an_asked_and_refused_repair_asks_nothing_and_keeps_the_blocks_words(self):
        self.stopped("echo ok")
        g = self.serve("openai", *NOTHING)
        r = self.code("--resume", "--model", "fake", "--json", check=False)
        ev = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]
        self.assertEqual((g.asked[SAME], g.asked[OWN]), ([], []))
        self.assertEqual(self.task()["status"], "blocked", r.stdout + r.stderr)
        reason = next(e["reason"] for e in ev if e.get("event") == "blocked")
        self.assertIn("suspected wrong", reason)
        self.assertIn("can't fail", reason)  # why it was refused, from the journal

    def test_resume_after_an_accepted_repair_uses_the_repaired_verify(self):
        f = self.serve("openai", plan(WRONG), *FIX, NOTHING[0], test_code.CLOSE)
        f.asides[OWN] = [own(), own(1)]
        f.asides[SAME] = [json.dumps({"verify": RIGHT})]
        r, ev = self.go(f)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        path = os.path.join(os.path.dirname(os.path.dirname(self.task()["path"])), "code", "T-0001.jsonl")
        with open(path) as fh:
            recs = [json.loads(ln) for ln in fh]
        kinds = [r.get("kind") for r in recs]
        self.assertLess(kinds.index("repair"), kinds.index("repaired"))  # logged before the brief was changed
        cut = max(i for i, k in enumerate(kinds) if k == "repaired") + 1  # a kill right after the repair: no end yet
        journals = os.path.join(self.tmp, "journals")
        os.makedirs(journals)
        with open(os.path.join(journals, "T-0001.jsonl"), "w") as fh:
            fh.write("\n".join(json.dumps(r) for r in recs[:cut]) + "\n")
        run = smith.Run(types.SimpleNamespace(escalate=None, max_steps=None, max_usd=None, planner=None), self.repo,
                        journals)
        smith._load(run, "T-0001")
        self.assertEqual(run.plan["criteria"][0]["verify"], RIGHT)  # the replay of the repaired record
        self.assertEqual(run.repairs["asked"], {1})  # so a resumed run does not ask again
        self.assertTrue(run.repairable)

    def test_resume_of_a_journal_with_no_own_plan_key_is_never_repaired(self):
        self.stopped("echo ok")
        path = os.path.join(os.path.dirname(os.path.dirname(self.task()["path"])), "code", "T-0001.jsonl")
        with open(path) as fh:
            recs = [json.loads(ln) for ln in fh]
        del recs[0]["options"]["own_plan"]  # an older journal: it never said whose plan this was
        recs = [r for r in recs if r.get("kind") not in ("repair", "repair_seen")]  # and knew nothing of repairs
        with open(path, "w") as fh:
            fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
        g = self.serve("openai", *NOTHING)
        self.code("--resume", "--model", "fake", "--json", check=False)
        self.assertEqual((g.asked[SAME], g.asked[OWN]), ([], []))  # fenced as before: no ask


class BaseWorktree(Verbs):
    def test_at_base_a_stale_registered_worktree_is_pruned_first(self):
        base = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        gone = tempfile.mkdtemp(prefix="fm-stale-")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "--detach", "-q", os.path.join(gone, "w"), base],
                       check=True, capture_output=True)
        subprocess.run(["rm", "-rf", os.path.join(gone, "w")], check=True)  # a kill left its entry, the folder is gone
        self.assertEqual(len(self.worktrees()), 2)
        run = mock.Mock(root=self.repo)
        run.env.return_value, run.confine.return_value = dict(os.environ), None
        with mock.patch.object(smith, "run_bounded", return_value=(1, "")):
            smith._at_base(run, base, "true")
        self.assertEqual(len(self.worktrees()), 1)

    def test_at_base_a_timeout_removes_the_worktree_too(self):
        base = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        run = mock.Mock(root=self.repo)
        run.env.return_value, run.confine.return_value = dict(os.environ), None
        seen = []
        with mock.patch.object(smith, "run_bounded", lambda cmd, cwd, *a, **k: (seen.append(cwd), (None, "slow"))[1]):
            got = smith._at_base(run, base, "sleep 99")
        self.assertEqual(got, (None, "slow"))  # a timeout is None, as run_bounded says it
        self.assertFalse(os.path.exists(seen[0]))
        self.assertEqual(len(self.worktrees()), 1)


if __name__ == "__main__":
    unittest.main()
