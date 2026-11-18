"""T-3536: sampo task evidence --run CMD --background returns at once and the detached run records the evidence as the
foreground path does; sampo task wait ID prints the result (exit 0 all passed, 1 any failed, 124 still running)."""
import json
import os
import re

from helpers import ForemanTestCase

BG = {"FOREMAN_NO_BACKGROUND": ""}  # the run really detaches here: each test waits it out before cleanup
GATED = "for i in $(seq 600); do [ -e {go} ] && break; sleep 0.1; done; [ -e {go} ] && echo {word}"  # ends on go


class Background(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = json.loads(self.fm("task", "new", "Slow check", "--type", "FIX", "--tier", "S",
                                      "--ac", "it holds :: true", "--step", "do it", "--json").stdout)["id"]
        self.fm("focus", self.tid)

    def bg(self, cmd):
        return self.fm("task", "evidence", self.tid, "--step", "1", "--run", cmd, "--background", env=BG)

    def gated(self, word):  # the go file lives outside the repo, so the gate itself never changes the tree
        return GATED.format(go=os.path.join(self.tmp, "go"), word=word)

    def go(self):
        open(os.path.join(self.tmp, "go"), "w").close()

    def evidence(self):
        return self.fm("task", "show", self.tid).stdout.split("## Verification evidence", 1)[1].split("\n## ", 1)[0]

    def test_it_returns_before_the_run_ends_and_wait_prints_the_recorded_pass(self):
        cmd = self.gated("green")
        p = self.bg(cmd)  # the run is still waiting for go, so the call came back before it ended
        self.assertIn(f"sampo task wait {self.tid}", p.stdout)
        self.assertEqual(len(p.stdout.strip().splitlines()), 1, p.stdout)
        self.assertNotIn("echo green", self.fm("task", "show", self.tid).stdout.split("## Verification evidence", 1)[1])
        self.go()
        w = self.fm("task", "wait", self.tid, "--timeout", "60", timeout=90)
        self.assertIn("evidence recorded (exit 0 · green)", w.stdout)
        self.assertIn("echo green", self.fm("task", "show", self.tid).stdout.split("## Verification evidence", 1)[1])
        self.assertIn("no background evidence runs", self.fm("task", "wait", self.tid).stdout)  # reported once

    def test_a_failing_run_makes_wait_exit_1_with_its_output(self):
        self.bg("echo boom; exit 3")
        w = self.fm("task", "wait", self.tid, "--timeout", "60", timeout=90, check=False)
        self.assertEqual(w.returncode, 1, w.stdout + w.stderr)
        self.assertIn("boom", w.stdout)
        self.assertIn("exit 3", w.stdout)

    def test_wait_gives_124_while_it_runs_then_the_result(self):
        self.bg(self.gated("late"))
        w = self.fm("task", "wait", self.tid, "--timeout", "0.5", check=False)
        self.assertEqual(w.returncode, 124, w.stdout + w.stderr)
        self.assertIn("still running", w.stdout)
        self.go()
        self.assertEqual(self.fm("task", "wait", self.tid, "--timeout", "60", timeout=90).returncode, 0)

    def test_background_needs_run(self):
        p = self.fm("task", "evidence", self.tid, "--step", "1", "true", "ok", "--background", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--run", p.stderr)

    def test_files_edited_during_the_run_keep_its_start_tree(self):
        self.fm("task", "evidence", self.tid, "--ac", "1", "--run", "true")
        before = re.search(r"\[tree (\w+)\]", self.evidence()).group(1)
        self.bg(self.gated("green"))
        with open(os.path.join(self.repo, "edited.py"), "w") as f:  # the builder keeps editing while it runs
            f.write("x = 1\n")
        self.go()
        self.fm("task", "wait", self.tid, "--timeout", "60", timeout=90)
        line = next(l for l in self.evidence().splitlines() if "echo green" in l)
        self.assertIn("files changed during the run", line)
        self.assertIn(f"[tree {before}]", line)

    def test_a_reused_pid_never_keeps_a_dead_run_pending(self):
        log = json.loads(self.fm("task", "evidence", self.tid, "--step", "1", "--run", "true", "--background", "--json",
                                 env=BG).stdout)["log"]
        self.fm("task", "wait", self.tid, "--timeout", "60", timeout=90)
        with open(os.path.join(os.path.dirname(log), "1.json"), "w") as f:  # this live pid, another process's start
            json.dump({"pid": os.getpid(), "start": "0", "cmd": "stale", "step": 1, "started": 0}, f)
        w = self.fm("task", "wait", self.tid, "--timeout", "5", check=False)
        self.assertEqual(w.returncode, 1, w.stdout + w.stderr)
        self.assertIn("ended without an exit code", w.stdout)
