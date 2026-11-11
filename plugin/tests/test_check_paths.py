"""T-0126: a gate can declare the paths it covers; fm check skips it when nothing under them changed since its last
pass (and says so), runs it when a covered file changed, and --fresh runs everything; a gate without paths always runs.
T-1101: another open task's red-first test file is left out of the suite gate, and fm check says so."""
import json
import os
import shutil
import subprocess
import sys
import unittest

from helpers import ForemanTestCase

HERE = os.path.dirname(os.path.abspath(__file__))


class CheckPaths(ForemanTestCase):
    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def count(self, name):
        path = os.path.join(self.tmp, name)
        return open(path).read().count("x") if os.path.exists(path) else 0

    def test_a_gate_runs_only_when_its_paths_changed(self):
        self.fm("init")
        self.write("src/app.py", "a = 1\n")
        self.fm("check", "add", f"echo x >> {os.path.join(self.tmp, 'src-gate')}")
        self.fm("check", "add", f"echo x >> {os.path.join(self.tmp, 'all-gate')}")
        self.fm("check", "paths", "1", "src/**")
        self.assertIn("src/**", self.fm("check", "list").stdout)
        self.fm("check")
        self.assertEqual((self.count("src-gate"), self.count("all-gate")), (1, 1))
        self.write("docs/notes.md", "n\n")  # outside src/
        out = self.fm("check").stdout
        self.assertEqual((self.count("src-gate"), self.count("all-gate")), (1, 2))
        self.assertIn("skipped: nothing under src/** changed", out)
        self.write("src/app.py", "a = 2\n")
        self.fm("check")
        self.assertEqual(self.count("src-gate"), 2)
        self.write("docs/notes.md", "m\n")
        self.fm("check", "--fresh")
        self.assertEqual(self.count("src-gate"), 3)

    def test_a_rename_out_of_the_paths_runs_it_and_skips_keep_the_real_runs_time(self):
        from sampo import core as c
        self.fm("init")
        self.write("src/app.py", "a = 1\n")
        self.fm("check", "add", f"echo x >> {os.path.join(self.tmp, 'src-gate')}")
        self.fm("check", "paths", "1", "src/**")
        self.fm("check")
        first = [e for e in c.ledger_tail(c.find_project(self.repo), 50) if e.get("event") == "check_run"][-1]["ts"]
        self.write("docs/a.md", "a\n")
        self.fm("check")  # skipped
        self.write("docs/a.md", "b\n")
        self.fm("check")  # skipped again: it still ages from the first, real run (review)
        last = [e for e in c.ledger_tail(c.find_project(self.repo), 50) if e.get("event") == "check_run"][-1]
        self.assertEqual(last["data"]["results"][0]["since"], first)
        os.makedirs(os.path.join(self.repo, "lib"))
        os.rename(os.path.join(self.repo, "src", "app.py"), os.path.join(self.repo, "lib", "app.py"))
        self.fm("check")
        self.assertEqual(self.count("src-gate"), 2, "a file moved out of src/ changed src/")

    def test_a_write_by_another_gate_under_its_paths_runs_it_next_time(self):
        self.fm("init")
        self.write("src/app.py", "a = 1\n")
        self.fm("check", "add", f"echo x >> {os.path.join(self.tmp, 'src-gate')}")
        self.fm("check", "add", "date +%N >> src/gen.py")  # a gate that writes (codegen)
        self.fm("check", "paths", "1", "src/**")
        self.fm("check")
        self.write("docs/a.md", "a\n")
        self.fm("check")  # gate 2 rewrote src/gen.py after gate 1 ran
        self.write("docs/a.md", "b\n")
        self.fm("check")
        self.assertEqual(self.count("src-gate"), 3, "T-1963: gate 1 never saw a src/gen.py gate 2 wrote, so it reruns")


RED = "import unittest\n\n\nclass Red(unittest.TestCase):\n    def test_red(self):\n        self.fail('not yet')\n"
GREEN = "import unittest\n\n\nclass Green(unittest.TestCase):\n    def test_green(self):\n        pass\n"


