"""T-0436: fm pause, one flag that stops everything Foreman runs unattended; fm pause off lifts it."""
import json
import os
import sys

from helpers import FM
from test_serve import ServeCase

from sampo import core as c
from sampo import hooks


class Panic(ServeCase):
    def setUp(self):
        super().setUp()
        self.fm("autonomy", "full")
        for title in ("one", "two"):
            self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s")

    def drive(self):
        out = self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Continuing.",
                                 "session_id": "s1"}).stdout.strip()
        return json.loads(out).get("decision") if out.startswith("{") else None

    def claude_runs(self):
        return sum(line.startswith("claude ") for line in self.called().splitlines())

    def test_panic_blocks_unattended(self):
        self.assertEqual(self.drive(), "block")
        self.fm("pause", "on")
        self.assertTrue(c.panicked())
        self.assertIsNone(self.drive())
        self.assertEqual(c.state_dict(c.find_project(self.repo))["autonomy"], "standard")  # tightened, not rewritten
        self.assertEqual(self.meta()["autonomy"], "full")
        self.assertFalse(hooks.second_due({"source": "startup"}, {}, False))
        self.assertIn("PAUSED", self.fm("status").stdout)
        for args in (["run"], ["serve"], ["night"], ["lane", "new", "T-0001"]):
            r = self.fm(*args, check=False, env=self.env())
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("sampo pause off", r.stderr, args)
        self.assertEqual(self.called(), "")  # no claude, no systemctl
        self.assertFalse(os.path.exists(self.unit))
        self.serve("stop")  # stopping still works: pause only tightens

    def test_its_review_every_claude_launcher_waits_and_only_the_user_lifts_it(self):
        # T-0591 (T-0436 review): fm session start, fm ideas, fm research ask, fm bench and fm relate still launched
        # claude children while paused; and Claude could run fm pause off itself
        from unittest import mock
        from sampo import relate
        pack = os.path.join(self.tmp, "pack.md")
        with open(pack, "w") as f:
            f.write("a pack\n")
        self.fm("pause", "on")
        for args in (["session", "start", "do x"], ["ideas", "--pack", pack], ["research", "ask", "what is x?"],
                     ["bench", "run"], ["relate"], ["oracle", "will it work?"]):
            r = self.fm(*args, check=False, env=self.env())
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("sampo pause off", r.stderr, args)
        self.assertEqual(self.called(), "")
        with mock.patch.dict(os.environ, {"FOREMAN_NO_BACKGROUND": ""}), mock.patch("subprocess.Popen") as popen:
            relate.spawn(c.find_project(self.repo))  # the SessionStart path: skipped, never raised
        popen.assert_not_called()
        out = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "fm pause off"}}).stdout
        self.assertIn('"deny"', out)
        self.assertTrue(c.panicked())

    def test_panic_blocks_unattended_in_a_running_loop(self):
        fm = f"{sys.executable} {FM}"  # the user pauses while the first session runs
        self.stub("claude", f'{fm} pause on\n{fm} task block $FOREMAN_DRIVE_TASK "needs a key"\n')
        r = self.fm("run", check=False, env=self.env())
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.claude_runs(), 1, "the second task never starts")
        self.assertIn("sampo pause off", r.stderr)

    def test_pause_now_stops_what_runs_frees_leases_and_writes_a_record(self):
        # T-1821: fm pause only stopped new starts; --now stops a running fm session's group and frees every lease
        import subprocess
        from sampo import bus
        sid = "20261028-090000-abcd"
        runner = subprocess.Popen(["sh", "-c", "sleep 60; true", sid], start_new_session=True)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        d = os.path.join(c.state_dir(), "agent-sessions", sid)
        os.makedirs(d)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"id": sid, "status": "running", "pid": runner.pid, "turns": 1}, f)
        bus._write_leases({"src/a.py": {"session": "s9", "task": "T-0001", "until": 9e12}})
        self.serve()  # a serve unit (systemctl is a stub)
        self.assertTrue(os.path.exists(self.unit))
        out = self.fm("pause", "--now", env=self.env()).stdout
        self.assertEqual(runner.wait(timeout=10), -15)  # SIGTERM to its group
        self.assertFalse(os.path.exists(self.unit))
        self.assertIn(f"systemctl --user disable --now foreman-serve-{self.slug}", self.called())
        self.assertTrue(c.panicked(hard=True))
        with open(os.path.join(c.state_dir(), "bus", "leases.json")) as f:
            self.assertEqual(json.load(f), {})
        with open(os.path.join(c.state_dir(), "stops.jsonl")) as f:
            rec = json.loads(f.read().splitlines()[-1])
        self.assertEqual((rec["sessions"], rec["serve"], rec["leases"]), ([sid], [self.slug], 1))
        self.assertNotIn("errors", rec)
        self.assertIn("1 sampo session", out)
        self.assertIn("1 sampo serve unit", out)
        self.assertIn("finish their turn", out)
        self.fm("pause", "on")  # a plain pause after it stays soft
        self.assertFalse(c.panicked(hard=True))
        self.assertTrue(c.panicked())

    def test_pause_now_run_inside_an_fm_session_writes_its_record_before_stopping_its_own_session(self):
        # T-1821 review: an fm session's agent running fm pause --now is in the group it stops: everything else
        # first, then the record, and its own session last
        import subprocess
        import time
        from sampo import bus
        sid, other = "20261028-090000-aaaa", "20261028-090000-bbbb"
        go = os.path.join(self.tmp, "go")
        fm = f"{sys.executable} {FM}"
        env = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", **self.env())
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        mine = subprocess.Popen(["sh", "-c", f"while [ ! -f {go} ]; do sleep 0.05; done; cd {self.repo}; "
                                 f"{fm} pause --now; sleep 60; true", sid], start_new_session=True, env=env)
        theirs = subprocess.Popen(["sh", "-c", "sleep 60; true", other], start_new_session=True)
        for p in (mine, theirs):
            self.addCleanup(lambda p=p: p.poll() is None and p.kill())
        for s, p in ((sid, mine), (other, theirs)):
            os.makedirs(os.path.join(c.state_dir(), "agent-sessions", s))
            with open(os.path.join(c.state_dir(), "agent-sessions", s, "meta.json"), "w") as f:
                json.dump({"id": s, "status": "running", "pid": p.pid, "turns": 1}, f)
        bus._write_leases({"src/a.py": {"session": "s9", "task": "T-0001", "until": 9e12}})
        open(go, "w").close()
        self.assertEqual(theirs.wait(timeout=20), -15)
        self.assertIn(mine.wait(timeout=20), (-15, -9))
        with open(os.path.join(c.state_dir(), "stops.jsonl")) as f:
            rec = json.loads(f.read().splitlines()[-1])
        self.assertEqual((sorted(rec["sessions"]), rec["leases"]), (sorted([sid, other]), 1))
        with open(os.path.join(c.state_dir(), "bus", "leases.json")) as f:
            self.assertEqual(json.load(f), {})

    def test_pause_off_now_is_refused(self):
        r = self.fm("pause", "off", "--now", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--now", r.stderr)

    def test_pause_now_stops_a_running_fm_run_session(self):
        import time
        fm = f"{sys.executable} {FM}"
        self.stub("claude", f"{fm} pause --now\nexec sleep 60\n")
        t0 = time.monotonic()
        r = self.fm("run", check=False, env=self.env())
        self.assertLess(time.monotonic() - t0, 30, "the session's group was stopped, not waited out")  # wall clock: a stop-the-group deadline of children
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fm pause --now", r.stderr)
        self.assertEqual(self.claude_runs(), 1)

    def test_bare_pause_only_reports(self):
        """T-4132: a bare `sampo pause`, typed to check, paused every project; it reports and changes nothing."""
        out = self.fm("pause").stdout
        self.assertFalse(c.panicked())
        self.assertIn("Not paused", out)
        self.assertIn("sampo pause on", out)
        self.fm("pause", "on")
        out = self.fm("pause").stdout
        self.assertTrue(c.panicked())
        self.assertIn("Paused", out)
        self.assertIn("sampo pause off", out)
        self.assertEqual(self.fm_json("pause"), {"paused": True, "hard": False})

    def test_panic_resume(self):
        self.fm("pause", "on")
        self.fm("pause", "off")
        self.assertFalse(c.panicked())
        self.assertEqual(self.drive(), "block")
        self.assertEqual(c.state_dict(c.find_project(self.repo))["autonomy"], "full")
        self.assertTrue(hooks.second_due({"source": "startup"}, {}, False))
        self.assertNotIn("PAUSED", self.fm("status").stdout)
        self.fm("night", "--dry-run")
        self.fm("pause", "off")  # already off: nothing to do, no error
