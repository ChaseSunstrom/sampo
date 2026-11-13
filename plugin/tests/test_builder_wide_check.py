"""T-4514: a builder agent never starts the wide check (sampo check --affected, the full sampo check): the main thread
runs the affected one at sampo lane accept, in a one-at-a-time queue, and a builder's copy doubled a 30 to 50 minute run
(three builders on 2026-11-12). The refusal is the PreToolUse hook's, keyed on the one thing only a builder's own Bash
call carries: an agent_id with the fm-builder agent_type. The CLI refuses nothing by where it runs (a lane is also the
train's, the accept check's and the main thread's place, and every lane fixture in the tests). A second check of the
same kind on the same tree while one runs is refused with the first one's pid and log path (a flock beside the project's
state, never a process-list pattern)."""
# map-links: plugin/lib/sampo/hooks.py, plugin/lib/sampo/cli.py, plugin/lib/sampo/core.py
import json
import os
import subprocess
import sys
import time
import unittest

from test_lane_train import TrainCase

from helpers import FM
from sampo import core as c
from sampo import train


class Calm(TrainCase):
    def setUp(self):
        # the accept check that runs these tests has SAMPO_CHECK_LOG set (test_run.HARNESS_ENV lets tests inherit it)
        # (core.mirror_env turned the SAMPO_ names into FOREMAN_ ones at import)
        for k in ("CHECK_LOG", "LANE_CHILD"):
            for pre in ("SAMPO_", "FOREMAN_"):
                if pre + k in os.environ:
                    self.addCleanup(os.environ.__setitem__, pre + k, os.environ.pop(pre + k))
        super().setUp()


WIDE = ("sampo check --affected", "sampo check", "fm check --fresh", "sampo check --affected --hold --evidence T-1",
        "python3 plugin/bin/sampo check --affected --json", "sampo quiet -- sampo check --affected",
        "FOREMAN_LOAD_GATE=0 sampo check run", "sampo check --since HEAD~1 --affected")
NARROW = ("python3 plugin/tests/run.py test_x -k name", "sampo check add 'true'", "sampo check list", "sampo check --help",
          "sampo check paths", "sampo task evidence T-1 --step 1 --run \"python3 plugin/tests/run.py test_x\"",
          "git status")


class BuilderHook(Calm):
    def setUp(self):
        super().setUp()
        self.tid = self.lane(1, "a.txt")
        self.path = os.path.join(self.tmp, "lane1")

    def pre(self, cmd, **ids):
        return self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": self.path,
                                        "session_id": "sess-b", **ids})

    def builder(self, cmd):
        return self.pre(cmd, agent_id="a42cb51b932d1111a", agent_type="sampo:fm-builder")

    def test_a_builders_own_wide_check_is_refused_with_one_line_naming_the_targeted_form(self):
        for cmd in WIDE:
            r = self.builder(cmd)
            self.assertEqual(r.returncode, 2, (cmd, r.stdout, r.stderr))
            why = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertEqual(len(why.splitlines()), 1, why)
            for need in ("main thread", "sampo lane accept", "plugin/tests/run.py", "-k"):
                self.assertIn(need, why, cmd)

    def test_the_same_calls_from_the_main_thread_a_reviewer_or_a_headless_run_are_not_refused(self):
        for cmd in WIDE:
            for ids in ({}, {"agent_id": "a42cb51b932d2222a", "agent_type": "sampo:fm-reviewer"},
                        {"agent_type": "sampo:fm-builder"}):  # a builder type without an agent_id is no subagent's call
                r = self.pre(cmd, **ids)
                self.assertNotIn("sampo lane accept", r.stdout + r.stderr, (cmd, ids))

    def test_a_builders_own_test_runs_and_the_check_setup_verbs_are_not_refused(self):
        for cmd in NARROW:
            r = self.builder(cmd)
            self.assertNotIn("sampo lane accept", r.stdout + r.stderr, cmd)

    def test_a_check_run_in_a_lane_by_the_cli_is_never_refused(self):
        # the train, the accept check, the main thread and every lane fixture run it here: no agent_id, no refusal
        r = self.fm("check", "--affected", cwd=self.path, check=False)
        self.assertNotEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("main thread", r.stderr)
        self.assertEqual(self.fm("check", cwd=self.path, check=False).returncode, 0)
        self.assertEqual(self.gate_runs(), 1)

    def test_the_accept_checks_run_carries_its_stdout_log(self):
        seen = {}

        class P:  # a Popen that records the environment it was started with
            pid, returncode = 1, 0

            def __init__(self, argv, **kw):
                seen.update(kw["env"])

            def communicate(self, timeout=None):
                return "", None
        from unittest import mock
        with mock.patch.object(train.subprocess, "Popen", P), mock.patch.object(c, "stdout_log", return_value="/x/accept.log"):
            train._run_check(["true"], self.path)
        self.assertEqual(seen.get("SAMPO_LANE_CHILD"), "1")
        self.assertEqual(seen.get("SAMPO_CHECK_LOG"), "/x/accept.log")


