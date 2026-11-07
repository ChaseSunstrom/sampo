"""fm mcp (T-0245): Foreman's memory and state as read-only MCP tools over stdio (JSON-RPC 2.0, one message a line)."""
import json
import os
import re
import time
import subprocess
import sys

from helpers import FM, ForemanTestCase

from sampo import core as c


class _Mcp(ForemanTestCase):
    def talk(self, *messages, raw=(), argv=(FM, "mcp"), cwd=None, env=None):
        lines = [m if isinstance(m, str) else json.dumps(m) for m in messages] + list(raw)
        p = subprocess.run([sys.executable, *argv], input="\n".join(lines) + "\n", capture_output=True, text=True,
                           cwd=cwd or self.repo, timeout=120, env=dict(os.environ, FOREMAN_HOME=self.home, **(env or {})))
        return [json.loads(x) for x in p.stdout.splitlines() if x.strip()], p

    @staticmethod
    def call(i, tool, **arguments):
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


class RoundTrip(_Mcp):
    def test_initialize_list_and_call(self):
        self.fm("init")
        tid = json.loads(self.fm("capture", "Parse dates in the importer", "--json").stdout)["id"]
        out, _ = self.talk(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            self.call(3, "state"),
            self.call(4, "task_show", id=tid))
        by = {m["id"]: m for m in out}
        self.assertEqual(len(out), 4)  # the notification gets no answer
        self.assertEqual(by[1]["result"]["serverInfo"]["name"], "foreman")
        self.assertIn("tools", by[1]["result"]["capabilities"])
        names = {t["name"] for t in by[2]["result"]["tools"]}
        self.assertEqual(names, {"state", "next", "recall", "research_list", "research_read", "task_show", "checks",
                                 "lib_suggest", "lib_search", "lib_show", "outline", "sym", "impact", "why", "fail",
                                 "logs", "data", "trace", "find", "exam_scorecard", "genesis_status", "goal", "triggers", "calibration", "which", "grants", "proof", "decide",
                                 "profile"})
        for t in by[2]["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")
        self.assertIn("Parse dates in the importer", by[3]["result"]["content"][0]["text"])
        self.assertFalse(by[3]["result"]["isError"])
        self.assertIn(tid, by[4]["result"]["content"][0]["text"])


class Edges(_Mcp):
    def test_bad_input_is_an_error_and_the_server_keeps_going(self):
        self.fm("init")
        out, _ = self.talk(self.call(1, "rm_everything"),
                           self.call(2, "research_read", name="../../etc/passwd"),
                           {"jsonrpc": "2.0", "id": 3, "method": "no/such"},
                           self.call(5, "task_show", id="T-1; rm -rf /"),
                           self.call(6, "next"),
                           {"jsonrpc": "2.0", "id": 7, "method": "initialize"},
                           raw=["{not json"])
        by = {m.get("id"): m for m in out}
        self.assertTrue(by[1]["result"]["isError"])
        self.assertTrue(by[2]["result"]["isError"])
        self.assertEqual(by[3]["error"]["code"], -32601)
        self.assertTrue(by[5]["result"]["isError"])
        self.assertFalse(by[6]["result"]["isError"])  # still serving after all of that
        self.assertEqual(by[None]["error"]["code"], -32700)  # the malformed line
        self.assertTrue(by[7]["result"]["protocolVersion"])  # a client that names none gets ours

    def test_a_sensitive_project_serves_nothing(self):
        self.fm("init")
        self.fm("sensitive", "on")
        out, _ = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.call(2, "state"))
        by = {m["id"]: m for m in out}
        self.assertEqual(by[1]["result"]["tools"], [])
        self.assertTrue(by[2]["result"]["isError"])
        self.assertIn("sensitive", by[2]["result"]["content"][0]["text"])


