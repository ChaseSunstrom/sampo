"""T-4670 (and T-4646): the drive's background wait must not let a session sit idle with a workable task queued.
JARVIS 2026-11-14: a turn ended 'read the T-1341 log when its script ends, then build T-1347' and the session idled until
a person typed, although T-1347 was queued and did not depend on the close script. Suspects, each with its test:
(a) T-4611 offered each queued task once per session, so a session that answered the offer with 'after the log' was
never pushed again while the same wait lasted: it is now named at each Stop until focused, blocked or deferred (bounded by
the drive's count limit); (b)+(f) a close started detached (nohup, setsid) inside a foreground command is no job in
Claude Code's list: the hook reads Sampo's own record of a live close (the `closing` marker `sampo task finish` keeps) and
waits as for a job; (c) a planned or captured task counts as startable (a pin: it already did); (d) the wait timer
called a queued close or a CI watch 'stuck' at 20 min: each kind has the time it is known to take and the line says 'still
running after N min (typical M)'; (e) the full-autonomy 'nothing else is queued: check the product end to end' push is
held while any close job runs.
reruns: HookCase fixtures (a focused task, a queued research task no builder lane takes, jobs in the engine's list)."""
import json
import os
import time

from test_hooks import HookCase, parse

from sampo import core as c
from sampo import hooks


class Wait(HookCase):
    CI = {"id": "bci1", "type": "shell", "status": "running", "description": "watch the pipeline"}

    def stop(self, *jobs, msg="Waiting on the script.", active=False, env=None):
        pl = {"stop_hook_active": active, "last_assistant_message": msg, "session_id": "sess-1",
              "background_tasks": list(jobs)}
        return parse(self.hook("Stop", pl, env=env)) or {}

    def close_job(self, tid, jid="bclose1"):
        return {"id": jid, "type": "shell", "status": "running",
                "description": f"Queue the close of {tid} behind CI: sampo task finish {tid}"}

    def done_task(self, title="Close behind CI"):
        tid = self.task(title)
        for n in (1, 2):
            self.fm("task", "evidence", tid, "--step", str(n), "--run", "true")
        return tid

    def drive_state(self):
        with open(os.path.join(self.project().dir, "gate.json")) as f:
            return json.load(f)["drive"]["sess-1"]

    def set_drive(self, **kw):
        gate = os.path.join(self.project().dir, "gate.json")
        with open(gate) as f:
            g = json.load(f)
        g["drive"]["sess-1"].update(kw)
        c.write_atomic(gate, json.dumps(g))

    def mark_closing(self, tid, pid):
        p = self.project()
        m = c.read_meta(p)
        m["closing"] = {str(pid): {"task": tid, "pid": pid, "root": p.root, "at": c.now()}}
        c.write_meta(p, m)


