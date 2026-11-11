"""T-4197: the test runs Sampo starts for a lane (sampo lane verify, sampo ship's test run, and sampo check in a lane or
builder worktree) start at nice 10, as the train and task-close runs do, so they never compete with the user's session.
The main checkout's foreground check keeps the caller's priority. Each test runs a probe that writes its own nice
value (os.getpriority) into the folder it runs in: the real launcher, no argv mocks."""
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes
from sampo import release

PROBE = "import os\nopen('probe.out', 'w').write(str(os.getpriority(os.PRIO_PROCESS, 0)))\n"


def mine():
    return os.getpriority(os.PRIO_PROCESS, 0)


def lane_nice(base):
    return min(19, base + 10)  # nice 10 adds to the caller's own nice value, capped at 19


@unittest.skipUnless(shutil.which("nice"), "no nice on this host: nothing starts niced")
class NiceRuns(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.write("probe.py", PROBE)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "probe"], check=True)
        self.probe = f"{sys.executable} probe.py"
        self.base = mine()

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def probed(self, folder):
        with open(os.path.join(folder, "probe.out")) as f:
            return int(f.read())

    def test_the_shared_wrapper_puts_the_command_at_nice_10(self):
        argv = c.nice_argv(["bash", "-c", "true"])
        self.assertEqual(argv[1:4], ["-n", "10", "bash"])
        self.assertEqual(argv[-2:], ["-c", "true"])

    def test_run_command_niced_starts_its_command_at_nice_10(self):
        code, _ = c.run_command(self.repo, self.probe, 60, niced=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.probed(self.repo), lane_nice(self.base))

    def test_run_command_without_niced_keeps_the_callers_priority(self):
        code, _ = c.run_command(self.repo, self.probe, 60)
        self.assertEqual(code, 0)
        self.assertEqual(self.probed(self.repo), self.base)

    def test_lane_verify_runs_its_green_check_niced(self):
        sha = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True).stdout.strip()
        # _pm_cmd_green_at runs its check in a throwaway worktree that is gone after: the check's own exit says the
        # nice value (green only at nice 10), so a launcher that doesn't nice it is red here
        probe = (f"{sys.executable} -c \"import os,sys; "
                 f"sys.exit(0 if os.getpriority(os.PRIO_PROCESS, 0) == {lane_nice(self.base)} else 1)\"")
        with mock.patch.object(c, "wait_for_load", lambda *a, **k: None):
            self.assertTrue(lanes._pm_cmd_green_at(self.repo, sha, probe))

    def test_ship_test_run_is_niced(self):
        log = os.path.join(self.tmp, "ship.log")
        with mock.patch.object(release, "_affected_first", lambda *a, **k: []):
            code, _ = release._run_test(None, self.repo, "HEAD", dict(os.environ), [sys.executable, "-c",
                                        f"import os,sys; sys.exit(0 if os.getpriority(os.PRIO_PROCESS, 0) == "
                                        f"{lane_nice(self.base)} else 1)"], log)
        self.assertEqual(code, 0)

    def test_a_lane_check_runs_its_gates_niced(self):
        self.fm("check", "add", self.probe)
        lane = os.path.join(self.tmp, "lane-one")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "lane-one", lane], check=True)
        self.fm("check", cwd=lane)
        self.assertEqual(self.probed(lane), lane_nice(self.base))

    def test_the_main_checkout_check_keeps_its_priority(self):
        self.fm("check", "add", self.probe)
        self.fm("check", cwd=self.repo)
        self.assertEqual(self.probed(self.repo), self.base)