class Review(_Mcp):
    """T-0245 review: no options through a query, bounded lines, no symlinked notes, and the last check results."""

    def test_the_reviews_cases(self):
        from sampo import core as c
        self.fm("init")
        p = c.find_project(self.repo)
        os.makedirs(os.path.join(p.dir, "research"), exist_ok=True)
        outside = os.path.join(self.tmp, "secret.md")
        with open(outside, "w") as f:
            f.write("outside the project")
        os.symlink(outside, os.path.join(p.dir, "research", "linked.md"))
        c.log_event(p, "check_run", data={"results": [{"cmd": "python3 -m pytest -q", "exit": 1, "s": 2.5}]})  # T-2244
        out, _ = self.talk(self.call(1, "recall", query="--corrections"),
                           self.call(2, "recall", query="-n 5"),
                           self.call(3, "research_read", name="linked"),
                           self.call(4, "checks"),
                           "x" * 1_100_000,
                           self.call(6, "state"))
        by = {m.get("id"): m for m in out}
        for i in (1, 2, 3):
            self.assertTrue(by[i]["result"]["isError"], i)
        self.assertIn("python3 -m pytest -q", by[4]["result"]["content"][0]["text"])
        self.assertIn("✗", by[4]["result"]["content"][0]["text"])
        self.assertEqual(by[None]["error"]["code"], -32600)  # the oversized line, refused
        self.assertFalse(by[6]["result"]["isError"])  # and the next request still served

    def test_the_exam_scorecard_is_served_with_its_exit_one(self):
        self.fm("init")  # T-2172: Desktop and other agents read the done gate; 'not every capability passes' answers
        self.fm("genesis", "init", "--file", "-", input="go\n\n## 2. The capability spec\n\n"
                "1. **Correctness.** Fixes what's broken. Exam: bug.\n\n## 3. The tower\n")
        out, _ = self.talk(self.call(1, "exam_scorecard"))
        self.assertFalse(out[0]["result"]["isError"])
        self.assertIn("Correctness", _text(out[0]))
        out, _ = self.talk(self.call(2, "genesis_status"))  # T-2201: the tower's stage and next action too
        self.assertFalse(out[0]["result"]["isError"])
        self.assertIn("gen-0: saved", _text(out[0]))
        self.assertIn("next:", _text(out[0]))


SECRET = "ghp_" + "a1B2" * 9  # a GitHub token shape c.redact knows


def _text(m):
    return m["result"]["content"][0]["text"]


