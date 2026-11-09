"""T-2369 (Mission 6, #5): a capability miss during an active task is that task's to find-build-test-use, and
fm task finish refuses the task while the miss is unresolved. Resolved only by run evidence, never by a typed line."""
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c


class MissLoop(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "Use gizmo", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        self.p = c.find_project(self.repo)
        self.tid = c.load_briefs(self.p)[-1].id

    def miss(self, cmd="true gizmo", err="bash: gizmo: command not found", sid="s1"):
        self.hook("PostToolUseFailure", {"tool_name": "Bash", "tool_input": {"command": cmd}, "error": err,
                                         "session_id": sid})
        time.sleep(1.05)  # timestamps are whole seconds and a run counts only when strictly later than the miss

    def git(self, *a):
        subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True)

    def passed(self, cmd, sid="s1"):
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": sid,
                                  "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False}})

    def finish(self):
        return self.fm("task", "finish", self.tid, "--audit", "self", "--run", "true", check=False)

    def refused(self, why=""):
        r = self.finish()
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("capability miss", r.stderr + r.stdout, why)
        self.assertEqual(c.find_brief(self.p, self.tid).status, "active")

    def closes(self):
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_a_miss_is_logged_with_the_task_and_the_command_and_fm_next_names_it(self):
        self.miss("FOO=1 true gizmo --x")
        (e,) = [e for e in c.ledger_events(self.p, ["capability_miss"])]
        self.assertEqual((e["task"], e["data"]["name"], e["data"]["kind"]), (self.tid, "gizmo", "command"))
        self.assertIn("true gizmo --x", e["data"]["cmd"])
        nxt = self.fm("next").stdout
        self.assertIn("open miss `gizmo`", nxt)
        self.assertIn("fm which", nxt)

    def test_a_command_not_found_in_an_fm_task_evidence_run_is_a_miss_with_no_hook(self):
        # T-2370: fm ran it, so every host feeds the miss path; the same command run green later resolves it
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "gizmo_zz --go", check=False)
        (e,) = c.ledger_events(self.p, ["capability_miss"])
        self.assertEqual((e["task"], e["data"]["name"], e["data"]["kind"], e["data"]["cmd"]),
                         (self.tid, "gizmo_zz", "command", "gizmo_zz --go"))
        self.refused()
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "echo ok", check=False)
        self.assertEqual(len(c.ledger_events(self.p, ["capability_miss"])), 1, "a red run with no miss logs none")

    def test_a_task_with_no_miss_pays_nothing_and_finishes(self):
        self.assertNotIn("open miss", self.fm("next").stdout)
        self.closes()

    def test_finish_refuses_on_every_try_and_a_later_green_run_of_the_same_command_closes_it(self):
        self.miss()
        for _ in range(3):
            self.refused()
        self.hook("PostToolUseFailure", {"tool_name": "Bash", "tool_input": {"command": "true gizmo"},
                                         "error": "Exit code 1\nboom"})
        self.refused("a red run resolves nothing")
        self.passed("cd sub && true  gizmo")  # where it ran and spacing are not what it was
        self.closes()

    def test_a_run_before_the_miss_does_not_count_and_a_second_miss_reopens_it(self):
        self.passed("true gizmo")
        self.miss()
        self.refused()
        self.passed("true gizmo")
        self.miss()
        self.refused("the newest miss is the open one")

    def test_an_fm_run_closes_it_but_a_red_as_expected_or_typed_run_does_not(self):
        self.miss("false gizmo")
        self.fm("task", "evidence", self.tid, "--step", "1", "--expect-fail", "--run", "false gizmo")
        self.refused("an expect-fail run")
        self.fm("task", "evidence", self.tid, "--step", "1", "false gizmo", "exit 0 · 3 passed [ran]")
        self.refused("a typed line, whatever it says")
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "true gizmo", check=False)
        self.refused("another command")
        self.miss("true gizmo")
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "true gizmo")
        self.refused("the false one is still open")
        self.miss("false gizmo")
        self.passed("false gizmo")
        self.closes()

    def test_the_command_fmfixes_pairs_with_the_failure_closes_it_when_the_tool_is_there_now(self):
        self.miss("git fnord", "bash: git: command not found")  # git is on this machine; ls is no pairing subject
        self.refused()
        self.passed("git ls-files")
        (e,) = c.ledger_events(self.p, ["fix_paired"])
        self.assertEqual(e["task"], self.tid)
        self.closes()

    def test_a_pairing_resolves_nothing_while_the_tool_is_absent_or_the_command_is_a_no_op(self):
        self.miss("gizmo --x")
        self.passed("python3 tools/gizmo.py --x")
        self.assertTrue(c.ledger_events(self.p, ["fix_paired"]), "the pairing is logged")
        self.refused("gizmo is still not on the machine")
        self.miss("touch a", "bash: touch: command not found")
        self.passed("touch b")
        self.refused("touch pairs with anything")
        self.assertIn("`touch`", self.finish().stderr)

    def test_a_pass_resolves_only_from_this_tasks_sessions_and_never_a_subagent(self):
        self.miss()
        self.passed("true gizmo", sid="another-lane")
        self.refused("a session with no event on this task")
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "true gizmo"}, "session_id": "s1",
                                  "agent_id": "a1", "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False}})
        self.refused("a subagent's run")
        self.passed("true gizmo", sid="s1")
        self.closes()

    def test_the_whole_command_is_compared_not_a_prefix_and_a_leading_cd_is_not_part_of_it(self):
        long = "true gizmo " + "x" * 150
        self.miss(long + " --one")
        self.passed(long + " --two")
        self.refused("same first 120 characters, another command")
        self.passed("cd sub && cd .. && " + long + " --one")
        self.closes()

    def test_a_resolving_pass_is_a_ledger_event_and_readers_never_scan_events(self):
        from sampo import miss as smiss
        self.miss()
        self.passed("true gizmo")
        (e,) = c.ledger_events(self.p, ["miss_resolved"])
        self.assertEqual((e["task"], e["data"]["name"]), (self.tid, "gizmo"))
        with mock.patch.object(c, "global_events", side_effect=AssertionError("no events scan")):
            self.assertEqual(smiss.open_misses(self.p, c.find_brief(self.p, self.tid)), [])
            self.miss("false gizmo")
            self.assertEqual([m["name"] for m in smiss.open_misses(self.p, c.find_brief(self.p, self.tid))], ["gizmo"])

    def test_the_refusal_prints_the_whole_command_and_the_way_to_run_it_green(self):
        self.miss("true gizmo --flag " + "y" * 200)
        r = self.finish()
        self.assertIn("y" * 200, r.stderr)
        self.assertIn(f"fm task evidence {self.tid} --step N --run", r.stderr)

    def test_a_tool_proved_event_naming_it_closes_it_but_another_name_does_not(self):
        self.miss()
        c.log_event(self.p, "tool_proved", task=self.tid, data={"name": "other"})
        self.refused()
        c.log_event(self.p, "tool_proved", task=self.tid, data={"name": "gizmo"})
        self.closes()

    def test_a_queued_grant_closes_it_only_with_a_green_run_of_the_fallback_step(self):
        self.miss()
        c.log_event(self.p, "grant_queued", task=self.tid, data={"name": "gizmo", "step": 1})
        self.refused("the grant alone")
        time.sleep(1.05)
        self.fm("task", "evidence", self.tid, "--step", "1", "--expect-fail", "--run", "false")
        self.refused("a red fallback run")
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "true")
        self.closes()

    def test_no_file_resolves_a_miss_however_it_is_named(self):
        # the criterion: only a green run, a proved tool or a queued grant plus its fallback close one
        self.miss("python3 app.py", "ModuleNotFoundError: No module named 'gizmolib'")
        os.makedirs(os.path.join(self.repo, "x"))
        for f in ("gizmolib.py", "x/gizmolib.py"):
            with open(os.path.join(self.repo, f), "w") as h:
                h.write("X = 1\n")
        self.assertIn("open miss `gizmolib`", self.fm("next").stdout)
        self.refused("a file of that name")
        self.miss()
        with open(os.path.join(self.repo, "gizmo.py"), "w") as f:
            f.write("X = 1\n")
        self.refused()

    def test_a_module_the_project_already_tracks_is_a_wrong_runner_not_a_miss(self):
        # T-4061: python3 -m unittest plugin.tests.test_x from the root can't import plugin/tests/helpers.py; 16 of 18
        # logged misses were this repo's own modules, each blocking a close until that exact command passed
        os.makedirs(os.path.join(self.repo, "tests"))
        with open(os.path.join(self.repo, "tests", "helpers.py"), "w") as f:
            f.write("X = 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "helpers")
        self.miss("python3 -m unittest tests.test_x", "ModuleNotFoundError: No module named 'helpers'")
        self.assertEqual(c.ledger_events(self.p, ["capability_miss"]), [], "tracked before the run: no miss")
        self.closes()

    def test_a_miss_logged_for_a_module_tracked_at_the_tasks_start_does_not_block(self):
        os.makedirs(os.path.join(self.repo, "lib", "pkg"))
        with open(os.path.join(self.repo, "lib", "pkg", "__init__.py"), "w") as f:
            f.write("")
        self.git("add", "-A")
        self.git("commit", "-qm", "pkg")
        self.fm("task", "new", "Use pkg", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        self.tid = c.load_briefs(self.p)[-1].id
        for name in ("pkg", "gizmolib"):  # as an older hook logged it; gizmolib isn't the project's
            c.log_event(self.p, "capability_miss", task=self.tid, data={"name": name, "cmd": f"python3 -m {name}",
                                                                         "kind": "module"})
        from sampo import miss
        self.assertEqual([m["name"] for m in miss.open_misses(self.p, c.find_brief(self.p, self.tid))], ["gizmolib"])

    def test_a_miss_logged_on_another_task_or_none_does_not_block_this_one(self):
        c.log_event(self.p, "capability_miss", task="T-9999", data={"name": "gizmo", "cmd": "gizmo", "kind": "command"})
        c.log_event(self.p, "capability_miss", task=None, data={"name": "gizmo", "cmd": "gizmo", "kind": "command"})
        self.closes()

    def test_done_refuses_too_so_it_is_not_a_way_round(self):
        self.miss()
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "true")
        r = self.fm("task", "done", self.tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("capability miss", r.stderr + r.stdout)

    def test_block_is_the_way_out_without_closing(self):
        self.miss()
        self.fm("task", "block", self.tid, "gizmo cannot be installed here")
        self.assertEqual(c.find_brief(self.p, self.tid).status, "blocked")


if __name__ == "__main__":
    unittest.main()
