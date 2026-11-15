"""T-4707: a builder's own `git commit` in its lane fails on a stale index.lock in the lane's git folder
(.git/worktrees/<lane>/index.lock) and nothing cleared it: the lane verbs clear locks (T-4616) only when the main thread
runs them. The failure hook now clears it, with the same check (older than the bar, no live git for that folder), when the
failed command's own error says `index.lock': File exists` in a linked worktree; the builder's next commit works. A fresh
lock, a lock a live git holds, a failure that does not name a lock, a main checkout's own lock and a plain (non-lane)
repo are left."""
import os
import shutil
import subprocess
import time

from helpers import ForemanTestCase


class LaneStaleLock(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.lane = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/lane", self.lane], check=True)
        self.procs = []
        with open(os.path.join(self.lane, "x.txt"), "w") as f:
            f.write("x\n")
        subprocess.run(["git", "-C", self.lane, "add", "x.txt"], check=True)

    def tearDown(self):
        for pr in self.procs:
            pr.kill()
            pr.wait()
        super().tearDown()

    def gitdir(self):
        r = subprocess.run(["git", "-C", self.lane, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True)
        return r.stdout.strip()

    def lock(self, path, age=3600):
        with open(path, "w") as f:
            f.write("half a write")
        t = time.time() - age
        os.utime(path, (t, t))
        return path

    def commit(self):
        return subprocess.run(["git", "-C", self.lane, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                               "-m", "work"], capture_output=True, text=True)

    def failure(self, cwd=None, agent=True, error=None):
        """The failure hook for the builder's failed commit (a subagent's call: it carries an agent_id)."""
        c = self.commit()
        pl = {"tool_name": "Bash", "tool_input": {"command": "git commit -m work"}, "cwd": cwd or self.lane,
              "error": error if error is not None else "Exit code 128\n" + c.stderr}
        if agent:
            pl["agent_id"] = "a1"
        return self.hook("PostToolUseFailure", pl)

    def test_the_commit_really_fails_on_the_lock_and_works_once_it_is_cleared(self):
        path = self.lock(os.path.join(self.gitdir(), "index.lock"))
        c = self.commit()
        self.assertNotEqual(c.returncode, 0)
        self.assertIn("index.lock", c.stderr)
        r = self.failure()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.commit().returncode, 0)

    def test_a_fresh_lock_is_left(self):
        path = self.lock(os.path.join(self.gitdir(), "index.lock"), age=5)
        self.failure()
        self.assertTrue(os.path.exists(path))

    def test_a_lock_a_live_git_holds_is_left(self):
        path = self.lock(os.path.join(self.gitdir(), "index.lock"))
        pr = subprocess.Popen(["git", "300"], executable=shutil.which("sleep"), cwd=self.lane, stdout=subprocess.DEVNULL)
        self.procs.append(pr)
        self.failure()
        self.assertTrue(os.path.exists(path))
        pr.kill()
        pr.wait()
        self.failure()  # the holder is gone: the same lock is now stale
        self.assertFalse(os.path.exists(path))

    def test_a_failure_that_does_not_name_a_lock_clears_nothing(self):
        path = self.lock(os.path.join(self.gitdir(), "index.lock"))
        self.failure(error="Exit code 1\nnothing to commit")
        self.assertTrue(os.path.exists(path))

    def test_a_plain_repo_is_not_a_lane_and_its_lock_is_left(self):
        path = self.lock(os.path.join(self.repo, ".git", "index.lock"))
        self.failure(cwd=self.repo, error="fatal: Unable to create '.git/index.lock': File exists.")
        self.assertTrue(os.path.exists(path))

    def test_a_file_outside_the_repo_git_folders_is_never_removed(self):
        outside = self.lock(os.path.join(self.tmp, "index.lock"))
        also = self.lock(os.path.join(self.lane, "index.lock"))  # a tracked-tree file of that name, not git's
        self.failure(error=f"fatal: Unable to create '{outside}': File exists.")
        self.assertTrue(os.path.exists(outside))
        self.assertTrue(os.path.exists(also))

    def test_the_main_threads_own_failure_clears_it_too_and_a_second_run_is_silent(self):
        path = self.lock(os.path.join(self.gitdir(), "index.lock"))
        self.failure(agent=False)
        self.assertFalse(os.path.exists(path))
        r = self.failure(agent=False)  # nothing left to clear: no error, no removal
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_removal_is_in_the_ledger(self):
        self.lock(os.path.join(self.gitdir(), "index.lock"))
        self.failure()
        from sampo import core as c
        self.assertTrue(any(e.get("event") == "index_lock_removed" for e in c.ledger_tail(c.find_project(self.repo))))


if __name__ == "__main__":
    import unittest
    unittest.main()
