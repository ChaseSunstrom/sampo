"""The suite itself (T-0218): a test class that redefines a TestCase method silently breaks it — a helper named fail()
made every assertion in its class pass, because assertions raise through self.fail."""
import ast
import builtins
import collections
import glob
import os
import re
import shutil
import symtable
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MEANT = {"setUp", "tearDown", "setUpClass", "tearDownClass", "run", "runTest"}  # overriding these is the design


SCOPE_NODES = {ast.ListComp: "listcomp", ast.SetComp: "setcomp", ast.DictComp: "dictcomp", ast.GeneratorExp: "genexpr",
               ast.Lambda: "lambda"}


def unbound(src):
    """T-0698: "line name" for each name read that no builtin is and nothing binds where it is read (stdlib ast and
    symtable, no pyflakes); a star import skips the file. T-6927: scopes are modelled. A name a function reads as a
    global must be bound at the module (or by a `global` statement), not only somewhere: smap, imported inside other
    functions of cli.py and read bare in _symbol_names, raised NameError in every affected(dependent=...) call."""
    tree = ast.parse(src)
    if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in ast.walk(tree)):
        return []
    tables, todo = [], [symtable.symtable(src, "<src>", "exec")]
    while todo:
        tables.append(todo.pop())
        todo += tables[-1].get_children()
    known = set(dir(builtins)) | {"__file__", "__builtins__"}
    known |= {s.get_name() for t in tables for s in t.get_symbols()
              if (t is tables[0] or s.is_declared_global()) and (s.is_assigned() or s.is_imported() or s.is_namespace())}
    nodes = {("top", 0): tree}
    for n in ast.walk(tree):
        name = n.name if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else SCOPE_NODES.get(type(n))
        if name:
            nodes.setdefault((name, n.lineno), n)
    found = set()
    for t in tables:
        for s in t.get_symbols():
            # read as a global, bound nowhere global; not is_global(): symtable takes any table named "top" for the
            # module, so a function top()'s own locals would read as globals
            if s.is_referenced() and not s.is_free() and (t is tables[0] or not s.is_local()) and s.get_name() not in known:
                where = nodes.get((t.get_name(), t.get_lineno()), tree)  # an annotation scope: the whole module
                found |= {(n.lineno, n.id) for n in ast.walk(where)
                          if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == s.get_name()}
    return [f"{line} {name}" for line, name in sorted(found)]


def inherited_tests(src):
    """T-3815: "line Class(Base)" for each class that runs a test_ method its module-local base (directly or through
    another) defines, unless its docstring says 'reruns:' why. A test the class defines itself, or sets to None in its
    body (`test_x = None`, or the locals().update idiom that nulls the base's tests), is not run again. A base with no
    tests of its own is free to share."""
    classes = {n.name: n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)}

    def tests(name, seen=()):
        cls = classes.get(name)
        if cls is None or name in seen:
            return set()
        own = {f.name for f in cls.body if isinstance(f, ast.FunctionDef) and f.name.startswith("test_")}
        return own | set().union(*(tests(b.id, seen + (name,)) for b in cls.bases if isinstance(b, ast.Name)))

    found = []
    for name, cls in classes.items():
        hidden = {f.name for f in cls.body if isinstance(f, ast.FunctionDef)}
        for st in cls.body:
            if isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant) and st.value.value is None:
                hidden |= {t.id for t in st.targets if isinstance(t, ast.Name)}
            if isinstance(st, ast.Expr) and "locals().update" in ast.unparse(st):
                hidden |= {n for b in cls.bases if isinstance(b, ast.Name) for n in tests(b.id)}
        bases = [b.id for b in cls.bases if isinstance(b, ast.Name) and tests(b.id) - hidden]
        reruns = any(line.strip().startswith("reruns:") for line in (ast.get_docstring(cls) or "").splitlines())
        if bases and not reruns:
            found.append(f"{cls.lineno} {name}({', '.join(bases)})")
    return found