class SecondCheck(Calm):
    def setUp(self):
        super().setUp()
        self.p = c.find_project(self.repo)

    def test_a_second_check_of_the_same_kind_is_refused_with_the_first_ones_pid_and_log(self):
        log = os.path.join(self.tmp, "first.log")
        for kind, argv in (("full", ["check"]), ("affected", ["check", "--affected"])):
            with c.check_claim(self.p, kind, log=log):
                r = self.fm(*argv, check=False)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn(f"pid {os.getpid()}", r.stderr)
                self.assertIn(log, r.stderr)
                self.assertEqual(len([ln for ln in r.stderr.splitlines() if "refused" in ln]), 1, r.stderr)
        self.assertEqual(self.gate_runs(), 0, "the refused run started nothing")

    def test_a_second_check_of_another_kind_or_on_another_tree_is_not_refused(self):
        with c.check_claim(self.p, "full", log=None):
            r = self.fm("check", "--affected", check=False)  # (it may fail on its own: nothing linked to run)
            self.assertNotIn("already running", r.stderr)
            self.assertNotEqual(r.returncode, 2, r.stderr)
            other = c.Project(self.p.slug, os.path.join(self.tmp, "elsewhere"), self.p.dir)
            with c.check_claim(other, "full", log=None):  # a different tree: its own claim
                pass

    def test_a_second_check_runs_once_the_first_is_over_even_if_it_died_without_cleaning_up(self):
        with c.check_claim(self.p, "full", log=None):
            pass
        self.assertEqual(self.fm("check", "--fresh").returncode, 0)
        self.assertEqual(self.gate_runs(), 1)
        # a claim file left by a process that is gone holds nothing: the flock died with it
        folder = os.path.join(self.p.dir, "check-claims")
        for name in os.listdir(folder):
            with open(os.path.join(folder, name), "w") as f:
                json.dump({"pid": 2 ** 22 + 1, "log": "/gone.log"}, f)
        self.assertEqual(self.fm("check", "--fresh").returncode, 0)
        self.assertEqual(self.gate_runs(), 2)

    def test_a_second_real_check_started_while_the_first_one_runs_is_refused_with_its_pid(self):
        release = os.path.join(self.repo, "RELEASE")
        self.fm("check", "add", "while [ ! -e RELEASE ]; do sleep 0.1; done")
        log = os.path.join(self.tmp, "first.out")
        env = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", FOREMAN_DOCTOR_BENCH="0")
        with open(log, "w") as out:
            first = subprocess.Popen([sys.executable, FM, "check"], cwd=self.repo, env=env, stdout=out, stderr=out,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            folder = os.path.join(self.p.dir, "check-claims")
            deadline = time.time() + 60
            while time.time() < deadline and not (os.path.isdir(folder) and os.listdir(folder)):
                time.sleep(0.1)
            time.sleep(0.5)
            r = self.fm("check", check=False)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn(f"pid {first.pid}", r.stderr)
            self.assertIn(log, r.stderr, "the first run's stdout file is its log")
        finally:  # tearDown runs before cleanups and removes the repo: free the first run here
            open(release, "a").close()
            try:
                first.wait(60)
            except subprocess.TimeoutExpired:
                first.kill()
                first.wait()


class ContractText(unittest.TestCase):
    def test_the_builder_contract_says_own_modules_only_and_leaves_the_accept_check_to_the_main_thread(self):
        from sampo import lanes as slanes
        agent = os.path.join(os.path.dirname(__file__), "..", "agents", "fm-builder.md")
        with open(agent, encoding="utf-8") as f:
            text = f.read()
        for t in (text, slanes.contract("T-9", "abc", "/m")):
            self.assertIn("Never run `sampo check --affected` or the full `sampo check`", t)
            self.assertIn("sampo lane accept", t)
            self.assertIn("-k", t)
            self.assertNotIn("Run it after your last commit", t)
            self.assertNotIn("add `--hold`", t)


if __name__ == "__main__":
    unittest.main()
