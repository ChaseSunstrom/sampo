"""T-4626: a repeat sampo focus on the focused task, and a return to a task focused before, keep its recorded base
(its diff and the tests linked to it stay as they were); a first focus still records the base."""
import contextlib
import os
import subprocess
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                           *a], capture_output=True, text=True, check=True).stdout.strip()


class FocusKeepsBase(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        for t in ("Fix the login", "Fix the logout"):
            self.fm("task", "new", t, "--type", "FIX", "--tier", "S", "--ac", "works :: true", "--step", "do it")
        self.p = c.find_project(self.repo)

    def brief(self, tid="T-0001"):
        return c.find_brief(self.p, tid)

    def commit(self, where, name):
        with open(os.path.join(where, name), "w") as f:
            f.write(name + "\n")
        git(where, "add", name)
        git(where, "commit", "-qm", name)

    def lane(self, name="lane1"):
        path = os.path.join(self.tmp, name)
        git(self.repo, "worktree", "add", "-q", "-b", name, path)
        return os.path.realpath(path)

    def test_a_first_focus_records_the_base(self):
        self.assertFalse(self.brief().meta.get("base"))
        self.fm("focus", "T-0001")
        b = self.brief()
        self.assertEqual(b.meta.get("base"), git(self.repo, "rev-parse", "HEAD"))
        self.assertTrue(b.meta.get("base_tree"))

    def test_a_repeat_focus_keeps_the_base_and_says_so(self):
        self.fm("focus", "T-0001")
        before = self.brief().meta
        self.commit(self.repo, "a.txt")
        r = self.fm("focus", "T-0001")
        after = self.brief().meta
        self.assertEqual((after["base"], after["base_tree"]), (before["base"], before["base_tree"]))
        self.assertNotEqual(after["base"], git(self.repo, "rev-parse", "HEAD"))
        self.assertIn("Already in focus", r.stdout)

    def test_a_return_to_a_task_focused_before_keeps_the_base(self):
        self.fm("focus", "T-0001")
        base = self.brief().meta["base"]
        self.commit(self.repo, "a.txt")
        self.fm("focus", "T-0002")
        self.fm("focus", "T-0001")
        self.assertEqual(self.brief().meta["base"], base)

    def test_a_repeat_focus_in_a_lane_keeps_the_base(self):
        lane = self.lane()
        self.fm("focus", "T-0001", cwd=lane)
        before = self.brief().meta
        self.commit(lane, "a.txt")
        self.fm("focus", "T-0001", cwd=lane)
        after = self.brief().meta
        self.assertEqual((after["base"], after["base_tree"]), (before["base"], before["base_tree"]))

    def test_a_lane_return_after_another_task_keeps_the_base(self):
        lane = self.lane()
        self.fm("focus", "T-0001", cwd=lane)
        base = self.brief().meta["base"]
        self.commit(lane, "a.txt")
        self.fm("focus", "T-0002", cwd=lane)
        self.fm("focus", "T-0001", cwd=lane)
        self.assertEqual(self.brief().meta["base"], base)

    def test_a_builder_lane_focused_twice_after_a_main_focus_keeps_its_diff(self):
        self.fm("focus", "T-0001")  # the main thread planned it
        self.fm("focus", "T-0002")
        lane = self.lane()
        os.makedirs(os.path.join(lane, "sub"))
        self.fm("focus", "T-0001", cwd=lane)
        self.commit(lane, "a.txt")
        self.fm("focus", "T-0001", cwd=os.path.join(lane, "sub"))
        b = self.brief()
        self.assertEqual(b.meta.get("lane"), lane)
        r = self.fm("task", "diff", "T-0001", cwd=lane, check=False)
        self.assertIn("a.txt", c._git(lane, "diff", "--name-only", c.task_base(lane, b), c.worktree_tree(lane)), r.stderr)

    def test_a_repeat_focus_after_a_failed_first_snapshot_does_not_snapshot_the_current_files(self):
        """The real loss: a loaded machine fails the first focus's file snapshot; the repeat focus took one of the files
        as they were by then, and base_tree (read before the start commit) made the task's diff empty."""
        self.commit(self.repo, "seed.txt")
        with mock.patch.object(c, "worktree_tree", lambda *a, **k: None), contextlib.chdir(self.repo):
            self.assertEqual(cli.main(["focus", "T-0001"]), 0)
        b = self.brief()
        self.assertTrue(b.meta.get("base"))
        self.assertFalse(b.meta.get("base_tree"))
        self.commit(self.repo, "a.txt")
        self.fm("focus", "T-0001")
        b = self.brief()
        self.assertFalse(b.meta.get("base_tree"), "a repeat focus must not take a start snapshot of the current files")
        self.assertIn("a.txt", c._git(self.repo, "diff", "--name-only", c.task_base(self.repo, b), c.worktree_tree(self.repo)))

    def test_a_focus_that_moves_the_task_between_checkouts_says_it_took_a_new_start(self):
        self.fm("focus", "T-0001")
        base = self.brief().meta["base"]
        self.fm("focus", "T-0002")  # T-0001 is paused: no checkout holds it now
        self.commit(self.repo, "a.txt")
        lane = self.lane()
        before = dict(self.brief().meta)
        r = self.fm("focus", "T-0001", cwd=lane)
        b = self.brief()
        self.assertIn("moved from the main checkout", r.stdout, f"{before} {r.stderr} {b.meta}")
        self.assertNotEqual(b.meta["base"], base)  # a lane doesn't hold the main checkout's work: a new start there
        self.assertIn("moved from the main checkout", r.stdout)
        self.assertIn(base[:9], r.stdout)  # the old start is named, so it can be put back by hand
