"""T-3273: mutation-test any Sampo module. modmutate.mutants flips comparisons, boolean operators and int/bool constants in
any function (or one, by name); tests_for finds the test files that import a module; run reports the flips no test
notices; fm bench guard --mutate --module X --flip [--func F] drives it all (idle priority, exit 1 on survivors)."""
import ast
import json
import os
import shutil
import subprocess
import sys
import unittest

from helpers import FM, ForemanTestCase

from sampo import modmutate

SRC = ("def check_a(x):\n    return x == 1\n\n\n"
       "def helper(z):\n    return z < 3 and z != 0\n\n\n"
       "def flags():\n    return True\n")


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Flips(unittest.TestCase):
    def test_flips_every_function_not_only_check_functions(self):
        ms = modmutate.mutants(SRC)
        base = ast.unparse(ast.parse(SRC))
        self.assertEqual({m["line"] for m in ms}, {2, 6, 10}, ms)  # check_a, helper and flags alike
        self.assertEqual(len(ms), 8, ms)  # == and 1 · < and 3 and and != and 0 · True
        for m in ms:
            self.assertNotEqual(ast.unparse(ast.parse(m["src"])), base, m)  # parseable, and not the source

    def test_func_names_one_function(self):
        ms = modmutate.mutants(SRC, func="helper")
        self.assertEqual(({m["line"] for m in ms}, len(ms)), ({6}, 5), ms)
        self.assertEqual(modmutate.mutants(SRC, func="nope"), [])

    def test_the_flip_names_its_operator_and_line(self):
        ms = modmutate.mutants(SRC, func="flags")
        self.assertEqual([m["stmt"] for m in ms], ["True → False: return True"])

    def test_strings_floats_none_and_docstrings_are_not_flipped(self):
        self.assertEqual(modmutate.mutants('def f():\n    """doc 1"""\n    return ("a", 1.5, None)\n'), [])

    def test_test_command_names_the_files_at_idle_priority(self):
        cmd = modmutate.test_cmd(["test_a", "test_b"])
        self.assertEqual(cmd[-2:], ["test_a", "test_b"])
        self.assertEqual(modmutate.niced(cmd)[-len(cmd):], cmd)
        if shutil.which("nice"):
            self.assertEqual([os.path.basename(modmutate.niced(cmd)[0])] + modmutate.niced(cmd)[1:3], ["nice", "-n", "19"])


class Importers(ForemanTestCase):
    def test_tests_for_lists_the_test_files_that_import_the_module(self):
        root = os.path.join(self.tmp, "plugin")
        put(os.path.join(root, "lib", "sampo", "widget.py"), "def f():\n    return 1\n")
        put(os.path.join(root, "lib", "gizmo.py"), "X = 1\n")
        put(os.path.join(root, "tests", "test_a.py"), "from sampo import widget\n")
        put(os.path.join(root, "tests", "test_b.py"), "import sampo.widget as w\n")
        put(os.path.join(root, "tests", "test_c.py"), "from sampo.widget import f\n")
        put(os.path.join(root, "tests", "test_d.py"), "import gizmo\n")
        put(os.path.join(root, "tests", "test_e.py"), "import sampo\n")  # the package alone isn't widget
        put(os.path.join(root, "tests", "test_bad.py"), "def (\n")  # unparseable: skipped, not fatal
        put(os.path.join(root, "tests", "helpers.py"), "from sampo import widget\n")  # not a test file
        widget = os.path.join(root, "lib", "sampo", "widget.py")
        self.assertEqual(modmutate.tests_for(widget, root), ["test_a.py", "test_b.py", "test_c.py"])
        self.assertEqual(modmutate.tests_for(os.path.join(root, "lib", "gizmo.py"), root), ["test_d.py"])

    def test_run_reports_an_untested_comparison_as_a_survivor_and_a_tested_one_as_killed(self):
        d = os.path.join(self.tmp, "fx")
        put(os.path.join(d, "rules.py"), "def tested(x):\n    return x > 5\n\n\ndef untested(y):\n    return y == 3\n")
        put(os.path.join(d, "t.py"), "import rules\nassert rules.tested(6) and not rules.tested(5)\n")
        kw = dict(tree_root=d, dest="fx", cmd=[sys.executable, "fx/t.py"])
        res = modmutate.run(os.path.join(d, "rules.py"), "fx/rules.py", **kw)
        self.assertEqual((res["total"], res["killed"]), (4, 2), res)  # > (two flips) killed; == and 3 survive
        self.assertEqual([m["line"] for m in res["survived"]], [6, 6], res)
        res = modmutate.run(os.path.join(d, "rules.py"), "fx/rules.py", func="tested", **kw)
        self.assertEqual((res["total"], res["killed"], res["survived"]), (2, 2, []), res)
        res = modmutate.run(os.path.join(d, "rules.py"), "fx/rules.py", limit=1, **kw)
        self.assertEqual((res["total"], res["tried"]), (4, 1), res)  # --max caps the mutants run, the total is kept

    def test_run_refuses_a_function_the_module_lacks(self):
        d = os.path.join(self.tmp, "fx")
        put(os.path.join(d, "rules.py"), "def tested(x):\n    return x > 5\n")
        put(os.path.join(d, "t.py"), "import rules\n")
        with self.assertRaisesRegex(ValueError, "no function tetsed"):  # a typo: no mutant would run
            modmutate.run(os.path.join(d, "rules.py"), "fx/rules.py", tree_root=d, dest="fx",
                          cmd=[sys.executable, "fx/t.py"], func="tetsed")


