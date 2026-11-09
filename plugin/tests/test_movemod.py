"""T-3274 (Moon C-010030): sampo move OLD NEW renames a Sampo module and rewrites its imports, dotted uses, file paths,
test names and fm shim in code, tests and docs, then proves it by importing every module in a fresh interpreter.
Each test builds a tiny git repo with a package of its own, so the real package is never touched."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from sampo import movemod  # noqa: E402

FILES = {
    "plugin/lib/tiny/__init__.py": "",
    "plugin/lib/tiny/a.py": "VALUE = 1\n",
    "plugin/lib/tiny/b.py": ("from tiny import a\nfrom tiny.a import VALUE\nimport tiny.a as aa\n"
                             "from tiny import a as q\n\n\ndef f():\n    return a.VALUE + VALUE + aa.VALUE + q.VALUE\n\n\n"
                             'LAZY = "tiny.a"\n'),
    "plugin/lib/fma.py": ('"""Old import path."""\nimport sys\nimport tiny.a\n'
                          'sys.modules[__name__] = sys.modules["tiny.a"]\n'),
    "plugin/tests/test_a.py": "from tiny import a\nimport tiny.a\n\n\ndef test_it():\n    assert a.VALUE == 1\n",
    "docs/a.md": "The module plugin/lib/tiny/a.py, run `python3 plugin/tests/run.py test_a`, and tiny.a.\n",
    "CHANGELOG.md": "- tiny.a (T-1): the first entry, history stays as written\n",
    "MASTER.md": "- `fma` (the old path) [→ docs/a.md]\n",
}


def git(root, *args):
    return subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, timeout=60, check=True).stdout


def make_repo():
    root = tempfile.mkdtemp(prefix="movemod-")
    for rel, text in FILES.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def snapshot(root):
    """Every file's bytes plus the index, so 'writes nothing' and 'rolled back' are exact claims."""
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != ".git"]
        for n in fns:
            full = os.path.join(dp, n)
            with open(full, "rb") as f:
                out[os.path.relpath(full, root)] = f.read()
    return out, git(root, "ls-files")


def read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


