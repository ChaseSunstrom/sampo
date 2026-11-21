"""T-4988: the session's scope at idle CPU priority (systemd CPUWeight=idle, cgroup cpu.idle=1), so the user's own
programs get the CPU they ask for first and the session's tests and builds every cycle they leave. sampo cpu idle on|off
sets it; each session start sets it on a scope that holds only this Claude Code's processes, unless turned off. A fake
cgroup folder, a fake /proc and a fake systemctl; the real scope is never touched."""
import argparse
import contextlib
import io
import json
import os
import sys
from unittest import mock

from test_cpu import Base

from sampo import core as c
from sampo import cpu
from sampo import hooks


class Idle(Base):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.put(self.cg, "cpu.idle", "0\n")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "systemctl.log")
        path = os.path.join(self.bin, "systemctl")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> {self.log}\n')
        os.chmod(path, 0o755)
        self.env = {"FOREMAN_CGROUP": self.cg}
        self.own = self.cg
        for patch in (mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"]}),
                      mock.patch.object(cpu, "own_scope", lambda: self.own)):
            patch.start()
            self.addCleanup(patch.stop)

    def idle(self, value):
        args = argparse.Namespace(action="idle", value=value, json=False, window=5.0)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cpu.cmd_cpu(args)
        return out.getvalue()

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def flag(self):
        with open(os.path.join(self.cg, "cpu.idle")) as f:
            return f.read().strip()

    def shown(self):
        return json.loads(self.fm("cpu", "--window", "0", "--json", env=self.env).stdout)

    def test_idle_on_sets_cpuweight_idle_on_the_session_scope_and_off_puts_it_back(self):
        said = self.idle("on")
        self.assertEqual(self.calls(), ["--user set-property --runtime kitty-1-0.scope CPUWeight=idle"])
        self.assertEqual(self.flag(), "1", "the fake systemctl changes nothing, so the cgroup file is written directly")
        self.assertIn("the user's own programs get the CPU they ask for first", said)
        self.assertIs(self.shown()["idle"], True)
        self.assertIn("priority: idle", self.fm("cpu", "--window", "0", env=self.env).stdout)
        self.idle("off")
        self.assertEqual(self.calls()[-1], "--user set-property --runtime kitty-1-0.scope CPUWeight=")
        self.assertEqual(self.flag(), "0")
        self.assertIs(self.shown()["idle"], False)
        self.assertIn("priority: normal", self.fm("cpu", "--window", "0", env=self.env).stdout)

    def test_without_systemctl_the_cgroup_file_is_written(self):
        with mock.patch.object(cpu.shutil, "which", lambda name: None):
            self.idle("on")
        self.assertEqual(self.flag(), "1")
        self.assertEqual(self.calls(), [])
        _, st = cpu._state()
        self.assertEqual(st["idle"]["how"], "cpu.idle=1")

    def test_idle_refuses_any_cgroup_but_a_session_scope_and_a_bad_word(self):
        from sampo import cli
        with self.assertRaises(cli.UsageError):
            self.idle("maybe")
        manager = os.path.join(self.tmp, "cg", "user.slice", "user-1000.slice", "user@1000.service")
        for other in (os.path.join(self.tmp, "cg", "user.slice", "user-1000.slice", "session-3.scope"),
                      manager, os.path.join(manager, "app.slice"), None):
            self.own = other
            with self.subTest(other=other), self.assertRaises(c.PolicyError) as e:
                self.idle("on")
            self.assertIn("systemd user", str(e.exception))
        self.assertEqual(self.calls(), [], "nothing set")
        self.assertEqual(self.flag(), "0")

    def test_a_kernel_without_cpu_idle_is_refused(self):
        os.remove(os.path.join(self.cg, "cpu.idle"))
        with self.assertRaises(c.PolicyError) as e:
            self.idle("on")
        self.assertIn("cpu.idle", str(e.exception))
        self.assertEqual(self.calls(), [])


