"""T-1145: a verify that names a test module or script file that doesn't exist yet is kept with a note (test-first
names tests before they exist); a missing npm script is refused when the criterion is added or edited."""
import os
from helpers import ForemanTestCase


class VerifyCheck(ForemanTestCase):
    def put(self, rel, text=""):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def add(self, verify):
        self.fm("init", check=False)
        self.fm("task", "new", "x", "--type", "FIX", "--tier", "S", "--step", "s", "--focus", check=False)
        return self.fm("task", "ac", "T-0001", "add", "works", "--verify", verify, check=False)

    def test_a_missing_module_or_file_warns_and_stores(self):
        for verify, named in (("python3 tests/run.py test_nowhere_x", "test_nowhere_x.py"),
                              ("python3 -m unittest test_nowhere_x", "test_nowhere_x.py"),
                              ("python3 tools/nope.py", "tools/nope.py"),
                              ("bash scripts/nope.sh", "scripts/nope.sh")):
            with self.subTest(verify=verify):
                self.setUp()
                self.put("tests/run.py")
                self.put(".claude/worktrees/agent-x/tests/test_nowhere_x.py")  # a builder's copy isn't this tree's
                r = self.add(verify)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"note: {named} doesn't exist yet", r.stderr)
                self.assertIn(verify, self.fm("task", "show", "T-0001").stdout)

    def test_a_missing_npm_script_is_refused(self):
        self.put("web/package.json", '{"scripts": {"test": "x"}}')
        r = self.add("cd web && npm run lint")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("lint", r.stderr)

    def test_real_cd_and_unknown_pass(self):
        for verify in ("python3 tests/run.py test_real", "python3 -m unittest tests.test_real", "python3 tools/ok.py",
                       "cd web && npm test", "cd web && npm run lint", "cargo test nothing_x", "python3 -c 'pass'",
                       "python3 tests/run.py -k test_some_case_name",  # T-1190: -k takes a test-name pattern
                       "python3 -m unittest -k test_some_case_name tests.test_real"):
            with self.subTest(verify=verify):
                self.setUp()
                self.put("tests/run.py")
                self.put("tests/test_real.py")
                self.put("tools/ok.py")
                self.put("web/package.json", '{"scripts": {"test": "x", "lint": "y"}}')
                r = self.add(verify)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("doesn't exist yet", r.stderr)  # T-1145: the real file and module exist
                if verify.startswith("cd "):  # T-4139: cd … && is a form an isolated worktree agent can't run typed
                    self.assertIn("sampo: note: this verify holds bash -c, cd", r.stderr)
                    self.assertNotIn("has no npm script", r.stderr)  # T-1133: the script it names exists
                    self.assertEqual(r.stderr.count("note:"), 1, r.stderr)  # the worktree note and nothing else
                else:
                    self.assertNotIn("note:", r.stderr)

    def test_task_new_ac_warns_and_keeps(self):
        self.fm("init")
        r = self.fm("task", "new", "x", "--type", "FIX", "--tier", "S", "--ac", "w :: python3 tools/nope.py",
                    "--step", "s")
        self.assertIn("note: tools/nope.py doesn't exist yet", r.stderr)
