"""T-5592: the lead's nav-batching nudge. After three turns in a row that each made one read (Read, Grep, Glob, or a Bash
cat, sed -n, ls, head, tail, grep, rg, find), the PostToolUse hook says once, in the lead's context, to batch
independent reads in one message; the note is quiet for the next 40 calls. Parallel reads, edits, other Bash, fewer than
three turns, a subagent's call and an unreadable transcript never note. Subagents get the batching line in their agent
files instead: a subagent's hook note would land in the main thread's context."""
import json
import os

from helpers import read_text
from test_hooks import HookCase

HEAD = "each made one read"  # the note's first words (hooks._nav_note)
AGENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agents")


class NavNudge(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.transcripts = 0

    def transcript(self, *turns):
        """A transcript: the user's prompt, then one assistant message per turn. A turn is a list of (tool, input)
        calls that share one message id, as parallel calls do; each call's result follows it."""
        self.transcripts += 1
        path = os.path.join(self.tmp, f"nav{self.transcripts}.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "find the parser"}}) + "\n")
            n = 0
            for t, calls in enumerate(turns):
                for tool, inp in calls:
                    n += 1
                    f.write(json.dumps({"type": "assistant", "message": {"id": f"msg{t}", "content": [
                        {"type": "tool_use", "id": f"u{n}", "name": tool, "input": inp}]}}) + "\n")
                    f.write(json.dumps({"type": "user", "message": {"content": [
                        {"type": "tool_result", "tool_use_id": f"u{n}", "content": "ok"}]}}) + "\n")
        return path

    def post(self, transcript, tool, ti, sid="sess-nav", agent=False):
        """The hook's additionalContext for one PostToolUse call (the call is the transcript's last)."""
        pl = {"tool_name": tool, "tool_input": ti, "tool_response": "", "session_id": sid, "transcript_path": transcript}
        if agent:
            pl["agent_id"] = "agent-1"
        proc = self.hook("PostToolUse", pl)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.ctx_of(proc)

    def test_three_single_read_turns_note_once(self):
        t = self.transcript([("Read", {"file_path": "a.py"})], [("Grep", {"pattern": "parse"})],
                            [("Read", {"file_path": "b.py"})])
        first = self.post(t, "Read", {"file_path": "b.py"})
        self.assertIn(HEAD, first)
        self.assertIn("batch", first)
        quiet = [self.post(t, "Read", {"file_path": "b.py"}) for _ in range(40)]
        self.assertFalse([x for x in quiet if HEAD in x], "a note inside the 40-call window")
        self.assertIn(HEAD, self.post(t, "Read", {"file_path": "b.py"}))  # the window is over: once more

    def test_unchanged_transcript_size_never_reopens_it(self):
        # T-5592 review: a read call whose transcript is the same size as at the last check is not parsed again. The
        # rewrite keeps the size (Read and Edit are both four letters) and makes three single-read turns, so a parse
        # here would note; the skip leaves it quiet.
        t = self.transcript([("Read", {"file_path": "a.py"})], [("Edit", {"file_path": "a.py"})],
                            [("Read", {"file_path": "c.py"})])
        self.assertNotIn(HEAD, self.post(t, "Read", {"file_path": "c.py"}, sid="sess-size"))
        with open(t, encoding="utf-8") as f:
            text = f.read()
        with open(t, "w", encoding="utf-8") as f:
            f.write(text.replace('"name": "Edit"', '"name": "Read"'))
        self.assertEqual(os.path.getsize(t), len(text.encode("utf-8")))
        self.assertNotIn(HEAD, self.post(t, "Read", {"file_path": "c.py"}, sid="sess-size"))

    def test_bash_reads_count_as_reads(self):
        t = self.transcript([("Bash", {"command": "ls -la"})], [("Bash", {"command": "sed -n 1,20p a.py"})],
                            [("Bash", {"command": "rg parse"})])
        self.assertIn(HEAD, self.post(t, "Bash", {"command": "cat b.py"}, sid="sess-bash"))

    def test_batches_edits_and_subagents_never_note(self):
        a, c = {"file_path": "a.py"}, {"file_path": "c.py"}
        cases = {
            "parallel": ([[("Read", a)], [("Read", a), ("Grep", {"pattern": "x"})], [("Read", c)]], False),
            "edit": ([[("Read", a)], [("Edit", a)], [("Read", c)]], False),
            "bash": ([[("Read", a)], [("Bash", {"command": "pytest -q"})], [("Read", c)]], False),
            "bash-write": ([[("Read", a)], [("Bash", {"command": "cat a > b"})], [("Read", c)]], False),
            "sed-i": ([[("Read", a)], [("Bash", {"command": "sed -i s/a/b/ a.py"})], [("Read", c)]], False),
            "find-exec": ([[("Read", a)], [("Bash", {"command": "find . -exec rm {} ;"})], [("Read", c)]], False),
            "short": ([[("Read", a)], [("Read", c)]], False),
            "subagent": ([[("Read", a)], [("Read", a)], [("Read", c)]], True),
        }
        for name, (turns, agent) in cases.items():
            with self.subTest(name):
                t = self.transcript(*turns)
                self.assertNotIn(HEAD, self.post(t, "Read", c, sid=f"sess-{name}", agent=agent))
        for name, path in (("missing", os.path.join(self.tmp, "missing.jsonl")),
                           ("garbage", os.path.join(self.tmp, "garbage.jsonl")), ("int", 3)):
            with self.subTest(name):
                if name == "garbage":
                    with open(path, "w") as f:
                        f.write("{not json\n[]\n")
                self.assertNotIn(HEAD, self.post(path, "Read", c, sid=f"sess-{name}"))
        self.assertNotIn("Traceback", self.hooks_log())


class NavNudgeAgents(HookCase):
    def test_agent_files_carry_the_batching_line(self):
        for name in ("fm-builder.md", "fm-reviewer.md"):
            lines = [x for x in read_text(os.path.join(AGENTS, name)).splitlines() if "batch independent reads" in x]
            self.assertEqual(len(lines), 1, f"{name}: one batching line")
