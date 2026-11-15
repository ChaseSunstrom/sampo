"""T-4692: while a train of this project runs (its own lock is held), the drive never tells the session to focus a task: it
offers a builder lane, or waits on the train as a job and says the train holds the checkout (2026-11-14, T-4681: a focus
made during a train left two lanes for hand closes). A dead train's leftovers and a hand close's commit are no train."""
# map-links: plugin/lib/sampo/hooks.py, plugin/lib/sampo/train.py
import fcntl
import json
import os
import subprocess
import sys
import threading
import time

from helpers import PLUGIN
from test_hooks import HookCase, parse

from sampo import core as c
from sampo import hooks

TIMER = os.path.join(PLUGIN, "hooks", "wait-timer")


class DriveDuringTrain(HookCase):
    def stop(self, msg="Waiting on the train."):
        return parse(self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": msg,
                                        "session_id": "sess-1"})) or {}

    def train(self):
        """What a running train holds: the train's own lock (the test process stands in for the train)."""
        return c.lock(os.path.join(self.project().dir, "train"))

    def said(self, out):
        return (out.get("reason") or "") + " " + (out.get("systemMessage") or "")

    def test_a_queued_task_is_offered_as_a_lane_never_a_focus_while_a_train_runs(self):
        self.fm("init")
        small = self.task("Small fix", focus=False)
        before = self.stop("Done for now.")  # no train: the drive pushes the task as it always did
        self.assertIn(f"sampo focus {small}", before.get("reason", ""), before)
        self.hook("UserPromptSubmit", {"prompt": "go on", "session_id": "sess-1"})
        with self.train():
            out = self.stop("Done for now.")
        text = self.said(out)
        self.assertEqual(out.get("decision"), "block", out)
        self.assertNotIn("sampo focus", text)
        self.assertIn(f"sampo lane brief {small}", text, "the lane starts without a focus")
        self.assertIn("train holds the checkout", text)

    def test_with_nothing_that_fits_a_lane_the_train_is_waited_on_as_a_job(self):
        self.fm("init")
        self.task("Survey the field", type_="RESEARCH", focus=False)  # no builder lane takes it
        with self.train():
            out = self.stop("Done for now.")
            again = self.stop("Done for now.")
        text = self.said(out)
        self.assertNotEqual(out.get("decision"), "block", out)
        self.assertNotIn("sampo focus", text)
        self.assertIn("train holds the checkout", text)
        self.assertIn("lane brief", text, "what can move meanwhile is named")
        self.assertNotIn("sampo focus", self.said(again))
        self.assertEqual(self.stop("Done for now.").get("decision"), "block", "the train over: the drive goes on")

    def test_a_dead_trains_leftovers_are_no_train(self):
        self.fm("init")
        small = self.task("Small fix", focus=False)
        folder = os.path.join(self.project().dir, "train")
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, ".lock"), "w").close()  # the lock file a train leaves behind: nothing holds it
        with open(os.path.join(folder, "closing"), "w") as f:
            f.write("999999999")  # a close that is gone
        out = self.stop("Done for now.")
        self.assertIn(f"sampo focus {small}", out.get("reason", ""), out)

    def test_a_hand_closes_trunk_commit_is_not_a_train(self):
        from sampo import train as strain
        self.fm("init")
        folder = os.path.join(self.project().dir, "train")
        with self.train():
            self.assertTrue(strain.train_live(self.project()))
            with open(os.path.join(folder, "closing"), "w") as f:
                f.write(str(os.getpid()))  # a live hand close writes its pid under the train's lock
            self.assertFalse(strain.train_live(self.project()))
            os.remove(os.path.join(folder, "closing"))
        self.assertFalse(strain.train_live(self.project()), "released: no train")

    def test_the_task_in_hand_is_still_pushed_during_a_train(self):
        self.fm("init")
        self.task("Fix login")  # focused: the session's own work goes on
        with self.train():
            out = self.stop("Done for now.")
        self.assertEqual(out.get("decision"), "block", out)
        self.assertNotIn("train holds the checkout", self.said(out))

    def arm(self, since):
        """The wait a Stop leaves for the timer: a train job with its kind (gate.json's drive[sid].wait_jobs)."""
        gate = os.path.join(self.project().dir, "gate.json")
        try:
            with open(gate) as f:
                g = json.load(f)
        except (OSError, ValueError):
            g = {}
        d = g.setdefault("drive", {}).setdefault("sess-1", {"count": 0})
        d["wait_jobs"] = {"since": since, "at": time.time(), "kinds": {k: "train" for k in since}}
        c.write_atomic(gate, json.dumps(g))

    def timer(self, **env):
        payload = json.dumps({"session_id": "sess-1", "cwd": self.repo, "hook_event_name": "Stop"})
        e = dict(os.environ, FOREMAN_HOME=self.home, SAMPO_WAIT_SETTLE_S="0", SAMPO_WAIT_TRAIN_POLL_S="0.1", **env)
        return subprocess.run([sys.executable, TIMER], input=payload, text=True, capture_output=True, env=e,
                              cwd=self.repo, timeout=60)

    def test_the_timer_wakes_the_idle_session_when_the_train_ends(self):
        self.fm("init")
        self.arm({"train": time.time()})
        folder = os.path.join(self.project().dir, "train")
        os.makedirs(folder, exist_ok=True)
        fd = os.open(os.path.join(folder, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)  # what the train holds, released from another thread: closing the descriptor
        released = threading.Timer(1.0, lambda: os.close(fd))
        released.start()
        self.addCleanup(released.cancel)
        t0 = time.monotonic()
        r = self.timer()
        released.join()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("train that held the checkout has ended", r.stderr)
        self.assertLess(time.monotonic() - t0, 30, "woken by the train's end, not by the 4 h deadline")
        self.assertEqual(self.timer().returncode, 0, "once per wait: the same wait is not woken twice")

    def test_a_train_still_running_at_the_deadline_is_named_stuck_not_ended(self):
        self.fm("init")
        self.arm({"train": time.time() - 3 * 3600})  # past twice its typical time (2 h)
        with self.train():
            r = self.timer()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("still running", r.stderr)
        self.assertNotIn("has ended", r.stderr)

    def test_a_train_is_a_kind_with_its_own_typical_time(self):
        self.assertGreaterEqual(hooks.JOB_TYPICAL_MIN.get("train", 0), 60)
        self.assertGreater(hooks.job_limit("train"), hooks.LONG_JOB_S)
        self.assertLess(hooks.job_limit("train"), 7500, "the timer hook's own timeout: it must live to the deadline")
        text = hooks.wait_wake({"train": 0}, 1, None, hooks.job_limit("train") + 1, kinds={"train": "train"})
        self.assertIn("a train", text)
        self.assertIn(f"typical {hooks.JOB_TYPICAL_MIN['train']} min", text)
