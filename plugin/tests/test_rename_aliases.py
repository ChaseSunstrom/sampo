"""T-2748: the library lives in plugin/lib/sampo/ (T-2742); each old plugin/lib/fmX.py is a shim kept for two releases.
An old import path must give the very same module object (so private names, mock.patch targets and module state are
shared), and nothing but the shims, fmguard (which keeps its name until T-2745) and this test names an old module."""
import ast
import importlib
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from helpers import LIB, PLUGIN, SampoTestCase

SHIM = re.compile(r'"""Old import path, kept for two releases \(T-2742\): the module is (sampo\.\w+)\."""\n'
                  r'import sys\nimport \1\nsys\.modules\[__name__\] = sys\.modules\["\1"\]\n\Z')
CALLS = {"lazy", "import_module", "__import__", "find_spec", "patch"}  # their first argument names a module


def shims():
    """{old name: the sampo module its shim names, None when the file isn't exactly a shim}."""
    out = {}
    for f in sorted(os.listdir(LIB)):
        if f.startswith("fm") and f.endswith(".py") and f != "fmguard.py":
            with open(os.path.join(LIB, f), encoding="utf-8") as fh:
                m = SHIM.match(fh.read())
            out[f[:-3]] = m.group(1) if m else None
    return out


def sources(root):
    """(path relative to root, text) of every tracked or new Python file: *.py and the python-shebang scripts."""
    r = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       capture_output=True, text=True, timeout=60)
    rels = r.stdout.split("\0") if r.returncode == 0 else [
        os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(PLUGIN) for f in fs]
    for rel in sorted(set(filter(None, rels))):
        path = os.path.join(root, rel)
        script = rel.startswith(("plugin/bin/", "plugin/hooks/")) and "." not in os.path.basename(rel)
        if not (rel.endswith(".py") or script) or not os.path.isfile(path) or os.path.islink(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if rel.endswith(".py") or (text.startswith("#!") and "python" in text.split("\n", 1)[0]):
            yield rel, text


def old_names(tree, old):
    """(line, what) for each import, module-naming call (lazy, importlib, patch targets), sys.modules key and
    attribute use (fmx.f, a merge's leftover with no import behind it) that names an old module."""
    head = lambda s: s.split(".", 1)[0]
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            yield from ((n.lineno, f"import {a.name}") for a in n.names if head(a.name) in old)
        elif isinstance(n, ast.ImportFrom) and not n.level and head(n.module or "") in old:
            yield n.lineno, f"from {n.module} import"
        elif isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            owner = getattr(f.value, "attr", getattr(f.value, "id", "")) if isinstance(f, ast.Attribute) else ""
            if name in CALLS or owner == "patch":
                for a in [*n.args, *(k.value for k in n.keywords)]:  # any argument, keywords too
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and head(a.value) in old:
                        yield n.lineno, f"{name}({a.value!r})"
                    elif isinstance(a, ast.JoinedStr) and a.values and isinstance(a.values[0], ast.Constant) and \
                            re.match(r"fm([a-z]|$)", str(a.values[0].value)):  # f'fm{x}': only the constant part shows
                        yield n.lineno, f"{name}(f'{a.values[0].value}…')"
        elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and n.value.attr == "modules" and \
                isinstance(n.slice, ast.Constant) and head(str(n.slice.value)) in old:
            yield n.lineno, f"sys.modules[{n.slice.value!r}]"
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in old:
            yield n.lineno, f"{n.value.id}.{n.attr}"


class RenameAliases(SampoTestCase):
    def test_every_old_module_is_a_shim_and_every_sampo_module_has_one(self):
        got = shims()
        self.assertEqual([k for k, v in got.items() if v is None], [], "not exactly the shim")
        moved = {"sampo." + f[:-3] for f in os.listdir(os.path.join(LIB, "sampo"))
                 if f.endswith(".py") and f != "__init__.py"}
        self.assertEqual(sorted(set(got.values()) ^ moved), [], "a sampo module without its shim, or the reverse")
        self.assertEqual(len(set(got.values())), len(got), "two shims name one module")
        self.assertTrue(os.path.isfile(os.path.join(LIB, "fmguard.py")), "fmguard stays where it is (T-2745)")
        with open(os.path.join(LIB, "sampo", "__init__.py"), encoding="utf-8") as fh:
            self.assertEqual([n for n in ast.parse(fh.read()).body if not isinstance(n, ast.Expr)], [],
                             "importing the package loads nothing")

    def test_an_old_import_path_is_the_same_module_object(self):
        for old, new in shims().items():
            with self.subTest(old):
                self.assertIs(importlib.import_module(old), importlib.import_module(new))
                self.assertEqual(sys.modules[old].__name__, new)
        core = importlib.import_module("sampo.core")
        self.assertEqual(os.path.realpath(core.PLUGIN_ROOT), os.path.realpath(PLUGIN), "a moved module's __file__")

    def test_the_old_path_imported_first_in_a_fresh_interpreter_is_the_module_too(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import fmcore, fmrisk, fmgenesis; import sampo.core, "
                "sampo.risk, sampo.rune; from fmcore import _writable; "
                "assert fmcore is sampo.core and fmrisk is sampo.risk and fmgenesis is sampo.rune; "
                "assert _writable is sampo.core._writable; print('ok')")
        r = subprocess.run([sys.executable, "-I", "-c", code, LIB], capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr[-2000:])

    def test_private_names_resolve_through_the_old_path(self):
        import fmcore
        from fmcore import _writable
        from sampo import core
        self.assertIs(fmcore._writable, core._writable)
        self.assertIs(_writable, core._writable)

    def test_patching_the_old_path_patches_the_module(self):
        from sampo import foresight
        real = foresight.setup_now
        with mock.patch("fmforesight.setup_now", return_value="patched"):
            self.assertEqual(foresight.setup_now(), "patched")
        self.assertIs(foresight.setup_now, real)
        with mock.patch.dict("fmforesight.__dict__", {"STALE_DAYS": -1}):
            self.assertEqual(foresight.STALE_DAYS, -1)
        self.assertNotEqual(foresight.STALE_DAYS, -1)

    def test_nothing_but_the_shims_names_an_old_module(self):
        old = set(shims())
        root = os.path.dirname(PLUGIN)
        skip = {f"plugin/lib/{n}.py" for n in old} | {"plugin/lib/fmguard.py", "plugin/tests/test_rename_aliases.py"}
        bad = []
        for rel, text in sources(root):
            if rel in skip:
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError as e:  # unread is unchecked: a file the scan can't parse fails it
                bad.append(f"{rel}:{e.lineno}: unparsable, so unscanned ({e.msg})")
                continue
            bad += [f"{rel}:{line}: {what}" for line, what in old_names(tree, old)]
        self.assertEqual(bad, [])

    def test_sampo_prints_its_own_name_not_fm(self):  # T-4020: "fm: refused: …" showed the old name in every chat
        r = self.fm("focus", "T-9999", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(r.stderr.startswith("sampo: "), r.stderr)
        old = re.compile(r"""(?:print|write)\(\s*f?["']fm: |["']Next: fm |"text": "fm: """)
        root = os.path.dirname(PLUGIN)
        bad = [f"{rel}:{text.count(chr(10), 0, m.start()) + 1}" for rel, text in sources(root)
               if rel.startswith("plugin/") and "/tests/" not in rel for m in old.finditer(text)]
        self.assertEqual(bad, [])

    def test_completion_and_help_teach_the_sampo_names(self):  # T-2775
        from sampo import cli
        words = lambda *w: self.fm("complete", "--", *w).stdout.split()  # noqa: E731
        self.assertLessEqual(set(cli.RENAMES), set(words("")))
        self.assertEqual(words("smith", "") , words("code", ""))
        self.assertEqual(words("rune", ""), words("genesis", ""))
        self.assertEqual(words("moons", ""), words("genesis", "moonshot", ""))
        self.assertEqual(words("underworld", ""), words("genesis", "underworld", ""))
        self.assertNotIn("smith", words("task", ""))  # the names are verbs, not subcommands
        hook = self.fm("complete", "--shell", "bash").stdout
        self.assertIn("complete -F _fm fm sampo", hook)
        text = self.fm("help", "--all").stdout
        for name, run in cli.RENAMES.items():
            self.assertRegex(text, rf"{name}\s+fm {' '.join(run)}\b")

    def test_the_scan_sees_each_form(self):
        tree = ast.parse("import fmx\nfrom fmx.y import z\nlazy('fmx', 'f')\nimportlib.import_module('fmx')\n"
                         "__import__('fmx')\nmock.patch('fmx.f')\nmock.patch.dict('fmx.__dict__', {})\n"
                         "sys.modules['fmx']\nimport fmguard\nlazy('sampo.x', 'f')\nprint('fmx')\n"
                         "fmx.f(design.g)\n")  # a merge can bring in a use of the old name with no import of it
        self.assertEqual([line for line, _ in old_names(tree, {"fmx"})], [1, 2, 3, 4, 5, 6, 7, 8, 12])

    def test_the_scan_sees_keyword_second_argument_and_fstring_targets(self):  # T-2783
        tree = ast.parse("mock.patch('sampo.x', new=1, target='fmx.y')\nmock.patch(target='fmx.f')\n"
                         "importlib.import_module(f'fm{name}')\nlazy('sampo.x', 'fmx')\nfind_spec(name=f'fmx.{y}')\n"
                         "print(f'fm{name}')\nlazy(f'{p}.x', 'f')\nlazy('sampo.x', 'f', 'fmxy')\n")
        self.assertEqual([line for line, _ in old_names(tree, {"fmx"})], [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