class McpTools(_Mcp):
    """T-0946: the stack library and the read-only instruments as MCP tools, inputs confined to the project."""

    def _files(self):
        with open(os.path.join(self.repo, "mod.py"), "w") as f:
            f.write("def helper(x):\n    return x + 1\n\n\ndef main():\n    return helper(2)\n")
        with open(os.path.join(self.repo, "app.log"), "w") as f:
            f.write("".join(f"INFO request {i} ok\n" for i in range(5)) + f"ERROR auth failed token={SECRET}\n")
        with open(os.path.join(self.repo, "rows.csv"), "w") as f:
            f.write("id,name\n1,ann\n2,bob\n")

    def test_tools_are_listed_with_schemas_from_the_registry(self):
        from sampo import instr
        self.fm("init")
        out, _ = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in out[0]["result"]["tools"]}
        for name in ("sym", "logs", "data", "trace"):
            want = instr.schema(name)
            got = tools[name]["inputSchema"]
            self.assertEqual(set(got["properties"]), set(want["properties"]), name)
            self.assertEqual(got["required"], ["file"] if name == "trace" else want["required"], name)  # no stdin
            for k, v in want["properties"].items():
                self.assertEqual(got["properties"][k]["description"], v["description"])
                self.assertTrue(got["properties"][k]["maxLength"])
            self.assertIn(instr.TOOLS[name][1][1:], tools[name]["description"])  # capitalised
        self.assertEqual(tools["fail"]["inputSchema"]["required"], ["text"])  # fail reads the given text
        for name in ("lib_suggest", "lib_search", "lib_show", "outline", "impact", "why"):
            self.assertEqual(tools[name]["inputSchema"]["type"], "object")

    def test_calls_inside_the_root_answer_and_are_redacted(self):
        self.fm("init")
        self._files()
        failure = ("FAILED test_mod.py::test_main - AssertionError: assert 3 == 4\n"
                   f"E   AssertionError: assert 3 == 4 {SECRET}\n")
        out, _ = self.talk(self.call(1, "logs", file="app.log"),
                           self.call(2, "data", file="rows.csv"),
                           self.call(3, "outline", path="mod.py"),
                           self.call(4, "sym", target="mod.py:helper"),
                           self.call(5, "fail", text=failure),
                           self.call(6, "lib_search", query="pytest fixtures"),
                           self.call(7, "lib_show", name="pytest", section="verify"),
                           self.call(8, "lib_suggest"),
                           self.call(9, "why", target="mod.py:2"),
                           self.call(10, "impact", path="mod.py"),
                           self.call(11, "logs", file=os.path.join(self.repo, "app.log")))  # absolute, still inside
        by = {m["id"]: m for m in out}
        for i in range(1, 12):
            self.assertFalse(by[i]["result"]["isError"], (i, _text(by[i])))
            self.assertNotIn(SECRET, _text(by[i]), i)
        self.assertIn("ERROR", _text(by[1]))
        self.assertIn("name", _text(by[2]))
        self.assertIn("helper", _text(by[3]))
        self.assertIn("helper", _text(by[4]))
        self.assertIn("assert 3 == 4", _text(by[5]))
        self.assertIn("pytest", _text(by[6]))
        self.assertIn("Verify", _text(by[7]))

    def test_paths_outside_flags_and_long_values_are_refused(self):
        self.fm("init")
        self._files()
        outside = os.path.join(self.tmp, "outside.log")
        with open(outside, "w") as f:
            f.write(f"ERROR secret elsewhere {SECRET}\n")
        os.symlink(outside, os.path.join(self.repo, "linked.log"))
        out, _ = self.talk(self.call(1, "logs", file="../outside.log"),
                           self.call(2, "logs", file=outside),
                           self.call(3, "logs", file="linked.log"),
                           self.call(4, "data", file="/etc/passwd"),
                           self.call(5, "outline", path="--help"),
                           self.call(6, "lib_search", query="-n 5"),
                           self.call(7, "sym", target="../outside.py:f"),
                           self.call(8, "why", target="/etc/passwd:1"),
                           self.call(9, "outline", path="a" * 5000),
                           self.call(10, "lib_show", name="x" * 5000),
                           self.call(11, "logs", file="app.log", since_good="../outside.log"),
                           self.call(12, "trace", file="-"),
                           self.call(13, "lib_show", name="pytest", section="--all"),
                           self.call(14, "outline", path="mod.py", extra="x"),
                           self.call(15, "outline", path="mod.py"))
        by = {m["id"]: m for m in out}
        for i in range(1, 15):
            self.assertTrue(by[i]["result"]["isError"], (i, _text(by[i])))
            self.assertNotIn("secret elsewhere", _text(by[i]), i)
        self.assertIn("outside", _text(by[1]))
        self.assertFalse(by[15]["result"]["isError"])  # still serving

    def test_a_call_that_runs_too_long_is_refused(self):
        self.fm("init")
        self._files()
        out, _ = self.talk(self.call(1, "outline", path="mod.py"), {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                           env={"FOREMAN_MCP_TIMEOUT": "0.001"})
        by = {m["id"]: m for m in out}
        self.assertTrue(by[1]["result"]["isError"])
        self.assertIn("timed out", _text(by[1]))
        self.assertEqual(by[2]["result"], {})


class McpFind(_Mcp):  # T-2127
    def test_find_ranks_leads_in_the_project_or_a_folder_and_never_takes_a_flag(self):
        from sampo import core as c
        self.fm("init")
        os.makedirs(os.path.join(self.repo, "src"))
        os.makedirs(os.path.join(self.repo, "empty"))
        for name, text in {"requirements.txt": "flask==3.0\n",
                           "src/m.py": "import zorblax\n\n\ndef use():\n    return zorblax.go()\n"}.items():
            with open(os.path.join(self.repo, name), "w") as f:
                f.write(text)
        out, _ = self.talk(self.call(1, "find"), self.call(2, "find", path="src"), self.call(3, "find", path=".."),
                           self.call(4, "find", path="--capture"), self.call(5, "find", path="empty"))
        by = {m["id"]: m["result"] for m in out}
        for i in (1, 2):
            self.assertFalse(by[i]["isError"], by[i])
            self.assertIn("zorblax", by[i]["content"][0]["text"])
        self.assertTrue(by[3]["isError"])
        self.assertIn("outside the project", by[3]["content"][0]["text"])
        self.assertTrue(by[4]["isError"])  # ./--capture: a folder that isn't there, never the flag
        self.assertFalse([b for b in c.load_briefs(c.find_project(self.repo, create=False), include_archive=True)
                          if b.meta.get("finder")])
        self.assertFalse(by[5]["isError"], by[5])

    def test_find_caps_at_200_leads_and_names_the_rest(self):
        self.fm("init")
        os.makedirs(os.path.join(self.repo, "src"))
        with open(os.path.join(self.repo, "src", "m.py"), "w") as f:
            f.write("".join(f"import zz{i}\n" for i in range(205)))
        out, _ = self.talk(self.call(1, "find", path="src"))
        rows = out[0]["result"]["content"][0]["text"].splitlines()
        self.assertEqual(len(rows), 202)
        self.assertRegex(rows[-1], r"^… \d+ more: fm find \./src --rank --json lists all$")

    def test_an_allowed_exit_with_nothing_on_stdout_is_an_error(self):
        from sampo import core as c
        from sampo import mcp
        from unittest import mock
        self.fm("init")
        crash = subprocess.CompletedProcess([], 1, stdout="", stderr="Traceback: boom")
        with mock.patch.object(mcp.subprocess, "run", return_value=crash):
            self.assertEqual(mcp._fm(c.find_project(self.repo, create=False), ["find"], ok=(0, 1)),
                             ("Traceback: boom", True))


class McpNew(_Mcp):
    """T-0946: fm mcp new scaffolds a stdlib-only, argv-only MCP server and its test; fm mcp register prints lines."""

    def new(self, *args, check=True):
        return self.fm("mcp", "new", *args, check=check)

    def test_a_generated_server_serves_and_refuses_injection(self):
        import ast
        d = os.path.join(self.tmp, "echoer")
        r = self.new("echoer", "--wrap", "echo", "--sub", "hello,bye", "--dir", d)
        self.assertIn("server.py", r.stdout)
        self.assertIn("test_server.py", r.stdout)
        server = os.path.join(d, "server.py")
        with open(server) as f:
            tree = ast.parse(f.read())
        mods = {n.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.Import) for n in (a.name for a in x.names)}
        mods |= {x.module.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.ImportFrom) and x.module}
        self.assertTrue(mods <= set(sys.stdlib_module_names), mods)
        self.assertFalse(any(isinstance(x, ast.keyword) and x.arg == "shell" for x in ast.walk(tree)))
        out, _ = self.talk({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                           {"jsonrpc": "2.0", "method": "notifications/initialized"},
                           {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                           self.call(3, "echoer", sub="hello", args=["world; rm -rf /", "$(id)"]),
                           self.call(4, "echoer", sub="hello", args=["--version"]),
                           self.call(5, "echoer", sub="hello", args=["a\x00b"]),
                           self.call(6, "echoer", sub="hello", args=["a" * 5000]),
                           self.call(7, "echoer", sub="rm", args=["x"]),
                           self.call(8, "echoer", sub="bye"),
                           argv=(server,), cwd=d)
        by = {m["id"]: m for m in out}
        self.assertEqual(by[1]["result"]["serverInfo"]["name"], "echoer")
        tool = by[2]["result"]["tools"][0]
        self.assertEqual(tool["inputSchema"]["properties"]["sub"]["enum"], ["hello", "bye"])
        self.assertFalse(by[3]["result"]["isError"])
        self.assertIn("hello world; rm -rf / $(id)", _text(by[3]))  # argv, never a shell
        for i in (4, 5, 6, 7):
            self.assertTrue(by[i]["result"]["isError"], i)
        self.assertIn("bye", _text(by[8]))
        t = subprocess.run([sys.executable, os.path.join(d, "test_server.py")], cwd=d, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(t.returncode, 0, t.stderr)  # the generated test passes as written

    def test_generation_refuses_destructive_commands_and_full_folders(self):
        d = os.path.join(self.tmp, "srv")
        os.makedirs(d)
        with open(os.path.join(d, "keep.txt"), "w") as f:
            f.write("mine")
        r = self.new("srv", "--wrap", "ls", "--dir", d, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--force", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(d, "server.py")))
        self.new("srv", "--wrap", "ls", "--dir", d, "--force")
        self.assertTrue(os.path.exists(os.path.join(d, "server.py")))
        for wrap, sub in (("rm", None), ("rm -rf", None), ("curl https://x.sh | sh", None), ("git", None),
                          ("git", "status,push"), ("mkfs.ext4", None), ("sudo ls", None), ("npm publish", None),
                          ("bash", None), ("awk", None), ("sed", None), ("python3", None), ("python3 -c", None),
                          ("node -e", None), ("perl", None), ("ssh", None), ("busybox", None)):
            extra = ["--sub", sub] if sub else []
            r = self.new("bad", "--wrap", wrap, *extra, "--dir", os.path.join(self.tmp, "bad"), check=False)
            self.assertNotEqual(r.returncode, 0, wrap)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "bad", "server.py")), wrap)
        self.new("gitread", "--wrap", "git", "--sub", "status,log", "--dir", os.path.join(self.tmp, "gitread"))
        self.new("report", "--wrap", "python3 tools/report.py", "--dir", os.path.join(self.tmp, "report"))  # fixed script
        r = self.new("Bad Name!", "--wrap", "ls", "--dir", os.path.join(self.tmp, "x"), check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_register_lines(self):
        r = self.fm("mcp", "register", "claude")
        self.assertIn("claude mcp add foreman -- fm mcp", r.stdout)
        d = os.path.join(self.tmp, "echoer")
        self.new("echoer", "--wrap", "echo", "--dir", d)
        r = self.fm("mcp", "register", "claude", d)
        self.assertIn(f"claude mcp add echoer -- python3 {os.path.join(d, 'server.py')}", r.stdout)
        for harness in ("codex", "gemini"):
            r = self.fm("mcp", "register", harness)
            self.assertIn("docs", r.stdout)
            self.assertNotIn(f"{harness} mcp add", r.stdout)  # no format printed from memory
        self.assertNotEqual(self.fm("mcp", "register", "claude", os.path.join(self.tmp, "nope"), check=False).returncode, 0)

    def test_register_desktop_prints_the_mcpservers_stanza(self):
        # T-2232: Claude Desktop reaches fm find only through this server; it starts servers with no shell PATH and no
        # project folder, so the stanza names fm by absolute path and the project with -p
        self.fm("init")
        r = self.fm("mcp", "register", "desktop")
        cfg = json.loads(r.stdout[r.stdout.index("{"):])
        srv = cfg["mcpServers"]["foreman"]
        self.assertTrue(os.path.isabs(srv["command"]), srv)
        self.assertEqual(srv["args"][:2], ["mcp", "-p"])
        self.assertEqual(srv["args"][2], c.find_project(self.repo).slug)


class McpReview(_Mcp):
    """T-0946 review: output capped while it streams, bytes that aren't UTF-8, honest schemas and errors."""

    def _server(self, name, wrap):
        d = os.path.join(self.tmp, name)
        self.fm("mcp", "new", name, "--wrap", wrap, "--dir", d)
        return os.path.join(d, "server.py"), d

    def test_endless_output_is_cut_at_the_cap_without_waiting_for_the_timeout(self):
        server, d = self._server("yeser", "yes")
        t0 = time.monotonic()
        out, _ = self.talk(self.call(1, "yeser", args=["y"]), argv=(server,), cwd=d)
        self.assertLess(time.monotonic() - t0, 15)  # wall clock: the cap-vs-timeout deadline of a child server
        self.assertIn("cut at", _text(out[0]))

    def test_bytes_that_arent_utf8_still_answer(self):
        server, d = self._server("printer", "printf")
        out, _ = self.talk(self.call(1, "printer", args=["\\377ok"]), argv=(server,), cwd=d)
        self.assertIn("result", out[0])
        self.assertIn("ok", _text(out[0]))

    def test_trace_needs_a_file_and_sym_says_its_shape(self):
        self.fm("init")
        out, _ = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           self.call(2, "sym", target="mod.py"))
        tools = {t["name"]: t for t in out[0]["result"]["tools"]}
        self.assertEqual(tools["trace"]["inputSchema"]["required"], ["file"])
        self.assertIn("PATH:NAME", _text(out[1]))

    def test_a_fallback_name_is_quoted_in_the_register_line(self):
        d = os.path.join(self.tmp, "a;touch x")
        os.makedirs(d)
        with open(os.path.join(d, "server.py"), "w") as f:
            f.write("# no NAME line\n")
        r = self.fm("mcp", "register", "claude", d)
        self.assertIn("claude mcp add 'a;touch x' --", r.stdout)

    def test_secret_files_are_never_served(self):
        self.fm("init")
        for name in (".env", ".env.local", "deploy.pem", "id_ed25519"):
            with open(os.path.join(self.repo, name), "w") as f:
                f.write("SECRET=1\n")
        out, _ = self.talk(*[self.call(i, "data", file=n) for i, n in
                             enumerate((".env", ".env.local", "deploy.pem", "id_ed25519", ".git/config"))])
        for m in out:
            self.assertTrue(m["result"]["isError"], m)
            self.assertIn("secret", _text(m))


class GoalTool(_Mcp):
    def test_the_goal_tool_reads_meta_only_and_is_listed(self):  # T-2141
        self.fm("init")
        out, _ = self.talk(self.call(1, "goal"))
        self.assertEqual(json.loads(_text(out[0])), {"goal": None, "line": None, "verdict": None})
        self.fm("goal", "set", "Ship the ledger", "--until", "2099-01-01")
        self.fm("goal", "item", "one", "--done-when", "touch ran-it")  # a check that would leave a mark if it ran
        self.fm("goal", "verdict", check=False)  # (stored while no task is linked: nothing ran)
        r = self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                    "--goal", "1")
        b = c.find_brief(c.find_project(self.repo), re.search(r"T-\d+", r.stdout)[0])
        b.meta["status"] = "done"  # drift would now run the item's check (fm goal show does)
        c.save_brief(c.find_project(self.repo), b, touch=False)
        out, _ = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.call(2, "goal"))
        self.assertIn("goal", {t["name"] for t in out[0]["result"]["tools"]})
        data = json.loads(_text(out[1]))
        self.assertEqual(data["goal"]["text"], "Ship the ledger")
        self.assertEqual(data["goal"]["items"][0]["state"], "done")
        self.assertIn("1/1 items done", data["line"])
        self.assertEqual(data["verdict"]["verdict"], "stopped")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ran-it")))


