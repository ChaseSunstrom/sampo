"""The claude -p helpers' reaper (T-1044): every child runs in its own process group, so a timeout kills its
grandchildren too; each is registered in state/children/<pid>, and SessionStart sweeps the ones a dead run left
behind — only when the pid is still the same process (its start time) running a helper (its command line)."""
import os
import signal
import subprocess
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

# a helper that starts a grandchild, says where it is, and waits on it (the grandchild off the pipes, so a runner that
# kills only the leader still returns, and the test fails instead of hanging for the sleep). Its mode ($STUB_MODE, else
# its first argument): fast answers at once; orphan leaves without waiting; escape's grandchild leaves the group
# (setsid) holding the pipes.
STUB = r'''#!/bin/sh
mode=${STUB_MODE:-$1}
if [ "$mode" = fast ]; then cat >/dev/null; echo '{"result": "ok", "total_cost_usd": 0.01}'; exit 0; fi
if [ "$mode" = escape ]; then setsid sleep 20 & echo $! >> "$STUB_PIDFILE"; wait; exit; fi
sleep 300 </dev/null >/dev/null 2>&1 &
echo $! >> "$STUB_PIDFILE"
[ "$mode" = orphan ] || wait
'''


def gone(pid, wait=5):
    """The pid has exited (a zombie waiting on init counts) within `wait` seconds."""
    end = time.time() + wait
    while time.time() < end:
        if not os.path.isdir("/proc"):  # macOS: init reaps an orphan at once, so a signal tells
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                pass
            time.sleep(0.05)
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


