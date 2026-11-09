"""T-3529: the user's private terms (host globs or plain words) live only in the state folder, outside every repo:
sampo private add|rm|list keeps them, never printing or logging one in clear, and core.redact_local (the .sampo
mirror's last pass) writes <host> or <private> in their place."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

from helpers import PRIVATE_TERMS, ForemanTestCase, read_text

from sampo import core as c

HOST = "*.home.example-private.test"  # made-up values: never a real private domain in a test
WORD = "wombatcorp"


class PrivateTerms(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def test_add_list_rm_keep_terms_in_the_state_folder_and_never_print_or_log_them(self):
        said = self.fm("private", "add", HOST).stdout + self.fm("private", "add", "WombatCorp").stdout
        path = os.path.join(c.state_dir(), c.PRIVATE_TERMS)
        self.assertTrue(path.startswith(os.path.join(self.home, "state") + os.sep), path)
        self.assertEqual(c.private_terms(), sorted([HOST, WORD]), "kept lowercased")
        self.assertEqual(os.stat(path).st_mode & 0o077, 0, "only the user reads the file")
        listed = self.fm("private", "list").stdout  # not a terminal: a count and digests, never the terms
        ledger = read_text(os.path.join(c.find_project(self.repo).dir, "ledger.jsonl"))
        for text in (said, listed, ledger):
            self.assertNotIn("example-private", text)
            self.assertNotIn(WORD, text.lower())
        self.assertIn("2 private terms", listed)
        self.assertIn(hashlib.sha256(WORD.encode()).hexdigest()[:8], ledger, "a digest and a count are logged")
        self.assertIn('"count": 2', ledger)
        self.fm("private", "rm", "WOMBATCORP")
        self.assertEqual(c.private_terms(), [HOST])
        self.assertNotEqual(self.fm("private", "rm", "not-there", check=False).returncode, 0)
        for bad in ("*", "*.x", "a\nb"):
            self.assertNotEqual(self.fm("private", "add", bad, check=False).returncode, 0, repr(bad))
        self.assertEqual(c.private_terms(), [HOST])

    def test_redact_local_writes_host_for_a_matching_host_and_private_for_a_word(self):
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w") as f:
            json.dump([HOST, WORD, "*", "ab", "*.x"], f)  # a hand-edited term that would match everything is left out
        self.assertEqual(c.private_terms(), [HOST, WORD])
        cases = {
            "GET http://llm.home.example-private.test:8080/v1 ok": "GET http://<host>:8080/v1 ok",
            "ssh alice@Jarvis.HOME.example-private.test now.": "ssh <host> now.",
            "the apex home.example-private.test too": "the apex <host> too",
            "WombatCorp's box": "<private>'s box",
            "nothome.example-private.testing and wombatcorpx stay": "nothome.example-private.testing and wombatcorpx stay",
        }
        for raw, want in cases.items():
            self.assertEqual(c.redact_local(raw), want, raw)
        self.assertTrue(c.has_private("see llm.home.example-private.test"))
        self.assertFalse(c.has_private("see <host>"))
        self.assertTrue(c.has_private("a wombatcorp here", ["WOMBATCORP"]), "terms passed in, any case")
        os.remove(os.path.join(c.state_dir(), c.PRIVATE_TERMS))
        self.assertEqual(c.redact_local("llm.home.example-private.test"), "llm.home.example-private.test")


class TrackedTree(unittest.TestCase):
    def test_no_tracked_file_holds_a_private_term(self):
        # T-4096: a test carried the JARVIS login for 9 h until fm ship's scan refused it (T-3931); the tracked tree is
        # scanned here, so a gate run before any merge finds it. The term itself is never printed. CI: a made-up one
        root = os.path.dirname(c.PLUGIN_ROOT)
        needles = sorted({max(t.removeprefix("*.").split("*"), key=len) for t in PRIVATE_TERMS})
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:  # 0600, so no term shows in ps as argv
            f.write("\n".join(needles) + "\n")
            f.flush()
            r = subprocess.run(["git", "-C", root, "grep", "-l", "-z", "-I", "-i", "-F", "-f", f.name],
                               capture_output=True, text=True, timeout=300)
        self.assertIn(r.returncode, (0, 1), "git grep failed")
        hits = [p for p in r.stdout.split("\0") if p and c.has_private(read_text(os.path.join(root, p)), PRIVATE_TERMS)]
        self.assertEqual(hits, [], "these tracked files name one of the user's private terms (sampo private list)")
