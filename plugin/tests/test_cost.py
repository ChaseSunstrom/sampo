"""T-1257: fm cost says how much of what entered the context was Foreman's own: its hook notes, fm commands' output,
its rules, skill and agent listing lines and output style, read from the transcripts Claude Code keeps."""
import datetime
import json
import os
import subprocess
import unittest
import unittest.mock

from test_hooks import HookCase


class ForemanShare(HookCase):
    def test_cost_counts_foremans_own_share_of_the_context(self):
        self.fm("init")
        cfg = os.path.join(self.tmp, "cfg")
        folder = os.path.join(cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        att = lambda a: {"type": "attachment", "timestamp": now, "attachment": a}
        bash = lambda i, cmd: {"type": "tool_use", "id": i, "name": "Bash", "input": {"command": cmd}}
        lines = [
            {"type": "assistant", "timestamp": now, "sessionId": "s1", "message": {
                "id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [bash("t1", "fm task show T-0001"), bash("t2", "ls -la")]}},
            {"type": "user", "timestamp": now, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 998},
                {"type": "tool_result", "tool_use_id": "t2", "content": "y" * 498}]}},
            att({"type": "hook_additional_context", "content": ["Foreman: " + "a" * 491], "hookEvent": "PreToolUse"}),
            att({"type": "hook_additional_context", "content": ["Ambient tests: " + "a" * 85], "hookEvent": "PreToolUse"}),
            att({"type": "hook_additional_context", "content": ["[from another plugin] " + "b" * 278]}),
            att({"type": "skill_listing", "content": "- foreman:next: " + "c" * 83 + "\n- graphify: " + "d" * 187}),
            att({"type": "agent_listing_delta", "addedLines": ["- sampo:fm-scout: " + "e" * 32, "- Explore: f"]}),
            att({"type": "output_style_instructions", "style": {"name": "foreman:Foreman", "prompt": "p" * 200}}),
            att({"type": "instructions", "files": [{"path": "/x/foreman/plugin/rules/foreman.md", "content": "r" * 300},
                                                   {"path": "/home/u/.claude/CLAUDE.md", "content": "u" * 100}]}),
        ]
        with open(os.path.join(folder, "s1.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        env = {"CLAUDE_CONFIG_DIR": cfg}
        share = self.fm_json("cost", env=env)["foreman_share"]
        got = share["chars"]
        self.assertEqual(got["hook notes"], 600, "Foreman's two notes, not the other plugin's")
        self.assertEqual(got["fm output"], 1000, "the fm command's result, not ls's")
        self.assertEqual(got["rules"], 300)
        self.assertEqual(got["output style"], 200)
        self.assertEqual(got["skill listing"], 100)
        self.assertEqual(got["agent listing"], 51)
        self.assertGreater(share["all"], sum(got.values()))
        self.assertIn("Foreman's own share of what entered the context", self.fm("cost", env=env).stdout)

    def test_fm_output_counts_only_fm_commands_and_says_which_verbs(self):
        # T-1276: 'fm task log … && python3 -m unittest' counted the test run's output as Foreman's
        self.fm("init")
        cfg = os.path.join(self.tmp, "cfg")
        folder = os.path.join(cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        cmds = {"t1": "fm task log T-0001 'x' && python3 -m unittest", "t2": "fm task show T-0001 | tail -3",
                "t3": "cd /x && fm focus T-0002", "t4": "fm task set T-1 x=y >/dev/null && fm focus T-1 2>&1 | tail -1",
                "t5": "for t in a b; do fm capture $t; done"}
        lines = [{"type": "assistant", "timestamp": now, "sessionId": "s1", "message": {
                     "id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "tool_use", "id": i, "name": "Bash", "input": {"command": cmd}}
                                 for i, cmd in cmds.items()]}},
                 {"type": "user", "timestamp": now, "message": {"content": [
                     {"type": "tool_result", "tool_use_id": i, "content": "z" * 98} for i in cmds]}}]
        with open(os.path.join(folder, "s1.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        share = self.fm_json("cost", env={"CLAUDE_CONFIG_DIR": cfg})["foreman_share"]
        self.assertEqual(share["chars"]["fm output"], 300, "t2, t3 and t4 are fm alone; t1 runs tests, t5 is a loop")
        self.assertEqual(share["fm_verbs"], {"task show": 100, "focus": 100, "task set": 100})


class OneRead(HookCase):
    def test_cost_reads_the_transcripts_once_for_tokens_and_time(self):
        # T-3521: scan and timing each parsed the week's transcripts (3.1 GB): 48-75 s under load
        import argparse
        import contextlib
        import io
        from unittest import mock
        from sampo import cost
        self.fm("init")
        cfg = os.path.join(self.tmp, "cfg")
        folder = os.path.join(cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        at = lambda s: (t + datetime.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
        lines = [{"type": "assistant", "timestamp": at(0), "sessionId": "s1", "message": {
                     "id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
                 {"type": "user", "timestamp": at(30), "message": {"content": [
                     {"type": "tool_result", "tool_use_id": "t1", "content": "z" * 98}]}},
                 {"type": "assistant", "timestamp": at(40), "sessionId": "s1", "message": {
                     "id": "m2", "usage": {"input_tokens": 20}, "content": []}}]
        with open(os.path.join(folder, "s1.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        reads, real = [], cost._entries
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}), \
                mock.patch.object(cost, "_entries", lambda *a, **k: reads.append(a) or real(*a, **k)), \
                contextlib.redirect_stdout(out):
            cost.cmd_cost(argparse.Namespace(project=self.project().slug, days=7, json=True))
        got = json.loads(out.getvalue())
        self.assertEqual(len(reads), 1, "one pass over the transcripts")
        self.assertEqual(got["messages"], 2)
        self.assertEqual(got["tool_result_chars"], {"Bash": 100})
        self.assertEqual(got["time"]["tools_s"], {"Bash": 30})
        self.assertEqual(got["time"]["model_s"], 10)


class LinePrefilter(unittest.TestCase):
    def test_request_blobs_are_skipped_and_a_newer_nested_stamp_still_counts(self):
        # T-3521: api-request-blob lines (a fifth of the bytes, no top-level timestamp) were parsed for nothing; the
        # entry's own stamp, last on its line, is read first, and an older one there still falls back to every stamp
        import tempfile
        from sampo import cost
        with tempfile.TemporaryDirectory() as d:
            rows = [{"type": "api-request-blob", "sessionId": "s", "message": {"content": [
                        {"timestamp": "2026-10-10T00:00:00.000Z"}]}},
                    {"type": "user", "message": {"x": {"timestamp": "2026-10-10T00:00:00.000Z"}},
                     "timestamp": "2026-09-01T00:00:00.000Z"},
                    {"type": "assistant", "timestamp": "2026-10-11T00:00:00.000Z"},
                    {"type": "system", "timestamp": "2026-09-01T00:00:00.000Z"}]
            with open(os.path.join(d, "a.jsonl"), "w") as f:
                f.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
            got = [e["type"] for _, e in cost._entries(d, "2026-10-01T00:00:00")]
        self.assertEqual(got, ["user", "assistant"])


class SkillUsage(HookCase):
    """T-1702: fm usage tells a skill a hook named and the session never loaded from one never offered, and counts a
    typed /sampo:x and a subagent's playbook Read (a lane's long path is cut in the tool event's target)."""
    def test_named_not_loaded_typed_slash_and_subagent_reads(self):
        self.fm("init")
        self.hook("UserPromptSubmit", {"prompt": "super improve it"})  # names /sampo:brainstorm; never loaded
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "fm next"}})
        self.hook("UserPromptSubmit", {"prompt": "super improve it", "session_id": "sess-3"})  # named, then loaded
        self.hook("PostToolUse", {"tool_name": "Skill", "tool_input": {"skill": "sampo:brainstorm"},
                                  "session_id": "sess-3"})
        self.hook("UserPromptSubmit", {"prompt": "/foreman:status", "session_id": "sess-2"})  # typed by the user (T-2879: the old name still counts)
        book = os.path.join(self.repo, ".claude", "worktrees", "agent-" + "a" * 40, "plugin", "skills", "playbooks",
                            "references", "review", "code-reviewer.md")
        self.hook("PostToolUse", {"tool_name": "Read", "tool_input": {"file_path": book}, "session_id": "sess-2",
                                  "agent_type": "sampo:fm-reviewer", "agent_id": "a1"})
        got = self.fm_json("usage")
        self.assertEqual(got["named_not_loaded"].get("brainstorm"), 1, got)
        self.assertNotIn("status", got["named_not_loaded"])
        self.assertEqual(got["skills"].get("status"), 1)
        self.assertEqual(got["skills"].get("brainstorm"), 1)
        self.assertEqual(got["playbooks"].get("review/code-reviewer"), 1, got["playbooks"])
        self.assertRegex(self.fm("usage").stdout, r"named, not loaded: .*\bbrainstorm ×1\b")  # the idle Next names next

    def test_a_skill_hint_in_a_note_or_the_drives_stop_reason_is_named(self):
        # T-1697's hints say "load: Skill foreman:…", no slash; the drive gives its Next line as a Stop reason.
        # T-1701: a FIX's hint names debug-test
        self.fm("init")
        self.task()
        ctx = self.ctx_of(self.hook("UserPromptSubmit", {"prompt": "carry on", "session_id": "sa"}))
        self.assertIn("Skill sampo:debug-test", ctx)
        stop = lambda sid: json.loads(self.hook("Stop", {"stop_hook_active": False, "session_id": sid,
                                                         "last_assistant_message": "Continuing next."}).stdout)
        self.assertIn("Skill sampo:debug-test", stop("sb")["reason"])
        self.assertIn("Skill sampo:debug-test", stop("sc")["reason"])
        self.hook("PostToolUse", {"tool_name": "Skill", "tool_input": {"skill": "sampo:debug-test"}, "session_id": "sc"})
        self.assertEqual(self.fm_json("usage")["named_not_loaded"].get("debug-test"), 2)

    def test_only_real_skills_count_and_a_long_name_is_not_logged(self):
        from sampo import hooks
        self.fm("init")
        hooks._log_inject("Stop", "auditing: run: the /sampo:review workflow", {"session_id": "s9", "cwd": self.repo})
        self.hook("UserPromptSubmit", {"prompt": "/sampo:zzz please", "session_id": "s8"})
        self.hook("UserPromptSubmit", {"prompt": "/foreman:" + "a" * 100000, "session_id": "s7"})
        got = self.fm_json("usage")
        self.assertNotIn("review", got["named_not_loaded"], "a workflow, not a skill")
        self.assertNotIn("zzz", got["skills"])
        from sampo import core
        with open(os.path.join(core.state_dir(), "events.jsonl")) as f:
            self.assertLess(max(map(len, f)), 2000)


class SampoSpelling(HookCase):
    """T-2845: a typed `sampo …` counts as the `fm …` it is, in every reader of commands."""

    def test_verb_program_and_bookkeeping_read_both_spellings(self):
        from sampo import cost
        for fm_cmd in ("fm task show T-0001", "cd /x && fm quiet -- fm task log T-1 'x' | tail -3", "fm check --affected"):
            sampo_cmd = fm_cmd.replace("fm ", "sampo ")
            self.assertEqual(cost._fm_verb(sampo_cmd), cost._fm_verb(fm_cmd), sampo_cmd)
            self.assertIsNotNone(cost._fm_verb(sampo_cmd), sampo_cmd)
        self.assertEqual(cost._program("sampo task show T-1"), "sampo task")
        self.assertEqual(cost._program("fm task show T-1"), "fm task")
        for cmd in ("fm task log T-1 x", "sampo task log T-1 x", "cd /x && sampo quiet -- sampo next && sampo focus T-1"):
            self.assertTrue(cost._only_bookkeeping(cmd), cmd)
        self.assertFalse(cost._only_bookkeeping("sampo task log T-1 x && pytest"))

    def test_usage_counts_a_sampo_command(self):
        self.fm("init")
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "fm next"}})
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "sampo next"}})
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "sampo task show T-0001"}})
        got = self.fm_json("usage")["commands"]
        self.assertEqual((got.get("next"), got.get("task show") or got.get("task")), (2, 1), got)


