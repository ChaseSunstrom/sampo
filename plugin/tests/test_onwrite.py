"""T-0947: validators run on every write and block until fixed. Built-ins (stack cards, JSON, skill and agent
frontmatter, Python) in the hook's own process; the project's own (fm check onwrite GLOB 'CMD {file}') with a 5 s cap.
A failure answers PostToolUse with decision block, in subagents too; a clean write passes silently."""
import glob
import json
import os
import shutil
import subprocess
import time

from test_hooks import HookCase, parse

from sampo import core as c

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OnWrite(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def post(self, path, tool="Write", **extra):
        return self.hook("PostToolUseValidate", dict({"tool_name": tool, "tool_input": {"file_path": path},
                                              "tool_response": {}, "session_id": "s1"}, **extra))

    def blocked(self, proc):
        data = parse(proc) or {}
        return data.get("reason", "") if data.get("decision") == "block" else ""

    def test_a_card_that_fails_the_lint_blocks_and_a_clean_one_passes(self):
        bad = self.write("skills/library/cards/python/broken-card.md", "---\nname: other\n---\nNo sections.\n")
        reason = self.blocked(self.post(bad))
        self.assertIn("broken-card.md", reason)
        self.assertIn("kebab-case and equal the file name", reason)
        src = sorted(glob.glob(os.path.join(PLUGIN, "skills", "library", "cards", "python", "*.md")))[0]
        good = os.path.join(self.repo, "skills", "library", "cards", "python", os.path.basename(src))
        shutil.copy(src, good)
        self.assertEqual(self.post(good, tool="Edit").stdout, "")

    def test_invalid_json_blocks_in_a_subagent_too(self):
        path = self.write("conf.json", '{"a": 1,,}')
        reason = self.blocked(self.post(path, agent_type="sampo:fm-builder", agent_id="a1"))
        self.assertIn("not valid JSON", reason)
        self.assertEqual(self.post(self.write("conf.json", '{"a": 1}')).stdout, "")
        jsonc = self.write("tsconfig.json", '{"compilerOptions": {}, // comments are fine here\n}')
        self.assertEqual(self.post(jsonc).stdout, "")

    def test_python_that_does_not_compile_blocks(self):
        self.assertIn("SyntaxError", self.blocked(self.post(self.write("app.py", "def f(:\n"))))
        # T-2035: a snippet drafted outside the project (it starts indented) isn't code the project runs
        self.assertNotEqual(os.path.relpath(self.tmp, self.repo), ".")
        self.assertEqual(self.post(self.write("../scratch/snippet.py", "    def f(self):\n        pass\n")).stdout, "")
        self.assertIn("not valid JSON", self.blocked(self.post(self.write("../scratch/draft.json", "[{,]"))))

    def test_skill_and_agent_frontmatter_need_name_and_description(self):
        skill = self.write("skills/thing/SKILL.md", "---\nname: thing\n---\nBody.\n")
        self.assertIn("description", self.blocked(self.post(skill)))
        self.write("skills/thing/SKILL.md", "---\nname: thing\ndescription: does the thing\n---\nBody.\n")
        self.assertEqual(self.post(skill).stdout, "")
        os.makedirs(os.path.join(self.repo, ".claude", "agents"), exist_ok=True)
        agent = self.write(".claude/agents/helper.md", "No frontmatter at all.\n")
        self.assertIn("name", self.blocked(self.post(agent)))
        notes = self.write("docs/agents/notes.md", "Just prose about agents.\n")  # not an agent definition
        self.assertEqual(self.post(notes).stdout, "")

    def test_foremans_own_skills_agents_and_json_pass(self):
        from sampo import hooks
        root = os.path.dirname(PLUGIN)
        tracked = subprocess.run(["git", "-C", root, "ls-files", "*.json"], capture_output=True, text=True).stdout
        files = glob.glob(os.path.join(PLUGIN, "**", "SKILL.md"), recursive=True) + \
            glob.glob(os.path.join(PLUGIN, "agents", "*.md")) + [os.path.join(root, f) for f in tracked.split()]
        # tracked files only: state/ holds runtime files (a guard tripfile is empty on purpose), never shipped
        self.assertGreater(len(files), 10)
        bad = [(f, hooks._syntax_note(f) or hooks._frontmatter_problem(f)) for f in files]
        self.assertEqual([b for b in bad if b[1]], [])

    def test_a_block_is_logged_as_an_enforce_event(self):
        self.post(self.write("bad.json", "{"))
        ev = [e for e in c.ledger_tail(self.project(), 50) if e.get("event") == "enforce"]
        self.assertEqual(len(ev), 1, ev)
        self.assertTrue(ev[0]["data"]["file"].endswith("bad.json"))

    def test_project_validator_registered_with_fm_check_onwrite(self):
        self.fm("check", "onwrite", "*.txt", "grep -q ok {file} || { echo line1; echo 'missing ok in' {file}; exit 1; }")
        listed = self.fm_json("check", "onwrite")["onwrite"]
        self.assertIn("*.txt", listed)
        path = self.write("notes/a.txt", "nope\n")
        reason = self.blocked(self.post(path))
        self.assertIn("missing ok in", reason)
        self.assertIn("a.txt", reason)
        self.assertEqual(self.post(self.write("notes/a.txt", "ok\n")).stdout, "")
        self.assertEqual(self.post(self.write("other.md", "nope\n")).stdout, "", "a non-matching write runs nothing")
        self.fm("check", "onwrite", "*.txt")  # no command: removes it
        self.assertEqual(self.fm_json("check", "onwrite")["onwrite"], {})
        self.assertEqual(self.post(self.write("notes/a.txt", "nope\n")).stdout, "")

    def test_a_project_validator_is_capped_at_five_seconds(self):
        self.fm("check", "onwrite", "*.slow", "sleep 30; cat {file}")
        t0 = time.monotonic()
        proc = self.hook("PostToolUseValidate", {"tool_name": "Write", "session_id": "s1", "tool_response": {},
                                         "tool_input": {"file_path": self.write("x.slow", "x")}}, timeout=20)
        self.assertLess(time.monotonic() - t0, 12)  # wall clock: a hook's timeout deadline, in a child
        self.assertEqual(self.blocked(proc), "", "a timeout says nothing about the file: it doesn't block")
        self.assertIn("timed out", self.ctx_of(proc))

    def test_onwrite_needs_file_placeholder(self):
        p = self.fm("check", "onwrite", "*.txt", "true", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("{file}", p.stderr)

    def test_validation_is_its_own_sync_entry_beside_the_catch_all(self):
        # a plain matcher only: the catch-all keeps recording every tool, the sync entry only validates writes
        with open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8") as f:
            groups = json.load(f)["hooks"]["PostToolUse"]
        sync = [g for g in groups if not any(h.get("async") for h in g["hooks"])]
        self.assertEqual([(g["matcher"], g["hooks"][0]["args"]) for g in sync],
                         [("Write|Edit|MultiEdit|NotebookEdit|WebFetch|WebSearch|mcp__.*", ["PostToolUseValidate"])])  # T-0972: outside text too
        rest = [g for g in groups if g not in sync]
        self.assertEqual([(g.get("matcher"), g["hooks"][0]["args"]) for g in rest], [(None, ["PostToolUse"])])

