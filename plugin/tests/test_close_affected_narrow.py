# map-links: plugin/lib/sampo/cli.py
"""T-4684: a change to ONE test module affects that module only. No code imports a test, so the run cache's "this class
read the file" records and the co-change pairs (a changed tests/test_stale_git_lock.py once listed 64 modules, about 25
minutes, then a timeout) say nothing about it. What really imports a changed test-folder module (a helper, read from the
imports, never from the file's name) still runs. A close after a gate pass on the same file content reuses it and says so;
a file changed after the gate runs again."""
import os
import shutil
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c

HERE = os.path.dirname(os.path.abspath(__file__))
CASE = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue({n})\n"


class Narrow(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp, "xdg-cache")
        patch = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache})
        patch.start()
        self.addCleanup(patch.stop)
        self.fm("init")
        self.fm("check", "affected", "python3 tests/run.py {names}")
        self.p = c.find_project(self.repo, create=False)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
                              check=True, capture_output=True, text=True).stdout

    def write(self, rel, text):
        os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def seed(self, files):
        for rel, text in files.items():
            self.write(rel, text)
        self.write(".gitignore", "__pycache__/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "suite")

    def record(self, **classes):
        """The run cache as a run leaves it: each class (test_x.X) with the files its last green run read; '*' is a child
        the recorder couldn't follow, which is how a class that reads a whole folder shows up."""
        import run
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        run.save_cache(run.cache_path(os.path.realpath(common)), {cid: ["d", 2, keys] for cid, keys in classes.items()}, 0)

    def focus(self):
        self.fm("task", "new", "note it", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s", "--focus")

    def names(self):
        _, tests, _ = cli.affected(self.p)
        return sorted(os.path.basename(t) for t in tests)

    def test_a_changed_test_module_affects_itself_only(self):
        self.seed({"plugin/tests/test_a.py": CASE.format(n="1"), "plugin/tests/test_b.py": CASE.format(n="2"),
                   "plugin/lib/mod.py": "X = 1\n"})
        # every other class once read the changed file whole (a scan of the folder, a child it could not follow)
        self.record(**{"test_b.T": ["*", "plugin/tests/test_a.py", "plugin/tests/test_b.py"]})
        self.focus()
        self.write("plugin/tests/test_a.py", CASE.format(n="3"))
        self.assertEqual(self.names(), ["test_a.py"])

    def test_the_real_importers_of_a_helper_run_whatever_the_file_is_called(self):
        self.seed({
            "plugin/tests/test_a.py": CASE.format(n="1"),
            "plugin/tests/test_support.py": "VALUE = 1\n",  # named like a test, used as a helper
            "plugin/tests/test_c.py": "import test_support\n" + CASE.format(n="test_support.VALUE"),
            "plugin/tests/test_d.py": "from test_support import VALUE\n" + CASE.format(n="VALUE"),
            "plugin/tests/mid.py": "import test_support\n",  # a helper's helper
            "plugin/tests/test_e.py": "from mid import test_support\n" + CASE.format(n="1"),
            "plugin/tests/test_f.py": "# test_support is only named in a comment\n" + CASE.format(n="1"),
            "plugin/tests/test_g.py": CASE.format(n="1"),
        })
        self.record(**{"test_g.T": ["*", "plugin/tests/test_support.py", "plugin/tests/test_g.py"]})
        self.focus()
        self.write("plugin/tests/test_support.py", "VALUE = 2\n")
        got = self.names()
        self.assertEqual(got, ["test_c.py", "test_d.py", "test_e.py", "test_support.py"])

    def test_a_new_test_module_still_brings_the_repo_shape_rules(self):
        self.seed({"plugin/tests/test_a.py": CASE.format(n="1"), "plugin/tests/test_suite_hygiene.py": CASE.format(n="1")})
        self.focus()
        self.write("plugin/tests/test_new.py", CASE.format(n="1"))
        self.assertEqual(self.names(), ["test_new.py", "test_suite_hygiene.py"])
        self.write("plugin/tests/test_a.py", CASE.format(n="9"))
        self.git("add", "-A")
        self.git("commit", "-qm", "the new one lands")
        self.fm("task", "new", "second", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s", "--focus")
        self.write("plugin/tests/test_a.py", CASE.format(n="10"))
        self.assertEqual(self.names(), ["test_a.py"], "an existing module changed: that module only")

    def test_a_source_change_still_brings_what_ran_it(self):
        self.seed({"plugin/lib/mod.py": "X = 1\n", "plugin/tests/test_a.py": CASE.format(n="1"),
                   "plugin/tests/test_b.py": CASE.format(n="1")})
        self.record(**{"test_b.T": ["*", "plugin/lib/mod.py", "plugin/tests/test_b.py"]})
        self.focus()
        self.write("plugin/lib/mod.py", "X = 2\n")
        self.assertIn("test_b.py", self.names())


class Close(ForemanTestCase):
    """A close of a test-only change after a train's gate pass that ran that module."""

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp, "xdg-cache")
        patch = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache})
        patch.start()
        self.addCleanup(patch.stop)
        self.fm("init")
        self.count = os.path.join(self.tmp, "count")
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(self.repo, "tests", "run.py"))
        for mod in ("calc", "other"):
            self.write(f"tests/test_{mod}.py", self.body(mod, 1))
        self.write(".gitignore", "__pycache__/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "suite")
        self.fm("check", "add", "python3 tests/run.py")
        self.fm("check", "affected", "python3 tests/run.py {names}")
        self.p = c.find_project(self.repo)

    def body(self, mod, n):
        return ("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n"
                f"        open({self.count!r}, 'a').write('{mod}\\n')\n        self.assertEqual({n}, 1)\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
                              check=True, capture_output=True, text=True).stdout

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def runs(self):
        try:
            with open(self.count) as f:
                return len(f.read().split())
        except OSError:
            return 0

    def finish(self, tid):
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False,
                    env={"FOREMAN_SENTINEL": "off"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def start(self):
        import run
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        # test_other once read test_calc whole: before T-4684 that made it part of a change to test_calc
        run.save_cache(run.cache_path(os.path.realpath(common)),
                       {"test_other.T": ["d", 2, ["*", "tests/test_calc.py", "tests/test_other.py"]]}, 0)
        tid = self.fm_json("task", "new", "Edit a test", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", "tests/test_calc.py")["id"]
        self.fm("focus", tid)
        self.write("tests/test_calc.py", self.body("calc", 1) + "# tidy\n")
        return tid

    def gate(self):
        """A train's pass on this code: its suite gate ran test_calc only (the module the task changed)."""
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        d = c.ledger_events(self.p, ("check_run",))[-1]["data"]
        rows = [dict(r, affected=["test_calc"]) if "run.py" in r["cmd"] else r for r in d["results"]]
        c.log_event(self.p, "check_run", data=dict(d, results=rows, train=["T-9999"]))

    def test_a_close_after_a_gate_pass_on_the_same_content_reuses_it_and_says_so(self):
        tid = self.start()
        self.gate()
        before = self.runs()
        out = self.finish(tid)
        self.assertEqual(self.runs(), before, "the module ran again on content the gate just passed")
        self.assertIn("linked tests: covered by the gates' pass (run ", out)
        self.assertIn("the suite on 1 affected module(s)", out)

    def test_a_file_changed_after_the_gate_runs_again(self):
        tid = self.start()
        self.gate()
        self.write("tests/test_calc.py", self.body("calc", 1) + "# changed after the gate\n")
        before = self.runs()
        out = self.finish(tid)
        self.assertEqual(self.runs(), before + 1, "only the changed module, once")
        self.assertNotIn("covered by the gates' pass", out)
        self.assertIn("linked tests: no reusable gate pass", out)


if __name__ == "__main__":
    unittest.main()