class OutputsReread(HookCase):
    """T-4825: sampo cost --outputs: each tool result's characters, re-read on every later call until a compaction,
    by tool and Bash command word, main thread against subagents; aggregates only."""

    def test_bash_kind_names_the_program_not_the_command(self):
        from sampo import cost
        for cmd, kind in (("cd /x && timeout 60 python3 /r/plugin/tests/run.py test_x", "tests"),
                          ("cd /a; cd b\nnice -n 5 ionice -c 3 sampo task show T-1", "sampo task"),
                          ("python3 plugin/bin/sampo cost --days 3", "sampo cost"),
                          ("fm next", "sampo next"), ("FOO=1 BAR='a b' git -C /x log -3", "git log"),
                          ("git status | head", "git status"), ("python3 -c 'print(1)'", "python3 inline"),
                          ("python3 - <<'EOF'\nprint(1)\nEOF", "python3 inline"), ("python3 tools/x.py a", "python3 script"),
                          ("bash run.sh", "bash script"), ("env FOO=1 stdbuf -oL /usr/bin/grep -rn x .", "grep"),
                          ("# a comment\nsed -n 1,5p secret.txt", "sed"), ("", "shell"), ("cd /x", "shell"),
                          ("echo $(whoami)", "echo"), ("$(evil) x", "other"),
                          ("sampo -p someproject task show T-1", "sampo"), ("sampo --json xyzzy", "sampo"),
                          ("python3 plugin/bin/sampo -p foo cost", "sampo"), ("sampo --json cost --outputs", "sampo cost")):
            self.assertEqual(cost._bash_kind(cmd), kind, cmd)

    def test_reread_by_kind_with_compaction_dedupe_subagents_and_the_cutoff(self):
        self.fm("init")
        cfg = os.path.join(self.tmp, "cfg")
        folder = os.path.join(cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(os.path.join(folder, "s1", "subagents"))
        now = datetime.datetime.now(datetime.timezone.utc)
        at = lambda days=0, s=0: (now - datetime.timedelta(days=days) + datetime.timedelta(seconds=s)).strftime(  # noqa: E731
            "%Y-%m-%dT%H:%M:%S.000Z")
        use = lambda i, name, **inp: {"type": "tool_use", "id": i, "name": name, "input": inp}  # noqa: E731
        call = lambda mid, read, blocks, s, days=0: {"type": "assistant", "timestamp": at(days, s), "sessionId": "s1",  # noqa: E731
                                                     "message": {"id": mid, "usage": {"cache_read_input_tokens": read},
                                                                 "content": blocks}}
        got_back = lambda s, i, n, days=0: {"type": "user", "timestamp": at(days, s), "message": {"content": [  # noqa: E731
            {"type": "tool_result", "tool_use_id": i, "content": "x" * (n - 2)}]}}  # json.dumps adds two quotes
        a1 = use("a1", "Bash", command="cd /x && timeout 60 python3 /r/plugin/tests/run.py test_x")
        main = [call("m1", 100, [a1], 0), call("m1", 100, [use("a2", "Read", file_path="/x/f")], 0),  # a split reply
                got_back(1, "a1", 400), got_back(1, "a2", 800),
                call("m2", 1000, [], 2), call("m2", 1000, [], 2),  # live: tests 400, Read 800, counted once
                {"type": "system", "subtype": "compact_boundary", "timestamp": at(0, 3)},
                call("m3", 500, [use("a3", "mcp__x__y")], 4),  # nothing live after the compaction
                got_back(5, "a3", 100), call("m4", 400, [], 6),
                call("old1", 9999, [use("o1", "Read", file_path="/x/g")], 0, days=30), got_back(1, "o1", 8000, days=30)]
        sub = [call("ms1", 200, [use("b1", "Bash", command="sed -n 1,5p /secret/path.txt")], 0), got_back(1, "b1", 200),
               call("ms2", 300, [], 2)]
        for name, rows in (("s1.jsonl", main), (os.path.join("s1", "subagents", "agent-1.jsonl"), sub)):
            with open(os.path.join(folder, name), "w") as f:
                f.write("\n".join(json.dumps(x) for x in rows) + "\n")
        env = {"CLAUDE_CONFIG_DIR": cfg}
        got = self.fm_json("cost", "--outputs", env=env)
        self.assertEqual((got["main"]["calls"], got["main"]["cache_read_tokens"], got["main"]["avg_context_read"]),
                         (4, 2000, 500), "m1 and m2 once each; the 30-day-old call left out")
        self.assertEqual((got["sub"]["calls"], got["sub"]["cache_read_tokens"], got["sub"]["avg_context_read"]),
                         (2, 500, 250))
        kinds = {k["kind"]: k for k in got["kinds"]}
        self.assertEqual({k: v["reread_tokens"] for k, v in kinds.items()},
                         {"main tests": 100, "main Read": 200, "main mcp": 25, "sub sed": 50})
        self.assertEqual((kinds["main Read"]["results"], kinds["main Read"]["avg_tokens"]), (1, 200))
        self.assertEqual(got["tool_output_reread_tokens"], 375)
        self.assertEqual(got["share_of_cache_read"], 15.0)  # 375 of 2,500
        self.assertEqual(kinds["main Read"]["share_of_cache_read"], 8.0)
        text = self.fm("cost", "--outputs", env=env).stdout
        self.assertIn("main Read", text)
        self.assertIn("15%", text)
        for private in ("secret", "/x/f", "test_x", "/r/plugin"):
            self.assertNotIn(private, text + json.dumps(got), "aggregates only: no command, path or text")

    def _project(self):
        cfg = os.path.join(self.tmp, "cfg")
        folder = os.path.join(cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        now = datetime.datetime.now(datetime.timezone.utc)
        at = lambda days=0, s=0: (now - datetime.timedelta(days=days) + datetime.timedelta(seconds=s)).strftime(  # noqa: E731
            "%Y-%m-%dT%H:%M:%S.000Z")
        use = lambda i, name, **inp: {"type": "tool_use", "id": i, "name": name, "input": inp}  # noqa: E731
        call = lambda mid, read, blocks, s=0, days=0: {"type": "assistant", "timestamp": at(days, s),  # noqa: E731
                                                       "message": {"id": mid, "usage": {"cache_read_input_tokens": read},
                                                                   "content": blocks}}
        got_back = lambda s, i, n, days=0: {"type": "user", "timestamp": at(days, s), "message": {"content": [  # noqa: E731
            {"type": "tool_result", "tool_use_id": i, "content": "x" * (n - 2)}]}}  # json.dumps adds two quotes
        write = lambda name, rows: open(os.path.join(folder, name), "w").write(  # noqa: E731
            "\n".join(json.dumps(x) for x in rows) + "\n")
        return cfg, call, use, got_back, write

    def test_the_cutoff_limits_the_totals_not_what_is_already_in_context(self):
        """A result read before the window is still in context for the window's first calls, and a result inside
        the window whose tool_use is older keeps its kind (T-4825 review)."""
        cfg, call, use, got_back, write = self._project()
        write("w.jsonl", [call("oc", 9999, [use("o1", "Read", file_path="/x/a"), use("o2", "Bash", command="git status")],
                               days=30),
                          got_back(1, "o1", 4000, days=30),
                          call("w1", 4000, []), got_back(1, "o2", 800), call("w2", 4000, [])])
        got = self.fm_json("cost", "--outputs", "--days", "7", env={"CLAUDE_CONFIG_DIR": cfg})
        self.assertEqual((got["main"]["calls"], got["main"]["cache_read_tokens"]), (2, 8000), "the 30-day call is out")
        kinds = {k["kind"]: k for k in got["kinds"]}
        self.assertEqual({k: v["reread_tokens"] for k, v in kinds.items()},
                         {"main Read": 2000, "main git status": 200}, "o1 in context for w1 and w2; o2 for w2")
        self.assertEqual(kinds["main git status"]["results"], 1, "a result whose tool_use is older keeps its kind")
        self.assertEqual(kinds["main Read"]["results"], 0, "a result before the window is not counted as one")
        self.assertEqual(got["share_of_cache_read"], 27.5)

    def test_a_forked_file_copying_history_counts_each_call_once_and_a_corrupt_id_is_skipped(self):
        cfg, call, use, got_back, write = self._project()
        history = [call("h1", 100, [use("a1", "Read", file_path="/x/a")]), got_back(1, "a1", 400), call("h2", 100, [])]
        write("a.jsonl", history)
        write("b.jsonl", history + [call("h3", 100, [])])  # a resumed session copies the history into a new file
        write("c.jsonl", [call("c1", 50, [{"type": "tool_use", "id": ["x"], "name": "Read", "input": {}},
                                          {"type": "tool_use", "id": {"y": 1}, "name": "Read", "input": {}}]),
                          {"type": "user", "timestamp": "2099-01-01T00:00:00.000Z", "message": {"content": [
                              {"type": "tool_result", "tool_use_id": {"z": 1}, "content": "abc"}]}},
                          call("c2", 50, [])])
        got = self.fm_json("cost", "--outputs", env={"CLAUDE_CONFIG_DIR": cfg})
        self.assertEqual((got["main"]["calls"], got["main"]["cache_read_tokens"]), (5, 400),
                         "h1, h2, h3, c1, c2 once each: 100+100+100+50+50")

    def test_context_parts_split_reply_attachments_compaction_and_snapshots(self):
        """T-4830: every part that sits in a context counts by kind, main and sub: a reply split over entries (its
        thinking estimated over the whole reply, never below 0), attachments by type, the user's text and reminders, a
        compact summary and a task notice. A prompt snapshot and a deferred-tool record are sent as no text and count
        nothing; a compaction clears the parts with the tool results; the tool total stays tool results only."""
        cfg, _, _, _, write = self._project()
        folder = os.path.join(cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(os.path.join(folder, "s1", "subagents"))
        now = datetime.datetime.now(datetime.timezone.utc)
        at = lambda s=0: (now + datetime.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
        att = lambda s, a: {"type": "attachment", "timestamp": at(s), "attachment": a}  # noqa: E731
        said = lambda s, content, **extra: {"type": "user", "timestamp": at(s), **extra,  # noqa: E731
                                            "message": {"content": content}}
        reply = lambda s, mid, block, out, read: {"type": "assistant", "timestamp": at(s), "sessionId": "s1",  # noqa: E731
                                                  "message": {"id": mid, "content": [block],
                                                              "usage": {"output_tokens": out,
                                                                        "cache_read_input_tokens": read}}}
        skill = {"type": "skill_listing", "content": "- sampo:next: " + "s" * 60}
        skill_chars = len(skill["content"])  # the listing's text, not its JSON
        tool_in = len(json.dumps({"file_path": "/x/a"}))  # the tool input's characters
        thinking = 50 * 4 - 20 - tool_in  # output tokens at 4 characters, less the reply's text and tool input
        reminder, task = len("<system-reminder>") + 60, len("<task-notification>") + 80
        main = [said(0, "p" * 40),  # the prompt: a plain string
                att(1, {"type": "hook_additional_context", "content": ["n" * 100], "hookEvent": "PreToolUse"}),
                att(1, {"type": "hook_success", "content": "", "hookName": "PostToolUse:Bash",
                        "stdout": json.dumps({"updatedToolOutput": "o" * 5000})}),  # UI only: no text reaches the model
                att(1, {"type": "hook_non_blocking_error", "content": "e" * 500}),  # UI only
                att(1, {"type": "command_permissions", "content": "p" * 500}),  # UI only
                att(1, {"type": "instructions",  # the files' text
                        "files": [{"path": "/x/CLAUDE.md", "content": "m" * 200}]}),
                att(1, {"type": "queued_command", "prompt": "s" * 70, "commandMode": "prompt",
                        "uuid": "u" * 36}),  # the steer: its prompt counts, its metadata does not
                att(1, {"type": "edited_text_file", "filename": "f" * 30, "snippet": "s" * 100}),
                att(1, {"type": "zz_future", "detail": "d" * 90, "uuid": "u" * 36, "count": 3}),  # no map: the fallback
                att(2, skill),
                att(3, {"type": "prompt_snapshot", "content": "q" * 5000}),
                {"type": "deferred_tools_record", "timestamp": at(3), "content": "d" * 5000},
                reply(4, "r1", {"type": "thinking", "thinking": "", "signature": "sig"}, 50, 1000),  # one reply, three entries
                reply(4, "r1", {"type": "text", "text": "t" * 20}, 50, 1000),
                reply(4, "r1", {"type": "tool_use", "id": "u1", "name": "Read", "input": {"file_path": "/x/a"}}, 50, 1000),
                said(5, [{"type": "tool_result", "tool_use_id": "u1", "content": "z" * 398},  # 400: a tool's
                         {"type": "text", "text": "<system-reminder>" + "w" * 60}]),  # a reminder, not the tool's
                reply(6, "r2", {"type": "text", "text": "u" * 10}, 5, 2000),
                {"type": "system", "subtype": "compact_boundary", "timestamp": at(7)},
                said(8, "c" * 300, isCompactSummary=True),
                att(9, skill),  # re-attached after the compaction
                said(10, "<task-notification>" + "k" * 80, origin={"kind": "task-notification"}),
                reply(11, "r3", {"type": "text", "text": "v" * 100}, 1, 3000),  # one output token holds no 100 characters
                reply(12, "r4", {"type": "text", "text": "x"}, 1, 4000)]
        sub = [said(0, "b" * 120), reply(1, "sr1", {"type": "text", "text": ""}, 0, 20)]
        write("w.jsonl", main)
        write(os.path.join("s1", "subagents", "agent-1.jsonl"), sub)
        got = self.fm_json("cost", "--outputs", env={"CLAUDE_CONFIG_DIR": cfg})
        self.assertEqual((got["main"]["calls"], got["main"]["cache_read_tokens"]), (4, 10000))
        self.assertEqual((got["sub"]["calls"], got["sub"]["cache_read_tokens"]), (1, 20))
        want = {"main user text": 2 * 40, "main hook notes": 2 * 100, "main attachment: skill_listing": 4 * skill_chars,
                "main attachment: instructions": 2 * 200, "main attachment: queued_command": 2 * 70,
                "main attachment: edited_text_file": 2 * 130, "main attachment: zz_future": 2 * 90,
                "main reply text": 20 + 100, "main tool input": tool_in, "main thinking": thinking,
                "main reminders": reminder, "main compact summary": 2 * 300, "main task notice": 2 * task,
                "sub user text": 120}
        self.assertEqual({k["kind"]: k["reread_tokens"] for k in got["parts"]}, {k: round(v / 4) for k, v in want.items()})
        self.assertEqual(got["parts_reread_tokens"], sum(round(v / 4) for v in want.values()))
        self.assertEqual({k["kind"]: k["reread_tokens"] for k in got["kinds"]}, {"main Read": 100},
                         "the Read result, 400 characters, counted in the one call that held it")
        self.assertEqual(got["tool_output_reread_tokens"], 100, "tool results only: no part is in this total")
        self.assertEqual(got["share_of_cache_read"], round(100 * 100 / 10020, 1))
        main_parts = sum(round(v / 4) for k, v in want.items() if k.startswith("main"))  # tokens, as the report rounds
        self.assertEqual(got["main"]["tool_outputs_per_call"], 25)  # 100 tokens re-read over 4 calls
        self.assertEqual(got["main"]["parts_per_call"], round(main_parts / 4))
        self.assertEqual(got["main"]["unaccounted_per_call"], 2500 - 25 - round(main_parts / 4),
                         "the context the transcripts do not hold: system prompt, tool schemas")
        self.assertEqual(got["sub"]["parts_per_call"], 30)
        self.assertEqual(got["sub"]["unaccounted_per_call"], 0, "20 cache-read tokens under 30 of parts: never below 0")
        self.assertFalse(any("snapshot" in k["kind"] or "deferred" in k["kind"] for k in got["parts"]))
        text = self.fm("cost", "--outputs", env={"CLAUDE_CONFIG_DIR": cfg}).stdout
        self.assertIn("not in the transcripts", text)
        self.assertIn("main reply text", text)
        self.assertNotIn("/x/a", text + json.dumps(got), "aggregates only: no tool input, path or text")

    def test_a_reply_with_no_thinking_block_estimates_no_thinking(self):
        """T-4830: thinking is estimated only for a reply that holds a thinking block. A reply of three tool calls and
        no thinking block gives no thinking, though its output tokens (the tokenizer's slack, the JSON separators) are
        there."""
        cfg, _, use, _, write = self._project()
        now = datetime.datetime.now(datetime.timezone.utc)
        at = lambda s: (now + datetime.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
        tools = [use("t1", "Read", file_path="/x/a"), use("t2", "Read", file_path="/x/b"),
                 use("t3", "Read", file_path="/x/c")]
        rows = [{"type": "user", "timestamp": at(0), "message": {"content": "p" * 40}}]
        rows += [{"type": "assistant", "timestamp": at(1), "message": {"id": "r1", "content": [block],
                                                                    "usage": {"output_tokens": 200,
                                                                              "cache_read_input_tokens": 1000}}}
                 for block in tools]
        rows += [{"type": "user", "timestamp": at(2), "message": {"content": [
            {"type": "tool_result", "tool_use_id": t, "content": "z" * 10} for t in ("t1", "t2", "t3")]}}]
        rows += [{"type": "assistant", "timestamp": at(3), "message": {"id": "r2", "content": [
            {"type": "text", "text": "done"}], "usage": {"output_tokens": 1, "cache_read_input_tokens": 2000}}}]
        write("w.jsonl", rows)
        got = self.fm_json("cost", "--outputs", env={"CLAUDE_CONFIG_DIR": cfg})
        self.assertEqual(got["main"]["calls"], 2)
        kinds = {k["kind"] for k in got["parts"]}
        self.assertNotIn("main thinking", kinds, "no thinking block: the output tokens are not thinking")
        tool_in = sum(len(json.dumps(t["input"])) for t in tools)
        self.assertIn("main tool input", kinds)
        self.assertEqual({k["kind"]: k["reread_tokens"] for k in got["parts"]}["main tool input"], round(tool_in / 4))


class LeadCacheRead(HookCase):
    """T-5769: `sampo cost --lead`: the lead's cache-read tokens per turn (median), the 24 h before and the 24 h after the
    keeper's first compact. The lead is the main thread: a subagent's turns are left out."""

    def _rig(self, first_ago_h):
        self.fm("init")
        cfg = os.path.join(self.tmp, "cfg")
        folder = os.path.join(cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        now = datetime.datetime.now(datetime.timezone.utc)
        self.now, self.cfg, self.folder = now, cfg, folder
        if first_ago_h is not None:
            os.makedirs(os.path.join(self.home, "state"), exist_ok=True)
            with open(os.path.join(self.home, "state", "keeper.json"), "w") as f:
                json.dump({"first": (now - datetime.timedelta(hours=first_ago_h)).timestamp(), "panes": {}}, f)

    def _turns(self, name, hours_ago_and_reads, sidechain=False):
        rows = []
        for i, (h, reads, *made) in enumerate(hours_ago_and_reads):
            ts = (self.now - datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            row = {"type": "assistant", "timestamp": ts, "sessionId": "lead-1", "message": {
                "id": f"{name}-{i}", "usage": {"input_tokens": 5, "cache_read_input_tokens": reads, "output_tokens": 9,
                                               "cache_creation_input_tokens": made[0] if made else 0},
                "content": []}}
            rows.append(row)
        where = os.path.join(self.folder, "lead-1", "subagents") if sidechain else self.folder
        os.makedirs(where, exist_ok=True)
        with open(os.path.join(where, name + ".jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")

    def test_lead_cache_read_median_before_and_after_the_first_compact(self):
        self._rig(first_ago_h=6)
        self._turns("lead-1", [(20, 300000, 4000), (12, 280000, 6000), (10, 320000, 5000),
                               (3, 90000, 30000), (2, 110000, 10000), (1, 100000, 20000)])
        self._turns("agent-1", [(2, 900000), (3, 900000)], sidechain=True)  # a subagent's context isn't the lead's
        got = self.fm_json("cost", "--lead", env={"CLAUDE_CONFIG_DIR": self.cfg})
        self.assertEqual(got["before"], {"turns": 3, "median_cache_read": 300000, "median_cache_creation": 5000})
        self.assertEqual(got["after"], {"turns": 3, "median_cache_read": 100000, "median_cache_creation": 20000},
                         "cache-creation tokens beside the reads: a compact rewrites the cache once, and that is a cost")
        self.assertAlmostEqual(got["change_pct"], -66.7, places=1)
        text = self.fm("cost", "--lead", env={"CLAUDE_CONFIG_DIR": self.cfg}).stdout
        self.assertIn("cache creation", text)

    def test_lead_cache_read_with_no_projects_folder_is_a_sentence_not_a_traceback(self):
        self.fm("init")
        proc = self.fm("cost", "--lead", env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "none")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("no compact", proc.stdout)
        got = json.loads(self.fm("cost", "--lead", "--json", env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "none")}).stdout)
        self.assertEqual(got["last_24h"], {"turns": 0, "median_cache_read": None, "median_cache_creation": None})

    def test_lead_cache_read_window_is_24_hours_each_side(self):
        self._rig(first_ago_h=30)
        self._turns("lead-1", [(70, 111), (50, 400000), (31, 200000), (29, 50000), (7, 60000), (4, 70000)])
        got = self.fm_json("cost", "--lead", env={"CLAUDE_CONFIG_DIR": self.cfg})
        self.assertEqual(got["before"]["turns"], 2)  # the turn 70 h back is out
        self.assertEqual(got["before"]["median_cache_read"], 300000)
        self.assertEqual(got["after"]["turns"], 2)  # the turn 4 h back is past 24 h after
        self.assertEqual(got["after"]["median_cache_read"], 55000)

    def test_lead_cache_read_before_the_keeper_has_compacted_anything(self):
        self._rig(first_ago_h=None)
        self._turns("lead-1", [(2, 100000), (1, 300000)])
        got = self.fm_json("cost", "--lead", env={"CLAUDE_CONFIG_DIR": self.cfg})
        self.assertIsNone(got["first_compact"])
        self.assertEqual(got["last_24h"]["turns"], 2)
        self.assertEqual(got["last_24h"]["median_cache_read"], 200000)
        text = self.fm("cost", "--lead", env={"CLAUDE_CONFIG_DIR": self.cfg}).stdout
        self.assertIn("no compact", text)


class PerLane(HookCase):
    """T-5950: `sampo cost --per-lane`: per landed lane the four token kinds of its lead, builder, reviewer and fixer
    shares, a median per role, a saved baseline to compare against, and the runs that fit no lane shown."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.cfg = os.path.join(self.tmp, "cfg")
        self.folder = os.path.join(self.cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(os.path.join(self.folder, "lead-1", "subagents"))
        self.t0 = datetime.datetime.now(datetime.timezone.utc).timestamp() - 10 * 3600
        self.p = self.project()
        self.addCleanup(lambda: os.environ.pop("FOREMAN_NOW", None))

    def at(self, hours):
        return self.t0 + hours * 3600

    def ev(self, hours, event, task=None, data=None):
        from sampo import core as c
        os.environ["FOREMAN_NOW"] = c.iso(self.at(hours))
        try:
            c.log_event(self.p, event, task=task, data=data or {})
        finally:
            os.environ.pop("FOREMAN_NOW", None)

    def reply(self, mid, i, cr, cw, out, model="claude-x", ts=None):
        row = {"type": "assistant", "sessionId": "lead-1", "message": {
            "id": mid, "model": model, "usage": {"input_tokens": i, "cache_read_input_tokens": cr,
                                                 "cache_creation_input_tokens": cw, "output_tokens": out}, "content": []}}
        if ts is not None:
            row["timestamp"] = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return row

    def write(self, path, rows):
        with open(path, "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")

    def run_file(self, name, rows):
        path = os.path.join(self.folder, "lead-1", "subagents", name + ".jsonl")
        self.write(path, rows)
        return path

    def merge(self, subject, hours, repo=None):
        """A commit on the repo's HEAD, dated `hours` after t0: the trunk's record of a landing."""
        from sampo import core as c
        when = c.iso(self.at(hours))
        subprocess.run(["git", "-C", repo or self.repo, "commit", "-q", "--allow-empty", "-m", subject], check=True,
                       env=dict(os.environ, GIT_COMMITTER_DATE=when, GIT_AUTHOR_DATE=when))

    def prompt(self, text):
        """A reviewer transcript's first row: the lead's prompt, as a string or as text blocks."""
        content = text if isinstance(text, str) else [{"type": "text", "text": t} for t in text]
        return {"type": "user", "message": {"role": "user", "content": content}}

    def stop(self, hours, kind, aid, rows, task=None):
        path = self.run_file(aid, rows) if rows is not None else os.path.join(self.tmp, "gone", aid + ".jsonl")
        self.ev(hours, "subagent", task=task, data={"agent_type": kind, "agent_id": aid, "transcript": path})

    def rig(self):
        """Lane A (T-0001, hours 0 to 3) and lane B (T-0002, hours 1 to 4) overlap between hours 1 and 3."""
        self.ev(0, "lane_builder", "T-0001", {"path": "/x/.claude/worktrees/agent-aaa"})
        self.ev(1, "lane_builder", "T-0002", {"path": "/x/.claude/worktrees/agent-bbb/"})
        self.ev(1.5, "reviewer_bound", "T-0001", {"agent_id": "rrr"})
        # a builder's own stop names no task: its worktree path in the lane_builder event binds it
        self.stop(2, "sampo:fm-builder", "aaa", [self.reply("m1", 100, 5000, 500, 50), self.reply("m2", 100, 5000, 500, 50),
                                                  self.reply("m2", 100, 5000, 500, 50)])  # m2 twice: one reply
        # the lead's focus (T-0099) at the reviewer's stop is a guess: the reviewer_bound row wins
        self.stop(2.5, "sampo:fm-reviewer", "rrr", [self.reply("m1", 10, 2000, 0, 5)], task="T-0099")
        self.stop(2.6, "sampo:fm-fixer", "fff", [self.reply("m1", 5, 100, 0, 1)], task="T-0001")
        self.stop(3.2, "sampo:fm-builder", "bbb", [self.reply("m1", 300, 9000, 800, 80)], task="T-0002")
        self.stop(3.5, "sampo:fm-reviewer", "rrb", [self.reply("m1", 20, 4000, 0, 10)], task="T-0002")
        self.stop(3.6, "sampo:fm-scout", "zzz", [self.reply("m1", 7, 70, 7, 7)])  # no task, bound to nothing
        self.stop(3.7, "sampo:fm-reviewer", "rgone", None, task="T-0002")  # its transcript is gone
        self.ev(3, "lane_landed", "T-0001", {"tasks": ["T-0001"]})
        self.ev(4, "lane_landed", "T-0002", {"tasks": ["T-0002"]})
        self.merge("Merge T-0001: lane A", 3)
        self.merge("Merge T-0002: lane B", 4)
        lead =[self.reply(f"l{i}", 10, 1000, 100, 20, "claude-lead", self.at(h))
                for i, h in enumerate((0.5, 0.6, 2, 2.1, 5))]  # A alone, A alone, A and B, A and B, no lane
        self.write(os.path.join(self.folder, "lead-1.jsonl"), lead)

    def per_lane(self, *flags):
        return self.fm_json("cost", "--per-lane", *flags, env={"CLAUDE_CONFIG_DIR": self.cfg})

    def test_per_lane_splits_each_landed_lane_by_role(self):
        self.rig()
        got = self.per_lane()
        self.assertEqual(got["landed"], 2)
        a, b = got["lanes"]["T-0001"], got["lanes"]["T-0002"]
        self.assertEqual({k: a["builder"][k] for k in ("input", "cache_read", "cache_write", "output")},
                         {"input": 200, "cache_read": 10000, "cache_write": 1000, "output": 100},
                         "two replies; the one repeated by message id counts once")
        self.assertEqual(a["reviewer"]["cache_read"], 2000, "the reviewer_bound row, not the focused T-0099")
        self.assertNotIn("T-0099", got["lanes"])
        self.assertEqual(a["fixer"]["input"], 5)
        self.assertEqual((b["builder"]["input"], b["reviewer"]["cache_read"]), (300, 4000))
        self.assertNotIn("fixer", b)
        self.assertEqual((a["lead"]["input"], a["lead"]["cache_read"], a["lead"]["cache_write"], a["lead"]["output"]),
                         (30, 3000, 300, 60), "two replies alone and half of two shared with lane B")
        self.assertEqual((b["lead"]["input"], b["lead"]["turns"]), (10, 1), "the other halves")
        self.assertEqual(a["lead"]["models"], {"claude-lead": 3})
        self.assertEqual(a["builder"]["models"], {"claude-x": 2})

    def test_per_lane_median_per_role(self):
        self.rig()
        m = self.per_lane()["median"]
        self.assertEqual(m["builder"]["n"], 2)
        self.assertEqual(m["builder"]["input"], 250)  # the median of 200 and 300
        self.assertEqual(m["builder"]["cache_read"], 9500)
        self.assertEqual(m["reviewer"]["n"], 2)
        self.assertEqual(m["fixer"]["n"], 1)
        self.assertEqual(m["lead"]["input"], 20)
        self.assertEqual(m["builder"]["equiv"], round((250 + 9500 * 0.1 + 900 * 1.25 + 90 * 5)))

    def test_per_lane_shows_the_runs_and_replies_that_fit_no_lane(self):
        self.rig()
        got = self.per_lane()
        un = got["unattributed"]
        by = {u["agent_id"]: u for u in un["subagents"]}
        self.assertEqual(set(by), {"zzz", "rgone"})
        self.assertEqual(by["zzz"]["why"], "no task found")
        self.assertEqual(by["zzz"]["input"], 7)
        self.assertEqual(by["rgone"]["why"], "transcript not read")
        self.assertEqual((un["lead"]["input"], un["lead"]["turns"]), (10, 1), "the reply at hour 5 is in no lane's window")
        text = self.fm("cost", "--per-lane", env={"CLAUDE_CONFIG_DIR": self.cfg}).stdout
        self.assertIn("Unattributed", text)
        self.assertIn("T-0001", text)
        self.assertIn("Median per role", text)

    def test_per_lane_reads_no_transcript_outside_the_projects_folder(self):
        self.rig()
        outside = os.path.join(self.tmp, "elsewhere.jsonl")
        self.write(outside, [self.reply("m1", 999, 0, 0, 0)])
        self.ev(3.8, "subagent", task="T-0001", data={"agent_type": "sampo:fm-builder", "agent_id": "xxx",
                                                      "transcript": outside})
        got = self.per_lane()
        self.assertEqual(got["lanes"]["T-0001"]["builder"]["input"], 200, "a path outside the folder is not opened")
        self.assertIn("xxx", [u["agent_id"] for u in got["unattributed"]["subagents"]])

    def test_per_lane_baseline_saved_and_compared_per_role(self):
        self.rig()
        none = self.fm("cost", "--per-lane", "--compare-baseline", env={"CLAUDE_CONFIG_DIR": self.cfg})
        self.assertEqual(none.returncode, 0, none.stderr)
        self.assertIn("No baseline saved", none.stdout)
        saved = self.per_lane("--save-baseline")
        self.assertTrue(saved["baseline_saved"])
        again = self.per_lane("--compare-baseline")
        self.assertEqual(again["baseline"]["change"]["builder"]["equiv"], 0.0)
        # the builder's work on lane B doubles; every other role is as it was
        self.run_file("bbb", [self.reply("m1", 300, 9000, 800, 80), self.reply("m2", 300, 9000, 800, 80)])
        got = self.per_lane("--compare-baseline")
        ch = got["baseline"]["change"]
        self.assertEqual(ch["builder"]["input"], 60.0)  # the median of 200 and 600 (400) against 200 and 300 (250)
        self.assertGreater(ch["builder"]["equiv"], 0)
        self.assertEqual(ch["reviewer"]["equiv"], 0.0)
        self.assertEqual(ch["lead"]["equiv"], 0.0)
        text = self.fm("cost", "--per-lane", "--compare-baseline", env={"CLAUDE_CONFIG_DIR": self.cfg}).stdout
        self.assertIn("builder: ", text)
        self.assertIn("+", text)

    def test_per_lane_counts_a_lane_once_by_its_merge_on_the_trunk_and_prints_git_beside_it(self):
        self.rig()
        # the ledger says T-0001 landed twice and T-0003 landed; the trunk has T-0001's merge twice (a re-landing), a
        # revert of T-0002's merge (no landing), an old merge outside the window and no merge of T-0003
        self.ev(3.5, "lane_landed", "T-0001", {"tasks": ["T-0001"]})
        self.ev(3.6, "lane_landed", "T-0003", {"tasks": ["T-0003"]})
        self.merge("Merge T-0001: lane A again", 3.9)
        self.merge('Revert "Merge T-0002: lane B"', 4.1)
        self.merge("Merge T-0004: long ago", -24 * 40)
        got = self.per_lane()
        self.assertEqual(got["landed"], 2, "by tasks with a merge commit in the window, a re-landed one once")
        self.assertEqual(sorted(got["lanes"]), ["T-0001", "T-0002"])
        self.assertEqual(got["git"], {"source": "git", "merges": 3, "tasks": 2})
        self.assertEqual(got["ledger"], {"tasks": 3, "events": 4})
        text = self.fm("cost", "--per-lane", env={"CLAUDE_CONFIG_DIR": self.cfg}).stdout
        self.assertIn("2 lane(s) landed", text)
        self.assertIn("git: 3 Merge T-", text)
        self.assertIn("ledger: 3 task(s)", text)

    def test_per_lane_from_a_worktree_reads_the_main_projects_lead_and_trunk(self):
        wt = os.path.join(self.tmp, "wt")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "side", wt], check=True)
        self.rig()  # the merges land on the main checkout's branch: the worktree's own HEAD lacks them
        got = self.fm_json("cost", "--per-lane", cwd=wt, env={"CLAUDE_CONFIG_DIR": self.cfg})
        self.assertEqual(got["landed"], 2)
        self.assertEqual(got["lanes"]["T-0001"]["lead"]["input"], 30, "the lead's transcripts are the main folder's")
        self.assertEqual(got["median"]["lead"]["n"], 2)

    def test_per_lane_binds_a_run_by_the_brief_or_task_its_first_message_names(self):
        self.rig()
        ask = "Read /s/audits/T-0002.review.md and do the review it describes. Also see T-0001 for context."
        self.stop(2.7, "sampo:fm-reviewer", "pbrief", [self.prompt(ask), self.reply("m1", 11, 100, 0, 1)], task="T-0099")
        self.stop(2.8, "sampo:fm-reviewer", "pblocks", [self.prompt(["hello", "The branch is worktree-agent-aaa (tip 1)."]),
                                                         self.reply("m1", 12, 100, 0, 1)])
        self.stop(2.9, "sampo:fm-reviewer", "pbare", [self.prompt("Review T-0001 and then T-0002."),
                                                       self.reply("m1", 13, 100, 0, 1)])
        self.stop(2.95, "sampo:fm-reviewer", "pnone", [self.prompt("Review the thing."), self.reply("m1", 14, 100, 0, 1)])
        self.stop(2.96, "sampo:fm-reviewer", "pold", [self.prompt("Read /s/audits/T-0009.review.md"),
                                                       self.reply("m1", 15, 100, 0, 1)])
        got = self.per_lane()
        a, b = got["lanes"]["T-0001"]["reviewer"], got["lanes"]["T-0002"]["reviewer"]
        self.assertEqual(b["input"], 20 + 11, "the review brief it names, not the focused T-0099 and not the T-0001 it mentions")
        self.assertEqual(a["input"], 10 + 12 + 13, "rrr (bound), the worktree its prompt names and the first bare task")
        by = {u["agent_id"]: u for u in got["unattributed"]["subagents"]}
        self.assertEqual(by["pnone"]["why"], "no task found")
        self.assertEqual(by["pold"]["why"], "T-0009 not landed in the window")
        self.assertNotIn("pbrief", by)

    def test_per_lane_counts_the_lead_replies_of_sessions_in_lane_worktrees(self):
        self.rig()
        # a session run inside a lane's worktree has its own project folder beside the main one
        for name in (self.folder + "--claude-worktrees-agent-zzz", self.folder + "-lanes-t1"):
            os.makedirs(name)
            self.write(os.path.join(name, "s.jsonl"), [self.reply("w-" + name[-3:], 7, 0, 0, 0, "claude-lead", self.at(0.55))])
        other = self.folder + "x"  # another project whose name only starts with this one's
        os.makedirs(other)
        self.write(os.path.join(other, "s.jsonl"), [self.reply("o1", 1000, 0, 0, 0, "claude-lead", self.at(0.55))])
        a = self.per_lane()["lanes"]["T-0001"]["lead"]
        self.assertEqual((a["input"], a["turns"]), (30 + 14, 5), "both worktree folders count; the sibling project does not")

    def test_per_lane_takes_the_lead_replies_a_caller_already_read(self):
        """T-5973: targets.py reads each transcript once for three readers and hands per_lane the lead's replies."""
        from sampo import cost
        self.rig()
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg}):
            own = cost.per_lane(self.p, 14)
            replies = cost.lead_replies(cost.project_folders(self.p), cost._since(14 + cost.PER_LANE_LOOKBACK_DAYS))
            self.assertTrue(replies)
            self.assertEqual(cost.per_lane(self.p, 14, lead_replies=replies), own, "the same replies, the same shares")
            bare = cost.per_lane(self.p, 14, lead_replies=[])
        self.assertTrue(own["lanes"]["T-0001"]["lead"]["turns"] > 0)
        self.assertNotIn("lead", bare["lanes"]["T-0001"], "none handed in is none read: the transcripts are not read again")

    def test_per_lane_keeps_the_early_lead_replies_of_a_lane_that_began_before_the_lookback(self):
        self.ev(-24 * 9, "lane_new", "T-0005", {})  # nine days back: before days 1 + the 7 days the ledger is read earlier
        self.ev(0, "lane_builder", "T-0005", {"path": "/x/.claude/worktrees/agent-eee"})
        self.ev(1, "lane_landed", "T-0005", {"tasks": ["T-0005"]})
        self.merge("Merge T-0005: slow lane", 1)
        self.write(os.path.join(self.folder, "lead-1.jsonl"), [
            self.reply("early", 40, 0, 0, 0, "claude-lead", self.at(-24 * 7)), self.reply("late", 2, 0, 0, 0, "claude-lead", self.at(0.5))])
        got = self.per_lane("--days", "1")
        self.assertEqual(got["lanes"]["T-0005"]["lead"]["input"], 42, "the reply from the lane's first days is in its window")

    def test_per_lane_shows_an_unplaced_run_from_before_the_window(self):
        self.rig()
        self.stop(-72, "sampo:fm-scout", "oldscout", [self.reply("m1", 9, 0, 0, 0)])  # no task, three days before the window
        self.stop(-71, "sampo:fm-reviewer", "oldrev", [self.reply("m1", 8, 0, 0, 0)], task="T-0077")  # another lane's
        got = self.per_lane("--days", "1")
        by = {u["agent_id"]: u for u in got["unattributed"]["subagents"]}
        self.assertIn("oldscout", by, "it fits no lane and says nothing of another: shown, not dropped")
        self.assertEqual((by["oldscout"]["input"], by["oldscout"]["why"]), (9, "no task found"))
        self.assertNotIn("oldrev", by, "a run bound to a task that isn't this window's is that lane's")

    def test_per_lane_names_the_other_role(self):
        self.rig()
        text = self.fm("cost", "--per-lane", env={"CLAUDE_CONFIG_DIR": self.cfg}).stdout
        self.assertIn("other = any agent that is not a builder, reviewer or fixer", text)

    def test_per_lane_baseline_flags_imply_per_lane_and_the_help_says_so(self):
        self.rig()
        got = self.fm_json("cost", "--save-baseline", env={"CLAUDE_CONFIG_DIR": self.cfg})
        self.assertTrue(got["baseline_saved"])
        self.assertIn("lanes", got)
        helped = self.fm("cost", "--help").stdout
        self.assertIn("implies --per-lane", " ".join(helped.split()))

    def test_per_lane_compare_says_when_a_role_has_no_earlier_figure(self):
        from sampo import cost
        self.rig()
        self.per_lane("--save-baseline")
        path = cost._baseline_path(self.p)
        with open(path) as f:
            saved = json.load(f)
        del saved["median"]["reviewer"]
        with open(path, "w") as f:
            json.dump(saved, f)
        got = self.per_lane("--compare-baseline")
        self.assertIsNone(got["baseline"]["change"]["reviewer"]["was"])
        text = self.fm("cost", "--per-lane", "--compare-baseline", env={"CLAUDE_CONFIG_DIR": self.cfg}).stdout
        self.assertIn("reviewer: no earlier figure", text)
        self.assertNotIn("≈ 0 →", text)
        self.assertIn("builder: ≈", text)

    def test_per_lane_ignores_a_baseline_file_of_the_wrong_shape_and_says_why(self):
        from sampo import cost
        self.rig()
        path = cost._baseline_path(self.p)
        for raw, why in (("null", "not a JSON object"), ("[1]", "not a JSON object"), ('"x"', "not a JSON object"),
                         ('{"median": []}', "no median"), ('{"saved": 1}', "no median"), ("{oops", "not valid JSON")):
            with self.subTest(raw=raw):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(raw)
                proc = self.fm("cost", "--per-lane", "--compare-baseline", env={"CLAUDE_CONFIG_DIR": self.cfg})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertIn("ignored", proc.stdout)
                self.assertIn(why, proc.stdout)
                got = self.per_lane("--compare-baseline")
                self.assertEqual(got["baseline"]["change"], {})

    def test_per_lane_with_nothing_landed_is_a_sentence_not_a_traceback(self):
        proc = self.fm("cost", "--per-lane", env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "none")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("0 lane(s) landed", proc.stdout)


class VsNative(HookCase):
    """T-6060: `sampo cost --vs-native`: dollars per landed lane by role (each token kind at its model family's price, a
    local model at zero), the plain Claude Code reference with its source and n, the ratio and the 10x line."""

    at, ev, reply, write, run_file, merge, stop = (PerLane.__dict__[k] for k in (
        "at", "ev", "reply", "write", "run_file", "merge", "stop"))
    SONNET, OPUS, HAIKU, LOCAL = "claude-sonnet-4-5", "claude-opus-4-7", "claude-haiku-4-5", "qwen3:30b"

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.cfg = os.path.join(self.tmp, "cfg")
        self.folder = os.path.join(self.cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(os.path.join(self.folder, "lead-1", "subagents"))
        self.t0 = datetime.datetime.now(datetime.timezone.utc).timestamp() - 10 * 3600
        self.p = self.project()
        self.addCleanup(lambda: os.environ.pop("FOREMAN_NOW", None))

    def rig(self, ref=True):
        """Lane A (T-0001): a sonnet builder, a haiku reviewer, an opus lead. Lane B (T-0002): a LOCAL builder, a sonnet reviewer."""
        self.ev(0, "lane_builder", "T-0001", {"path": "/x/.claude/worktrees/agent-aaa"})
        self.ev(1, "lane_builder", "T-0002", {"path": "/x/.claude/worktrees/agent-bbb/"})
        self.ev(1.5, "reviewer_bound", "T-0001", {"agent_id": "rrr"})
        self.stop(2, "sampo:fm-builder", "aaa", [self.reply("m1", 100, 5000, 500, 50, self.SONNET),
                                                  self.reply("m2", 100, 5000, 500, 50, self.SONNET)])
        self.stop(2.5, "sampo:fm-reviewer", "rrr", [self.reply("m1", 10, 2000, 0, 5, self.HAIKU)])
        self.stop(3.2, "sampo:fm-builder", "bbb", [self.reply("m1", 300, 9000, 800, 80, self.LOCAL)], task="T-0002")
        self.stop(3.5, "sampo:fm-reviewer", "rrb", [self.reply("m1", 20, 4000, 0, 10, self.SONNET)], task="T-0002")
        self.ev(3, "lane_landed", "T-0001", {"tasks": ["T-0001"]})
        self.ev(4, "lane_landed", "T-0002", {"tasks": ["T-0002"]})
        self.merge("Merge T-0001: lane A", 3)
        self.merge("Merge T-0002: lane B", 4)
        self.write(os.path.join(self.folder, "lead-1.jsonl"), [
            self.reply(f"l{i}", 10, 1000, 100, 20, self.OPUS, self.at(h)) for i, h in enumerate((0.5, 0.6))])
        if ref:
            os.makedirs(self.p.dir, exist_ok=True)
            with open(os.path.join(self.p.dir, "native-reference.json"), "w") as f:
                json.dump({"usd_per_task": 0.01, "n": 3, "source": "exam tests, plain arm", "date": "2026-11-22",
                           "measured": True}, f)

    def vs(self, *flags):
        return self.fm_json("cost", "--vs-native", *flags, env={"CLAUDE_CONFIG_DIR": self.cfg})

    def test_vs_native_prices_each_role_per_token_kind_and_model_family(self):
        self.rig()
        got = self.vs()["lanes"]
        a, b = got["T-0001"], got["T-0002"]
        # sonnet $3 in, $0.30 read, $3.75 write, $15 out per M: 200 in, 10000 read, 1000 write, 100 out
        self.assertAlmostEqual(a["builder"]["usd"], 0.00885, places=8)
        # opus $5, $0.50, $6.25, $25: 20 in, 2000 read, 200 write, 40 out
        self.assertAlmostEqual(a["lead"]["usd"], 0.00335, places=8)
        # haiku $1, $0.10, $1.25, $5: 10 in, 2000 read, 5 out
        self.assertAlmostEqual(a["reviewer"]["usd"], 0.000235, places=8)
        self.assertAlmostEqual(b["reviewer"]["usd"], 0.00141, places=8)

    def test_vs_native_counts_a_local_model_lane_at_zero_dollars_but_keeps_its_tokens(self):
        self.rig()
        got = self.vs()
        b = got["lanes"]["T-0002"]["builder"]
        self.assertEqual(b["usd"], 0)
        self.assertTrue(b["local"])
        self.assertEqual(b["tokens"]["cache_read"], 9000, "counted, not dropped")
        self.assertFalse(got["lanes"]["T-0001"]["builder"]["local"])
        self.assertEqual(got["median"]["builder"]["n"], 2)

    def test_vs_native_prints_the_reference_with_its_source_and_n_the_ratio_and_the_10x_line(self):
        self.rig()
        got = self.vs()
        ref = got["reference"]
        self.assertEqual((ref["usd_per_task"], ref["n"], ref["source"]), (0.01, 3, "exam tests, plain arm"))
        self.assertAlmostEqual(got["lane_usd_median"], (0.012435 + 0.00141) / 2, places=8)
        self.assertAlmostEqual(got["ratio"], got["lane_usd_median"] / 0.01, places=4)
        self.assertAlmostEqual(got["target_usd"], 0.001, places=8)
        self.assertEqual(got["target_ratio"], 0.1)
        self.assertAlmostEqual(got["builder_usd_median"], 0.00885 / 2, places=8)
        text = self.fm("cost", "--vs-native", env={"CLAUDE_CONFIG_DIR": self.cfg}).stdout
        for part in ("lead", "builder", "reviewer", "fixer", "exam tests, plain arm", "n 3", "Ratio", "10x"):
            self.assertIn(part, text)
        # the dollar figures of the synthetic lanes, not only the role names
        for part in ("T-0001: $0.0124", "T-0002: $0.0014", "reviewer $0.0002", "(local)", "Per lane, all roles (median): $0.0069",
                     "0.69x", "$0.0010 per lane", "6.9 times over it"):
            self.assertIn(part, text)
        self.assertIn("T-6071", text, "headless children (rival, cards) are not counted yet, and the view says so")
        self.assertNotIn("rival: none recorded", text)

    def test_a_missing_or_corrupt_price_table_gives_no_dollar_figure_and_no_ratio_never_met(self):
        from sampo import cost
        self.rig()
        for name, body in (("missing.json", None), ("corrupt.json", "{not json"), ("empty.json", "{}"),
                           ("noopus.json", json.dumps({"families": {"sonnet": {"input": 3, "cache_read": 0.3,
                                                                                "cache_write": 3.75, "output": 15},
                                                                    "haiku": {"input": 1}}}))):
            path = os.path.join(self.tmp, name)
            if body is not None:
                with open(path, "w") as f:
                    f.write(body)
            with self.subTest(name), unittest.mock.patch.object(cost, "MODEL_PRICES", path), \
                    unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg}):
                got = cost.vs_native(self.p, 14)
                text = cost.vs_native_text(got)
            self.assertIsNone(got["ratio"])
            self.assertIsNone(got["lane_usd_median"])
            self.assertIsNone(got["builder_ratio"])
            self.assertTrue(got["no_price"], "the view says which family has no price")
            self.assertNotIn("met", text.replace("(median)", ""))
            self.assertIn("no price", text)
            self.assertNotIn("lead $", text, "a Claude role with no price shows n/a, never $0")

    def test_a_local_only_lane_is_zero_dollars_even_with_no_price_table(self):
        from sampo import cost
        rec = {"input": 300, "cache_read": 9000, "cache_write": 800, "output": 80, "turns": 1, "models": {self.LOCAL: 1}}
        got = cost.usd(rec, {"families": {}})
        self.assertEqual(got["usd"], 0)
        self.assertTrue(got["local"])

    def test_a_provider_prefixed_claude_id_is_claude_and_an_unknown_family_prices_at_the_highest(self):
        from sampo import cost
        table = cost.prices()
        for m in ("us.anthropic.claude-sonnet-4-5-v1:0", "anthropic/claude-opus-4-7", "anthropic.claude-3-haiku"):
            self.assertNotEqual(cost.model_family(m, table)[0], "local", m)
        self.assertEqual(cost.model_family("us.anthropic.claude-sonnet-4-5-v1:0", table), ("sonnet", False))
        self.assertEqual(cost.model_family("anthropic/claude-opus-4-7", table), ("opus", False))
        self.assertEqual(cost.model_family("qwen3:30b", table), ("local", False))
        fam, guess = cost.model_family("anthropic/claude-next-9", table)
        self.assertEqual((fam, guess), ("opus", True), "the highest-priced family, flagged")
        rec = {"input": 1_000_000, "cache_read": 0, "cache_write": 0, "output": 0, "turns": 1,
               "models": {"anthropic/claude-next-9": 1}}
        got = cost.usd(rec, table)
        self.assertAlmostEqual(got["usd"], 5.0)
        self.assertEqual(got["assumed"], ["anthropic/claude-next-9"])

    def test_the_text_labels_the_reference_unmeasured_and_says_claude_exam_runs_are_paused(self):
        self.rig(ref=False)
        text = self.fm("cost", "--vs-native", env={"CLAUDE_CONFIG_DIR": self.cfg}).stdout
        for part in ("$0.93", "n 1", "not measured", "paused"):
            self.assertIn(part, text)

    def test_the_help_says_headless_children_are_not_counted_yet(self):
        out = self.fm("cost", "--help").stdout
        self.assertIn("not counted yet", out.replace("\n", " ").replace("  ", " "))

    def test_vs_native_with_no_reference_says_so_and_prints_no_ratio(self):
        from sampo import cost
        self.rig(ref=False)
        with unittest.mock.patch.object(cost, "NATIVE_REFERENCE", os.path.join(self.tmp, "nowhere.json")):
            with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg}):
                got = cost.vs_native(self.p, 14)
                text = cost.vs_native_text(got)
        self.assertIsNone(got["reference"])
        self.assertIsNone(got["ratio"])
        self.assertIn("no native reference", text)
        self.assertIn("lead", text, "the dollars are still shown")

    def test_vs_native_with_nothing_landed_is_a_sentence_not_a_traceback(self):
        proc = self.fm("cost", "--vs-native", env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "none")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("0 lane(s) landed", proc.stdout)

    def test_the_stored_reference_is_the_one_exam_task_and_marked_unmeasured(self):
        from sampo import cost
        self.rig(ref=False)
        ref = cost.native_reference(self.p)
        self.assertEqual((ref["usd_per_task"], ref["n"], ref["measured"]), (0.93, 1, False))
        self.assertIn("s8-049", ref["source"])


class CpuOrigin(HookCase):
    """T-6006: `sampo cost --cpu` prints the CPU-seconds the test runs of the window cost, by the origin that started
    them (builder, land gate, behind verify, ship), per landed lane; each run.py appends one row to the repo's CPU log."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.cache = os.path.join(self.tmp, "cache")
        self.now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    def common(self):
        out = subprocess.run(["git", "-C", self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             check=True, capture_output=True, text=True).stdout.strip()
        return os.path.realpath(out)

    def log(self, origin, cpu, ago_h=1.0, saved=0.0, reused=0):
        import run
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache}):
            run.log_cpu(self.common(), {"t": round(self.now - ago_h * 3600, 3), "origin": origin, "cpu_s": cpu,
                                        "saved_cpu_s": saved, "reused": reused, "ran": 5, "ok": True})

    def merge(self, subject):
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "--allow-empty", "-m", subject], check=True)

    def cpu(self, *flags):
        return self.fm_json("cost", "--cpu", "--days", "1", *flags, env={"XDG_CACHE_HOME": self.cache})

    def test_cpu_seconds_per_landed_lane_by_origin(self):
        self.merge("Merge T-0001: lane A")
        self.merge("Merge T-0002: lane B")
        for origin, cpu in (("builder", 40.0), ("builder", 20.0), ("land", 30.0), ("verify", 60.0), ("ship", 10.0)):
            self.log(origin, cpu)
        self.log("verify", 999.0, ago_h=48)  # outside the day: not counted
        got = self.cpu()
        self.assertEqual(got["landed"], 2)
        by = got["origins"]
        self.assertEqual({k: v["cpu_s"] for k, v in by.items()}, {"builder": 60.0, "land": 30.0, "verify": 60.0, "ship": 10.0})
        self.assertEqual({k: v["per_lane_cpu_s"] for k, v in by.items()},
                         {"builder": 30.0, "land": 15.0, "verify": 30.0, "ship": 5.0})
        self.assertEqual((got["cpu_s"], got["per_lane_cpu_s"], by["builder"]["runs"]), (160.0, 80.0, 2))

    def test_the_text_names_each_origin_and_what_reuse_saved(self):
        self.merge("Merge T-0001: lane A")
        self.log("land", 12.0, saved=300.0, reused=40)
        text = self.fm("cost", "--cpu", "--days", "1", env={"XDG_CACHE_HOME": self.cache}).stdout
        self.assertIn("per landed lane", text)
        self.assertRegex(text, r"land\s+12\.0 CPU s")
        self.assertIn("reused 40 classes", text)
        self.assertIn("300", text)

    def test_no_log_or_no_landed_lane_is_a_sentence_not_a_traceback(self):
        proc = self.fm("cost", "--cpu", env={"XDG_CACHE_HOME": self.cache})
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("no test run was metered", proc.stdout)

    def test_an_unknown_origin_in_a_row_counts_as_other_and_a_torn_line_is_skipped(self):
        self.merge("Merge T-0001: lane A")
        self.log("martian", 5.0)
        with open(os.path.join(self.cache, "sampo", "run-cache", os.listdir(os.path.join(self.cache, "sampo", "run-cache"))[0]),
                  "a") as f:
            f.write('{"t": 1, "origin"\n')
        got = self.cpu()
        self.assertEqual(got["origins"]["other"]["cpu_s"], 5.0)

    def test_run_command_stamps_the_origin_a_run_py_reads(self):
        from sampo import core as c
        for origin in ("land", "verify", "ship"):
            code, out = c.run_command(self.repo, "echo o=$SAMPO_RUN_ORIGIN", 30, origin=origin)
            self.assertEqual(code, 0)
            self.assertIn(f"o={origin}", out)
        code, out = c.run_command(self.repo, "echo o=[$SAMPO_RUN_ORIGIN]", 30, env={"PATH": os.environ["PATH"]})
        self.assertIn("o=[]", out, "no origin given, none set: the runner calls it a builder's")


if __name__ == "__main__":
    import unittest
    unittest.main()