class RepeatOffer(Wait):
    """(a): the queued independent task is named again at each Stop of the wait."""

    def test_a_declined_offer_is_made_again_at_each_stop_of_the_same_wait(self):
        self.fm("init")
        self.task("Fix the pipeline")
        queued = self.task("Survey the field", type_="RESEARCH", focus=False)
        first = self.stop(self.CI)
        self.assertIn(f"Start {queued}", first.get("reason", ""), first)
        for _ in range(3):  # the session answered 'after the log': its Stop follows the block (stop_hook_active)
            out = self.stop(self.CI, msg="I'll read the log when its script ends, then build it.", active=True)
            self.assertEqual(out.get("decision"), "block", out)
            self.assertIn(f"Start {queued}", out["reason"])
            self.assertIn(f"sampo focus {queued}", out["reason"])

    def test_the_task_is_not_named_once_it_is_focused_blocked_or_deferred(self):
        self.fm("init")
        self.task("Fix the pipeline")
        blocked = self.task("Survey the field", type_="RESEARCH", focus=False)
        deferred = self.task("Survey more", type_="RESEARCH", focus=False)
        for _ in range(2):
            self.assertIn("Start T-", self.stop(self.CI, active=True).get("reason", ""))
        self.fm("task", "block", blocked, "cannot start: waits on the owner")
        out = self.stop(self.CI, active=True)
        self.assertIn(f"Start {deferred}", out.get("reason", ""), "the next independent task is named")
        self.assertNotIn(f"Start {blocked}", out.get("reason", ""))
        self.fm("task", "defer", deferred)
        out = self.stop(self.CI, active=True)
        self.assertNotIn("Start T-", out.get("reason", ""), out)
        self.assertIsNone(out.get("decision"), "nothing queued is left: the wait is as before")

    def test_a_focused_task_ends_its_own_offer(self):
        self.fm("init")
        self.task("Fix the pipeline")
        queued = self.task("Survey the field", type_="RESEARCH", focus=False)
        self.assertIn(f"Start {queued}", self.stop(self.CI).get("reason", ""))
        self.fm("checkpoint")
        self.fm("focus", queued)
        self.assertNotIn(f"Start {queued}", self.stop(self.CI, active=True).get("reason", ""))

    def test_the_repeat_is_bounded_by_the_drives_count_limit(self):
        self.fm("init")
        self.task("Fix the pipeline")
        self.task("Survey the field", type_="RESEARCH", focus=False)
        self.stop(self.CI)
        self.set_drive(count=hooks.DRIVE_MAX)
        out = self.stop(self.CI, active=True)
        self.assertIsNone(out.get("decision"), "the count limit ends every continuation")

    def test_each_repeat_counts_once_and_names_the_way_out(self):
        self.fm("init")
        self.task("Fix the pipeline")
        queued = self.task("Survey the field", type_="RESEARCH", focus=False)
        self.stop(self.CI)
        n = self.drive_state()["count"]
        out = self.stop(self.CI, active=True)
        self.assertEqual(self.drive_state()["count"], n + 1)
        self.assertIn(f'sampo task block {queued}', out["reason"], "a session that cannot start it has the way out")

    def test_a_task_that_depends_on_the_running_work_is_never_named(self):
        self.fm("init")
        active = self.task("Fix the pipeline")
        queued = self.task("Survey the field", type_="RESEARCH", focus=False)
        self.fm("task", "set", queued, f"depends_on={active}")
        for _ in range(3):
            self.assertNotIn("Start T-", self.stop(self.CI, active=True).get("reason", ""))

    def test_a_running_builder_keeps_its_once_per_task_offers(self):
        # an agent's wait keeps T-4611's rule: the repeat is for the wait on a script or a watcher only
        self.fm("init")
        self.task("Fix the pipeline")
        self.task("Survey the field", type_="RESEARCH", focus=False)
        agent = {"id": "aw1", "type": "agent", "status": "running", "description": "Build"}
        outs = [self.stop(agent, self.CI, active=bool(i)) for i in range(3)]
        self.assertFalse([o for o in outs[1:] if "Start T-" in o.get("reason", "")], outs)


class Startable(Wait):
    """(c): the queued task the offer reaches (a pin: a planned one, a captured one in full autonomy)."""

    def test_a_planned_task_no_lane_takes_is_named(self):
        self.fm("init")
        self.task("Fix the pipeline")
        big = self.task("Rework the store", tier="L", focus=False)
        self.fm("task", "set", big, "--section", "Plan review", "--text", "Reviewed: the plan holds.")
        self.assertIn(f"Start {big}", self.stop(self.CI).get("reason", ""))

    def test_a_captured_task_is_named_in_full_autonomy(self):
        self.fm("init")
        self.fm("autonomy", "full")
        self.task("Fix the pipeline")
        self.fm("capture", "Add a CSV export to the reports page", "--type", "FEATURE", "--tier", "S")
        out = self.stop(self.CI)
        self.assertIn("Start T-", out.get("reason", ""), out)

    def test_a_planned_and_a_captured_task_are_named_again_too(self):
        self.fm("init")
        self.fm("autonomy", "full")
        self.task("Fix the pipeline")
        big = self.task("Rework the store", tier="L", focus=False)
        self.fm("task", "set", big, "--section", "Plan review", "--text", "Reviewed: the plan holds.")
        self.stop(self.CI)
        for _ in range(2):
            self.assertIn(f"Start {big}", self.stop(self.CI, active=True).get("reason", ""))


