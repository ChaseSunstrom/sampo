"""T-4525: a turn that ends on a background wait arms a timer (a second Stop hook, asyncRewake) that wakes the idle
session once the waited job has run past LONG_JOB_S. JARVIS idled 2.4 h on a script waiting for a process listing that
matched its own launcher: LONG_JOB_S is only read at a Stop, and an idle session has none."""
import json
import os
import signal
import subprocess
import sys
import time

from helpers import PLUGIN, ForemanTestCase

from sampo import core as c
from sampo import hooks

TIMER = os.path.join(PLUGIN, "hooks", "wait-timer")
SID = "sess-1"


class Decision(ForemanTestCase):
    """The decision: the waited jobs' start times, when the wait began, the session's last turn, now -> text or none."""

    def test_a_job_past_the_limit_is_named_with_its_age_and_what_to_do(self):
        now = 10_000.0
        text = hooks.wait_wake({"b7k2": now - hooks.LONG_JOB_S - 300}, armed_at=now - hooks.LONG_JOB_S - 300,
                               turn_at=None, now=now)
        self.assertIn("b7k2", text)
        self.assertIn("25 min", text)  # 20 min limit + 5 min
        self.assertRegex(text, r"(?i)inspect")
        self.assertRegex(text, r"(?i)stop")
        self.assertRegex(text, r"(?i)queued work")
        self.assertEqual(len(text.splitlines()), 1, "one plain line")

    def test_a_job_inside_the_limit_wakes_nothing(self):
        now = 10_000.0
        self.assertIsNone(hooks.wait_wake({"b7k2": now - hooks.LONG_JOB_S + 5}, armed_at=now - 600, turn_at=None, now=now))
        self.assertIsNone(hooks.wait_wake({}, armed_at=now - 600, turn_at=None, now=now), "the job ended: nothing waited")

    def test_the_oldest_job_leads_and_the_others_are_counted(self):
        now = 10_000.0
        since = {"a": now - 1500, "b": now - 3000, "c": now - 60}
        text = hooks.wait_wake(since, armed_at=now - 3000, turn_at=None, now=now)
        self.assertTrue(text.index("b") < len(text) and "50 min" in text, text)
        self.assertIn("a", text)
        self.assertNotIn(" c ", text, "a job inside the limit is not stale")

    def test_once_a_turn_since_the_wait_began_sends_nothing(self):
        now = 10_000.0
        since = {"b7k2": now - 2000}
        self.assertIsNone(hooks.wait_wake(since, armed_at=now - 2000, turn_at=now - 1000, now=now))
        self.assertIsNotNone(hooks.wait_wake(since, armed_at=now - 2000, turn_at=now - 3000, now=now),
                             "a turn before the wait is not a turn since")

    def test_once_per_job_a_woken_job_is_not_named_again(self):
        now = 10_000.0
        since = {"b7k2": now - 2000, "z9": now - 1900}
        self.assertIsNone(hooks.wait_wake(since, armed_at=now - 2000, turn_at=None, now=now, woken={"b7k2", "z9"}))
        text = hooks.wait_wake(since, armed_at=now - 2000, turn_at=None, now=now, woken={"b7k2"})
        self.assertIn("z9", text)
        self.assertNotIn("b7k2", text)


class Entry(ForemanTestCase):
    def stop_hooks(self):
        with open(os.path.join(PLUGIN, "hooks", "hooks.json")) as f:
            return [h for g in json.load(f)["hooks"]["Stop"] for h in g["hooks"]]

    def test_the_stop_event_has_the_timer_beside_the_drive_hook(self):
        hs = self.stop_hooks()
        drive = [h for h in hs if h.get("args") == ["Stop"]]
        self.assertEqual(len(drive), 1)
        self.assertEqual(drive[0]["timeout"], 30)
        self.assertFalse(drive[0].get("asyncRewake"), "the drive hook stays synchronous")
        timer = [h for h in hs if "wait-timer" in h.get("command", "")]
        self.assertEqual(len(timer), 1)
        self.assertIs(timer[0].get("asyncRewake"), True)
        self.assertGreater(timer[0]["timeout"], hooks.LONG_JOB_S + 120, "the hook timeout outlasts the deadline")

    def test_the_timer_script_is_executable(self):
        self.assertTrue(os.access(TIMER, os.X_OK))