class JudgmentTool(_Mcp):  # T-2344: #2 and #8 through MCP (Claude Desktop and other agents)
    def test_judgment_calibration_tool_is_listed_and_writes_nothing(self):
        self.fm("init")
        p = c.find_project(self.repo)
        c.log_event(p, "race_pick", task="T-0001", data={"winner": "T-0002", "stated": 1, "first_won": False})
        c.log_event(p, "race_pick", task="T-0003", data={"winner": "T-0003", "stated": 2, "first_won": True})
        before = c.read_meta(p)
        ledger = os.path.getsize(os.path.join(p.dir, "ledger.jsonl"))
        out, _ = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.call(2, "calibration"))
        self.assertIn("calibration", {t["name"] for t in out[0]["result"]["tools"]})
        d = json.loads(_text(out[1]))
        for k in ("kinds", "interim", "brier", "bars", "races", "doors_parked"):
            self.assertIn(k, d)
        self.assertEqual(d["races"], {"run": 2, "judged": 2, "first_lost": 1})
        self.assertEqual(c.read_meta(p), before)  # no settle, no bar move
        self.assertEqual(os.path.getsize(os.path.join(p.dir, "ledger.jsonl")), ledger)


class ToolsTools(_Mcp):  # T-2378: #5 through MCP (Claude Desktop and other agents), read-only
    def test_which_and_grants_are_listed_and_write_nothing(self):
        from sampo import grants
        self.fm("init")
        p = c.find_project(self.repo)
        r = self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        tid = re.search(r"T-\d+", r.stdout)[0]
        grants.queue(p, tid, ["plugin"], "needs a table printer", what="tabulate", fallback="plain columns")
        ledger = os.path.getsize(os.path.join(p.dir, "ledger.jsonl"))
        out, _ = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.call(2, "which", task="parse json files"),
                           self.call(3, "grants"), self.call(4, "which", task=""))
        names = {t["name"] for t in out[0]["result"]["tools"]}
        self.assertTrue({"which", "grants"} <= names)
        found = json.loads(_text(out[1]))
        self.assertEqual(found["task"], "parse json files")
        self.assertIn("commands", found)
        rows = json.loads(_text(out[2]))
        self.assertEqual([x["task"] for x in rows], [tid])
        self.assertIn("tabulate", rows[0]["text"])
        self.assertIn("plain columns", rows[0]["text"])
        self.assertTrue(out[3]["result"]["isError"])  # nothing to look up
        self.assertEqual(os.path.getsize(os.path.join(p.dir, "ledger.jsonl")), ledger)  # no search receipt logged


