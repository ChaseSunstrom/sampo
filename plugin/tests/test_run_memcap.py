"""T-6926: each run.py class runs under a memory cap. Its processes get SAMPO_TEST_MEM_MB of data each (RLIMIT_DATA,
default 6144 MB, only ever lowered; 0 turns it off), oom_score_adj 500 (only ever raised) and no core dump (RLIMIT_CORE
1), and what they start inherits all three. A runaway class then fails alone, by name and with its peak RSS, and the
kernel takes a test worker before the session if memory still runs out. On 2026-11-25 at 22:34 one class's busy loop
over a mocked sleep reached 38.7 GB and the global OOM kill stopped the whole tmux scope, the lead session with it."""
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

NESTED = ("SAMPO_SLOT", "FOREMAN_SLOT", "SAMPO_SLOTS", "FOREMAN_SLOTS", "SAMPO_NO_TEST_CACHE", "FOREMAN_NO_TEST_CACHE",
          "SAMPO_CACHE_FULL", "FOREMAN_CACHE_FULL", "SAMPO_TEST_MEM_MB", "FOREMAN_TEST_MEM_MB")


def capped(mb):
    """The data limit a cap of mb gives here: the cap is only ever lowered, so a run already under a tighter one (this
    suite's own cap, a prlimit) keeps that."""
    soft = resource.getrlimit(resource.RLIMIT_DATA)[0]
    return mb << 20 if soft == resource.RLIM_INFINITY else min(mb << 20, soft)


HOG = '''import unittest


class Hog(unittest.TestCase):
    def test_it_fills_400_mb(self):
        b = b"x" * (400 << 20)  # every page written, so it is resident, not only reserved
        self.assertEqual(len(b), 400 << 20)
'''

QUICK = '''import unittest


class Quick(unittest.TestCase):
    def test_it_passes(self):
        self.assertTrue(True)
'''

LIMITS = '''import os
import resource
import subprocess
import sys
import unittest

PROBE = ("import resource; print(resource.getrlimit(resource.RLIMIT_DATA)[0]); print(open('/proc/self/oom_score_adj').read().strip());"
         " print(resource.getrlimit(resource.RLIMIT_CORE)[0])")


class Limits(unittest.TestCase):
    def test_the_class_and_its_children_carry_the_cap_and_the_oom_score(self):
        want = int(os.environ["WANT_DATA"])
        self.assertEqual(resource.getrlimit(resource.RLIMIT_DATA)[0], want)
        with open("/proc/self/oom_score_adj") as f:
            self.assertGreaterEqual(int(f.read()), int(os.environ["WANT_SCORE"]))
        self.assertIn(resource.getrlimit(resource.RLIMIT_CORE)[0], (0, 1))  # 1: a piped core_pattern starts no helper
        out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(int(out[0]), want)
        self.assertGreaterEqual(int(out[1]), int(os.environ["WANT_SCORE"]))
        self.assertIn(int(out[2]), (0, 1))
'''


