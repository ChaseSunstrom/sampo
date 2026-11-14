"""T-4614: a sampo code run that ends without closing (out of time, or any other stop) with the keep check red where it
was green puts the files it changed back to the newest state at which the keep check was green (the start when it never
was after an edit), keeps the undone work as a patch the final message names and says how to reapply, and says so when
the check could not be run to its end. A run whose keep check is green at its end, one with no keep check, and one that
finishes with done are left as they are. On exam run all14-04f8, 4 of 14 runs ended out of time and left 5 kept checks
broken. The unit cases drive a Run's own methods (a real keep check on a real git tree); the end-to-end ones script the
model with test_code's fake."""
import glob
import json
import os
import types

from test_code import PLAN, CodeCase, call, plan

from sampo import smith

SEED = ("seed with a green suite", (
    ("calc.py", "def add(a, b):\n    return a - b\n"),
    ("test_calc.py", "import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n    def test_zero(self):\n"
                     "        self.assertEqual(calc.add(1, 0), 1)\n")))
# the keep check of the unit cases: green while add(1, 0) is 1; slow (past any bound) while a file named slow is there
CHECK = ("import os, sys, time\nimport calc\nif os.path.exists('slow'):\n    time.sleep(30)\n"
         "sys.exit(0 if calc.add(1, 0) == 1 else 1)\n")
ADD, MUL = "def add(a, b):\n    return a + b\n", "def add(a, b):\n    return a * b\n"  # green, red


def events(r):
    return [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]