class TriggersTool(_Mcp):  # T-2364: the trigger table with recoveries and trials, as JSON, read-only
    def test_triggers_tool_returns_the_table_recoveries_and_trials(self):
        self.fm("init")
        out, _ = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.call(2, "triggers"))
        d = json.loads(_text(out[1]))
        self.assertLessEqual({"triggers", "recoveries", "trials", "self_running"}, set(d))


class ProofTool(_Mcp):  # T-2395: the unproven rollup through MCP (Claude Desktop, other agents)
    def test_proof_tool_is_listed_and_returns_the_rollup_rows(self):
        self.fm("init")
        r = self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        tid = re.search(r"T-\d+", r.stdout)[0]
        self.fm("task", "evidence", tid, "--step", "1", "pytest", "9 passed")  # typed, so the rollup holds it
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        b.meta["status"] = "done"
        c.save_brief(p, b, touch=False)
        out, _ = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.call(2, "proof"))
        self.assertIn("proof", {t["name"] for t in out[0]["result"]["tools"]})
        d = json.loads(_text(out[1]))
        self.assertEqual([x["id"] for x in d["tasks"]], [tid])
        self.assertIn("typed", d["tasks"][0]["kinds"])


class DecideTool(_Mcp):  # T-2489: the model layer's decide() for other agents, booked apart from Foreman's own uses
    BASE = {"kind": "intake-type", "prompt": "the parser crashes on an empty file", "choices": ["FIX", "FEATURE"]}

    def test_decide_is_listed_validates_its_input_and_answers_through_the_model_layer(self):
        from sampo import core as c
        from sampo import mcp
        from sampo import models
        from unittest import mock
        self.fm("init")
        p = c.find_project(self.repo, create=False)
        self.assertIn("decide", {t[0] for t in mcp.TOOLS})
        got = {"choice": "FIX", "probability": 0.9, "backend": "ollama", "cleared": True, "tried": ["ollama: ok (5 ms)"],
               "ms": 5}
        with mock.patch.object(models, "decide", return_value=got) as dec:
            text, err = mcp._call(p, "decide", dict(self.BASE))
        self.assertFalse(err)
        self.assertEqual((json.loads(text)["choice"], json.loads(text)["backend"]), ("FIX", "ollama"))
        # booked under its own kind, so an outside agent's questions never score Foreman's own intake use
        self.assertEqual(dec.call_args.args[:3], ("mcp-intake-type", self.BASE["prompt"], ["FIX", "FEATURE"]))
        self.assertIs(dec.call_args.kwargs["p"], p)
        for bad in ({"kind": "has space"}, {"choices": ["one"]}, {"choices": "FIX"}, {"choices": ["a", 2]},
                    {"choices": ["x"] * 13}, {"prompt": ""}, {"prompt": "x" * 4001}):
            with mock.patch.object(models, "decide") as dec:
                text, err = mcp._call(p, "decide", dict(self.BASE, **bad))
            self.assertTrue(err, bad)
            dec.assert_not_called()
        with mock.patch.object(models, "decide", return_value=None):
            text, err = mcp._call(p, "decide", dict(self.BASE))
        self.assertTrue(err)
        self.assertIn("no backend answered", text)
