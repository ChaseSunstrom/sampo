"""T-4571: a plan whose only flaw is a verify written over several lines (a python3 -c whose program is on the lines
inside the quotes: exam cascade11l, conc-work-queue, three full M plans refused for it, the run ended with no edit) is
kept. The harness writes the command, as written, to a check script in the run's own folder (never in the worked tree)
and the criterion's verify becomes one line that runs it. The guard, the can't-fail rule and the other plan checks read
the script's own text. A criterion still flawed after that is set aside alone; a plan with none left still ends."""
import argparse
import json
import os
import shlex
import subprocess
import sys

import test_code
from test_code import CodeCase, M_PLAN, PLAN, call, journal_text, plan

from helpers import PLUGIN

sys.path.insert(0, os.path.join(PLUGIN, "lib"))
from sampo import smith  # noqa: E402

PROGRAM = "python3 -c \"\nimport calc\nassert calc.add(2, 3) == 5, calc.add(2, 3)\nprint('ok')\n\""
OTHER = "python3 -c \"\nimport calc\nassert calc.add(2, 1) == 3\n\""
SHAPE = dict(M_PLAN, criteria=[  # the 2026-11-13 plan: readings, options, steps, three criteria, the first over lines
    {"text": "calc.add adds two numbers", "verify": PROGRAM},
    {"text": "calc.add keeps zero", "verify": OTHER},
    {"text": "the project's own test passes", "verify": "python3 test_calc.py"}])
FIX = test_code.FIX