@unittest.skipUnless(sys.platform.startswith("linux"), "RLIMIT_DATA bounds mmap and /proc/self/oom_score_adj exist on Linux")
class MemCap(unittest.TestCase):
    """The real run.py in a scratch repo, with toy classes."""

    def setUp(self):
        top = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, top, True)
        self.repo = os.path.join(top, "repo")
        self.tests = os.path.join(self.repo, "plugin", "tests")
        os.makedirs(self.tests)
        for name in ("run.py", "runcover.py"):
            shutil.copy(os.path.join(run.HERE, name), os.path.join(self.tests, name))
        for name, text in (("test_hog.py", HOG), ("test_quick.py", QUICK), ("test_limits.py", LIMITS)):
            with open(os.path.join(self.tests, name), "w") as f:
                f.write(text)
        git = ["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.test"]
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        self.env = dict({k: v for k, v in os.environ.items() if k not in NESTED},
                        XDG_CACHE_HOME=os.path.join(top, "cache"), FOREMAN_SLOTS="0",
                        FM_TEST_TIMES=os.path.join(top, "t.json"))

    def go(self, *names, preexec=None, **env):
        return subprocess.run([sys.executable, os.path.join(self.tests, "run.py"), *names], capture_output=True,
                              text=True, timeout=300, cwd=self.repo, env=dict(self.env, **env), preexec_fn=preexec)

    def test_a_class_over_its_cap_fails_alone_by_name_with_its_peak(self):
        r = self.go("test_hog", "test_quick", SAMPO_TEST_MEM_MB="200")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("FAILED (1 classes: test_hog.Hog)", r.stderr)  # the quick class passed beside it
        self.assertRegex(r.stderr, r"==== test_hog\.Hog\n(?s:.*)MemoryError")
        self.assertRegex(r.stderr, r"run\.py: test_hog\.Hog over its memory cap \(200 MB of data a process, "
                                   r"SAMPO_TEST_MEM_MB; 0 turns it off\): peak RSS \d+ MB \(T-6926\)")

    def test_0_turns_the_cap_off(self):
        r = self.go("test_hog", "test_quick", SAMPO_TEST_MEM_MB="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("memory cap", r.stderr)

    def test_with_the_cap_off_the_oom_score_and_no_core_still_hold(self):
        r = self.go("test_limits", SAMPO_TEST_MEM_MB="0", WANT_DATA=str(resource.getrlimit(resource.RLIMIT_DATA)[0]),
                    WANT_SCORE="500")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_cap_and_the_oom_score_reach_the_class_and_its_children(self):
        r = self.go("test_limits", SAMPO_TEST_MEM_MB="3000", WANT_DATA=str(capped(3000)), WANT_SCORE="500")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_default_cap_is_6144_mb(self):
        r = self.go("test_limits", WANT_DATA=str(capped(run.MEM_MB)), WANT_SCORE="500")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(run.MEM_MB, 6144)

    def test_a_tighter_limit_already_set_stands(self):
        hard = resource.getrlimit(resource.RLIMIT_DATA)[1]

        def tight():  # the run starts under a 2500 MB soft limit: a 3000 MB cap must not raise it
            resource.setrlimit(resource.RLIMIT_DATA, (2500 << 20, hard))
        r = self.go("test_limits", preexec=tight, SAMPO_TEST_MEM_MB="3000", WANT_DATA=str(capped(2500)), WANT_SCORE="500")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_cap_past_what_the_kernel_takes_leaves_the_rest_standing(self):
        """A SAMPO_TEST_MEM_MB past rlim_t under no data limit: setrlimit raises OverflowError; the class still runs,
        with no core dump and its OOM score, rather than dying before its first test."""
        hard = resource.getrlimit(resource.RLIMIT_DATA)[1]
        if hard != resource.RLIM_INFINITY:
            self.skipTest("a finite hard data limit clamps the cap: no overflow to reach")

        def unlimited():
            resource.setrlimit(resource.RLIMIT_DATA, (resource.RLIM_INFINITY, hard))
        r = self.go("test_limits", preexec=unlimited, SAMPO_TEST_MEM_MB=str(10 ** 20),
                    WANT_DATA=str(resource.RLIM_INFINITY), WANT_SCORE="500")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_higher_oom_score_already_set_stands(self):
        def high():
            with open("/proc/self/oom_score_adj", "w") as f:
                f.write("800")
        r = self.go("test_limits", preexec=high, SAMPO_TEST_MEM_MB="3000", WANT_DATA=str(capped(3000)), WANT_SCORE="800")
        self.assertEqual(r.returncode, 0, r.stderr)


class CapKnob(unittest.TestCase):
    """mem_cap_mb reads the knob: a bad value keeps the default, a negative one is off."""

    def test_the_knob(self):
        for value, want in (("", run.MEM_MB), ("512", 512), ("0", 0), ("-3", 0), ("lots", run.MEM_MB)):
            with mock.patch.dict(os.environ, {"SAMPO_TEST_MEM_MB": value}):
                os.environ.pop("FOREMAN_TEST_MEM_MB", None)
                self.assertEqual(run.mem_cap_mb(), want, value)


if __name__ == "__main__":
    unittest.main()
