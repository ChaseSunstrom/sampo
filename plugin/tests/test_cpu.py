"""T-3592: this session's CPU, measured. The scope's cgroup cpu.stat is the exact total (short-lived processes
included); sampling /proc splits it by kind, the rest shown unsampled. The self-check reports how far a 10-minute
average is over the goal (10% of the machine by default) as information, and sampo cpu cap (optional, never set by
Sampo) puts a CPUQuota on the session's systemd scope. A fake cgroup folder, a fake /proc and a fake systemctl."""
import argparse
import contextlib
import io
import json
import os
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import cpu
from sampo import selfcheck

T = cpu.TICK


def stat(pid, comm, ppid, secs, reaped=0, start=100):
    return f"{pid} ({comm}) S {ppid} 0 0 0 0 0 0 0 0 0 {round(secs * T)} 0 {round(reaped * T)} 0 20 0 1 0 {start} 0 0\n"


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.cg = os.path.join(self.tmp, "cg", "user.slice", "user-1000.slice", "user@1000.service", "kitty-1-0.scope")
        os.makedirs(self.cg)
        self.proc = os.path.join(self.tmp, "proc")
        self.usage(0)
        self.put(self.cg, "cpu.max", "max 100000\n")

    def put(self, d, name, text):
        with open(os.path.join(d, name), "w") as f:
            f.write(text)

    def usage(self, secs):
        self.put(self.cg, "cpu.stat", f"usage_usec {round(secs * 1e6)}\nuser_usec 0\nsystem_usec 0\n")

    def process(self, pid, comm, ppid, secs, argv, cwd="/home/alice/app", exe="/usr/bin/python3", reaped=0):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        self.put(d, "stat", stat(pid, comm, ppid, secs, reaped))
        self.put(d, "cmdline", "\0".join(argv) + "\0")
        for name, target in (("cwd", cwd), ("exe", exe)):
            if not os.path.islink(os.path.join(d, name)):
                os.symlink(target, os.path.join(d, name))


