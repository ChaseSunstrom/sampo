"""T-2978: scenario minting in real repos. One deterministic mutation per scenario, kept only when the project's own
tests go red on the mutant and stay green unmutated; a mutant the tests miss is dropped and counted; the main checkout
and HEAD stay untouched. Minted scenarios are graded like seeded ones (test_exam covers the grading)."""
# map-links: plugin/lib/sampo/mint.py plugin/lib/sampo/louhi.py plugin/lib/sampo/cli.py

import json
import os
import subprocess
import sys

from helpers import ForemanTestCase, git_repo, read_text

CALC = '''def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def window(xs, k):
    out = []
    for i in range(len(xs) - k + 1):
        out.append(xs[i:i + k])
    return out
'''

TEST_CALC = '''import unittest

from calc import clamp, window


class Calc(unittest.TestCase):
    def test_clamp_low(self):
        self.assertEqual(clamp(-1, 0, 10), 0)

    def test_clamp_high(self):
        self.assertEqual(clamp(11, 0, 10), 10)

    def test_clamp_inside(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_window(self):
        self.assertEqual(window([1, 2, 3], 2), [[1, 2], [2, 3]])
'''

RUN = '''import os
import sys
import unittest

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
suite = unittest.defaultTestLoader.discover(here)
sys.exit(0 if unittest.TextTestRunner(verbosity=0).run(suite).wasSuccessful() else 1)
'''


