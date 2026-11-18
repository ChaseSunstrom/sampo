"""fm session (T-0323): agent sessions started detached, one normalised event stream per session, resumed on send,
stopped as a whole process group — with a stub claude; codex, gemini and opencode lines through the same normaliser."""
import json
import os
import subprocess
import sys
import time
from unittest import mock

from helpers import FM, ForemanTestCase

from sampo import session as s

# stream-json like claude -p: init, an assistant text + tool call, its result, the final result. STUB_SLEEP holds the
# turn open (for stop); the prompt and --resume come back in the text so the test can see what the runner passed.
STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
a = sys.argv[1:]
prompt = a[a.index("-p") + 1]
resume = a[a.index("--resume") + 1] if "--resume" in a else None
with open(os.environ["STUB_PIDS"], "a") as f:
    f.write(f"{os.getpid()}\n")
def out(o):
    print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "stub"})
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
out({"type": "assistant", "message": {"content": [{"type": "text", "text": f"got {prompt} resume={resume}"},
    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls -la"}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "a b",
    "is_error": False}]}})
out({"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.01,
     "session_id": "sess-1"})
'''


class Session(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.pids = os.path.join(self.tmp, "pids")
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_PIDS": self.pids}

    def tearDown(self):  # before the scratch folder goes
        self.stop_all()
        super().tearDown()

    def stop_all(self):
        for row in json.loads(self.fm("session", "list", "--json", env=self.env).stdout)["sessions"]:
            if row["status"] in ("starting", "running"):
                self.fm("session", "stop", row["id"], env=self.env, check=False)

    def start(self, prompt, **env):
        out = self.fm("session", "start", "--agent", "claude", "--cwd", self.repo, "--json", prompt,
                      env=dict(self.env, **env)).stdout
        return json.loads(out)["id"]

    def events(self, sid):
        return [json.loads(line) for line in self.fm("session", "tail", sid, env=self.env).stdout.splitlines()]

    def wait(self, cond, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            got = cond()
            if got:
                return got
            time.sleep(0.1)
        self.fail("timed out")

    def row(self, sid):
        return next(r for r in json.loads(self.fm("session", "list", "--json", env=self.env).stdout)["sessions"]
                    if r["id"] == sid)

    def test_a_session_runs_detached_and_resumes_on_send(self):
        sid = self.start("hello")
        self.wait(lambda: sum(e["kind"] == "result" for e in self.events(sid)) == 1)
        ev = self.events(sid)
        self.assertEqual([e["kind"] for e in ev], ["user", "init", "text", "tool", "tool_result", "result"])
        self.assertEqual(ev[2]["text"], "got hello resume=None")
        self.assertEqual((ev[3]["tool"], ev[3]["detail"]), ("Bash", "ls -la"))
        self.assertTrue(ev[4]["ok"] and ev[5]["ok"])
        self.assertEqual(ev[5]["cost_usd"], 0.01)
        self.wait(lambda: self.row(sid)["status"] == "idle")
        self.fm("session", "send", sid, "again", env=self.env)
        self.wait(lambda: sum(e["kind"] == "result" for e in self.events(sid)) == 2)
        self.assertIn("got again resume=sess-1", [e.get("text") for e in self.events(sid)])
        row = self.wait(lambda: (r := self.row(sid))["status"] == "idle" and r)
        self.assertEqual((row["agent"], row["turns"], row["agent_session"], row["title"]), ("claude", 2, "sess-1", "hello"))
        self.assertEqual(len(self.events(sid)[-1:]), 1)
        follow = subprocess.run([sys.executable, FM, "session", "tail", sid, "--from", "6"], capture_output=True,
                                text=True, env=dict(os.environ, FOREMAN_HOME=self.home, **self.env))
        self.assertEqual(json.loads(follow.stdout.splitlines()[0])["kind"], "user")  # from line 6: the second turn

    def test_stop_ends_the_whole_process_group(self):
        sid = self.start("slow", STUB_SLEEP="60")
        self.wait(lambda: os.path.exists(self.pids) and open(self.pids).read().strip())
        child = int(open(self.pids).read().split()[0])
        self.fm("session", "stop", sid, env=self.env)
        self.wait(lambda: not _alive(child))
        self.assertEqual(self.row(sid)["status"], "stopped")
        self.assertEqual(self.events(sid)[-1]["kind"], "status")

    def test_stop_marks_before_it_reads_the_pid(self):
        # T-1789: stop read the runner's pid, then wrote the stopped marker. A runner that set its pid and checked for
        # the marker in between was never killed, ran its turn on, and its later writes put the status back to running.
        sid = self.start("hello")
        self.wait(lambda: self.row(sid)["status"] == "idle")
        marked, real = [], s.read_meta

        def spy(x):
            marked.append(os.path.exists(os.path.join(s._dir(x), "stopped")))
            return real(x)
        with mock.patch.object(s, "read_meta", spy):
            s.stop(sid)
        self.assertTrue(marked and marked[0], "stop read the pid before it wrote the stopped marker")

    def test_a_dead_runner_reads_as_died(self):
        sid = self.start("hello")
        self.wait(lambda: self.row(sid)["status"] == "idle")
        dead = subprocess.Popen(["true"])
        dead.wait()
        s.write_meta(sid, dict(s.read_meta(sid), status="running", pid=dead.pid))
        self.assertEqual(self.row(sid)["status"], "died")

    def test_review_fixes(self):  # T-0323 review: options inside a message, stop before the runner, idle drops the pid
        out = self.fm("session", "start", "--agent", "claude", "--cwd", self.repo, "--json", "--", "-1 fix the --follow flag",
                      env=self.env).stdout
        sid = json.loads(out)["id"]
        self.wait(lambda: self.row(sid)["status"] == "idle")
        self.assertEqual(self.events(sid)[0]["text"], "-1 fix the --follow flag")
        self.assertIsNone(s.read_meta(sid)["pid"], "an idle session holds no pid a reuse could match")
        self.fm("session", "send", sid, "--", "and --model too", env=self.env)
        self.wait(lambda: sum(e["kind"] == "result" for e in self.events(sid)) == 2)
        self.assertIn("and --model too", [e.get("text") for e in self.events(sid)])
        # T-0332: options may follow the id, as people (and Claude) type them; the message still starts after --
        out = self.fm("session", "send", sid, "--json", "--", "-v and three", env=self.env).stdout
        self.assertEqual(json.loads(out)["id"], sid)
        self.wait(lambda: sum(e["kind"] == "result" for e in self.events(sid)) == 3)
        self.assertIn("-v and three", [e.get("text") for e in self.events(sid)])
        self.assertIn("--jsn", self.fm("session", "send", sid, "--jsn", "x", env=self.env, check=False).stderr)
        # its review: only options right before -- move; a flag inside an unprotected message is still an error
        p = self.fm("session", "send", sid, "please", "add", "--title", "to", "the", "list", env=self.env, check=False)
        self.assertNotEqual(p.returncode, 0)
        slow = self.start("slow", STUB_SLEEP="60")
        self.fm("session", "stop", slow, env=self.env)  # before its runner has taken the message, most likely
        time.sleep(1.5)
        self.assertEqual(self.row(slow)["status"], "stopped")
        self.assertNotIn("result", [e["kind"] for e in self.events(slow)])

    def test_list_follow_streams_new_sessions(self):
        proc = subprocess.Popen([sys.executable, FM, "session", "list", "--json", "--follow", "--interval", "0.1"],
                                stdout=subprocess.PIPE, text=True, env=dict(os.environ, FOREMAN_HOME=self.home, **self.env))
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close()))
        self.assertEqual(json.loads(proc.stdout.readline())["sessions"], [])
        sid = self.start("hello")
        self.assertEqual(json.loads(proc.stdout.readline())["sessions"][0]["id"], sid)

    def test_agents_lists_what_is_installed(self):
        rows = {r["agent"]: r for r in json.loads(self.fm("session", "agents", "--json", env=self.env).stdout)["agents"]}
        self.assertEqual(set(rows), {"claude", "codex", "gemini", "opencode", "agy", "cursor"})
        self.assertTrue(rows["claude"]["installed"])


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0): reaped means gone
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


class Normalise(ForemanTestCase):
    def kinds(self, agent, lines):
        st = {}
        return [e for o in lines for e in s.normalise(agent, o, st)], st

    def test_codex_exec_json(self):
        ev, st = self.kinds("codex", [
            {"type": "thread.started", "thread_id": "th-1"}, {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "thinking"}},
            {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "ls", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "ls",
                                                "aggregated_output": "a b", "exit_code": 0, "status": "completed"}},
            {"type": "item.completed", "item": {"id": "i2", "type": "file_change", "changes": [{"path": "x.py", "kind": "update"}],
                                                "status": "completed"}},
            {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10}}])
        self.assertEqual([e["kind"] for e in ev], ["init", "tool", "tool_result", "tool", "tool_result", "text", "result"])
        self.assertEqual((ev[0]["agent_session"], ev[1]["detail"], ev[3]["detail"]), ("th-1", "ls", "x.py"))
        self.assertTrue(ev[-1]["ok"])
        failed, _ = self.kinds("codex", [{"type": "turn.failed", "error": {"message": "quota"}}])
        self.assertEqual((failed[0]["kind"], failed[0]["ok"], failed[0]["text"]), ("result", False, "quota"))

    def test_gemini_stream_json(self):
        ev, _ = self.kinds("gemini", [
            {"type": "init", "session_id": "g-1", "model": "gemini-x"},
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "message", "role": "assistant", "content": "Hel", "delta": True},
            {"type": "message", "role": "assistant", "content": "lo", "delta": True},
            {"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t1", "parameters": {"command": "ls"}},
            {"type": "tool_result", "tool_id": "t1", "status": "success", "output": "a"},
            {"type": "result", "status": "success", "stats": {}}])
        self.assertEqual([e["kind"] for e in ev], ["init", "text", "tool", "tool_result", "result"])
        self.assertEqual((ev[1]["text"], ev[2]["detail"]), ("Hello", "ls"))

    # T-3347: the cursor and agy fixtures are written from the documented event shapes, NOT recorded from real binaries.
    def test_cursor_stream_json(self):
        ev, _ = self.kinds("cursor", [
            {"type": "system", "subtype": "init", "session_id": "c-1", "model": "gpt-x", "cwd": "/w"},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}, "session_id": "c-1"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}, "session_id": "c-1"},
            {"type": "tool_call", "subtype": "started", "call_id": "k1",
             "tool_call": {"readToolCall": {"args": {"path": "a.py"}}}},
            {"type": "tool_call", "subtype": "completed", "call_id": "k1",
             "tool_call": {"readToolCall": {"args": {"path": "a.py"}, "result": {"success": {"content": "x", "totalLines": 1}}}}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Hello", "session_id": "c-1"}])
        self.assertEqual([e["kind"] for e in ev], ["init", "text", "tool", "tool_result", "result"])
        self.assertEqual((ev[0]["agent_session"], ev[2]["tool"], ev[2]["detail"], ev[3]["ok"], ev[4]["ok"]),
                         ("c-1", "read", "a.py", True, True))
        bad, _ = self.kinds("cursor", [{"type": "result", "subtype": "error", "is_error": True, "result": "no key"}])
        self.assertEqual((bad[0]["ok"], bad[0]["text"]), (False, "no key"))

    def test_run_failover_accepts_agy_and_cursor(self):
        from sampo import cli
        for a in ("agy", "cursor"):
            self.assertEqual(cli.build_parser().parse_args(["run", "--failover", a]).failover, a)

    def test_agy_json_envelope(self):
        ev, _ = self.kinds("agy", [{"conversation_id": "a-1", "status": "success", "response": "done",
                                    "usage": {"input_tokens": 3}}])
        self.assertEqual([e["kind"] for e in ev], ["init", "text", "result"])
        self.assertEqual((ev[0]["agent_session"], ev[1]["text"], ev[2]["ok"]), ("a-1", "done", True))
        bad, _ = self.kinds("agy", [{"conversation_id": "a-1", "status": "error", "response": ""}])
        self.assertFalse(bad[-1]["ok"])

    def test_cursor_and_agy_argv(self):
        self.assertEqual(s.argv("cursor", "go", "c-1", "gpt-x", []),
                         ["cursor-agent", "-p", "go", "--output-format", "stream-json", "--resume", "c-1",
                          "--model", "gpt-x"])
        self.assertNotIn("--force", s.argv("cursor", "go", None, None, []))  # write mode is the caller's grant
        self.assertIn("--force", s.argv("cursor", "go", None, None, ["--force"]))
        self.assertEqual(s.argv("agy", "go", None, None, []), ["agy", "-p", "go", "--output-format", "stream-json"])

    def test_opencode_run_json(self):
        ev, _ = self.kinds("opencode", [
            {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
            {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "hi"}},
            {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": "bash", "callID": "c1",
                                                                 "state": {"status": "completed", "input": {"command": "ls"},
                                                                           "output": "a"}}},
            {"type": "error", "sessionID": "ses_1", "error": {"name": "APIError", "data": {"message": "boom"}}}])
        self.assertEqual([e["kind"] for e in ev], ["init", "text", "tool", "tool_result", "error"])
        self.assertEqual((ev[0]["agent_session"], ev[2]["detail"], ev[4]["text"]), ("ses_1", "ls", "boom"))

    def test_odd_shapes_never_crash_the_runner(self):
        for agent, o in [("codex", {"type": "turn.failed", "error": "boom"}), ("opencode", {"type": "error", "error": "x"}),
                         ("claude", {"type": "assistant", "message": "x"}), ("gemini", {"type": "tool_result", "error": 3})]:
            self.assertTrue(all(e["kind"] in ("raw", "result", "error", "tool_result") for e in s.normalise(agent, o, {})))
        self.assertNotIn("--x", s.argv("codex", "go", "--x", None, []))  # a resume id is never an option
        with self.assertRaises(ValueError):
            s.read_meta("20260101-000000-abcd\n")

    def test_unknown_lines_are_kept_raw_and_argv_resumes(self):
        ev, _ = self.kinds("claude", [{"type": "something_new"}, {"type": "system", "subtype": "hook_started"},
                                      {"type": "rate_limit_event", "rate_limit_info": {}}])
        self.assertEqual([e["kind"] for e in ev], ["raw"])  # claude's housekeeping lines are dropped
        self.assertEqual(s.argv("claude", "go", "sess-1", "opus", []),
                         ["claude", "-p", "go", "--output-format", "stream-json", "--verbose", "--resume", "sess-1",
                          "--model", "opus"])
        self.assertEqual(s.argv("codex", "go", "th-1", None, []), ["codex", "exec", "--json", "--full-auto", "resume", "th-1", "go"])
        self.assertEqual(s.argv("gemini", "go", None, None, ["--yolo"]),
                         ["gemini", "-p", "go", "--output-format", "stream-json", "--yolo"])
        self.assertEqual(s.argv("opencode", "go", "ses_1", None, []),
                         ["opencode", "run", "--format", "json", "--session", "ses_1", "go"])
