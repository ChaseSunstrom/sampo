"""T-4616: a git lock file (index.lock, HEAD.lock, packed-refs.lock, a refs lock) in a lane's git dir or the main .git
that is older than the bound with no live git process for that git dir or worktree is removed and reported by the lane
verbs (list, brief), the train's start and doctor --repair; a younger lock, or one a live git process may hold, is left
and named. Both conditions are required: age alone never decides."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import doctor
from sampo import lanes as slanes
from sampo import train


class StaleGitLock(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.lane = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/lane", self.lane], check=True)
        self.procs = []

    def tearDown(self):
        for pr in self.procs:
            pr.kill()
            pr.wait()
            if pr.stdin:
                pr.stdin.close()
        super().tearDown()

    def lane_gitdir(self):
        with open(os.path.join(self.lane, ".git")) as f:
            return f.read().strip().split(" ", 1)[1]

    def lock(self, path, age=3600):
        with open(path, "w") as f:
            f.write("")
        t = time.time() - age
        os.utime(path, (t, t))
        return path

    def fake_git(self, cwd):
        """A live process whose argv[0] is git (a sleep under that name), started in cwd."""
        pr = subprocess.Popen(["git", "300"], executable=shutil.which("sleep"), cwd=cwd, stdout=subprocess.DEVNULL)
        self.procs.append(pr)
        return pr

    def rows(self, **kw):
        return slanes.git_locks(self.repo, **kw)

    def test_no_lock_is_silent(self):
        self.assertEqual(self.rows(), [])
        self.assertEqual(slanes.clear_git_locks(self.repo), [])
        self.assertEqual(slanes.git_lock_lines([]), [])

    def test_an_old_lock_with_no_git_process_is_removed_and_reported(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        rows = slanes.clear_git_locks(self.repo)
        self.assertEqual([(r["path"], r["state"]) for r in rows], [(os.path.realpath(path), "removed")])
        self.assertFalse(os.path.exists(path))
        line = "\n".join(slanes.git_lock_lines(rows))
        self.assertIn("removed", line)
        self.assertIn("index.lock", line)
        self.assertIn("no git process", line)

    def test_a_fresh_lock_stays_and_is_named(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"), age=5)
        rows = slanes.clear_git_locks(self.repo)
        self.assertEqual([r["state"] for r in rows], ["fresh"])
        self.assertTrue(os.path.exists(path))
        self.assertIn("younger", "\n".join(slanes.git_lock_lines(rows)))

    def test_an_old_lock_with_a_live_git_process_in_the_lane_stays_and_is_named(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        pr = self.fake_git(self.lane)
        rows = slanes.clear_git_locks(self.repo)
        self.assertEqual([r["state"] for r in rows], ["held"])
        self.assertEqual(rows[0]["pids"], [pr.pid])
        self.assertTrue(os.path.exists(path))
        self.assertIn(str(pr.pid), "\n".join(slanes.git_lock_lines(rows)))
        pr.kill()
        pr.wait()
        self.assertEqual([r["state"] for r in slanes.clear_git_locks(self.repo)], ["removed"])  # a second run, process gone

    def test_a_git_process_in_another_repo_or_another_lane_does_not_hold_this_lanes_lock(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        self.fake_git(other)
        self.fake_git(self.repo)  # main's own work never touches the lane's index
        self.assertEqual([r["state"] for r in slanes.clear_git_locks(self.repo)], ["removed"])
        self.assertFalse(os.path.exists(path))

    def test_a_process_that_is_not_git_does_not_hold_a_lock(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        pr = subprocess.Popen(["sleep", "300"], cwd=self.lane)  # a build tool in the lane, not git
        self.procs.append(pr)
        self.assertEqual([r["state"] for r in slanes.clear_git_locks(self.repo)], ["removed"])
        self.assertFalse(os.path.exists(path))

    def test_a_git_named_by_dash_c_holds_the_lock_from_any_cwd(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        pr = subprocess.Popen(["git", "-c", "read x; :", "-C", self.lane], executable="/bin/sh", cwd=self.tmp,
                              stdin=subprocess.PIPE)  # a shell named git, waiting on its stdin, with -C lane on its line
        self.procs.append(pr)
        time.sleep(0.2)
        self.assertEqual([r["state"] for r in slanes.clear_git_locks(self.repo)], ["held"])
        self.assertTrue(os.path.exists(path))

    def test_a_process_in_a_lane_holds_a_lock_in_the_shared_git_dir(self):
        ref = self.lock(os.path.join(self.repo, ".git", "refs", "heads", "foreman", "x.lock"))
        pr = self.fake_git(self.lane)
        self.assertEqual([r["state"] for r in slanes.clear_git_locks(self.repo)], ["held"])
        self.assertTrue(os.path.exists(ref))
        pr.kill()
        pr.wait()
        self.assertEqual([r["state"] for r in slanes.clear_git_locks(self.repo)], ["removed"])
        self.assertFalse(os.path.exists(ref))

    def test_only_git_locks_are_touched(self):
        gd = self.lane_gitdir()
        keep = [self.lock(os.path.join(gd, "config.lock")), self.lock(os.path.join(gd, "gc.pid")),
                self.lock(os.path.join(self.repo, ".git", "shallow.lock")),
                self.lock(os.path.join(self.repo, "index.lock"))]  # a tracked-tree file of that name is not a git lock
        gone = [self.lock(os.path.join(gd, "HEAD.lock")), self.lock(os.path.join(self.repo, ".git", "index.lock")),
                self.lock(os.path.join(self.repo, ".git", "packed-refs.lock")),
                self.lock(os.path.join(self.repo, ".git", "refs", "tags", "v1.lock"))]
        rows = slanes.clear_git_locks(self.repo)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["state"] == "removed" for r in rows))
        self.assertEqual([os.path.exists(x) for x in keep], [True] * 4)
        self.assertEqual([os.path.exists(x) for x in gone], [False] * 4)

    def test_a_lock_that_is_a_symlink_or_a_directory_is_left_alone(self):
        gd = self.lane_gitdir()
        os.mkdir(os.path.join(gd, "index.lock"))
        os.symlink(os.path.join(self.tmp, "nowhere"), os.path.join(gd, "HEAD.lock"))
        self.assertEqual(slanes.clear_git_locks(self.repo), [])
        self.assertTrue(os.path.isdir(os.path.join(gd, "index.lock")))
        self.assertTrue(os.path.islink(os.path.join(gd, "HEAD.lock")))

    def test_a_lock_touched_after_the_scan_is_not_removed(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        rows = slanes.git_locks(self.repo)
        self.lock(path, age=0)  # a git process rewrote it between the scan and the removal
        done = slanes.remove_stale(rows)
        self.assertEqual([r["state"] for r in done], ["fresh"])
        self.assertTrue(os.path.exists(path))

    def test_a_lock_with_a_future_mtime_is_not_old(self):
        self.lock(os.path.join(self.lane_gitdir(), "index.lock"), age=-3600)
        self.assertEqual([r["state"] for r in slanes.clear_git_locks(self.repo)], ["fresh"])

    def test_process_detection_reads_a_proc_tree_and_unreadable_entries_do_not_break_it(self):
        proc = os.path.join(self.tmp, "proc")
        for pid, argv, cwd in (("11", "git\0status\0", self.lane), ("12", "/usr/bin/git\0commit\0", self.tmp),
                               ("13", "vim\0x\0", self.lane), ("14", "git-remote-https\0origin\0", self.lane)):
            os.makedirs(os.path.join(proc, pid))
            with open(os.path.join(proc, pid, "cmdline"), "w") as f:
                f.write(argv)
            os.symlink(cwd, os.path.join(proc, pid, "cwd"))
        os.makedirs(os.path.join(proc, "15"))  # a process that exited: no cmdline
        os.makedirs(os.path.join(proc, "self"))
        self.assertEqual(slanes.git_pids([self.lane], proc_root=proc), [11, 14])
        self.assertEqual(slanes.git_pids([os.path.join(self.tmp, "none")], proc_root=proc), [])
        self.assertIsNone(slanes.git_pids([self.lane], proc_root=os.path.join(self.tmp, "no-proc")))  # unknown, not "none"

    def test_an_unreadable_process_table_leaves_the_lock_alone(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        with mock.patch.object(slanes, "git_pids", return_value=None):
            rows = slanes.clear_git_locks(self.repo)
        self.assertEqual([r["state"] for r in rows], ["held"])
        self.assertTrue(os.path.exists(path))

    def test_the_process_table_is_read_only_when_a_lock_is_old(self):
        self.lock(os.path.join(self.lane_gitdir(), "index.lock"), age=5)
        with mock.patch.object(slanes, "git_pids", side_effect=AssertionError("scanned /proc for a fresh lock")):
            self.assertEqual([r["state"] for r in slanes.clear_git_locks(self.repo)], ["fresh"])

    def test_lane_list_removes_and_says_so(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        out = self.fm("lane", "list", "--json").stdout
        got = json.loads(out)
        self.assertEqual([r["state"] for r in got["git_locks"]], ["removed"])
        self.assertFalse(os.path.exists(path))
        text = self.fm("lane", "list").stdout
        self.assertNotIn("index.lock", text)  # the second run finds none: silent
        self.lock(path)
        self.assertIn("removed", self.fm("lane", "list").stdout)

    def test_lane_list_names_a_fresh_lock_and_leaves_it(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"), age=3)
        out = self.fm("lane", "list").stdout
        self.assertIn("younger", out)
        self.assertTrue(os.path.exists(path))

    def test_lane_brief_clears_and_reports_in_its_warnings(self):
        tid = json.loads(self.fm("task", "new", "Built", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        got = json.loads(self.fm("lane", "brief", tid, "--json").stdout)
        self.assertTrue(any("index.lock" in w and "removed" in w for w in got["warnings"]), got["warnings"])
        self.assertFalse(os.path.exists(path))

    def test_the_train_clears_locks_before_it_starts(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        err = io.StringIO()
        with mock.patch.object(train, "_run", return_value="ran") as run, \
                mock.patch.object(train, "_train_run_lock", return_value=contextlib.nullcontext()), \
                mock.patch.object(train, "_after_train_run"), contextlib.redirect_stderr(err):
            from sampo import core as c
            self.assertEqual(train.train(c.find_project(self.repo), self.repo, ["T-0001"]), "ran")
        run.assert_called_once()
        self.assertFalse(os.path.exists(path))
        self.assertIn("removed", err.getvalue())

    def test_doctor_repair_removes_a_stale_lock_in_a_project_and_names_it(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        fresh = self.lock(os.path.join(self.repo, ".git", "HEAD.lock"), age=2)
        moved = doctor.repair([self.repo])
        self.assertTrue(any(a == os.path.realpath(path) and "removed" in b for a, b in moved), moved)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(any(a == os.path.realpath(fresh) and "left" in b for a, b in moved), moved)
        self.assertTrue(os.path.exists(fresh))

    def test_doctor_repair_through_the_cli(self):
        path = self.lock(os.path.join(self.lane_gitdir(), "index.lock"))
        out = self.fm("doctor", "--repair", timeout=120).stdout
        self.assertIn("index.lock", out)
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
