"""T-4728: two runs sharing one temp root never remove each other's folders. On 2026-11-15 a merge train's scratch worktree
and three builders' test folders vanished from a shared /tmp mid-run; the remover was NOT identified (the sweep's pattern,
fmtest-<pid>- and fmfork-<pid>-, cannot match the train's sampo-train-*). This hardens against a possible cause: run.py's
sweep judged a run's folder dead by its pid alone (a live run seen from another pid namespace reads as dead), and helpers.py
left bare /tmp names no run owned. Now a live run holds a lock on its folder that outlives any pid reading, and every name
helpers makes sits in that folder."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import run  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# a stand-in for a run in another pid namespace: it holds a flock on the folder and names nothing the sweep can signal
HOLDER = ("import fcntl, os, sys\n"
          "f = open(os.path.join(sys.argv[1], '.runlock'), 'w')\n"
          "fcntl.flock(f, fcntl.LOCK_EX)\n"
          "print('ready', flush=True)\n"
          "sys.stdin.read()\n")

# a second run's view of the temp root: it imports the test helpers with TMPDIR at the shared base
IMPORTER = ("import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import helpers\n"
            "helpers._repo_template()\n"
            "print(os.getpid(), flush=True)\n"
            "sys.stdin.read()\n")


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def start(code, *args, **env):
    p = subprocess.Popen([sys.executable, "-c", code, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                         env=dict(os.environ, **env))
    return p, p.stdout.readline().strip()


def stop(p):
    p.stdin.close()
    p.wait(30)


class SharedTempRoot(unittest.TestCase):
    def test_a_lock_held_by_a_run_keeps_its_folder_though_its_pid_reads_dead(self):
        with tempfile.TemporaryDirectory() as base:
            pid = dead_pid()  # what a live run in another pid namespace looks like from here
            mine = os.path.join(base, f"{run.TMP_PREFIX}{pid}-abc")
            fork = os.path.join(base, f"fmfork-{pid}-abc")
            os.makedirs(os.path.join(mine, "sub"))
            os.makedirs(fork)
            holder, said = start(HOLDER, mine)
            try:
                self.assertEqual(said, "ready")
                run.sweep(base)
                self.assertTrue(os.path.isdir(os.path.join(mine, "sub")), "a held folder was removed")
                self.assertFalse(os.path.exists(fork), "an unheld dead run's folder is still swept")
            finally:
                stop(holder)
            run.sweep(base)  # the holder is gone: now it is nobody's
            self.assertFalse(os.path.exists(mine))

    def test_a_dead_runs_folder_with_no_lock_is_still_swept(self):
        with tempfile.TemporaryDirectory() as base:
            old = os.path.join(base, f"{run.TMP_PREFIX}{dead_pid()}-abc")
            os.makedirs(os.path.join(old, "sub"))
            with open(os.path.join(old, ".runlock"), "w"):  # the lock file a dead run left, nobody holding it
                pass
            run.sweep(base)
            self.assertEqual(os.listdir(base), [])

    def test_the_sweep_removes_only_inside_the_temp_root(self):
        """Pins behaviour the old sweep already had (rmtree refuses a link, so nothing outside went); it stays so that the
        explicit link and root checks the lock change added cannot be dropped without a red here."""
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as outside:
            keep = os.path.join(outside, "precious")
            os.makedirs(keep)
            link = os.path.join(base, f"{run.TMP_PREFIX}{dead_pid()}-link")
            os.symlink(outside, link)  # a matching name that leads out of the root
            run.sweep(base)
            self.assertTrue(os.path.isdir(keep))
            self.assertTrue(os.path.isdir(outside))

    def test_a_class_holds_its_folder_for_as_long_as_it_runs(self):
        seen = []

        class Fake:
            pid = os.getpid()
            returncode = 0

            def communicate(self, timeout=None):
                seen.append(run.held(self.tmp))
                return "", "Ran 1 test in 0.0s\n"

        def popen(argv, **kw):
            Fake.tmp = kw["env"]["TMPDIR"]
            return Fake()

        with mock.patch.object(run.subprocess, "Popen", side_effect=popen):
            run._run_one("test_x.C", [], 60, None, None)
        self.assertEqual(seen, [True])
        self.assertFalse(os.path.exists(Fake.tmp), "the class's folder goes at its end")

    def test_a_spawn_that_fails_leaks_neither_the_lock_nor_the_folder(self):
        made = []

        def popen(argv, **kw):
            made.append(kw["env"]["TMPDIR"])
            raise OSError("no fork")

        with mock.patch.object(run.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(OSError):
                run._run_one("test_x.C", [], 60, None, None)
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]), "the class's folder is removed when it never started")

    def test_the_helpers_leave_no_bare_name_in_the_temp_root(self):
        with tempfile.TemporaryDirectory() as base:
            other, pid = start(IMPORTER, HERE, TMPDIR=base)
            try:
                self.assertTrue(pid.isdigit(), pid)
                names = os.listdir(base)
                self.assertTrue(names)
                for n in names:
                    self.assertRegex(n, rf"^{run.TMP_PREFIX}{pid}-", f"{n}: a bare name in the shared root")
                run.sweep(base)  # another run starts: the live one's folders stay, every one of them
                self.assertEqual(sorted(os.listdir(base)), sorted(names))
            finally:
                stop(other)
            self.assertEqual(os.listdir(base), [], "what the run made goes with it")

    def test_a_killed_importer_leaves_only_what_the_next_sweep_removes(self):
        with tempfile.TemporaryDirectory() as base:
            other, pid = start(IMPORTER, HERE, TMPDIR=base)
            other.kill()  # no atexit: what a killed run leaves
            other.wait(30)
            self.assertTrue(os.listdir(base))
            run.sweep(base)
            self.assertEqual(os.listdir(base), [])


if __name__ == "__main__":
    unittest.main()
