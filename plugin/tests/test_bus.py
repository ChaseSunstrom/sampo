"""T-0708 (Frontier 08, first slice): sessions on one machine message each other through the hooks that already run,
lease the functions they edit, and one conductor lists and steers them all."""
import json
import os

from test_hooks import HookCase, parse

from sampo import bus, core as c

MOD = "def f(x):\n    a = x + 1\n    return a\n\n\ndef g(y):\n    b = y * 2\n    return b\n"


class Base(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.task()

    def note(self, session, tool="Bash", ti=None):
        r = self.hook("PreToolUse", {"tool_name": tool, "tool_input": ti or {"command": "ls"}, "session_id": session})
        return r, (parse(r) or {}).get("hookSpecificOutput") or {}

    def snap(self, sid, **kw):
        d = os.path.join(self.home, "state", "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{sid}.json"), "w") as f:
            json.dump(dict({"ts": c.now(), "session_id": sid, "project": self.project().slug, "cwd": self.repo,
                            "context_pct": 30}, **kw), f)


class Bus(Base):
    def test_a_fleet_row_names_the_sessions_model(self):
        # T-3701: the pane's SESSIONS panel shows each live session's model, from its statusline snapshot
        from sampo import bus
        self.snap("live0002", model="Opus 5.5")
        self.assertEqual(next(r for r in bus.rows() if r["session"] == "live0002")["model"], "Opus 5.5")

    def test_a_message_reaches_its_session_once_and_all_reach_everyone(self):
        self.fm("bus", "send", "sess-2", "use the new parser", env={"CLAUDE_CODE_SESSION_ID": "sess-1"})
        _, out = self.note("sess-2")
        self.assertIn("use the new parser", out.get("additionalContext", ""))
        self.assertNotIn("use the new parser", self.note("sess-2")[1].get("additionalContext", ""), "once")
        self.assertNotIn("use the new parser", self.note("sess-3")[1].get("additionalContext", ""), "not addressed")
        self.fm("bus", "send", "all", "freeze the API", "--type", "steer")
        self.assertIn("freeze the API", self.note("sess-3")[1].get("additionalContext", ""))
        self.assertIn("freeze the API", self.note("sess-2")[1].get("additionalContext", ""))

    def test_a_send_without_its_text_says_how_instead_of_crashing(self):
        p = self.fm("bus", "send", "sess-2", check=False)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn('fm bus send <session|all> "<text>"', p.stderr)

    def test_unread_mail_keeps_a_session_from_stopping(self):
        self.fm("bus", "send", "sess-9", "one more thing: bump the version")
        r = parse(self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Done.", "session_id": "sess-9"}))
        self.assertEqual((r or {}).get("decision"), "block")
        self.assertIn("bump the version", r["reason"])


class Watch(Base):
    """T-0777: a plugin monitor runs fm bus watch for the whole session; each line it prints wakes Claude."""

    def test_it_prints_each_new_message_for_its_session_once(self):
        import io
        import threading
        import time
        from sampo import bus as sbus
        sbus.send("sess-w", "old news", sender="sess-1")
        buf, done = io.StringIO(), threading.Event()
        t = threading.Thread(target=sbus.watch, kwargs={"sid": "sess-w", "interval": 0.05, "out": buf,
                                                         "stop": done.is_set})
        t.start()
        try:
            beat = os.path.join(sbus._dir(), "watch-sess-w")
            for _ in range(100):
                if os.path.exists(beat):
                    break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(beat), "the watcher keeps a heartbeat")
            sbus.send("sess-w", "use the new parser", sender="sess-1")
            sbus.send("sess-z", "not yours", sender="sess-1")
            sbus.send("all", "freeze the API", kind="steer", sender="sess-1")
            for _ in range(100):
                if buf.getvalue().count("\n") >= 2:
                    break
                time.sleep(0.05)
            time.sleep(0.3)  # more polls: nothing printed twice
        finally:
            done.set()
            t.join(5)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("use the new parser", lines[0])
        self.assertIn("freeze the API", lines[1])
        self.assertIn("fm bus read", lines[0])
        self.assertNotIn("not yours", buf.getvalue())
        self.assertNotIn("old news", buf.getvalue())
        self.assertFalse(os.path.exists(beat), "a stopped watcher drops its heartbeat")
        self.assertIn("use the new parser", self.note("sess-w")[1].get("additionalContext", ""), "never marks read")

    def test_a_watcher_ended_by_a_signal_drops_its_heartbeat(self):
        # T-0819: Claude Code ends a monitor with a signal, which skipped the finally: watch-<sid> outlived the session
        import signal
        import subprocess
        import sys
        import time
        from sampo import bus as sbus
        from helpers import FM
        beat = os.path.join(sbus._dir(), "watch-sess-sig")
        for sig in (signal.SIGTERM, signal.SIGHUP):
            w = subprocess.Popen([sys.executable, FM, "bus", "watch", "sess-sig"], cwd=self.repo,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(200):
                    if os.path.exists(beat):
                        break
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(beat))
                w.send_signal(sig)
                w.wait(10)
            finally:
                w.kill()
            self.assertFalse(os.path.exists(beat), f"{sig.name} left the heartbeat")

    def test_session_start_survives_a_racing_cleanup_and_keeps_to_its_own_claude(self):
        # T-0820 (round-1 review): an overlapping SessionStart removing the same dead entry crashed record_pid, and a
        # claude -p run from a session's Bash re-mapped that session's claude to itself
        from sampo import bus as sbus
        d = os.path.join(self.home, "state", "sessions", "by-pid")
        os.makedirs(d, exist_ok=True)
        real_remove, real_chain = os.remove, sbus._chain
        def racing_remove(path):
            real_remove(path)
            raise FileNotFoundError(path)  # the other SessionStart got there first
        open(os.path.join(d, "999999"), "w").close()  # a dead pid's entry
        names = {101: "sh", 102: "claude", 103: "bash", 104: "claude"}  # hook → sh → child claude -p → bash → parent
        os.remove, sbus._chain = racing_remove, lambda pid, depth: [(p, "t") for p in names][:depth]
        real_comm = sbus._comm if hasattr(sbus, "_comm") else None
        sbus._comm = lambda pid: names.get(pid, "")
        try:
            sbus.record_pid("sess-child")
        finally:
            os.remove, sbus._chain = real_remove, real_chain
            if real_comm:
                sbus._comm = real_comm
        self.assertEqual(sorted(os.listdir(d)), ["101", "102"], "up to its own claude, not the parent session's")

    def test_it_finds_its_session_from_the_session_start_hook(self):
        from sampo import bus as sbus
        saved = {k: os.environ.pop(k, None) for k in ("CLAUDE_CODE_SESSION_ID", "FOREMAN_SESSION_ID")}
        try:
            self.assertIsNone(sbus.own_session())
            self.hook("SessionStart", {"session_id": "sess-pid", "source": "startup"})
            self.assertEqual(sbus.own_session(), "sess-pid", "this process is the hook's parent")
        finally:
            os.environ.update({k: v for k, v in saved.items() if v})

    def test_a_resumed_session_keeps_its_watcher(self):
        # T-0841 (live, 2026-10-15): Claude Code started as one session, then resumed this conversation under another
        # id; the monitor kept the first id from its environment, so the resumed session's mail never printed
        import io
        import threading
        import time
        from sampo import bus as sbus
        saved = {k: os.environ.pop(k, None) for k in ("CLAUDE_CODE_SESSION_ID", "FOREMAN_SESSION_ID")}
        buf, done = io.StringIO(), threading.Event()
        try:
            self.hook("SessionStart", {"session_id": "sess-first", "source": "startup"})
            t = threading.Thread(target=sbus.watch, kwargs={"interval": 0.05, "out": buf, "stop": done.is_set})
            t.start()
            time.sleep(0.3)
            self.hook("SessionStart", {"session_id": "sess-resumed", "source": "resume"})
            time.sleep(0.3)
            sbus.send("sess-resumed", "picked up after the resume", sender="sess-1")
            for _ in range(100):
                if "picked up" in buf.getvalue():
                    break
                time.sleep(0.05)
        finally:
            done.set()
            t.join(5)
            os.environ.update({k: v for k, v in saved.items() if v})
        self.assertIn("picked up after the resume", buf.getvalue())

    def test_a_resume_in_the_same_claude_process_is_not_another_session(self):
        # T-0841: the same live resume told the session another one (its own first id) was active 0m ago
        from sampo import hooks
        now = c.now()
        self.assertIsNone(hooks._other_session({"id": "sess-first", "seen": now, "pid": 4242}, "sess-b", 4242))
        self.assertIn("Another Claude Code session",
                      hooks._other_session({"id": "sess-first", "seen": now, "pid": 4242}, "sess-b", 999) or "")
        self.assertIn("Another Claude Code session",
                      hooks._other_session({"id": "sess-first", "seen": now}, "sess-b", None) or "", "unknown: as before")

    def test_wake_prefers_a_fresh_watcher_over_tmux(self):
        import time
        from sampo import bus as sbus
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        log = os.path.join(self.tmp, "tmux.log")
        with open(os.path.join(bin_, "tmux"), "w") as f:
            f.write(f"#!/bin/sh\necho \"$@\" >> {log}\n")
        os.chmod(os.path.join(bin_, "tmux"), 0o755)
        env = {"PATH": bin_ + os.pathsep + os.environ["PATH"]}
        self.snap("sess-w", tmux_pane="%7")
        beat = os.path.join(sbus._dir(), "watch-sess-w")
        open(beat, "w").close()
        self.assertIn("woke 1", self.fm("bus", "send", "sess-w", "hi", "--wake", env=env).stdout)
        self.assertFalse(os.path.exists(log), "the watcher delivers it: nothing typed into tmux")
        os.utime(beat, (time.time() - 60, time.time() - 60))
        self.assertIn("woke 1", self.fm("bus", "send", "sess-w", "hi again", "--wake", env=env).stdout)
        self.assertTrue(os.path.exists(log), "a stale watcher: tmux stays the fallback")

    def test_the_monitor_entry_uses_only_the_keys_claude_code_reads(self):
        # strict keys, and `claude plugin validate --strict` doesn't check them
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "monitors", "monitors.json")) as f:
            entries = json.load(f)
        for e in entries:
            self.assertLessEqual(set(e), {"name", "command", "description", "when"}, e)
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/bin/fm bus watch", e["command"])
            self.assertTrue(os.access(os.path.join(root, "bin", "fm"), os.X_OK))


