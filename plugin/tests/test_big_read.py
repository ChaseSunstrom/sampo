"""T-5597: in bypass mode a whole-file Read that the outline refusal would stop is answered with its first 250 lines
(updatedInput limit 250) and a one-line trailer naming `sampo outline` and the next offset. A subagent's whole Read of a
file over 8 KB and 250 lines gets it every time. Any other case, and every other permission mode, keeps today's answer."""
import contextlib
import io
import json
import os
import shlex
import unittest
from unittest import mock

from helpers import ForemanTestCase
from sampo import hooks


BIG_LINES = 1100  # 220 defs of five lines: over the outline's 600, and over 8 KB


class BigRead(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.big = self.write("big.py", [f"def handler_{i}(x):\n    y = x\n    return y\n\n\n" for i in range(220)])
        self.wide = self.write("wide.log", ["# " + "w" * 40 + "\n" for _ in range(300)])  # 300 lines, over 8 KB
        self.mid = self.write("mid.log", ["# " + "m" * 38 + "\n" for _ in range(240)])  # 240 lines, over 8 KB
        self.light = self.write("light.log", ["x\n" for _ in range(300)])  # 300 lines, 600 bytes
        self.small = self.write("small.py", ["x = 1\n"])

    def write(self, name, lines):
        path = os.path.join(self.repo, name)
        with open(path, "w") as f:
            f.write("".join(lines))
        return path

    def pre(self, tool_input, mode="bypassPermissions", **extra):
        p = self.hook("PreToolUse", dict({"tool_name": "Read", "tool_input": tool_input, "permission_mode": mode}, **extra))
        return json.loads(p.stdout or "{}").get("hookSpecificOutput") or {}

    def test_whole_read_is_rewritten_to_250_lines(self):
        self.assertGreater(os.path.getsize(self.big), 8192)
        out = self.pre({"file_path": self.big})
        self.assertEqual(out.get("permissionDecision"), "allow")
        self.assertEqual(out.get("updatedInput"), {"file_path": self.big, "limit": 250})
        trailer = out.get("additionalContext", "")
        self.assertIn(f"Sampo: {BIG_LINES} lines, showing 1-250", trailer)
        self.assertIn(f"sampo outline {self.big}", trailer)
        self.assertIn("next offset=251", trailer)
        self.assertIn("allow_large: true", trailer)
        self.assertEqual(len(trailer.splitlines()), 1, "one short line")
        self.assertEqual(self.pre({"file_path": self.big}), {}, "asked again: it reads whole")

    def test_subagent_whole_read_over_8kb_is_rewritten(self):
        for _ in range(2):  # every time, not only the first
            out = self.pre({"file_path": self.big}, agent_id="sub-1")
            self.assertEqual(out.get("updatedInput"), {"file_path": self.big, "limit": 250})
            self.assertIn("sampo outline", out.get("additionalContext", ""))
        self.assertEqual(self.pre({"file_path": self.big}, agent_id="sub-2").get("updatedInput"),
                         {"file_path": self.big, "limit": 250}, "each subagent has its own context")
        self.assertEqual(self.pre({"file_path": self.wide}, agent_id="sub-1").get("updatedInput"),
                         {"file_path": self.wide, "limit": 250}, "over 250 lines and over 8 KB, though under 600")
        self.assertEqual(self.pre({"file_path": self.big}).get("updatedInput"),
                         {"file_path": self.big, "limit": 250}, "the subagents' reads do not spend the lead's first read")

    def test_no_rewrite_elsewhere(self):
        self.assertEqual(self.pre({"file_path": self.big, "allow_large": True}), {})
        self.assertEqual(self.pre({"file_path": self.big, "offset": 251, "limit": 250}), {})
        self.assertEqual(self.pre({"file_path": self.big, "limit": 100}), {})
        self.assertEqual(self.pre({"file_path": self.big, "pages": "1-2"}), {})
        self.assertEqual(self.pre({"file_path": self.small}), {}, "a small file reads whole")
        self.assertEqual(self.pre({"file_path": self.wide}), {}, "the lead's refusal needs 600 lines")
        self.assertEqual(self.pre({"file_path": self.mid}, agent_id="sub-1"), {}, "250 lines or fewer: no rewrite")
        self.assertEqual(self.pre({"file_path": self.light}, agent_id="sub-1"), {}, "under 8 KB: no rewrite")
        self.assertEqual(self.pre({"file_path": self.wide, "allow_large": True}, agent_id="sub-1"), {})
        blob = os.path.join(self.repo, "blob.bin")  # a binary read has no lines for limit to count
        with open(blob, "wb") as f:
            f.write(b"\x89PNG\x00\x00\x00\r" + b"\x00\x9c\n" * 5000)
        self.assertEqual(self.pre({"file_path": blob}, agent_id="sub-1"), {}, "a binary file reads as it does today")
        pdf = self.write("doc.pdf", ["p" * 40 + "\n" for _ in range(300)])  # over 8 KB, 300 lines: paged, not limited
        self.assertEqual(self.pre({"file_path": pdf}, agent_id="sub-1"), {}, "a PDF keeps its pages")
        ssh = os.path.join(self.repo, ".ssh")
        os.makedirs(ssh, exist_ok=True)
        key = os.path.join(ssh, "key.big")
        with open(key, "w") as f:
            f.write("k\n" * BIG_LINES)
        out = self.pre({"file_path": key})
        self.assertNotIn("updatedInput", out, "a credential read stays the guard's")
        self.assertNotIn("sampo outline", out.get("additionalContext", ""))

    def test_granted_credential_path_is_not_rewritten(self):
        # a repo's own .ssh/*.pub: the guard grants it (its public part), so the hook's credential check passes on it,
        # yet the rewrite must still decline: the credential rule is the rewrite's own, not the guard's
        ssh = os.path.join(self.repo, ".ssh")
        os.makedirs(ssh, exist_ok=True)
        pub = os.path.join(ssh, "key.pub")
        with open(pub, "w") as f:
            f.write("ssh-ed25519 AAAA\n" * BIG_LINES)
        out = self.pre({"file_path": pub})
        self.assertNotIn("updatedInput", out, "a granted credential path is never rewritten")
        self.assertNotIn("sampo outline", out.get("additionalContext", ""))

    def test_a_rewrite_that_raises_falls_back_to_the_refusal(self):
        # in-process, with only the rewrite stubbed to raise: the Read branch logs it and the outline refusal answers
        raw = json.dumps({"tool_name": "Read", "tool_input": {"file_path": self.big}, "session_id": "sess-boom",
                          "permission_mode": "bypassPermissions"})
        with mock.patch.object(hooks, "_big_read_rewrite", side_effect=OSError("the rewrite broke")), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            hooks._pre_tool_use(raw)
        out = json.loads(buf.getvalue() or "{}").get("hookSpecificOutput") or {}
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertNotIn("updatedInput", out)
        self.assertIn("read a range", out.get("permissionDecisionReason", ""))

    def test_the_trailer_quotes_the_path(self):
        spaced = self.write("big notes.py", [f"def h_{i}(x):\n    return x\n\n\n" for i in range(300)])
        self.assertGreater(os.path.getsize(spaced), 8192)
        out = self.pre({"file_path": spaced}, agent_id="sub-1")
        trailer = out.get("additionalContext", "")
        self.assertIn(f"sampo outline {shlex.quote(spaced)} lists", trailer)
        self.assertEqual(len(trailer.splitlines()), 1, "one short line")

    def test_outline_refusal_stays_in_other_modes(self):
        for mode in ("default", "acceptEdits", "plan", "auto", "dontAsk"):
            for agent in (None, "sub-1"):  # its own session per mode, so one mode's first read is not another's
                extra = {"session_id": f"mode-{mode}", **({"agent_id": agent} if agent else {})}
                out = self.pre({"file_path": self.big}, mode=mode, **extra)
                self.assertEqual(out.get("permissionDecision"), "deny", (mode, agent))
                self.assertNotIn("updatedInput", out, (mode, agent))
                self.assertIn("read a range", out.get("permissionDecisionReason", ""), (mode, agent))
        self.assertEqual(self.pre({"file_path": self.wide}, mode="default", agent_id="sub-1", session_id="mode-w"), {},
                         "the outline refusal needs 600 lines in every mode")


if __name__ == "__main__":
    unittest.main()