class MoveModTest(unittest.TestCase):

    def setUp(self):
        self.root = make_repo()

    def test_dry_run_names_every_site_and_writes_nothing(self):
        before = snapshot(self.root)
        rep = movemod.run(self.root, "a", "c", pkg="plugin/lib/tiny")
        self.assertEqual(snapshot(self.root), before, "a dry run wrote something")
        self.assertFalse(rep["applied"])
        self.assertEqual(rep["blockers"], [])
        sites = {s["file"] for s in rep["sites"]}
        self.assertTrue({"plugin/lib/tiny/b.py", "plugin/tests/test_a.py", "docs/a.md", "MASTER.md"} <= sites, sites)
        self.assertNotIn("CHANGELOG.md", sites, "history is never rewritten")
        self.assertIn(("plugin/lib/tiny/a.py", "plugin/lib/tiny/c.py"), [tuple(m) for m in rep["moves"]])

    def test_apply_moves_and_rewrites_without_alias(self):
        rep = movemod.run(self.root, "a", "c", apply=True, pkg="plugin/lib/tiny")
        self.assertTrue(rep["applied"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "plugin/lib/tiny/a.py")), "an alias was left behind")
        self.assertTrue(os.path.exists(os.path.join(self.root, "plugin/lib/tiny/c.py")))
        self.assertIn("plugin/lib/tiny/c.py", git(self.root, "ls-files"), "git mv didn't stage the module")
        b = read(self.root, "plugin/lib/tiny/b.py")
        self.assertIn("from tiny import c\n", b)
        self.assertIn("from tiny.c import VALUE", b)
        self.assertIn("import tiny.c as aa", b)
        self.assertIn("from tiny import c as q", b)
        self.assertIn("return c.VALUE + VALUE + aa.VALUE + q.VALUE", b)
        self.assertIn('LAZY = "tiny.c"', b)
        self.assertTrue(os.path.exists(os.path.join(self.root, "plugin/tests/test_c.py")), "test name not moved")
        self.assertIn("from tiny import c", read(self.root, "plugin/tests/test_c.py"))
        doc = read(self.root, "docs/a.md")
        self.assertIn("plugin/lib/tiny/c.py", doc)
        self.assertIn("test_c`", doc)
        self.assertIn("and tiny.c.", doc)
        self.assertIn("`fmc`", read(self.root, "MASTER.md"))
        self.assertTrue(os.path.exists(os.path.join(self.root, "plugin/lib/fmc.py")), "fm shim not renamed")
        self.assertIn("tiny.c", read(self.root, "plugin/lib/fmc.py"))
        self.assertEqual(read(self.root, "CHANGELOG.md"), FILES["CHANGELOG.md"])

    def test_apply_refuses_an_existing_new_a_non_module_old_and_a_name_clash(self):
        with open(os.path.join(self.root, "plugin/lib/tiny/c.py"), "w", encoding="utf-8") as f:
            f.write("X = 1\n")
        before = snapshot(self.root)
        with self.assertRaises(movemod.MoveError):
            movemod.run(self.root, "a", "c", apply=True, pkg="plugin/lib/tiny")
        self.assertEqual(snapshot(self.root), before)
        with self.assertRaises(movemod.MoveError):
            movemod.run(self.root, "nope", "x", apply=True, pkg="plugin/lib/tiny")
        with self.assertRaises(movemod.MoveError):
            movemod.run(self.root, "a", "a", apply=True, pkg="plugin/lib/tiny")
        self.assertEqual(snapshot(self.root), before)

    def test_a_bare_name_clash_in_a_file_blocks_the_move(self):
        with open(os.path.join(self.root, "plugin/lib/tiny/b.py"), "a", encoding="utf-8") as f:
            f.write("c = 3\n")
        before = snapshot(self.root)
        with self.assertRaises(movemod.MoveError) as ctx:
            movemod.run(self.root, "a", "c", apply=True, pkg="plugin/lib/tiny")
        self.assertIn("b.py", str(ctx.exception))
        self.assertEqual(snapshot(self.root), before)

    def test_alias_leaves_a_sys_modules_shim_like_the_fm_shims(self):
        movemod.run(self.root, "a", "c", apply=True, alias=True, pkg="plugin/lib/tiny")
        self.assertTrue(os.path.exists(os.path.join(self.root, "plugin/lib/tiny/a.py")), "no shim at the old path")
        shim = read(self.root, "plugin/lib/tiny/a.py")
        self.assertIn('sys.modules[__name__] = sys.modules["tiny.c"]', shim)
        self.assertIn("import tiny.c", shim)
        self.assertTrue(os.path.exists(os.path.join(self.root, "plugin/lib/tiny/c.py")))
        self.assertEqual(read(self.root, "plugin/lib/tiny/__init__.py"), "", "an untouched module changed")
        self.assertEqual(read(self.root, "plugin/lib/fma.py"), FILES["plugin/lib/fma.py"], "the old fm shim changed")
        self.assertIn("from tiny.c import VALUE", read(self.root, "plugin/lib/tiny/b.py"))
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import tiny.a as m; "
                 "assert m.__name__ == 'tiny.c' and m.VALUE == 1, m.__name__")
        subprocess.run([sys.executable, "-I", "-c", probe, os.path.join(self.root, "plugin/lib")],
                       capture_output=True, text=True, timeout=60, check=True)

    def test_a_module_that_already_fails_does_not_block_the_move(self):
        with open(os.path.join(self.root, "plugin/lib/tiny/broken.py"), "w", encoding="utf-8") as f:
            f.write('raise RuntimeError("optional dependency missing")\n')
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "a module that was broken before the move")
        rep = movemod.run(self.root, "a", "c", apply=True, pkg="plugin/lib/tiny")
        self.assertTrue(rep["applied"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "plugin/lib/tiny/c.py")))
        self.assertEqual(rep["proof"]["failed"], {}, "a module broken before the move was blamed on it")
        self.assertEqual(rep["proof"]["was_failing"], ["tiny.broken"])

    def test_a_failed_import_rolls_the_move_back(self):
        # broken.py imports fine before the move; the move makes it fail by putting c.py beside it
        with open(os.path.join(self.root, "plugin/lib/tiny/broken.py"), "w", encoding="utf-8") as f:
            f.write('import os\nif os.path.exists(os.path.join(os.path.dirname(__file__), "c.py")):\n'
                    '    raise RuntimeError("boom")\n')
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "a module the move breaks")
        before = snapshot(self.root)
        with self.assertRaises(movemod.MoveError) as ctx:
            movemod.run(self.root, "a", "c", apply=True, pkg="plugin/lib/tiny")
        self.assertIn("rolled back", str(ctx.exception))
        self.assertIn("broken", str(ctx.exception))
        self.assertEqual(snapshot(self.root), before, "a failed proof left changes behind")

    def test_an_import_probe_that_cannot_run_refuses_before_writing(self):
        before = snapshot(self.root)
        with mock.patch.object(movemod, "PROOF_TIMEOUT", 0.001):
            with self.assertRaises(movemod.MoveError) as ctx:
                movemod.run(self.root, "a", "c", apply=True, pkg="plugin/lib/tiny")
        self.assertIn("cannot run", str(ctx.exception))
        self.assertEqual(snapshot(self.root), before, "a probe that could not run still wrote")

    def test_an_interrupt_mid_apply_rolls_back_and_propagates(self):
        before = snapshot(self.root)
        ok = {"imported": 0, "failed": {}, "run": ""}
        with mock.patch.object(movemod, "_prove", side_effect=[ok, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                movemod.run(self.root, "a", "c", apply=True, pkg="plugin/lib/tiny")
        self.assertEqual(snapshot(self.root), before, "an interrupted move left changes behind")

    def test_bare_names_that_are_not_the_module_keep_their_names(self):
        with open(os.path.join(self.root, "plugin/lib/tiny/d.py"), "w", encoding="utf-8") as f:
            f.write("from tiny import a\n\n\ndef g(a=1):\n    return a\n\n\n"
                    "def h():\n    a = 3\n    return g(a=a) + a\n\n\ndef m():\n    return a.VALUE\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "a file with bare names a")
        rep = movemod.run(self.root, "a", "c", apply=True, pkg="plugin/lib/tiny")
        self.assertTrue(rep["applied"])
        d = read(self.root, "plugin/lib/tiny/d.py")
        self.assertIn("from tiny import c\n", d)
        self.assertIn("def g(a=1):\n    return a\n", d, "a parameter was renamed")
        self.assertIn("    a = 3\n    return g(a=a) + a\n", d, "a local or a kwarg was renamed")
        self.assertIn("return c.VALUE", d)

    def test_the_proof_names_the_affected_tests(self):
        rep = movemod.run(self.root, "a", "c", apply=True, pkg="plugin/lib/tiny")
        self.assertEqual(rep["proof"]["imported"], 3)  # __init__, b, c
        self.assertEqual(rep["proof"]["failed"], {})
        self.assertIn("test_c", rep["tests"])
        self.assertIn("python3 plugin/tests/run.py test_c", rep["proof"]["run"])


if __name__ == "__main__":
    unittest.main()
