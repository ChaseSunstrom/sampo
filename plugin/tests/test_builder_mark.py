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

    def test_a_fixers_start_and_stop_mark_its_lane_task_too(self):  # T-5681 review: the editing set, not fm-builder alone
        fixer = dict(self.pl, agent_type="sampo:fm-fixer")
        hooks._mark_builder(fixer, True)
        self.assertTrue(self.returned())
        hooks._mark_builder(fixer, False)
        self.assertFalse(self.returned())

    def test_a_returned_builder_drops_its_leases_but_only_its_scope_ones(self):  # T-5594: its scope leases end with it
        from sampo import bus
        with c.lock(bus._dir()):
            data = bus._leases()
            bus._claim(data, self.p, "builder-session", "T-0001", "scope.py", "*", 600, by="scope")  # taken by lane start
            bus._claim(data, self.p, "builder-session", "T-0001", "edited.py", "*", 600)  # taken by an edit: not the scope
            bus._claim(data, self.p, "lane-T-0001", "T-0001", "pre.py", "*", 600)  # the brief's pre-claim
            bus._claim(data, self.p, "builder-session", "T-0002", "other.py", "*", 600, by="scope")  # another task's
            bus._write_leases(data)
        hooks._mark_builder(self.pl, True)
        self.assertTrue(self.returned())
        left = sorted((v["session"], v["file"]) for v in bus._leases().values())
        self.assertEqual(left, [("builder-session", "edited.py"), ("builder-session", "other.py"),
                                ("lane-T-0001", "pre.py")])
        self.assertNotIn("Traceback", self.log())

    def test_the_main_sessions_lease_on_the_task_survives_a_return(self):  # T-5594: a mid-edit lease is not the builder's
        from sampo import bus
        with c.lock(bus._dir()):
            data = bus._leases()
            bus._claim(data, self.p, "builder-session", "T-0001", "scope.py", "*", 600, by="scope")
            bus._claim(data, self.p, "main-sess", "T-0001", "mid_edit.py", "*", 600)  # the main session, same task
            bus._write_leases(data)
        hooks._mark_builder(self.pl, True)
        self.assertTrue(self.returned())
        left = sorted((v["session"], v["file"]) for v in bus._leases().values())
        self.assertEqual(left, [("main-sess", "mid_edit.py")])
        self.assertNotIn("Traceback", self.log())

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
