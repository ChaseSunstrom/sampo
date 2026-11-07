"""fm claude (T-0328): every Claude Code session on this device, read from Claude Code's own store — list, show (text,
tools, images), subagents, scratchpad files, and send (a headless continuation by the session's own id)."""
import base64
import json
import os
import time

from helpers import ForemanTestCase

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
SID = "11111111-2222-4333-8444-555555555555"
HEADLESS = "99999999-2222-4333-8444-555555555555"
AGENT = "a1b2c3d4e5f6"

STUB = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB_ARGV"], "w") as f:
    json.dump(sys.argv[1:], f)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "forked-1"}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": "forked-1"}))
'''


def entry(kind, content=None, **kw):
    e = {"type": kind, "sessionId": SID, "cwd": "/work/app", "entrypoint": "cli", "version": "2.1.287",
         "gitBranch": "main", "timestamp": "2026-10-06T00:35:01.018Z", "uuid": f"u{time.time_ns()}", **kw}
    if content is not None:
        e["message"] = {"role": "user" if kind == "user" else "assistant", "content": content,
                        **({"model": "claude-opus-5-5"} if kind == "assistant" else {})}
    return e


class Claude(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = os.path.join(self.tmp, "claude-cfg")
        self.tmpdir = os.path.join(self.tmp, "tmpdir")
        proj = os.path.join(self.cfg, "projects", "-work-app")
        os.makedirs(os.path.join(proj, SID, "subagents"))
        lines = [
            {"type": "attachment", "attachment": {"type": "hook_success"}, "cwd": "/work/app", "entrypoint": "cli",
             "sessionId": SID},
            entry("user", "Fix the login timeout"),
            entry("assistant", [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Looking."},
                                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
            entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py\nb.py", "is_error": False}]),
            entry("assistant", [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/x/shot.png"}}]),
            entry("user", [{"type": "tool_result", "tool_use_id": "t2", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}]}]),
            entry("user", [{"type": "text", "text": "and this one"},
                           {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}]),
            entry("user", "<local-command-stdout>noise</local-command-stdout>", isMeta=True),
            {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "also the logout"}, "sessionId": SID},
            entry("assistant", [{"type": "tool_use", "id": "t3", "name": "Agent",
                                 "input": {"subagent_type": "foreman:fm-recon", "description": "Map the auth code"}}]),
            {"type": "ai-title", "aiTitle": "Login timeout fix", "sessionId": SID},
            {"type": "last-prompt", "sessionId": SID},
        ]
        with open(os.path.join(proj, f"{SID}.jsonl"), "w") as f:  # the session worked in this test's repo
            f.write("".join(json.dumps(x) + "\n" for x in lines).replace("/work/app", self.repo))
        with open(os.path.join(proj, SID, "subagents", f"agent-{AGENT}.jsonl"), "w") as f:
            f.write(json.dumps(entry("assistant", [{"type": "text", "text": "Auth lives in auth.py"}],
                                     isSidechain=True)) + "\n")
        with open(os.path.join(proj, SID, "subagents", f"agent-{AGENT}.meta.json"), "w") as f:
            json.dump({"agentType": "foreman:fm-recon", "description": "Map the auth code", "toolUseId": "t3"}, f)
        with open(os.path.join(proj, f"{HEADLESS}.jsonl"), "w") as f:
            f.write(json.dumps(dict(entry("user", "Review this diff"), sessionId=HEADLESS, entrypoint="sdk-py")) + "\n")
        scratch = os.path.join(self.tmpdir, f"claude-{os.getuid()}", "-work-app", SID, "scratchpad")
        os.makedirs(os.path.join(scratch, "sub"))
        with open(os.path.join(scratch, "notes.txt"), "w") as f:
            f.write("remember the timeout")
        with open(os.path.join(scratch, "sub", "shot.png"), "wb") as f:
            f.write(base64.b64decode(PNG))
        os.symlink("/etc/hostname", os.path.join(scratch, "escape.txt"))
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        with open(os.path.join(self.bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(self.bindir, "claude"), 0o755)
        self.argv = os.path.join(self.tmp, "argv.json")
        self.env = {"CLAUDE_CONFIG_DIR": self.cfg, "TMPDIR": self.tmpdir, "STUB_ARGV": self.argv,
                    "PATH": self.bindir + os.pathsep + os.environ["PATH"]}

    def tearDown(self):
        for row in json.loads(self.fm("session", "list", "--json", env=self.env).stdout)["sessions"]:
            if row["status"] in ("starting", "running"):
                self.fm("session", "stop", row["id"], env=self.env, check=False)
        super().tearDown()

    def j(self, *args):
        return json.loads(self.fm("claude", *args, "--json", env=self.env).stdout)

    def test_list_shows_terminal_sessions_and_hides_headless_ones(self):
        rows = self.j("list")["sessions"]
        self.assertEqual([r["id"] for r in rows], [SID])
        r = rows[0]
        self.assertEqual((r["title"], r["cwd"], r["kind"], r["branch"], r["subagents"], r["live"]),
                         ("Login timeout fix", self.repo, "terminal", "main", 1, True))  # just written: live
        self.assertEqual(r["prompt"], "Fix the login timeout")
        old = time.time() - 3600
        for name in os.listdir(os.path.join(self.cfg, "projects", "-work-app")):
            if name.endswith(".jsonl"):
                os.utime(os.path.join(self.cfg, "projects", "-work-app", name), (old, old))
        self.assertFalse(self.j("list")["sessions"][0]["live"])
        snap = os.path.join(self.home, "state", "sessions", f"{SID}.json")
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        with open(snap, "w") as f:
            json.dump({"session_id": SID, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
        self.assertTrue(self.j("list")["sessions"][0]["live"], "a fresh statusline snapshot means the terminal is open")
        self.assertEqual({r["kind"] for r in self.j("list", "--all")["sessions"]}, {"terminal", "headless"})

    def test_show_reads_text_tools_images_and_subagents(self):
        ev = [json.loads(line) for line in self.fm("claude", "show", SID, env=self.env).stdout.splitlines()]
        self.assertEqual([e["kind"] for e in ev],
                         ["user", "text", "tool", "tool_result", "tool", "image", "tool_result", "user", "image", "user",
                          "tool"])
        self.assertEqual((ev[9]["text"], ev[9]["queued"]), ("also the logout", True))  # typed mid-turn
        self.assertEqual((ev[2]["tool"], ev[2]["detail"], ev[3]["ok"]), ("Bash", "ls", True))
        self.assertEqual(ev[5]["tool_use_id"], "t2")
        self.assertIn("foreman:fm-recon", ev[10]["detail"])
        img = self.j("image", SID, ev[8]["ref"])
        self.assertEqual((img["media_type"], img["data"]), ("image/png", PNG))
        later = [json.loads(line) for line in self.fm("claude", "show", SID, "--from", str(ev[7]["n"]), env=self.env)
                 .stdout.splitlines()]
        self.assertEqual(later[0]["text"], "and this one")
        agents = self.j("agents", SID)["agents"]
        self.assertEqual((agents[0]["id"], agents[0]["type"], agents[0]["description"]),
                         (AGENT, "foreman:fm-recon", "Map the auth code"))
        sub = [json.loads(line) for line in self.fm("claude", "show", SID, "--agent", AGENT, env=self.env).stdout.splitlines()]
        self.assertEqual(sub[0]["text"], "Auth lives in auth.py")

    def test_scratchpad_files_stay_inside_the_scratchpad(self):
        files = {f["path"]: f for f in self.j("files", SID)["files"]}
        self.assertEqual(set(files), {"notes.txt", "sub/shot.png", "escape.txt"})
        self.assertEqual(files["sub/shot.png"]["kind"], "image")
        self.assertEqual(self.j("file", SID, "notes.txt")["text"], "remember the timeout")
        self.assertEqual(self.j("file", SID, "sub/shot.png")["data"], PNG)
        for bad in ["../../../../etc/hostname", "escape.txt", "/etc/hostname"]:
            r = self.fm("claude", "file", SID, bad, "--json", env=self.env, check=False)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertNotEqual(self.fm("claude", "show", "../x", env=self.env, check=False).returncode, 0)

    def test_review_fixes(self):  # T-0328 review
        proj = os.path.join(self.cfg, "projects", "-work-app")
        odd = "aaaaaaaa-2222-4333-8444-555555555555"
        with open(os.path.join(proj, f"{odd}.jsonl"), "w") as f:  # U+2028 in a prompt; a non-string text block
            f.write(json.dumps(dict(entry("user", "line one\u2028line two"), sessionId=odd)).replace("/work/app", self.repo) + "\n")
            f.write(json.dumps(dict(entry("assistant", [{"type": "text", "text": None}, {"type": "text", "text": 7}]), sessionId=odd)) + "\n")
            f.write(json.dumps(dict(entry("assistant", [{"type": "text", "text": "after"}]), sessionId=odd)) + "\n")
        row = next(r for r in self.j("list")["sessions"] if r["id"] == odd)
        self.assertEqual(row["cwd"], self.repo)
        ev = [json.loads(x) for x in self.fm("claude", "show", odd, env=self.env).stdout.splitlines()]
        self.assertEqual(ev[-1]["text"], "after", "odd blocks are skipped, never a crash")
        last = [json.loads(x) for x in self.fm("claude", "show", SID, "--from", "-3", env=self.env).stdout.splitlines()]
        self.assertEqual(last[-1]["tool"], "Agent", "--from -N: the last N lines")
        scratch = os.path.join(self.tmpdir, f"claude-{os.getuid()}", "-work-app", odd)
        os.makedirs(scratch)
        os.symlink(os.path.join(self.tmp, "fhome"), os.path.join(scratch, "scratchpad"))  # a planted symlink root
        self.assertNotEqual(self.fm("claude", "file", odd, "state", "--json", env=self.env, check=False).returncode, 0)
        kinds = {f["path"]: f["kind"] for f in self.j("files", SID)["files"]}
        self.assertEqual(kinds["escape.txt"], "link")

    def test_pasted_html_is_the_users_and_another_sessions_message_is_not(self):  # T-3687
        proj = os.path.join(self.cfg, "projects", "-work-app")
        mixed = "bbbbbbbb-2222-4333-8444-555555555555"
        with open(os.path.join(proj, f"{mixed}.jsonl"), "w") as f:
            for e in ({"type": "attachment", "attachment": {"type": "queued_command", "prompt":
                       '<cross-session-message from="uds:/a.sock">rename the tab</cross-session-message>'}},
                      entry("user", '<agent-message from="a1">ship it</agent-message>'),
                      entry("user", "<b>the footer</b> overlaps")):
                f.write(json.dumps(dict(e, sessionId=mixed)).replace("/work/app", self.repo) + "\n")
        ev = [json.loads(x) for x in self.fm("claude", "show", mixed, env=self.env).stdout.splitlines()]
        self.assertEqual([e["text"] for e in ev if e["kind"] == "user"], ["<b>the footer</b> overlaps"])
        row = next(r for r in self.j("list")["sessions"] if r["id"] == mixed)
        self.assertEqual(row["prompt"], "<b>the footer</b> overlaps")

    def test_send_continues_the_session_by_its_own_id(self):
        # options before the id: argparse fills the id/message list in one go
        out = json.loads(self.fm("claude", "send", "--json", SID, "--", "--also check the logout", env=self.env).stdout)
        deadline = time.time() + 15
        while time.time() < deadline and not os.path.exists(self.argv):
            time.sleep(0.1)
        argv = json.load(open(self.argv))
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], SID)
        self.assertIn("--fork-session", argv, "a continuation always forks: an idle terminal may still be open")
        self.assertIn(" --also check the logout", argv)
        self.assertEqual(out["claude_session"], SID)