class SuiteHygiene(unittest.TestCase):
    def test_tests_never_see_the_machines_real_load(self):
        # T-0744: under load (a finish's full run beside other work) host_strain() turned lanes and ambient runs off,
        # and test_hooks.Stop, test_ambient and test_orchestration failed only then
        import helpers  # noqa: F401  (it pins the host every test sees)
        from sampo import core
        self.assertIn("load=", os.environ.get("FOREMAN_HOST", ""))
        self.assertIsNone(core.host_strain())

    def test_a_test_that_replaces_path_still_cant_reach_the_paid_claude(self):
        # T-0993: fmcore's native-install fallback (T-0990) found the real ~/.local/bin/claude for tests that replace
        # PATH, around the lockout (T-0992); every fm a test starts inherits this environment
        import tempfile
        import helpers  # noqa: F401  (it sets the lockout)
        from sampo import core
        with tempfile.TemporaryDirectory() as empty:
            env = dict(os.environ, PATH=empty, HOME=os.path.expanduser("~"))
            self.assertEqual(core.claude_path(env)["PATH"], empty)

    def test_the_screenshot_harness_runs_on_a_socket_of_its_own(self):
        # T-0744: two suites at once shared tmux -L fm-shot and killed each other's session
        with open(os.path.join(os.path.dirname(__file__), "test_shot.py"), encoding="utf-8") as f:
            self.assertIn("FM_SHOT_SOCKET", f.read())

    def test_no_test_class_shadows_a_testcase_method(self):
        inherited = {n for n in dir(unittest.TestCase) if not n.startswith("__")} - MEANT
        found = []
        for path in sorted(glob.glob(os.path.join(HERE, "**", "*.py"), recursive=True)):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                found += [f"{os.path.relpath(path, HERE)}:{fn.lineno} {cls.name}.{fn.name}" for fn in cls.body
                          if isinstance(fn, ast.FunctionDef) and fn.name in inherited]
        self.assertEqual(found, [])

    def test_inherited_test_methods_run_only_where_the_docstring_says_reruns(self):
        # T-3815: a subclass that inherits another class's test_ methods runs them again (238 runs in the suite, most
        # wanting only the base's setUp); a deliberate rerun under another fixture says 'reruns:' why in its docstring
        self.maxDiff = None
        self.assertEqual(inherited_tests("class A(T):\n    def test_x(self):\n        pass\nclass B(A):\n    pass\n"),
                         ["4 B(A)"])
        self.assertEqual(inherited_tests("class H(T):\n    def setUp(self):\n        pass\n"
                                         "class B(H):\n    def test_y(self):\n        pass\n"), [])
        self.assertEqual(inherited_tests('class A(T):\n    def test_x(self):\n        pass\n'
                                         'class B(A):\n    """reruns: under a fault."""\n'), [])
        self.assertEqual(inherited_tests("class A(T):\n    def test_x(self):\n        pass\n"
                                         "class M(A):\n    pass\nclass C(M):\n    pass\n"), ["4 M(A)", "6 C(M)"])
        self.assertEqual(inherited_tests("class A(T):\n    def test_x(self):\n        pass\nclass B(A):\n    test_x = None\n"), [])
        self.assertEqual(inherited_tests("class A(T):\n    def test_x(self):\n        pass\n"
                                         "class B(A):\n    locals().update({n: None for n in dir(A)})\n"), [])
        found = []
        for path in sorted(glob.glob(os.path.join(HERE, "**", "*.py"), recursive=True)):
            with open(path, encoding="utf-8") as f:
                found += [f"{os.path.relpath(path, HERE)}:{x}" for x in inherited_tests(f.read())]
        self.assertEqual(found, [])


# T-1264: files whose functions are called by a name built at run time: per file, each prefix with the file and code that
# build it (T-3699: the Board builds two, m_<panel> and p_<panel>)
DISPATCHED = {"sampo/smithtui.py": [("_on_", "sampo/smithtui.py", 'f"_on_{kind}"')],
              "sampo/instr.py": [("cmd_", "sampo/cli.py", 'lazy("sampo.instr", "cmd_" + name)')],
              "sampo/outcomes.py": [("_", "sampo/outcomes.py", 'globals()["_" + flag]')],
              "sampo/smithboard.py": [("m_", "sampo/smithboard.py", 'globals()[f"m_{k}"]'),
                                      ("p_", "sampo/smithboard.py", 'globals()[f"p_{k}"]')]}


