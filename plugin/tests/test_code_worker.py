"""T-1214: fm code --worker. Fake worker CLIs (small scripts on PATH) do the work; the harness judges what they changed
(the guard on each file, Git's own config and hooks restored) and its own verify decides. No real CLI, no model."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

from helpers import read_text
from test_code import CodeCase, journal_text

VERIFY = "python3 test_calc.py"


class Worker(CodeCase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        # T-1284: the workers' fix, portable (BSD sed's -i takes the script as a backup suffix, so macOS never fixed it)
        self.cli("fix-calc", "exec python3 -c 'import pathlib; p = pathlib.Path(\"calc.py\"); "
                             "p.write_text(p.read_text().replace(\"a - b\", \"a + b\"))'\n")

    def cli(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def work(self, *args, check=True):
        return self.code(*args, check=check, env={"PATH": self.bin + os.pathsep + os.environ["PATH"]})

    def test_with_no_model_the_worker_cli_plans_too_read_only(self):
        # T-1320: a Claude Code user with no API key or local model: fm code "<any request>" --worker claude plans
        # with the CLI in its read-only mode, then works with it as usual
        plan = {"type": "FIX", "tier": "S", "title": "make calc.add add",
                "criteria": [{"text": "calc.add adds", "verify": VERIFY}], "steps": ["fix calc.add"]}
        reply = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(plan),
                            "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 20}})
        args = os.path.join(self.tmp, "plan-args")
        with open(os.path.join(self.tmp, "reply.json"), "w") as f:
            f.write(reply)
        self.cli("claude", 'mode=; prev=\nfor a in "$@"; do [ "$prev" = "--permission-mode" ] && mode=$a; prev=$a; done\n'
                           f'if [ "$mode" = plan ]; then printf "%s\\n" "$@" > {args}; cat {self.tmp}/reply.json\n'
                           'else fix-calc; fi\n')
        r = self.work("make calc.add add", "--worker", "claude", "--json", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("return a + b", self.calc())
        self.assertIn("make calc.add add", read_text(args), "the request reached the planner")
        events = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
        self.assertIn(120, [e.get("total_in", 0) + e.get("total_out", 0) for e in events if e["event"] == "tokens"])

    def test_the_change_report_leaves_out_build_artefacts(self):
        # T-1320: the check's own python run leaves __pycache__/*.pyc; it isn't the task's change
        self.cli("claude", "fix-calc\nmkdir -p __pycache__ node_modules/x\necho 1 > __pycache__/calc.cpython-314.pyc\n"
                           "echo 1 > node_modules/x/i.js\n")
        r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, "--json")
        changed = [e for e in (json.loads(line) for line in r.stdout.splitlines() if line.startswith("{"))
                   if e["event"] == "changed"]
        self.assertTrue(changed, r.stdout)
        for e in changed:
            self.assertEqual(sorted(e["files"]), ["calc.py"], e)
            self.assertEqual(sorted(e["stat"]), ["calc.py"], e)

    def planner(self, result, extra=""):
        """A fake claude whose plan-mode turn prints `result` as its JSON reply (after `extra` shell lines)."""
        with open(os.path.join(self.tmp, "reply.json"), "w") as f:
            f.write(json.dumps(result))
        args = os.path.join(self.tmp, "plan-args")
        self.cli("claude", 'mode=; prev=\nfor a in "$@"; do [ "$prev" = "--permission-mode" ] && mode=$a; prev=$a; done\n'
                           f'if [ "$mode" = plan ]; then printf "%s\\n" "$@" > {args}; {extra}\ncat {self.tmp}/reply.json\n'
                           'else fix-calc; fi\n')
        return args

    def test_the_cli_planner_answers_a_question_and_its_tools_are_read_only(self):
        args = self.planner({"type": "result", "is_error": False, "usage": {},
                             "result": json.dumps({"answer": "calc.add subtracts (calc.py line 2)\x1b[2J"})})
        r = self.work("what does calc.add do?", "--worker", "claude", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        answer = [e for e in (json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")) if e["event"] == "answer"]
        self.assertEqual(len(answer), 1)
        self.assertIn("calc.py line 2", answer[0]["text"])
        self.assertNotIn("\x1b", answer[0]["text"], "no terminal control from an answer")
        self.assertIn("return a - b", self.calc())
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0)
        got = read_text(args).splitlines()
        self.assertIn("--disallowedTools", got, "the planner can't run or edit, whatever its settings allow")

    def test_a_planner_that_changes_files_is_stopped_before_any_task(self):
        self.planner({"type": "result", "is_error": False, "usage": {}, "result": json.dumps(
            {"type": "FIX", "tier": "S", "title": "t", "criteria": [{"text": "adds", "verify": VERIFY}],
             "steps": ["fix"]})}, extra="echo '# planner' >> calc.py")
        r = self.work("make calc.add add", "--worker", "claude", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("calc.py", r.stderr)
        self.assertIn("planning", r.stderr)
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0, "no task from that plan")

    def test_a_cli_planner_error_is_said(self):
        self.planner({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"})
        r = self.work("make calc.add add", "--worker", "claude", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Not logged in", r.stderr)

    def test_a_cli_planned_run_resumes_with_the_same_planner(self):
        # T-1320 review: the journal names the planner ("claude (plan, read-only)"), which isn't a model profile
        import types
        import unittest.mock
        from sampo import smith
        from sampo import models
        run = types.SimpleNamespace(worker="claude", model_name="claude (plan, read-only)", root=self.repo,
                                    worker_timeout=5, live_tools=lambda who, lean=False: None, live_beat=lambda who: None)
        with unittest.mock.patch.object(models, "resolve", side_effect=models.ModelError("no model profile")):
            self.assertEqual(smith.choose_profile(run, None)["kind"], "cli")
            with self.assertRaises(models.ModelError):
                smith.choose_profile(run, "my-model")  # a model asked for by name is never swapped for the CLI

    def stopped_cli_is_gone(self, args, pidfile):
        """Start fm code as the TUI does (its own session), wait for the CLI it starts, stop fm code as the TUI does
        (SIGTERM to its group), and say whether that CLI is gone within 5 s."""
        import signal
        from test_code import FM
        env = self.env({"PATH": self.bin + os.pathsep + os.environ["PATH"]})
        p = subprocess.Popen([sys.executable, FM, "code", *args], cwd=self.repo, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 30
        while not os.path.exists(pidfile) and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(pidfile), "the CLI started")
        time.sleep(0.3)
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(15)
        pid = int(read_text(pidfile).strip())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        os.kill(pid, 9)
        return False

    def test_stopping_fm_code_stops_the_worker_it_started(self):
        # T-1326: the TUI stops a run with SIGTERM to fm code's group; the worker CLI, in its own session, ran on
        pidfile = os.path.join(self.tmp, "worker.pid")
        self.cli("slowcli", f"echo $$ > {pidfile}\nexec sleep 60\n")
        self.assertTrue(self.stopped_cli_is_gone(["FIX: make calc.add add", "--worker", "slowcli {prompt}", "--verify",
                                                  VERIFY], pidfile), "the worker outlived fm code")

    def test_stopping_fm_code_stops_the_planner_it_started(self):
        # T-1326: a quit in ~/projects/nixie left claude planning, orphaned, for minutes
        pidfile = os.path.join(self.tmp, "planner.pid")
        self.planner({"type": "result", "result": "{}", "usage": {}}, extra=f"echo $$ > {pidfile}; exec sleep 60")
        self.assertTrue(self.stopped_cli_is_gone(["make calc.add add", "--worker", "claude"], pidfile),
                        "the planner outlived fm code")

    def test_with_no_model_a_cli_that_cant_plan_read_only_says_what_to_give(self):
        r = self.work("make calc.add add", "--worker", "fix-calc {prompt}", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--verify", r.stderr)
        tools = os.path.join(self.tmp, "tools")  # git and python3 only: no CLI that can plan, the real claude least
        os.makedirs(tools)
        for t in ("git", "python3"):
            os.symlink(shutil.which(t), os.path.join(tools, t))
        r = self.code("make calc.add add", check=False, env={"PATH": self.bin + os.pathsep + tools})
        self.assertIn("--worker claude", r.stderr, "no CLI that can plan is installed: say which would")

    # T-1328: runners
    def runners(self, *args, config=None):
        if config is not None:
            with open(os.path.join(self.home, "code.toml"), "w") as f:
                f.write(config)
        r = self.work("runners", *args, "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def test_runners_lists_every_cli_and_what_it_can_do(self):
        for name in ("claude", "codex"):
            self.cli(name, "echo hi\n")
        got = {r["name"]: r for r in self.runners()["runners"]}
        self.assertTrue(got["claude"]["installed"] and got["claude"]["plans"] and got["claude"]["verified"])
        self.assertTrue(got["codex"]["installed"] and got["codex"]["works"])
        self.assertFalse(got["codex"]["verified"], "a read-only mode not run here isn't claimed")
        self.assertIn("opencode", got)
        self.assertFalse(got["opencode"]["works"], "no preset that confines its shell commands")
        self.assertEqual(self.runners()["default"], "claude", "the verified planner, with nothing configured")

    def test_no_model_and_no_flag_uses_the_verified_planner_and_says_so(self):
        self.planner({"type": "result", "is_error": False, "usage": {}, "result": json.dumps(
            {"type": "FIX", "tier": "S", "title": "t", "criteria": [{"text": "adds", "verify": VERIFY}],
             "steps": ["fix"]})})
        r = self.work("make calc.add add", "--json", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        events = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        self.assertEqual(next(e for e in events if e["event"] == "start")["worker"], "claude")
        self.assertTrue(any(e["event"] == "note" and "claude" in e["text"] for e in events), "it says which runner")

    def test_the_config_picks_the_default_and_defines_a_custom_cli(self):
        self.cli("mycli", "fix-calc\n")
        self.assertEqual(self.runners(config='runner = "mine"\n[runners.mine]\nwork = "mycli {prompt}"\n')["default"],
                         "mine")
        r = self.work("FIX: make calc.add add", "--verify", VERIFY, "--json", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        events = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        self.assertEqual(next(e for e in events if e["event"] == "start")["worker"], "mine")
        self.work("runners", "--use", "claude")
        self.assertIn('runner = "claude"', read_text(os.path.join(self.home, "code.toml")))
        self.assertIn("[runners.mine]", read_text(os.path.join(self.home, "code.toml")), "the rest of the file stays")

    def test_a_modelless_close_with_a_gap_blocks_cleanly(self):
        # T-2937 audit: --worker with --verify has no model, so a PERFORMANCE close's missing numbers can't be asked;
        # it blocks with finish's own reason, never a traceback
        self.cli("claude", "fix-calc\n")
        r = self.work("PERFORMANCE: make calc.add add", "--worker", "claude", "--verify", VERIFY, check=False)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.task()["status"], "blocked", r.stdout + r.stderr)

    def test_a_project_picks_a_runner_by_name_only(self):
        self.cli("codex", "fix-calc\n")
        self.work("runners", "--use", "codex", "--here")
        self.assertEqual(self.runners()["default"], "codex")
        r = self.work("runners", "--use", "rm -rf ~ {prompt}", "--here", check=False)
        self.assertNotEqual(r.returncode, 0, "a project's choice names a runner; it can't be a command")
        self.assertIn("codex", r.stderr + r.stdout, "it lists the names it takes")

    def test_a_broken_config_is_named_and_the_list_still_shows(self):
        got = self.runners(config="runner =\n")
        self.assertIn("code.toml", got["problem"])
        self.assertIn("claude", [r["name"] for r in got["runners"]])
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write('[runners.mine]\nwork = "mycli"\n')
        r = self.work("runners", check=False)
        self.assertIn("{prompt}", r.stdout + r.stderr, "a template without {prompt} says what it lacks")

    def test_a_hand_edited_project_runner_never_becomes_a_command(self):
        import glob
        self.cli("codex", "fix-calc\n")
        self.work("runners", "--use", "codex", "--here")
        [meta] = [f for f in glob.glob(os.path.join(self.home, "**", "meta.json"), recursive=True)
                  if '"code_runner"' in read_text(f)]
        text = read_text(meta).replace('"codex"', '"touch pwned {prompt}"')
        with open(meta, "w") as f:
            f.write(text)
        r = self.work("make calc.add add", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("isn't one fm code can run", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "pwned")))

    def test_a_broken_model_setup_is_an_error_not_a_fallback_to_claude(self):
        self.cli("claude", f"touch {self.tmp}/claude-ran\n")
        with open(os.path.join(self.home, "models.json"), "w") as f:
            f.write('{"default": "local-typo", "profiles": {}}')
        r = self.work("make calc.add add", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("local-typo", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "claude-ran")))

    def test_runners_lists_the_models_too_and_a_model_does_the_work(self):
        self.serve("openai")
        got = self.runners()
        self.assertIn("fake", [x["name"] for x in got["models"]])
        self.assertIsNone(got["default"], "a model is set up: it does the work, as before")

    def test_a_project_pin_clears_and_here_alone_is_refused(self):
        self.cli("codex", "fix-calc\n")
        self.work("runners", "--use", "codex", "--here")
        self.work("runners", "--use", "", "--here")
        self.assertEqual(self.runners()["default"], "claude")
        self.assertNotEqual(self.work("runners", "--here", check=False).returncode, 0)

    def test_use_sets_the_top_level_key_only(self):
        self.runners(config='[runners.mine]\nwork = "mycli {prompt}"\nrunner = "x"\n')
        self.work("runners", "--use", "claude")
        import tomllib
        with open(os.path.join(self.home, "code.toml"), "rb") as f:
            conf = tomllib.load(f)
        self.assertEqual(conf["runner"], "claude")
        self.assertEqual(conf["runners"]["mine"]["runner"], "x", "a key inside a table is left alone")

    # T-1354: lean nested claude sessions: no user plugins or MCP servers, only Foreman's guard (as --settings)
    def lean_claude(self):
        plan = {"type": "FIX", "tier": "S", "title": "t", "criteria": [{"text": "adds", "verify": VERIFY}], "steps": ["s"]}
        with open(os.path.join(self.tmp, "reply.json"), "w") as f:
            f.write(json.dumps({"type": "result", "is_error": False, "usage": {"input_tokens": 5}, "result": json.dumps(plan)}))
        self.cli("claude", 'mode=; prev=\nfor a in "$@"; do [ "$prev" = "--permission-mode" ] && mode=$a; prev=$a; done\n'
                           f'if [ "$mode" = plan ]; then printf "%s\\n" "$@" > {self.tmp}/plan-args; cat {self.tmp}/reply.json\n'
                           f'else printf "%s\\n" "$@" > {self.tmp}/work-args; fix-calc; fi\n')

    def args_of(self, kind):
        return read_text(os.path.join(self.tmp, f"{kind}-args")).splitlines()

    def test_claude_runs_lean_by_default_with_only_the_guard(self):
        import shlex
        self.lean_claude()
        r = self.work("make calc.add add", "--worker", "claude", "--json", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        plan, work = self.args_of("plan"), self.args_of("work")
        for flag in ("--restricted", "--strict-mcp-config", "--settings"):
            self.assertIn(flag, plan)
        for flag in ("--strict-mcp-config", "--settings"):
            self.assertIn(flag, work)
        self.assertEqual(work[work.index("--setting-sources") + 1], "project,local")
        for argv in (plan, work):
            guard = json.loads(argv[argv.index("--settings") + 1])
            hooks = [h for e in guard["hooks"]["PreToolUse"] for h in e["hooks"]]
            self.assertTrue(hooks)
            for h in hooks:
                self.assertTrue(os.path.exists(shlex.split(h["command"])[0]), h["command"])
        notes = [e["text"] for e in self.events(r) if e["event"] == "note"]
        self.assertTrue(any("lean" in t for t in notes), notes)
        self.assertTrue([e for e in self.events(r) if e["event"] == "tokens"], "the first call's tokens still show")

    def test_lean_false_launches_claude_as_before_and_a_bad_value_is_named(self):
        self.lean_claude()
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write("lean = false\n")
        r = self.work("make calc.add add", "--worker", "claude", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        for kind in ("plan", "work"):
            self.assertFalse({"--settings", "--restricted", "--strict-mcp-config"} & set(self.args_of(kind)), kind)
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write('lean = "false"\n')
        r = self.work("make calc.add add", "--worker", "claude", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("lean", r.stderr)

    # T-1329: live progress from a streaming CLI, replayed from a real claude stream (this machine's paths removed)
    STREAM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "claude-worker-stream.jsonl")

    def events(self, r):
        return [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]

    def planner_stream(self, path):
        """A fake claude whose plan-mode turn prints the stream at `path`; its work turn fixes calc."""
        self.cli("claude", 'mode=; prev=\nfor a in "$@"; do [ "$prev" = "--permission-mode" ] && mode=$a; prev=$a; done\n'
                           f'if [ "$mode" = plan ]; then cat {path}; else fix-calc; fi\n')

    def test_a_claude_worker_turn_shows_each_tool_use_as_it_happens(self):
        self.cli("claude", f'fix-calc\nsed "s#/work/repo#$PWD#g" {self.STREAM}\n')
        r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, "--json")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        tools = [(e["action"], e["target"]) for e in self.events(r) if e["event"] == "tool"]
        self.assertIn(("edit", "calc.py"), tools, "a path in the repo shows relative")
        self.assertIn(("run", "python3 test_calc.py"), tools)
        self.assertTrue(any(a == "run" and t.startswith("pwd && git status") for a, t in tools), tools)
        self.assertTrue(all(len(t) <= 200 and "\n" not in t for _, t in tools))

    def test_a_claude_worker_turn_is_priced_and_capped(self):  # T-1357
        args = os.path.join(self.tmp, "args")
        self.cli("claude", f'printf "%s\\n" "$@" > {args}\nfix-calc\nsed "s#/work/repo#$PWD#g" {self.STREAM}\n')
        r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, "--json", "--max-usd", "2")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        argv = read_text(args).splitlines()
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.00")
        self.assertEqual(self.events(r)[-1]["usd"], 0.0059)
        with open(os.path.join(self.home, "state", "spend.jsonl")) as f:
            rows = [e for e in map(json.loads, f) if e["feature"] == "fm code"]
        self.assertEqual([e["usd"] for e in rows], [0.0059])

    def test_a_claude_worker_never_updates_itself_mid_turn(self):
        # T-1417: an update rewrites ~/.local/bin/claude, which the judge now holds, and would block a healthy run
        self.cli("claude", f'echo "$DISABLE_AUTOUPDATER" > {self.tmp}/au\nfix-calc\n')
        self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY)
        self.assertEqual(read_text(os.path.join(self.tmp, "au")).strip(), "1")

    def bug(self):
        with open(os.path.join(self.repo, "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a - b\n")
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "the bug again"], capture_output=True)

    def test_a_lean_claude_worker_whose_session_loads_more_than_the_guard_is_stopped(self):
        # T-1388: a lean worker starts with no MCP server and only built-in plugins (a live init line, 2026-10-21); one
        # that lists more (a repo's .mcp.json or settings got past --strict-mcp-config) is stopped before it acts
        base = {"type": "system", "subtype": "init", "mcp_servers": [], "tools": ["Bash", "Edit"],
                "plugins": [{"name": "cc-plugin-telemetry", "path": "builtin"}]}
        for extra, named in (({"mcp_servers": [{"name": "evil", "status": "connected"}]}, "MCP server evil"),
                             ({"tools": ["Bash", "mcp__evil__run"]}, "tool mcp__evil__run"),
                             ({"plugins": [{"name": "evil", "path": "/x/evil"}]}, "plugin evil")):
            with self.subTest(named=named):
                self.bug()
                self.cli("claude", f"echo '{json.dumps(dict(base, **extra))}'\nsleep 5\nfix-calc\n")
                r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, "--json", check=False)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                blocked = [e["reason"] for e in self.events(r) if e["event"] == "blocked"]
                self.assertTrue(blocked and named in blocked[0], (blocked, r.stderr[-400:]))
                self.assertIn("a - b", self.calc(), "stopped before it acted")
        self.bug()
        self.cli("claude", f"echo '{json.dumps(base)}'\nfix-calc\n")
        self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, "--json")
        self.assertIn("a + b", self.calc(), "a lean session as launched goes on")

    def test_the_planner_reads_its_reply_from_the_streams_result_line(self):
        plan = {"type": "FIX", "tier": "S", "title": "t", "criteria": [{"text": "adds", "verify": VERIFY}], "steps": ["s"]}
        lines = ['{"type":"system","subtype":"init"}', "not json at all",
                 json.dumps({"type": "assistant", "message": {"content": [
                     {"type": "tool_use", "name": "Read", "input": {"file_path": os.path.join(self.repo, "calc.py")}}]}}),
                 json.dumps({"type": "result", "is_error": False, "result": json.dumps(plan),
                             "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 20}})]
        with open(os.path.join(self.tmp, "stream.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        self.planner_stream(os.path.join(self.tmp, "stream.jsonl"))
        r = self.work("make calc.add add", "--worker", "claude", "--json", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        ev = self.events(r)
        self.assertIn(("read", "calc.py"), [(e["action"], e["target"]) for e in ev if e["event"] == "tool"])
        self.assertIn(120, [e.get("total_in", 0) + e.get("total_out", 0) for e in ev if e["event"] == "tokens"])

    def test_a_planner_stream_with_no_result_line_is_an_error(self):
        with open(os.path.join(self.tmp, "stream.jsonl"), "w") as f:
            f.write('{"type":"system","subtype":"init"}\n{"type":"assistant","message":{"content":[]}}\n')
        self.planner_stream(os.path.join(self.tmp, "stream.jsonl"))
        r = self.work("make calc.add add", "--worker", "claude", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no result", r.stderr)

    def test_the_reader_keeps_draining_when_a_callback_fails(self):
        from sampo import core
        r, w = os.pipe()
        data = b"x" * 300000 + b"\nlast\n"
        got = bytearray()

        def boom(line):
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")
        writer = subprocess.Popen([sys.executable, "-c", "import os,sys; os.write(1, sys.stdin.buffer.read())"],
                                  stdin=subprocess.PIPE, stdout=w)
        os.close(w)
        writer.stdin.write(data)
        writer.stdin.close()
        with os.fdopen(r, "rb") as f:
            core.pump(f, got.extend, boom, 0.05, lambda: 1 / 0)
        writer.wait(10)
        self.assertEqual(bytes(got), data, "every byte drained though both callbacks raised")

    def test_stream_targets_carry_no_control_characters(self):
        from sampo import models
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo \x1b]52;c;aGk=\x07 \x9b2J"}},
            {"type": "tool_use", "name": "Odd\x1bTool", "input": {}}]}})
        for action, target in models.stream_tools(line):
            self.assertTrue((action + target).isprintable(), (action, target))

    def test_a_result_line_with_no_reply_text_is_an_error(self):
        with open(os.path.join(self.tmp, "stream.jsonl"), "w") as f:
            f.write('{"type":"result","subtype":"error_max_turns","is_error":true,"usage":{}}\n')
        self.planner_stream(os.path.join(self.tmp, "stream.jsonl"))
        r = self.work("make calc.add add", "--worker", "claude", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("error_max_turns", r.stderr)
        self.assertLess(len(r.stderr), 2000, "not the whole stream")

    def test_a_claude_plan_override_without_stream_json_plans_from_plain_output(self):
        plan = {"type": "FIX", "tier": "S", "title": "t", "criteria": [{"text": "adds", "verify": VERIFY}], "steps": ["s"]}
        with open(os.path.join(self.tmp, "plain.json"), "w") as f:
            f.write(json.dumps(plan))
        self.planner_stream(os.path.join(self.tmp, "plain.json"))
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write('[runners.claude]\nplan = "claude -p {prompt} --permission-mode plan"\n')
        r = self.work("make calc.add add", "--worker", "claude", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)

    def test_a_quiet_turn_sends_a_heartbeat(self):
        self.cli("claude", "sleep 2\nfix-calc\n")
        r = self.code("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, "--json", check=False,
                      env={"PATH": self.bin + os.pathsep + os.environ["PATH"], "FM_HEARTBEAT": "0.5"})
        beats = [e for e in self.events(r) if e["event"] == "working"]
        self.assertGreaterEqual(len(beats), 2, r.stdout)
        self.assertEqual(beats[0]["worker"], "claude")
        self.assertTrue(all(isinstance(b.get("silent"), int) for b in beats), "how long since it was last heard")

    def stalled(self, before, status="done"):
        """T-1336: the stalled events of a worker turn that prints `before` (shell), then goes quiet past FM_STALL."""
        self.cli("claude", before + "\nsleep 1.6\nfix-calc\n")
        r = self.code("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, "--json", check=False,
                      env={"PATH": self.bin + os.pathsep + os.environ["PATH"], "FM_HEARTBEAT": "0.2",
                           "FM_STALL": "0.6"})
        self.assertEqual(self.task()["status"], status, r.stdout + r.stderr)
        got = [e for e in self.events(r) if e["event"] == "stalled"]
        self.assertEqual(len(got), 1, f"one stalled event per silence: {got}")
        self.assertEqual(got[0]["worker"], "claude")
        return got[0]

    def test_a_turn_silent_after_a_line_is_named_silent(self):
        e = self.stalled("echo 'reading the code'")
        self.assertEqual(e["cause"], "silent", e)
        self.assertIn("silent", e["text"])
        self.assertIn("reading the code", e["last"])

    def test_a_turn_that_prints_nothing_is_named_no_output_since_launch(self):
        e = self.stalled(":")
        self.assertEqual(e["cause"], "launch", e)
        self.assertIn("no output since launch", e["text"])
        from test_code import run_friction_lines
        self.assertIn("stalled: launch", run_friction_lines(self), "T-1339: a stall is friction with fm code")

    def test_a_turn_waiting_at_a_prompt_names_it(self):
        e = self.stalled("printf '\\033[1mOverwrite calc.py? (y/n) '")
        self.assertEqual(e["cause"], "prompt", e)
        self.assertIn("prompt", e["text"])
        self.assertIn("Overwrite calc.py? (y/n)", e["last"])
        self.assertTrue(e["last"].isprintable(), e["last"])

    def test_a_stalls_cause_reads_where_the_cursor_rests(self):
        from sampo import smith
        self.assertEqual(smith.stall_cause(b"Password: "), ("prompt", "Password:"), "a colon still waiting")
        self.assertEqual(smith.stall_cause(b"Running tests:\n")[0], "silent", "a finished line asks nothing")
        self.assertEqual(smith.stall_cause(b" \n\n"), ("silent", ""))
        self.assertEqual(smith.stall_cause(b"\x1b]0;title\x07done\n"), ("silent", "done"), "escapes never show")

    def test_a_turn_at_a_rate_limit_names_the_limit(self):
        e = self.stalled("echo 'Error: 429 rate limit reached, retrying in 60 s'\necho", status="blocked")  # T-1476: the run stops there
        self.assertEqual(e["cause"], "limit", e)
        self.assertIn("limit", e["text"])

    def test_a_named_planner_not_verified_here_warns(self):
        self.cli("codex", "printf '%s' '" + json.dumps({"type": "FIX", "tier": "S", "title": "t", "criteria": [
            {"text": "adds", "verify": VERIFY}], "steps": ["s"]}) + "'\n")
        r = self.work("make calc.add add", "--worker", "codex", "--json", check=False)
        self.assertIn("not verified", r.stdout + r.stderr)

    def test_a_worker_that_fixes_the_bug_closes_the_task(self):
        # a table name: claude's unattended form, with the prompt as one argument
        args = os.path.join(self.tmp, "args")
        self.cli("claude", f'printf "%s\\n" "$@" > {args}\nfix-calc\necho fixed\n')
        r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY)
        self.assertIn("return a + b", self.calc())
        t = self.task()
        self.assertEqual(t["status"], "done", r.stdout + r.stderr)
        self.assertEqual(t["type"], "FIX")
        self.assertIn("make calc.add add", t["title"])
        self.assertRegex(read_text(t["path"]), r"`python3 test_calc.py` → exit 0")
        got = read_text(args).splitlines()
        self.assertEqual(got[0], "-p")
        self.assertIn("--permission-mode", got)
        self.assertIn("make calc.add add", read_text(args))
        shown = self.last_commit()
        self.assertIn("T-0001", shown)
        self.assertIn("calc.py", shown)
        start = json.loads(journal_text(self).splitlines()[0])
        self.assertEqual(start["options"]["worker"], "claude")
        self.assertEqual(start["options"]["verify"], VERIFY)

    def test_protected_writes_and_git_hooks_are_reverted_and_reported(self):
        cfg = os.path.join(self.repo, ".git", "config")
        before = read_text(cfg)
        self.cli("sneaky", 'fix-calc\nmkdir -p .claude\necho "{}" > .claude/settings.json\n'
                           'printf "#!/bin/sh\\necho pwned\\n" > .git/hooks/pre-commit\n'
                           'chmod +x .git/hooks/pre-commit\necho ".claude/" >> .git/info/exclude\n'
                           'printf "[core]\\n\\thooksPath = /tmp/evil\\n" >> .git/config\n')
        r = self.work("make calc.add add", "--worker", "sneaky {prompt}", "--verify", VERIFY)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "hooks", "pre-commit")))
        self.assertEqual(read_text(cfg), before)
        self.assertIn("reverted .claude/settings.json", r.stdout)
        self.assertIn("pre-commit", r.stdout)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertNotIn("settings.json", self.last_commit())
        reverts = [json.loads(x) for x in journal_text(self).splitlines() if '"revert"' in x]
        self.assertTrue(any(x.get("file") == ".claude/settings.json" for x in reverts), reverts)

    def test_a_worker_cannot_change_the_files_the_verify_names(self):  # T-1271
        spec = os.path.join(self.repo, "test_calc.py")
        before = read_text(spec)
        self.cli("cheat", 'fix-calc\necho "print(\'ok\')" > test_calc.py\n')
        r = self.work("make calc.add add", "--worker", "cheat {prompt}", "--verify", VERIFY)
        self.assertEqual(read_text(spec), before)
        self.assertIn("reverted test_calc.py", r.stdout)
        self.assertIn("return a + b", self.calc())
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)

    def test_a_folder_the_verify_names_is_protected_too(self):  # T-1271
        os.makedirs(os.path.join(self.repo, "tests"))
        t = os.path.join(self.repo, "tests", "test_x.py")
        with open(t, "w") as f:
            f.write("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_t(self):\n        pass\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "tests"], cwd=self.repo, check=True)
        self.cli("cheat", 'fix-calc\necho "assert 1" > tests/test_x.py\n'
                          'echo "import sys" > tests/conftest.py\nmkdir -p tests/__pycache__\n'
                          'echo x > tests/__pycache__/test_x.cpython-314.pyc\n')
        r = self.work("make calc.add add", "--worker", "cheat {prompt}", "--verify",
                      "python3 test_calc.py && python3 -m unittest discover -s tests")
        self.assertIn("def test_t", read_text(t))
        self.assertIn("reverted tests/test_x.py", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "tests", "conftest.py")), "a new file there is the check too")
        self.assertNotIn("__pycache__", r.stdout, "a bytecode cache is no part of the check")

    def test_the_users_own_uncommitted_work_survives_a_stash_or_a_delete(self):
        # T-1270: the T-1214 review: a worker's git stash or rm of the user's in-progress files was never undone
        with open(os.path.join(self.repo, "notes.md"), "w") as f:
            f.write("my notes\n")  # untracked, not ignored
        with open(os.path.join(self.repo, "test_calc.py"), "a") as f:
            f.write("# my own edit\n")  # tracked, modified
        self.cli("tidy", 'git stash -q\nrm -f notes.md\nfix-calc\n')
        r = self.work("make calc.add add", "--worker", "tidy {prompt}", "--verify", VERIFY)
        self.assertEqual(read_text(os.path.join(self.repo, "notes.md")), "my notes\n")
        self.assertIn("# my own edit", read_text(os.path.join(self.repo, "test_calc.py")))
        self.assertIn("return a + b", self.calc(), "the worker's own fix stays")
        self.assertIn("your uncommitted work", r.stdout)

    def test_a_hanging_worker_is_killed_at_the_timeout(self):
        pid = os.path.join(self.tmp, "pid")
        self.cli("hang", f"sleep 60 &\necho $! > {pid}\nsleep 60\n")
        t0 = time.time()
        r = self.work("make calc.add add", "--worker", "hang", "--worker-timeout", "2", "--verify", VERIFY,
                      check=False)
        self.assertEqual(r.returncode, 1)
        self.assertLess(time.time() - t0, 45)  # wall clock: the --worker-timeout 2 kill deadline
        self.assertIn("timed out after 2 s", r.stdout)
        self.assertEqual(self.task()["status"], "blocked")
        with self.assertRaises(ProcessLookupError):  # the background child died with the group
            os.kill(int(read_text(pid)), 0)

    def test_a_missing_cli_is_a_one_line_error_and_no_task(self):
        r = self.work("make calc.add add", "--worker", "nosuchcli", "--verify", VERIFY, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nosuchcli", r.stderr)
        self.assertEqual(r.stderr.strip().splitlines()[1:], ["Next: fm code runners"], r.stderr)  # T-1334
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0)

    def test_a_worker_claiming_success_without_a_change_is_not_done(self):
        self.cli("liar", 'echo "All done, tests pass"\n')
        r = self.work("make calc.add add", "--worker", "liar", "--verify", VERIFY, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertNotEqual(self.task()["status"], "done")
        self.assertIn("return a - b", self.calc())

    def test_a_verify_that_already_passes_stops_before_any_task(self):
        self.cli("liar", "true\n")
        r = self.work("make calc.add add", "--worker", "liar", "--verify", "true", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("proves nothing", r.stderr)
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0)

    def test_no_preset_auto_approves_a_workers_shell_commands(self):
        # security review: gemini --yolo and aider --yes-always ran any shell command the CLI chose, which Foreman's
        # guard never sees (it judges the files afterwards, not the commands)
        from sampo import smith
        self.assertNotIn("--yolo", smith.WORKERS["gemini"])
        self.assertIn("--approval-mode auto_edit", smith.WORKERS["gemini"])
        self.assertIn("--no-suggest-shell-commands", smith.WORKERS["aider"])
        self.assertNotIn("opencode", smith.WORKERS, "its run mode doesn't confine shell commands")


    def test_the_runs_leases_are_dropped_when_it_ends(self):
        """T-1294 review: the run's 20-minute lease would refuse the user's own session on that code afterwards."""
        import time
        d = os.path.join(self.home, "state", "bus")
        os.makedirs(d, exist_ok=True)
        lease = {"session": "fm-code-T-0001", "project": "x", "file": "calc.py", "symbol": "add", "task": "T-0001",
                 "until": time.time() + 1200}
        other = dict(lease, session="someone-else")
        with open(os.path.join(d, "leases.json"), "w") as f:
            json.dump({"a": lease, "b": other}, f)
        self.cli("plain", "fix-calc\n")
        self.work("make calc.add add", "--worker", "plain {prompt}", "--verify", VERIFY)
        with open(os.path.join(d, "leases.json")) as f:
            self.assertEqual([v["session"] for v in json.load(f).values()], ["someone-else"])

    def test_a_worker_turn_knows_which_run_it_belongs_to(self):
        """T-1294: so its own Claude session's Foreman leases count as the run's."""
        out = os.path.join(self.tmp, "env")
        self.cli("envr", f"env > {out}\nfix-calc\n")
        self.work("make calc.add add", "--worker", "envr {prompt}", "--verify", VERIFY)
        with open(out) as f:
            self.assertIn("FOREMAN_WORKER_OF=fm-code-T-0001", f.read())

    def test_a_worker_turn_runs_its_hooks_quiet(self):
        """T-1327: Foreman's hooks in the nested session run only the guard."""
        out = os.path.join(self.tmp, "quiet")
        self.cli("quietw", f'printf "%s" "$FOREMAN_QUIET" > {out}\nfix-calc\n')
        self.work("make calc.add add", "--worker", "quietw {prompt}", "--verify", VERIFY)
        self.assertEqual(read_text(out), "1")

    def test_the_planner_cli_runs_its_hooks_quiet(self):
        out = os.path.join(self.tmp, "quiet")
        self.planner({"type": "result", "is_error": False, "usage": {}, "result": json.dumps({"answer": "ok"})},
                     extra=f'printf "%s" "$FOREMAN_QUIET" > {out}')
        self.work("what does calc.add do?", "--worker", "claude", check=False)
        self.assertEqual(read_text(out), "1")

    def test_a_worker_gets_no_forge_cloud_or_registry_credentials(self):
        out = os.path.join(self.tmp, "env")
        self.cli("envy", f'env > {out}\nfix-calc\n')
        self.code("make calc.add add", "--worker", "envy {prompt}", "--verify", VERIFY,
                  env={"PATH": self.bin + os.pathsep + os.environ["PATH"], "GITHUB_TOKEN": "ghp_x1",
                       "AWS_SECRET_ACCESS_KEY": "aws1", "NPM_TOKEN": "npm1",  # pragma: allowlist secret
                       "OPENAI_API_KEY": "sk-keep1",  # pragma: allowlist secret
                       "PGPASSWORD": "pg1", "STRIPE_API_KEY": "st1", "MYSQL_PWD": "my1"})  # pragma: allowlist secret
        seen = read_text(out)
        for gone in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "NPM_TOKEN", "PGPASSWORD", "STRIPE_API_KEY", "MYSQL_PWD"):
            self.assertNotIn(gone + "=", seen)
        self.assertIn("OPENAI_API_KEY=sk-keep1", seen, "a model key stays, so the CLI can log in")

    def test_a_write_hidden_by_a_gitignore_line_is_still_judged(self):
        # the builder's open item: a worker adds a .gitignore line, then writes the path it hides
        self.cli("hider", 'fix-calc\necho ".claude/" >> .gitignore\nmkdir -p .claude\n'
                          'echo "{}" > .claude/settings.json\n')
        r = self.work("make calc.add add", "--worker", "hider {prompt}", "--verify", VERIFY)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "settings.json")), r.stdout + r.stderr)
        self.assertIn("reverted .claude/settings.json", r.stdout)

    # -- the review's findings (research T-1214-review)
    def git(self, *a):
        return subprocess.run(["git", "-C", self.repo, *a], capture_output=True, text=True).stdout.strip()

    def commit_files(self, files):
        for name, text in files.items():
            with open(os.path.join(self.repo, name), "w") as f:
                f.write(text)
        self.git("add", "-f", *files)
        self.git("commit", "-qm", "setup")

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("setsid"), "the subreaper is Linux's")
    def test_a_process_the_worker_detached_dies_with_its_turn(self):
        # setsid left the process group, so it outlived the kill and could plant a hook after the judge
        pid = os.path.join(self.tmp, "pid")
        self.cli("detach", f'setsid sleep 60 &\necho $! > {pid}\nfix-calc\n')
        self.work("make calc.add add", "--worker", "detach {prompt}", "--verify", VERIFY)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(read_text(pid)), 0)

    def test_the_verify_runs_without_the_credentials_the_worker_never_got(self):
        seen = os.path.join(self.tmp, "seen")
        self.cli("leak", 'fix-calc\n'
                         f"cat > probe.py <<EOF\nimport os\nopen('{seen}', 'w').write("
                         "os.environ.get('GITHUB_TOKEN', 'none'))\nEOF\n")  # a new file: --verify doesn't name it yet
        self.code("make calc.add add", "--worker", "leak {prompt}", "--verify", VERIFY + " && python3 probe.py",
                  env={"PATH": self.bin + os.pathsep + os.environ["PATH"], "GITHUB_TOKEN": "ghp_x1"})
        self.assertEqual(read_text(seen), "none")

    def test_a_credential_file_git_ignores_is_put_back(self):
        self.commit_files({".gitignore": ".env\nnode_modules/\n"})
        with open(os.path.join(self.repo, ".env"), "w") as f:
            f.write("SECRET=mine\n")
        self.cli("envw", 'fix-calc\necho "SECRET=pwned" > .env\n')
        r = self.work("make calc.add add", "--worker", "envw {prompt}", "--verify", VERIFY)
        self.assertEqual(read_text(os.path.join(self.repo, ".env")), "SECRET=mine\n")
        self.assertIn("reverted .env", r.stdout)

    def test_a_big_ignored_build_folder_keeps_the_fix_and_a_hidden_write_is_still_judged(self):
        self.commit_files({".gitignore": "node_modules/\n"})
        self.cli("npm", 'fix-calc\nmkdir -p node_modules/x\n'
                        'for i in $(seq 400); do echo $i > node_modules/x/f$i.js; done\n'
                        'echo ".claude/" >> .gitignore\nmkdir -p .claude\necho "{}" > .claude/settings.json\n')
        r = self.work("make calc.add add", "--worker", "npm {prompt}", "--verify", VERIFY)
        self.assertIn("return a + b", self.calc())
        self.assertEqual(len(os.listdir(os.path.join(self.repo, "node_modules", "x"))), 400)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "settings.json")), r.stdout)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)

    def test_a_change_hidden_with_assume_unchanged_is_seen_and_committed(self):
        self.cli("hide", 'git update-index --assume-unchanged calc.py\nfix-calc\n')
        r = self.work("make calc.add add", "--worker", "hide {prompt}", "--verify", VERIFY)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("calc.py", self.last_commit())
        self.assertTrue(self.git("ls-files", "-v", "calc.py").startswith("H "))

    def test_a_planted_hook_in_a_read_only_hooks_folder_is_still_removed(self):
        self.cli("lock", 'fix-calc\nprintf "#!/bin/sh\\n" > .git/hooks/pre-commit\n'
                         'chmod +x .git/hooks/pre-commit\nchmod 555 .git/hooks\n')
        r = self.work("make calc.add add", "--worker", "lock {prompt}", "--verify", VERIFY, check=False)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "hooks", "pre-commit")))

    def test_a_sensitive_repo_never_goes_to_a_worker(self):
        self.fm("sensitive", "on")
        self.cli("claude", "echo hi\n")
        r = self.work("make calc.add add", "--worker", "claude", "--verify", VERIFY, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sensitive", r.stderr)
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0)

    def test_the_users_own_hooks_link_and_global_git_config_are_kept_and_put_back(self):
        ext = os.path.join(self.tmp, "githooks")
        os.makedirs(ext)
        hooks = os.path.join(self.repo, ".git", "hooks")
        shutil.rmtree(hooks)
        os.symlink(ext, hooks)
        self.cli("glob", 'fix-calc\n'
                         'printf "[core]\\n\\tfsmonitor = /nonexistent/evil\\n" >> "$HOME/.gitconfig"\n')
        r = self.work("make calc.add add", "--worker", "glob {prompt}", "--verify", VERIFY)
        self.assertTrue(os.path.islink(hooks))
        self.assertFalse(os.path.exists(os.path.join(self.fake_home, ".gitconfig")), r.stdout)

    def test_a_branch_the_worker_made_from_a_detached_head_keeps_its_commit(self):
        sha = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "--detach")
        self.cli("hop", 'git switch -q -c other\ngit commit -q --allow-empty -m mine\nfix-calc\n')
        self.work("make calc.add add", "--worker", "hop {prompt}", "--verify", VERIFY, check=False)
        self.assertNotEqual(self.git("rev-parse", "other"), sha)  # its own new branch: its commit stays on it
        self.assertEqual(self.git("symbolic-ref", "-q", "HEAD"), "")  # detached again

    def test_a_branch_that_was_already_there_is_put_back_when_the_worker_commits_on_it(self):
        sha = self.git("rev-parse", "HEAD")
        self.git("branch", "other")
        self.git("checkout", "-q", "--detach")
        self.cli("hop", 'git switch -q other\ngit commit -q --allow-empty -m mine\nfix-calc\n')
        self.work("make calc.add add", "--worker", "hop {prompt}", "--verify", VERIFY, check=False)
        self.assertEqual(self.git("rev-parse", "other"), sha)  # not the worker's to move: put back
        self.assertEqual(self.git("symbolic-ref", "-q", "HEAD"), "")  # detached again

    def test_worker_argv(self):
        from sampo import smith
        old = os.getcwd()
        os.chdir(self.repo)  # T-5584: the gate reads the project the cwd is in; this one has the switch on (setUp)
        try:
            self.assertEqual(smith.worker_argv("codex", "do it", which=False), ["codex", "exec", "--full-auto", "do it"])
            self.assertEqual(smith.worker_argv("my-cli --msg={prompt} -q", "a b", which=False),
                             ["my-cli", "--msg=a b", "-q"])
            self.assertEqual(smith.worker_argv("plain", "x y", which=False), ["plain", "x y"])
        finally:
            os.chdir(old)