class CheckSkip(ForemanTestCase):
    """T-1101: another open task's red-first test file (new, failing in its step-1 evidence) doesn't fail this task's
    fm check: run.py leaves out the modules FOREMAN_SKIP_TESTS names, and fm check names what it left out."""
    write = CheckPaths.write

    def suite(self, commit_red=False):
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(self.repo, "tests", "run.py"))
        self.write("tests/test_green.py", GREEN)
        self.write("tests/test_red.py", RED)  # task B's new test
        git = ["git", "-C", self.repo]
        subprocess.run(git + ["add", "tests" if commit_red else "tests/run.py", "tests/test_green.py"], check=True)
        subprocess.run(git + ["commit", "-qm", "suite"], check=True)

    def task(self, title, *extra):
        tid = self.fm_json("task", "new", title, "--type", "FEATURE", "--tier", "S", "--step", "red",
                           "--ac", "works :: true", *extra)["id"]
        self.fm("focus", tid)
        return tid

    def red_elsewhere(self, *a_extra, commit_red=False, b_cmd="python3 tests/run.py test_red"):
        """Task B recorded test_red failing at step 1 and was checkpointed; task A is active: A's fm check."""
        self.fm("init")
        self.suite(commit_red)
        self.fm("check", "add", "python3 tests/run.py")
        b = self.task("B builds red")
        self.fm("task", "evidence", b, "--step", "1", "--run", b_cmd, check=False)
        self.fm("checkpoint")
        self.task("A", *a_extra)
        return b, self.fm("check", check=False)

    def test_run_py_leaves_out_the_named_modules(self):
        self.suite()
        run = os.path.join(self.repo, "tests", "run.py")
        clean = {k: v for k, v in os.environ.items() if k != "FOREMAN_SKIP_TESTS"}
        r = subprocess.run([sys.executable, run], capture_output=True, text=True, env=clean)
        self.assertEqual(r.returncode, 1, r.stderr)
        r = subprocess.run([sys.executable, run], capture_output=True, text=True,
                           env=dict(clean, FOREMAN_SKIP_TESTS="test_red"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("test_red", r.stderr, "it says what it left out")

    def test_another_tasks_red_first_test_is_skipped_and_named(self):
        b, r = self.red_elsewhere()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"skipped 1 test file(s) that are only task {b}'s red-first tests: test_red", r.stdout)

    def test_a_red_test_in_this_tasks_scope_still_runs(self):
        self.assertEqual(self.red_elsewhere("--scope", "tests/test_red.py")[1].returncode, 1)

    def test_a_committed_test_file_is_shared_and_still_runs(self):
        _, r = self.red_elsewhere(commit_red=True)
        self.assertEqual(r.returncode, 1, "other tests may live in it: only a new file is one task's alone")
        self.assertNotIn("skipped", r.stdout)

    def test_a_pass_with_tests_left_out_isnt_reused_once_they_count_again(self):
        b, r = self.red_elsewhere()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.fm("task", "drop", b, "not needed")
        r = self.fm("check", check=False)
        self.assertNotIn("passed on this exact tree already", r.stdout)
        self.assertEqual(r.returncode, 1, "the red file runs again (and fails) once B is gone")

    def test_a_look_alike_tests_folder_proves_nothing_about_the_gates_own(self):
        os.makedirs(os.path.join(self.repo, "other", "tests"))
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(self.repo, "other", "tests", "run.py"))
        self.write("other/tests/test_red.py", RED)  # B's new file, under another folder
        _, r = self.red_elsewhere(commit_red=True, b_cmd="python3 other/tests/run.py test_red")
        self.assertEqual(r.returncode, 1, "tests/test_red.py is the gate's own, committed file")
        self.assertNotIn("skipped", r.stdout)

    def test_an_inherited_skip_list_skips_nothing(self):
        self.fm("init")
        self.suite()
        self.fm("check", "add", "python3 tests/run.py")
        self.task("A")
        r = self.fm("check", check=False, env={"FOREMAN_SKIP_TESTS": "test_red"})
        self.assertEqual(r.returncode, 1, "only fm check's own red-first list leaves a file out")

    def test_gate_env_a_second_gate_still_sees_the_skip_list(self):
        """T-4285: the gate loop reused its environment variable for a failure note, so every gate after the first
        ran without FOREMAN_SKIP_TESTS and failed on another task's red-first test."""
        self.fm("init")
        self.suite()
        # the probe passes only when the environment names test_red, whichever of the two gates runs second
        self.write("probe.py", "import os, sys\nsys.exit(0 if os.environ.get('FOREMAN_SKIP_TESTS') == 'test_red' else 1)\n")
        self.fm("check", "add", "python3 tests/run.py")
        self.fm("check", "add", "python3 probe.py")
        b = self.task("B builds red")
        self.fm("task", "evidence", b, "--step", "1", "--run", "python3 tests/run.py test_red", check=False)
        self.fm("checkpoint")
        self.task("A")
        r = self.fm("check", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"skipped 1 test file(s) that are only task {b}'s red-first tests: test_red", r.stdout)

    def test_gate_env_a_gate_after_an_environment_failure_still_runs(self):
        """T-4285: an environment failure in one gate made the next gate's environment a string, so sampo check
        crashed with a traceback instead of running that gate."""
        self.fm("init")
        self.write("src/app.py", "a = 1\n")
        marker = os.path.join(self.tmp, "second-gate")
        self.fm("check", "add", "echo 'ModuleNotFoundError: No module named zz' >&2; exit 1")
        self.fm("check", "add", f"echo x >> {marker}")
        r = self.fm("check", check=False)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 1, "the first gate failed")
        self.assertEqual(open(marker).read().count("x") if os.path.exists(marker) else 0, 1,
                         "the second gate ran after the environment failure")


class CheckPython(ForemanTestCase):
    """T-1805: fm check --python VER runs the gates with that interpreter as python3 (oldest: the lowest Foreman
    supports), so a push or release catches a break only that version has; a version not on the machine is refused."""

    def test_the_gates_run_under_the_named_python(self):
        self.fm("init")
        bin_, mark = os.path.join(self.tmp, "bin"), os.path.join(self.tmp, "mark")
        os.makedirs(bin_)
        stub = os.path.join(bin_, "python3.12")
        with open(stub, "w") as f:  # answers fmpy's version probe as a supported 3.12; a gate's run leaves a mark
            f.write(f'#!/bin/sh\ncase "$*" in *version_info*) echo 3 12 9;; *) echo ran >> {mark};; esac\n')
        os.chmod(stub, 0o755)
        self.fm("check", "add", "python3 -c pass")
        env = {"PATH": bin_ + os.pathsep + os.environ["PATH"]}
        p = self.fm("check", "--python", "oldest", "--json", env=env)
        self.assertIn(stub, p.stderr, "it names the interpreter")
        self.assertEqual(json.loads(p.stdout)["failed"], 0, "--json stays one document")
        with open(mark) as f:
            self.assertEqual(f.read().count("ran"), 1, "the gate's python3 was the 3.12 one")
        r = self.fm("check", "--python", "3.99", env=env, check=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # fm's usage error
        self.assertIn("python3.99", r.stderr)


if __name__ == "__main__":
    unittest.main()