def dead(lib, words):
    """T-1264: "file:line name" for each function in lib/*.py and lib/sampo/*.py that no word in `words` names besides its
    own def."""
    found = []
    for path in sorted(glob.glob(f"{lib}/*.py") + glob.glob(f"{lib}/sampo/*.py")):
        name = os.path.relpath(path, lib)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        prefixes = []
        for prefix, where, how in DISPATCHED.get(name, []):
            with open(os.path.join(lib, where), encoding="utf-8") as f:
                if how in f.read():  # the exemption ends with the dispatch
                    prefixes.append(prefix)
        defs = [n for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        count = collections.Counter(n.name for n in defs)
        for n in defs:
            if n.name.startswith("__") or words[n.name] > count[n.name]:
                continue
            if any(n.name.startswith(p) and words[n.name[len(p):]] for p in prefixes):
                continue
            found.append(f"{name}:{n.lineno} {n.name}")
    return found


class DeadFunctions(unittest.TestCase):
    def test_every_lib_function_is_named_somewhere_besides_its_def(self):
        # T-1264: a function nothing calls, tests, documents or dispatches to is dead weight every reader pays for
        repo, words = os.path.dirname(os.path.dirname(HERE)), collections.Counter()
        for top in ("plugin", "mods", "site"):
            for d, dirs, files in os.walk(os.path.join(repo, top)):
                dirs[:] = [x for x in dirs if x not in ("node_modules", "__pycache__", ".git")]
                for f in files:
                    if f.endswith((".py", ".ts", ".tsx", ".md", ".json", ".toml", ".sh", ".cjs", ".js")) or "." not in f:
                        with open(os.path.join(d, f), encoding="utf-8", errors="ignore") as fh:
                            words.update(re.findall(r"\w+", fh.read()))
        self.assertEqual(dead(os.path.join(repo, "plugin", "lib"), words), [])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "fmx.py"), "w") as f:
            f.write("def used():\n    pass\n\ndef _gone():\n    pass\n")
        self.assertEqual(dead(tmp, collections.Counter(["used", "used", "_gone"])), ["fmx.py:4 _gone"])


def config_dir_sets(src):
    """T-6020: [(line, value node or None)] for each place a module SETS CLAUDE_CONFIG_DIR: a dict key, a keyword argument,
    an item store, setdefault/putenv/setenv, through a name bound to the variable's name too (reads, pops and the name
    inside a stub script's text are not sets). None: the value is not in sight."""
    tree = ast.parse(src)
    alias = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
             and n.value.value == "CLAUDE_CONFIG_DIR" for t in n.targets if isinstance(t, ast.Name)}

    def is_key(k):
        return isinstance(k, ast.Constant) and k.value == "CLAUDE_CONFIG_DIR" or isinstance(k, ast.Name) and k.id in alias
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            found += [(k.lineno, v) for k, v in zip(n.keys, n.values) if is_key(k)]
        elif isinstance(n, ast.keyword) and n.arg == "CLAUDE_CONFIG_DIR":
            found.append((n.value.lineno, n.value))
        elif isinstance(n, ast.Assign):
            found += [(t.lineno, n.value) for t in n.targets if isinstance(t, ast.Subscript) and is_key(t.slice)]
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("setdefault", "putenv", "setenv") \
                and n.args and is_key(n.args[0]):
            found.append((n.lineno, n.args[1] if len(n.args) > 1 else None))
    return sorted(found, key=lambda x: x[0])


def unsafe_config_dir_sets(src):
    """T-6020: the lines of each set of CLAUDE_CONFIG_DIR whose value is not a safe_config_dir() call, or a name or
    attribute that the module binds only from one (a parameter or any other binding is unsafe)."""
    tree = ast.parse(src)

    def safe_call(v):
        return isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "safe_config_dir"

    bound = {}  # "name" or ".attr" -> every value assigned to it in the module
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                key = t.id if isinstance(t, ast.Name) else "." + t.attr if isinstance(t, ast.Attribute) else None
                if key:
                    bound.setdefault(key, []).append(n.value)

    def safe(v):
        if safe_call(v):
            return True
        key = v.id if isinstance(v, ast.Name) else "." + v.attr if isinstance(v, ast.Attribute) else None
        return bool(key) and bool(bound.get(key)) and all(safe_call(x) for x in bound[key])
    return [line for line, v in config_dir_sets(src) if not safe(v)]


