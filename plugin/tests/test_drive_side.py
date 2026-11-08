"""T-1176: the drive's side-task offer never hands a builder lane work a builder can't do (a brainstorm, a release),
and a task whose gates run in the background (meta gates_running with a live pid) is in progress, not a side task."""
import os
import subprocess

from test_hooks import HookCase

from sampo import core as c


class LaneHeld(HookCase):
    """T-1469: with every queued task held by a builder lane, the drive said 'fm focus T-x' each Stop, which fm focus
    refuses ('held by lane'); a lane's task is running work, said once."""

    def stop(self):
        return self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Waiting on the builders.",
                                  "session_id": "sess-1"}).stdout

    def test_a_task_a_lane_holds_is_not_offered_to_focus(self):
        self.fm("init")
        self.fm("autonomy", "full")
        tid = self.task("Lane work", focus=False)
        from sampo import cli
        cli.mutate(self.project(), tid, lambda x: x.meta.__setitem__("builder", c.now()), "lane_builder", {})
        first = self.stop()
        self.assertNotIn(f"fm focus {tid}", first)
        self.assertIn(tid, first)
        self.assertNotIn(tid, self.stop(), "said once, not on every Stop")

    def test_fm_next_skips_a_task_a_lane_holds(self):
        # T-1478: the Stop note's trailing "Next: T-x: fm focus T-x" (fm next) still named a lane's task
        self.fm("init")
        held = self.task("Lane work", focus=False)
        free = self.task("Free work", focus=False)
        from sampo import cli
        cli.mutate(self.project(), held, lambda x: x.meta.__setitem__("builder", c.now()), "lane_builder", {})
        out = self.fm("next").stdout
        self.assertNotIn(f"fm focus {held}", out)
        self.assertIn(free, out)


    def test_a_task_waiting_on_an_open_dependency_is_not_offered(self):
        # T-1708: T-1637 depends on T-1636, which a lane held; the Stop drive and fm next named T-1637 every Stop
        self.fm("init")
        self.fm("autonomy", "full")
        dep = self.task("Lane work", focus=False)
        waits = self.task("Waits on it", focus=False)
        free = self.task("Free work", focus=False)
        self.fm("task", "set", waits, f"depends_on=[{dep}]")
        from sampo import cli
        cli.mutate(self.project(), dep, lambda x: x.meta.__setitem__("builder", c.now()), "lane_builder", {})
        out = self.stop()
        self.assertNotIn(f"fm focus {waits}", out)
        self.assertIn(free, out)
        self.assertNotIn(f"fm focus {waits}", self.fm("next").stdout)


class DriveSide(HookCase):
    def stop(self, msg):
        return self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": msg, "session_id": "sess-1"})

    def reasons(self, n=3):
        self.hook("SubagentStart", {"agent_id": "a1", "agent_type": "sampo:fm-reviewer"})
        return [self.stop("Waiting for the review.").stdout for _ in range(n)]

    def offered(self, n=3):
        return " ".join(self.reasons(n))

    def test_research_and_release_get_no_lane(self):
        self.fm("init")
        research = self.task("Brainstorm ideas", type_="RESEARCH", focus=False)
        release = self.task("Release v2.0", focus=False)
        self.task("Current work")
        text = self.offered()
        self.assertIn(research, text)
        self.assertIn(release, text)
        self.assertNotIn("fm lane brief", text)

    def test_live_gate_run_is_not_offered_but_a_dead_one_is(self):
        self.fm("init")
        busy = self.task("Gates running", focus=False)
        self.task("Current work")
        p = self.project()
        c.update_meta(p, gates_running={"task": busy, "pid": os.getpid(), "at": c.now()})
        self.assertNotIn(f"Start {busy}", self.offered())
        done = subprocess.Popen(["true"])
        done.wait()
        c.update_meta(p, gates_running={"task": busy, "pid": done.pid, "at": c.now()})
        self.assertIn(f"Start {busy}", self.offered())

    def test_a_task_waiting_on_an_open_task_that_is_not_active_is_not_offered(self):
        # T-1899: T-1898 depended on T-1877 (queued, not active) and was offered as side work on every Stop
        self.fm("init")
        dep = self.task("First in the chain", focus=False)
        waits = self.task("Waits on it", focus=False)
        self.fm("task", "set", waits, f"depends_on=[{dep}]")
        self.task("Current work")
        text = self.offered()
        self.assertIn(f"Start {dep}", text)
        self.assertNotIn(f"Start {waits}", text)


class EconomyOffer(HookCase):  # T-2495: past 80% weekly (economy mode) a side task is main-thread prep, never a lane
    stop, reasons, offered = DriveSide.stop, DriveSide.reasons, DriveSide.offered  # its helpers, not its tests

    def usage(self, pct):
        import json
        import time
        d = os.path.join(self.home, "state", "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "snap.json"), "w") as f:  # a day left in the week: on pace, so side work is offered
            json.dump({"rate_limits": {"seven_day": {"used_percentage": pct, "resets_at": time.time() + 86400}}}, f)

    def test_past_80_percent_weekly_the_offer_drafts_in_the_scratchpad_instead_of_a_builder_lane(self):
        self.fm("init")
        side = self.task("Fix the parser", focus=False)
        self.task("Current work")
        self.usage(85)
        text = self.offered()
        self.assertIn(side, text)
        self.assertNotIn("fm lane brief", text)
        self.assertIn("scratchpad", text)

    def test_under_80_percent_the_builder_lane_offer_stays(self):
        self.fm("init")
        side = self.task("Fix the parser", focus=False)
        self.task("Current work")
        self.usage(40)
        self.assertIn(f"sampo lane brief {side}", self.offered())
