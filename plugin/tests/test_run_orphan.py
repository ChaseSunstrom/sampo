"""T-4538: a run.py whose parent process exits ends itself within about 10 s, its classes' process groups with it, so
their CPU slots are freed: nobody is left to read its result (an orphan held a slot for 33 minutes on 2026-11-12). A
run started detached on purpose sets SAMPO_KEEP_ORPHAN=1 and finishes as before."""
import contextlib
import errno
import fcntl
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

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


def gone_why(pid):
    """(True once pid has ended, the branch that answered: kill, proc or ps, with the state it saw). Ended: no such
    process, or a zombie (its parent has ended, its reaper has not yet)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True, "kill:none"
    except PermissionError:
        return False, "kill:EPERM"
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
        return state in ("Z", "X"), f"proc:{state}"
    except OSError:
        pass
    # T-4633: no /proc (macOS): ask ps, whose stat column starts with Z for a zombie; no row means it has ended
    got = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    state = got.stdout.strip()[:1] or "none"
    return state in ("Z", "X", "none"), f"ps:{state}"


def gone(pid):
    """True once pid has ended (see gone_why)."""
    return gone_why(pid)[0]


def ps_row(pid):
    """T-4829: the process's row as ps prints it (pid, ppid, pgid, stat), 'none' when ps has no row for it."""
    try:
        got = subprocess.run(["ps", "-o", "pid=,ppid=,pgid=,stat=", "-p", str(pid)], capture_output=True, text=True)
    except OSError as e:  # the diagnosis must report, not raise: a missing ps is named in the row
        return f"ps-error:{e.errno}"
    return " ".join(got.stdout.split()) or "none"


def watch_gone(pid, t0, budget=10.0, marks=(0, 5, 9)):
    """T-4829: poll until pid has ended ('' is returned) or budget seconds after t0 have passed. Else the detail for the
    failure: the pid, then at each mark (seconds after t0) the process's ps row and the branch of gone() that answered.
    CI's log cuts long lines, so the row is cut at 60 characters and the detail stays well under 400 in all."""
    due = list(marks)
    samples = []
    while True:
        ended, branch = gone_why(pid)
        elapsed = time.monotonic() - t0
        while due and elapsed >= due[0]:
            samples.append(f"{due.pop(0):g}s ps={ps_row(pid)[:60]} gone={branch}")
        if ended:
            return ""
        if elapsed >= budget:
            return f"pid {pid}, " + "; ".join(samples)
        time.sleep(0.1)


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
        # T-4758: run.py is gone the moment it kills the class's group; without the kernel's parent-death signal (macOS)
        # the class pid is reaped a beat later, so it gets what is left of the same ten seconds, not one look
        cls = int(read(os.path.join(self.d, "pid")))
        detail = watch_gone(cls, t0)  # T-4829: CI's macOS log had only 'kept running'; the detail says which row and branch
        if detail:
            self.fail(f"its class kept running: {detail}")
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


class OrphanReport(unittest.TestCase):
    """T-4829: the orphan check's failure names the class's ps row and gone()'s branch at 0, 5 and 9 s."""

    def live_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def test_a_class_that_stays_names_its_ps_row_and_gone_branch_at_each_mark(self):
        child = self.live_child()  # the forced failure: a live pid the check sees at every mark
        detail = watch_gone(child.pid, time.monotonic(), budget=0.5, marks=(0, 0.2, 0.4))
        self.assertTrue(detail.startswith(f"pid {child.pid}, "), detail)
        for mark in ("0s", "0.2s", "0.4s"):
            self.assertRegex(detail, rf"{mark} ps=\S.*? gone=(proc|ps|kill):\S+")
        self.assertEqual(detail.count("gone="), 3)

    def test_an_ended_class_gives_no_detail(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()  # reaped: no such process any more
        self.assertEqual(watch_gone(child.pid, time.monotonic(), budget=0.5, marks=(0,)), "")

    def test_the_detail_stays_short_even_for_a_long_ps_row(self):
        child = self.live_child()
        with mock.patch.dict(globals(), {"ps_row": lambda pid: "9" * 500}):
            detail = watch_gone(child.pid, time.monotonic(), budget=0.3, marks=(0, 0.1, 0.2))
        self.assertLess(len("AssertionError: its class kept running: " + detail), 400)


class OrphanKill(unittest.TestCase):
    def test_a_failed_killpg_in_the_orphan_path_reaches_stderr_with_its_errno(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()  # reaped, so its group is gone and killpg fails with ESRCH
        run.LIVE_CLASSES.add(child.pid)
        self.addCleanup(run.LIVE_CLASSES.discard, child.pid)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run.kill_orphans()
        self.assertIn(f"killpg({child.pid}, SIGKILL) failed: errno {errno.ESRCH}", err.getvalue())
