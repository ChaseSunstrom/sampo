"""The suite itself (T-0218): a test class that redefines a TestCase method silently breaks it — a helper named fail()
made every assertion in its class pass, because assertions raise through self.fail."""
import ast
import builtins
import collections
import glob
import os
import re
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MEANT = {"setUp", "tearDown", "setUpClass", "tearDownClass", "run", "runTest"}  # overriding these is the design


def unbound(src):
    """T-0698: "line name" for each name read that no builtin is and nothing in the module binds (stdlib ast, no
    pyflakes). Scopes aren't modelled, so it only misses a name also bound somewhere else; a star import skips it."""
    tree, bound = ast.parse(src), set(dir(builtins)) | {"__file__", "__builtins__"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            bound.add(n.id)
        for attr in ("name", "asname", "arg", "rest"):  # def, class, import, argument, except and match names
            if isinstance(getattr(n, attr, None), str):
                bound.add(getattr(n, attr).split(".")[0])
    return [] if "*" in bound else [f"{n.lineno} {n.id}" for n in ast.walk(tree) if isinstance(n, ast.Name)
                                    and isinstance(n.ctx, ast.Load) and n.id not in bound]


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


# T-1264: files whose functions are called by a name built at run time: the prefix, and the file and code that build it
DISPATCHED = {"sampo/smithtui.py": ("_on_", "sampo/smithtui.py", 'f"_on_{kind}"'),
              "sampo/instr.py": ("cmd_", "sampo/cli.py", 'lazy("sampo.instr", "cmd_" + name)'),
              "sampo/outcomes.py": ("_", "sampo/outcomes.py", 'globals()["_" + flag]')}


def dead(lib, words):
    """T-1264: "file:line name" for each function in lib/*.py and lib/sampo/*.py that no word in `words` names besides its
    own def."""
    found = []
    for path in sorted(glob.glob(f"{lib}/*.py") + glob.glob(f"{lib}/sampo/*.py")):
        name = os.path.relpath(path, lib)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        prefix, where, how = DISPATCHED.get(name, (None, None, None))
        if where:
            with open(os.path.join(lib, where), encoding="utf-8") as f:
                prefix = prefix if how in f.read() else None  # the exemption ends with the dispatch
        defs = [n for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        count = collections.Counter(n.name for n in defs)
        for n in defs:
            if n.name.startswith("__") or words[n.name] > count[n.name]:
                continue
            if prefix and n.name.startswith(prefix) and words[n.name[len(prefix):]]:
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


class UndefinedNames(unittest.TestCase):
    def test_every_name_read_is_bound(self):
        # T-0698: renames left _new_context_file, _UPDATE, _veto_note, _mask_substs and _IFS_USE read but bound
        # nowhere, each a NameError at run time (in the guard, a blocked call)
        self.assertEqual(unbound("import os\ndef f(a):\n    return os, a, len, _gone\n"), ["3 _gone"])
        plugin, found = os.path.dirname(HERE), []
        for path in sorted(glob.glob(f"{plugin}/lib/*.py") + glob.glob(f"{plugin}/lib/sampo/*.py") + glob.glob(f"{plugin}/hooks/*")):
            if not os.path.isfile(path):  # T-0783: importing a hook script leaves hooks/__pycache__/
                continue
            with open(path, encoding="utf-8") as f:
                src = f.read()
            if path.endswith(".py") or src.startswith("#!/usr/bin/env python3"):  # the hooks are extensionless
                found += [f"{os.path.relpath(path, plugin)}:{x}" for x in unbound(src)]
        self.assertEqual(found, [])