# T-6020: files that set CLAUDE_CONFIG_DIR to a folder under the test's own /tmp, each with why no headless child
# (ideas.child_env, which refuses a folder under one others can write: T-5706) starts from it. Found by tracing
# config_dir_problem over every module that sets it: the ones that did reach a launcher use safe_config_dir. A file that
# names a launcher entry point must say in its reason what is mocked; test_never_launches_is_checked holds the rest.
_COST = "`fm cost` and the usage readers parse transcripts under it; no child"
_FOLDER = "`fm` commands read or write plugins, settings or skills under it; no child"
_HOOKS = "a hook or a lane-side `fm` command reads its transcripts and settings; no child"
CONFIG_DIR_NEVER_LAUNCHES = {
    "helpers.py": "the default env of fm(): a folder that does not exist, so no real account is read; a test that starts a "
                  "child passes its own env",
    "test_child_launch.py": "it tests the refusal itself, with the ancestor check mocked where a child must start",
    "test_rename_scan.py": "scans the tree for renamed words with the variable set; starts nothing",
    "test_ideas.py": "`fm cost --sessions` reads transcripts under it; its run_child is mocked (mock.patch.object)",
    "test_bench.py": "the stub claude keeps its transcript there; bench runs the claude itself, not through ideas.child_env",
    "test_claude.py": "the stub claude keeps its transcript there; the argv tests run it directly",
    "test_perf_cost.py": _COST, "test_cost.py": _COST, "test_cost_forecast.py": _COST, "test_cost_sources.py": _COST,
    "test_speed.py": _COST, "test_usage_age.py": _COST, "test_economics.py": _COST, "test_flow_report.py": _COST,
    "test_clock_windows.py": _COST, "test_steermeter.py": _COST, "test_friction.py": _COST, "test_repeats.py": _COST,
    "test_followups.py": _COST, "test_concerns.py": _COST, "test_concerns_order.py": _COST,
    "test_concerns_repeat.py": _COST, "test_round_f.py": _COST, "test_skillroute.py": _COST, "test_lib_hostile.py": _COST,
    "test_tool_proof.py": _COST, "test_ahead.py": _COST,
    "test_plugins.py": _FOLDER, "test_settings.py": _FOLDER, "test_update.py": _FOLDER, "test_library.py": _FOLDER,
    "test_supply_delta.py": _FOLDER, "test_which.py": _FOLDER, "test_migrate.py": _FOLDER, "test_targets.py": _FOLDER,
    "test_sandbox.py": _FOLDER, "test_profile.py": _FOLDER, "test_code_skills.py": _FOLDER, "test_doctor.py": _FOLDER,
    "test_private_terms.py": _FOLDER,
    "test_hooks.py": _HOOKS + "; it names ideas.fresh_cwd only in a comment on the scratch folder its setUp makes itself, "
                     "so no launcher runs and none needs a mock", "test_long.py": _HOOKS, "test_keeper.py": _HOOKS, "test_guard_cases.py": _HOOKS,
    "test_guard_mine.py": _HOOKS, "test_pilotsig.py": _HOOKS, "test_flywheel.py": _HOOKS, "test_tool_queue.py": _HOOKS,
}
LAUNCHER_NAMES = re.compile(r"\b(run_child|run_children|child_env|fresh_cwd)\b")