class Plans(CodeCase):
    def run_(self, tmp_name="code"):
        run = smith.Run(argparse.Namespace(), self.repo, os.path.join(self.tmp, tmp_name))
        run.guard = lambda tool, args: None
        return run

    def test_a_multi_line_verify_is_stored_as_a_script_and_called_in_one_line(self):
        run = self.run_()
        got, note = smith.fit_plan(run, SHAPE)
        first = got["criteria"][0]["verify"]
        self.assertNotIn("\n", first)
        self.assertEqual(got["criteria"][2]["verify"], "python3 test_calc.py", "a one-line verify stays as it is")
        words = shlex.split(first)
        self.assertEqual(words[0], "bash")
        self.assertTrue(words[1].startswith(os.path.join(run.jdir, "checks") + os.sep), words)
        with open(words[1], encoding="utf-8") as f:
            self.assertIn(PROGRAM, f.read(), "the command is stored as it was written, lines and indentation kept")
        self.assertIn("2 multi-line verify", note)
        self.assertIsNone(smith.check_plan(got), "the plan that was refused for its line breaks stands")

    def test_the_stored_check_runs_red_before_and_green_after_as_written(self):
        run = self.run_()
        got, _ = smith.fit_plan(run, SHAPE)
        run.plan = got
        self.assertIsNone(smith._plan_problem(run), "red on the buggy tree: the plan problem is none")
        call_ = got["criteria"][0]["verify"]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        red = subprocess.run(["bash", "-c", call_], cwd=self.repo, capture_output=True, text=True, env=env)
        self.assertNotEqual(red.returncode, 0, "the check fails on the bug")
        self.assertIn("AssertionError", red.stderr)
        with open(os.path.join(self.repo, "calc.py"), "w") as f:
            # a different size from the buggy file: a .pyc left by the plan check's own run in the same second is keyed
            # on mtime (whole seconds) and size, and would serve the bug again
            f.write("def add(a, b):\n    return a + b  # fixed\n")
        green = subprocess.run(["bash", "-c", call_], cwd=self.repo, capture_output=True, text=True, env=env)
        self.assertEqual(green.returncode, 0, green.stderr)
        self.assertIn("ok", green.stdout)

    def test_the_script_is_not_in_the_worked_tree_and_the_diff_stays_clean(self):
        run = self.run_()
        got, _ = smith.fit_plan(run, SHAPE)
        self.assertEqual(self.status(), "")
        path = shlex.split(got["criteria"][0]["verify"])[1]
        self.assertFalse(os.path.realpath(path).startswith(os.path.realpath(self.repo) + os.sep))

    def test_the_same_plan_twice_makes_one_script_and_a_plan_without_lines_is_unchanged(self):
        run = self.run_()
        a, _ = smith.fit_plan(run, SHAPE)
        b, _ = smith.fit_plan(run, SHAPE)
        self.assertEqual(a, b)
        self.assertEqual(len(os.listdir(os.path.join(run.jdir, "checks"))), 2)
        same, note = smith.fit_plan(run, PLAN)
        self.assertIs(same, PLAN)
        self.assertIsNone(note)

    def test_a_script_whose_text_is_a_cant_fail_command_is_still_refused(self):
        run = self.run_()
        quiet = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 test_calc.py\necho done"}])
        got, _ = smith.fit_plan(run, quiet)
        self.assertNotIn("\n", got["criteria"][0]["verify"])
        self.assertIn("can't fail", smith.check_plan(got, "fix") or "")
        self.assertIsNone(smith.check_plan(got), "outside small mode the rule is not asked, as before")
        exits = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 test_calc.py\nexit 0"}])
        self.assertIn("can't fail", smith.check_plan(smith.fit_plan(run, exits)[0], "fix") or "")

    def test_a_script_whose_text_the_guard_refuses_is_refused_and_never_run(self):
        run = self.run_()
        run.guard = lambda tool, args: "Ward: blocked: outside" if "rm -rf" in args["command"] else None
        bad = dict(PLAN, criteria=[{"text": "home is whole", "verify": "python3 test_calc.py\nrm -rf ~/x"}])
        run.plan, _ = smith.fit_plan(run, bad)
        why = smith._plan_problem(run)
        self.assertIn("refused by Smith's guard", why)
        self.assertEqual(why.criterion, 1)
        self.assertTrue(os.path.exists(os.path.join(self.fake_home, "keep")))

    def test_a_script_with_a_shell_syntax_error_is_named_by_its_own_text(self):
        run = self.run_()
        bad = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 -c \"\nimport calc\n"}])
        got, _ = smith.fit_plan(run, bad)
        run.plan = got
        why = smith._plan_problem(run)
        self.assertIn("not a complete shell command", why)
        self.assertIn("import calc", why, "the model is shown its own lines, not the path of a script")

    def test_a_file_named_like_a_check_script_with_other_content_is_not_read_as_one(self):
        os.makedirs(os.path.join(self.tmp, "checks"))
        fake = os.path.join(self.tmp, "checks", "0123456789ab.sh")
        with open(fake, "w") as f:
            f.write("echo planted\n")
        self.assertIsNone(smith.script_body(f"bash {fake}"))
        self.assertIsNone(smith.script_body("bash /nowhere/checks/0123456789ab.sh"))
        self.assertIsNone(smith.script_body("python3 test_calc.py"))

    def test_a_criterion_still_flawed_after_the_repairs_is_set_aside_alone(self):
        run = self.run_()
        worse = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 test_calc.py"},
                                     {"text": "line one\nline two", "verify": "python3 test_calc.py"}])
        fitted, _ = smith.fit_plan(run, worse)
        self.assertIn("one line each", smith.check_plan(fitted))
        got = smith._set_aside(run, fitted, smith.check_plan, dry=True)
        self.assertIsNotNone(got)
        left, items = got
        self.assertEqual([c["text"] for c in left["criteria"]], ["calc.add adds"])
        self.assertEqual(len(items), 1)
        self.assertIn("criterion 2", items[0]["why"])

    def test_no_criterion_left_still_ends_as_before(self):
        run = self.run_()
        lone = dict(PLAN, criteria=[{"text": "line one\nline two", "verify": "python3 test_calc.py"}])
        fitted, _ = smith.fit_plan(run, lone)
        self.assertIsNone(smith._set_aside(run, fitted, smith.check_plan, dry=True))

    def test_the_user_s_own_plan_is_not_rewritten(self):
        run = self.run_()
        run.own_plan = True
        self.assertEqual(smith.fit_plan(run, SHAPE), (SHAPE, None), "a task the user wrote is as written")

    def test_a_huge_verify_is_left_for_the_check_to_refuse(self):
        run = self.run_()
        huge = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "echo x\n" * 5000}])
        got, note = smith.fit_plan(run, huge)
        self.assertIs(got, huge)
        self.assertIn("one line each", smith.check_plan(got))
        self.assertFalse(os.path.exists(os.path.join(run.jdir, "checks")))

    def test_an_unwritable_folder_leaves_the_plan_as_it_was(self):
        with open(os.path.join(self.tmp, "blocked"), "w") as f:
            f.write("a file where the folder should be")
        run = smith.Run(argparse.Namespace(), self.repo, os.path.join(self.tmp, "blocked"))
        got, note = smith.fit_plan(run, SHAPE)
        self.assertIs(got, SHAPE)
        self.assertIsNone(note)


