"""T-0125: the self-improvement loop. fm friction digests Foreman's own friction since the last pass; fm next calls for a
pass every N closed tasks; a marked pass starts the next digest after it, which then reports what became of its items."""
import json
import os
import unittest

from helpers import ForemanTestCase

first = lambda text: json.JSONDecoder().raw_decode(text)[0]  # --focus prints its own lines after the JSON


class Friction(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def close_one(self, n):
        tid = first(self.fm("task", "new", f"tidy {n}", "--type", "CLEAN", "--tier", "S", "--ac", "ok :: true", "--step", "a", "--focus",
                                 "--json").stdout)["id"]
        self.fm("task", "finish", tid, "--run", "true", "--audit", "self check")
        return tid

    def test_the_digest_names_each_kind_of_friction_and_a_pass_starts_the_next_after_it(self):
        self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "npm publish"}})  # a guard block
        tid = first(self.fm("task", "new", "Fix", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "a", "--focus",
                                 "--json").stdout)["id"]
        self.fm("task", "log", tid, "steer: make it faster")
        self.fm("capture", "Gates by path", "--source", "self")
        out = self.fm("friction").stdout
        for needle in ("publish", "steer: make it faster", "Gates by path"):
            self.assertIn(needle, out)
        self.assertIn("e.g. `npm publish`", out)  # T-0172: the command itself, so a false block can be grounded
        path = self.fm("friction", "--brief").stdout.strip().split()[-1]
        with open(path, encoding="utf-8") as f:
            brief = f.read()
        self.assertIn("publish", brief)  # self-contained: the digest is in it
        self.assertIn("at most 5", brief)
        self.fm("friction", "--mark")
        out = self.fm("friction").stdout
        self.assertNotIn("steer: make it faster", out)  # the next pass starts after this one
        self.assertIn("Gates by path", out)  # and reports what became of the last pass's self items (open)

    def test_classes_group_todays_refusals_with_a_count_and_whether_they_repeated(self):  # T-2992
        from sampo import core as c
        p = c.find_project(self.repo)
        for _ in range(2):
            self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "npm publish"}})
        for _ in range(3):
            c.log_event(p, "self_change_refused", task="T-1", data={"sha": "a", "rows": []})
        c.log_event(p, "enforce", task="T-1", data={"kind": "merge_refused", "reason": "guard: git push"})
        c.log_event(p, "first_brief_close_failed", task="T-2", data={"cmd": "x", "tail": "y"})
        c.log_event(p, "task_done", task="T-3")  # not a refusal
        got = {r["class"]: r for r in first(self.fm("friction", "--classes", "--json").stdout)["classes"]}
        self.assertEqual(got["self-change hold"]["count"], 3)
        self.assertTrue(got["self-change hold"]["repeated"])
        self.assertEqual(got["merge refused: guard: git push"]["count"], 1)
        self.assertFalse(got["merge refused: guard: git push"]["repeated"])
        self.assertIn("close failed", got)
        self.assertEqual(sum(1 for k in got if k.startswith("guard block")), 1)
        self.assertEqual(next(v for k, v in got.items() if k.startswith("guard block"))["count"], 2)
        self.assertIn("×3 (repeated)", self.fm("friction", "--classes").stdout)

    def test_failed_calls_say_why_and_a_background_wait_isnt_a_stop(self):
        # T-0301: five failed Reads of different scratch files read as one cause, and a wait isn't friction
        for i in range(3):
            self.hook("PostToolUseFailure", {"tool_name": "Read", "tool_input": {"file_path": f"/tmp/scratch{i}.md"},
                                             "error": "File does not exist."})
        self.fm("task", "new", "Fix login", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "a", "--focus")
        self.hook("SubagentStart", {"agent_id": "a1", "agent_type": "sampo:fm-reviewer", "session_id": "sess-1"})
        self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Waiting for the audit.",
                           "session_id": "sess-1"})
        out = self.fm("friction").stdout
        self.assertIn("3× Read: File does not exist.", out)
        self.assertNotIn("wait on background work", out)

    def test_failed_bash_calls_split_by_program_and_a_grep_no_match_isnt_friction(self):  # T-2733
        for cmd in ("cd /x && fm tsk new a", "fm tsk new b", "python3 -c 'raise SystemExit(1)'", "grep -q foo bar.txt"):
            self.hook("PostToolUseFailure", {"tool_name": "Bash", "tool_input": {"command": cmd}, "error": "Exit code 1"})
        out = self.fm("friction").stdout
        self.assertIn("2× Bash fm: Exit code 1", out)
        self.assertIn("1× Bash python3: Exit code 1", out)
        self.assertNotIn("grep -q foo", out)  # exit 1 is grep's 'no match', not a failure

    def test_digest_labels_downstream_project_friction(self):
        # T-0435: _ledgers read every project's ledger but dropped which one a line came from: JARVIS's steers and
        # corrections reached Foreman's pass unlabelled, and how much friction each project had wasn't visible
        from helpers import git_repo
        other, quiet = git_repo(self.tmp, "jarvis"), git_repo(self.tmp, "private")
        for root in (other, quiet):
            self.fm("init", cwd=root)
            tid = first(self.fm("task", "new", "Work", "--type", "FIX", "--tier", "S", "--step", "a", "--json", cwd=root).stdout)["id"]
            self.fm("task", "log", tid, "steer: the panels must load before anything else", cwd=root)
        self.fm("sensitive", "on", cwd=quiet)
        out = self.fm("friction").stdout
        self.assertIn("[jarvis-", out)  # the steer, labelled with its project
        self.assertIn("the panels must load before anything else", out)
        self.assertIn("other projects", out)
        self.assertNotIn("private-", out, "a sensitive project's ledger stays out")

    def test_friction_hook_errors(self):
        # T-0437: hooks never fail a tool call; the errors they swallow went only to hooks.log, where a pass never looked
        import time
        from sampo import core as c
        log = os.path.join(c.state_dir(), "logs", "hooks.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(log, "w") as f:
            for event in ("PreToolUse", "PreToolUse", "Stop"):
                f.write(f"{now} {event} Traceback (most recent call last):\n  File \"/a/fmcore.py\", line 9, in x\n"
                        f"  File \"/a/plugin/lib/fmhooks.py\", line 1474, in run\nNameError: boom\n")
            f.write(f"{now} Stop paused for 10 min after 3 failures in a row\n")  # the breaker's note isn't an error
        out = self.fm("friction").stdout
        self.assertIn("3 in the last 24 h, most in PreToolUse (2)", out)
        self.assertIn("NameError: boom (fmhooks.py:1474, state/logs/hooks.log)", out)  # T-2107: the innermost frame

    def test_slow_gate_ignores_reused_passes(self):
        # T-0424: a reused pass records 0.0 s; counting it dragged the median to 0 ("12s vs usual 0s")
        from sampo import core as c
        p = c.find_project(self.repo)
        with open(os.path.join(p.dir, "ledger.jsonl"), "a") as f:
            for gate, times in (("fm replay", [10, 0.0, 0.0, 0.0, 11, 12]), ("pytest", [10, 0.0, 0.0, 0.0, 10, 30])):
                for s in times:
                    f.write(json.dumps({"ts": c.now(), "event": "check_run",
                                        "data": {"results": [{"cmd": gate, "exit": 0, "s": s}]}}) + "\n")
        out = self.fm("friction").stdout
        self.assertNotIn("fm replay", out)  # 12 s against its real runs' 11 s isn't slow
        self.assertIn("pytest: 30s vs usual 10s", out)

    def _runs(self, rows):
        from sampo import core as c
        p = c.find_project(self.repo)
        with open(os.path.join(p.dir, "ledger.jsonl"), "a") as f:
            for results, load in rows:  # T-2763
                data = {"results": [{"cmd": g, "exit": 0, "s": s} for g, s in results], **({"load": load} if load is not None else {})}
                f.write(json.dumps({"ts": c.now(), "event": "check_run", "data": data}) + "\n")

    def test_gates_slow_together_or_under_load_are_one_machine_line(self):
        usual = [(("doctor", 10), ("roundtrip", 10), ("bench", 10), ("lone", 10))] * 3
        self._runs([(r, 0.2) for r in usual] + [((("doctor", 40), ("roundtrip", 50), ("bench", 60), ("lone", 10)), 2.5)])
        out = self.fm("friction").stdout
        self.assertIn("3 gates slow together under load (doctor, roundtrip, bench)", out)
        self.assertNotIn("doctor: 40s", out)

    def test_a_lone_slow_gate_on_a_quiet_machine_stays_a_slow_gate_line(self):
        usual = [(("doctor", 10), ("lone", 10))] * 3
        self._runs([(r, 0.2) for r in usual] + [((("doctor", 40), ("lone", 10)), 0.2)])
        out = self.fm("friction").stdout
        self.assertIn("doctor: 40s vs usual 10s", out)
        self.assertNotIn("slow together", out)

    def test_fm_next_calls_for_a_pass_every_n_closed_tasks_at_a_task_boundary(self):
        nxt = lambda: self.fm("next").stdout
        self.close_one(1)
        self.close_one(2)
        self.assertNotIn("self-improvement pass", nxt(), "off unless the project asks for it")
        self.fm("friction", "--every", "2")
        self.assertIn("self-improvement pass", nxt())
        first(self.fm("task", "new", "Busy", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "a", "--focus",
                           "--json").stdout)
        self.assertNotIn("self-improvement pass", nxt(), "never in the middle of a task")

    def test_a_pass_waits_two_hours_after_the_last_so_it_has_friction_to_read(self):
        # T-0154: pass 2 came due 13 minutes after pass 1 with one new friction line
        from sampo import core as c
        nxt = lambda: self.fm("next").stdout
        self.fm("friction", "--every", "1")
        self.fm("friction", "--mark")
        self.close_one(1)
        self.assertNotIn("self-improvement pass", nxt())
        p = c.find_project(self.repo)
        meta = c.read_meta(p)
        meta["rsi_at"] = c.iso(c.time.time() - 3 * 3600)
        c.write_meta(p, meta)
        self.assertIn("self-improvement pass", nxt())

    def test_a_pass_whose_brief_is_out_is_not_due_again(self):
        # T-0858: the Stop hook kept saying "pass due" while the pass's recon was running
        nxt = lambda: self.fm("next").stdout
        self.fm("friction", "--every", "1")
        self.close_one(1)
        self.assertIn("self-improvement pass", nxt())
        self.fm("friction", "--brief")
        self.assertNotIn("self-improvement pass", nxt(), "in progress: its brief was just written")
        self.fm("friction", "--mark")
        self.assertNotIn("self-improvement pass", nxt())

    def test_unknown_fm_words_rank_by_the_sessions_that_reached_for_them(self):  # T-2382
        env = lambda s: {"CLAUDE_CODE_SESSION_ID": s}  # noqa: E731
        for s in ("s1", "s1", "s2"):
            self.assertEqual(self.fm("next", "--last", env=env(s), check=False).returncode, 2)
        self.fm("bogus", env=env("s3"), check=False)
        self.fm("T-0007", env=env("s3"), check=False)  # a task id typed as a verb: a typo, not a vote
        out = self.fm("friction").stdout
        self.assertIn("2 sessions: fm next --last", out)
        self.assertIn("1 session: fm bogus", out)
        self.assertNotIn("T-0007", out.split("unknown fm verbs")[1])
        self.assertLess(out.index("fm next --last"), out.index("fm bogus"))

    def test_a_bare_keep_going_prompt_is_a_stall(self):
        # T-0887: "continue" after Claude stopped says it stopped too early; what the Stop hook did just before says why
        from sampo import core as c
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            f.write(json.dumps({"ts": c.now(), "kind": "drive_drained", "session_id": "sess-1"}) + "\n")
        for prompt in ("continue", "Keep going!", "go on please", "continue the refactor but skip the tests"):
            self.hook("UserPromptSubmit", {"prompt": prompt})
        out = self.fm("friction").stdout
        self.assertIn("3 keep-going prompt(s)", out)
        self.assertIn("after drive_drained", out)

    def test_distinct_notes_of_one_kind_are_not_repeats(self):
        # T-0860: eight tripwire notes, each a different task's lesson on a different file, read as one note "8x"
        from sampo import core as c
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for i in range(8):
                f.write(json.dumps({"ts": c.now(), "kind": "inject", "event": "PreToolUse",
                                    "key": "done also changed this file", "h": f"h{i}", "chars": 120}) + "\n")
            for _ in range(3):
                f.write(json.dumps({"ts": c.now(), "kind": "inject", "event": "PreToolUse", "key": "same note",
                                    "h": "same", "chars": 80}) + "\n")
        out = self.fm("friction").stdout
        repeated = out.split("notes repeated 3+ times", 1)[1].split("\n\n", 1)[0]
        self.assertIn("same note", repeated)
        self.assertNotIn("done also changed this file", repeated)

    def test_a_note_averaging_over_1500_chars_is_named_even_every_turn(self):
        # T-2109: the repeat line skips SessionStart/UserPromptSubmit, so the biggest per-turn note was never flagged
        from sampo import core as c
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for key, chars in (("session map", 1900), ("prompt hint", 300)):
                for i in range(4):
                    f.write(json.dumps({"ts": c.now(), "kind": "inject", "event": "SessionStart", "key": key,
                                        "h": f"h{i}", "chars": chars}) + "\n")
        out = self.fm("friction").stdout
        big = out.split("notes averaging over 1,500 characters", 1)[1].split("\n\n", 1)[0]
        self.assertIn("SessionStart: session map — 1,900 chars avg × 4", big)
        self.assertNotIn("prompt hint", big)

    def test_a_deliberate_add_on_is_left_out_of_the_note_budget(self):  # T-2589: the compact recap, the repo note
        from sampo import core as c
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for i in range(3):
                f.write(json.dumps({"ts": c.now(), "kind": "inject", "event": "SessionStart", "key": "project state",
                                    "h": f"h{i}", "chars": 9000, "addon": 8000}) + "\n")
        out = self.fm("friction").stdout
        self.assertNotIn("project state — 9,000", out)
        self.assertNotIn("notes averaging over 1,500 characters per injection (trim them: T-2109)\n- SessionStart: project", out)

    def test_guard_example_drops_heredoc_bodies_and_adds_the_rules_hint(self):
        # T-2280: the first 160 chars cut a heredoc before the line the guard judged
        from sampo import core as c
        from sampo import hooks
        claude = os.path.join(self.tmp, "claude")
        os.makedirs(os.path.join(claude, "projects", "x"))
        body = "\n".join(f"print({i})  # padding padding padding" for i in range(12))
        cmd = f"cat > /tmp/t.py <<'EOF'\n{body}\nEOF\npython3 $D/run.py"
        with open(os.path.join(claude, "projects", "x", "s2.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "cwd": self.repo, "sessionId": "s2", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}) + "\n")
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            f.write(json.dumps({"ts": c.iso(c.time.time() - 60), "kind": "guard_block", "session_id": "s2",
                                "category": "state-direct", "tool": "Bash", "rule": "x",
                                "target": "$D/run.py (not known before it runs)",
                                "cmd": hooks._window(c.redact(cmd.replace("\n", " ")), "$D/run.py")}) + "\n")  # as the hook logs it
            edit = "fm edit <<'EOF'\nSENTINEL-BODY\nEOF"
            f.write(json.dumps({"ts": c.iso(c.time.time() - 30), "kind": "guard_block", "session_id": "s2",
                                "category": "core", "tool": "Bash", "target": "edit-target",
                                "cmd": edit.replace("\n", " ")}) + "\n")
        with open(os.path.join(claude, "projects", "x", "s2.jsonl"), "a") as f:
            f.write(json.dumps({"type": "assistant", "cwd": self.repo, "sessionId": "s2", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": edit}}]}}) + "\n")
        out = self.fm("friction", env={"CLAUDE_CONFIG_DIR": claude}).stdout
        self.assertIn("python3 $D/run.py", out)  # T-2296 review: found though the target sits past char 160
        self.assertNotIn("print(3)", out)
        self.assertNotIn("name it literally", out)  # today's guard allows it: no hint for a block already fixed
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:  # no transcript: unrechecked, the hint stays
            f.write(json.dumps({"ts": c.iso(c.time.time() - 20), "kind": "guard_block", "session_id": "s3",
                                "category": "state-direct", "tool": "Bash", "target": "$Z/q (not known before it runs)",
                                "cmd": "python3 $Z/q"}) + "\n")
        out = self.fm("friction", env={"CLAUDE_CONFIG_DIR": claude}).stdout
        self.assertIn("name it literally", out)  # the rule's hint
        self.assertIn("SENTINEL-BODY", out)  # a fm edit body is the judged target: kept

    def test_give_up_lines_say_why_each_task_blocked(self):
        # T-2295: the line named task ids only
        from sampo import core as c
        p = c.find_project(self.repo)
        for tid, why in (("T-0006", "can't reproduce"), ("T-0007", "needs the prod key")):
            c.log_event(p, "focus", task=tid)
            c.log_event(p, "task_block", task=tid, data={"reason": why})
        out = self.fm("friction").stdout
        self.assertIn("T-0006 (can't reproduce)", out)
        self.assertIn("T-0007 (needs the prod key)", out)

    def test_the_digest_rechecks_guard_blocks_and_shows_the_trend(self):
        # T-0187: 'done since: T-0178' said nothing of whether the fix removed its friction. Each guard block is re-run
        # through today's guard (its full command from the session's transcript); a mark keeps the window's counts
        from sampo import core as c
        claude = os.path.join(self.tmp, "claude")
        os.makedirs(os.path.join(claude, "projects", "x"))
        cmds = {"ls -la\npwd": "state-direct", "npm publish": "publish"}  # a block a later fix cleared; a real one
        with open(os.path.join(claude, "projects", "x", "s1.jsonl"), "w") as f:
            for cmd in cmds:
                f.write(json.dumps({"type": "assistant", "cwd": self.repo, "sessionId": "s1", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}) + "\n")
        at = lambda s: c.iso(c.time.time() + s)
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for cmd, cat in cmds.items():
                f.write(json.dumps({"ts": at(-60), "kind": "guard_block", "session_id": "s1", "category": cat,
                                    "tool": "Bash", "target": cmd, "cmd": cmd.replace("\n", " ")}) + "\n")  # as logged
            for i in range(10):
                f.write(json.dumps({"ts": at(-60), "kind": "tool", "tool": "Bash", "target": "x"}) + "\n")
        env = {"CLAUDE_CONFIG_DIR": claude}
        out = self.fm("friction", env=env).stdout
        self.assertIn("e.g. `ls -la pwd` — today's guard allows it", out)
        self.assertRegex(out, r"publish: npm publish .*still blocks")
        self.fm("friction", "--mark", env=env)
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for i in range(10):
                f.write(json.dumps({"ts": at(5), "kind": "tool", "tool": "Bash", "target": "x"}) + "\n")
        out = self.fm("friction", env=env).stdout
        self.assertIn("guard blocks 20 → 0", out)  # per 100 tool calls, the last pass's window against this one

    def test_an_edit_body_stays_in_the_example_under_either_spelling(self):  # T-2845
        from sampo import friction
        for word in ("fm", "sampo"):
            shown = friction._example({"cmd": f"{word} edit f.py <<'EOF'\nkept body\nEOF"}, {})[0]
            self.assertIn("kept body", shown, word)
        self.assertNotIn("dropped body", friction._example({"cmd": "cat f <<'EOF'\ndropped body\nEOF"}, {})[0])


if __name__ == "__main__":
    unittest.main()
