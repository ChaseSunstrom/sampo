"""T-4679: the drive's demotion of a waited-on job to a 'service' and the wait timer (T-4670) read one table of a job
kind's typical time (hooks.JOB_TYPICAL_MIN, through job_limit). Before: the timer let a CI watch run 60 min and a queued
close 120, but the drive dropped it from its wait at the plain 20 (LONG_JOB_S), pushed the session on (the finish push for
the task whose close was behind CI) while the timer still counted it as waited on. A job's kind is read from its command's
structure (T-4670), never from its description; a job of no kind keeps the plain figure.
reruns: HookCase fixtures (a task with every step done, a queued research task no builder lane takes, jobs in the engine's
list) and an in-process check that both readers follow one patched table."""
import json
import os
import time
from unittest import mock

from test_hooks import HookCase, parse

from sampo import hooks

CI_COMMAND = "sampo ci-watch --commit abc123"


class Table(HookCase):
    """The two readers follow one object, not two copies of equal numbers."""

    def test_the_drive_and_the_timer_flip_at_the_same_age_for_any_table(self):
        for typical in (10, 30, 90):
            with mock.patch.dict(hooks.JOB_TYPICAL_MIN, {"ci": typical}):
                edge = hooks.job_limit("ci")
                for age in (edge - 60, edge - 1, edge, edge + 60):
                    now = 10_000_000.0
                    since = {"b1": now - age}
                    stale = bool(hooks._stale_jobs(since, since["b1"], None, now, kinds={"b1": "ci"}))
                    self.assertEqual(hooks.drive_waits_on(age, "ci"), not stale, (typical, age))

    def test_a_kind_with_no_entry_is_the_plain_figure_for_both(self):
        for kind in (None, "?", "service"):
            self.assertEqual(hooks.job_limit(kind), hooks.LONG_JOB_S)
            self.assertTrue(hooks.drive_waits_on(hooks.LONG_JOB_S - 1, kind))
            self.assertFalse(hooks.drive_waits_on(hooks.LONG_JOB_S, kind))

    def test_the_job_holding_the_active_tasks_close_keeps_its_longer_figure(self):
        self.assertTrue(hooks.drive_waits_on(hooks.FINISH_JOB_S - 1, None, mine=True))
        self.assertFalse(hooks.drive_waits_on(hooks.FINISH_JOB_S, None, mine=True))
        self.assertTrue(hooks.drive_waits_on(hooks.FINISH_JOB_S - 1, "close", mine=True))

    def test_a_mine_job_is_never_given_less_than_its_kind_has(self):
        with mock.patch.dict(hooks.JOB_TYPICAL_MIN, {"close": 600}):  # a table longer than FINISH_JOB_S
            self.assertTrue(hooks.drive_waits_on(hooks.FINISH_JOB_S + 60, "close", mine=True))


class Drive(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = self.task("Fix the pipeline")
        for n in (1, 2):
            self.fm("task", "evidence", self.tid, "--step", str(n), "--run", "true")
        self.task("Survey the field", type_="RESEARCH", focus=False)  # no builder lane takes it

    def stop(self, *jobs):
        pl = {"stop_hook_active": False, "last_assistant_message": "The watch is running.", "session_id": "sess-1",
              "background_tasks": list(jobs)}
        return parse(self.hook("Stop", pl)) or {}

    def age(self, jid, minutes):
        gate = os.path.join(self.project().dir, "gate.json")
        g = {}
        if os.path.exists(gate):
            with open(gate) as f:
                g = json.load(f)
        since = g.setdefault("drive", {}).setdefault("sess-1", {"count": 0}).setdefault("since", {})
        since[jid] = since.get(jid, time.time()) - minutes * 60
        with open(gate, "w") as f:
            json.dump(g, f)

    def pushed_finish(self, *jobs, minutes):
        self.stop(*jobs)
        self.age(jobs[0]["id"], minutes)
        out = self.stop(*jobs)
        return f"sampo task finish {self.tid}" in (out.get("reason") or "")

    @staticmethod
    def job(text=None, command=None, jid="b1"):
        j = {"id": jid, "type": "shell", "status": "running"}
        if text:
            j["description"] = text
        if command:
            j["command"] = command
        return j

    def test_a_ci_watch_inside_its_limit_is_still_waited_on(self):
        self.assertFalse(self.pushed_finish(self.job(command=CI_COMMAND), minutes=25), "red before T-4679: dropped at 20")
        self.assertFalse(self.pushed_finish(self.job(command=CI_COMMAND, jid="b2"), minutes=55))

    def test_a_ci_watch_past_its_limit_is_demoted(self):
        self.assertTrue(self.pushed_finish(self.job(command=CI_COMMAND), minutes=hooks.job_limit("ci") / 60 + 5))

    def test_a_close_of_another_task_inside_its_limit_is_still_waited_on_and_past_it_demoted(self):
        close = self.job("Queue the close of T-9999 behind CI: sampo task finish T-9999")
        self.assertFalse(self.pushed_finish(close, minutes=25))
        self.assertFalse(self.pushed_finish(self.job("Queue the close of T-9998: sampo task finish T-9998", jid="b2"),
                                            minutes=100))
        self.assertTrue(self.pushed_finish(self.job("sampo task finish T-9997", jid="b3"),
                                           minutes=hooks.job_limit("close") / 60 + 5))

    def test_a_job_of_no_kind_is_demoted_at_the_plain_figure_as_before(self):
        self.assertTrue(self.pushed_finish(self.job("watch the pipeline"), minutes=25))
        self.assertTrue(self.pushed_finish(self.job("tail the CI log", command="tail -f ci.log", jid="b2"), minutes=25))

    def test_words_about_a_watch_in_the_description_do_not_make_it_a_ci_watch(self):
        self.assertTrue(self.pushed_finish(self.job("sampo ci-watch --commit abc123 (the CI watch)"), minutes=25),
                        "a description is not a command")
