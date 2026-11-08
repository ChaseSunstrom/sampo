"""T-0071 round D: faster and cheaper — fm task finish, fm quiet, fm gates, thrash notes, fm cost, fm usage, lens
yield, skill size caps."""
import json
import os
import subprocess
import sys

from helpers import FM, ForemanTestCase

from sampo import core as c
from sampo import recall


class Speed(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def test_finish_closes_an_s_task_in_one_call(self):
        self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--step", "do it", "--ac", "flag exists :: test -f flag",
                "--ac", "works", "--focus")
        open(os.path.join(self.repo, "flag"), "w").close()
        p = self.fm("task", "finish", "T-0001", "--run", "true", "--audit", "self checklist on the diff")
        self.assertIn("T-0001 done (verification:", p.stdout)
        b = c.find_brief(self.p, "T-0001")
        self.assertEqual(b.status, "done")
        self.assertTrue(all(a.checked for a in b.acceptance()))
        self.assertIn("`test -f flag`", b.section("Verification evidence"))

    def test_finish_stops_on_a_failing_check_and_keeps_it_recorded(self):
        self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--step", "do it", "--ac", "flag :: test -f flag",
                "--focus")
        p = self.fm("task", "finish", "T-0001", "--run", "true", "--audit", "x", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("ac 1: test -f flag → ✗ exit 1", p.stderr)
        b = c.find_brief(self.p, "T-0001")
        self.assertEqual(b.status, "active")
        self.assertEqual(b.audits(), [], "no self audit over a failing check")
        self.fm("task", "new", "m", "--type", "FEATURE", "--tier", "M")
        # T-0097: M/L close in one call too, naming their audit lenses
        self.assertIn("--lens", self.fm("task", "finish", "T-0002", "--audit", "x", check=False).stderr)

    def test_quiet_prints_one_line_or_the_tail(self):
        p = self.fm("quiet", "--", "bash", "-c", "seq 1 500")
        self.assertEqual(p.stdout.count("\n"), 1)
        self.assertIn("✓ exit 0", p.stdout)
        self.assertIn("500", p.stdout)
        p = self.fm("quiet", "--tail", "3", "--", "bash", "-c", "seq 1 500; exit 4", check=False)
        self.assertEqual(p.returncode, 4)
        self.assertEqual(p.stdout.splitlines()[:3], ["498", "499", "500"])

    def test_quiet_feeds_a_heredoc_to_its_command_and_never_waits_on_an_idle_pipe(self):  # T-1766
        p = self.fm("quiet", "--", sys.executable, "-", input="print('ran from stdin')\n")
        self.assertIn("ran from stdin", p.stdout)
        e = dict(os.environ, FOREMAN_HOME=self.home)
        e.pop("CLAUDE_CODE_SESSION_ID", None)
        idle = subprocess.Popen([sys.executable, FM, "quiet", "--", "cat"], cwd=self.repo, env=e, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:  # its stdin stays open and empty, as an agent's may: cat must get /dev/null and end
            idle.wait(timeout=20)
        finally:
            idle.stdin.close()
            out = idle.stdout.read()
            idle.wait(10)
        self.assertIn("✓ exit 0", out)

    def test_gates_list_what_done_needs(self):
        out = self.fm("gates", "fix", "m").stdout
        for want in ("evidence", "intent + adversary or edge or maintainer or operator", "docs impact", "lesson",
                     "sampo task prove", "scope", "why-not-caught"):  # T-1977: fm task finish refuses an M fix without it
            self.assertIn(want, out)
        self.assertNotIn("why-not-caught", self.fm("gates", "fix", "s").stdout)
        self.assertIn("none when it edits no project file", self.fm("gates", "clean", "s").stdout)  # T-1982: a ship task
        self.fm("task", "new", "t", "--type", "CLEAN", "--tier", "S", "--step", "s", "--ac", "a")
        self.assertIn("Done needs: evidence, audits, behaviour lock, scope", self.fm("focus", "T-0001").stdout)

    def test_repeated_edits_without_a_check_get_one_thrash_note(self):
        self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--step", "s", "--ac", "a", "--focus")
        path = os.path.join(self.repo, "a.py")

        def edit():
            r = self.hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": path}})
            return r.stdout
        notes = [edit() for _ in range(7)]
        self.assertEqual(sum("edited 6 times without a check" in n for n in notes), 1, notes)
        self.assertNotIn("Foreman", notes[4])

    def test_the_same_failure_three_times_says_stop_and_diagnose(self):
        notes = [recall.note_failure(self.p, "T-0009", "E: KeyError: 'user'") for _ in range(4)]
        self.assertIsNone(notes[1])
        self.assertIn("come up 3 times in T-0009", notes[2])
        self.assertIsNone(notes[3])

    def test_cost_reads_the_transcripts(self):
        folder = os.path.join(self.tmp, "claude", "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        usage = {"input_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 10000,
                 "output_tokens": 200}
        rows = [{"type": "assistant", "timestamp": c.now(), "sessionId": "abcdef123",
                 "message": {"id": "m1", "usage": usage, "content": [{"type": "tool_use", "id": "t1", "name": "Read"}]}},
                {"type": "assistant", "timestamp": c.now(), "sessionId": "abcdef123",  # same message, second block
                 "message": {"id": "m1", "usage": usage, "content": [{"type": "text", "text": "x"}]}},
                {"type": "user", "timestamp": c.now(), "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "y" * 5000}]}},
                {"type": "assistant", "timestamp": c.now(), "sessionId": "abcdef123", "message": {"id": "m2", "content": [
                    {"type": "tool_use", "id": "t2", "name": "Shot"}]}},
                {"type": "user", "timestamp": c.now(), "message": {"content": [  # T-0194: a screenshot
                    {"type": "tool_result", "tool_use_id": "t2", "content": [
                        {"type": "image", "source": {"type": "base64", "data": "A" * 400000}}]}]}}]
        with open(os.path.join(folder, "abcdef123.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        data = self.fm_json("cost", env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "claude")})
        self.assertEqual(data["messages"], 1, "a message's usage counts once")
        self.assertEqual(data["tokens"]["input_tokens"], 1000)
        self.assertEqual(data["input_equivalent"], 1000 + 1000 + 1000)
        self.assertIn("Read", data["tool_result_chars"])
        self.assertLess(data["tool_result_chars"]["Shot"], 10000, "an image costs its tokens, not its base64 length")

    def test_cost_says_where_the_time_went(self):
        # T-0188 (brainstorm round 4, the user's 'faster'): the model's time against each tool's, a shell command by
        # what it ran; time waiting on the person and gaps over 10 minutes left out
        folder = os.path.join(self.tmp, "claude", "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        t0 = c.time.time() - 3600
        at = lambda s: c.iso(t0 + s)
        usage = {"input_tokens": 1, "output_tokens": 1}
        rows = [{"type": "user", "timestamp": at(0), "message": {"content": "go"}},  # the person's prompt
                {"type": "assistant", "timestamp": at(5), "sessionId": "s1", "message": {"id": "m1", "usage": usage,
                 "content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "cd /x && fm check"}}]}},
                {"type": "user", "timestamp": at(25), "message": {"content": [{"type": "tool_result", "tool_use_id": "b1"}]}},
                {"type": "assistant", "timestamp": at(30), "sessionId": "s1", "message": {"id": "m2", "usage": usage,
                 "content": [{"type": "text", "text": "done"}]}},
                {"type": "user", "timestamp": at(3000), "message": {"content": "next"}},  # an hour away: not counted
                {"type": "assistant", "timestamp": at(3004), "sessionId": "s1", "message": {"id": "m3", "usage": usage,
                 "content": [{"type": "text", "text": "ok"}]}}]
        with open(os.path.join(folder, "s1.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        r = self.fm("cost", env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "claude")})
        data = self.fm_json("cost", env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "claude")})
        self.assertEqual(data["time"]["model_s"], 14)  # 5 + 5 + 4
        self.assertEqual(data["time"]["tools_s"], {"Bash": 20})
        self.assertEqual(data["time"]["shell_s"], {"fm check": 20})
        self.assertIn("Time", r.stdout)
        self.assertIn("fm check", r.stdout)

    def test_usage_counts_skills_and_lists_the_unused(self):
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for tool, target in (("Skill", "sampo:intake"), ("Bash", "fm task new x"), ("Bash", "fm check")):
                f.write(json.dumps({"ts": c.now(), "kind": "tool", "tool": tool, "target": target,
                                    "project": self.p.slug}) + "\n")
        data = self.fm_json("usage")
        self.assertEqual(data["skills"], {"intake": 1})
        self.assertEqual(data["commands"], {"task new": 1, "check": 1})
        self.assertIn("tidy", data["unused"]["skills"])
        self.assertNotIn("intake", data["unused"]["skills"])