class _Stubbed(ForemanTestCase):
    def setUp(self):
        super().setUp()
        from sampo import core as c
        from sampo import ideas
        self.c, self.fmideas = c, ideas
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        self.stub = os.path.join(bindir, "claude")
        with open(self.stub, "w") as f:
            f.write(STUB)
        os.chmod(self.stub, 0o755)
        self.pidfile = os.path.join(self.tmp, "grandchild.pid")
        env = mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"],
                                           "STUB_PIDFILE": self.pidfile})
        env.start()
        self.addCleanup(env.stop)
        self.fm("init")
        self.reg = os.path.join(c.state_dir(), "children")

    def tearDown(self):
        for pid in self.pids():  # a failed test's grandchildren (a pid reused since is no sleep of ours)
            try:
                if os.path.isdir("/proc"):
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        name = f.read().split(b"\0")[0].decode(errors="replace")
                else:  # T-1284: macOS has no /proc
                    name = (subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                           timeout=5).stdout.split() or [""])[0]
                if os.path.basename(name) == "sleep":
                    os.kill(pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                pass
        super().tearDown()

    def pids(self):
        try:
            with open(self.pidfile) as f:
                return [int(x) for x in f.read().split()]
        except (OSError, ValueError):
            return []

    def grandchildren(self, n=1):
        """The stub's grandchildren, once n have started."""
        end = time.time() + 30  # a slow runner's shell starts; a pass returns as soon as they're up
        while time.time() < end:
            if len(self.pids()) >= n:
                return self.pids()
            time.sleep(0.05)
        self.fail(f"the stub never started {n} grandchildren")

    def grandchild_reaped(self, n=1):
        """Every grandchild exited; one that didn't is killed here, so a failing test leaves no sleep behind."""
        alive = [pid for pid in self.grandchildren(n) if not gone(pid)]
        for pid in alive:
            os.kill(pid, signal.SIGKILL)
        return not alive

    def entries(self):
        return sorted(os.listdir(self.reg)) if os.path.isdir(self.reg) else []


class Timeout(_Stubbed):
    def test_run_child_kills_the_whole_group(self):
        with self.assertRaises(ValueError) as e:
            self.fmideas.run_child("ideas", "sys", "a prompt", "haiku", 1)
        self.assertIn("timed out", str(e.exception))
        self.assertTrue(self.grandchild_reaped(), "the grandchild outlived the timeout")
        self.assertEqual(self.entries(), [], "the reaped child left its registry entry")

    def test_run_children_kills_every_group(self):
        out = self.fmideas.run_children([([self.stub, "-p"], "a prompt", "lens")], 1, "ideas")
        self.assertEqual(out, [(None, "timed out after 1s")])
        self.assertTrue(self.grandchild_reaped(), "the grandchild outlived the timeout")
        self.assertEqual(self.entries(), [])

    def test_a_child_that_finishes_leaves_no_entry(self):
        with mock.patch.dict(os.environ, {"STUB_MODE": "fast"}):
            self.assertEqual(self.fmideas.run_child("ideas", "sys", "a prompt", "haiku", 30), "ok")
            self.assertEqual(self.fmideas.run_children([([self.stub, "-p"], "a prompt", "lens")], 30, "ideas"),
                             [("ok", None)])
        self.assertEqual(self.entries(), [])

    def test_an_interrupt_kills_every_unfinished_group(self):
        prs, calls, popen, communicate = [], [], self.fmideas._popen, subprocess.Popen.communicate

        def record(*a, **kw):
            prs.append(popen(*a, **kw))
            return prs[-1]

        def interrupted(pr, *a, **kw):
            if pr not in prs:  # the registry's own ps (no /proc): not one of the children
                return communicate(pr, *a, **kw)
            calls.append(pr)
            if len(calls) == 2:  # ^C while waiting on the second child, both sleepers up
                self.grandchildren(2)
                raise KeyboardInterrupt
            return communicate(pr, *a, **kw)
        jobs = [([self.stub, mode], "a prompt", mode) for mode in ("fast", "slow", "slow")]
        with mock.patch.object(self.fmideas, "_popen", record), \
                mock.patch.object(subprocess.Popen, "communicate", interrupted), \
                self.assertRaises(KeyboardInterrupt):
            self.fmideas.run_children(jobs, 30, "ideas")
        self.assertTrue(self.grandchild_reaped(2), "a grandchild outlived the interrupt")
        self.assertEqual([pr.returncode for pr in prs], [0, -signal.SIGKILL, -signal.SIGKILL])
        self.assertEqual(self.entries(), [])

    def test_a_grandchild_that_left_the_group_cannot_hang_the_reap(self):
        t0 = time.time()
        with mock.patch.dict(os.environ, {"STUB_MODE": "escape"}), self.assertRaises(ValueError):
            self.fmideas.run_child("ideas", "sys", "a prompt", "haiku", 1)
        self.assertLess(time.time() - t0, 15, "the reap waited on the escaped grandchild's pipes")  # tearDown kills it; wall clock: a reap deadline over children
        self.assertEqual(self.entries(), [])


class TimeoutWithoutProc(Timeout):
    """reruns: Timeout's tests with no /proc (T-3815), as macOS runs them (T-1284): the registry's ps calls are
    subprocesses of their own, which the interrupt test's patched communicate must not count as children."""
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, self.fmideas, "PROC", getattr(self.fmideas, "PROC", "/proc"))
        self.fmideas.PROC = os.path.join(self.tmp, "noproc")


