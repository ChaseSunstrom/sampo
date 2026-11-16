"""T-4399: a run ended from outside (SIGTERM or SIGINT: the shell's timeout) ends as its --deadline does: the classes it
was running are killed with their stacks and named, the classes not started are named, the summary prints, the exit
code says it was ended (143, 130), and a run that finishes is unchanged."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# the class's working folder is the run's temp folder (cwd); kid.pid is written last, once its child runs
SLEEPER = ("import os, subprocess, time, unittest\n"
           "class S(unittest.TestCase):\n"
           "    def test_s(self):\n"
           "        kid = subprocess.Popen(['sleep', '60'])  # a child in the class's own session\n"
           "        open('kid.pid', 'w').write(str(kid.pid))\n"
           "        time.sleep(60)\n")
QUICK = "import unittest\nclass Q(unittest.TestCase):\n    def test_q(self):\n        pass\n"
# a class that shrugs off the run's SIGABRT (faulthandler's stack dump is then not written), so the run waits its 5 s
# before SIGKILL: the window a second signal must end in at once
STUBBORN = "import signal\n" + SLEEPER.replace(
    "        kid = subprocess.Popen",
    "        signal.signal(signal.SIGABRT, signal.SIG_IGN)\n        kid = subprocess.Popen")
# a red class whose failure message quotes the deadline's kill line: a real failure, not a stop (T-4399)
QUOTES = ("import unittest\nclass R(unittest.TestCase):\n    def test_r(self):\n        self.fail(\"run.py: test_c_quotes.R "
          "still running at the run's deadline (5s in): killed (T-1809)\")\n")


def _folder(d, quick_only=False, stubborn=False):
    """a folder with run.py alone (as test_check_paths copies it) and the test modules: the sleeper, then the quick one"""
    shutil.copy(os.path.join(HERE, "run.py"), d)
    with open(os.path.join(d, "test_b_quick.py"), "w") as f:
        f.write(QUICK)
    if not quick_only:
        with open(os.path.join(d, "test_a_sleep.py"), "w") as f:
            f.write(STUBBORN if stubborn else SLEEPER)


def _run(d, *args):
    """run.py one class at a time, as a subprocess in d, with its own times file and cache"""
    env = dict(os.environ, FOREMAN_NO_TEST_CACHE="1", FM_TEST_TIMES=os.path.join(d, "times.json"),
               XDG_CACHE_HOME=os.path.join(d, "cache"), FOREMAN_FORK_CLI="0")
    return subprocess.Popen([sys.executable, os.path.join(d, "run.py"), "-j", "1", *args], cwd=d,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def _sleeping(d, timeout=120):
    """waits until the sleeping class runs (kid.pid is its last line before the sleep); a run under load can be slow"""
    end = time.time() + timeout
    while not os.path.exists(os.path.join(d, "kid.pid")):
        if time.time() > end:
            raise AssertionError("the sleeping class never started")
        time.sleep(0.1)
    with open(os.path.join(d, "kid.pid")) as f:
        return int(f.read())


def _alive(pid):
    """True for a process that exists and is not a zombie (a killed child reaped later reads as gone)"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class SigtermRun(unittest.TestCase):
    def ended(self, sig):
        """a run on a sleeping class and a quick one (one slot): sig is sent once the sleeper runs. Returns the exit
        code, stderr, the seconds it took, the sleeper's child pid and the times file's text (None when unwritten)."""
        with tempfile.TemporaryDirectory() as d:
            _folder(d)
            p = _run(d)
            kid = _sleeping(d)
            t0 = time.time()
            p.send_signal(sig)
            _, err = p.communicate(timeout=120)
            took = time.time() - t0
            times = None
            if os.path.exists(os.path.join(d, "times.json")):
                with open(os.path.join(d, "times.json")) as f:
                    times = f.read()
            return p.returncode, err, took, kid, times

    def test_sigterm_names_the_running_class_and_its_unstarted_ones(self):  # names_the_running_class
        code, err, took, _, _ = self.ended(signal.SIGTERM)
        self.assertEqual(code, 143)
        self.assertLess(took, 20)  # killed within a few polls, not at its 480 s cap
        self.assertIn("still running when the run was ended from outside", err)  # the class that was running
        self.assertIn("test_a_sleep.S", err)
        self.assertIn("test_s", err)  # its stack
        self.assertIn("ended from outside (SIGTERM)", err)  # one line saying how it ended
        self.assertIn("not run class: test_b_quick.Q", err)  # never started
        self.assertIn("INCOMPLETE (ended from outside)", err)

    @unittest.skipUnless(sys.platform.startswith("linux"), "the /proc reads are Linux's")
    def test_sigterm_children_gone(self):  # children_gone
        code, err, took, kid, _ = self.ended(signal.SIGTERM)
        end = time.time() + 5
        while _alive(kid) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(_alive(kid), "the sleeper's child outlived the run")

    def test_sigterm_leaves_nothing_green(self):  # nothing_green
        code, err, _, _, times = self.ended(signal.SIGTERM)
        self.assertNotRegex(err, r"(?m)^OK$")  # the verdict is not green
        self.assertIn("INCOMPLETE (ended from outside)", err)  # the run says it did not finish, not that it had nothing to run
        self.assertTrue(times is None or "test_a_sleep.S" not in times)  # the unfinished class is not timed as done

    def test_sigint_exits_130_and_names_the_running_class(self):  # sigint
        code, err, took, _, _ = self.ended(signal.SIGINT)
        self.assertEqual(code, 130)
        self.assertLess(took, 20)
        self.assertIn("ended from outside (SIGINT)", err)
        self.assertIn("test_a_sleep.S", err)
        self.assertIn("not run class: test_b_quick.Q", err)

    def test_second_signal_ends_at_once(self):  # second_signal
        # the class ignores SIGABRT, so the run would wait its 5 s before SIGKILL: a second signal must cut that wait
        with tempfile.TemporaryDirectory() as d:
            _folder(d, stubborn=True)
            p = _run(d)
            _sleeping(d)
            p.send_signal(signal.SIGTERM)
            time.sleep(1)  # the first ends the run and starts the wait; the second must not sit through it
            t0 = time.time()
            p.send_signal(signal.SIGTERM)
            p.communicate(timeout=20)
            self.assertIn(p.returncode, (143, -signal.SIGTERM))
            self.assertLess(time.time() - t0, 3)

    def test_quoted_deadline_line_in_a_red_class_is_a_failure(self):  # quoted_deadline
        # a class whose failure text quotes the deadline's kill line is FAILED (exit 1), not stopped at its deadline (124)
        with tempfile.TemporaryDirectory() as d:
            _folder(d, quick_only=True)
            with open(os.path.join(d, "test_c_quotes.py"), "w") as f:
                f.write(QUOTES)
            p = _run(d, "test_c_quotes")
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 1, err)
            self.assertIn("FAILED", err)
            self.assertNotIn("stopped at its deadline", err)

    def test_normal_run_unchanged(self):  # normal_run_unchanged
        with tempfile.TemporaryDirectory() as d:
            _folder(d, quick_only=True)
            p = _run(d, "test_b_quick")
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0)
            self.assertRegex(err, r"(?m)^OK$")
            self.assertNotIn("ended from outside", err)