class ConfigDir(unittest.TestCase):
    def test_the_walker_finds_each_way_a_test_sets_it(self):
        src = ("import os\nf({'CLAUDE_CONFIG_DIR': 1})\nf(CLAUDE_CONFIG_DIR=2)\nos.environ['CLAUDE_CONFIG_DIR'] = 3\n"
               "os.environ.pop('CLAUDE_CONFIG_DIR')\nx = os.environ['CLAUDE_CONFIG_DIR']\ns = 'CLAUDE_CONFIG_DIR=4'\n")
        self.assertEqual([line for line, _ in config_dir_sets(src)], [2, 3, 4])

    def test_the_walker_judges_each_set_by_itself(self):
        # T-6020 review: one safe_config_dir() call in a file must not hide another set that is under /tmp
        src = ("x = self.safe_config_dir()\nf({'CLAUDE_CONFIG_DIR': x})\nf(CLAUDE_CONFIG_DIR=os.path.join(t, 'cc'))\n"
               "self.cc = self.safe_config_dir()\nf(CLAUDE_CONFIG_DIR=self.cc)\nf(CLAUDE_CONFIG_DIR=self.safe_config_dir())\n"
               "y = self.safe_config_dir()\ny = os.path.join(t, 'y')\nf(CLAUDE_CONFIG_DIR=y)\nf(CLAUDE_CONFIG_DIR=param)\n")
        self.assertEqual(unsafe_config_dir_sets(src), [3, 9, 10])

    def test_the_walker_finds_the_other_spellings(self):
        src = ("import os\nos.environ.setdefault('CLAUDE_CONFIG_DIR', a)\nos.putenv('CLAUDE_CONFIG_DIR', b)\n"
               "K = 'CLAUDE_CONFIG_DIR'\nf({K: c})\nos.environ[K] = d\nmp.setenv('CLAUDE_CONFIG_DIR', e)\n"
               "os.environ.update(CLAUDE_CONFIG_DIR=g)\nos.environ.update({'CLAUDE_CONFIG_DIR': h})\n")
        self.assertEqual(unsafe_config_dir_sets(src), [2, 3, 5, 6, 7, 8, 9])

    def test_every_test_that_sets_it_uses_safe_config_dir_or_never_starts_a_child(self):
        # T-6020: T-5706's launcher refuses a CLAUDE_CONFIG_DIR under a folder others can write, which /tmp is: a test that
        # reaches ideas.run_child or run_children (a stub claude) with one under self.tmp reads "didn't run", red
        bad, listed = [], set()
        for path in sorted(glob.glob(os.path.join(HERE, "**", "*.py"), recursive=True)):
            name = os.path.relpath(path, HERE)
            with open(path, encoding="utf-8") as f:
                unsafe = unsafe_config_dir_sets(f.read())
            if unsafe and name in CONFIG_DIR_NEVER_LAUNCHES:
                listed.add(name)
            elif unsafe:
                bad.append(f"{name}: lines {unsafe}")
        self.assertEqual(bad, [], "set CLAUDE_CONFIG_DIR from self.safe_config_dir() (helpers), or list the file in "
                                 "CONFIG_DIR_NEVER_LAUNCHES with why no headless child starts from it")
        self.assertEqual(sorted(set(CONFIG_DIR_NEVER_LAUNCHES) - listed), [],
                         "listed but every set of it is now safe_config_dir (or the file is gone): drop it")

    def test_never_launches_is_checked(self):
        # T-6020 review: a reason is text; this holds what can be held: a file that names a launcher entry point says what
        # is mocked, and no reason is empty
        for name, why in CONFIG_DIR_NEVER_LAUNCHES.items():
            self.assertGreater(len(why), 30, name)
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                if LAUNCHER_NAMES.search(f.read()):
                    self.assertIn("mock", why, f"{name} names a launcher entry point: its reason must say what is mocked")


class UndefinedNames(unittest.TestCase):
    def test_every_name_read_is_bound(self):
        # T-0698: renames left _new_context_file, _UPDATE, _veto_note, _mask_substs and _IFS_USE read but bound
        # nowhere, each a NameError at run time (in the guard, a blocked call)
        self.assertEqual(unbound("import os\ndef f(a):\n    return os, a, len, _gone\n"), ["3 _gone"])
        # T-6927: bound only inside another function is unbound here (cli._symbol_names read smap so); a global
        # statement, a nested function's free name, a class body, a lambda and a comprehension are each modelled
        self.assertEqual(unbound("def f():\n    import json as smap\n    return smap\n"
                                 "def g():\n    return smap\n"), ["5 smap"])
        self.assertEqual(unbound("def f():\n    global late\n    late = 1\ndef g():\n    return late\n"
                                 "def h(a):\n    def i():\n        return a\n    return i\n"
                                 "class C:\n    y = 1\n    z = [_ for _ in (y,)]\n"), [])  # the first iterable: the class's
        self.assertEqual(unbound("x = 1\nclass C:\n    y = x\n    def m(self):\n        return y\n"
                                 "f = lambda: gone\nl = [gone2 for _ in ()]\n"), ["5 y", "6 gone", "7 gone2"])
        self.assertEqual(unbound("from os import *\ndef f():\n    return getcwd\n"), [])
        self.assertEqual(unbound("def top(p):\n    r = p\n    return r, gone\n"), ["3 gone"])  # the module's table name
        plugin, found = os.path.dirname(HERE), []
        for path in sorted(glob.glob(f"{plugin}/lib/*.py") + glob.glob(f"{plugin}/lib/sampo/*.py") + glob.glob(f"{plugin}/hooks/*")):
            if not os.path.isfile(path):  # T-0783: importing a hook script leaves hooks/__pycache__/
                continue
            with open(path, encoding="utf-8") as f:
                src = f.read()
            if path.endswith(".py") or src.startswith("#!/usr/bin/env python3"):  # the hooks are extensionless
                found += [f"{os.path.relpath(path, plugin)}:{x}" for x in unbound(src)]
        self.assertEqual(found, [])
