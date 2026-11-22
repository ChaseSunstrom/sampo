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


class StuckKit:
    """The helpers every class below shares, as a mixin (not a test case itself): a class that subclassed another test
    class would run that class's tests again, subprocess hooks included."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def age_stamps(self, minutes=20):
        for n in os.listdir(c.calls_dir()):
            path = os.path.join(c.calls_dir(), n)
            rec = json.load(open(path))
            rec["at"] -= minutes * 60
            with open(path, "w") as f:
                json.dump(rec, f)

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


class StuckAgentCall(StuckKit, ForemanTestCase):
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

    def test_only_a_subagents_foreground_call_is_stamped(self):
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


class StuckAgentCallReview(StuckKit, ForemanTestCase):
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


class StuckAgentCallSecond(StuckKit, ForemanTestCase):
    """The second review: a tick from a sibling lane, a secret in the command, a tick that never waits on a notifier."""

    def lane(self, name):
        path = f"{self.repo}.lanes/{name}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", f"b-{name}", path], check=True)
        return path

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


class StuckAgentCallThird(StuckKit, ForemanTestCase):
    """The third review (2026-11-21): the minute tick's subset run leaves the other checks' state alone, and the tick
    runs on any PreToolUse (the main thread's too), after the guard has answered, and only with time left."""

    def act_state(self):
        with open(os.path.join(self.p.dir, "selfcheck-act.json"), encoding="utf-8") as f:
            return json.load(f)

    def run_acting(self, check, now, spawned, told):
        return selfcheck.act(self.p, [], now, checks=(check,), spawn=lambda p, argv: spawned.append(argv) or 1,
                             tell=lambda p, text: told.append(text))

    def test_a_subset_run_keeps_the_other_checks_acted_and_first(self):
        def landings(p, briefs, now):
            return selfcheck.Hit("lanes wait", action=["true"])
        now, spawned, told = time.time(), [], []
        self.run_acting(landings, now, spawned, told)
        self.run_acting(selfcheck.stuck_agent_calls, now + 60, spawned, told)  # the minute tick runs one check only
        st = self.act_state()["landings"]
        self.assertEqual(st.get("acted"), [now], "the action ledger of a check the tick did not run stays")
        self.assertEqual(st.get("first"), now, "and so does when its finding began")
        self.run_acting(landings, now + 600, spawned, told)
        self.assertEqual(len(spawned), 1, "the 30-minute backoff still holds after a tick")
        self.run_acting(selfcheck.stuck_agent_calls, now + selfcheck.ESCALATE_S, spawned, told)
        self.run_acting(landings, now + selfcheck.ESCALATE_S + 1, spawned, told)
        self.assertEqual(len(told), 1, "a finding that outlives 2 h escalates to a tell though a tick ran in between")

    def test_a_check_that_ran_and_found_nothing_is_still_reset(self):
        firing = [True]

        def flicker(p, briefs, now):
            return selfcheck.Hit("flicker", action=["true"]) if firing[0] else None
        now, spawned, told = time.time(), [], []
        self.run_acting(flicker, now, spawned, told)
        self.assertIn("acted", self.act_state()["flicker"])
        firing[0] = False
        self.run_acting(flicker, now + 60, spawned, told)
        st = self.act_state()["flicker"]
        self.assertNotIn("acted", st, "resolved: a new finding starts fresh")
        self.assertNotIn("first", st)

    def test_a_lone_hang_is_told_by_the_main_threads_next_call(self):
        self.pre()  # agent-a's call: the only subagent, so no other subagent call will tick
        self.age_stamps()
        try:
            os.remove(os.path.join(self.p.dir, "stuck.tick"))  # a minute on
        except OSError:
            pass
        self.pre(tid="toolu_main", agent=None, cmd="ls")  # the parent keeps working: its own call is the trigger
        alerts = os.path.join(self.p.dir, "alerts.jsonl")
        self.assertTrue(os.path.exists(alerts) and "agent-a" in open(alerts).read())

    def raw(self, **extra):
        return json.dumps(dict({"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "toolu_t",
                                "cwd": self.repo}, **extra))

    def with_a_stamp(self):
        c.stamp_call("toolu_x", agent="agent-a", command=CMD, cwd=self.repo, tool="Bash")

    def test_the_tick_runs_after_the_guard_has_answered(self):
        from sampo import hooks
        self.with_a_stamp()
        order = []
        with mock.patch.object(hooks, "_pre_tool_use", lambda raw: order.append("guard") or 0), \
                mock.patch.object(hooks, "_stuck_tick", lambda pl: order.append("tick")):
            code = hooks._pre_typed(self.raw())
        self.assertEqual((code, order), (0, ["guard", "tick"]))
        order.clear()
        with mock.patch.object(hooks, "_pre_tool_use", lambda raw: order.append("guard") or 0), \
                mock.patch.object(hooks, "_stuck_tick", lambda pl: order.append("tick")):
            hooks._pre_typed(self.raw(agent_id="agent-b"))
        self.assertEqual(order, ["guard", "tick"], "a subagent's own call too")

    def test_the_tick_gives_way_when_the_hook_has_no_time_left(self):
        from sampo import hooks
        self.with_a_stamp()
        ticks = []
        try:
            with mock.patch.object(hooks, "_pre_tool_use", lambda raw: 0), \
                    mock.patch.object(hooks, "_stuck_tick", lambda pl: ticks.append(pl)), \
                    mock.patch.object(hooks, "_left", lambda: 0.2):
                self.assertEqual(hooks._pre_typed(self.raw()), 0)
            self.assertEqual(ticks, [], "a guard that took the deadline leaves no time for a git call")
            self.assertIn("stuck_tick", hooks._TIMING.get("ran_out", []), "the skipped part is named in the hook's row")
        finally:
            hooks._TIMING.clear()

    def test_a_tick_cut_by_the_deadline_never_costs_the_guards_answer(self):
        from sampo import hooks

        def cut(pl):
            raise hooks._OutOfTime
        self.with_a_stamp()
        try:
            with mock.patch.object(hooks, "_pre_tool_use", lambda raw: 0), mock.patch.object(hooks, "_stuck_tick", cut):
                self.assertEqual(hooks._pre_typed(self.raw()), 0)
            self.assertIn("stuck_tick", hooks._TIMING.get("ran_out", []))
        finally:
            hooks._TIMING.clear()

    def test_no_call_stamp_no_tick(self):
        from sampo import hooks
        ticks = []
        with mock.patch.object(hooks, "_pre_tool_use", lambda raw: 0), \
                mock.patch.object(hooks, "_stuck_tick", lambda pl: ticks.append(pl)):
            hooks._pre_typed(self.raw())
        self.assertEqual(ticks, [], "nothing to be stuck: an ordinary call pays one directory listing, no project lookup")

    def test_a_stamp_without_a_cwd_is_no_projects_hang(self):
        c.stamp_call("toolu_e", agent="agent-z", command=CMD, cwd="", tool="Bash")
        self.age_stamps()
        self.assertIsNone(self.stuck(20), "it would show in every project")
