"""T-4538: a run.py whose parent process exits ends itself within about 10 s, its classes' process groups with it, so
their CPU slots are freed: nobody is left to read its result (an orphan held a slot for 33 minutes on 2026-11-12). A
run started detached on purpose sets SAMPO_KEEP_ORPHAN=1 and finishes as before."""
import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import run  # noqa: E402  (the run.py under test: its HERE is the folder the scratch repo copies from)

# the class writes its pid and a started mark, then sleeps the seconds in secs, then a done mark
SLOW = ("import os, time, unittest\n"
        "class Slow(unittest.TestCase):\n    def test_it(self):\n"
        "        d = os.environ['ORPHAN_DIR']\n"
        "        with open(os.path.join(d, 'pid'), 'w') as f:\n            f.write(str(os.getpid()))\n"
        "        open(os.path.join(d, 'started'), 'w').close()\n"
        "        with open(os.path.join(d, 'secs')) as f:\n            time.sleep(float(f.read()))\n"
        "        open(os.path.join(d, 'done'), 'w').close()\n")
# a stand-in for a check that ends mid-run: it starts run.py (its own session, output to a file), waits until the
# class has started, prints run.py's pid, and exits without waiting for it
PARENT = ("import os, subprocess, sys, time\n"
          "marker, log, argv = sys.argv[1], sys.argv[2], sys.argv[3:]\n"
          "with open(log, 'w') as out:\n"
          "    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,\n"
          "                         start_new_session=True)\n"
          "print(p.pid, flush=True)\n"
          "end = time.time() + 60\n"
          "while not os.path.exists(marker) and time.time() < end:\n    time.sleep(0.05)\n"
          "os._exit(0)\n")
# what a run inherits from the test runner that started it: its slot, its deadline and its cache
NESTED = ("SAMPO_SLOT", "FOREMAN_SLOT", "SAMPO_SLOTS", "FOREMAN_SLOTS", "SAMPO_CACHE_FULL", "FOREMAN_CACHE_FULL",
          "SAMPO_KEEP_ORPHAN", "FOREMAN_KEEP_ORPHAN", "FM_RUN_DEADLINE", "FM_TEST_TIMES", "SAMPO_TEST_RUN_ID",
          "FOREMAN_TEST_RUN_ID", "SAMPO_CPU_BASE", "FOREMAN_CPU_BASE", "SAMPO_CPU_GATE", "FOREMAN_CPU_GATE")


def gone(pid):
    """True once pid has ended: no such process, or a zombie (its parent has ended, its reaper has not yet)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except OSError:
        return True


def read(path):
    with open(path) as f:
        return f.read()


class Orphan(unittest.TestCase):
    def setUp(self):
        top = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, top, True)
        self.repo = os.path.join(top, "repo")
        self.tests = os.path.join(self.repo, "plugin", "tests")
        os.makedirs(self.tests)
        for name in ("run.py", "runcover.py"):
            shutil.copy(os.path.join(run.HERE, name), os.path.join(self.tests, name))
        with open(os.path.join(self.tests, "test_slow.py"), "w") as f:
            f.write(SLOW)
        git = ["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.test"]
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        self.d, self.xdg = os.path.join(top, "d"), os.path.join(top, "xdg")
        os.makedirs(self.d)
        os.makedirs(self.xdg)
        self.log = os.path.join(self.d, "run.log")
        self.env = dict({k: v for k, v in os.environ.items() if k not in NESTED},
                        XDG_CACHE_HOME=os.path.join(top, "cache"), XDG_RUNTIME_DIR=self.xdg, SAMPO_SLOTS="1",
                        FM_TEST_TIMES=os.path.join(top, "t.json"), ORPHAN_DIR=self.d)
        self.run_pid = None

    def start(self, secs, **env):
        """(run.py's pid, the parent that exits) once the class has started, running for secs seconds."""
        with open(os.path.join(self.d, "secs"), "w") as f:
            f.write(str(secs))
        parent = subprocess.Popen([sys.executable, "-c", PARENT, os.path.join(self.d, "started"), self.log,
                                   sys.executable, os.path.join(self.tests, "run.py"), "test_slow"],
                                  cwd=self.repo, env=dict(self.env, **env), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(self.kill)
        self.addCleanup(parent.stdout.close)
        pid = self.run_pid = int(parent.stdout.readline())
        end = time.monotonic() + 60
        while not os.path.exists(os.path.join(self.d, "started")) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(os.path.join(self.d, "started")), "the class never started")
        return pid, parent

    def kill(self):
        """Whatever the test left running: run.py's group (its session leader is run.py), and the class's own pid."""
        if self.run_pid is not None:
            with contextlib.suppress(OSError):
                os.killpg(self.run_pid, signal.SIGKILL)
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(read(os.path.join(self.d, "pid"))), signal.SIGKILL)

    def slots_free(self):
        """Every slot lock file of the pool is free (the run created at least one)."""
        folder = os.path.join(self.xdg, f"sampo-slots-{os.getuid()}")
        names = os.listdir(folder)
        self.assertTrue(names, "the run took no slot, so nothing shows it was freed")
        for name in names:
            fd = os.open(os.path.join(folder, name), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            finally:
                os.close(fd)
        return True

    def test_an_orphaned_run_ends_within_ten_seconds_with_its_class_and_slot(self):
        pid, parent = self.start(secs=60)  # the class would hold its slot a minute
        self.assertEqual(parent.wait(timeout=60), 0)  # the parent exits mid-run: run.py is an orphan now
        t0 = time.monotonic()
        while not gone(pid) and time.monotonic() - t0 < 10:
            time.sleep(0.1)
        self.assertTrue(gone(pid), "run.py outlived its parent")
        self.assertTrue(gone(int(read(os.path.join(self.d, "pid")))), "its class kept running")
        self.assertFalse(os.path.exists(os.path.join(self.d, "done")))
        self.assertTrue(self.slots_free(), "a CPU slot is still held")

    def test_the_keep_switch_lets_a_detached_run_finish_after_its_parent(self):
        pid, parent = self.start(secs=4, SAMPO_KEEP_ORPHAN="1")
        self.assertEqual(parent.wait(timeout=60), 0)
        time.sleep(2)  # well past the poll in which an orphan ends
        self.assertFalse(gone(pid), "the switch did not keep the run")
        t0 = time.monotonic()
        while not gone(pid) and time.monotonic() - t0 < 60:
            time.sleep(0.2)
        self.assertTrue(os.path.exists(os.path.join(self.d, "done")), "the class did not finish")
        self.assertRegex(read(self.log), r"Ran 1 tests? in")

    def test_the_switch_is_an_operator_switch_so_no_class_inherits_it(self):
        self.assertIn("KEEP_ORPHAN", run.OPERATOR_SWITCHES)