class Lease(Base):
    def test_an_edit_leases_its_function_against_other_sessions(self):
        path = os.path.join(self.repo, "mod.py")
        with open(path, "w") as f:
            f.write(MOD.replace("x + 1", "x + 2"))
        self.hook("PostToolUse", {"tool_name": "Edit", "session_id": "sess-a", "tool_response": {},
                                  "tool_input": {"file_path": path, "old_string": "x + 1", "new_string": "x + 2"}})
        r, out = self.note("sess-b", "Edit", {"file_path": path, "old_string": "a = x + 2", "new_string": "a = x"})
        self.assertEqual(out.get("permissionDecision"), "deny", r.stdout + r.stderr)
        self.assertIn("leased", out.get("permissionDecisionReason", ""))
        _, out = self.note("sess-b", "Edit", {"file_path": path, "old_string": "b = y * 2", "new_string": "b = y"})
        self.assertNotEqual(out.get("permissionDecision"), "deny", "another function is free")
        _, out = self.note("sess-a", "Edit", {"file_path": path, "old_string": "a = x + 2", "new_string": "a = x"})
        self.assertNotEqual(out.get("permissionDecision"), "deny", "the holder edits on")
        self.assertIn("mod.py", self.fm("lease", "list").stdout)

    def test_drop_confirms_what_it_dropped_and_lists_no_other_lease(self):
        # T-1729: a drop printed another session's lease line as its result, not the caller's own drop
        a, b = {"CLAUDE_CODE_SESSION_ID": "sess-a"}, {"CLAUDE_CODE_SESSION_ID": "sess-b"}
        self.fm("lease", "take", "a.py::f", "b.py", env=a)
        self.fm("lease", "take", "c.py", env=b)
        out = self.fm("lease", "drop", env=a).stdout
        self.assertIn("Dropped 2 lease(s)", out)
        self.assertIn("dropped: a.py::f", out)
        self.assertIn("dropped: b.py::*", out)
        self.assertNotIn("c.py", out, "another session's lease isn't the drop's result")
        self.assertIn("c.py", self.fm("lease", "list").stdout)

    def test_an_edit_in_a_nested_def_leases_the_outer_def_and_a_method_its_own(self):  # T-3948
        path = os.path.join(self.repo, "f.py")
        src = ("def outer():\n    def inner():\n        k = 2\n    return inner\n\n"
               "class C:\n    def m(self):\n        def h():\n            z = 9\n")
        with open(path, "w") as f:
            f.write(src)
        for new in ("k = 2", "z = 9"):
            self.hook("PostToolUse", {"tool_name": "Edit", "session_id": "sess-a", "tool_response": {},
                                      "tool_input": {"file_path": path, "old_string": "x", "new_string": new}})
        self.assertEqual(sorted(v["symbol"] for v in bus._leases().values()), ["m", "outer"])
        out = self.fm("lease", "drop", env={"CLAUDE_CODE_SESSION_ID": "sess-a"}).stdout
        self.assertIn("dropped: f.py::outer", out)
        self.assertNotIn("::inner", out)

    def test_closing_a_task_releases_its_leases_at_once_and_leaves_another_tasks(self):  # T-3398
        a = {"CLAUDE_CODE_SESSION_ID": "sess-a"}
        self.fm("lease", "take", "a.py", env=a)
        other = json.loads(self.fm("task", "new", "Other", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                   "--step", "s", "--json").stdout)["id"]
        self.fm("focus", other)
        self.fm("lease", "take", "c.py", env=a)
        self.fm("task", "drop", "T-0001", "--reason", "test")
        out = self.fm("lease", "list").stdout
        self.assertNotIn("a.py", out, "the closed task's lease is gone now, not in 20 minutes")
        self.assertIn("c.py", out, "another task's lease stays")

    def lease_files(self):
        return sorted(v["file"] for v in bus._leases().values())

    def test_a_release_leaves_another_projects_lease_of_the_same_task_id(self):  # T-3398 review
        p = self.project()
        other = type("P", (), {"slug": "other-proj"})()
        data = {}
        bus._claim(data, p, "s1", "T-0001", "mine.py", "*", 600)
        bus._claim(data, other, "s1", "T-0001", "theirs.py", "*", 600)
        bus._claim(data, other, "lane-T-0001", "T-0001", "theirs2.py", "*", 600)
        bus._write_leases(data)
        bus.release(p, "T-0001")
        self.assertEqual(self.lease_files(), ["theirs.py", "theirs2.py"])

    def test_block_keeps_the_lane_pre_claim_and_drop_releases_it(self):  # T-3398 review
        p = self.project()
        for verb, left in (("block", ["own.py", "pre.py"]), ("drop", [])):  # T-3432: an open task keeps all its leases
            data = {}
            bus._claim(data, p, "s1", "T-0001", "own.py", "*", 600)
            bus._claim(data, p, "lane-T-0001", "T-0001", "pre.py", "*", 600)
            bus._write_leases(data)
            self.fm("task", verb, "T-0001", "--reason", "x", "--force") if verb == "drop" else self.fm(
                "task", verb, "T-0001", "x")
            self.assertEqual(self.lease_files(), left, verb)

    def test_a_failing_release_never_fails_the_close(self):  # T-3398 review
        data = {}
        bus._claim(data, self.project(), "s1", "T-0001", "own.py", "*", 600)
        bus._write_leases(data)
        d = bus._dir()
        os.chmod(d, 0o555)
        self.addCleanup(lambda: os.path.isdir(d) and os.chmod(d, 0o755))
        r = self.fm("task", "drop", "T-0001", "--reason", "x", "--force")
        self.assertEqual(r.returncode, 0)
        self.assertIn("not released", r.stderr)

    def test_take_confirms_what_it_took_and_lists_no_other_lease(self):  # T-1748
        a, b = {"CLAUDE_CODE_SESSION_ID": "sess-a"}, {"CLAUDE_CODE_SESSION_ID": "sess-b"}
        self.fm("lease", "take", "c.py", env=b)
        out = self.fm("lease", "take", "a.py::f", "b.py", env=a).stdout
        self.assertIn("Took 2 lease(s)", out)
        self.assertIn("took: a.py::f", out)
        self.assertIn("took: b.py::*", out)
        self.assertNotIn("c.py", out, "another session's lease isn't the take's result")

    def test_a_take_over_another_lanes_pre_claim_succeeds_with_a_note_and_an_explicit_lease_still_refuses(self):
        # T-3283: a builder lane's brief pre-claims its scope file-wide; edits treat that as advisory (T-1138), so a
        # take over it must not refuse either (a builder waited 25 min on it); another session's explicit lease still does
        from sampo import bus as sbus
        a, b = {"CLAUDE_CODE_SESSION_ID": "sess-a"}, {"CLAUDE_CODE_SESSION_ID": "sess-b"}
        sbus.preclaim(self.project(), "T-0002", ["cli.py"])
        r = self.fm("lease", "take", "cli.py::x", env=a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Took 1 lease(s)", r.stdout)
        self.assertIn("lane-T-0002", r.stderr, "the take names the pre-claim it went over")
        r = self.fm("lease", "take", "cli.py::x", env=b, check=False)
        self.assertEqual(r.returncode, 1, "an explicit lease of another session still refuses")
        self.assertIn("overlaps session sess-a", r.stderr)

    def test_a_long_drop_is_cut_and_a_lanes_claims_still_held_are_said(self):
        # T-1731 review: one line per lease uncapped; a lane's bare drop said "Dropped 0" with its pre-claims held
        from sampo import bus as sbus
        a = {"CLAUDE_CODE_SESSION_ID": "sess-a"}
        self.fm("lease", "take", *[f"m{i}.py" for i in range(10)], env=a)
        out = self.fm("lease", "drop", env=a).stdout
        self.assertIn("Dropped 10 lease(s)", out)
        self.assertEqual(out.count("dropped: "), 8)
        self.assertIn("… 2 more", out)
        sbus.preclaim(self.project(), "T-0001", ["scope.py"])
        out = self.fm("lease", "drop", env=a).stdout
        self.assertIn("still held: 1 lane claim(s) of T-0001, freed when its lane merges or is removed (fm lease list)",
                      out)  # T-1837: builders read the bare count as unfinished work


    def test_a_fm_code_workers_edits_lease_as_the_run_so_the_run_and_its_next_worker_edit_on(self):
        """T-1294: each worker turn is its own Claude session; leasing under it made fm code's judge put the
        worker's fix back, and the next turn's session was refused the same function."""
        path = os.path.join(self.repo, "mod.py")
        with open(path, "w") as f:
            f.write(MOD.replace("x + 1", "x + 2"))
        run = {"FOREMAN_WORKER_OF": "fm-code-T-0001"}
        self.hook("PostToolUse", {"tool_name": "Edit", "session_id": "worker-turn-1", "tool_response": {},
                                  "tool_input": {"file_path": path, "old_string": "x + 1", "new_string": "x + 2"}},
                  env=run)
        edit = {"tool_name": "Edit", "tool_input": {"file_path": path, "old_string": "a = x + 2", "new_string": "a = x"}}
        for sid, env in (("fm-code-T-0001", None), ("worker-turn-2", run)):
            out = (parse(self.hook("PreToolUse", dict(edit, session_id=sid), env=env)) or {}).get(
                "hookSpecificOutput") or {}
            self.assertNotEqual(out.get("permissionDecision"), "deny", f"{sid}: the run's own lease")
        out = (parse(self.hook("PreToolUse", dict(edit, session_id="someone-else"))) or {}).get("hookSpecificOutput") or {}
        self.assertEqual(out.get("permissionDecision"), "deny", "a session outside the run is still refused")
        stray = {"FOREMAN_WORKER_OF": "my-shell-rc"}  # only fm code's own value names a run
        out = (parse(self.hook("PreToolUse", dict(edit, session_id="someone-else"), env=stray)) or {}).get(
            "hookSpecificOutput") or {}
        self.assertEqual(out.get("permissionDecision"), "deny", "a stray FOREMAN_WORKER_OF isn't a run")


class Conductor(Base):
    def test_a_session_whose_claude_is_gone_or_a_one_off_isnt_listed(self):
        # T-1001: killed UI-harness sessions stayed 'live' for 15 minutes and filled the pane's More lines
        import subprocess
        from helpers import PLUGIN
        self.snap("live0001")
        self.snap("dead0001")
        gone = subprocess.Popen(["true"])
        gone.wait()
        d = os.path.join(self.home, "state", "sessions", "by-pid")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, str(gone.pid)), "w") as f:
            json.dump({"sid": "dead0001", "start": "1"}, f)
        out = self.fm("conductor").stdout
        self.assertIn("live0001", out)
        self.assertNotIn("dead0001", out)
        subprocess.run([os.path.join(PLUGIN, "hooks", "statusline")], input=json.dumps({"session_id": "sess-oneoff", "cwd": self.repo}),
                       text=True, capture_output=True, timeout=15, cwd=self.repo,
                       env=dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_ONEOFF="1"))
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "sessions", "sess-oneoff.json")))

    def test_a_snapshot_naming_a_dead_or_reused_claude_pid_isnt_listed(self):
        # T-1003: a later SessionStart swept the by-pid records, so the snapshot has to carry its own pid
        import subprocess
        self.snap("live0002", claude_pid=os.getpid(), claude_start=self.start_of(os.getpid()))
        self.snap("reuse002", claude_pid=os.getpid(), claude_start="1")
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.snap("dead0002", claude_pid=gone.pid, claude_start="1")
        out = self.fm("conductor").stdout
        self.assertIn("live0002", out)
        self.assertNotIn("dead0002", out)
        self.assertNotIn("reuse002", out)

    @staticmethod
    def start_of(pid):  # T-1249: as fmbus reads it, ps where there's no /proc (macOS)
        from sampo import bus as sbus
        return sbus._proc(pid)[1]

    def test_it_lists_live_sessions_and_its_steer_reaches_them(self):
        self.snap("sess-x", context_pct=41)
        self.snap("sess-y", ts="2020-01-01T00:00:00Z")
        out = self.fm("conductor").stdout
        self.assertIn("sess-x", out)
        self.assertNotIn("sess-y", out, "not seen for years: not live")
        self.fm("conductor", "steer", "stop touching the guard")
        self.assertIn("stop touching the guard", self.note("sess-x")[1].get("additionalContext", ""))

    def conductor(self, action, *words):
        """T-3932: in-process, so _ssh can be stubbed: no test ever ssh's anywhere."""
        import argparse
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bus.cmd_conductor(argparse.Namespace(action=action, words=list(words), json=False, wake=False))
        return buf.getvalue()

    def test_send_to_one_remote_calls_only_that_remote_and_an_unknown_name_lists_the_known_ones(self):  # T-3932
        import subprocess
        from unittest import mock
        from sampo import cli
        self.fm("conductor", "remote", "add", "jarvis", "dev@192.0.2.7")
        self.fm("conductor", "remote", "add", "other", "ops@10.0.0.2")
        self.snap("sess-aaaa01")
        calls = []

        def ssh(r, command, timeout=20):
            calls.append((r["target"], command))
            return subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(bus, "_ssh", ssh):
            self.conductor("send", "jarvis", "use the new parser")
            self.assertEqual(len(calls), 1, calls)
            self.assertEqual(calls[0][0], "dev@192.0.2.7")
            self.assertIn("bus send all 'use the new parser'", calls[0][1])
            self.assertNotIn("steer", calls[0][1], "a send is not a steer")
            with self.assertRaises(cli.UsageError) as ctx:
                self.conductor("send", "nobody", "hi")
            fail = mock.patch.object(bus, "_ssh", lambda r, command, timeout=20: subprocess.CompletedProcess([], 255, "", "no"))
            with fail, self.assertRaises(cli.UsageError) as dead:
                self.conductor("send", "other", "hi")
            self.assertIn("did not take the message", str(dead.exception))
        self.assertEqual(len(calls), 1, "an unknown name calls no remote")
        for known in ("jarvis", "other", "sess-aaaa01"):
            self.assertIn(known, str(ctx.exception))

    def test_send_to_a_session_prefix_writes_only_its_mailbox_and_steer_still_broadcasts(self):  # T-3932
        import subprocess
        from unittest import mock
        self.fm("conductor", "remote", "add", "jarvis", "dev@192.0.2.7")
        self.snap("sess-aaaa01")
        self.snap("sess-bbbb02")
        calls = []

        def ssh(r, command, timeout=20):
            calls.append(command)
            return subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(bus, "_ssh", ssh):
            self.conductor("send", "sess-aa", "only for you")
            self.assertEqual(calls, [], "a local session's send never ssh's")
            self.assertIn("only for you", self.note("sess-aaaa01")[1].get("additionalContext", ""))
            self.assertNotIn("only for you", self.note("sess-bbbb02")[1].get("additionalContext", ""))
            from sampo import cli
            with self.assertRaises(cli.UsageError) as amb:
                self.conductor("send", "sess-", "x")  # two live sessions share the prefix: nobody is sent to
            self.assertIn("matches 2", str(amb.exception))
            self.conductor("steer", "freeze the API")
            self.assertEqual(len(calls), 1, "steer still reaches each remote")
            self.assertIn("bus send all", calls[0])
            self.assertIn("--type steer", calls[0])
        self.assertIn("freeze the API", self.note("sess-bbbb02")[1].get("additionalContext", ""))

    def test_the_real_parser_takes_send_and_an_unknown_name_exits_non_zero_listing_the_known(self):  # T-3932
        p = self.fm("conductor", "send", "nosuch-name", "x", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("invalid choice", p.stderr, "send is a real conductor action")
        self.assertIn("No remote or live session named nosuch-name", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


class FailingMark(HookCase):
    """T-1449: one note builder raising (a full disk failed the bus's seen-mark write) dropped every PreToolUse note."""

    def test_a_failing_mark_write_keeps_the_other_notes_and_the_mail(self):
        self.fm("init")
        tid = self.task(scope=["src/auth/**"])
        self.fm("bus", "send", "sess-2", "use the new parser", env={"CLAUDE_CODE_SESSION_ID": "sess-1"})
        bus = os.path.join(self.home, "state", "bus")
        os.chmod(bus, 0o555)  # the mark can't be written, as on a full disk
        try:
            r = self.hook("PreToolUse", {"tool_name": "Edit", "session_id": "sess-2", "tool_input": {
                "file_path": os.path.join(self.repo, "docs", "x.md"), "old_string": "a", "new_string": "b"}})
        finally:
            os.chmod(bus, 0o755)
        ctx = ((parse(r) or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        self.assertIn(f"outside {tid} scope", ctx)
        self.assertIn("use the new parser", ctx)


class NoProcfs(HookCase):  # T-2492: macOS has no /proc, so a process's start time comes from ps
    def test_a_slow_ps_is_asked_again_before_the_process_counts_as_gone(self):
        import subprocess
        from unittest import mock
        from sampo import bus as sbus
        real_isdir = os.path.isdir
        slow = subprocess.TimeoutExpired(["ps"], 5)
        done = subprocess.CompletedProcess(["ps"], 0, stdout="  1 Sat Nov  1 12:00:00 2026\n")
        with mock.patch.object(sbus.os.path, "isdir", side_effect=lambda x: False if x == "/proc" else real_isdir(x)), \
                mock.patch("builtins.open", side_effect=OSError("no procfs")), \
                mock.patch.object(sbus.subprocess, "run", side_effect=[slow, done]) as run:
            self.assertEqual(sbus._proc(424242), (1, "Sat Nov  1 12:00:00 2026"))
        self.assertEqual(len(run.call_args_list), 2)
        self.assertGreater(run.call_args_list[1].kwargs["timeout"], run.call_args_list[0].kwargs["timeout"])

    def comm(self, *outs):  # T-2512: _comm on a host without procfs, ps answering outs in turn
        import subprocess
        from unittest import mock
        from sampo import bus as sbus
        real_isdir = os.path.isdir
        done = [subprocess.CompletedProcess(["ps"], 0, stdout=o) for o in outs]
        with mock.patch.object(sbus.os.path, "isdir", side_effect=lambda x: False if x == "/proc" else real_isdir(x)), \
                mock.patch("builtins.open", side_effect=OSError("no procfs")), \
                mock.patch.object(sbus.subprocess, "run", side_effect=done) as run:
            return sbus._comm(424242), len(run.call_args_list)

    def test_a_shebang_script_named_claude_counts_as_claude(self):  # T-2512: macOS ps names it by its interpreter
        self.assertEqual(self.comm("/bin/sh\n", "/bin/sh /var/folders/x/bin/claude /usr/bin/python3 fm lane brief T-1\n"),
                         ("claude", 2))
        self.assertEqual(self.comm("node\n", "node --no-warnings /usr/local/bin/claude --resume\n"), ("claude", 2))
        self.assertEqual(self.comm("/bin/bash\n", "/bin/bash ./build.sh claude\n"), ("bash", 2))
        self.assertEqual(self.comm("/Users/me/.local/bin/claude\n"), ("claude", 1))