class DetachedClose(Wait):
    """(b)+(f): a close started detached is no job in the engine's list; Sampo's own marker of a live close is read."""

    def test_a_live_close_marker_is_a_wait_and_the_queued_task_is_named(self):
        self.fm("init")
        tid = self.done_task()
        queued = self.task("Survey the field", type_="RESEARCH", focus=False)
        self.mark_closing(tid, os.getpid())  # nohup sampo task finish: its marker, its pid alive
        for active in (False, True, True):
            out = self.stop(active=active)  # the engine's list holds no job
            self.assertEqual(out.get("decision"), "block", out)
            self.assertIn(f"Start {queued}", out["reason"])
            self.assertNotIn("task finish", out["reason"], "the close is under way: no push to run it again")

    def test_with_no_independent_task_the_session_is_not_pushed_to_close_what_is_closing(self):
        self.fm("init")
        tid = self.done_task()
        self.mark_closing(tid, os.getpid())
        for active in (False, True):
            out = self.stop(active=active)
            self.assertNotIn("task finish", json.dumps(out), out)
            self.assertIsNone(out.get("decision"), out)

    def test_a_marker_whose_process_ended_is_no_wait(self):
        self.fm("init")
        tid = self.done_task()
        self.task("Survey the field", type_="RESEARCH", focus=False)
        self.mark_closing(tid, 2 ** 22 + 12345)  # a pid no process holds
        out = self.stop()
        self.assertIn(f"sampo task finish {tid}", out.get("reason", ""), "as before: the close is not under way")

    def test_another_tasks_close_marker_is_no_wait_for_this_one(self):
        self.fm("init")
        tid = self.done_task()
        other = self.task("Another", focus=False)
        self.mark_closing(other, os.getpid())
        self.assertIn(f"sampo task finish {tid}", self.stop().get("reason", ""))

    def test_the_marker_wait_leaves_one_drive_event_and_the_timer_record(self):
        self.fm("init")
        tid = self.done_task()
        self.mark_closing(tid, os.getpid())
        self.stop()
        self.assertEqual(len([e for e in self.events() if e.get("kind") == "drive_wait"]), 1)
        jobs = self.drive_state().get("wait_jobs") or {}
        self.assertTrue(jobs.get("since"), "the timer hook reads the wait from the same record")


class StuckLine(Wait):
    """(d): a queued close or a CI watch is called stuck at its own kind's time, in 'still running after N min (typical M)'."""

    NOW = 100_000.0

    def wake(self, age_min, kind, **kw):
        since = {"b1": self.NOW - age_min * 60}
        return hooks.wait_wake(since, armed_at=since["b1"], turn_at=None, now=self.NOW, kinds={"b1": kind}, **kw)

    def test_a_queued_close_is_not_stuck_at_the_plain_limit(self):
        self.assertIsNone(self.wake(25, "close"))
        self.assertIsNone(self.wake(100, "close"))

    def test_a_ci_watch_is_not_stuck_at_the_plain_limit(self):
        self.assertIsNone(self.wake(25, "ci"))

    def test_past_its_kinds_time_the_line_says_how_long_and_what_is_typical(self):
        for kind in ("close", "ci"):
            text = self.wake(200, kind)
            self.assertIn("b1", text)
            self.assertIn("still running after 200 min", text)
            self.assertRegex(text, r"\(typical \d+ min")
            self.assertEqual(len(text.splitlines()), 1, "one plain line")

    def test_a_job_of_no_known_kind_keeps_the_plain_limit_and_line(self):
        since = {"b1": self.NOW - 25 * 60}
        self.assertIn("25 min", hooks.wait_wake(since, armed_at=since["b1"], turn_at=None, now=self.NOW) or "")
        self.assertIn("25 min", hooks.wait_wake(since, armed_at=since["b1"], turn_at=None, now=self.NOW,
                                                kinds={"b1": "?"}) or "", "an unknown kind is a plain job")

    def test_the_timer_hook_outlasts_the_longest_kind_deadline(self):
        # a hook the engine kills at its timeout wakes nobody: the close deadline (2 h) must fit inside it
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(hooks.__file__))), "..", "hooks", "hooks.json")
        with open(os.path.normpath(path)) as f:
            every = [h for g in json.load(f)["hooks"]["Stop"] for h in g["hooks"]]
        timer = next(h for h in every if h.get("asyncRewake"))
        self.assertGreater(timer["timeout"], hooks.job_limit("close") + 60)
        self.assertGreater(timer["timeout"], hooks.job_limit("ci") + 60)

    def test_the_stop_records_each_waited_jobs_kind_for_the_timer(self):
        self.fm("init")
        tid = self.done_task()
        ci = {"id": "bci2", "type": "shell", "status": "running", "command": "sampo ci-watch --commit abc123"}
        mention = {"id": "bm", "type": "shell", "description": "watch the CI pipeline on gitlab"}  # words, not the watcher
        self.stop(self.close_job(tid), ci, mention, {"id": "bx", "type": "shell", "description": "tail the log"})
        kinds = (self.drive_state().get("wait_jobs") or {}).get("kinds") or {}
        self.assertEqual(kinds, {"bclose1": "close", "bci2": "ci"}, "a job of no known kind is not listed")


