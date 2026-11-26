"""T-5089: fm doctor names a hook fallback: a hook answered from another tree than the live one is a FAIL."""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

from helpers import ForemanTestCase

from sampo import core as c
from sampo import doctor as d

TB = "Traceback (most recent call last):\n  File \"x\", line 1, in f\n    f()\nNameError: name 'gone' is not defined"
SHA = "0123456789ab"
GREEN = "refs/sampo/green/hook"


def literal(node):
    """A log message's literal text, each interpolated value as "x" (a `+ traceback` tail dropped)."""
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(literal(v) if isinstance(v, ast.Constant) else "x" for v in node.values)
    if isinstance(node, ast.BinOp):
        return literal(node.left) + literal(node.right)
    return ""


class HookFallback(ForemanTestCase):
    def log(self, *entries, name="hooks.log", at=None):
        path = os.path.join(self.home, "state", "logs", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write("".join(f"{at or c.now()} {e}\n" for e in entries))

    def fails(self, tree, error):
        r = d.check_hook_fallback()
        self.assertEqual((r.name, r.status), ("hooks on fallback", "FAIL"), r.detail)
        self.assertIn(tree, r.detail)
        self.assertIn(error, r.detail)
        return r

    def passes(self):
        r = d.check_hook_fallback()
        self.assertEqual((r.name, r.status), ("hooks on fallback", "PASS"), r.detail)

    def test_no_log_or_only_other_lines_pass(self):
        self.passes()
        self.log("Stop " + TB, "PreToolUse slow hook (ms): import 900",
                 "PreToolUse the working copy of fmguard failed; the search is refused (fix plugin/lib/fmguard.py):\n"
                 + TB,
                 # a quoted phrase inside another error is not a fallback
                 "Stop ValueError: 'the working copy of fmguard failed; using the committed guard'",
                 "Stop Traceback (most recent call last):\nthe live tree raised on this call; asking the green tree x")
        self.passes()

    def test_a_failed_import_answered_by_the_committed_library(self):
        self.log("Stop the working copy of Sampo's library failed to import (SyntaxError('bad (x); y')); using the "
                 "committed library (fix plugin/lib)")
        self.fails("committed library (HEAD)", "SyntaxError('bad (x); y')")

    def test_no_writer_emits_the_green_tree_or_head_did_not_load_forms(self):
        # nothing logs these: a line in that shape is not read as a fallback (the writers were checked above)
        self.log("Stop the working copy of Sampo's library failed to import (ImportError('no core')) and HEAD did not "
                 f"load; using the green tree {SHA} ({GREEN}) (fix plugin/lib)")
        self.passes()

    def test_the_guard_and_task_reader_fallbacks(self):
        for message, tree in (
                ("the working copy of fmguard failed; using the committed guard:\n", "committed guard (HEAD)"),
                ("the working copy of fmguard failed; using the committed guard (fix plugin/lib/fmguard.py):\n",
                 "committed guard (HEAD)"),
                ("the working copy of sampo.core failed to read the task; trying the committed one (fix "
                 "plugin/lib/sampo/core.py):\n", "committed sampo.core")):
            with self.subTest(message=message):
                self.log("PreToolUse " + message + TB)
                self.fails(tree, "NameError: name 'gone' is not defined")

    def test_the_latest_is_named_and_all_are_counted_across_the_rotated_log(self):
        self.log("Stop the working copy of Sampo's library failed to import (ImportError('old')); using the committed "
                 "library (fix plugin/lib)", name="hooks.log.1")
        self.log("PreToolUse the working copy of fmguard failed; using the committed guard:\n" + TB
                 + " \x1b[2J")
        r = self.fails("committed guard (HEAD)", "NameError")
        self.assertIn("2 fallback(s)", r.detail)
        self.assertNotIn("\x1b", r.detail)

    def test_a_line_older_than_24h_passes(self):
        self.log("Stop the working copy of Sampo's library failed to import (ImportError('x')); using the committed "
                 "library (fix plugin/lib)", at="2020-01-01T00:00:00Z")
        self.passes()

    def test_the_line_the_real_hook_entry_writes_is_read(self):
        self.fm("init")
        copy = os.path.join(self.tmp, "fcopy")
        shutil.copytree(c.PLUGIN_ROOT, os.path.join(copy, "plugin"),
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", "-C", copy, *args], check=True, capture_output=True)
        with open(os.path.join(copy, "plugin", "lib", "sampo", "hooks.py"), "a") as f:
            f.write("\ndef broken(:\n")
        payload = {"session_id": "sess-1", "cwd": self.repo, "hook_event_name": "SessionStart", "source": "startup"}
        r = subprocess.run([sys.executable, os.path.join(copy, "plugin", "hooks", "hook"), "SessionStart"],
                           input=json.dumps(payload), capture_output=True, text=True, cwd=self.repo, timeout=60,
                           env=dict(os.environ, FOREMAN_HOME=self.home))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.fails("committed library (HEAD)", "SyntaxError")

    def test_every_fallback_message_a_writer_logs_is_read(self):
        # the writers share no template with the check: a reworded or new message must not turn into a silent PASS
        seen = []
        for rel in (("hooks", "hook"), ("lib", "sampo", "hooks.py")):
            with open(os.path.join(c.PLUGIN_ROOT, *rel), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "log_error":
                    text = literal(node.args[1])
                    if re.search(r"\b(?:using|trying|asking) the (?:committed|green)\b", text):
                        seen.append(text)
        self.assertTrue(seen, "no fallback writer found")
        for text in seen:
            with self.subTest(text=text):
                self.assertRegex(f"{c.now()} Stop {text.splitlines()[0]}", d._HOOK_FALLBACK)

    def test_sampo_doctor_reports_it(self):
        self.fm("init")
        self.log("Stop the working copy of Sampo's library failed to import (ImportError('x')); using the committed "
                 "library (fix plugin/lib)")
        r = self.fm("doctor", check=False, timeout=120)
        row = next((l for l in r.stdout.splitlines() if "hooks on fallback" in l), "")
        self.assertIn("FAIL", row, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
