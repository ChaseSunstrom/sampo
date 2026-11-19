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


class Reach(GuardCase):
    """T-4930 (T-4906's review): the index follows the names an import binds, so a Sampo module reached another way ran
    its writers unrefused: sys.modules, pkgutil.resolve_name, runpy, importlib.util, a sys.path entry put first, and
    __import__ of a computed name (which skipped the guard's literal pre-gate entirely)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        g._writer_index()  # warm, in this process: the index is not what these cases test

    def refused(self, code, word=None):
        r = self.bash(py(code))
        self.assertBlocked(r, "core", code)
        if word:
            self.assertIn(word, r.detail, code)

    def test_a_sampo_module_reached_by_another_route_is_refused(self):
        for code, word in (('import sampo.core; import sys; sys.modules["sampo.core"].set_panic(False)', "modules"),
                           ('import sampo.core; from sys import modules; modules["sampo.core"].set_panic(False)', "modules"),
                           ('import sampo.core, pkgutil; pkgutil.resolve_name("sampo.core:set_panic")(False)', "pkgutil"),
                           ('import sampo.core; import runpy; runpy.run_module("sampo.core")', "runpy"),
                           ('import sampo.core; import importlib.util as u; print(u.find_spec("sampo.core"))', "importlib"),
                           ('import sampo.core, operator; print(operator.attrgetter("fit"))', "operator"),
                           ('import sys; sys.path.insert(0, "/tmp/x"); import sampo.core as c; print(c.fit("a", 1))', "path"),
                           # review: sys by another name, a module's own sys, the table under another name
                           ('import sys; s = sys; s.path.insert(0, "/tmp/x"); import sampo.core as c; print(c.fit("a", 1))',
                            "sys"),
                           ('import collections; collections._sys.path.insert(0, "/tmp/x"); import sampo.core', "sys"),
                           ('import sampo.core; from sys import modules as M; M["sampo.core"].set_panic(1)', "modules"),
                           ('import sampo.core, sys; getattr(sys, "modules")["sampo.core"].set_panic(1)', "getattr")):
            with self.subTest(code=code):
                self.refused(code, word)

    def test_an_import_by_a_computed_name_or_route_is_refused(self):
        for code in ('__import__("sam" + "po.core").core.set_panic(False)',
                     'import importlib; importlib.import_module("".join(["sam", "po.core"])).set_panic(False)',
                     'import importlib, sys; importlib.import_module(sys.argv[1]).set_panic(0)',
                     # review: the machinery renamed, stored, fetched by a string, or handed **kw
                     'from importlib import import_module as im; im("sampo.core").set_panic(False)',
                     'import importlib; getattr(importlib, "import_module")("json")',
                     'import importlib; f = importlib.import_module; f("sampo.core")',
                     'import importlib; importlib.import_module(**{{"name": "sampo.core"}})'):  # (bash() formats it)
            with self.subTest(code=code):
                self.refused(code, "computed")

    def test_a_literal_naming_sampo_handed_to_the_machinery_is_a_sampo_import(self):
        lib = os.path.dirname(os.path.abspath(g.__file__))
        for code in ('n = "sampo"; __import__(n)',
                     'import importlib; importlib.import_module(".core", "sampo").set_panic(False)',
                     f'import runpy; runpy.run_path("{lib}/fmcore.py")["write_meta"]("x", {{{{}}}})',
                     f'import importlib.util as u; s = u.spec_from_file_location("x", "{lib}/sampo/core.py")',
                     'import pkgutil; pkgutil.resolve_name("sampo.core:set_panic")(False)'):
            with self.subTest(code=code):
                self.refused(code)

    def test_a_literal_import_of_another_module_still_runs(self):
        for code in ('print(__import__("json").dumps(1))', 'import importlib; print(importlib.import_module("json"))',
                     'for m in ("json", "csv"): __import__(m)',
                     'import importlib; print([importlib.import_module(m) for m in ["json", "csv"]])',
                     'print("import_module")'):
            with self.subTest(code=code):
                self.assertIsNone(self.bash(py(code)))

    def test_a_harmless_module_beside_a_sampo_read_still_runs(self):
        lib = os.path.dirname(os.path.abspath(g.__file__))
        for code in ('import hashlib; from sampo.core import fit; print(fit("abc", 2), hashlib.sha1(b"").hexdigest())',
                     'import sys; from sampo.core import fit; print(fit(sys.argv[1], 2))'):
            with self.subTest(code=code):
                self.assertIsNone(self.bash(f"cd {lib} && " + py(code) + " abc"))

    def test_a_sampo_folder_in_the_run_folder_that_is_not_the_indexed_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sampo"))
            for name, text in (("__init__.py", ""), ("core.py", "def fit(*a):\n    open('/tmp/x', 'w')\n")):
                with open(os.path.join(d, "sampo", name), "w") as f:
                    f.write(text)
            r = self.bash(f"cd {d} && " + py('from sampo.core import fit; print(fit("a", 1))'))
            self.assertBlocked(r, "core")
            self.assertIn("run folder", r.detail)

    def test_the_indexed_trees_own_folder_still_reads(self):
        lib = os.path.dirname(os.path.abspath(g.__file__))  # plugin/lib: its sampo/ is the tree the index reads
        self.assertIsNone(self.bash(f"cd {lib} && " + py('from sampo.core import fit; print(fit("abc", 2))')))

    def test_the_indexed_lib_put_first_by_a_literal_still_reads(self):
        """A read session's usual form (test_guard's table had it): sys.path.insert(0, <the lib>) then a read. Only
        that lib, first, by a literal: another folder, an append (site-packages stays before it) or index 1 is a
        route."""
        lib = os.path.dirname(os.path.abspath(g.__file__))
        plugin = os.path.dirname(lib)
        read = 'import fmguard; print(fmguard.CATEGORIES)'
        self.assertIsNone(self.bash(py(f'import sys; sys.path.insert(0, "{lib}"); {read}')))
        self.assertIsNone(self.bash(f"cd {plugin} && " + py(f'import sys; sys.path.insert(0, "lib"); {read}')))
        for code in (f'import sys; sys.path.insert(0, "{plugin}"); {read}',
                     f'import sys; sys.path.append("{lib}"); {read}',
                     f'import sys; sys.path.insert(1, "{lib}"); {read}',
                     f'import sys; sys.path.insert(0, "{lib}"); sys.path.insert(0, "/tmp/x"); {read}',
                     f'import sys; d = "{lib}"; sys.path.insert(0, d); {read}'):
            with self.subTest(code=code):
                self.refused(code, "sys.path")
        self.assertBlocked(self.bash(f"cd /tmp && " + py(f'import sys; sys.path.insert(0, "lib"); {read}')), "core")


class FailClosed(GuardCase):
    """T-4930: an index that can't be had refuses a Sampo import (it fell back to the hand list, the hole T-4906 closed),
    and the PreToolUse hook (5 s, a refusal on timeout) never builds it inline: a cold tree starts one detached build and
    the command is refused until it is there."""

    def setUp(self):
        super().setUp()
        g._INDEX_MEMO.clear()
        self.addCleanup(g._INDEX_MEMO.clear)

    def test_an_index_that_cannot_be_built_refuses_a_sampo_import(self):
        with mock.patch.object(g, "_writer_index", side_effect=RecursionError("deep")):
            r = self.bash(py('import sampo.core as c; print(c.fit("a", 2))'))
        self.assertBlocked(r, "core")
        self.assertIn("could not be built (RecursionError)", r.detail)

    def test_the_hook_never_builds_the_index_inline(self):
        with mock.patch.object(g, "INDEX_INLINE", False), \
                mock.patch.object(g, "_build_index", side_effect=AssertionError("built inline")), \
                mock.patch.object(g, "_index_spawn", return_value=True) as spawn:
            t = time.process_time()
            r = self.bash(py('import sampo.core as c; print(c.fit("a", 2))'))
            spent = time.process_time() - t
        self.assertBlocked(r, "core")
        self.assertIn("being built", r.detail)
        spawn.assert_called_once()
        self.assertLess(spent, 1.0, "a cold tree costs the hook no build")

    def test_the_hook_decides_with_inline_builds_off_on_its_own_context(self):
        from types import SimpleNamespace
        from sampo import hooks
        seen, guard = [], mock.Mock()
        guard.findings.return_value = []
        guard.check.side_effect = lambda tool, inp, ctx, found: seen.append(ctx.index_inline)
        pl = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        with mock.patch.object(hooks, "_guard_ctx", side_effect=lambda *a: (SimpleNamespace(), None, None)):
            hooks._guard_decision(pl, guard)
            hooks._guard_decision(pl, guard, inline=True)  # sampo guard try
        self.assertEqual(seen, [False, True])
        self.assertIs(g.INDEX_INLINE, True, "the module default is untouched (review: a global swap races)")
        with mock.patch.object(g, "_build_index", side_effect=AssertionError("built inline")), \
                mock.patch.object(g, "_index_load", return_value=None), \
                mock.patch.object(g, "_index_spawn", return_value=True):
            ctx = self.ctx()
            ctx.index_inline = False
            self.assertBlocked(g.check("Bash", {"command": py('import sampo.core as c; print(c.fit("a", 2))')}, ctx),
                               "core")

    def test_unparsed_text_naming_a_sampo_import_is_judged_strictly_when_cold(self):
        # review: with no index the text gate was the hand list alone (set_panic is not on it)
        with mock.patch.object(g, "INDEX_INLINE", False), mock.patch.object(g, "_index_spawn", return_value=True):
            r = self.bash("printf 'from sampo import state\\nstate.set_panic(0)' | python3")
        self.assertBlocked(r, "core")
        self.assertIn("Sampo", r.detail)

    def test_no_state_folder_refuses_rather_than_building_in_the_hook(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(g.IndexBroken) as e:
                g._writer_index(d, None, inline=False)
        self.assertEqual(e.exception.cause, "NoStateFolder")

    def test_a_build_that_cannot_start_or_died_is_refused_with_its_cause(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cache:
            stamp = g._index_stamp(d, g._index_files(d))
            with mock.patch("subprocess.Popen", side_effect=OSError("no fork")):
                with self.assertRaises(g.IndexBroken) as e:
                    g._writer_index(d, cache, inline=False)
            self.assertEqual(e.exception.cause, "OSError")
            self.assertEqual(os.listdir(cache), [], "its marker is gone")
            mark = os.path.join(cache, f"ward-index-{stamp}.building")
            open(mark, "w").close()
            os.utime(mark, (time.time() - g.INDEX_BUILD_STALE - 5,) * 2)  # a build that died
            self.assertIsNone(g._writer_index(d, cache, inline=False))
            with self.assertRaises(g.IndexBroken) as e:
                g._writer_index(d, cache, inline=False)
            self.assertEqual(e.exception.cause, "BuildDied")
            self.assertFalse(os.path.exists(mark), "cleared, not taken over")
            failed = os.path.join(cache, f"ward-index-{stamp}.failed")
            os.utime(failed, (time.time() - g.INDEX_RETRY - 5,) * 2)
            with mock.patch.object(g, "_index_spawn", return_value=True) as spawn:
                self.assertIsNone(g._writer_index(d, cache, inline=False), "tried once more after INDEX_RETRY")
            spawn.assert_called_once()

    def test_a_build_that_cannot_write_the_index_records_why(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cache:
            stamp = g._index_stamp(d, g._index_files(d))
            mark = os.path.join(cache, f"ward-index-{stamp}.building")
            open(mark, "w").close()
            with mock.patch.object(g, "_index_store", return_value=False):
                g._index_build_detached(d, cache, stamp, mark)
            with open(os.path.join(cache, f"ward-index-{stamp}.failed")) as f:
                self.assertEqual(f.read(), "OSError")
            self.assertFalse(os.path.exists(mark))

    def test_text_that_only_names_a_sampo_import_is_not_refused_when_cold(self):
        with mock.patch.object(g, "INDEX_INLINE", False), mock.patch.object(g, "_index_spawn", return_value=True):
            self.assertIsNone(self.bash('grep -n "from sampo import core" plugin/lib/sampo/cli.py'))

    def test_one_build_at_a_time_holds_while_the_marker_is_held_and_ends_with_the_build(self):
        """T-5064: the refusal is the O_EXCL marker's, not a timing of the real build. Held by hand it is refused
        without a process; a build that already ran to its end (a test process left waiting for a CPU slot while the
        detached child finished) has cleared it, so the next call is a new build: True, by design."""
        import subprocess
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cache:
            with open(os.path.join(d, "a.py"), "w") as f:
                f.write("def r():\n    return 1\n")
            stamp = g._index_stamp(d, g._index_files(d))
            mark = os.path.join(cache, f"ward-index-{stamp}.building")
            open(mark, "w").close()
            with mock.patch("subprocess.Popen") as popen:
                self.assertFalse(g._index_spawn(d, cache, stamp), "one build at a time")
            popen.assert_not_called()
            self.assertTrue(os.path.exists(mark), "a young marker is left to its build")
            os.remove(mark)
            real = subprocess.Popen

            def finished_first(*a, **k):  # the parent is descheduled until the child is done
                p = real(*a, **k)
                p.wait(60)
                return p
            with mock.patch("subprocess.Popen", side_effect=finished_first):
                self.assertTrue(g._index_spawn(d, cache, stamp))
            self.assertFalse(os.path.exists(mark), "the finished build cleared its marker")
            self.assertTrue(g._index_spawn(d, cache, stamp), "the marker was the only thing holding a second one off")
            end = time.time() + 60
            while time.time() < end and os.path.exists(mark):
                time.sleep(0.05)

    def test_a_detached_build_writes_the_index_once_per_stamp(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cache:
            with open(os.path.join(d, "__init__.py"), "w") as f:
                f.write("")
            with open(os.path.join(d, "a.py"), "w") as f:
                f.write("def r():\n    return 1\n")
            self.assertIsNone(g._writer_index(d, cache, inline=False), "cold: nothing to judge by yet")
            # (no "second spawn is refused" check here: it holds only while the build runs, and a test process that a
            # crowded CI box leaves waiting for a CPU slot sees the build already done: T-5064. See the next test.)
            end = time.time() + 60
            while time.time() < end and not any(n.endswith(".json") for n in os.listdir(cache)):
                time.sleep(0.05)
            g._INDEX_MEMO.clear()
            idx = g._writer_index(d, cache, inline=False)
            self.assertIsNotNone(idx, os.listdir(cache))
            self.assertEqual(idx["modules"]["sampo.a"]["safe"], ["r"])
            end = time.time() + 10
            while time.time() < end and any(n.endswith(".building") for n in os.listdir(cache)):
                time.sleep(0.05)
            self.assertFalse([n for n in os.listdir(cache) if n.endswith(".building")], "the build marker is gone")


if __name__ == "__main__":
    unittest.main()
