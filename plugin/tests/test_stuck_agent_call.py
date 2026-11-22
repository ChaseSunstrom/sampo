"""T-5045: a builder subagent stuck in one tool call is noticed within minutes. The PreToolUse hook leaves a small stamp
for a subagent's foreground Bash call (agent, command, start); PostToolUse and the agent's end remove it; the
stuck_agent_calls self-check names a call running past the limit with agent, command, age and what to do. Nothing here
types into a terminal: these stamps are not the typing stamps (typed_refuse never reads them)."""
import json
import os
import subprocess
import time
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import selfcheck

CMD = "python3 plugin/tests/run.py test_slow"


class StuckAgentCall(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def pre(self, tid="toolu_1", agent="agent-a", cmd=CMD, tool="Bash", **extra):
        pl = {"tool_name": tool, "tool_input": {"command": cmd, **extra.pop("ti", {})} if cmd else {}, "tool_use_id": tid}
        if agent:
            pl["agent_id"] = agent
        pl.update(extra)
        r = self.hook("PreToolUse", pl)
        self.assertEqual(r.returncode, 0, r.stderr)

    def post(self, event="PostToolUse", tid="toolu_1", **extra):
        r = self.hook(event, {"tool_name": "Bash", "tool_input": {"command": CMD}, "tool_use_id": tid,
                              "tool_response": {"stdout": "ok"}, **extra})
        self.assertEqual(r.returncode, 0, r.stderr)

    def stuck(self, minutes):
        return selfcheck.stuck_agent_calls(self.p, [], time.time() + minutes * 60)

    def test_a_subagent_call_past_the_limit_is_named_with_agent_command_age_and_action(self):
        self.pre()
        self.assertIsNone(self.stuck(5), "a call five minutes in is an ordinary test run")
        hit = self.stuck(20)
        self.assertIn("agent-a", hit)
        self.assertIn(CMD, hit)
        self.assertRegex(hit, r"2[01] min")
        self.assertRegex(hit, r"message it|stop it")
        self.assertTrue(hit.tell, "the user is told once: it is a hung builder")

    def test_it_reaches_the_drive_line_and_the_tick_on_any_project(self):
        self.pre()
        old = time.time() + 20 * 60
        self.assertTrue(any("agent-a" in str(x) for x in selfcheck.lines(self.p, [], old)),
                        "lines() is what the drive and sampo next print")
        told = []
        out = selfcheck.act(self.p, [], old, checks=(selfcheck.stuck_agent_calls,), tell=lambda p, text: told.append(text))
        self.assertEqual([o[:2] for o in out], [("stuck_agent_calls", "tell")])
        self.assertIn("agent-a", told[0])

    def test_a_finished_call_leaves_nothing(self):
        self.pre()
        self.post()
        self.assertIsNone(self.stuck(60))
        self.pre(tid="toolu_2")
        self.post("PostToolUseFailure", tid="toolu_2")
        self.assertIsNone(self.stuck(60))

    def test_the_end_of_the_agent_clears_a_call_no_post_reported(self):
        self.pre()
        self.pre(tid="toolu_9", agent="agent-b")
        r = self.hook("SubagentStop", {"agent_id": "agent-a"})
        self.assertEqual(r.returncode, 0, r.stderr)
        hit = self.stuck(60)
        self.assertIn("agent-b", hit)
        self.assertNotIn("agent-a", hit)

    def test_only_a_subagents_foreground_bash_call_is_stamped(self):
        self.pre(agent=None)  # the main thread: its own turn is on screen
        self.pre(tid="toolu_3", ti={"run_in_background": True})  # returns at once
        self.assertIsNone(self.stuck(60))
        self.assertFalse(os.path.isdir(c.calls_dir()) and os.listdir(c.calls_dir()))

    def test_the_stamp_holds_a_bounded_command(self):
        self.pre(cmd="echo " + "x" * 5000)
        rec = json.load(open(os.path.join(c.calls_dir(), os.listdir(c.calls_dir())[0])))
        self.assertLessEqual(len(rec["cmd"]), 400)
        self.assertEqual(rec["agent"], "agent-a")

    def test_a_call_the_guard_refuses_leaves_no_stamp(self):
        r = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "tool_use_id": "toolu_d",
                                     "agent_id": "agent-a"})
        self.assertTrue(r.returncode == 2 or "deny" in r.stdout, f"the guard must refuse this call: {r.stdout}")
        self.assertIsNone(self.stuck(60), "it never ran, so no Post will come for it")

    def test_another_projects_call_is_not_named_here_but_a_stale_or_unreadable_stamp_is_harmless(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        self.pre(cwd=elsewhere)
        os.makedirs(c.calls_dir(), exist_ok=True)
        with open(os.path.join(c.calls_dir(), "junk"), "w") as f:
            f.write("{not json")
        self.assertIsNone(self.stuck(60))

    def test_a_call_from_before_a_reboot_is_no_call_at_all(self):
        self.pre()
        n = os.listdir(c.calls_dir())[0]
        path = os.path.join(c.calls_dir(), n)
        rec = json.load(open(path))
        rec["boot"] = "some-other-boot"
        with open(path, "w") as f:
            json.dump(rec, f)
        self.assertIsNone(self.stuck(60))

    def test_the_typing_rule_is_untouched_by_these_stamps(self):
        self.pre()
        self.assertIsNone(c.typed_refuse(), "a plain test run types nothing: no switch refused")


class StuckAgentCallReview(StuckAgentCall):
    """The review's findings: a lane folder beside the repo, a second hang the same day, any tool, and a trigger."""

    def test_a_builder_in_a_lanes_sibling_folder_is_named(self):
        lane = self.repo + ".lanes/T-1"
        os.makedirs(lane)
        self.pre(cwd=lane)
        self.assertIn("agent-a", self.stuck(20), "sampo lane new puts lanes in <repo>.lanes/")

    def test_a_symlinked_root_still_matches(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.repo, link)
        os.makedirs(os.path.join(self.repo, "sub"), exist_ok=True)
        self.pre(cwd=os.path.join(link, "sub"))
        self.assertIn("agent-a", self.stuck(20))

    def test_a_second_separate_hang_the_same_day_is_told_again(self):
        told = []
        run = lambda t: selfcheck.act(self.p, [], t, checks=(selfcheck.stuck_agent_calls,),
                                      tell=lambda p, text: told.append(text))
        self.pre()
        first = run(time.time() + 20 * 60)
        self.post()
        self.assertEqual(run(time.time() + 21 * 60), [])  # resolved
        time.sleep(1.1)
        self.pre(tid="toolu_9", agent="agent-b")
        second = run(time.time() + 40 * 60)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1, "a new hang is told even within the day")
        self.assertIn("agent-b", told[1])

    def test_any_tool_is_stamped_and_named_without_a_command(self):
        self.pre(tid="toolu_m", cmd="", tool="mcp__x__fetch")
        hit = self.stuck(20)
        self.assertIn("mcp__x__fetch", hit)

    def test_a_nested_agent_call_is_not_a_hang(self):
        self.pre(tid="toolu_n", cmd="", tool="Agent")
        self.assertIsNone(self.stuck(60))

    def test_another_subagents_call_tells_when_a_hang_exists(self):
        self.pre()
        n = os.listdir(c.calls_dir())[0]
        path = os.path.join(c.calls_dir(), n)
        rec = json.load(open(path))
        rec["at"] -= 20 * 60
        with open(path, "w") as f:
            json.dump(rec, f)
        os.remove(os.path.join(self.p.dir, "stuck.tick"))  # a minute on
        self.pre(tid="toolu_o", agent="agent-b")  # the hook is the trigger: no Stop, no timer
        alerts = os.path.join(self.p.dir, "alerts.jsonl")
        self.assertTrue(os.path.exists(alerts) and "agent-a" in open(alerts).read())


