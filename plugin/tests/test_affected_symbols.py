# map-links: plugin/lib/sampo/cli.py plugin/lib/sampo/map.py
"""T-3306: a changed hub file (linked to over a quarter of the tests) selects tests by the symbols that changed, not by
the file: any cli.py edit used to run ~2,000 tests. When in doubt it keeps the test (a dropped test hides a regression)."""
import json
import os
import subprocess
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c

NAME = "orchestrator"
HUB = f"src/{NAME}.py"
CONVENTION = f"tests/test_{NAME}.py"
# the map links a test to a source whose stem ends with the test's (4+ letters): eight tests linked by name alone
R = [f"tests/test_{NAME[-k:]}.py" for k in range(4, 12)]
BODY = '''import os
LIMIT = 1


def deco(f):
    return f


def alpha():
    return 1


def beta():
    return 2


def gamma():
    return alpha()


def delta(x=LIMIT):
    return x


@deco
def eps():
    return 6


def where():
    return os.getcwd()


def cmd_run():
    return 4


def cmd_lz():
    return 5


def build(add, lazy):
    add("go", cmd_run)
    add("lz", lazy("m", "cmd_lz"))


class K:
    def meth(self):
        return 3
'''
USES = ["alpha()", "alpha()", "beta()", "beta()", "gamma()", "delta()", 'fm("go")', 'fm("lz")']
# T-3603: test files the map links to nothing and that name nothing in the hub (a test driving it through a hook's
# JSON or a child process); only the run cache's records tie them to the code they ran
DRIVERS = ["test_drive_alpha", "test_drive_beta", "test_drive_gamma", "test_drive_delta", "test_drive_meth",
           "test_drive_text", "test_drive_other"]


