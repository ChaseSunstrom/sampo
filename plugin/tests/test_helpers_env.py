"""T-3185: an exported GIT_DIR must never steer the tests' git calls at a real repo.

At 02:54 on 2026-11-05 a test run inherited GIT_DIR and wrote core.bare=true and the fixture identity
t@example.com into the project's own .git/config; every merge into main then failed."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LEAKY = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")


class GitEnvIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fm-gitenv-")
        self.real = os.path.join(self.tmp, "real")
        subprocess.run(["git", "init", "-q", self.real], check=True)

    def run_with(self, code):
        env = {**os.environ, "GIT_DIR": os.path.join(self.real, ".git"), "GIT_WORK_TREE": self.real,
               "PYTHONPATH": HERE}
        return subprocess.run([sys.executable, "-c", code], env=env, cwd=HERE, capture_output=True, text=True,
                              timeout=60)

    def real_config(self, key):
        return subprocess.run(["git", "-C", self.real, "config", "--local", "--get", key], capture_output=True,
                              text=True).stdout.strip()

    def test_importing_the_helpers_drops_every_variable_that_redirects_git(self):
        r = self.run_with("import os, helpers; print([k for k in %r if k in os.environ])" % (LEAKY,))
        self.assertEqual(r.stdout.strip(), "[]", r.stderr[-800:])

    def test_a_scratch_repo_made_under_an_exported_git_dir_leaves_the_real_repo_alone(self):
        r = self.run_with("import os, helpers; helpers.git_repo(%r)" % self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertEqual(self.real_config("user.email"), "")
        self.assertNotEqual(self.real_config("core.bare"), "true")


class ScratchRepoCost(unittest.TestCase):
    """T-3581: every test's scratch repo cost 5 git processes (init, 2x config, add, commit), ~35,000 a suite."""

    def setUp(self):
        sys.path.insert(0, HERE)
        import helpers
        self.helpers, self.tmp = helpers, tempfile.mkdtemp(prefix="fm-repocost-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def git(self, root, *a):
        return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True)

    def test_a_repo_after_the_processes_first_costs_one_git_process(self):
        self.helpers.git_repo(self.tmp, "warm")
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as run:
            self.helpers.git_repo(self.tmp, "app")
        self.assertEqual(run.call_count, 1, run.call_args_list)

    def test_each_repo_reads_clean_and_makes_its_own_init_commit_when_called(self):
        self.helpers.git_repo(self.tmp, "warm")
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": "@1000000000 +0000"}):
            root = self.helpers.git_repo(self.tmp, "app")
        self.assertEqual(self.git(root, "log", "--format=%ct %ae %an %s").stdout.split("\n")[0],
                         "1000000000 t@example.com t init")
        self.assertEqual(self.git(root, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(self.git(root, "branch", "--show-current").stdout.strip(), "main")
        self.assertEqual(self.git(root, "config", "user.email").stdout.strip(), "t@example.com")
        self.assertEqual(self.git(root, "diff-files", "--quiet").returncode, 0, "a copied index reads dirty")
        self.assertEqual(self.git(root, "diff-index", "--quiet", "HEAD").returncode, 0)
        self.assertEqual(self.git(root, "status", "--porcelain").stdout, "")
        with open(os.path.join(root, "README.md"), "w") as f:
            f.write("# APP\n")  # same size, same second: still seen
        self.assertEqual(self.git(root, "diff-files", "--name-only").stdout.strip(), "README.md")
        with open(os.path.join(self.tmp, "warm", "README.md")) as f:
            self.assertEqual(f.read(), "# app\n", "an edit in one repo reached another")

    def test_a_seeded_repo_is_init_then_its_seed_commit_made_now_in_one_git_process(self):
        # T-3678: a class's seed files (test_code's calc.py) ride in its template: 1 git process a test, not 3
        seed = ("seed the bug", (("calc.py", "def add(a, b):\n    return a - b\n"), ("pkg/t.py", "x = 1\n")))
        warm = self.helpers.git_repo(self.tmp, "warm", seed=seed)
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as run, \
                mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": "@1000000000 +0000"}):
            root = self.helpers.git_repo(self.tmp, "app", seed=seed)
        self.assertEqual(run.call_count, 1, run.call_args_list)
        log = self.git(root, "log", "--format=%ct %s").stdout.split("\n")
        self.assertEqual((log[0], log[1].split(" ", 1)[1]), ("1000000000 seed the bug", "init"))
        self.assertEqual(self.git(root, "show", "--format=", "--name-only", "HEAD").stdout.split(), ["calc.py", "pkg/t.py"])
        self.assertEqual(self.git(root, "diff-files", "--quiet").returncode, 0, "a copied index reads dirty")
        self.assertEqual(self.git(root, "diff-index", "--quiet", "HEAD").returncode, 0)
        self.assertEqual(self.git(root, "status", "--porcelain").stdout, "")
        with open(os.path.join(root, "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")  # same size, same second: still seen
        self.assertEqual(self.git(root, "diff-files", "--name-only").stdout.strip(), "calc.py")
        for other in (warm, self.helpers._repo_template(seed)):
            with open(os.path.join(other, "calc.py")) as f:
                self.assertIn("a - b", f.read(), "an edit in one repo reached another")
        self.assertNotIn("calc.py", os.listdir(self.helpers._repo_template()), "an unseeded repo gets no seed")

    def test_a_scratch_repos_commits_start_no_background_maintenance(self):
        # T-3678: each commit forked a detached `git maintenance run --auto` (a git process a commit), and the seeded
        # template's raced the next copy: its maintenance.lock vanished mid-copytree
        trace = os.path.join(self.tmp, "trace")
        with mock.patch.dict(os.environ, {"GIT_TRACE": trace}):
            root = self.helpers.git_repo(self.tmp, "app", seed=("s", (("a.py", "x\n"),)))
            self.git(root, "commit", "-q", "--allow-empty", "-m", "later")
        with open(trace) as f:
            said = f.read()
        self.assertIn("commit", said)
        self.assertNotIn("maintenance", said)


class GateKeyIsolation(unittest.TestCase):
    def test_an_in_process_gate_signs_with_the_tests_own_key_not_the_users(self):
        # T-3195: test_lanes.SelfChangeGate read the real ~/.config/sampo/selfgate.key
        sys.path.insert(0, HERE)
        from helpers import SampoTestCase
        from sampo import selfgate

        class T(SampoTestCase):
            def runTest(self):
                pass
        t = T()
        t.setUp()
        try:
            self.assertTrue(selfgate._keyfile().startswith(t.tmp + os.sep), selfgate._keyfile())
        finally:
            t.doCleanups()
            t.tearDown()


if __name__ == "__main__":
    unittest.main()