class _MintRepo(ForemanTestCase):
    """The fixture repo (setUp), write(), git() and mint(): no tests of its own (T-3815), so a subclass shares them."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.repo = git_repo(self.tmp, "proj")
        os.makedirs(os.path.join(self.repo, "tests"))
        self.write("calc.py", CALC)
        self.write(os.path.join("tests", "test_calc.py"), TEST_CALC)
        self.write(os.path.join("tests", "run.py"), RUN)
        self.git("add", ".")
        self.git("commit", "-qm", "calc with tests")
        self.head = self.git("rev-parse", "HEAD").strip()

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True).stdout

    def mint(self, *args):
        return self.fm_json("exam", "gen", "--from-repo", self.repo, "--n", "6", "--seed", "3", *args)

class MintFromRepo(_MintRepo):
    def test_keeps_caught_mutants_drops_and_counts_missed_ones(self):
        # the <=/>= swaps at the clamp bounds are never tested at x == lo or x == hi, so the suite stays green: missed
        r = self.mint()
        m = r["minted"]
        self.assertGreaterEqual(m["kept"], 1)
        self.assertGreaterEqual(m["missed"], 1)
        self.assertEqual(m["tried"], m["kept"] + m["missed"] + m["skipped"])
        self.assertEqual(len(r["scenarios"]), m["kept"])
        self.assertLessEqual(m["kept"], 6)  # --n bounds the kept scenarios

    def test_each_minted_scenario_is_a_bug_planted_in_a_real_file_and_symbol(self):
        r = self.mint()
        self.assertTrue(r["scenarios"])
        for s in r["scenarios"]:
            key = json.loads(read_text(os.path.join(os.path.dirname(s["repo"]), "key.json")))
            self.assertEqual(s["kind"], "bug")
            self.assertEqual(key["origin"], "minted")
            self.assertEqual(key["exec_t"], 120)  # mint's CAP_S: grading gives the real suite the cap minting gave it
            # T-3004: a bug report names what went red; without it a model hunts the wrong suite in a big repo
            self.assertIn(key["commands"]["exec"], key["request"])
            self.assertIn(key["commands"]["exec"], s["request"])
            self.assertEqual(len(key["defects"]), 1)
            d = key["defects"][0]
            self.assertEqual(d["files"], ["calc.py"])
            self.assertIn(d["symbols"][0], ("clamp", "window"))
            # the planted file differs from the clean one and still compiles (a mutant is one valid edit)
            planted = read_text(os.path.join(s["repo"], "calc.py"))
            clean = read_text(os.path.join(os.path.dirname(s["repo"]), "hidden", "clean", "calc.py"))
            self.assertEqual(clean, CALC)
            self.assertNotEqual(planted, CALC)
            compile(planted, "calc.py", "exec")

    def test_minted_scenarios_run_and_grade_by_planted_file_and_symbol(self):
        # the oracle answers from the key: closes every minted scenario; a finding in the wrong file or the wrong
        # symbol closes none of them (the grade reads the planted file and symbol, as seeded scenarios do)
        r = self.mint()
        ids = sorted(s["id"] for s in r["scenarios"])
        o = self.fm_json("exam", "run", r["run"], "--agent", "oracle")
        self.assertEqual(sorted(o["closed"]["train"] + o["closed"]["holdout"]), ids)
        self.assertEqual(self.fm_json("exam", "check", r["run"])["checked"], len(ids))
        run_dir = os.path.join(os.path.dirname(os.path.dirname(r["scenarios"][0]["repo"])))
        for sid, wrong in ((ids[0], {"file": "calc.py", "symbol": "nope"}), (ids[1], {"file": "other.py", "symbol": "clamp"})):
            ans = os.path.join(run_dir, "answers", "oracle", sid + ".json")
            a = json.loads(read_text(ans))
            a["findings"] = [dict(wrong, says="wrong place")]
            with open(ans, "w", encoding="utf-8") as f:
                json.dump(a, f)
        g = self.fm_json("exam", "grade", r["run"], "--agent", "oracle")
        closed = g["closed"]["train"] + g["closed"]["holdout"]
        self.assertNotIn(ids[0], closed)
        self.assertNotIn(ids[1], closed)
        self.assertEqual(sorted(closed), ids[2:])

    def test_is_deterministic_per_seed(self):
        a = self.mint()["minted"]["mutations"]
        b = self.mint()["minted"]["mutations"]
        self.assertTrue(a)
        self.assertEqual(a, b)

    def test_main_checkout_and_head_are_untouched(self):
        before = self.git("status", "--porcelain")
        self.mint()
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.head)
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual(read_text(os.path.join(self.repo, "calc.py")), CALC)
        self.assertEqual(self.git("branch", "--format=%(refname:short)").split(), ["main"])
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

    def test_a_sensitive_repo_is_never_minted(self):  # T-2995 review: its journals would become training data
        os.makedirs(os.path.join(self.repo, ".claude"))
        self.write(os.path.join(".claude", "settings.json"), '{"permissions": {"defaultMode": "default"}}')
        r = self.fm("exam", "gen", "--from-repo", self.repo, "--n", "2", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sensitive", r.stderr)

    def test_red_at_head_mints_nothing(self):
        self.write(os.path.join("tests", "test_calc.py"), TEST_CALC + "\n    def test_broken(self):\n        self.fail()\n")
        self.git("commit", "-qam", "a red test at HEAD")
        r = self.mint()
        self.assertEqual(r["minted"]["kept"], 0)
        self.assertEqual(r["scenarios"], [])
        self.assertIn("red", r["minted"]["reason"])


class MintWithUntrackedDeps(_MintRepo):  # T-2987: real projects keep their deps out of git (.venv, node_modules)
    def setUp(self):
        super().setUp()
        lib = os.path.join(self.repo, ".venv", "lib")
        os.makedirs(lib)
        os.makedirs(os.path.join(self.repo, ".venv", "bin"))
        with open(os.path.join(lib, "helperlib.py"), "w") as f:
            f.write("ONE = 1\n")
        py = os.path.join(self.repo, ".venv", "bin", "python3")  # a venv's interpreter: its lib is on the path
        with open(py, "w") as f:
            f.write(f'#!/bin/sh\nPYTHONPATH="$(dirname "$0")/../lib:$PYTHONPATH" exec {sys.executable} "$@"\n')
        os.chmod(py, 0o755)
        self.write(".gitignore", ".venv/\n")
        self.write(os.path.join("tests", "test_calc.py"), "import helperlib  # only in the untracked venv\n" + TEST_CALC)
        self.git("add", ".")
        self.git("commit", "-qm", "tests need the venv")
        self.head = self.git("rev-parse", "HEAD").strip()

    def test_mints_through_the_linked_venv_and_leaves_it_untouched(self):
        before = sorted(os.listdir(os.path.join(self.repo, ".venv", "lib")))
        r = self.mint()
        self.assertGreaterEqual(r["minted"]["kept"], 1, r["minted"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.repo, ".venv", "lib"))), before)  # no __pycache__ written
        key = json.loads(read_text(os.path.join(os.path.dirname(r["scenarios"][0]["repo"]), "key.json")))
        self.assertEqual(key["env"]["PYTHONPATH"], "src:.")  # the copy's sources shadow an editable install
        self.assertTrue(os.path.islink(os.path.join(r["scenarios"][0]["repo"], ".venv")))

    def test_a_given_test_command_replaces_the_detected_one(self):  # a slow suite mints against a fast subset
        r = self.mint("--test-cmd", "uv run python3 -B tests/run.py")
        self.assertEqual(r["minted"]["command"], "python3 -B tests/run.py")
        self.assertGreaterEqual(r["minted"]["kept"], 1, r["minted"])

    def test_only_limits_the_mutated_files(self):  # a big repo mints from one module and the tests that cover it
        self.write("other.py", "def twice(x):\n    if x > 0:\n        return x * 2\n    return 0\n")
        self.git("add", ".")
        self.git("commit", "-qm", "an uncovered module")
        r = self.mint("--only", "other.py")
        self.assertEqual(r["minted"]["kept"], 0)  # its mutants exist, but no test covers other.py
        self.assertGreaterEqual(r["minted"]["missed"], 1)
        self.assertTrue(all(m.startswith("other.py:") for m in r["minted"]["mutations"]))

    def test_grading_copies_keep_a_linked_dep_dir_and_skip_a_real_one(self):  # a minted TS repo graded without tsc
        from sampo import louhi
        src = os.path.join(self.tmp, "graded")
        os.makedirs(os.path.join(src, "real", "node_modules"))
        os.makedirs(os.path.join(src, "deps"))
        os.symlink(os.path.join(src, "deps"), os.path.join(src, "node_modules"))
        tmp, dst = louhi._copy(src)
        self.assertTrue(os.path.islink(os.path.join(dst, "node_modules")))
        self.assertFalse(os.path.exists(os.path.join(dst, "real", "node_modules")))

    def test_a_runner_wrapper_is_unwrapped(self):
        from sampo import mint
        self.assertEqual(mint._unwrap("uv run pytest -q"), "pytest -q")
        self.assertEqual(mint._unwrap("poetry run python -m pytest"), "python -m pytest")
        self.assertEqual(mint._unwrap("cargo test"), "cargo test")
