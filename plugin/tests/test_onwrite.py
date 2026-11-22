"""T-0947: validators run on every write and block until fixed. Built-ins (stack cards, JSON, skill and agent
frontmatter, Python) in the hook's own process; the project's own (fm check onwrite GLOB 'CMD {file}') with a 5 s cap.
A failure answers PostToolUse with decision block, in subagents too; a clean write passes silently."""
import glob
import json
import os
import shutil
import subprocess
import time
import unittest

from helpers import read_text
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

    def rustfmt_stub(self, fmt):
        """T-5600: a `rustfmt` of its own, first on PATH. Each call logs its arguments as one line of self.tmp/rustfmt.log
        (a new stub starts a new log), then runs the shell line `fmt`, which reads the source on stdin and writes the
        formatted text to stdout, as `rustfmt --emit stdout` does. Needs no toolchain. Returns the env that puts it first."""
        bindir = os.path.join(self.tmp, "rustbin")
        os.makedirs(bindir, exist_ok=True)
        log = os.path.join(self.tmp, "rustfmt.log")
        if os.path.exists(log):
            os.remove(log)
        stub = os.path.join(bindir, "rustfmt")
        with open(stub, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho \"$*\" >> %s\n%s\n" % (log, fmt))
        os.chmod(stub, 0o755)
        return {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}

    def rustfmt_log(self):
        path = os.path.join(self.tmp, "rustfmt.log")
        return read_text(path).splitlines() if os.path.exists(path) else []

    def rustfmt_events(self):
        return [e for e in c.ledger_tail(self.project(), 50) if e.get("event") == "rustfmt"]

    def write_hook(self, path, tool="Write", env=None, timeout=10):
        return self.hook("PostToolUseValidate", {"tool_name": tool, "tool_input": {"file_path": path},
                                                 "tool_response": {}, "session_id": "s1"}, env=env, timeout=timeout)

    def edition_of(self, rel, env):
        self.write_hook(self.write(rel, "fn main() {}\n"), env=env)
        return self.rustfmt_log()[-1].split("--edition ")[1].split()[0]

    def test_rust_file_is_formatted_on_write(self):
        # T-5600: a misformatted .rs file is formatted in place by the write hook, after a Write and after an Edit; the
        # same answer says so, so the builder re-reads the file before its next Edit (a stale Edit would fail)
        env = self.rustfmt_stub(fmt="sed 's/fn main(){}/fn main() {}/'")
        path = self.write("src/main.rs", "fn main(){}\n")
        proc = self.write_hook(path, env=env)
        self.assertEqual(self.blocked(proc), "")
        self.assertEqual(read_text(path), "fn main() {}\n")
        self.assertEqual(self.ctx_of(proc), "Sampo: rustfmt reformatted src/main.rs; re-read it before the next Edit")
        self.assertEqual(len(self.rustfmt_events()), 1, "one event per reformat")
        self.write("src/main.rs", "fn main(){}\n")
        proc = self.write_hook(path, tool="Edit", env=env)
        self.assertEqual(read_text(path), "fn main() {}\n")
        self.assertIn("rustfmt reformatted src/main.rs", self.ctx_of(proc))
        self.assertEqual(len(self.rustfmt_events()), 2)

    def test_rust_format_is_quiet_and_bounded(self):
        # T-5600: a clean file is never written and says nothing; a rustfmt error, a timeout or no rustfmt on PATH
        # leaves the file as it is, says nothing and blocks nothing
        clean = self.write("src/lib.rs", "fn main() {}\n")
        mtime = os.stat(clean).st_mtime_ns
        env = self.rustfmt_stub(fmt="cat")
        proc = self.write_hook(clean, env=env)
        self.assertEqual((proc.stdout, self.ctx_of(proc)), ("", ""))
        self.assertEqual(os.stat(clean).st_mtime_ns, mtime, "a clean file is left untouched")
        self.assertEqual(len(self.rustfmt_log()), 1, "one rustfmt call per write")
        self.assertEqual(self.rustfmt_events(), [], "no reformat, no event")

        # a parse error: rustfmt exits 1 with its message on stderr and nothing on stdout: the file is left alone
        bad = self.write("src/broken.rs", "fn main(){\n")
        env = self.rustfmt_stub(fmt="cat >/dev/null; echo 'error: expected item' >&2; exit 1")
        proc = self.write_hook(bad, env=env)
        self.assertEqual((proc.stdout, self.blocked(proc), self.ctx_of(proc)), ("", "", ""))
        self.assertEqual(read_text(bad), "fn main(){\n")
        self.assertEqual(len(self.rustfmt_log()), 1)

        # a timeout: the one call is capped at ONWRITE_TIMEOUT (5 s), says nothing and blocks nothing
        slow = self.write("src/slow.rs", "fn main(){}\n")
        env = self.rustfmt_stub(fmt="sleep 30")
        t0 = time.monotonic()
        proc = self.write_hook(slow, env=env, timeout=20)
        self.assertLess(time.monotonic() - t0, 12)  # wall clock: a hook's timeout deadline, in a child
        self.assertEqual((proc.stdout, self.blocked(proc), self.ctx_of(proc)), ("", "", ""))
        self.assertEqual(len(self.rustfmt_log()), 1, "the call ran, and was cut at 5 s")

        # no rustfmt on PATH: the hook runs nothing and says nothing
        empty = os.path.join(self.tmp, "nobin")
        os.makedirs(empty, exist_ok=True)
        plain = self.write("src/plain.rs", "fn main(){}\n")
        proc = self.write_hook(plain, env={"PATH": empty})
        self.assertEqual((proc.stdout, self.blocked(proc)), ("", ""))
        self.assertEqual(read_text(plain), "fn main(){}\n")

    def test_rust_edition_from_cargo_toml(self):
        # T-5600: the edition is the nearest Cargo.toml's edition line; a member with edition.workspace = true takes its
        # workspace root's; with no Cargo.toml in the project, 2021 (checked first: nothing else is written yet)
        env = self.rustfmt_stub(fmt="cat")
        self.assertEqual(self.edition_of("loose/snippet.rs", env), "2021")
        self.write("Cargo.toml", '[package]\nname = "app"\nedition = "2024"\n')
        self.assertEqual(self.edition_of("src/main.rs", env), "2024")
        self.write("tools/x/Cargo.toml", '[package]\nname = "x"\nedition = "2015"\n')
        self.assertEqual(self.edition_of("tools/x/src/lib.rs", env), "2015", "the nearest Cargo.toml wins")
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/core"]\n\n[workspace.package]\nedition = "2018"\n')
        self.write("crates/core/Cargo.toml", '[package]\nname = "core"\nedition.workspace = true\n')
        self.assertEqual(self.edition_of("crates/core/src/lib.rs", env), "2018",
                         "a member takes the workspace root's edition")

    def test_a_cargo_toml_above_the_project_is_ignored(self):
        # T-5600: the walk stops at the project root: a Cargo.toml in the folder that holds the project is not its edition
        with open(os.path.join(self.tmp, "Cargo.toml"), "w", encoding="utf-8") as f:
            f.write('[package]\nname = "outer"\nedition = "2018"\n')
        env = self.rustfmt_stub(fmt="cat")
        self.assertEqual(self.edition_of("src/main.rs", env), "2021")

    def test_the_edition_line_may_end_in_a_comment_or_use_single_quotes(self):
        # T-5600: `edition = "2018"  # ...` and `edition = '2015'` are both the edition line
        env = self.rustfmt_stub(fmt="cat")
        self.write("Cargo.toml", '[package]\nname = "app"\nedition = "2018"  # the edition this crate is written in\n')
        self.assertEqual(self.edition_of("src/main.rs", env), "2018")
        self.write("Cargo.toml", "[package]\nname = 'app'\nedition = '2015'\n")
        self.assertEqual(self.edition_of("src/main.rs", env), "2015")

    def test_a_child_module_is_left_alone(self):
        # T-5600: rustfmt gets the written file's text on stdin and never its path, so cargo fmt's recursion into an
        # out-of-line module (src/child.rs here) can't rewrite a file the write didn't touch
        env = self.rustfmt_stub(fmt="sed 's/fn main(){}/fn main() {}/'")
        lib = self.write("src/lib.rs", "mod child;\nfn main(){}\n")
        child = self.write("src/child.rs", "fn child(){}\n")
        proc = self.write_hook(lib, env=env)
        self.assertEqual(read_text(lib), "mod child;\nfn main() {}\n")
        self.assertEqual(read_text(child), "fn child(){}\n", "the child module is untouched")
        self.assertEqual(self.ctx_of(proc), "Sampo: rustfmt reformatted src/lib.rs; re-read it before the next Edit")
        self.assertNotIn(".rs", self.rustfmt_log()[-1], "the call names no file")

    def test_a_symlink_out_of_the_project_is_never_written_through(self):
        # T-5600: a symlink whose target lies outside the project is not formatted: its target is not the project's
        outside = os.path.join(self.tmp, "outside.rs")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("fn main(){}\n")
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        link = os.path.join(self.repo, "src", "link.rs")
        os.symlink(outside, link)
        env = self.rustfmt_stub(fmt="sed 's/fn main(){}/fn main() {}/'")
        self.write_hook(link, env=env)
        self.assertEqual(read_text(outside), "fn main(){}\n")
        self.assertEqual(self.rustfmt_log(), [])

    @unittest.skipUnless(shutil.which("rustfmt"), "needs rustfmt on PATH")
    def test_real_rustfmt_formats_the_file_and_leaves_its_child_module(self):
        # T-5600: the same with the real rustfmt: the file is formatted, its out-of-line child module is not
        lib = self.write("src/lib.rs", "mod child;\nfn main(){}\n")
        child = self.write("src/child.rs", "fn child(){}\n")
        proc = self.write_hook(lib)
        self.assertEqual(read_text(lib), "mod child;\nfn main() {}\n")
        self.assertEqual(read_text(child), "fn child(){}\n")
        self.assertEqual(self.ctx_of(proc), "Sampo: rustfmt reformatted src/lib.rs; re-read it before the next Edit")

    def test_builder_file_says_the_hook_formats_rust(self):
        # T-5600: the builder contract says the write hook formats .rs files, so a builder runs no cargo fmt turn
        with open(os.path.join(PLUGIN, "agents", "fm-builder.md"), encoding="utf-8") as f:
            text = f.read()
        lines = [l for l in text.splitlines() if "rustfmt" in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("- "), lines[0])
        self.assertIn("write hook", lines[0])
        self.assertIn("cargo fmt", lines[0])
        self.assertLess(text.index(lines[0]), text.index("\n\nReport in"), "inside the contract, before the report")