class Runs(CodeCase):
    def changes(self):
        """What git sees of the worked tree besides python's own bytecode (the verify imports calc)."""
        return [x for x in self.status().splitlines() if "__pycache__" not in x]

    def test_the_2026_11_13_plan_shape_runs_to_a_closed_task_with_a_clean_tree(self):
        f = self.serve("anthropic", plan(SHAPE), *FIX, test_code.CLOSE)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertNotIn("not a valid plan", json.dumps(f.seen))
        brief = open(self.task()["path"], encoding="utf-8").read()
        self.assertIn("calc.add adds two numbers", brief)
        line = next(x for x in brief.splitlines() if "calc.add adds two numbers" in x)
        self.assertIn("verify with `bash ", line)
        self.assertEqual(self.changes(), [], "the run's own check script is not the model's change")
        script = shlex.split(line.split("verify with `", 1)[1].rstrip("`"))[1]
        self.assertTrue(os.path.isfile(script))
        self.assertFalse(os.path.realpath(script).startswith(os.path.realpath(self.repo) + os.sep))
        green = subprocess.run(["bash", script], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(green.returncode, 0, "green on the fixed tree: " + green.stderr)

    def test_the_field_by_field_path_keeps_a_multi_line_verify_too(self):
        ladder = [{"text": "nope"}] * 3 + [{"text": "Type: FIX."}, {"text": "S"}, {"text": "Make calc.add add"},
                                          {"text": f"- calc.add adds :: `{PROGRAM}`"}, {"text": "1. fix calc.add"}]
        look = call("done", summary="calc.add subtracts (calc.py:2): a - b should be a + b")  # the look before the plan
        self.serve("openai", look, *ladder, *FIX)
        r = self.code("fix add", "--model", "fake", "--small", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout[-3000:] + r.stderr)
        brief = open(self.task()["path"], encoding="utf-8").read()
        self.assertIn("verify with `bash ", brief)
        self.assertNotIn("set aside", r.stdout, "nothing was left out: the check stands")
        self.assertEqual(self.changes(), [])

    def test_one_criterion_with_a_flaw_no_script_mends_is_set_aside_while_the_plan_starts(self):
        mixed = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 test_calc.py"},
                                     {"text": "home is whole", "verify": "python3 test_calc.py\nrm -rf ~"}])
        self.serve("openai", plan(mixed), plan(mixed), *FIX)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("set aside", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.fake_home, "keep")), "the refused script never ran")
        brief = open(self.task()["path"], encoding="utf-8").read()
        self.assertIn("calc.add adds", brief)
        self.assertNotIn("home is whole", brief)
        self.assertNotIn("rm -rf", brief)
        got = [x for x in journal_text(self).splitlines() if '"unproven"' in x]
        self.assertEqual(len(got), 1)
