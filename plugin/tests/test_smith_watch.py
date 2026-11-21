"""T-4301: a step reworded for the plan gate after a one-way refusal (smith._piece, STEP_LIMIT, T-4282) is watched while
it runs. The words carry the limit and Smith's guard meets each command, but a reworded step with no one-way word and a
deleting command the guard lets through would still lose untracked data (smith-stack review, finding 3). So the plan marks
such a step (a flag on the plan, journaled, never a match on the step's text); before it the harness lists the untracked
files (bounded: FM_WATCH_MAX files, FM_WATCH_HASH bytes each), after each command of the step and at its end it compares,
and a file that is gone or changed stops the run blocked with the file named, the file put back when it was copied.
Every model reply is scripted."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts the plugin's lib on the path)
from test_code import CLOSE, M_PLAN, CodeCase, call, journal_text, plan
from test_smith_small import DONE, FIX_EDIT

from sampo import smith

HARD = dict(M_PLAN, steps=["delete ./", "fix calc.add"])  # step 1 is one-way on untracked data: the gate refuses it
REWORD = {"text": "read calc.py and leave every file where it is"}  # the planner's answer: no one-way word in it
NOTES = "my notes: keep these\n"
PROMISE = "which it promised to leave"


class WatchBase(CodeCase):
    NO_COPY = {"FM_DOOR_CAP": "1"}  # no copy of the tree fits, so the gate's refusal goes to the planner

    def put(self, rel, text=NOTES):
        full = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        return full

    def go(self, *step_one, files=("notes.txt",), env=None, reword=True):
        """A run whose step 1 the gate refused and the planner reworded (REWORD); step_one is the model's turns in it."""
        for rel in files:
            self.put(rel)
        script = [plan(HARD), plan(HARD), REWORD, *step_one] if reword else [plan(M_PLAN), *step_one]
        f = self.serve("anthropic", *script)
        r = self.code("fix add", "--model", "fake", check=False, env={**self.NO_COPY, **(env or {})})
        return f, r

    def exists(self, rel):
        return os.path.exists(os.path.join(self.repo, rel))

    def read(self, rel):
        with open(os.path.join(self.repo, rel)) as f:
            return f.read()

    def records(self, kind, tid="T-0003"):
        return [r for r in map(json.loads, journal_text(self, tid).splitlines()) if r.get("kind") == kind]

    def last_task(self):
        return "T-0003"


