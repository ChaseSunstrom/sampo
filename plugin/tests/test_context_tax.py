"""T-1258: the context tax ratchet. A scripted session (start, a prompt, edits across three tasks, test runs, a
compaction, stop) is replayed through the hooks, and what each event injects is held under a byte budget, read from
the inject events the hooks log (T-0499). A one-line note isn't said twice while it's still in the context."""
import json
import os

from test_edit_cards import _CardCase, card

from sampo import core as c

# bytes each hook event may inject over the whole replay: measured, then about a quarter of headroom
# (2026-10-20: SessionStart 814, UserPromptSubmit 397 under its own 400 cap, PreToolUse 1538, PostToolUse 0;
# 2026-10-22, T-1697's skill hint once a stage instead of the procedure on every step: SessionStart 852, UserPromptSubmit 321)
BUDGET = {"SessionStart": 1000, "UserPromptSubmit": 400, "PreToolUse": 1900, "PostToolUse": 200}
POINTER = "its notes were shown earlier this session"


class ContextTax(_CardCase):
    def setUp(self):
        super().setUp()
        # a set-up install: without ~/.claude/rules/foreman.md every start adds a one-time setup note (CI's bare HOME)
        home = os.path.join(self.tmp, "userhome")
        os.makedirs(os.path.join(home, ".claude", "rules"))
        open(os.path.join(home, ".claude", "rules", "foreman.md"), "w").close()
        self.addCleanup(os.environ.__setitem__, "HOME", os.environ["HOME"])
        os.environ["HOME"] = home
        self.long_paths = 0

    def injects(self):
        path = os.path.join(c.state_dir(), "events.jsonl")
        with open(path, encoding="utf-8") as f:
            return [e for e in map(json.loads, f) if e.get("kind") == "inject"]

    def ran_tests(self):
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "python3 -m unittest"},
                                  "tool_response": {"stdout": "Ran 3 tests in 0.1s\n\nOK", "stderr": ""}})

    def start(self, source):
        """T-1284: a SessionStart, counting what its notes owe to this machine's temp folder: the project and brief
        paths are measured as if under Linux's /tmp/tmpXXXXXXXX, so macOS's /private/var/folders/… costs nothing."""
        out = self.hook("SessionStart", {"source": source}).stdout
        text = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
        real = os.path.realpath(self.tmp)  # macOS writes either form: /var/folders/… or /private/var/folders/…
        n_real = text.count(real) if real != self.tmp else 0
        n_raw = text.count(self.tmp) - (n_real if self.tmp in real else 0)
        self.long_paths += n_raw * (len(self.tmp) - 16) + n_real * (len(real) - 16)  # 16: len("/tmp/tmpXXXXXXXX")

    def new_task(self, title):
        self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")

    def test_a_replayed_session_stays_under_its_budget_and_says_a_one_liner_once(self):
        self.map([card("python", "lang", 5, "ext:.py")])
        self.start("startup")
        self.hook("UserPromptSubmit", {"prompt": "make the parser accept tabs"})
        notes = [self.edit("app.py") for _ in range(3)]
        self.ran_tests()
        self.new_task("Second")
        notes += [self.edit("app.py") for _ in range(3)]
        self.new_task("Third")
        notes += [self.edit("app.py") for _ in range(2)]
        self.assertEqual(sum(POINTER in n for n in notes), 0, "T-3078: a card's notes come once per session, no pointer")
        self.start("compact")
        self.new_task("Fourth")
        self.assertNotIn(POINTER, self.edit("app.py"))
        self.ran_tests()
        self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Done."})
        used = {"SessionStart": -self.long_paths}
        for e in self.injects():
            used[e["event"]] = used.get(e["event"], 0) + e["chars"]
        for event, cap in BUDGET.items():
            self.assertLessEqual(used.get(event, 0), cap, f"{event} injected {used.get(event, 0)} bytes: {used}")


if __name__ == "__main__":
    import unittest
    unittest.main()
