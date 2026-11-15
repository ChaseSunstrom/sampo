"""T-4644: a task whose every step is done and whose close a background job of the same session already holds (a script
waiting behind CI's containers, then `sampo task finish ID`) is waiting, not owed a close. JARVIS 2026-11-14 (T-1335, then
T-1336): the Stop hook pushed "sampo task finish ID" three times per task, each a turn answered "already queued", then
idled up to an hour with other work queued. Cause: the wait branch demotes a job running past LONG_JOB_S (20 min) to a
service, and a close behind CI runs longer, so the job fell out of `running` and the finish push came; the wait branch (and
T-4611's start of an independent task) was never entered. A job whose own text (its command or description, or what
the PostToolUse hook recorded at its start) holds the task's id with the finish/done/close verb now stays a wait for up to
FINISH_JOB_S; a job that names no such task (a CI watcher), an ended job and a headless run are as before.
reruns: DriveWaitQueue's setup (HookCase fixtures: a focused task, a queued research task, a CI job in the engine's list)
with the job naming the task's close instead of a watcher (T-4611)."""
import json
import os
import time

from test_hooks import HookCase, parse

from sampo import hooks


class NamesClose(HookCase):
    def test_the_task_id_with_the_finish_or_done_verb_in_the_jobs_own_text(self):
        for text in ("sampo task finish T-1335 --audit x", "close T-1335 after CI", "fm task done T-1335",
                     "wait for the containers, then T-1335's FINISH"):
            self.assertTrue(hooks._names_close(text, "T-1335"), text)

    def test_another_task_a_longer_id_or_no_verb_is_not_a_close_of_this_task(self):
        for text in ("sampo task finish T-13350", "sampo task finish T-1336", "watch the pipeline for T-1335",
                     "sampo task finish ID", "", None, 5):
            self.assertFalse(hooks._names_close(text, "T-1335"), text)

    def test_a_job_dict_is_read_from_its_command_and_description(self):
        self.assertEqual(hooks._job_closes({"id": "b1", "command": "x; sampo task finish T-7 --audit y"}), ["T-7"])
        self.assertEqual(hooks._job_closes({"id": "b2", "description": "queue the close of T-7 and T-9"}), ["T-7", "T-9"])
        self.assertEqual(hooks._job_closes({"id": "b3", "description": "watch the pipeline"}), [])
        self.assertEqual(hooks._job_closes("not a dict"), [])