class AffectedSymbols(ForemanTestCase):
    def write(self, rel, body):
        os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(body)

    def commit(self, msg):
        for args in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg]):
            subprocess.run(["git", "-C", self.repo, *args], check=True)

    def setUp(self):
        super().setUp()
        files = {HUB: BODY, "src/other.py": "N = 1\n", CONVENTION: "# convention\n", "tests/test_other.py": "N = 1\n"}
        files.update({t: use + "\n" for t, use in zip(R, USES)})
        files.update({f"tests/{mod}.py": "pass\n" for mod in DRIVERS})
        for rel, body in files.items():
            self.write(rel, body)
        self.commit("seed")
        self.fm("init")
        self.fm("check", "affected", "echo {tests}")
        self.fm("task", "new", "note it", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s", "--focus")
        self.p = c.find_project(self.repo, create=False)

    def edit(self, old, new):
        path = os.path.join(self.repo, HUB)
        with open(path) as f:
            text = f.read()
        self.assertIn(old, text)
        with open(path, "w") as f:
            f.write(text.replace(old, new))

    def run_affected(self):
        from sampo import cli
        detail = {}
        _, tests, _ = cli.affected(self.p, detail=detail)
        return tests, detail

    def test_one_function_change_selects_the_tests_naming_it_and_the_convention_test(self):
        self.edit("return 2", "return 22")
        tests, detail = self.run_affected()
        self.assertEqual(["beta"], detail[HUB]["symbols"])
        self.assertEqual(sorted([CONVENTION, R[2], R[3]]), tests)

    def test_a_function_pulls_in_the_functions_that_use_it(self):
        self.edit("return 1", "return 11")  # gamma() returns alpha()
        tests, detail = self.run_affected()
        self.assertEqual(["alpha", "gamma"], detail[HUB]["symbols"])
        self.assertEqual(sorted([CONVENTION, R[0], R[1], R[4]]), tests)

    def test_a_constant_pulls_in_a_function_whose_default_reads_it(self):
        self.edit("LIMIT = 1", "LIMIT = 2")
        tests, detail = self.run_affected()
        self.assertEqual(["LIMIT", "delta"], detail[HUB]["symbols"])
        self.assertEqual(sorted([CONVENTION, R[5]]), tests)

    def test_a_changed_default_names_its_function(self):
        self.edit("def delta(x=LIMIT)", "def delta(x=LIMIT + 1)")
        self.assertEqual(["delta"], self.run_affected()[1][HUB]["symbols"])

    def test_a_changed_decorator_line_names_the_decorated_function(self):
        self.edit("@deco\n", "@deco  # now\n")
        self.assertEqual(["eps"], self.run_affected()[1][HUB]["symbols"])

    def test_a_changed_import_pulls_in_the_functions_using_it(self):
        self.edit("import os\n", "import os, sys\n")
        self.assertEqual(["os", "sys", "where"], self.run_affected()[1][HUB]["symbols"])

    def test_a_star_import_falls_back_to_the_file(self):
        self.edit("import os\n", "import os\nfrom os.path import *\n")
        tests, detail = self.run_affected()
        self.assertIsNone(detail[HUB]["symbols"])
        self.assertEqual(sorted([CONVENTION, *R]), tests)

    def test_a_deleted_function_selects_the_tests_naming_it(self):
        self.edit("def beta():\n    return 2\n", "")
        tests, detail = self.run_affected()
        self.assertEqual(["beta"], detail[HUB]["symbols"])
        self.assertEqual(sorted([CONVENTION, R[2], R[3]]), tests)

    def test_a_renamed_function_names_both_names(self):
        self.edit("def beta():", "def beta2():")
        self.assertEqual(["beta", "beta2"], self.run_affected()[1][HUB]["symbols"])

    def test_a_handler_change_selects_the_tests_calling_its_verb(self):
        self.edit("return 4", "return 44")  # add("go", cmd_run)
        tests, detail = self.run_affected()
        self.assertEqual(["build", "cmd_run", "go", "run"], detail[HUB]["symbols"])
        self.assertEqual(sorted([CONVENTION, R[6]]), tests)

    def test_a_lazy_handler_change_selects_the_tests_calling_its_verb(self):
        self.edit("return 5", "return 55")  # add("lz", lazy("m", "cmd_lz"))
        tests, detail = self.run_affected()
        self.assertEqual(["cmd_lz", "lz"],detail[HUB]["symbols"])
        self.assertEqual(sorted([CONVENTION, R[7]]), tests)

    def test_a_test_with_a_map_links_line_is_always_kept(self):
        self.write("tests/test_declared.py", f"# map-links: {HUB}\npass\n")
        self.commit("declared")
        self.edit("return 2", "return 22")
        tests, _ = self.run_affected()
        self.assertEqual(sorted([CONVENTION, R[2], R[3], "tests/test_declared.py"]), tests)

    def test_a_method_change_names_the_method_and_its_class(self):
        self.edit("return 3", "return 33")
        self.assertEqual(["K", "meth"], self.run_affected()[1][HUB]["symbols"])

    def test_module_level_code_with_side_effects_falls_back_to_the_file(self):
        self.edit("LIMIT = 1\n", "LIMIT = 1\nprint('side effect')\n")
        tests, detail = self.run_affected()
        self.assertIsNone(detail[HUB]["symbols"])
        self.assertEqual(sorted([CONVENTION, *R]), tests)

    def test_a_syntax_error_falls_back_to_the_file(self):
        self.edit("return 1", "return (")
        self.assertIsNone(self.run_affected()[1][HUB]["symbols"])

    def test_a_file_few_tests_link_stays_file_level(self):
        self.write("src/other.py", "N = 2\n")
        tests, detail = self.run_affected()
        self.assertEqual({}, detail)
        self.assertEqual(["tests/test_other.py"], tests)

    def test_a_file_linked_to_a_few_heavy_test_files_is_a_hub_too(self):
        from sampo import cli
        heavy = "".join(f"    def test_{i}(self): pass\n" for i in range(cli.HUB_WEIGHT))
        self.write("tests/test_heavy.py", "# map-links: src/other.py\n" + heavy)
        self.commit("heavy")
        self.write("src/other.py", "N = 2\n")
        tests, detail = self.run_affected()
        self.assertEqual(["N"], detail["src/other.py"]["symbols"])
        self.assertEqual(["tests/test_other.py", "tests/test_heavy.py"], sorted(detail["src/other.py"]["tests"], reverse=True))

    def ran(self, records):
        """T-3603: the test runner's run cache (run.py's own writer) holding, per test module (DRIVERS), one class
        whose record names its module and the keys given."""
        import run
        cache = os.path.join(self.tmp, "cache")
        patch = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache})
        patch.start()
        self.addCleanup(patch.stop)
        new = {f"{mod}.Drive": ["d", 1, [f"tests/{mod}.py", f"tests/{mod}.py::", f"tests/{mod}.py::Drive.test_a", *keys]]
               for mod, keys in records.items()}
        run.save_cache(run.cache_path(os.path.realpath(os.path.join(self.repo, ".git"))), new, 0)

    def test_a_test_that_ran_a_changed_function_is_selected_and_one_that_ran_another_is_not(self):
        self.ran({"test_drive_beta": [HUB + "::", HUB + "::beta"], "test_drive_alpha": [HUB + "::", HUB + "::alpha"],
                  "test_drive_gamma": [HUB + "::", HUB + "::gamma", HUB + "::alpha"],
                  "test_drive_other": ["src/other.py::", "src/other.py::f"]})
        self.edit("return 2", "return 22")
        tests, _ = self.run_affected()
        self.assertEqual(sorted([CONVENTION, R[2], R[3], "tests/test_drive_beta.py"]), tests)
        self.edit("return 22", "return 2")
        self.edit("return 6", "return 66")  # eps: no class ran it
        self.assertFalse([t for t in self.run_affected()[0] if "drive" in t], "an uncalled function selects none")

    def test_a_record_whose_test_file_is_gone_selects_nothing(self):
        self.ran({"test_drive_gone": [HUB + "::beta"]})
        self.edit("return 2", "return 22")
        self.assertNotIn("tests/test_drive_gone.py", self.run_affected()[0])

    def test_a_changed_constant_selects_the_tests_that_ran_a_function_reading_it(self):
        self.ran({"test_drive_delta": [HUB + "::", HUB + "::delta"], "test_drive_alpha": [HUB + "::", HUB + "::alpha"]})
        self.edit("LIMIT = 1", "LIMIT = 2")
        tests, _ = self.run_affected()
        self.assertIn("tests/test_drive_delta.py", tests)
        self.assertNotIn("tests/test_drive_alpha.py", tests)

    def test_a_class_level_change_selects_the_tests_that_ran_its_methods(self):
        self.ran({"test_drive_meth": [HUB + "::", HUB + "::K.meth"], "test_drive_alpha": [HUB + "::", HUB + "::alpha"]})
        self.edit("class K:\n", "class K:\n    flag = 1\n\n")
        tests, _ = self.run_affected()
        self.assertIn("tests/test_drive_meth.py", tests)
        self.assertNotIn("tests/test_drive_alpha.py", tests)

    def test_a_change_not_told_by_function_selects_every_test_that_ran_the_file(self):
        self.ran({"test_drive_alpha": [HUB + "::", HUB + "::alpha"], "test_drive_other": ["src/other.py::"]})
        self.edit("LIMIT = 1\n", "LIMIT = 1\nprint('side effect')\n")
        tests, _ = self.run_affected()
        self.assertIn("tests/test_drive_alpha.py", tests)
        self.assertNotIn("tests/test_drive_other.py", tests)

    def test_a_test_that_read_a_changed_file_whole_is_selected(self):
        self.ran({"test_drive_text": [HUB, "data/table.json"], "test_drive_alpha": [HUB + "::", HUB + "::alpha"]})
        self.edit("return 2", "return 22")
        self.assertIn("tests/test_drive_text.py", self.run_affected()[0])
        self.edit("return 22", "return 2")
        self.write("data/table.json", "{}\n")
        self.assertEqual(["tests/test_drive_text.py"], self.run_affected()[0])

    def test_a_record_from_before_function_keys_counts_only_without_a_newer_one(self):
        """A record written before T-3603 names every module a class imported as read whole: beside a newer record of
        the class it is ignored (main's cache held 190 such readers of hooks.py), alone it is the file-level fallback."""
        import run
        self.ran({"test_drive_alpha": [HUB + "::", HUB + "::alpha"]})
        old = {f"{mod}.Drive": ["old", 1, [f"tests/{mod}.py", HUB]] for mod in ("test_drive_alpha", "test_drive_gamma")}
        path = run.cache_path(os.path.realpath(os.path.join(self.repo, ".git")))
        run.save_cache(path, old, 0)
        self.assertEqual(["old", "d"], [r[0] for r in run.load_cache(path)[1]["test_drive_alpha.Drive"]])
        self.edit("return 2", "return 22")
        tests, _ = self.run_affected()
        self.assertNotIn("tests/test_drive_alpha.py", tests)
        self.assertIn("tests/test_drive_gamma.py", tests)

    def test_merge_888c3d75d_selects_the_firewall_test_that_ran_outline_first(self):
        """T-3670's miss: the merge changed hooks._outline_first and the constant beside it, and its train's affected
        gate left out test_firewall.OutlineFirst, which drives the Read hook only through its JSON payload. Its record
        names the function; a class that ran another hook function stays out."""
        from sampo import map as smap
        import run
        top = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        hooks = "plugin/lib/sampo/hooks.py"
        if subprocess.run(["git", "-C", top, "cat-file", "-e", "888c3d75d^{commit}"], capture_output=True).returncode:
            self.skipTest("merge 888c3d75d isn't in this clone")
        common = subprocess.run(["git", "-C", top, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.tmp, "real")}):
            run.save_cache(run.cache_path(os.path.realpath(common)), {
                "test_firewall.OutlineFirst": ["d", 4, ["plugin/tests/test_firewall.py::OutlineFirst.test_a", hooks + "::",
                                                        hooks + "::run", hooks + "::_outline_first"]],
                "test_run_cache.Other": ["d", 1, ["plugin/tests/test_run_cache.py::", hooks + "::", hooks + "::run",
                                                  hooks + "::_ask_prompt"]]}, 0)
            got = smap.ran_tests(top, "888c3d75d^1", "888c3d75d", [hooks])
        self.assertEqual(["plugin/tests/test_firewall.py"], got)

    def test_json_lists_per_hub_the_symbols_and_tests_chosen(self):
        self.edit("return 2", "return 22")
        out = json.loads(self.fm("check", "--affected", "--json").stdout)
        self.assertEqual({"mode": "symbols", "symbols": ["beta"], "tests": sorted([CONVENTION, R[2], R[3]])},
                         out["hubs"][HUB])
