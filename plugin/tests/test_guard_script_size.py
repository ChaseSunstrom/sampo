"""T-4925: the refusal of a script past both read budgets ("the script was too long to read in time") names the script's
bytes, its lines and the CPU seconds the reads spent, and gets the shorter-script hint the parse-too-long refusal gets.
Only the refusal's text and its hint change: the same scripts are refused, and the same ones allowed."""
import os
import re
import tempfile
from unittest import mock

from test_guard import GuardCase, g

HINT = "Run a shorter script (split it, or run the part you need directly): a shorter one is read in full."
NUMBERS = re.compile(r"\((\d+) bytes, (\d+) lines; (\d+\.\d\d) s CPU spent against the budgets \S+ s, then \S+ s\)")


class ScriptSizeRefusal(GuardCase):
    def script(self, text):
        # a folder of its own per test: the verdict cache is keyed by the path and the bytes, so a rerun never answers stale
        d = tempfile.mkdtemp(dir=os.path.dirname(self.home))
        p = os.path.join(d, "slow.sh")
        with open(p, "w") as f:
            f.write(text)
        return p

    def refused(self, text):
        p = self.script(text)
        with mock.patch.dict(os.environ, {"FM_GUARD_SCRIPT_BUDGET": "0.000001", "FM_GUARD_SCRIPT_HARD": "0.000001"}):
            r = self.bash(f"bash {p}")
        self.assertBlocked(r, "system")
        self.assertIn("too long to read in time", r.detail)
        return r, p

    def test_the_refusal_names_the_script_bytes_and_lines(self):
        r, p = self.refused("echo hi\n" * 4000)
        m = NUMBERS.search(r.detail)
        self.assertIsNotNone(m, r.detail)
        self.assertEqual(int(m[1]), os.path.getsize(p), "the bytes the guard read are the file's bytes")
        self.assertEqual(int(m[1]), 32000)
        self.assertEqual(int(m[2]), 4000)

    def test_a_last_line_without_a_newline_is_a_line(self):
        r, p = self.refused("echo hi\n" * 4000 + "echo x")
        m = NUMBERS.search(r.detail)
        self.assertIsNotNone(m, r.detail)
        self.assertEqual((int(m[1]), int(m[2])), (32006, 4001), "4000 lines ended by a newline, and one without")

    def test_the_refusal_names_the_cpu_seconds_the_reads_spent(self):
        r, _p = self.refused("echo hi\n" * 4000)
        spent = float(NUMBERS.search(r.detail)[3])
        # both reads ran out of their own budgets (1 us each): the clock starts at this read, not at the hook's start
        self.assertGreaterEqual(spent, 0.0)
        self.assertLess(spent, 5.0, r.detail)
        self.assertIn("(32000 bytes, 4000 lines; ", r.detail)
        self.assertIn(" s CPU spent against the budgets 1e-06 s, then 1e-06 s), so it, or a script it runs, was not read "
                      "to the end: split it into smaller scripts or run its steps as separate commands", r.detail)

    def test_the_refusal_gets_the_shorter_script_hint(self):
        r, _p = self.refused("echo hi\n" * 4000)
        self.assertEqual(g._hint(r.detail, r.category), " " + HINT)
        self.assertTrue(g.message(r, self.ctx()).endswith(" " + HINT), g.message(r, self.ctx()))

    def test_the_parse_too_long_refusal_keeps_the_same_hint(self):
        detail = "a.sh line 3: rm -rf ~ (rm-outside; the script was too long to parse in time)"
        self.assertEqual(g._hint(detail, "system"), " " + HINT)

    def test_other_refusals_get_no_shorter_script_hint(self):
        for detail in ("a.sh: the script was too long to read", "a.sh: rm -rf ~ (rm-outside)",
                       "a.sh: the script was read to the end and refused"):
            with self.subTest(detail=detail):
                self.assertNotIn("Run a shorter script", g._hint(detail, "system"))
