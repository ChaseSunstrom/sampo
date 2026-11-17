"""T-4889: a merge train's suite gate cut at its deadline is no red. Its not-run classes are never failures: cut with
nothing failed, the gate runs once more, and cut again the train stops as cut off, no lane blamed and no base probed; a
real red beside cut classes is judged on the real ones alone and the whole suite is owed to the confirm. A probe of a tree
names only classes that tree defines (a lane's new class in an old module is no class of the base's)."""
# map-links: plugin/lib/sampo/train.py, plugin/tests/run.py
import os
import tempfile
import unittest

import test_train_flaky as ttf
from test_lane_train import OK

# test_train_flaky's runner, worded as tests/run.py words a deadline cut: while BUDGET holds a count, a whole run (module
# names, no class) runs none of the classes CUTSET names (all with none), lists them as `not run class:` and ends 124
# when nothing failed; a probe (class names) always runs in full
CUT_SUITE = '''import importlib, os, subprocess, sys, unittest
BUDGET, CUTSET = {0!r}, {1!r}
names = sys.argv[1:] or sorted(f[:-3] for f in os.listdir("plugin/tests") if f.startswith("test_") and f.endswith(".py"))
jobs = []
for n in names:
    if "." in n:
        jobs.append(n)
        continue
    mod = importlib.import_module(n)
    jobs += [n + "." + k for k, v in vars(mod).items() if isinstance(v, type) and issubclass(v, unittest.TestCase)]
left = int(open(BUDGET).read()) if os.path.exists(BUDGET) else 0
cut = []
if left and not any("." in n for n in sys.argv[1:]):
    open(BUDGET, "w").write(str(left - 1))
    want = open(CUTSET).read().split() if os.path.exists(CUTSET) else []
    if want == ["SILENT"]:
        sys.exit(124)  # a timeout that said nothing at all
    cut = [j for j in jobs if not want or j.rsplit(".", 1)[-1] in want]
bad = []
for j in (j for j in jobs if j not in cut):
    r = subprocess.run([sys.executable, "-m", "unittest", j], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr)
        bad.append(j)
for j in bad:
    print("  failed class: " + j)
if bad:
    print("FAILED (%d classes: %s)" % (len(bad), ", ".join(bad)))
elif cut:
    print("INCOMPLETE (stopped at its deadline)")
if cut:
    print("not run (deadline): %d" % len(cut))
    print("".join("  not run class: %s\\n" % j for j in cut), end="")
sys.exit(1 if bad else 124 if cut else 0)
'''
STRONG = "import unittest\n\n\nclass Strong(unittest.TestCase):\n    def test_it(self):\n        self.fail('this lane breaks it')\n"


class CutGate(ttf.Gate):
    def setUp(self):
        super().setUp()
        self.budget, self.cutset = os.path.join(self.tmp, "cut-budget"), os.path.join(self.tmp, "cut-set")
        self.base("suite.py", CUT_SUITE.format(self.budget, self.cutset))
        self.green_env()  # no start check: the gate itself is what these tests cut

    def cuts(self, n, only=""):
        with open(self.budget, "w") as f:
            f.write(str(n))
        if only:
            with open(self.cutset, "w") as f:
                f.write(only)

    def out(self, r):
        return r.stdout + r.stderr

    def captures(self):
        from sampo import core as c
        return [b.title for b in c.load_briefs(self.p) if "red on the bare base" in b.title]

    def test_a_gate_cut_with_nothing_failed_runs_once_more_and_lands_every_lane(self):
        self.cuts(1)
        a, b = self.lane(1, "test_a.py", OK.format("A")), self.lane(2, "test_b.py", OK.format("B"))
        r = self.train(a, b, check=False)
        out = self.out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertTrue(self.landed("test_a.py") and self.landed("test_b.py"), out)
        self.assertNotIn("left out", out)
        self.assertIn("cut off at its deadline", out)
        args = self.gate_args()
        self.assertEqual([x for x in args if "." in x], [], f"nothing failed, so no class is probed: {args}")
        self.assertEqual(len(args), 2, f"the gate, then the gate once more: {args}")

    def test_a_gate_that_timed_out_naming_nothing_runs_once_more_and_lands(self):
        self.cuts(1, "SILENT")
        a = self.lane(1, "test_a.py", OK.format("A"))
        r = self.train(a, check=False)
        out = self.out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertTrue(self.landed("test_a.py"), out)
        self.assertIn("cut off at its deadline", out)
        self.assertEqual(len(self.gate_args()), 2, f"the gate, then the gate once more, no bisection: {self.gate_args()}")

    def test_a_gate_cut_every_time_stops_the_train_blames_no_lane_and_probes_no_base(self):
        self.cuts(99)
        a, b = self.lane(1, "test_a.py", OK.format("A")), self.lane(2, "test_b.py", OK.format("B"))
        r = self.train(a, b, check=False)
        out = self.out(r)
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("cut off at its deadline", out)
        self.assertNotIn("inconclusive", out)
        self.assertNotIn("left out", out)
        self.assertFalse(self.landed("test_a.py") or self.landed("test_b.py"), out)
        for tid in (a, b):
            self.assertNotIn("left out of a merge train", self.fm("task", "show", tid).stdout, tid)
        self.assertEqual(len(self.gate_args()), 2, f"the gate and one rerun, no probe: {self.gate_args()}")
        self.assertEqual(self.events("lane_train_base_inconclusive"), [])

    def test_a_real_red_beside_cut_classes_is_judged_on_the_real_one_and_the_cut_ones_run_before_landing(self):
        self.cuts(1, "Late")  # the cut class is lane a's new one in an old module: the base has no such class
        late = OK.format("Doctor") + "\n\n" + OK.format("Late")
        a, b = self.lane(1, "plugin/tests/test_doctor.py", late), self.lane(2, "test_strong.py", STRONG)
        r = self.train(a, b, check=False)
        out = self.out(r)
        self.assertIn(f"left out {b}: its merge turns the gates red", out)
        self.assertNotIn(f"left out {a}", out)
        self.assertNotIn("red on the bare base", out)
        self.assertEqual(self.captures(), [], "the base was never red")
        self.assertTrue(self.landed("plugin/tests/test_doctor.py") and not self.landed("test_strong.py"), out)
        self.assertIn("class Late", open(os.path.join(self.repo, "plugin/tests/test_doctor.py")).read())
        whole = [x for x in self.gate_args() if "." not in x]
        self.assertGreaterEqual(len(whole), 2, f"the cut class ran in a whole confirm before anything landed: {whole}")


