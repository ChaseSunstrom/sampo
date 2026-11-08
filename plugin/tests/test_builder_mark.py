"""T-3609: a builder's start/stop mark lands while another process holds the project lock: the brief is found outside
the lock, the lock covers one brief's re-read and save, a timeout is retried, and contention is one counted line in
hooks.log, never a traceback (a lost mark left builder_returned unset: the cap counted a returned builder as live)."""
import os
import subprocess
import sys
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c
from sampo import hooks

AGENT = "a1b2c3d4e5f6"  # a builder's lane is .claude/worktrees/agent-<its agent id>
HOLD = r"""import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(float(sys.argv[2]))
"""


class BuilderMark(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "Build it", "--type", "FIX", "--tier", "S", "--ac", "works :: true", "--step", "do it")
        self.p = c.find_project(self.repo)
        b = c.find_brief(self.p, "T-0001")
        b.meta["lane"] = os.path.join(self.tmp, "worktrees", f"agent-{AGENT}")
        c.save_brief(self.p, b)
        self.pl = {"agent_type": "sampo:fm-builder", "agent_id": AGENT, "cwd": self.repo}

    def returned(self):
        return c.find_brief(self.p, "T-0001").meta.get("builder_returned")

    def log(self):
        try:
            with open(os.path.join(c.state_dir(), "logs", "hooks.log"), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def test_the_mark_lands_while_another_process_holds_the_lock(self):
        holder = subprocess.Popen([sys.executable, "-c", HOLD, os.path.join(self.p.dir, ".lock"), "2"],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        hooks._mark_builder(self.pl, True)
        self.assertTrue(self.returned(), "the builder's stop was lost to a busy lock")
        self.assertNotIn("Traceback", self.log())

    def test_a_timeout_is_retried_and_noted_in_one_line(self):
        real, calls = cli.mutate, []

        def busy_twice(*a, **kw):
            calls.append(1)
            if len(calls) < 3:
                raise c.LockTimeout("lock busy")
            return real(*a, **kw)
        with mock.patch.object(cli, "mutate", busy_twice):
            hooks._mark_builder(self.pl, True)
        self.assertTrue(self.returned())
        with mock.patch.object(cli, "mutate", side_effect=c.LockTimeout("lock busy")):
            hooks._mark_builder(dict(self.pl), False)  # never lands: one line says so
        self.assertTrue(self.returned(), "the busy start changed nothing")
        lines = [ln for ln in self.log().splitlines() if "builder mark" in ln]
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("busy 2x", lines[0])
        self.assertIn("busy 3x", lines[1])
        self.assertNotIn("Traceback", self.log())

    def test_briefs_are_parsed_outside_the_lock_and_a_no_op_takes_none(self):
        seen, real = [], c.load_briefs

        def spy(*a, **kw):
            seen.append(os.path.exists(os.path.join(self.p.dir, ".lock")) and self.locked())
            return real(*a, **kw)
        with mock.patch.object(c, "load_briefs", spy):
            hooks._mark_builder(self.pl, True)
        self.assertTrue(self.returned())
        self.assertTrue(seen)
        self.assertFalse(any(seen), "every brief parsed under the project lock")
        with mock.patch.object(cli, "mutate", side_effect=AssertionError("locked for a no-op")):
            hooks._mark_builder(self.pl, True)  # already marked: nothing to write

    def locked(self):
        import fcntl
        fd = os.open(os.path.join(self.p.dir, ".lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