class Ping(CodeCase):  # T-1335
    cli, work = Worker.cli, Worker.work

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)

    def test_a_ping_answer_is_sorted(self):
        from sampo import smith
        s = smith.ping_state
        self.assertEqual(s(0, "ok"), "ok")
        self.assertEqual(s(0, ""), "no answer")
        self.assertEqual(s(None, ""), "no answer")
        self.assertEqual(s(1, "Invalid API key · Please run /login"), "not logged in")
        self.assertEqual(s(1, "Claude AI usage limit reached|1792745600"), "usage limit")
        self.assertEqual(s(1, "error: unknown option '--strict-mcp-config'"), "flag refused")
        self.assertEqual(s(1, "Error: connect ECONNREFUSED 127.0.0.1:443"), "endpoint down")
        self.assertEqual(s(3, "boom"), "failed (exit 3)")

    def test_runners_ping_asks_each_one_a_tiny_question_and_keeps_the_answer(self):
        args = os.path.join(self.tmp, "args")
        self.cli("claude", f'case "$*" in *--version*) echo "2.1.0 (Claude Code)";;\n'
                           f'*) pwd > {args}; echo \'{{"type": "result", "is_error": false, "result": "ok"}}\';; esac\n')
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write('[runners.mine]\nwork = "claude {prompt}"\n')
        r = self.work("runners", "--ping", "claude", "--json")
        row = next(x for x in json.loads(r.stdout)["runners"] if x["name"] == "claude")
        self.assertEqual((row["ping"]["state"], row["ping"]["version"]), ("ok", "2.1.0 (Claude Code)"))
        self.assertIsInstance(row["ping"]["ms"], int)
        self.assertNotEqual(os.path.realpath(read_text(args).strip()), os.path.realpath(self.repo), "a scratch folder")
        with open(os.path.join(self.home, "state", "code-runners-ping.json")) as f:
            self.assertEqual(json.load(f)["claude"]["state"], "ok")
        mine = self.work("runners", "--ping", "mine").stdout
        self.assertIn("version only", mine)  # no read-only plan command: never handed a prompt
        self.assertIn("isn't a runner", self.work("runners", "--ping", "nope", check=False).stderr)


