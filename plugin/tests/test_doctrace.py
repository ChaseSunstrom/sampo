"""sampo docs --trace: each doc claim (docs.claims) linked to the code lines that back it; --since REF marks the claims whose
backing changed since a git ref (T-3287)."""
import os
import re
import subprocess

from helpers import ForemanTestCase

from sampo import doctrace

TOOL = ('#!/usr/bin/env python3\n'
        'import argparse\n'
        'p = argparse.ArgumentParser(prog="tool")\n'
        'p.add_argument("--json", action="store_true")\n'
        's = p.add_subparsers(dest="cmd", required=True)\n'
        's.add_parser("run")\n'
        'p.parse_args()\n')
README = ("# tool\n"
          "\n"
          "Run `src/keep.py` and `src/edit.py`, then `tool --json`, then `tool run`.\n"
          "Old `src/gone.py` is gone; `tool --nosuch` fails.\n")


class DocTrace(ForemanTestCase):
    def setUp(self):
        super().setUp()
        files = {"README.md": README, "bin/tool": TOOL, "src/keep.py": "KEEP = 1\n", "src/edit.py": "EDIT = 1\n"}
        for rel, text in files.items():
            path = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        os.chmod(os.path.join(self.repo, "bin", "tool"), 0o755)  # a bin/ program the docs' `tool …` lines name
        self.git("add", "-A")
        self.git("commit", "-qm", "tool and its docs")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@e.c", "-c", "user.name=t", *args],
                              capture_output=True, text=True, check=True).stdout

    def write(self, rel, text):  # committed: the trace reads the tracked tree
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        self.git("add", "-A")
        self.git("commit", "-qm", f"write {rel}")

    def rows(self, **kw):
        return {(r["file"], r["kind"], r["detail"]): r for r in doctrace.trace(self.repo, **kw)}

    def test_trace_links_each_claim_to_the_lines_that_back_it(self):
        rows = self.rows()
        keep = rows[("README.md", "path", "src/keep.py")]
        self.assertEqual((keep["verdict"], [b["file"] for b in keep["backing"]], keep["stale"]),
                         ("proven", ["src/keep.py"], False))
        gone = rows[("README.md", "path", "src/gone.py")]
        self.assertEqual((gone["verdict"], gone["backing"]), ("failed", []))  # a failed claim has no backing
        json_line = TOOL.splitlines().index('p.add_argument("--json", action="store_true")') + 1
        flag = rows[("README.md", "flag", "--json")]
        self.assertEqual(flag["line"], 3)  # the doc line: where the claim's text is
        self.assertEqual([(b["file"], b["line"]) for b in flag["backing"]], [("bin/tool", json_line)])
        run_line = TOOL.splitlines().index('s.add_parser("run")') + 1
        sub = rows[("README.md", "subcommand", "tool run")]
        self.assertEqual([(b["file"], b["line"]) for b in sub["backing"]], [("bin/tool", run_line)])
        self.assertEqual(rows[("README.md", "flag", "--nosuch")]["backing"], [])
        self.assertFalse(any(b["file"] == "README.md" for r in rows.values() for b in r["backing"]),
                         "a doc is never the backing of its own claim")

    def test_trace_since_marks_only_the_claims_whose_backing_changed(self):
        with open(os.path.join(self.repo, "src", "edit.py"), "w") as f:  # committed after the ref
            f.write("EDIT = 2\n")
        self.git("commit", "-qam", "edit the backing of one path")
        with open(os.path.join(self.repo, "bin", "tool"), "a") as f:  # uncommitted: the worktree counts too
            f.write("# a comment\n")
        rows = self.rows(since=self.base)
        self.assertTrue(rows[("README.md", "path", "src/edit.py")]["stale"])
        self.assertFalse(rows[("README.md", "path", "src/keep.py")]["stale"])
        self.assertTrue(rows[("README.md", "flag", "--json")]["stale"])
        self.assertTrue(rows[("README.md", "subcommand", "tool run")]["stale"])
        self.assertFalse(rows[("README.md", "flag", "--nosuch")]["stale"])
        self.assertEqual(rows[("README.md", "path", "src/keep.py")]["backing"][0]["file"], "src/keep.py")
        with self.assertRaises(ValueError):  # a ref git can't resolve is an error, never every claim fresh
            self.rows(since="no-such-ref")

    def test_docs_trace_prints_one_row_per_claim_and_json_carries_the_same_rows(self):
        with open(os.path.join(self.repo, "src", "edit.py"), "w") as f:
            f.write("EDIT = 2\n")
        self.git("commit", "-qam", "edit")
        text = self.fm("docs", "--trace", "--since", self.base, cwd=self.repo).stdout.splitlines()
        json_line = TOOL.splitlines().index('p.add_argument("--json", action="store_true")') + 1
        self.assertIn(f"README.md:3 flag --json -> bin/tool:{json_line}", text)  # fresh: bin/tool is not in the diff
        self.assertIn("README.md:4 path src/gone.py -> UNBACKED", text)
        self.assertIn("README.md:3 path src/edit.py -> src/edit.py STALE", text)
        self.assertIn("README.md:3 path src/keep.py -> src/keep.py", text)
        self.assertEqual(len(text), len(doctrace.trace(self.repo)))  # one row per claim
        data = self.fm_json("docs", "--trace", "--since", self.base, cwd=self.repo)
        rows = data["rows"]
        self.assertEqual(len(rows), len(text))
        self.assertEqual(sum(r["stale"] for r in rows), sum(t.endswith("STALE") for t in text))
        self.assertEqual(sum(not r["backing"] for r in rows), sum(t.endswith("UNBACKED") for t in text))
        self.assertTrue(all(re.fullmatch(r"[1-9]\d*", str(r["line"])) for r in rows))  # never a line 0
        bad = self.fm("docs", "--since", self.base, cwd=self.repo, check=False)  # --since needs --trace
        self.assertEqual(bad.returncode, 1)

    def test_trace_ranks_definitions_first_and_judges_staleness_on_them(self):
        self.write("src/mention.py", 'print("pass --json to the tool")\n')  # a mention: no definition
        self.write("tests/test_tool.py", 'p.add_argument("--json")\n')  # a test file never backs a claim
        ref = self.git("rev-parse", "HEAD").strip()
        self.write("src/mention.py", 'print("the tool takes --json")\n')  # only the mention changes
        flag = self.rows(since=ref)[("README.md", "flag", "--json")]
        json_line = TOOL.splitlines().index('p.add_argument("--json", action="store_true")') + 1
        self.assertEqual([(b["file"], b["line"], b["def"]) for b in flag["backing"]],
                         [("bin/tool", json_line, True), ("src/mention.py", 1, False)])
        self.assertFalse(flag["stale"], "a mention-only change does not stale a defined flag")
        self.assertEqual(flag["target"]["file"], "bin/tool")
        self.assertEqual([b["stale"] for b in flag["backing"] if not b["def"]], [True])

    def test_trace_caps_the_backing_list_and_counts_every_backing(self):
        self.write("src/many.py", 'p.add_argument("--json")\n' * 7)
        flag = self.rows()[("README.md", "flag", "--json")]
        self.assertEqual(flag["backing_total"], 8)  # bin/tool's definition and seven in src/many.py
        self.assertEqual(len(flag["backing"]), doctrace.BACKING_MAX)
        self.assertTrue(all(b["def"] for b in flag["backing"]))

    def test_a_fenced_command_the_doc_quotes_is_linked_by_its_first_word(self):
        self.write("GUIDE.md", "# guide\n\n```sh\ntool run 'two words' --json\n```\n")  # the claim's words lose the quotes
        cmd = self.rows()[("GUIDE.md", "command", "tool run two words --json")]
        self.assertEqual(cmd["line"], 4)  # no line shows the claim's words; its first word's line does
        run_line = TOOL.splitlines().index('s.add_parser("run")') + 1
        self.assertEqual([(b["file"], b["line"]) for b in cmd["backing"]], [("bin/tool", run_line)])

    def test_docs_trace_strict_fails_when_a_claim_has_no_backing(self):
        ok = self.fm("docs", "--trace", cwd=self.repo, check=False)
        self.assertEqual(ok.returncode, 0)  # UNBACKED is a report unless --strict asks for more
        strict = self.fm("docs", "--trace", "--strict", cwd=self.repo, check=False)
        self.assertEqual(strict.returncode, 1)
        self.assertIn("README.md:4 path src/gone.py -> UNBACKED", strict.stdout.splitlines())  # the rows still print

    def test_since_refuses_an_option_or_an_empty_ref(self):
        for ref in ("-x", "--no-index", ""):
            with self.assertRaises(ValueError):
                doctrace.trace(self.repo, since=ref)
        bad = self.fm("docs", "--trace", "--since=--no-index", cwd=self.repo, check=False)
        self.assertEqual(bad.returncode, 1)