class Bench(ForemanTestCase):
    def lane(self):
        """A builder's worktree: the plugin tree with a widget module, one test that imports it, and a run.py that passes
        only when it is named (so the default tests are checked by the baseline run)."""
        lane = os.path.join(self.tmp, "lane")
        put(os.path.join(lane, "plugin", "lib", "fmguard.py"), "def check(x):\n    return x\n")
        put(os.path.join(lane, "plugin", "lib", "sampo", "widget.py"),
            "def only(x):\n    return x == 1\n\n\ndef other(y):\n    return y > 2\n")
        put(os.path.join(lane, "plugin", "lib", "sampo", "loner.py"), "def lone(x):\n    return x == 1\n")
        put(os.path.join(lane, "plugin", "tests", "test_widget.py"), "from sampo import widget\n")
        put(os.path.join(lane, "plugin", "tests", "run.py"),
            "import sys\nsys.exit(0 if 'test_widget' in sys.argv[1:] else 1)\n")
        return lane

    def bench(self, lane, *args):
        return subprocess.run([FM, "bench", "guard", "--mutate", "--json", *args], cwd=lane, capture_output=True,
                              text=True, env=dict(os.environ, FOREMAN_HOME=self.home), timeout=300)

    def test_flip_module_runs_the_importing_tests_and_exits_1_on_survivors(self):
        r = self.bench(self.lane(), "--module", "plugin/lib/sampo/widget.py", "--flip", "--func", "only")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # the stub passes only when test_widget is named
        data = json.loads(r.stdout)
        self.assertEqual((data["total"], data["survivors"]), (2, 2), data)  # == and 1 in only, nothing else

    def test_flip_module_without_an_importing_test_refuses(self):
        r = self.bench(self.lane(), "--module", "plugin/lib/sampo/loner.py", "--flip")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("imports sampo.loner", r.stdout + r.stderr)

    def test_func_needs_flip(self):
        r = self.bench(self.lane(), "--module", "plugin/lib/sampo/widget.py", "--func", "only")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("--flip", r.stdout + r.stderr)

    def test_func_limits_the_flips_to_one_function(self):
        r = self.bench(self.lane(), "--module", "plugin/lib/sampo/widget.py", "--flip", "--func", "other", "--tests", "true")
        data = json.loads(r.stdout)
        self.assertEqual((data["total"], [m["line"] for m in data["survived"]]), (2, [6, 6]), data)


if __name__ == "__main__":
    unittest.main()