class JobKind(Wait):
    """(d): a job is a CI watch by the structure of its command (the `ci-watch` verb of sampo or fm at a command position,
    read with shlex), never by words in its description: the kind sets how long it is waited on."""

    def kind(self, **job):
        return hooks.job_kind(dict({"id": "b1", "type": "shell"}, **job))

    def test_a_description_that_only_mentions_ci_or_a_pipeline_is_no_kind(self):
        for text in ("watch the pipeline", "CI run of the gitlab pipeline", "github actions status", "gh run watch",
                     "sampo ci-watch --commit abc"):  # (a description is not a command)
            self.assertIsNone(self.kind(description=text), text)
            self.assertIsNone(self.kind(name=text, title=text, summary=text), text)
        for command in ("echo ci-watch", "grep -r pipeline docs", "gh run watch 123", "cat ci-watch.log",
                        "echo 'sampo ci-watch'", "./ci-watch.sh", "sampo task show ci-watch"):
            self.assertIsNone(self.kind(command=command), command)

    def test_the_ci_watch_verb_of_sampo_or_fm_is_the_ci_kind(self):
        for command in ("sampo ci-watch --commit X", "fm ci-watch --last-ship", "cd /srv/repo && sampo ci-watch --commit X",
                        "sampo ci-watch --commit X | tee /tmp/w.log", "FOO=1 sampo ci-watch --commit X",
                        "nohup sampo ci-watch --commit X", "python3 plugin/bin/sampo ci-watch --commit X",
                        "/home/u/.claude/foreman/plugin/bin/fm ci-watch", "git push && sampo ci-watch --commit X",
                        "cd a; sampo ci-watch --commit X &", "bash -c 'cd r && sampo ci-watch --commit X'",
                        "# wait for CI\nsampo ci-watch --commit X", "sampo ci-watch --commit X # note",
                        "sampo ci-watch --url http://h/p#frag"):
            self.assertEqual(self.kind(command=command), "ci", command)

    def test_a_bg_start_records_the_ci_kind_where_the_command_is_known(self):
        self.fm("init")
        self.fm("autonomy", "full")
        self.task("Fix the pipeline")
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "sampo ci-watch --commit X",
                  "run_in_background": True}, "tool_response": {"backgroundTaskId": "bciw"}})
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "tail -f x",
                  "run_in_background": True}, "tool_response": {"backgroundTaskId": "bplain"}})
        self.stop({"id": "bciw", "type": "shell", "status": "running"}, {"id": "bplain", "type": "shell", "status": "running"})
        kinds = (self.drive_state().get("wait_jobs") or {}).get("kinds") or {}
        self.assertEqual(kinds, {"bciw": "ci"})

    def test_an_unparsable_or_huge_or_non_text_command_is_no_kind(self):
        for command in ("sampo ci-watch 'unclosed", "", None, 5, ["sampo", "ci-watch"], "x " * 5000 + "sampo ci-watch"):
            self.assertIsNone(self.kind(command=command), repr(command)[:40])
        self.assertIsNone(hooks.job_kind("not a dict"))

    def test_a_close_found_by_the_drive_stays_the_close_kind(self):
        self.assertEqual(hooks.job_kind({"id": "b1"}, closing=True), "close")
        self.assertEqual(hooks.job_kind({"id": "b1", "command": "sampo ci-watch --commit X"}, closing=True), "close")


class EndToEndPush(Wait):
    """(e): the full-autonomy 'nothing else is queued' push is held while a close job runs."""

    def test_the_push_is_held_while_another_tasks_close_runs(self):
        self.fm("init")
        self.fm("autonomy", "full")
        self.task("Fix the pipeline")
        out = self.stop(self.close_job("T-9999"))
        self.assertNotIn("end to end", json.dumps(out), out)
        self.assertIsNone(out.get("decision"), out)

    def test_the_push_is_held_while_the_active_tasks_own_close_runs(self):
        self.fm("init")
        self.fm("autonomy", "full")
        tid = self.done_task()
        out = self.stop(self.close_job(tid))
        self.assertNotIn("end to end", json.dumps(out), out)

    def test_the_control_a_ci_watch_alone_still_gets_the_push_once(self):
        self.fm("init")
        self.fm("autonomy", "full")
        self.task("Fix the pipeline")
        out = self.stop(self.CI)
        self.assertIn("end to end", out.get("reason", ""), out)
        self.assertNotIn("end to end", self.stop(self.CI, active=True).get("reason", ""), "once per drain")
