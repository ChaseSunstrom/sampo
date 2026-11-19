"""T-5047: the train's affected-tests runs (the base probe and each lane's probe) are stamped with a run id, and when they
go red the reason names each failing test with its first exception line from that run's record (run.py --last, T-4268),
so nobody reruns to learn them. No record (a runner that is not tests/run.py, one that left none): the notes read as
before."""
# map-links: plugin/lib/sampo/train.py, plugin/tests/test_lane_train.py
import os
import unittest

from test_lane_train import TrainCase

# a stand-in for plugin/tests/run.py: runs unittest on its arguments; when stamped with SAMPO_TEST_RUN_ID and red it leaves
# a record named by that id (unless NO_RECORD), and `--last --json --run-id ID` prints that record, exit 3 when there is none
FAKE_RUN = '''import json, os, re, subprocess, sys
REC = {rec!r}
args = sys.argv[1:]
if "--last" in args:
    path = os.path.join(REC, args[args.index("--run-id") + 1] + ".json")
    if not os.path.exists(path):
        sys.exit(3)
    print(open(path).read())
    sys.exit(0)
env = dict(os.environ, PYTHONPATH="plugin/tests" + os.pathsep + os.environ.get("PYTHONPATH", ""))
r = subprocess.run([sys.executable, "-m", "unittest", *args], capture_output=True, text=True, env=env)
sys.stderr.write(r.stderr)
rid = os.environ.get("SAMPO_TEST_RUN_ID")
if rid and r.returncode and not os.environ.get("NO_RECORD"):
    exc = re.search(r"(?m)^(\\w*Error: .*)$", r.stderr)
    failed = [{{"class": m.group(2), "test": m.group(1), "first": exc.group(1) if exc else "", "text": ""}}
              for m in re.finditer(r"(?m)^(?:FAIL|ERROR): (\\w+) \\(([\\w.]+)\\.\\1\\)$", r.stderr)]
    with open(os.path.join(REC, rid + ".json"), "w") as f:
        json.dump({{"ok": False, "failed": failed}}, f)
sys.exit(r.returncode)
'''
RED = ("import unittest\n\n\nclass {0}(unittest.TestCase):\n    def test_it(self):\n"
       "        self.assertEqual('seven', 'eight')\n")
FLAKY = ("import os, unittest\n\n\nclass Flaky(unittest.TestCase):\n    def test_it(self):\n"
         "        first = not os.path.exists({0!r})\n        open({0!r}, 'w').close()\n"
         "        self.assertFalse(first, 'a load flake')\n")
RUN = "python3 plugin/tests/run.py"


class FirstFailure(TrainCase):
    def setUp(self):
        super().setUp()
        self.rec = os.path.join(self.tmp, "records")
        os.makedirs(self.rec)
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write(FAKE_RUN.format(rec=self.rec))
        self.git("add", "-A")
        self.git("commit", "-qm", "a runner")
        self.fm("check", "affected", RUN + " {tests}")

    def red_base(self):
        with open(os.path.join(self.repo, "plugin", "tests", "test_doctor.py"), "w") as f:
            f.write(RED.format("Doctor"))
        self.git("add", "-A")
        self.git("commit", "-qm", "red base")
        return self.git("rev-parse", "HEAD")

    def base_red(self, cmd=RUN):
        from sampo import train
        sub = (cmd, cmd + " {names}", ["plugin/tests/test_doctor.py"])
        return train._base_red_once(self.repo, self.git("rev-parse", "HEAD"), sub)


class BaseProbe(FirstFailure):
    def test_a_red_base_names_its_failing_test_and_exception_from_the_record(self):
        self.red_base()
        why = self.base_red()
        self.assertIn("failing test_doctor.Doctor", why)
        self.assertIn("test_it: AssertionError: 'seven' != 'eight'", why)
        self.assertEqual(len(os.listdir(self.rec)), 2, "the run and its flake rerun each left a record under their own id")

    def test_no_record_the_reason_reads_as_before(self):
        self.red_base()
        os.environ["NO_RECORD"] = "1"
        try:
            why = self.base_red()
        finally:
            del os.environ["NO_RECORD"]
        self.assertIn(f"{RUN} {{names}} on 1 affected test module(s), failing test_doctor.Doctor\n", why)
        self.assertNotIn("test_it:", why.splitlines()[0])
        self.assertEqual(os.listdir(self.rec), [])

    def test_a_runner_that_is_not_run_py_is_never_stamped_and_reads_as_before(self):
        self.red_base()
        with open(os.path.join(self.repo, "other.py"), "w") as f:
            f.write(FAKE_RUN.format(rec=self.rec))
        self.git("add", "-A")
        self.git("commit", "-qm", "another runner")
        why = self.base_red("python3 other.py")
        self.assertIn("python3 other.py {names} on 1 affected test module(s), failing test_doctor.Doctor\n", why)
        self.assertNotIn("AssertionError", why.splitlines()[0])
        self.assertEqual(os.listdir(self.rec), [])


class LaneProbe(FirstFailure):
    def test_a_blamed_lane_names_its_failing_test_and_exception(self):
        bad = self.lane(1, "test_bad.py", RED.format("Bad"))
        r = self.train(bad, check=False)  # the only lane is left out: nothing to merge
        self.assertIn(f"{bad}: its affected tests fail with the lane merged onto the base: "
                      "test_bad.Bad (test_it: AssertionError: 'seven' != 'eight')", r.stdout + r.stderr)

    def test_a_flaky_note_names_the_first_run_failure_from_its_record(self):
        a = self.lane(1, "test_flaky.py", FLAKY.format(os.path.join(self.tmp, "flaked")))
        r = self.train(a)
        self.assertIn("flaky (red once, green on a rerun): test_flaky.Flaky (test_it: AssertionError: True is not false", r.stdout)
        self.assertNotIn(f"left out {a}", r.stdout)

    def test_no_record_the_lane_notes_read_as_before(self):
        bad = self.lane(1, "test_bad.py", RED.format("Bad"))
        r = self.train(bad, check=False, env={"NO_RECORD": "1"})
        out = r.stdout + r.stderr
        self.assertIn(f"{bad}: its affected tests fail with the lane merged onto the base: test_bad.Bad", out)
        self.assertNotIn("test_bad.Bad (", out)
        self.assertEqual(os.listdir(self.rec), [])


if __name__ == "__main__":
    unittest.main()