class Sweep(_Stubbed):
    def setUp(self):
        # this test's own helpers read /proc; SweepWithoutProc (a subclass, so not skipped with it) covers the ps path
        if type(self) is Sweep and not os.path.isdir("/proc"):
            self.skipTest("no /proc here: SweepWithoutProc covers the reaper's ps path")
        super().setUp()
        self.procs = []

    def tearDown(self):
        for pr in self.procs:  # the groups, before the wait (the pid is still ours) and before tmp goes
            if pr.poll() is None:
                os.killpg(pr.pid, signal.SIGKILL)
            pr.wait()
        super().tearDown()

    def helper(self, argv=None, start=None, age=4000):
        """A helper started by a run that then died: registered, its entry aged (and its start time forged)."""
        pr = self.fmideas._popen(argv or [self.stub, "-p", "--no-session-persistence"], 1, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(pr)
        path = os.path.join(self.reg, str(pr.pid))
        with open(path) as f:
            st, _, timeout = f.read().split()
        with open(path, "w") as f:
            f.write(f"{start or st} {time.time() - age:.0f} {timeout}")
        return pr

    def test_a_stale_helper_is_killed_with_its_group(self):
        pr = self.helper()
        self.grandchildren()  # the stub has started its grandchild
        self.assertEqual(self.fmideas.sweep_orphans(), 1)
        self.assertEqual(pr.wait(5), -signal.SIGKILL)
        self.assertTrue(self.grandchild_reaped())
        self.assertEqual(self.entries(), [])

    def test_a_dead_leaders_group_is_killed(self):
        pr = self.helper([self.stub, "orphan", "--no-session-persistence"])
        self.grandchildren()
        pr.wait()  # the leader is gone; its grandchild still holds the group (and so the pid)
        self.assertEqual(self.fmideas.sweep_orphans(), 1)
        self.assertTrue(self.grandchild_reaped(), "the dead leader's group outlived the sweep")
        self.assertEqual(self.entries(), [])

    def test_a_reused_pid_a_stranger_and_a_fresh_helper_are_left_alone(self):
        reused = self.helper(start="1")  # the pid now belongs to another process: its start time differs
        stranger = self.helper(argv=["sleep", "300"])  # the same process, but not a helper's command line
        fresh = self.helper(age=0)
        self.assertEqual(self.fmideas.sweep_orphans(), 0)
        for pr in (reused, stranger, fresh):
            self.assertIsNone(pr.poll(), pr.args)
        self.assertEqual(self.entries(), [str(fresh.pid)], "a stale entry is dropped, a fresh one kept")

    def test_garbage_and_no_registry_are_harmless(self):
        self.assertEqual(self.fmideas.sweep_orphans(), 0)  # no folder yet
        os.makedirs(self.reg, exist_ok=True)
        for name, body in (("notapid", "1 2 3"), ("999999999", "garbled"), ("999999998", "1 0 1")):  # last: dead
            with open(os.path.join(self.reg, name), "w") as f:
                f.write(body)
        self.assertEqual(self.fmideas.sweep_orphans(), 0)
        self.assertEqual(self.entries(), [])


class Hook(_Stubbed):
    def session_start(self, background, **sweep):
        """The hook's output, the sweep it called, and the orphans_reaped events in the ledger."""
        from sampo import hooks
        with mock.patch.dict(os.environ, {"FOREMAN_NO_BACKGROUND": background}), \
                mock.patch("subprocess.Popen"), \
                mock.patch.object(self.fmideas, "sweep_orphans", **sweep) as called:
            out = hooks.session_start({"source": "compact", "session_id": "s-1", "cwd": self.repo})
        events = self.c.ledger_tail(self.c.find_project(self.repo))
        return out, called, [e["data"] for e in events if e["event"] == "orphans_reaped"]

    def test_session_start_sweeps_and_survives_a_sweep_error(self):
        out, sweep, reaped = self.session_start("", side_effect=RuntimeError("sweep boom"))
        sweep.assert_called_once()
        self.assertIn("additionalContext", out["hookSpecificOutput"])
        self.assertEqual(reaped, [])
        with open(os.path.join(self.c.state_dir(), "logs", "hooks.log")) as f:
            self.assertIn("sweep boom", f.read())

    def test_session_start_records_what_it_reaped(self):
        self.assertEqual(self.session_start("", return_value=2)[2], [{"count": 2}])
        self.assertEqual(self.session_start("", return_value=0)[2], [{"count": 2}], "none reaped: no event")

    def test_a_background_free_child_never_sweeps(self):
        out, sweep, _ = self.session_start("1", return_value=0)
        sweep.assert_not_called()
        self.assertIn("additionalContext", out["hookSpecificOutput"])


class SweepWithoutProc(Sweep):
    """reruns: Sweep's tests with no /proc (T-3815), as macOS runs it (T-1249): ps gives each helper's start time and command line."""
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, self.fmideas, "PROC", getattr(self.fmideas, "PROC", "/proc"))
        self.fmideas.PROC = os.path.join(self.tmp, "noproc")