class QueuedFinish(HookCase):
    CI = {"id": "bci1", "type": "shell", "status": "running", "description": "watch the pipeline"}

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = self.done_task()
        self.queued = self.task("Survey the field", type_="RESEARCH", focus=False)  # no builder lane takes it
        self.sent = []

    def done_task(self):
        tid = self.task("Fix the pipeline")
        for n in (1, 2):
            self.fm("task", "evidence", tid, "--step", str(n), "--run", "true")
        return tid

    def job(self, text, jid="bclose1"):
        return {"id": jid, "type": "shell", "status": "running", "description": text}

    def close_job(self, tid=None, jid="bclose1"):
        return self.job(f"Queue the close of {tid or self.tid} behind CI: sampo task finish {tid or self.tid}", jid)

    def stop(self, *jobs, env=None, engine=True):
        pl = {"stop_hook_active": False, "last_assistant_message": "The close is queued.", "session_id": "sess-1"}
        if engine:
            pl["background_tasks"] = list(jobs)
        out = parse(self.hook("Stop", pl, env=env)) or {}
        self.sent.append(out)
        return out

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

    def waits(self):
        return [e for e in self.events() if e.get("kind") == "drive_wait"]

    def test_the_control_a_task_with_every_step_done_and_no_job_is_pushed_to_finish(self):
        out = self.stop()
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn(f"sampo task finish {self.tid}", out["reason"])

    def test_a_close_job_past_the_service_limit_is_still_a_wait_and_an_independent_task_is_started(self):
        first = self.stop(self.close_job())
        self.assertIn(f"Start {self.queued}", first.get("reason", ""), "T-4611's path: the queued task is started")
        self.assertNotIn("task finish", first.get("reason", ""))
        self.age("bclose1", 25)  # past LONG_JOB_S: a CI watcher is a service by now, a close behind CI is not
        for _ in range(3):  # T-4670: the independent task is named again at each Stop, never the finish push
            out = self.stop(self.close_job())
            self.assertIn(f"Start {self.queued}", out.get("reason", ""), out)
            self.assertNotIn("task finish", json.dumps(out))
        self.assertEqual(len(self.waits()), 1, "one wait, not one per Stop")

    def test_with_no_independent_task_the_hook_stays_quiet_from_the_first_stop(self):
        self.fm("task", "set", self.queued, f"depends_on={self.tid}")
        for _ in range(2):
            out = self.stop(self.close_job())
            self.assertIsNone(out.get("decision"), out)  # not even the generic "meanwhile do what doesn't need it"
        self.age("bclose1", 25)
        self.assertIsNone(self.stop(self.close_job()).get("decision"))
        self.assertEqual(len(self.waits()), 1)

    def test_the_job_is_found_from_the_start_the_hook_recorded_when_the_engine_sends_no_list(self):
        self.hook("PostToolUse", {"tool_name": "Bash", "session_id": "sess-1", "tool_response": "ID: bscript1",
                                  "tool_input": {"command": f"./wait-for-ci.sh && sampo task finish {self.tid}",
                                                 "run_in_background": True}})
        self.stop(engine=False)
        self.age("bscript1", 25)
        out = self.stop(engine=False)
        self.assertNotIn("task finish", json.dumps(out), out)  # (T-4670: the queued task is named again, not the finish)
        self.assertIn(f"Start {self.queued}", out.get("reason", ""), out)
        self.assertEqual(len(self.waits()), 1)
        row = next(e for e in self.events() if e.get("kind") == "bg_start")
        self.assertEqual(row.get("closes"), [self.tid])
        self.assertNotIn("wait-for-ci", json.dumps(row), "the command's text is not stored, only the ids it closes")
        self.hook("PostToolUse", {"tool_name": "TaskStop", "session_id": "sess-1", "tool_input": {"task_id": "bscript1"}})
        self.assertIn(f"sampo task finish {self.tid}", self.stop(engine=False).get("reason", ""), "ended: pushed again")

    def test_a_job_that_names_no_close_of_this_task_is_a_service_after_the_limit_as_before(self):
        for jobs in ([self.CI], [self.job("sampo task finish T-9999 --audit x")],
                     [self.job(f"watch the pipeline for {self.tid}")]):
            self.setUp_again()
            self.stop(*jobs)
            self.age(jobs[0]["id"], 25)
            out = self.stop(*jobs)
            self.assertEqual(out.get("decision"), "block", out)
            self.assertIn(f"sampo task finish {self.tid}", out["reason"])

    def setUp_again(self):  # a fresh drive record: the previous round's said-once marks must not carry over
        gate = os.path.join(self.project().dir, "gate.json")
        if not os.path.exists(gate):
            return
        with open(gate) as f:
            g = json.load(f)
        g["drive"] = {}
        with open(gate, "w") as f:
            json.dump(g, f)

    def test_a_subagent_that_names_the_finish_is_no_queued_close(self):
        agent = {"id": "abuild1", "type": "agent", "status": "running", "description": f"Review and finish {self.tid}"}
        self.fm("task", "set", self.queued, f"depends_on={self.tid}")  # nothing independent to offer
        out = self.stop(agent)
        self.assertIn("meanwhile do", out.get("reason", ""), "the generic side-work push stays for an agent's wait")

    def test_a_young_job_that_names_no_close_waits_as_before(self):
        out = self.stop(self.CI)
        self.assertNotIn("task finish", out.get("reason", ""), out)

    def test_the_job_ended_gives_the_finish_push_again(self):
        self.stop(self.close_job())
        self.age("bclose1", 25)
        self.assertNotIn("task finish", self.stop(self.close_job()).get("reason", ""))  # T-4670: Start T-0002, no finish
        out = self.stop()  # the engine's list no longer holds it
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn(f"sampo task finish {self.tid}", out["reason"])

    def test_a_close_that_hangs_past_the_longer_limit_is_pushed_again(self):
        self.stop(self.close_job())
        self.age("bclose1", hooks.FINISH_JOB_S / 60 + 5)
        out = self.stop(self.close_job())
        self.assertEqual(out.get("decision"), "block", "a hung close job must not hide the push for ever")
        self.assertIn(f"sampo task finish {self.tid}", out["reason"])

    def test_the_wait_timer_still_names_the_long_job_to_an_idle_session(self):
        # T-4525's deadline is unchanged: the job's start is in wait_jobs, so the timer wakes the session once at LONG_JOB_S
        self.stop(self.close_job())
        self.age("bclose1", 25)
        self.stop(self.close_job())
        with open(os.path.join(self.project().dir, "gate.json")) as f:
            jobs = json.load(f)["drive"]["sess-1"]["wait_jobs"]["since"]
        self.assertIn("bclose1", jobs)
        self.assertIn("bclose1", hooks.wait_wake(jobs, armed_at=min(jobs.values()), turn_at=None,
                                                 now=min(jobs.values()) + hooks.LONG_JOB_S + 1) or "")

    def test_a_headless_run_is_as_before(self):
        env = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "FOREMAN_BENCH": "1"}
        # (claude -p has its own wait, _headless_wait: it ends with the turn, so it never leaves a job to its notice)
        ours = self.stop(self.close_job("T-1"), env=env).get("reason", "").replace("bclose1", "J")
        self.age("bclose1", 25)
        aged = self.stop(self.close_job(), env=env).get("reason", "").replace("bclose1", "J")
        self.assertEqual(ours, aged)
        self.assertEqual(aged, self.stop(self.CI, env=env).get("reason", "").replace("bci1", "J"), "as for a watcher")
        self.assertNotIn("Start T-", aged)