class Meter(Base):
    def test_the_cgroup_total_is_split_by_kind_and_the_rest_is_unsampled(self):
        lane = "/home/alice/foreman.lanes/T-0001"
        claude = (10, "claude", 1, ["claude"], "/home/alice/app", "/home/alice/.local/share/claude/versions/2.1.0")
        wrapper = (11, "bash", 10, ["/usr/bin/bash", "-c", "nice -n 10 python3 plugin/tests/run.py test_x"])
        git = (12, "git", 11, ["git", "status"], "/home/alice/app", "/usr/bin/git")
        builder = (13, "python3", 10, ["python3", "x.py"], lane)
        hook = (14, "python3", 10, ["python3", "/home/alice/foreman/plugin/hooks/hook", "Stop"])
        quick = (15, "git", 13, ["git", "commit"], lane, "/usr/bin/git")
        # (own CPU s, reaped children's CPU s) at each scan; None: not running
        cpu_s = {10: [(0, 0), (0.5, 0.3), (1.0, 0.6)],  # hooks and quick commands that lived between two scans
                 11: [(0, 0), (0, 0.2), (0, 0.4)],
                 12: [(3, 0), (4, 0), (5, 0)],
                 13: [(7, 0), (7.5, 0), (8, 1.3)],  # 15 reaped at 1.3 s: 0.1 s after the last scan saw it
                 14: [None, (0.2, 0), (0.4, 0)],  # started inside the window: all of it counts
                 15: [(1.0, 0), (1.2, 0), None]}
        clock = [0.0]

        def at(i):  # the fake /proc and cgroup at the i-th scan (one second apart)
            pids = []
            for spec in (claude, wrapper, git, builder, hook, quick):
                now = cpu_s[spec[0]][i]
                if now is not None:
                    self.process(spec[0], spec[1], spec[2], now[0], *spec[3:], reaped=now[1])
                    pids.append(str(spec[0]))
            self.put(self.cg, "cgroup.procs", "\n".join(pids) + "\n")
            self.usage(100 + i * 3.0)  # 3 cores in all: 0.15 of it from processes neither seen nor reaped here

        def sleep(s):
            clock[0] += s
            at(round(clock[0]))

        at(0)
        with mock.patch.object(cpu, "ncores", return_value=16):
            got = cpu.sample(self.cg, 2, proc=self.proc, every=1, sleep=sleep, clock=lambda: clock[0])
        self.assertAlmostEqual(got["cores"], 3.0)
        self.assertAlmostEqual(got["pct"], 3.0 / 16 * 100)
        kinds = {k: round(v, 3) for k, v in got["kinds"].items()}
        self.assertEqual(kinds, {"tests": 1.2, "builders' commands": 0.55, "Claude itself": 0.5,
                                 "short-lived under Claude": 0.3, "hooks": 0.2, "git": 0.1})
        self.assertEqual(list(kinds)[0], "tests", "most first")
        self.assertAlmostEqual(got["unsampled"], 0.15, places=3)
        self.assertEqual(cpu.top(got), ("tests", got["kinds"]["tests"]))

    def test_sampo_cpu_reads_the_scope_and_shows_its_cpu_max(self):
        self.fm("init")
        self.put(self.cg, "cpu.max", "200000 100000\n")
        self.put(self.cg, "cgroup.procs", f"{os.getpid()}\n")
        got = json.loads(self.fm("cpu", "--window", "0.1", "--json", env={"FOREMAN_CGROUP": self.cg}).stdout)
        self.assertEqual(got["scope"], self.cg)
        self.assertEqual(got["cpu_max"], "200000 100000")
        self.assertEqual(got["limit_cores"], 2.0)
        self.assertEqual(got["cores"], 0.0)
        self.assertEqual(got["goal_pct"], 10.0)
        self.assertIn("kinds", got)
        text = self.fm("cpu", "--window", "0.1", env={"FOREMAN_CGROUP": self.cg}).stdout
        self.assertIn("kitty-1-0.scope", text)
        self.assertIn("limit: 2 cores", text)
        missing = self.fm("cpu", "--window", "0", env={"FOREMAN_CGROUP": os.path.join(self.tmp, "none")}, check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("cgroup", missing.stderr + missing.stdout)

    def test_sampo_cpu_shows_the_test_suites_cpu_by_day(self):  # T-3757
        self.fm("init")
        self.put(self.cg, "cgroup.procs", f"{os.getpid()}\n")
        pdir = c.find_project(self.repo, create=False).dir
        with open(os.path.join(pdir, "test-cpu.json"), "w") as f:
            json.dump({"classes": {}, "days": {"2026-11-05": 1500.4, "2026-11-06": 1650, "2026-11-07": 2900}}, f)
        text = self.fm("cpu", "--window", "0.1", env={"FOREMAN_CGROUP": self.cg}).stdout
        self.assertIn("test suite: about 2,900 CPU s a full run (11-05 1,500 · 11-06 1,650 · 11-07 2,900)", text)
        got = json.loads(self.fm("cpu", "--window", "0.1", "--json", env={"FOREMAN_CGROUP": self.cg}).stdout)
        self.assertEqual(got["tests_trend"][-1], {"day": "2026-11-07", "cpu_s": 2900.0})

    def test_the_self_check_reports_the_ten_minute_average_over_the_goal_only(self):
        self.fm("init")
        p = c.find_project(self.repo)
        top = {"cores": 2.0, "pct": 12.5, "kinds": {"tests": 1.6, "git": 0.1}, "unsampled": 0.3, "secs": 0.5}
        with mock.patch.dict(os.environ, {"FOREMAN_CGROUP": self.cg}), \
                mock.patch.object(cpu, "ncores", return_value=16), \
                mock.patch.object(cpu, "sample", return_value=top) as scan:
            self.usage(0)
            self.assertIsNone(cpu.goal_line(p, now=1000), "no history yet")
            self.usage(4 * 300)
            self.assertIsNone(cpu.goal_line(p, now=1300), "under 10 minutes of samples")
            self.usage(4 * 660)  # 4 cores since the first sample: 25% of 16
            line = cpu.goal_line(p, now=1660)
            self.assertIn("25%", line)
            self.assertIn("15 points over the 10% goal", line)
            self.assertIn("tests 1.6 cores", line)
            self.assertEqual(scan.call_count, 1)
            self.fm("cpu", "goal", "30")
            self.assertIsNone(cpu.goal_line(p, now=1700), "the project's goal is 30%")
            self.fm("cpu", "goal", "10")
            self.usage(4 * 660 + 0.5 * 700)  # half a core since the second sample, 11 min ago
            self.assertIsNone(cpu.goal_line(p, now=2360), "under the goal")
            self.assertEqual(scan.call_count, 1, "no process scan under the goal")
        self.assertIn(selfcheck.cpu_goal, selfcheck.CHECKS)
        refused = self.fm("cpu", "goal", "0", check=False)
        self.assertNotEqual(refused.returncode, 0)


class Cap(Base):
    """In-process: the cap reads only /proc/self/cgroup (own_scope, patched here), never FOREMAN_CGROUP."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "systemctl.log")
        path = os.path.join(self.bin, "systemctl")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> {self.log}\n')
        os.chmod(path, 0o755)
        self.env = {"FOREMAN_CGROUP": self.cg}
        for patch in (mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"]}),
                      mock.patch.object(cpu, "own_scope", lambda: self.own)):
            patch.start()
            self.addCleanup(patch.stop)
        self.own = self.cg

    def cap(self, value):
        args = argparse.Namespace(action="cap", value=value, json=False, window=5.0)
        with contextlib.redirect_stdout(io.StringIO()):
            return cpu.cmd_cpu(args)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_cap_sets_a_runtime_quota_on_the_session_scope_and_off_lifts_it(self):
        n = os.cpu_count()
        self.cap("25")
        self.assertEqual(self.calls(), [f"--user set-property --runtime kitty-1-0.scope CPUQuota={round(25 * n)}%"])
        got = json.loads(self.fm("cpu", "--window", "0", "--json", env=self.env).stdout)
        self.assertEqual(got["cap"]["pct"], 25)
        self.assertEqual(got["cap"]["unit"], "kitty-1-0.scope")
        self.cap("off")
        self.assertEqual(self.calls()[-1], "--user set-property --runtime kitty-1-0.scope CPUQuota=")
        self.assertIsNone(json.loads(self.fm("cpu", "--window", "0", "--json", env=self.env).stdout)["cap"])

    def test_cap_refuses_a_bad_percent_and_any_cgroup_but_a_session_scope(self):
        from sampo import cli
        for bad in ("0", "150", "x"):
            with self.subTest(bad=bad), self.assertRaises(cli.UsageError):
                self.cap(bad)
        manager = os.path.join(self.tmp, "cg", "user.slice", "user-1000.slice", "user@1000.service")
        for other in (os.path.join(self.tmp, "cg", "user.slice", "user-1000.slice", "session-3.scope"),  # not systemd user's
                      manager, os.path.join(manager, "app.slice"), None):  # every app under it; no cgroup v2
            self.own = other
            with self.subTest(other=other), self.assertRaises(c.PolicyError) as e:
                self.cap("25")
            self.assertIn("systemd user", str(e.exception))
        self.assertEqual(self.calls(), [], "nothing set")

    def test_cap_ignores_foreman_cgroup(self):
        self.own = None  # not a systemd session; FOREMAN_CGROUP names a scope that would pass
        with mock.patch.dict(os.environ, {"FOREMAN_CGROUP": self.cg}), self.assertRaises(c.PolicyError):
            self.cap("25")
        self.assertEqual(self.calls(), [])
