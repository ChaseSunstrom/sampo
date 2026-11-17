"""T-4906: the Ward named Sampo's writers from a hand list (_FM_WRITERS), so a writer outside it ran through a plain
import: set_panic, lift_project_pauses, set_standing_autonomy. The writer set is now derived from the code: an index of
every Sampo module's functions, a writer one whose calls (resolved by module alias, self and method name) reach a
file-system write or a process start, cached per tree. A use of a Sampo module the index can't show is safe (a writer,
a name it doesn't know, the module passed around) is refused, and so are file writes beside a Sampo import."""
# map-links: plugin/lib/fmguard.py
import os
import tempfile
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on the path)
from test_guard import GuardCase
import fmguard as g


def py(code):
    return f"python3 -c '{code}'"


class Refused(GuardCase):
    def refused(self, code, name=None):
        r = self.bash(py(code))
        self.assertBlocked(r, "core", code)
        if name:
            self.assertIn(name, r.detail, code)

    def test_writers_outside_the_hand_list_are_refused_through_every_import_form(self):
        for code, name in (("import sampo.core as c; c.set_panic(False)", "set_panic"),
                           ("from sampo.core import lift_project_pauses; lift_project_pauses()", "lift_project_pauses"),
                           ("from sampo import profile; profile.set_standing_autonomy(1)", "set_standing_autonomy"),
                           ("import fmcore as c; c.set_panic(False)", "set_panic"),
                           ("import sampo.core; sampo.core.set_panic(False)", "set_panic"),
                           ("from sampo.core import set_panic as s; s(False)", "set_panic"),
                           ("from fmcore import set_panic; set_panic(False)", "set_panic"),
                           ("from sampo import home as h; h._move(\"a\", \"b\")", "_move")):
            with self.subTest(code=code):
                self.refused(code, name)

    def test_a_name_the_index_cannot_show_is_safe_is_refused(self):
        for code in ("import sampo.core as c; c.no_such_function_zz(1)",  # unknown: another tree may have it
                     "import sampo.core as c; f = c; f.set_panic(False)",  # the module passed around
                     "import sampo.core as c; print(vars(c))",
                     "from sampo import core; print(core.read_meta(1)); open(\"/tmp/x\", \"w\").write(\"1\")"):
            with self.subTest(code=code):
                self.refused(code)

    def test_a_writer_method_name_on_any_object_is_refused(self):
        idx = g._writer_index()
        name = sorted(idx["methods"])[0]
        self.refused(f"import sampo.core as c; x = c.read_meta(1); x.{name}()", name)


class Allowed(GuardCase):
    def test_pure_functions_still_read(self):
        for code in ("import sampo.core as c; print(c.fit(\"abc\", 2))",
                     "from sampo.core import parse_ts; print(parse_ts(\"2026-01-01T00:00:00Z\"))",
                     "from sampo import core; print(core.read_meta(1))",
                     "import fmcore as c; print(c.joinmore([1, 2], 5))"):
            with self.subTest(code=code):
                self.assertIsNone(self.bash(py(code)))


