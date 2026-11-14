"""T-4611: a Stop while one background job (a CI watcher) runs lets the turn end on 'the drive waits for background
work' only when no queued task could move meanwhile. JARVIS idled 5+ minutes on a watcher with three queued tasks, three
times in one evening: with no task focused, only a builder-laneable task was offered (none while the host strained or for a
research task), and a planned, reviewed task with no free lane was skipped. An independent queued task (not the focused
one, not waiting on an open task, not under way here) is told to start, naming the running job; with only dependent
tasks, an empty queue, the drive off or a pause, the wait is as before."""
from test_hooks import HookCase, parse


class DriveWaitQueue(HookCase):
    JOB = {"id": "bci1", "type": "shell", "status": "running", "description": "watch the pipeline"}

    def stop(self, jobs=None, msg="Waiting on CI."):
        return parse(self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": msg, "session_id": "sess-1",
                                        "background_tasks": [self.JOB] if jobs is None else jobs})) or {}

    def review(self, tid):
        self.fm("task", "set", tid, "--section", "Plan review", "--text", "Reviewed: the plan holds.")

    def test_an_unfocused_session_is_told_to_start_a_queued_research_task_and_names_the_job(self):
        self.fm("init")
        queued = self.task("Survey the field", type_="RESEARCH", focus=False)  # no builder lane takes a research task
        out = self.stop()
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn(f"Start {queued}", out["reason"])
        self.assertIn("bci1", out["reason"], "the still-running job is named")
        self.assertIn(f"sampo focus {queued}", out["reason"], "no task is focused: start it by focusing it")
        self.assertNotIn("waits for background work", out.get("systemMessage", ""))

    def test_a_focused_session_checkpoints_its_task_and_starts_a_planned_one_no_lane_takes(self):
        self.fm("init")
        self.task("Fix the pipeline")
        big = self.task("Rework the store", tier="L", focus=False)
        self.review(big)  # planned and reviewed, L tier: no lane takes it, so side work skipped it
        out = self.stop()
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn(f"Start {big}", out["reason"])
        self.assertIn("bci1", out["reason"])
        self.assertIn(f"sampo focus {big}", out["reason"])
        self.assertIn("checkpoint", out["reason"], "the waiting task is kept, not dropped")

    def test_each_queued_task_is_offered_once_then_the_wait_is_as_before(self):
        self.fm("init")
        only = self.task("Survey the field", type_="RESEARCH", focus=False)
        self.assertIn(f"Start {only}", self.stop().get("reason", ""))
        again = self.stop()
        self.assertIsNone(again.get("decision"), "offered once: not a block on every Stop")

    def test_only_dependent_tasks_wait_as_before(self):
        self.fm("init")
        first = self.task("Survey the field", type_="RESEARCH", focus=False)
        second = self.task("Survey more", type_="RESEARCH", focus=False)
        active = self.task("Fix the pipeline")
        self.fm("task", "set", first, f"depends_on={active}")
        self.fm("task", "set", second, f"depends_on={first}")
        for out in (self.stop(), self.stop()):  # the first Stop's own "meanwhile" push, then the wait
            self.assertNotIn("Start T-", out.get("reason", ""), out)
            self.assertNotIn("sampo focus", out.get("reason", ""), out)
        self.assertIn("the drive waits for background work (bci1)", out.get("systemMessage", ""))

    def test_an_empty_queue_waits_as_before(self):
        self.fm("init")
        self.task("Fix the pipeline")
        for out in (self.stop(), self.stop()):
            self.assertNotIn("Start T-", out.get("reason", ""), out)
            self.assertNotIn("sampo focus", out.get("reason", ""), out)
        self.assertIn("the drive waits for background work (bci1)", out.get("systemMessage", ""))

    def test_a_task_under_way_here_is_not_started_over_the_waiting_one(self):
        self.fm("init")
        started = self.task("Done here, waiting on its gate", type_="RESEARCH", focus=False)
        self.fm("task", "evidence", started, "--step", "1", "--run", "true")
        self.task("Fix the pipeline")
        self.assertNotIn(f"Start {started}", self.stop().get("reason", ""))

    def test_a_running_builder_keeps_the_unfocused_session_waiting(self):
        # T-3539: the merge-keeping session isn't pushed to focus a task while a builder lane runs
        self.fm("init")
        self.task("Survey the field", type_="RESEARCH", focus=False)
        agent = {"id": "aw1", "type": "agent", "status": "running", "description": "Build"}
        out = self.stop([agent, self.JOB])
        self.assertIsNone(out.get("decision"), out)

    def test_drive_off_and_a_pause_leave_the_wait_alone(self):
        self.fm("init")
        self.task("Survey the field", type_="RESEARCH", focus=False)
        self.fm("drive", "off")
        self.assertIsNone(self.stop().get("decision"), "drive off")
        self.fm("drive", "on")
        self.fm("pause", "on")
        self.assertIsNone(self.stop().get("decision"), "paused")