class Config(CodeCase):  # T-1359
    def view(self, *args, env=None):
        r = self.code("config", "--json", *args, env=env)
        return {x["key"]: (x["value"], x["source"]) for x in json.loads(r.stdout)["settings"]}, json.loads(r.stdout)

    def test_each_setting_shows_its_value_and_where_it_came_from(self):
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write('lean = false\nlimit_wait = 120\nchain = ["claude", "codex"]\n[on]\nclosed = "true"\n')
        self.fm("init")
        self.fm("sensitive", "on")
        got, _ = self.view("--max-steps", "7", env={"FOREMAN_CONFINE": "0"})
        self.assertEqual(got["lean"], (False, "code.toml"))
        self.assertEqual(got["limit_wait"], (120, "code.toml"))
        self.assertEqual(got["chain"], (["claude", "codex"], "code.toml"))
        self.assertEqual(got["on.closed"], ("true", "code.toml"))
        self.assertEqual(got["on.blocked"], (None, "default"))
        self.assertEqual(got["max_steps"], (7, "flag"))
        self.assertEqual(got["worker_timeout"], (1200, "default"))
        self.assertEqual(got["max_usd"], (None, "default"))
        self.assertEqual(got["confine"], ("off", "env FOREMAN_CONFINE"))
        self.assertEqual(got["sensitive"], (True, "project"))
        for key in ("runner", "model"):
            self.assertIn(key, got)
        self.assertIn("lean", self.code("config").stdout)

    def test_explain_says_what_a_setting_does_and_how_to_change_it(self):
        out = self.code("config", "--explain", "limit_wait").stdout
        self.assertIn("usage limit", out)
        self.assertIn("code.toml limit_wait", out)
        r = self.code("config", "--explain", "nope", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("limit_wait", r.stderr)

    def test_a_broken_code_toml_is_named_with_its_line_and_the_view_falls_back(self):
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write("runner = 'claude'\nlean = \n")
        got, raw = self.view()
        self.assertIn("line 2", raw["problem"])
        self.assertEqual(got["lean"], (True, "default"))
        self.assertIn("line 2", self.code("config").stdout)
        r = self.code("fix add", check=False)  # a run still refuses it
        self.assertEqual(r.returncode, 1)
        self.assertIn("code.toml", r.stderr)


class Routing(CodeCase):  # T-1358
    cli, work = Worker.cli, Worker.work

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.cli("fix-calc", "exec python3 -c 'import pathlib; p = pathlib.Path(\"calc.py\"); "
                             "p.write_text(p.read_text().replace(\"a - b\", \"a + b\"))'\n")
        self.fm("init")

    def past(self, worker, kind, end, usd=None, n=1, project="elsewhere"):
        d = os.path.join(self.home, "state", "projects", project, "code")
        os.makedirs(d, exist_ok=True)
        for _ in range(n):
            self.seq = getattr(self, "seq", 0) + 1
            evs = [{"event": "start", "worker": worker}, {"event": "tool", "worker": worker},
                   {"event": "plan", "plan": {"type": kind}}, {"event": end, **({"usd": usd} if usd is not None else {})}]
            with open(os.path.join(d, f"T-{self.seq:04d}.events.jsonl"), "w") as f:
                f.write("".join(json.dumps(e) + "\n" for e in evs))

    def test_runners_shows_each_runners_record_per_kind(self):
        self.past("claude", "FIX", "closed", 0.5, n=2)
        self.past("claude", "FIX", "blocked")
        self.past("claude", "FEATURE", "closed")
        d = os.path.join(self.home, "state", "projects", "elsewhere", "code")
        with open(os.path.join(d, "T-0099.events.jsonl"), "w") as f:  # still running: no verdict, not counted
            f.write(json.dumps({"event": "start", "worker": "claude"}) + "\n" + json.dumps(
                {"event": "plan", "plan": {"type": "FIX"}}) + "\n")
        r = self.work("runners", "--json")
        self.assertEqual(json.loads(r.stdout)["records"]["claude"],
                         {"FIX": {"runs": 3, "closed": 2, "priced": 2, "usd": 1.0},
                          "FEATURE": {"runs": 1, "closed": 1, "priced": 0, "usd": None}})
        self.assertIn("claude: FIX 3 runs, 2 closed, $0.50 a closed run; FEATURE 1 run, 1 closed",
                      self.work("runners").stdout)

    def test_auto_hands_the_work_to_the_cheapest_runner_green_on_the_kind(self):
        self.cli("claude", "exit 2\n")  # the planner (no plan needed with --verify); it must never work
        self.cli("spare", "fix-calc\n")
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write('runner = "auto"\n[runners.spare]\nwork = "spare {prompt}"\n')
        self.past("claude", "FIX", "closed", 0.5, n=3)
        self.past("spare", "FIX", "closed", 0.1, n=3)
        self.past("spare", "FEATURE", "blocked", n=3)
        r = self.work("FIX: make calc.add add", "--verify", VERIFY, "--json")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        notes = [e["text"] for e in map(json.loads, r.stdout.splitlines()) if e["event"] == "note"]
        self.assertIn("auto: spare works this FIX (3 runs, 3 closed, $0.10 a closed run; the cheapest green one)", notes)
        self.assertEqual(runner_records_for(self, "spare")["FIX"]["runs"], 4, "this run counts for spare")

    def test_auto_with_no_green_runner_keeps_the_planning_runner(self):
        self.cli("claude", "fix-calc\n")
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write('runner = "auto"\n')
        self.past("claude", "FIX", "blocked", n=3)
        r = self.work("FIX: make calc.add add", "--verify", VERIFY)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("auto: no runner has 3 runs of FIX with two thirds closed yet; claude keeps it", r.stdout)


def runner_records_for(case, name):
    r = case.work("runners", "--json")
    return json.loads(r.stdout)["records"][name]


class Failover(CodeCase):  # T-1356 (not a Worker subclass: its tests would run twice)
    cli, work = Worker.cli, Worker.work

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.cli("fix-calc", "exec python3 -c 'import pathlib; p = pathlib.Path(\"calc.py\"); "
                             "p.write_text(p.read_text().replace(\"a - b\", \"a + b\"))'\n")

    def toml(self, text):
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write(text)

    def test_a_failure_is_sorted_by_its_exit_and_reply(self):
        from sampo import smith
        k = smith.failure_kind
        self.assertEqual(k(0, "Claude AI usage limit reached|1792745600"), (None, None), "exit 0 is no failure")
        self.assertEqual(k(None, ""), ("timeout", None))
        self.assertEqual(k(1, "Claude AI usage limit reached|1792745600"), ("limit", 1792745600))
        kind, at = k(1, "You've hit your limit · resets 3pm (Europe/London)")
        self.assertEqual((kind, time.localtime(at)[3:5]), ("limit", (15, 0)))
        self.assertTrue(0 < at - time.time() <= 86400 + 3600, "the next 3 pm, not a past one")  # + a DST hour
        self.assertEqual(k(1, "Invalid API key · Please run /login"), ("auth", None))
        self.assertEqual(k(2, "Traceback (most recent call last):"), ("crash", None))
        self.assertEqual(k(1, "5-hour limit reached"), ("limit", None), "no reset said")

    def test_the_next_reset_keeps_its_clock_time_across_a_dst_change(self):  # T-2378: found failing on 2026-10-31
        from sampo import smith
        old = os.environ.get("TZ")
        os.environ["TZ"] = "America/Chicago"
        time.tzset()
        try:  # 15:15 CDT on 2026-10-31; clocks fall back at 02:00 on 11-01, so "+ 86400 s" lands on 14:00
            at = smith._reset_at("You've hit your limit · resets 3pm", 1793477700)
            self.assertEqual(time.localtime(at)[1:5], (11, 1, 15, 0))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_a_limit_resetting_soon_is_waited_out_and_the_turn_done_again(self):
        mark = os.path.join(self.tmp, "limited")
        self.cli("claude", f'if [ ! -f {mark} ]; then touch {mark}\n'
                           'echo "Claude AI usage limit reached|$(( $(date +%s) + 2 ))"; exit 1; fi\nfix-calc\n')
        self.toml("limit_wait = 60\n")
        r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("usage limit; waiting until", r.stdout)

    def test_a_limit_past_the_wait_stops_the_run_with_its_resume_command(self):
        self.cli("claude", 'echo "Claude AI usage limit reached|$(( $(date +%s) + 7200 ))"; exit 1\n')
        self.toml("limit_wait = 60\n")
        r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("hit its usage limit, which resets at", r.stderr)
        self.assertEqual(r.stderr.strip().splitlines()[-1], "Next: fm code --resume T-0001")
        self.assertEqual(self.task()["status"], "active")

    def test_a_crash_passes_the_turn_to_the_next_runner_in_the_chain(self):
        self.cli("claude", "echo boom; exit 2\n")
        self.cli("spare", "fix-calc\n")
        self.toml('chain = ["claude", "spare"]\n[runners.spare]\nwork = "spare {prompt}"\n')
        r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("claude: crash (exit 2); spare takes the turn", r.stdout)

    def test_a_chain_naming_a_runner_twice_tries_each_once_a_turn(self):
        self.cli("claude", "echo boom; exit 2\n")
        self.cli("spare", "echo bust; exit 3\n")
        self.toml('chain = ["claude", "spare", "claude"]\n[runners.spare]\nwork = "spare {prompt}"\n')
        r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, check=False)
        handoffs = r.stdout.count("takes the turn")  # two runners: at most one handoff in each of the four turns
        self.assertTrue(1 <= handoffs <= 4, r.stdout)
        self.assertEqual(self.task()["status"], "blocked")

    def test_signed_out_with_no_chain_stops_the_run(self):
        self.cli("claude", 'echo "Invalid API key · Please run /login"; exit 1\n')
        r = self.work("FIX: make calc.add add", "--worker", "claude", "--verify", VERIFY, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("isn't signed in", r.stderr)


if __name__ == "__main__":
    import unittest
    unittest.main()