class Index(unittest.TestCase):
    def tree(self, d, files):
        for name, text in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(text)

    def test_a_writer_is_a_function_whose_calls_reach_a_write(self):
        with tempfile.TemporaryDirectory() as d:
            self.tree(d, {
                "__init__.py": "",
                "a.py": ("import os\nimport subprocess\n"
                         "def w(p):\n    open(p, 'w').write('1')\n"
                         "def r(p):\n    return open(p).read()\n"
                         "def via(p):\n    w(p)\n"
                         "def gone(p):\n    os.remove(p)\n"
                         "def spawn():\n    subprocess.run(['true'])\n"
                         "def pure(x):\n    return x.replace('a', 'b')\n"
                         "LIMIT = 3\n"
                         "class Box:\n    def keep(self, p):\n        self.put(p)\n    def put(self, p):\n        w(p)\n"
                         "    def look(self):\n        return 1\n"),
                "b.py": ("from sampo import a as x\nfrom sampo.a import r\n"
                         "def far(p):\n    x.via(p)\n"
                         "def near(p):\n    return r(p)\n"
                         "def by_method(o, p):\n    o.keep(p)\n")})
            idx = g._build_index(d)
            a, b = idx["modules"]["sampo.a"], idx["modules"]["sampo.b"]
            self.assertEqual(sorted(a["writers"]), ["gone", "spawn", "via", "w"])
            self.assertEqual(sorted(a["safe"]), ["pure", "r"])
            self.assertIn("LIMIT", a["names"])
            self.assertEqual(a["classes"], {"Box": True})
            self.assertEqual(sorted(b["writers"]), ["by_method", "far"])
            self.assertEqual(b["safe"], ["near"])
            self.assertIn("keep", idx["methods"])
            self.assertNotIn("look", idx["methods"])

    def test_an_import_writes_only_through_what_it_runs(self):
        with tempfile.TemporaryDirectory() as d:
            self.tree(d, {
                "__init__.py": "",
                "w.py": "import os\nos.makedirs('x')\n",
                "a.py": ("import re\n"
                         "def w(p):\n    open(p, 'w').write('1')\n"
                         "def make():\n    def run(p):\n        w(p)\n    return run\n"
                         "RUN = make()\nPAT = re.compile('x')\n"
                         "def look(s):\n    return PAT.search(s)\n"
                         "class Run:\n    def search(self, p):\n        w(p)\n"),
                "b.py": "from sampo import w\n",
                "c.py": "def later():\n    from sampo import w\n"})
            idx = g._build_index(d)
            mods = idx["modules"]
            self.assertFalse(mods["sampo.a"]["import_writes"], "a closure made at import runs nothing")
            self.assertIn("make", mods["sampo.a"]["writers"], "its caller may run what it returns")
            self.assertIn("look", mods["sampo.a"]["safe"], "a pattern's search is no Sampo method by its name")
            self.assertTrue(mods["sampo.w"]["import_writes"])
            self.assertTrue(mods["sampo.b"]["import_writes"], "it imports a module whose import writes")
            self.assertFalse(mods["sampo.c"]["import_writes"])
            self.assertIn("later", mods["sampo.c"]["writers"])

    def test_a_name_a_module_imports_is_judged_where_it_is_defined(self):
        import ast
        with tempfile.TemporaryDirectory() as d:
            self.tree(d, {
                "__init__.py": "",
                "a.py": "def w(p):\n    open(p, 'w').write('1')\ndef r(p):\n    return p\n",
                "b.py": "from sampo.a import r, w\nfrom sampo import a as inner\n"})
            mods = g._build_index(d)["modules"]
            self.assertEqual(mods["sampo.b"]["imports"], {"inner": "sampo.a", "r": "sampo.a.r", "w": "sampo.a.w"})
            idx = {"modules": mods, "methods": []}
            for code, why in (("from sampo.b import r; r(1)", ""), ("import sampo.b as b; b.r(1)", ""),
                              ("import sampo.b as b; b.w(1)", "sampo.a.w (it writes)"),
                              ("import sampo.b as b; b.inner.w(1)", "sampo.a.w (it writes)"),
                              ("from sampo.b import inner; inner.w(1)", "sampo.a.w (it writes)"),
                              ("import sampo.b as b; f = b.inner", "Sampo's module sampo.a passed on as a value")):
                with self.subTest(code=code):
                    self.assertEqual(g._fm_use_refused(ast.parse(code), idx), why)

    def test_every_import_form_meets_a_module_whose_import_writes(self):
        import ast
        idx = {"modules": {"sampo": {"writers": [], "safe": [], "classes": {}, "names": [], "imports": {},
                                     "import_writes": False},
                           "sampo.w": {"writers": [], "safe": ["r"], "classes": {}, "names": [], "imports": {},
                                       "import_writes": True}}, "methods": []}
        for code in ("import sampo.w", "from sampo.w import r", "from sampo import w"):
            with self.subTest(code=code):
                self.assertEqual(g._fm_use_refused(ast.parse(code), idx), "sampo.w (its import writes)")

    def test_an_underscore_twin_is_a_sampo_module(self):
        self.assertEqual(g._fm_target("fmlane_overlap"), "sampo.lane_overlap")
        self.assertTrue(g._FM_INTERNALS.search("import fmflake_forecast as f"))

    def test_the_index_is_built_once_per_tree_and_rebuilt_when_a_module_changes(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cache:
            self.tree(d, {"__init__.py": "", "a.py": "def r():\n    return 1\n"})
            g._INDEX_MEMO.clear()
            first = g._writer_index(d, cache)
            self.assertEqual(first["modules"]["sampo.a"]["safe"], ["r"])
            g._INDEX_MEMO.clear()
            with mock.patch.object(g, "_build_index", side_effect=AssertionError("rebuilt")):
                self.assertEqual(g._writer_index(d, cache), first, "read from its cache")
            time.sleep(0.01)
            self.tree(d, {"a.py": "def r(p):\n    open(p, 'a').write('x')\n"})
            g._INDEX_MEMO.clear()
            self.assertEqual(g._writer_index(d, cache)["modules"]["sampo.a"]["writers"], ["r"])

    def test_a_name_holding_a_writer_is_a_writer(self):
        with tempfile.TemporaryDirectory() as d:
            self.tree(d, {
                "__init__.py": "",
                "a.py": ("import functools\nimport os\nimport re\nfrom shutil import move as mv\n"
                         "def w(p):\n    open(p, 'w').write('1')\n"
                         "def r(p):\n    return p\n"
                         "class Store:\n    def put(self, p):\n        w(p)\n"
                         "_move = os.replace\n_git = functools.partial(w, 1)\n_ok = functools.partial(r, 1)\n"
                         "HANDLERS = {'x': w}\nPAT = re.compile('x')\nSTORE = Store()\nLIMIT = 3\n_mv = mv\n"
                         "def via_name(p):\n    return _git(p)\n"
                         "def passes_on(p):\n    return list(map(w, [p]))\n"
                         "def keeps_a_variable(move):\n    return move\n")})
            a = g._build_index(d)["modules"]["sampo.a"]
            for name in ("_move", "_git", "HANDLERS", "STORE", "_mv", "via_name", "passes_on"):
                self.assertIn(name, a["writers"], name)
            for name in ("_ok", "PAT", "LIMIT"):
                self.assertIn(name, a["names"], name)
            self.assertIn("keeps_a_variable", a["safe"], "a variable named move is no shutil.move")
            self.assertFalse(a["import_writes"], "naming a writer at import runs nothing")

    def test_the_real_tree_knows_the_writers_the_hand_list_missed(self):
        mods = g._writer_index()["modules"]
        core = mods["sampo.core"]
        for name in ("set_panic", "lift_project_pauses", "write_meta"):
            self.assertIn(name, core["writers"])
        for name in ("fit", "parse_ts", "read_meta"):
            self.assertIn(name, core["safe"])
        self.assertIn("_move", mods["sampo.home"]["writers"], "os.replace under another name")
        for m in ("sampo.lanes", "sampo.map"):
            self.assertIn("_git", mods[m]["writers"], "a partial of core's runner")


if __name__ == "__main__":
    unittest.main()