FAKE_TOKEN = "fake-token-0123456789abcdef-not-real"


class StuckAgentCallSecond(ForemanTestCase):
    """The second review: a tick from a sibling lane, a secret in the command, a tick that never waits on a notifier."""

    pre = StuckAgentCall.pre

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def lane(self, name):
        path = f"{self.repo}.lanes/{name}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", f"b-{name}", path], check=True)
        return path

    def age_stamps(self, minutes=20):
        for n in os.listdir(c.calls_dir()):
            path = os.path.join(c.calls_dir(), n)
            rec = json.load(open(path))
            rec["at"] -= minutes * 60
            with open(path, "w") as f:
                json.dump(rec, f)

    def test_a_tick_from_one_lane_names_a_hang_in_a_sibling_lane(self):
        x, y = self.lane("T-1"), self.lane("T-2")
        self.pre(cwd=x)  # agent-a hangs in lane X
        self.age_stamps()
        os.remove(os.path.join(self.p.dir, "stuck.tick"))  # a minute on
        self.pre(tid="toolu_b", agent="agent-b", cwd=y)  # agent-b's call in lane Y ticks the check
        alerts = os.path.join(self.p.dir, "alerts.jsonl")
        self.assertTrue(os.path.exists(alerts) and "agent-a" in open(alerts).read(), "the tick named a sibling lane's hang")

    def test_the_check_run_for_a_lane_project_scans_the_whole_repo(self):
        x, y = self.lane("T-1"), self.lane("T-2")
        self.pre(cwd=x)
        pl = c.find_project(y)
        self.assertEqual(os.path.realpath(pl.root), os.path.realpath(y))
        self.assertIn("agent-a", selfcheck.stuck_agent_calls(pl, [], time.time() + 20 * 60))

    def test_a_secret_in_the_command_reaches_neither_the_stamp_nor_the_alert(self):
        cmd = f"curl -H 'Authorization: Bearer {FAKE_TOKEN}' https://example.invalid/x"
        self.pre(cmd=cmd)
        raw = "".join(open(os.path.join(c.calls_dir(), n)).read() for n in os.listdir(c.calls_dir()))
        self.assertNotIn(FAKE_TOKEN, raw)
        hit = str(selfcheck.stuck_agent_calls(self.p, [], time.time() + 20 * 60))
        self.assertIn("curl", hit)
        self.assertNotIn(FAKE_TOKEN, hit)
        self.age_stamps()
        told = []
        selfcheck.act(self.p, [], time.time(), checks=(selfcheck.stuck_agent_calls,), tell=lambda p, t: told.append(t))
        self.assertTrue(told and FAKE_TOKEN not in told[0])

    def test_an_old_stamp_with_a_secret_is_redacted_when_read(self):
        self.pre()
        self.age_stamps()
        n = os.listdir(c.calls_dir())[0]
        path = os.path.join(c.calls_dir(), n)
        rec = json.load(open(path))
        rec["cmd"] = f"curl -H 'Authorization: Bearer {FAKE_TOKEN}' x"
        with open(path, "w") as f:
            json.dump(rec, f)
        self.assertNotIn(FAKE_TOKEN, str(selfcheck.stuck_agent_calls(self.p, [], time.time())))

    def test_the_hook_tick_never_runs_the_synchronous_notifier(self):
        from sampo import hooks, serve
        c.update_meta(self.p, notify="echo told")  # the user's command: the slow path
        self.pre()
        self.age_stamps()
        os.remove(os.path.join(self.p.dir, "stuck.tick"))
        calls = []
        spawned = []
        with mock.patch.object(serve, "_notify", lambda *a, **k: calls.append(a)), \
                mock.patch.object(selfcheck, "notify", lambda *a, **k: calls.append(a)), \
                mock.patch.object(selfcheck, "deliver", lambda *a, **k: calls.append(a)), \
                mock.patch.object(selfcheck, "_spawn", lambda p, argv: spawned.append(argv)), \
                mock.patch.object(subprocess, "run", lambda *a, **k: calls.append(a)):
            hooks._stuck_tick({"cwd": self.repo})
        self.assertEqual(calls, [], "the hook never waits on a notifier")
        self.assertEqual(len(spawned), 1, "delivery goes to one detached child")
        self.assertIn("agent-a", spawned[0][-1])
        alerts = os.path.join(self.p.dir, "alerts.jsonl")
        self.assertTrue(os.path.exists(alerts) and "agent-a" in open(alerts).read())
