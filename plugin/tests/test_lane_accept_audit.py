"""T-4391: `sampo lane accept ID --audit LENS HOW RESULT` takes three values per audit. Typed with two values and a second
--audit (accept ID --audit intent '<text>' --audit adversary '<text>') it once recorded one audit and exited 0 with
nothing said about the dropped one. How it got past nargs=3: Python 3.14's argparse lets an option with a fixed count
take the next option string as its last value when it is one short, so the second --audit became the first audit's
RESULT, and 'adversary' and the text fell into the command's task-id list (nargs="*"), which accept never read. Every
triple is now read before anything is recorded or started: a known lens, a non-empty how and result, no value that is
one of the command's own option strings, and no words left over. The real command runs here (a temp project and lane): nothing starts a check or a land."""
# map-links: plugin/lib/sampo/train.py
import contextlib
import io
import os
import types
import unittest
from unittest import mock

from test_lane_land import LandCase, OLD

from sampo import cli
from sampo import core as c
from sampo import train

TWO = ["--audit", "intent", "reviewed the diff", "--audit", "adversary", "tried to break it"]


class AuditCase(LandCase):
    def setUp(self):
        super().setUp()
        self.spawned = []
        for name, ret in (("_spawn_check", (os.getpid(), os.path.join(self.tmp, "check.log"))),
                          ("spawn_land", (4242, "land.log"))):
            m = mock.patch.object(train, name, side_effect=lambda *a, _r=ret, **k: self.spawned.append(name) or _r)
            m.start()
            self.addCleanup(m.stop)
        self.p = c.find_project(self.repo)
        self.tid = self.lane(1, "f1.txt", when=OLD)

    def accept(self, *argv, check_flag=True):
        """The CLI's own parser and dispatch, in this process (the starts above are replaced)."""
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        os.chdir(self.repo)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(["lane", "accept", self.tid, *argv])
                except SystemExit as e:
                    code = e.code
        finally:
            os.chdir(here)
        return code or 0, out.getvalue() + err.getvalue()
    def audits(self):
        return [lens for lens, _, _ in c.find_brief(self.p, self.tid).audits()]

    def nothing_happened(self):
        self.assertEqual(self.audits(), [])
        self.assertEqual(self.spawned, [])
        self.assertEqual(train.first_ids(self.p), [])


class TwoValues(AuditCase):
    def test_two_values_refused(self):
        """The two-value form: refused, the form and the lens it could not read named, nothing recorded or started."""
        code, text = self.accept(*TWO)
        self.assertNotEqual(code, 0, text)
        self.assertIn("--audit LENS HOW RESULT", text)
        self.assertIn("could not read the intent audit", text)
        self.assertIn("RESULT is the option --audit", text)
        self.nothing_happened()


class Malformed(AuditCase):
    def test_eaten_flag_refused(self):
        code, text = self.accept("--audit", "intent", "how", "--no-check")
        self.assertNotEqual(code, 0, text)
        self.assertIn("--audit LENS HOW RESULT", text)
        self.nothing_happened()
        code, text = self.accept("--audit", "intent", "--audit", "adversary", "result")
        self.assertNotEqual(code, 0, text)
        self.assertIn("--audit LENS HOW RESULT", text)
        self.nothing_happened()

    def test_unknown_lens_refused(self):
        code, text = self.accept("--audit", "intent", "how", "ok", "--audit", "vibes", "how", "ok")
        self.assertNotEqual(code, 0, text)
        self.assertIn("vibes", text)
        self.assertIn("--audit LENS HOW RESULT", text)
        self.nothing_happened()

    def test_empty_how_or_result_refused(self):
        for bad in (("intent", "", "ok"), ("intent", "how", ""), ("intent", "  ", "ok")):
            code, text = self.accept("--audit", *bad)
            self.assertNotEqual(code, 0, text)
            self.assertIn("--audit LENS HOW RESULT", text)
            self.nothing_happened()


class ThreeValues(AuditCase):
    def test_three_values_record_each_audit(self):
        code, text = self.accept("--audit", "intent", "read the diff", "no findings",
                                 "--audit", "adversary", "tried to break it", "none found")
        self.assertEqual(code, 0, text)
        self.assertEqual(sorted(self.audits()), ["adversary", "intent"])

    def test_three_values_once(self):
        code, text = self.accept("--audit", "intent", "read the diff", "no findings")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.audits(), ["intent"])


if __name__ == "__main__":
    unittest.main()