class Watch(WatchBase):
    # -- AC 1: a loss stops the run, names the file, puts it back, and the run does not end done ---------------------
    def test_loses_a_deleted_untracked_file_stops_the_run_names_it_and_puts_it_back(self):
        f, r = self.go(call("run", cmd="rm notes.txt"), FIX_EDIT, DONE, DONE, CLOSE)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("step 1 changed untracked notes.txt, " + PROMISE, out)
        self.assertIn("put back", out)
        self.assertTrue(self.exists("notes.txt"), "the file is back")
        self.assertEqual(self.read("notes.txt"), NOTES)
        self.assertEqual(self.task(self.last_task())["status"], "blocked", out)
        self.assertEqual(self.records("end")[-1]["status"], "blocked")
        self.assertIn("a - b", self.calc(), "the run stopped there: the later edit never ran")

    def test_loses_a_changed_untracked_file_stops_the_run_and_the_old_bytes_come_back(self):
        f, r = self.go(call("run", cmd="echo changed > notes.txt"), FIX_EDIT, DONE, DONE, CLOSE)
        out = r.stdout + r.stderr
        self.assertIn("step 1 changed untracked notes.txt, " + PROMISE, out)
        self.assertEqual(self.read("notes.txt"), NOTES)
        self.assertEqual(self.task(self.last_task())["status"], "blocked", out)

    def test_loses_a_file_in_a_folder_the_step_removed_is_put_back_in_its_folder(self):
        f, r = self.go(call("run", cmd="rm -r data"), FIX_EDIT, DONE, DONE, CLOSE, files=("data/a/in.txt", "data/b.txt"))
        out = r.stdout + r.stderr
        self.assertIn(PROMISE, out)
        self.assertEqual(self.read("data/a/in.txt"), NOTES, out)
        self.assertEqual(self.read("data/b.txt"), NOTES, out)
        self.assertEqual(self.task(self.last_task())["status"], "blocked", out)

    def test_loses_a_file_the_edit_tool_rewrites_is_caught_at_the_step_end(self):
        f, r = self.go(call("write", file="notes.txt", content="overwritten\n"), DONE, DONE, CLOSE)
        out = r.stdout + r.stderr
        self.assertIn("step 1 changed untracked notes.txt, " + PROMISE, out)
        self.assertEqual(self.read("notes.txt"), NOTES)
        self.assertNotEqual(self.task(self.last_task())["status"], "done", out)

    def test_loses_a_file_too_big_to_copy_is_named_and_the_stop_says_it_stays_gone(self):
        self.put("big.bin", "x" * 200)
        f, r = self.go(call("run", cmd="rm big.bin"), FIX_EDIT, DONE, DONE, CLOSE, files=(), env={"FM_WATCH_HASH": "64"})
        out = r.stdout + r.stderr
        self.assertIn("step 1 changed untracked big.bin, " + PROMISE, out)
        self.assertFalse(self.exists("big.bin"))
        self.assertIn("no copy", out, "it says what it could not put back")
        self.assertNotEqual(self.task(self.last_task())["status"], "done", out)

    def test_loses_names_a_second_file_the_one_step_lost(self):
        f, r = self.go(call("run", cmd="rm a.txt b.txt c.txt"), DONE, files=("a.txt", "b.txt", "c.txt"))
        out = r.stdout + r.stderr
        self.assertIn("step 1 changed untracked a.txt", out)
        self.assertIn("2 more", out)
        self.assertTrue(all(self.exists(x) for x in ("a.txt", "b.txt", "c.txt")))

    # -- AC 2: no false alarm; the bound is said; no flag, no watch --------------------------------------------------
    def test_creates_a_file_the_step_makes_or_removes_again_is_no_loss(self):
        f, r = self.go(call("run", cmd="echo new > made.txt"), call("run", cmd="echo t > tmp.txt"),
                       call("run", cmd="rm tmp.txt"), FIX_EDIT, DONE, DONE, CLOSE)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn(PROMISE, out)
        self.assertEqual(self.task(self.last_task())["status"], "done", out)
        self.assertTrue(self.exists("made.txt"))
        self.assertEqual(self.read("notes.txt"), NOTES)

    def test_creates_a_command_that_only_reads_the_files_changes_nothing(self):
        f, r = self.go(call("run", cmd="cat notes.txt"), FIX_EDIT, DONE, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task(self.last_task())["status"], "done")
        self.assertTrue([x for x in self.records("watch_before") if x["step"] == 1], "the watch is journaled")

    def test_bound_more_untracked_files_than_the_limit_runs_with_the_count_said_and_the_first_ones_watched(self):
        names = [f"f{i}.txt" for i in range(5)]
        f, r = self.go(call("run", cmd="rm f4.txt"), FIX_EDIT, DONE, DONE, CLOSE, files=names, env={"FM_WATCH_MAX": "3"})
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("5 untracked files, 3 watched", out)
        self.assertFalse(self.exists("f4.txt"), "beyond the bound: not watched, not put back")
        row = self.records("watch_before")[0]
        self.assertEqual((row["step"], row["total"], row["watched"]), (1, 5, 3))

    def test_bound_a_file_inside_the_limit_is_still_watched(self):
        names = [f"f{i}.txt" for i in range(5)]
        f, r = self.go(call("run", cmd="rm f0.txt"), DONE, files=names, env={"FM_WATCH_MAX": "3"})
        self.assertIn("step 1 changed untracked f0.txt, " + PROMISE, r.stdout + r.stderr)
        self.assertTrue(self.exists("f0.txt"))

    def test_unflagged_a_step_the_gate_never_refused_is_not_watched(self):
        f, r = self.go(call("run", cmd="rm notes.txt"), FIX_EDIT, DONE, CLOSE, reword=False)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(self.task("T-0001")["status"], "done", out)
        self.assertNotIn(PROMISE, out)
        self.assertFalse(self.exists("notes.txt"))
        kinds = {json.loads(x).get("kind") for x in journal_text(self, "T-0001").splitlines()}
        self.assertFalse({"watch", "watch_before"} & kinds, "no cost: no listing, no record")

    def test_unflagged_the_second_step_of_a_reworded_plan_is_not_watched(self):
        f, r = self.go(FIX_EDIT, DONE, call("run", cmd="rm notes.txt"), DONE, CLOSE)  # step 1 fixes calc; step 2 deletes
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn(PROMISE, out)
        self.assertEqual([x["step"] for x in self.records("watch_before")], [1])

    # -- the flag: carried on the plan, journaled, not read from the step's words -------------------------------------
    def test_flag_is_journaled_for_the_reworded_step_only(self):
        f, r = self.go(FIX_EDIT, DONE, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([x["steps"] for x in self.records("watch")], [[1]])

    def test_flag_a_planners_own_step_text_with_the_limit_words_is_not_flagged(self):
        said = dict(M_PLAN, steps=[f"fix calc.add ({smith.STEP_LIMIT})"])
        self.put("notes.txt")
        self.serve("anthropic", plan(said), call("run", cmd="rm notes.txt"), FIX_EDIT, DONE, CLOSE)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertNotIn(PROMISE, r.stdout + r.stderr)
        self.assertEqual(self.task("T-0001")["status"], "done", r.stdout + r.stderr)


class WatchReview(WatchBase):
    """T-4301 review fixes: a parent folder swapped for a file, a step that raises, ignored data, the bytes read."""

    def test_enotdir_a_watched_folder_replaced_by_a_file_is_a_loss(self):
        f, r = self.go(call("run", cmd="rm -rf data && echo x > data"), DONE, files=("data/a/in.txt",))
        out = r.stdout + r.stderr
        self.assertIn(PROMISE, out)
        self.assertNotEqual(self.task(self.last_task())["status"], "done", out)

    def test_raise_a_model_error_after_a_write_still_puts_the_file_back(self):
        f, r = self.go(call("write", file="notes.txt", content="overwritten\n"))
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read("notes.txt"), NOTES, r.stdout + r.stderr)

    def test_raise_a_resumed_run_replays_the_watch_and_catches_the_loss(self):
        f, r = self.go(call("write", file="notes.txt", content="overwritten\n"))
        self.assertNotEqual(r.returncode, 0)
        self.serve("anthropic", call("run", cmd="rm notes.txt"), DONE)
        r = self.code("--resume", "--model", "fake", check=False, env=self.NO_COPY)
        out = r.stdout + r.stderr
        self.assertIn(PROMISE, out)
        self.assertEqual(self.read("notes.txt"), NOTES, out)

    def test_ignored_local_data_is_watched(self):
        self.put(".gitignore", "*.db\n")
        f, r = self.go(call("run", cmd="rm app.db"), DONE, files=("app.db",))
        out = r.stdout + r.stderr
        self.assertIn("step 1 changed untracked app.db, " + PROMISE, out)
        self.assertEqual(self.read("app.db"), NOTES)

    def test_ignored_rebuilt_folders_are_not_watched(self):
        self.put(".gitignore", "build/\n")
        f, r = self.go(call("run", cmd="rm -r build"), FIX_EDIT, DONE, DONE, CLOSE, files=("build/x.txt",))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(PROMISE, r.stdout + r.stderr)

    def test_ignored_build_output_the_step_rewrites_is_no_loss(self):
        self.put(".gitignore", "*.log\n.coverage\n")
        f, r = self.go(call("run", cmd="echo more >> run.log; echo x > .coverage"), FIX_EDIT, DONE, DONE, CLOSE,
                       files=("run.log", ".coverage"))
        self.assertNotIn(PROMISE, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class WatchUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def put(self, rel, text="data\n"):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)

    def test_put_back_refuses_a_parent_that_resolves_outside_the_root(self):
        self.put("d/a.txt")
        w = smith._Watch(self.root, 1).begin()
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        shutil.rmtree(os.path.join(self.root, "d"))
        os.symlink(outside, os.path.join(self.root, "d"))
        self.assertEqual(w.lost(), [("d/a.txt", "gone")])
        self.assertFalse(w.put_back("d/a.txt"))
        self.assertEqual(os.listdir(outside), [])
        w.close()

    def test_put_back_refuses_a_folder_in_the_way(self):
        self.put("a.txt")
        w = smith._Watch(self.root, 1).begin()
        os.unlink(os.path.join(self.root, "a.txt"))
        os.makedirs(os.path.join(self.root, "a.txt"))
        self.assertFalse(w.put_back("a.txt"))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "a.txt")))
        w.close()

    def test_close_removes_the_scratch_folder(self):
        self.put("a.txt")
        w = smith._Watch(self.root, 1).begin()
        tmp = w.tmp
        self.assertTrue(os.path.isdir(tmp))
        w.close()
        self.assertFalse(os.path.exists(tmp))
        w.close()

    def test_a_git_that_fails_without_timing_out_watches_nothing_and_says_so(self):
        self.put("a.txt")
        shutil.rmtree(os.path.join(self.root, ".git"))
        w = smith._Watch(self.root, 1).begin()
        self.assertEqual((w.total, w.files), (-1, {}))
        w.close()

    def test_ignored_files_are_watched_only_when_they_are_local_data(self):
        self.put(".gitignore", "*.log\n*.db\n*.sqlite\n")
        for n in ("a.log", "app.db", "x.sqlite"):
            self.put(n)
        w = smith._Watch(self.root, 1).begin()
        self.assertEqual(sorted(k for k in w.files if k != ".gitignore"), ["app.db", "x.sqlite"])
        w.close()

    def test_a_listing_failure_watches_nothing(self):
        self.put("a.txt")
        with mock.patch.object(smith, "_git", side_effect=subprocess.TimeoutExpired("git", 1)):
            w = smith._Watch(self.root, 1).begin()
        self.assertEqual((w.total, w.files), (-1, {}))
        self.assertEqual(w.lost(), [])
        w.close()

    def test_the_copy_budget_bounds_the_copies_and_the_bytes_read(self):
        self.put("a.txt", "x" * 100)
        self.put("b.txt", "y" * 100)
        reads = []
        real = smith._watch_probe
        with mock.patch.object(smith, "WATCH_BYTES", 150), \
                mock.patch.object(smith, "_watch_probe", side_effect=lambda *a: (reads.append(a), real(*a))[1]):
            w = smith._Watch(self.root, 1).begin()
        self.assertEqual(w.copied, 1)
        self.assertEqual(w.files["b.txt"][3], None, "past the budget: size and mtime only")
        w.close()

    def test_a_symlink_is_watched_by_its_target_and_a_fifo_by_size(self):
        self.put("t.txt")
        os.symlink("t.txt", os.path.join(self.root, "l"))
        w = smith._Watch(self.root, 1).begin()
        self.assertEqual(w.files["l"][0], "l")
        os.unlink(os.path.join(self.root, "l"))
        os.symlink("other", os.path.join(self.root, "l"))
        self.assertIn(("l", "changed"), w.lost())
        w.close()