class Reading(unittest.TestCase):
    """The readers themselves: what failed, what only ran out of time, and which classes a module has."""
    CUT = "\nnot run (deadline): 2\n  not run class: test_late.L\n  not run class: test_later.M.test_x\n"

    def test_cut_classes_are_never_failing_and_a_cut_alone_failed_nothing(self):
        from sampo import train
        red = "FAIL: test_a (test_x.X.test_a)\n\n  failed class: test_x.X\n\nFAILED (1 classes: test_x.X)\n" + self.CUT
        self.assertEqual(train._failed(red), ["test_x.X"])
        self.assertFalse(train._nothing_failed({"exit": 1, "out": red}))
        self.assertTrue(train._nothing_failed({"exit": 124, "out": "INCOMPLETE (stopped at its deadline)" + self.CUT}))
        self.assertTrue(train._nothing_failed({"exit": 124, "out": ""}), "a timeout that said nothing")
        self.assertFalse(train._nothing_failed({"exit": 124, "out": "ERROR: test_b (test_y.Y.test_b)\n"}),
                         "a kill after a class failed is a red")
        self.assertFalse(train._nothing_failed({"exit": 1, "out": "boom"}), "no cut, no 124: not a cut")

    def write(self, text):
        fd, path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_a_module_defines_its_top_level_classes_imports_and_assignments(self):
        from sampo import train
        text = ("import unittest\nfrom helpers import Base as Shared, Other\nimport pkg.mod\n\n\nclass A(unittest.TestCase):\n"
                "    class Inner:\n        pass\n\n\ntry:\n    class B(A):\n        pass\nexcept ImportError:\n"
                "    class C(A):\n        pass\nif True:\n    D = A\nE: type = A\n\n\ndef f():\n    class Hidden:\n        pass\n")
        got = train._defines(self.write(text))
        self.assertTrue({"A", "B", "C", "D", "E", "Shared", "Other", "pkg", "unittest"} <= got, got)
        self.assertFalse({"Inner", "Hidden", "Base"} & got, got)

    def test_a_module_that_makes_classes_at_run_time_or_cannot_be_read_is_unknown(self):
        from sampo import train
        for text in ("from helpers import *\n", "for n in 'ab':\n    globals()[n] = type(n, (), {})\n",
                     "import sys\nsetattr(sys.modules[__name__], 'X', type('X', (), {}))\n", "class (:\n"):
            self.assertIsNone(train._defines(self.write(text)), text)
        self.assertIsNone(train._defines(os.path.join(tempfile.gettempdir(), "no-such-module-T-4889.py")))


class ProbeNames(ttf.Gate):
    def test_a_lanes_new_class_in_an_old_module_is_not_probed_on_the_base(self):
        """Before T-4889 the base probe named test_doctor.Strong (its module is on the base, the class is not): the
        runner failed on a name it could not load and the train called the base red, or, with tests/run.py's 'no test
        matches', inconclusive, and stopped."""
        self.green_env()
        strong = OK.format("Doctor") + "\n\n" + STRONG
        a, b = self.lane(1, "plugin/tests/test_doctor.py", strong), self.lane(2, "test_b.py", OK.format("B"))
        r = self.train(a, b, check=False)
        out = r.stdout + r.stderr
        self.assertIn(f"left out {a}: its merge turns the gates red", out)
        self.assertNotIn("red on the bare base", out)
        self.assertNotIn("inconclusive", out)
        self.assertTrue(self.landed("test_b.py"), out)
        base_runs = [x for x in self.gate_args() if x == "test_doctor.Strong"]
        self.assertLessEqual(len(base_runs), 1, f"probed alone on the merged tree only: {self.gate_args()}")


if __name__ == "__main__":
    unittest.main()
