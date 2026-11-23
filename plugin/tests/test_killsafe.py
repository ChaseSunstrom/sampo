"""T-6095: killsafe signals only a pid this process started (or itself), never a pid a stub or a stale record names.

On 2026-11-22 a test's Mock(pid=N) reached os.killpg through pilotsig._verb and _run_headless. killsafe now also needs the
pid to be a child of this process (its /proc stat ppid is our pid) or a pid a real Popen of this process recorded when it was
constructed, the one way a leader that was already reaped still reaches its group. Every real signal here goes to a child
this test started; the pids that must be refused are only ever seen by a mock of os.kill and os.killpg."""
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
for _p in (HERE, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sampo import killsafe, pilotsig  # noqa: E402

SLEEP = "import time; time.sleep(60)"
GRANDCHILD = "import time; time.sleep(30)"  # short-lived: tearDown kills it by its real pid if a test failed first
LEADER = ("import subprocess, sys; g = subprocess.Popen([sys.executable, '-c', %r], stdin=subprocess.DEVNULL, "
          "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(g.pid, flush=True)" % GRANDCHILD)
HAS_PROC = os.path.isdir("/proc/self")


def _sleeper():
    """A child of this test in its own session and group."""
    return subprocess.Popen([sys.executable, "-c", SLEEP], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _alive(pid):
    """False once the pid is gone or a zombie (a grandchild whose parent died is reaped by init, or left a zombie)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except OSError:
        return False


class KillsafeChildOnly(unittest.TestCase):
    def setUp(self):
        self._grandchild = None  # the real pid of a grandchild a test started, killed in tearDown if it is still alive

    def tearDown(self):
        if self._grandchild is not None and _alive(self._grandchild):
            os.kill(self._grandchild, signal.SIGKILL)

    def test_an_int_pid_that_is_not_our_child_is_refused_and_nothing_is_sent(self):
        # 4242 is a stub's pid here; getppid() is a live process that is not our child. Both are only ever mocked.
        for pid in (4242, 4243, os.getppid()):
            with self.subTest(pid=pid), mock.patch("os.killpg") as g, mock.patch("os.kill") as k:
                self.assertIs(killsafe.killpg(pid, signal.SIGKILL), False)
                self.assertIs(killsafe.kill(pid, signal.SIGKILL), False)
                g.assert_not_called()
                k.assert_not_called()

    def test_a_mock_pid_is_refused_and_never_reaches_killpg(self):
        stub = mock.Mock(pid=4242)
        with mock.patch("os.killpg") as g, mock.patch("os.kill") as k:
            self.assertIs(killsafe.killpg(stub, signal.SIGKILL), False)
            self.assertIs(killsafe.kill(stub.pid, signal.SIGKILL), False)  # the int of the stub is still not our child
            g.assert_not_called()
            k.assert_not_called()

    def test_a_stubbed_popen_through_the_verb_signals_nothing(self):
        """The 2026-11-22 shape: pilotsig._verb with subprocess.Popen stubbed to a Mock(pid=N) and its group kill unpatched."""
        stub = mock.Mock(pid=4242)
        stub.wait.return_value = 0
        with mock.patch.object(pilotsig, "verb_path", return_value=os.path.abspath(__file__)), \
                mock.patch.object(pilotsig, "_cwd", return_value=HERE), \
                mock.patch("subprocess.Popen", return_value=stub), \
                mock.patch("os.killpg") as g, mock.patch("os.kill") as k:
            pilotsig._verb(None, "T-1")
        g.assert_not_called()
        k.assert_not_called()

    def test_a_mock_is_never_registered_as_started(self):
        for stub in (mock.Mock(pid=4242), mock.MagicMock(spec=subprocess.Popen, pid=4242)):
            with self.subTest(stub=type(stub).__name__), mock.patch("os.killpg") as g:
                killsafe.started(stub)
                self.assertIs(killsafe.killpg(4242, signal.SIGKILL), False)
                g.assert_not_called()

    def test_a_real_child_and_its_group_are_signalled(self):
        child = _sleeper()
        try:
            self.assertIs(killsafe.killpg(child.pid, signal.SIGKILL), True)
            self.assertEqual(child.wait(timeout=30), -signal.SIGKILL)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_a_real_child_is_signalled_by_kill(self):
        child = _sleeper()
        try:
            self.assertIs(killsafe.kill(child.pid, signal.SIGKILL), True)
            self.assertEqual(child.wait(timeout=30), -signal.SIGKILL)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    @unittest.skipUnless(HAS_PROC, "the child check reads /proc; elsewhere the Popen record is the only test")
    def test_a_popen_leader_reaped_early_still_reaches_its_grandchild_with_no_started_call(self):
        """The verb's shape without any started() call: a real Popen leader spawns a sleep grandchild in its group and exits,
        the leader is waited (reaped, so no /proc entry and no ppid), and killpg(leader pid) must still reach the grandchild.
        Only the Popen wrap recorded the leader."""
        child = subprocess.Popen([sys.executable, "-c", LEADER], start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            self._grandchild = int(child.stdout.readline())
            child.wait(timeout=30)
            self.assertFalse(os.path.exists(f"/proc/{child.pid}"), "the leader must be reaped for this test to mean anything")
            self.assertIs(killsafe.killpg(child.pid, signal.SIGKILL), True)
            end = time.monotonic() + 30
            while _alive(self._grandchild) and time.monotonic() < end:
                time.sleep(0.05)
            self.assertFalse(_alive(self._grandchild), "the grandchild in the leader's group survived the group kill")
        finally:
            child.stdout.close()

    def test_the_registry_is_bounded_and_drops_the_oldest_pid(self):
        base = 2 ** 30  # plain ints no process holds; only the registry is touched, under mock.patch.dict's restore
        with mock.patch.dict(killsafe._STARTED, clear=True):
            for i in range(killsafe.REGISTRY_MAX + 10):
                killsafe._remember(base + i)
            self.assertEqual(len(killsafe._STARTED), killsafe.REGISTRY_MAX)
            self.assertNotIn(base, killsafe._STARTED)
            self.assertNotIn(base + 9, killsafe._STARTED)
            self.assertIn(base + 10, killsafe._STARTED)
            self.assertIn(base + killsafe.REGISTRY_MAX + 9, killsafe._STARTED)

    def test_a_reaped_pid_that_was_not_started_is_refused(self):
        pid = os.posix_spawn(sys.executable, [sys.executable, "-c", SLEEP], os.environ)  # not a Popen: nothing records it
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        with mock.patch("os.killpg") as g:
            self.assertIs(killsafe.killpg(pid, signal.SIGKILL), False)
        g.assert_not_called()

    def test_kill_other_keeps_the_plain_int_rule_for_a_recorded_pid(self):
        """The records' pids (a pidfile, an env marker) are another process's: only a Mock, 0, 1 and negatives are refused."""
        with mock.patch("os.kill") as k:
            for pid in (mock.Mock(pid=4242), 0, 1, -1, "4243", None):
                self.assertIs(killsafe.kill_other(pid, signal.SIGKILL), False)
            k.assert_not_called()
            self.assertIs(killsafe.kill_other(4243, signal.SIGTERM), True)
        k.assert_called_once_with(4243, signal.SIGTERM)

    def test_a_missing_started_pid_raises_for_the_caller(self):
        """A registered pid whose process is gone reaches os.killpg, so ESRCH is the caller's, as before."""
        child = _sleeper()
        killsafe.started(child)
        child.kill()
        child.wait(timeout=30)
        with self.assertRaises(OSError):
            killsafe.killpg(child.pid, signal.SIGKILL)