class Timer(ForemanTestCase):
    """The script, run as Claude Code runs it: the Stop payload on stdin, exit 2 and stderr to wake."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def arm(self, since=None, at=None, turn_at=None, sid=SID):
        """The drive state a Stop leaves: gate.json's drive[sid] with its wait_jobs (and turn_at, as a prompt writes)."""
        gate = os.path.join(self.p.dir, "gate.json")
        try:
            with open(gate) as f:
                g = json.load(f)
        except (OSError, ValueError):
            g = {}
        d = g.setdefault("drive", {}).setdefault(sid, {"count": 0})
        d["wait_jobs"] = {"since": since or {}, "at": at if at is not None else time.time()}
        if turn_at is not None:
            d["turn_at"] = turn_at
        c.write_atomic(gate, json.dumps(g))

    def env(self, limit=2, settle=0):
        return dict(os.environ, FOREMAN_HOME=self.home, SAMPO_WAIT_SETTLE_S=str(settle), SAMPO_WAIT_LIMIT_S=str(limit))

    def start(self, **kw):
        payload = {"session_id": kw.pop("sid", SID), "cwd": self.repo, "hook_event_name": "Stop"}
        proc = subprocess.Popen([sys.executable, TIMER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env(**kw), cwd=self.repo)
        self.addCleanup(self.reap, proc)
        return proc, json.dumps(payload)

    @staticmethod
    def reap(proc):
        """No timer outlives its test (a sleeping one would hold a pipe and a pid), and no pipe is left open."""
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for f in (proc.stdin, proc.stdout, proc.stderr):
            if f and not f.closed:
                f.close()

    def run_timer(self, timeout=30, **kw):
        proc, payload = self.start(**kw)
        out, err = proc.communicate(payload, timeout=timeout)
        return proc.returncode, out, err

    def test_nothing_waited_on_exits_0_at_once(self):
        t0 = time.monotonic()
        code, out, err = self.run_timer()
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertLess(time.monotonic() - t0, 5)
        self.arm(since={})
        self.assertEqual(self.run_timer()[0], 0, "a wait with no job: nothing to time")

    def test_a_job_past_its_deadline_wakes_with_the_line(self):
        self.arm(since={"b7k2": time.time() - 10})
        code, out, err = self.run_timer(limit=2)
        self.assertEqual(code, 2, err)
        self.assertIn("b7k2", err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_it_sleeps_until_the_deadline_in_one_call(self):
        self.arm(since={"b7k2": time.time()})
        t0 = time.monotonic()
        code, out, err = self.run_timer(limit=2)
        self.assertEqual(code, 2, err)
        self.assertGreaterEqual(time.monotonic() - t0, 1.5)

    def test_a_job_that_ended_in_time_wakes_nothing(self):
        t = time.time()
        self.arm(since={"b7k2": t}, at=t)
        proc, payload = self.start(limit=3)
        proc.stdin.write(payload)
        proc.stdin.close()
        time.sleep(1)  # the next Stop: the job is gone, its state rewritten
        self.arm(since={}, at=time.time())
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertEqual(proc.stderr.read(), "")

    def test_once_a_turn_since_the_wait_began_wakes_nothing(self):
        t = time.time()
        self.arm(since={"b7k2": t - 10}, at=t - 5, turn_at=t - 1)
        self.assertEqual(self.run_timer(limit=2)[0], 0)

    def test_once_per_waited_job_a_second_timer_stays_quiet(self):
        self.arm(since={"b7k2": time.time() - 10})
        self.assertEqual(self.run_timer(limit=2)[0], 2)
        self.arm(since={"b7k2": time.time() - 20})  # the same job, seen at a later Stop
        self.assertEqual(self.run_timer(limit=2)[0], 0)
        self.arm(since={"b7k2": time.time() - 20, "n1": time.time() - 15})
        code, out, err = self.run_timer(limit=2)
        self.assertEqual(code, 2)
        self.assertIn("n1", err)
        self.assertNotIn("b7k2", err)

    def test_a_wait_state_older_than_this_stop_is_not_trusted(self):
        """The drive writes wait_jobs at every Stop that reaches it; one from an earlier Stop says nothing of this one."""
        self.arm(since={"b7k2": time.time() - 100}, at=time.time() - 600)
        self.assertEqual(self.run_timer(limit=2)[0], 0)

    def test_a_newer_timer_ends_the_older_one(self):
        t = time.time()
        self.arm(since={"b7k2": t}, at=t)
        old, payload = self.start(limit=60)
        old.stdin.write(payload)
        old.stdin.close()
        time.sleep(1.5)
        self.arm(since={}, at=time.time())  # the next Stop found nothing waited on
        self.assertEqual(self.run_timer(limit=60)[0], 0)
        try:
            self.assertEqual(old.wait(timeout=10), 0, "ended by the newer timer, cleanly (an exit 2 would wake)")
        finally:
            if old.poll() is None:
                old.send_signal(signal.SIGKILL)
        self.assertEqual(old.stderr.read(), "")

    def test_a_broken_state_or_payload_never_wakes_or_fails(self):
        c.write_atomic(os.path.join(self.p.dir, "gate.json"), "{not json")
        self.assertEqual(self.run_timer()[0], 0)
        proc = subprocess.run([sys.executable, TIMER], input="not json", text=True, capture_output=True,
                              env=self.env(), cwd=self.repo, timeout=30)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))


class DriveWrites(ForemanTestCase):
    """The Stop hook leaves the waited jobs where the timer reads them (the engine's list, a non-agent job)."""

    def test_the_stop_that_waits_records_the_non_agent_jobs(self):
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "Fix login", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "step", tid, "add", "reproduce")
        self.fm("task", "ac", tid, "add", "works", "--verify", "pytest")
        self.fm("focus", tid)
        stop = {"stop_hook_active": False, "last_assistant_message": "Waiting on the script.", "session_id": SID,
                "background_tasks": [{"id": "b7k2", "type": "shell"}, {"id": "a1", "type": "agent"}]}
        from helpers import HOOK
        subprocess.run([sys.executable, HOOK, "Stop"], input=json.dumps(dict(stop, cwd=self.repo)), text=True,
                       capture_output=True, env=dict(os.environ, FOREMAN_HOME=self.home), cwd=self.repo, timeout=60)
        p = c.find_project(self.repo)
        with open(os.path.join(p.dir, "gate.json")) as f:
            w = json.load(f)["drive"][SID]["wait_jobs"]
        self.assertEqual(sorted(w["since"]), ["b7k2"], "a subagent is work that ends: never timed")
        self.assertLessEqual(abs(w["at"] - time.time()), 60)