class Stopped(CodeCase):
    """A Run whose keep check is on, driven by hand: edits through its own touch, checks through its own keep_run."""
    repo_seed = SEED

    def setUp(self):
        super().setUp()
        self.put("chk.py", CHECK)
        self.run_ = run = smith.Run(types.SimpleNamespace(), self.repo, os.path.join(self.tmp, "journal"))
        os.makedirs(run.jdir)
        run.tid, run.keep_cmd, run.keep_state, run.keep_why = "T-0001", "python3 chk.py", "green", "chk"

    def put(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def edit(self, text, name="calc.py"):
        self.run_.touch(name)  # what the edit and write tools do first
        self.put(name, text)
        self.run_.edited = True

    def patches(self):
        return glob.glob(os.path.join(self.run_.jdir, "T-0001.dropped*.patch"))

    def test_green_at_the_start_and_broken_by_the_work_puts_the_start_back_and_names_the_patch(self):
        self.edit(MUL)
        lost = self.run_.unwind()
        self.assertEqual(self.calc(), "def add(a, b):\n    return a - b\n")
        self.assertEqual(lost["restored"], ["calc.py"])
        patch = self.patches()
        self.assertEqual(len(patch), 1)
        self.assertEqual(lost["patch"], patch[0])
        self.assertIn("+    return a * b", open(patch[0]).read())
        self.assertIn(patch[0], lost["text"], "the message names the patch")
        self.assertIn(f"git apply {patch[0]}", lost["text"], "and how to reapply it")
        self.assertIn("keep check", lost["text"])

    def test_green_after_a_first_edit_and_broken_by_a_second_gives_the_tree_after_the_first(self):
        self.edit(ADD)
        self.assertEqual(self.run_.keep_run("step")[0], "green")  # the first edit's check: a snapshot of this tree
        self.edit(MUL)
        lost = self.run_.unwind()
        self.assertEqual(self.calc(), ADD, "the newest green state, not the start")
        self.assertEqual(lost["restored"], ["calc.py"])
        self.assertIn("+    return a * b", open(lost["patch"]).read())
        self.assertIn("-    return a + b", open(lost["patch"]).read())

    def test_a_file_first_changed_after_the_last_green_goes_back_to_what_it_was_at_the_start(self):
        self.edit(ADD)
        self.run_.keep_run("step")
        self.put("helper.py", "X = 1\n")  # not yet touched at the check: absent then
        self.run_.touch("helper.py")
        self.put("helper.py", "X = 2\n")
        self.edit(MUL)
        lost = self.run_.unwind()
        self.assertEqual(self.calc(), ADD)
        self.assertEqual(sorted(lost["restored"]), ["calc.py", "helper.py"])
        self.assertEqual(open(os.path.join(self.repo, "helper.py")).read(), "X = 1\n")

    def test_a_check_still_green_at_the_end_keeps_the_partial_work(self):
        self.edit(ADD)  # green, and never checked before the stop: the stop's own check finds it green
        self.assertEqual(self.run_.unwind(), {})
        self.assertEqual(self.calc(), ADD)
        self.assertEqual(self.patches(), [])

    def test_no_keep_check_leaves_the_tree_as_the_last_edit_made_it(self):
        self.run_.keep_cmd = self.run_.keep_state = None
        self.edit(MUL)
        self.assertEqual(self.run_.unwind(), {})
        self.assertEqual(self.calc(), MUL)
        self.assertEqual(self.patches(), [])

    def test_a_stop_after_every_step_ended_is_restored_too_when_the_check_is_red(self):
        # out of time in the verify's fix rounds: the steps are done, the tree is red
        self.edit(ADD)
        self.run_.keep_run("step")
        self.edit(MUL)
        self.run_.steps_done = True
        lost = self.run_.unwind()
        self.assertEqual(self.calc(), ADD)
        self.assertEqual(lost["restored"], ["calc.py"])

    def test_a_stop_after_every_step_ended_with_the_check_green_changes_nothing(self):
        self.edit(ADD)
        self.run_.steps_done = True
        self.assertEqual(self.run_.unwind(), {})
        self.assertEqual(self.calc(), ADD)

    def test_a_check_that_cannot_finish_at_the_stop_puts_back_what_was_edited_after_the_last_green(self):
        self.edit(ADD)
        self.run_.keep_run("step")
        self.edit(ADD + "\n\ndef sub(a, b):\n    return a - b\n")  # still the right answer, but unchecked
        self.put("slow", "")
        old, smith.KEEP_STOP_TIMEOUT = smith.KEEP_STOP_TIMEOUT, 1
        self.addCleanup(setattr, smith, "KEEP_STOP_TIMEOUT", old)
        lost = self.run_.unwind()
        self.assertEqual(self.calc(), ADD, "the newest tree the check passed on")
        self.assertEqual(lost["restored"], ["calc.py"])
        self.assertIn("could not be run to its end", lost["text"])
        self.assertIn("def sub", open(lost["patch"]).read())

    def test_a_check_that_cannot_finish_with_nothing_edited_since_the_last_green_restores_nothing(self):
        self.edit(ADD)
        self.run_.keep_run("step")
        self.put("slow", "")
        self.assertEqual(self.run_.unwind(), {}, "the tree is the one the check passed on: it is not run at all")
        self.assertEqual(self.calc(), ADD)

    def test_a_run_that_ended_its_drive_is_never_restored_by_the_stop(self):
        self.edit(MUL)
        self.run_.finished = True  # what cmd_code sets when _drive returns: its verify and close have judged the tree
        self.assertEqual(self.run_.unwind(), {})
        self.assertEqual(self.calc(), MUL)
        self.assertEqual(self.patches(), [])

    def test_the_stop_is_once_a_run(self):
        self.edit(MUL)
        first = self.run_.unwind()
        self.edit(MUL)  # whatever happens after, a second unwind returns the first's answer and touches nothing
        self.assertIs(self.run_.unwind(), first)
        self.assertEqual(len(self.patches()), 1)


class OutOfTime(CodeCase):
    """The run as a person starts it: a deadline that ends it with the work half made."""
    repo_seed = SEED
    P = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 -c 'import calc; assert calc.add(2, 3) == 5'"}])

    def run_script(self, *script, steps=None):
        self.serve("openai", plan(steps or self.P), *script)
        return self.code("fix add", "--model", "fake", "--json", "--no-commit", "--deadline", "6", check=False)

    def patches(self):
        return glob.glob(os.path.join(os.path.dirname(os.path.dirname(self.task("T-0001")["path"])), "code",
                                      "T-0001.dropped*.patch"))

    def test_out_of_time_with_the_keep_check_red_puts_the_tree_back_and_names_the_patch(self):
        r = self.run_script(call("edit", file="calc.py", old="a - b", new="a * b"), *[call("run", cmd="sleep 1")] * 12)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("out of time", r.stdout + r.stderr)
        self.assertIn("a - b", self.calc(), "back where the keep check was green")
        patch = self.patches()
        self.assertEqual(len(patch), 1, r.stdout + r.stderr)
        self.assertIn("+    return a * b", open(patch[0]).read())
        self.assertIn(patch[0], r.stderr)
        self.assertIn("git apply", r.stderr)
        end = [e for e in events(r) if e["event"] == "error"][-1]
        self.assertEqual(end["restored"], ["calc.py"])
        self.assertEqual(end["patch"], patch[0])

    def test_out_of_time_with_the_keep_check_green_keeps_the_partial_work(self):
        r = self.run_script(call("edit", file="calc.py", old="a - b", new="a + b"), *[call("run", cmd="sleep 1")] * 12)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("out of time", r.stdout + r.stderr)
        self.assertIn("a + b", self.calc())
        self.assertEqual(self.patches(), [])

    def test_a_run_that_finishes_with_done_is_left_as_it_ends(self):
        r = self.run_script(call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="fixed"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a + b", self.calc())
        self.assertEqual(self.patches(), [])