class AtStart(Idle):
    """A shell (pid 10) runs Claude Code (pid 20), which runs a hook (30) and a test (31) under it."""

    def setUp(self):
        super().setUp()
        self.process(1, "systemd", 0, 1, ["/sbin/init"])
        self.process(5, "kitty", 1, 1, ["kitty"])
        self.process(10, "bash", 5, 1, ["bash"])
        self.process(20, "claude", 10, 1, ["claude"])
        self.process(30, "python3", 20, 1, ["python3", "hook"])
        self.process(31, "python3", 30, 1, ["python3", "-m", "unittest"])
        self.procs("10", "20", "30", "31")
        os.environ.pop("FOREMAN_NO_CPU_IDLE", None)  # helpers set it for every test; ForemanTestCase puts env back
        self.addCleanup(os.environ.__setitem__, "FOREMAN_NO_CPU_IDLE", "1")

    def procs(self, *pids):
        self.put(self.cg, "cgroup.procs", "".join(p + "\n" for p in pids))

    def start(self, pid=20):
        return cpu.idle_at_start(pid, proc=self.proc)

    def test_a_scope_of_claudes_own_processes_goes_idle_at_start(self):
        self.assertEqual(self.start(), "cpu.idle=1")
        self.assertEqual(self.calls(), ["--user set-property --runtime kitty-1-0.scope CPUWeight=idle"])
        self.assertEqual(self.flag(), "1")
        self.assertEqual(cpu._state()[1]["idle"]["by"], "session start")
        self.assertIn("set by session start", self.fm("cpu", "--window", "0", env=self.env).stdout)

    def test_the_start_leaves_a_waiter_that_puts_it_back_when_claude_ends(self):
        started, real = [], cpu.subprocess.Popen

        def popen(argv, **kw):  # the waiter is recorded, not started; systemctl (run uses Popen too) runs
            return started.append((argv, kw)) if argv[0] == sys.executable else real(argv, **kw)
        with mock.patch.dict(os.environ, {"FOREMAN_NO_BACKGROUND": ""}), \
                mock.patch.object(cpu.subprocess, "Popen", popen):
            self.start()
        (argv, kw), = started
        self.assertIn("cpu._release_main(*sys.argv[2:])", argv[2])
        self.assertEqual(argv[-3:], [self.cg, "kitty-1-0.scope", "20"])
        self.assertTrue(kw["start_new_session"])
        released = []
        with mock.patch.object(cpu, "release", lambda d, unit: released.append((d, unit))):
            cpu._release_main(self.cg, "kitty-1-0.scope", "999999999")  # no such process: returns at once
        self.assertEqual(released, [(self.cg, "kitty-1-0.scope")])

    def test_release_puts_the_scope_back_once_no_claude_runs_in_it(self):
        self.start()
        self.procs("10", "31")  # Claude Code ended; the window's shell and a leftover test run on
        self.assertEqual(cpu.release(self.cg, "kitty-1-0.scope", proc=self.proc), "cpu.idle=0")
        self.assertEqual(self.calls()[-1], "--user set-property --runtime kitty-1-0.scope CPUWeight=")
        self.assertEqual(self.flag(), "0")
        self.assertIsNone(cpu.release(self.cg, "kitty-1-0.scope", proc=self.proc), "normal already: nothing to do")

    def test_release_leaves_it_while_another_claude_runs_in_the_scope(self):
        self.start()
        self.process(21, "claude", 10, 1, ["claude"])
        self.procs("10", "21")
        self.assertIsNone(cpu.release(self.cg, "kitty-1-0.scope", proc=self.proc))
        self.assertEqual(self.flag(), "1")

    def test_an_idle_scope_costs_nothing_at_start(self):
        self.put(self.cg, "cpu.idle", "1\n")
        self.assertIsNone(self.start())
        self.assertEqual(self.calls(), [], "a resume or compaction runs no systemctl")

    def test_a_scope_shared_with_another_program_stays_normal(self):
        self.process(40, "code", 5, 1, ["code"])  # an editor that runs Claude Code: its own process is in the scope
        self.procs("10", "20", "30", "40")
        self.assertIsNone(self.start())
        self.assertEqual(self.flag(), "0")
        self.assertEqual(self.calls(), [])

    def test_another_tabs_shell_or_an_editor_claude_runs_inside_keeps_it_normal(self):
        self.process(50, "bash", 5, 1, ["bash"])  # another window's shell in the same scope
        self.procs("10", "20", "30", "50")
        self.assertIsNone(self.start())
        self.process(10, "vim", 5, 1, ["vim"])  # Claude Code under an editor's terminal: the editor is the user's
        self.put(os.path.join(self.proc, "10"), "stat", "10 (vim) S 5 0 0 0 0 0 0 0 0 0 1 0 0 0 20 0 1 0 100 0 0\n")
        self.procs("10", "20", "30")
        self.assertIsNone(self.start())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.flag(), "0")

    def test_a_worker_session_leaves_its_parents_scope_alone(self):
        with mock.patch.dict(os.environ, {"FOREMAN_WORKER_OF": "s-0"}):
            self.assertIsNone(self.start())
        self.assertEqual(self.calls(), [])

    def test_a_process_gone_since_the_list_was_read_is_nobodys(self):
        self.procs("10", "20", "30", "99")  # 99 has no /proc entry
        self.assertEqual(self.start(), "cpu.idle=1")

    def test_turned_off_the_environment_or_no_session_scope_leaves_it(self):
        self.idle("off")
        self.assertIsNone(self.start(), "the user's off holds for later sessions")
        self.idle("on")
        self.put(self.cg, "cpu.idle", "0\n")
        n = len(self.calls())
        with mock.patch.dict(os.environ, {"FOREMAN_NO_CPU_IDLE": "1"}):
            self.assertIsNone(self.start())
        self.assertIsNone(self.start(pid=None))
        self.own = os.path.join(self.tmp, "cg", "user.slice", "user-1000.slice", "session-3.scope")
        self.assertIsNone(self.start())
        self.assertEqual(len(self.calls()), n)
        self.assertEqual(self.flag(), "0")

    def test_it_never_raises(self):
        with mock.patch.object(cpu, "own_scope", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.start())

    def test_session_start_marks_the_scope(self):
        seen = []
        with mock.patch.object(cpu, "idle_at_start", lambda pid: seen.append(pid)), \
                mock.patch.object(hooks, "_claude_pid", lambda: 20):
            for source in ("startup", "resume", "compact", "clear"):
                hooks.session_start({"session_id": "s-1", "source": source, "cwd": self.tmp})
        self.assertEqual(seen, [20, 20], "a new process (start or resume); a compaction or /clear keeps its scope")
