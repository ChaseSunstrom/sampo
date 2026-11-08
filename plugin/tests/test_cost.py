"""T-1257: fm cost says how much of what entered the context was Foreman's own: its hook notes, fm commands' output,
its rules, skill and agent listing lines and output style, read from the transcripts Claude Code keeps."""
import datetime
import json
import os
import unittest

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


if __name__ == "__main__":
    import unittest
    unittest.main()
