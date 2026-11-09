"""T-3502: run.py skips a class already green at the same code, reruns 1 in 10 of those anyway and drops the cache
when one fails; the full gate, -k runs, nested runs and FOREMAN_NO_TEST_CACHE=1 never skip. T-3593: "the same code" is
what the class read when it passed: runcover.py records every file its processes open or run, every folder they list,
and the whole tree when a child escapes it; a change outside those skips the class, a class with no record runs."""
import contextlib
import hashlib
import inspect
import io
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import run  # noqa: E402
import runcover  # noqa: E402

TRUE = shutil.which("true") or "/bin/true"  # macOS keeps true in /usr/bin only

NESTED = ("SAMPO_SLOT", "FOREMAN_SLOT", "SAMPO_SLOTS", "FOREMAN_SLOTS", "SAMPO_NO_TEST_CACHE", "FOREMAN_NO_TEST_CACHE",
          "SAMPO_CACHE_FULL", "FOREMAN_CACHE_FULL")  # T-3638: a verify's opt-in in the caller's env must not skip a plain run


def outer(**extra):
    """This environment as a run.py started from a shell sees it: not inside a class's CPU slot, cache not switched off."""
    return dict({k: v for k, v in os.environ.items() if k not in NESTED}, **extra)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def scratch(d, files):
    """A git repo in d holding files ({path: text}), all committed."""
    for path, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(d, path)), exist_ok=True)
        with open(os.path.join(d, path), "w") as f:
            f.write(text)
    git(d, "init", "-q", "-b", "main")
    git(d, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    git(d, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    return d


TEST = "import unittest\nclass C(unittest.TestCase):\n    def test_x(self):\n        pass\n"
UNITS = '''"""A module."""
import os

X = 1


def f(a=1):
    """F's doc."""
    return a + 1


def g():
    return 2


class C:
    Y = 2

    def m(self):
        def inner():
            return 3
        return inner()


def gen():
    return [1]
'''
MON = hasattr(sys, "monitoring")  # T-3603: where runcover.py records the functions a process ran


def module(rel):
    """The key a module a process imported (ran, rather than read) is recorded under."""
    return rel + "::" if MON else rel


class Coverage(unittest.TestCase):
    """T-3593: what a class's recorded lines count as, and which changes touch them."""

    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        scratch(self.d, {
            ".gitignore": "state/\n",
            "plugin/tests/run.py": "# the runner\n",
            "plugin/tests/helpers.py": "X = 1\n",
            "plugin/tests/runcover.py": "# the recorder\n",
            "plugin/tests/test_a.py": TEST,
            "plugin/tests/fixtures/data.txt": "x\n",
            "plugin/lib/mod.py": "X = 1\n",
            "plugin/lib/units.py": UNITS,
            "docs/guide.md": "words\n",
            ".sampo/tasks/T-1.md": "state\n",
        })
        os.makedirs(os.path.join(self.d, "state"))
        open(os.path.join(self.d, "state", "x.json"), "w").close()
        self.here = os.path.join(self.d, "plugin", "tests")

    def tree(self):
        with mock.patch.object(run, "HERE", self.here):
            got = run.tree()
        self.assertEqual(got[0], self.d)
        return got

    def edit(self, path, text="changed\n"):
        os.makedirs(os.path.dirname(os.path.join(self.d, path)), exist_ok=True)
        with open(os.path.join(self.d, path), "a") as f:
            f.write(text)

    def test_a_file_whose_name_is_not_utf8_is_hashed_and_saved(self):  # T-3651: the 05:20 ship crashed in digest()
        bad = os.fsdecode(b"bad\xff.txt")  # Python holds the stray byte as the surrogate '\udcff'
        try:
            with open(os.path.join(self.d, bad), "w") as f:
                f.write("x\n")
        except OSError as e:  # APFS (macOS) refuses a name that isn't UTF-8 (EILSEQ): such a file can't exist there
            self.skipTest(f"this file system refuses the name: {e.strerror}")
        blobs = self.tree()[1]
        self.assertIn(bad, blobs)
        paths = sorted([bad, "./", "*", "?" + bad])
        d = run.digest(paths, run.values(self.d, blobs))
        self.assertEqual(d, run.digest(paths, run.values(self.d, blobs)))
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "c.json")
            run.save_cache(p, {"m.C": [d, 1, paths]}, 0)
            self.assertEqual(run.load_cache(p), (0, {"m.C": [[d, 1, paths]]}))

    def changes(self, paths, edit):
        """Whether edit() changes the digest of what a class that read paths depends on."""
        before = run.digest(sorted(paths), run.values(*self.tree()[:2]))
        edit()
        return before != run.digest(sorted(paths), run.values(*self.tree()[:2]))

    def moved(self, old, new, rel="plugin/lib/units.py", units=("", "f", "g", "C.m", "gen")):
        """The unit keys of rel ('' its module's own code) whose value an edit of old to new there moves."""
        keys = [f"{rel}::{u}" for u in units]
        before = run.values(*self.tree()[:2])
        before = {k: before(k) for k in keys}
        p = os.path.join(self.d, rel)
        with open(p) as f:
            text = f.read()
        self.assertIn(old, text)
        with open(p, "w") as f:
            f.write(text.replace(old, new, 1))
        after = run.values(*self.tree()[:2])
        return sorted(k[len(rel) + 2:] for k in keys if after(k) != before[k])

    def test_a_function_s_key_moves_with_its_own_body_alone(self):  # T-3603
        for old, new, want in (("return a + 1", "return a + 2", ["f"]),
                               ("return 3", "return 4", ["C.m"]),  # a nested function is its outer one's
                               ("    return 2\n", "    return 2  # two\n", []),  # a comment
                               ("X = 1\n", "\n\nX = 1\n", []),  # every line below moved
                               ("def f(a=1)", "def f(a=2)", [""]),  # a default runs at import: the module's
                               ('"""F\'s doc."""', '"""F\'s new doc."""', ["f"]),  # no code here reads __doc__
                               ("X = 1", "X = 2", [""]), ("Y = 2", "Y = 3", [""]),
                               ("def g():", "@staticmethod\ndef g():", [""]),  # a decorator
                               ("def gen():", "def h():\n    pass\n\n\ndef gen():", [""]),  # a new function
                               ("return [1]", "yield 1", ["", "gen"]),  # a call no longer runs its body
                               ("def g():", "def g2():", ["", "g"])):  # gone: the class that ran it reruns
            with self.subTest(new):
                git(self.d, "checkout", "-q", "--", ".")
                self.assertEqual(self.moved(old, new), want)
        annotate = ("", "__annotate__", "C.__annotate__", "f")  # no def of that name: the annotations' own code
        git(self.d, "checkout", "-q", "--", ".")
        self.assertEqual(self.moved("X = 1", "X: int = 1", units=annotate), ["", "C.__annotate__", "__annotate__"])
        self.assertEqual(self.moved("return a + 1", "return a + 2", units=annotate), ["f"])

    def test_a_file_that_doesn_t_parse_or_is_gone_moves_every_key(self):
        every = ["", "C.m", "f", "g", "gen"]
        self.assertEqual(self.moved("return 2", "return ("), every)
        self.assertEqual(sorted(self.moved("return (", "return 2")), every, "and back")
        before = run.values(*self.tree()[:2])("plugin/lib/units.py::f")
        os.remove(os.path.join(self.d, "plugin", "lib", "units.py"))
        self.assertNotEqual(run.values(*self.tree()[:2])("plugin/lib/units.py::f"), before)

    def test_a_function_it_ran_is_keyed_by_its_unit_and_its_module_a_file_it_read_whole(self):  # T-3603
        d, u = self.d, f"{self.d}/plugin/lib/units.py"
        text = "\n".join([f"m {u}", f"c {u} f", f"c {u} C.m", f"c {u} <lambda>", f"c {u} C.<genexpr>",
                          f"c {u} __annotate__", f"c {u} C.__annotate__",  # a module or class body's own code
                          f"m {d}/plugin/lib/mod.py", f"c {d}/plugin/lib/mod.py f", f"f {d}/plugin/lib/mod.py",
                          f"c {d}/plugin/lib/gone.py f", f"m {d}/.sampo/x.py", f"c {d}/.sampo/x.py f"])
        with mock.patch.object(run, "HERE", self.here):
            got = run.covered(text, d, self.tree()[1])
        self.assertEqual(got, {"plugin/lib/units.py::", "plugin/lib/units.py::f", "plugin/lib/units.py::C.m",
                               "plugin/lib/units.py::__annotate__", "plugin/lib/units.py::C.__annotate__",  # values()'s
                               "plugin/lib/mod.py", "plugin/lib/gone.py::", "plugin/lib/gone.py::f"})

    def test_recorded_lines_name_files_modules_folders_and_the_whole_tree(self):
        d, tag = self.d, sys.implementation.cache_tag
        text = "\n".join([f"f {d}/plugin/lib/mod.py", f"f {d}/plugin/lib/__pycache__/mod.{tag}.pyc",
                          f"f {d}/plugin/tests/fixtures/data.txt", f"d {d}/plugin/tests/fixtures", f"d {d}", "*",
                          "f /usr/lib/python3/os.py",  # outside the repo
                          f"f {d}/.git/HEAD", f"f {d}/state/x.json",  # on disk, but git doesn't see them
                          f"f {d}/.sampo/tasks/T-1.md", f"d {d}/.sampo/tasks",  # Sampo's task state: no test reads it
                          f"f {d}/plugin/lib/gone.py"])  # missing: creating it reruns
        with mock.patch.object(run, "HERE", self.here):
            got = run.covered(text, d, self.tree()[1])
        self.assertEqual(got, {"plugin/lib/mod.py", "plugin/tests/fixtures/data.txt", "plugin/tests/fixtures/", "./",
                               "*", "plugin/lib/gone.py"})

    def test_a_change_touches_only_the_classes_that_read_it(self):
        reads = ["plugin/lib/mod.py", "plugin/tests/test_a.py"]
        self.assertFalse(self.changes(reads, lambda: self.edit("docs/guide.md")))
        self.assertFalse(self.changes(reads, lambda: self.edit("plugin/lib/new.py")), "a new file nobody read")
        self.assertTrue(self.changes(reads, lambda: self.edit("plugin/lib/mod.py")))
        self.assertTrue(self.changes(reads, lambda: self.edit("plugin/tests/test_a.py", "# note\n")))
        self.assertTrue(self.changes(reads, lambda: os.remove(os.path.join(self.d, "plugin", "lib", "mod.py"))))

    def test_a_listed_folder_changes_with_its_names_and_a_missing_file_once_it_exists(self):
        self.assertFalse(self.changes(["plugin/tests/fixtures/"], lambda: self.edit("plugin/tests/fixtures/data.txt")))
        self.assertTrue(self.changes(["plugin/tests/fixtures/"], lambda: self.edit("plugin/tests/fixtures/new.txt")))
        self.assertTrue(self.changes(["./"], lambda: self.edit("top.md")))
        self.assertTrue(self.changes(["plugin/lib/gone.py"], lambda: self.edit("plugin/lib/gone.py")))

    def test_the_whole_tree_changes_with_any_file_but_sampo_s_task_state(self):
        self.assertTrue(self.changes(["*"], lambda: self.edit("docs/guide.md")))
        self.assertFalse(self.changes(["*", "./"], lambda: self.edit(".sampo/tasks/T-1.md")))

    def test_a_file_s_mode_counts_committed_or_not(self):  # review: test_docs checks os.access(hook, X_OK)
        hook = os.path.join(self.d, "plugin", "tests", "run.py")
        self.assertTrue(self.changes(["plugin/tests/run.py"], lambda: os.chmod(hook, 0o755)))
        git(self.d, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qam", "x")
        self.assertTrue(self.changes(["plugin/tests/run.py"], lambda: os.chmod(hook, 0o644)))
        self.edit("plugin/lib/new.py")
        new = os.path.join(self.d, "plugin", "lib", "new.py")
        self.assertTrue(self.changes(["plugin/lib/new.py"], lambda: os.chmod(new, 0o755)), "an untracked file's mode too")

    def test_git_s_head_and_tags_count_only_for_a_class_that_ran_git_on_the_repo(self):  # review
        commit = ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"]
        self.assertTrue(self.changes([run.GIT_STATE], lambda: git(self.d, *commit)))
        self.assertTrue(self.changes([run.GIT_STATE], lambda: git(self.d, "tag", "v1")))
        self.assertTrue(self.changes([run.GIT_STATE], lambda: git(self.d, "checkout", "-q", "-b", "side")))
        self.assertFalse(self.changes([run.GIT_STATE], lambda: git(self.d, "branch", "lane")),
                         "another lane's branch moving: every lane commit would rerun it")
        self.assertFalse(self.changes(["*", "plugin/lib/mod.py", "./", "plugin/"], lambda: git(self.d, *commit)),
                         "a whole-tree record keys on the files alone")
        with mock.patch.object(run, "HERE", self.here):
            got = run.covered("g\n*\n", self.d, self.tree()[1])
        self.assertEqual(got, {run.GIT_STATE, "*"})

    def test_what_a_stat_saw_counts(self):  # review: os.path.exists(skills/x/references/y.md) fires no audit event
        d, tag = self.d, sys.implementation.cache_tag  # noqa: F841
        text = "\n".join([f"s {d}/plugin/lib/mod.py", f"s {d}/plugin/tests/fixtures", f"s {d}/plugin/lib/gone.py",
                          f"s {d}/state/x.json"])
        with mock.patch.object(run, "HERE", self.here):
            got = run.covered(text, d, self.tree()[1])
        self.assertEqual(got, {"plugin/lib/mod.py", "?plugin/tests/fixtures", "?plugin/lib/gone.py", "?state/x.json"})
        self.assertTrue(self.changes(["?plugin/lib/gone.py"], lambda: self.edit("plugin/lib/gone.py")), "appears")
        self.assertTrue(self.changes(["?docs/guide.md"], lambda: os.remove(os.path.join(d, "docs", "guide.md"))), "goes")
        self.assertFalse(self.changes(["?plugin/tests/fixtures"], lambda: self.edit("plugin/tests/fixtures/data.txt")),
                         "a folder that still exists")
        self.assertTrue(self.changes(["?plugin/tests/fixtures"],
                                     lambda: shutil.rmtree(os.path.join(d, "plugin", "tests", "fixtures"))))

    def test_run_py_and_the_helpers_beside_it_are_every_class_s(self):
        with mock.patch.object(run, "HERE", self.here):
            self.assertEqual(run.shared(*self.tree()[:2]),
                             {"plugin/tests/run.py", "plugin/tests/helpers.py", "plugin/tests/runcover.py"})

    def test_blobs_are_git_hashes_of_the_files_on_disk_untracked_ones_too(self):
        self.edit("plugin/lib/mod.py")
        self.edit("plugin/lib/new.py", "Y = 2\n")
        _, blobs, _ = self.tree()
        for path in ("plugin/lib/mod.py", "plugin/lib/new.py", "docs/guide.md"):
            self.assertEqual(blobs[path], "100644 " + git(self.d, "hash-object", path).strip(), path)
        os.remove(os.path.join(self.d, "docs", "guide.md"))
        self.assertNotIn("docs/guide.md", self.tree()[1], "a deleted file isn't hashed")

    def test_every_worktree_of_a_repo_shares_one_cache(self):
        wt = os.path.join(os.path.realpath(tempfile.mkdtemp()), "lane")
        self.addCleanup(shutil.rmtree, os.path.dirname(wt), True)
        git(self.d, "worktree", "add", "-q", "--detach", wt)
        with mock.patch.object(run, "HERE", os.path.join(wt, "plugin", "tests")):
            lane = run.tree()
        self.assertEqual(lane[0], wt)
        self.assertEqual(lane[2], self.tree()[2])

    def commit(self, *args):
        git(self.d, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "x", *args)

    def test_the_branch_name_key_moves_with_the_name_alone(self):  # T-3691: git rev-parse --abbrev-ref HEAD
        key = "\0ref:HEAD"
        self.assertFalse(self.changes([key], lambda: self.commit("--allow-empty")), "a commit on the same branch")
        self.assertFalse(self.changes([key], lambda: self.edit("plugin/lib/mod.py")))
        self.assertFalse(self.changes([key], lambda: git(self.d, "branch", "lane")), "another lane's branch")
        self.assertTrue(self.changes([key], lambda: git(self.d, "checkout", "-q", "-b", "side")))
        self.assertTrue(self.changes([key], lambda: git(self.d, "tag", "side")), "now ambiguous: --abbrev-ref says heads/side")
        self.assertTrue(self.changes([key], lambda: git(self.d, "update-ref", "refs/remotes/side/HEAD", "HEAD")),
                        "a remote ref the short name could mean too")
        self.assertTrue(self.changes([key], lambda: git(self.d, "checkout", "-q", "--detach")))
        self.assertFalse(self.changes([key], lambda: self.commit("--allow-empty")), "detached: still HEAD")

    def test_a_remote_s_default_branch_key_moves_with_its_target(self):  # T-3691: symbolic-ref --short origin/HEAD
        key, sha = "\0ref:refs/remotes/origin/HEAD", git(self.d, "rev-parse", "HEAD").strip()
        git(self.d, "update-ref", "refs/remotes/origin/main", sha)
        git(self.d, "update-ref", "refs/remotes/origin/dev", sha)
        self.assertTrue(self.changes([key], lambda: git(self.d, "symbolic-ref", "refs/remotes/origin/HEAD",
                                                        "refs/remotes/origin/main")), "appears")
        self.commit("--allow-empty")
        self.assertFalse(self.changes([key], lambda: git(self.d, "update-ref", "refs/remotes/origin/main", "HEAD")),
                         "its branch moving")
        self.assertTrue(self.changes([key], lambda: git(self.d, "symbolic-ref", "refs/remotes/origin/HEAD",
                                                        "refs/remotes/origin/dev")))
        self.assertTrue(self.changes([key], lambda: git(self.d, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")), "goes")

    def test_head_s_commit_and_what_a_path_is_in_it(self):  # T-3691: git rev-parse HEAD, git show HEAD:path
        self.assertTrue(self.changes(["\0HEAD"], lambda: self.commit("--allow-empty")))
        self.assertFalse(self.changes(["\0HEAD"], lambda: git(self.d, "tag", "v1")))
        key = "\0HEAD:plugin/lib/mod.py"
        self.assertFalse(self.changes([key], lambda: self.edit("plugin/lib/mod.py")), "not committed: HEAD's is the same")
        self.edit("docs/guide.md")
        self.assertFalse(self.changes([key], lambda: self.commit("docs/guide.md")), "another file's commit")
        self.assertTrue(self.changes([key], lambda: self.commit("plugin/lib/mod.py")))
        self.edit("plugin/lib/units.py")
        self.assertTrue(self.changes(["\0HEAD:plugin/lib"], lambda: self.commit("plugin/lib/units.py")), "a folder's tree")
        self.edit("plugin/lib/new.py")
        git(self.d, "add", "plugin/lib/new.py")
        self.assertTrue(self.changes(["\0HEAD:plugin/lib/new.py"], self.commit), "appears")
        self.assertTrue(self.changes([key], lambda: (git(self.d, "rm", "-q", "plugin/lib/mod.py"), self.commit())), "goes")
        bad = dict(self.tree()[1], **{"\0HEAD": "0" * 40})
        self.assertNotEqual(run.values(self.d, bad)(key), run.values(self.d, bad)(key), "a commit git can't list: unknown")

    def test_git_s_keys_are_not_files(self):  # T-3691
        def move():
            git(self.d, "checkout", "-q", "-b", "other")
            self.commit("--allow-empty")
            git(self.d, "update-ref", "refs/remotes/origin/main", "HEAD")
            git(self.d, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.assertFalse(self.changes(["*", "./", "plugin/", "?docs/x"], move))

    def test_the_git_state_key_is_head_and_the_tags_as_before(self):  # T-3691: a record saved before still matches
        git(self.d, "tag", "v1")
        git(self.d, "update-ref", "refs/remotes/origin/main", "HEAD")
        head = git(self.d, "rev-parse", "HEAD", "--symbolic-full-name", "HEAD")
        tags = git(self.d, "for-each-ref", "--format=%(objectname) %(refname)", "refs/tags")
        self.assertEqual(self.tree()[1][run.GIT_STATE], hashlib.sha1((head + tags).encode()).hexdigest())

    def test_git_s_bounded_reads_in_this_repo_are_their_own_keys(self):  # T-3691
        d = self.d
        text = "\n".join([f"r {d}\0HEAD", f"r {d}\0refs/remotes/origin/HEAD", f"h {d}\0plugin/lib/mod.py", f"h {d}\0"])
        with mock.patch.object(run, "HERE", self.here):
            blobs = self.tree()[1]
            self.assertEqual(run.covered(text, d, blobs),
                             {"\0ref:HEAD", "\0ref:refs/remotes/origin/HEAD", "\0HEAD:plugin/lib/mod.py", "\0HEAD"})
            for ln in (f"r {d}/docs\0HEAD", "h /elsewhere\0x", f"r {d}x\0HEAD"):
                self.assertEqual(run.covered(ln, d, blobs), {"*", run.GIT_STATE}, "another repo's: " + ln)

    def test_outside_git_there_is_no_tree(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(run, "HERE", d):
            self.assertIsNone(run.tree())


MOD_C = ("def f():\n    def inner():\n        return 1\n    return inner()\n\n\ndef g():\n    return 2\n\n\n"
         "class K:\n    def m(self):\n        return (lambda: 3)()\n")
HELPERS = """import os, sys


def devtools(browser, url):  # helpers.devtools' part the recorder sees: a browser started, then told its page
    pid = os.posix_spawn(browser, [browser, '--headless', '--remote-debugging-pipe', 'about:blank'], dict(os.environ))
    os.waitpid(pid, 0)
    sys.audit('sampo.page', url)
"""


class Recorder(unittest.TestCase):
    """T-3593: runcover.py in cover_python's venv: what a process and its children record."""

    @classmethod
    def setUpClass(cls):
        cls.d = os.path.realpath(tempfile.mkdtemp())
        scratch(cls.d, {"lib/mod_a.py": "A = 1\n", "lib/mod_b.py": "B = 1\n", "data.txt": "x\n", "folder/f.txt": "y\n",
                        "lib/mod_c.py": MOD_C, "lib/helpers.py": HELPERS,
                        "bin/tool": "#!/bin/sh\ntrue\n", "bin/sleep": "#!/bin/sh\ntrue\n",
                        "bin/pytool": "#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, '.')\nimport mod_b\n"})
        for name in ("tool", "pytool", "sleep"):
            os.chmod(os.path.join(cls.d, "bin", name), 0o755)
        cls.cache = os.path.realpath(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cls.cache}):
            cls.py = run.cover_python()
        cls.blobs = dict.fromkeys(git(cls.d, "ls-files").split(), "blob")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, True)
        shutil.rmtree(cls.cache, True)

    def record(self, code, raw=False, held=None):
        """What a script in the repo running `code` (and its children) recorded, as covered() reads it (raw: the
        file's text; held: what the file holds before)."""
        out = os.path.join(tempfile.mkdtemp(), "cover")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), True)
        if held is not None:
            with open(out, "w") as f:
                f.write(held)
        with open(os.path.join(self.d, "probe.py"), "w") as f:
            f.write("import os, subprocess, sys, tempfile\nsys.path.insert(0, 'lib')\n" + code)
        subprocess.run([self.py, "probe.py"], cwd=self.d, check=True, timeout=120, capture_output=True,
                       env=dict(os.environ, RUNPY_COVER=out, RUNPY_COVER_ROOTS=os.pathsep.join(run._roots(self.d))))
        with open(out) as f:
            return f.read() if raw else run.covered(f.read(), self.d, self.blobs)

    def test_the_venv_python_is_this_python_with_the_recorder_outside_tmp_and_made_once(self):
        self.assertIsNotNone(self.py)
        r = subprocess.run([self.py, "-c", "import sys, runcover; print(sys.version, runcover.PY)"], capture_output=True,
                           text=True, check=True)
        self.assertEqual(r.stdout.split(), [*sys.version.split(), self.py])
        self.assertTrue(self.py.startswith(os.path.join(self.cache, "sampo", "run-py", "")), "a job sandbox hides /tmp")
        stale = os.path.join(self.cache, "sampo", "run-py", "0ld")
        os.makedirs(stale)
        os.utime(stale, (1, 1))
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache}):
            self.assertEqual(run.cover_python(), self.py)
        self.assertEqual(os.listdir(os.path.dirname(stale)), [os.path.basename(os.path.dirname(os.path.dirname(self.py)))],
                         "one venv, and one no run used for weeks is gone")

    def test_a_cleared_environment_or_closed_descriptors_lose_nothing_and_write_nowhere_else(self):
        got = self.record("os.environ.clear()\nimport mod_a\n")
        self.assertIn(module("lib/mod_a.py"), got)
        mine = os.path.join(tempfile.mkdtemp(), "mine")
        self.addCleanup(shutil.rmtree, os.path.dirname(mine), True)
        got = self.record(f"open('data.txt').read()\nos.closerange(3, 4096)\nfd = os.open({mine!r}, os.O_CREAT | os.O_WRONLY)\n"
                          "import mod_b\nos.close(fd)\n")
        self.assertIn(module("lib/mod_b.py"), got)
        with open(mine) as f:
            self.assertEqual(f.read(), "", "a reused descriptor number is never written to")

    def test_modules_files_folders_and_a_child_python_s_reads(self):
        got = self.record("import mod_a\nopen('data.txt').read()\nos.listdir('folder')\n"
                          "subprocess.run([sys.executable, '-c', 'import mod_b'], cwd='lib', check=True)\n")
        self.assertLessEqual({module("lib/mod_a.py"), "data.txt", "folder/", module("lib/mod_b.py")}, got)
        self.assertNotIn("*", got)

    @unittest.skipUnless(MON, "sys.monitoring needs Python 3.12")
    def test_a_module_s_functions_count_once_run_its_source_only_once_read(self):  # T-3603
        for _ in range(2):  # the second import reads the .pyc the first wrote
            got = self.record("import mod_c\nmod_c.f()\nmod_c.K().m()\n")
            self.assertEqual({p for p in got if p.startswith("lib/mod_c.py")},
                             {"lib/mod_c.py::", "lib/mod_c.py::f", "lib/mod_c.py::K.m"})
        got = self.record("import mod_c\nopen('lib/mod_c.py').read()\n")  # read as data (inspect.getsource, a test)
        self.assertEqual({p for p in got if p.startswith("lib/mod_c.py")}, {"lib/mod_c.py"})

    @unittest.skipUnless(MON, "sys.monitoring needs Python 3.12")
    def test_a_fork_records_what_its_parent_ran_before_it_recorded(self):  # T-3603: a fork server's preloaded calls
        child = ("import os, sys\nsys.path.insert(0, 'lib')\nimport mod_c\nmod_c.f()\nif os.fork() == 0:\n"
                 "    os.environ['RUNPY_COVER'] = os.environ['LATER']\n    open('data.txt').read()\n    os._exit(0)\n"
                 "os.wait()\n")
        got = self.record("e = {k: v for k, v in os.environ.items() if k != 'RUNPY_COVER'}\n"
                          f"subprocess.run([sys.executable, '-c', {child!r}], env=dict(e, LATER=os.environ['RUNPY_COVER']),"
                          " check=True)\n")
        self.assertLessEqual({"data.txt", "lib/mod_c.py::", "lib/mod_c.py::f"}, got)
        self.assertNotIn("lib/mod_c.py::g", got)

    @unittest.skipUnless(MON, "sys.monitoring needs Python 3.12")
    def test_a_line_the_file_holds_is_not_written_again_one_it_lacks_always_is(self):  # T-3691
        """A fork server's calls each wrote all it had run (84% of what classes recorded); each process now skips
        what the file already holds, and still writes what its parent ran since, and its own reads."""
        child = ("import os, sys\nsys.path.insert(0, 'lib')\nimport mod_c\nmod_c.f()\n"
                 "def call(code):\n    if os.fork() == 0:\n        os.environ['RUNPY_COVER'] = os.environ['LATER']\n"
                 "        exec(code)\n        os._exit(0)\n    os.wait()\n"
                 "call('open(\"data.txt\").read()')\ncall('open(\"data.txt\").read()')\nmod_c.K().m()\n"
                 "call('os.listdir(\"folder\")')\n")
        text = self.record("e = {k: v for k, v in os.environ.items() if k != 'RUNPY_COVER'}\n"
                           f"subprocess.run([sys.executable, '-c', {child!r}], env=dict(e, LATER=os.environ['RUNPY_COVER']),"
                           " check=True)\nsubprocess.run([sys.executable, '-c', 'import mod_a'], cwd='lib', check=True)\n"
                           "subprocess.run([sys.executable, '-c', 'import mod_a'], cwd='lib', check=True)\n", raw=True)
        lines = text.split("\n")
        mod_c = os.path.join(self.d, "lib", "mod_c.py")
        for ln in (f"c {mod_c} f", f"m {mod_c}", f"f {os.path.join(self.d, 'data.txt')}",
                   f"m {os.path.join(self.d, 'lib', 'mod_a.py')}"):
            self.assertEqual(lines.count(ln), 1, ln)
        self.assertIn(f"c {mod_c} K.m", lines, "what the parent ran after its first call reaches its next one")
        self.assertIn(f"d {os.path.join(self.d, 'folder')}", lines)

    def test_a_line_half_written_is_not_one_the_file_holds(self):  # T-3691: a sibling's write read mid-way
        data = os.path.join(self.d, "data.txt")
        text = self.record("open('data.txt').read()\n", raw=True, held=f"f {data}.bak\nf {data}")
        self.assertIn(f"f {data}", text.split("\n")[1:])

    def test_a_file_too_big_to_read_back_is_written_to_as_if_empty(self):  # T-3691: HELD_MAX bounds _held's read
        data = os.path.join(self.d, "data.txt")
        pad = "x" * (runcover.HELD_MAX + 1)
        text = self.record("open('data.txt').read()\n", raw=True, held=f"f {data}\n{pad}\n")
        self.assertEqual(text.split("\n").count(f"f {data}"), 2, "unread past the bound, the line is written again")

    @unittest.skipUnless(MON, "sys.monitoring needs Python 3.12")
    def test_the_monitoring_tool_id_is_not_the_flight_recorder_s(self):  # T-3603: debug.py's _RECORDER holds 4
        r = subprocess.run([self.py, "-c", "import sys; print(sys.monitoring.get_tool(3), sys.monitoring.get_tool(4))"],
                           capture_output=True, text=True, check=True, env=dict(os.environ, RUNPY_COVER=os.devnull))
        self.assertEqual(r.stdout.split(), ["runcover", "None"])

    def test_what_it_only_stats_exists_or_accesses(self):
        got = self.record("os.path.exists('missing.txt')\nos.path.isdir('folder')\nos.access('data.txt', os.X_OK)\n"
                          "os.stat('lib/mod_a.py')\nos.lstat('lib/mod_b.py')\n")
        self.assertLessEqual({"?missing.txt", "?folder", "data.txt", "lib/mod_a.py", "lib/mod_b.py"}, got)
        r = subprocess.run([self.py, "-c", "import os, shutil; print(shutil._use_fd_functions, os.stat in os.supports_fd,"
                            " os.stat in os.supports_dir_fd, os.lstat in os.supports_dir_fd)"],
                           capture_output=True, text=True, check=True, env=dict(os.environ, RUNPY_COVER=os.devnull))
        self.assertEqual(r.stdout.split(), ["True"] * 4, "rmtree keeps its fd-based, symlink-safe walk")

    def test_it_starts_once_though_site_runs_its_pth_twice(self):  # T-3691: a venv seeing system packages does
        r = subprocess.run([self.py, "-c", "import os, posix, runcover; f = os.lstat; runcover.setup(runcover.PY)\n"
                            "print(os.lstat.__wrapped__ is posix.lstat, os.lstat is f)"],
                           capture_output=True, text=True, check=True, env=dict(os.environ, RUNPY_COVER=os.devnull))
        self.assertEqual(r.stdout.split(), ["True", "True"], "every stat and audit event was judged twice")

    def test_a_path_asked_again_is_judged_once(self):  # T-3691: the guard stats the same paths thousands of times
        out = os.path.join(tempfile.mkdtemp(), "cover")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), True)
        code = ("import os, runcover\nask = lambda: (os.path.exists('data.txt'), os.path.isdir('/nowhere'), os.stat('lib'))\n"
                "ask()\nn = [0]\nf = runcover._inside\n"
                "runcover._inside = lambda *a: (n.__setitem__(0, n[0] + 1), f(*a))[1]\n"
                "for _ in range(500):\n    ask()\nprint(n[0])\nos.path.isdir('folder')\nprint(n[0])\n")
        r = subprocess.run([self.py, "-c", code], cwd=self.d, capture_output=True, text=True, check=True,
                           env=dict(os.environ, RUNPY_COVER=out, RUNPY_COVER_ROOTS=os.pathsep.join(run._roots(self.d))))
        self.assertEqual(r.stdout.split(), ["0", "1"], "judged on the first ask only")

    def test_a_path_asked_again_is_recorded_again_where_the_answer_or_the_file_changed(self):  # T-3691: the memo
        other, mine = (os.path.join(tempfile.mkdtemp(), name) for name in ("other", "cover"))
        for p in (other, mine):
            self.addCleanup(shutil.rmtree, os.path.dirname(p), True)
        outside = os.path.dirname(other)
        text = self.record(
            f"os.path.exists('data.txt')\nos.chdir('lib')\nos.path.exists('data.txt')\nos.chdir('..')\n"  # another folder
            f"os.path.isdir({outside!r})\nos.environ['RUNPY_COVER_ROOTS'] += os.pathsep + {outside!r} + os.sep\n"
            f"os.path.isdir({outside!r})\n"  # a repo named later
            f"os.environ['RUNPY_COVER'] = {other!r}\nos.path.exists('data.txt')\n"  # another record file
            f"os.environ = dict(os.environ, RUNPY_COVER={mine!r})\nos.path.exists('data.txt')\n",  # a plain dict
            raw=True).split("\n")
        self.assertIn(f"s {os.path.join(self.d, 'data.txt')}", text)
        self.assertIn(f"s {os.path.join(self.d, 'lib', 'data.txt')}", text)
        self.assertIn(f"s {outside}", text)
        for p in (other, mine):
            with open(p) as f:
                self.assertIn(f"s {os.path.join(self.d, 'data.txt')}", f.read().split("\n"), p)

    def test_a_forked_child_s_reads_and_one_that_execs_this_python(self):
        got = self.record("pid = os.fork()\nif pid == 0:\n    import mod_b\n    os._exit(0)\nos.waitpid(pid, 0)\n")
        self.assertIn(module("lib/mod_b.py"), got)
        got = self.record("pid = os.fork()\nif pid == 0:\n    os.chdir('lib')\n"
                          "    os.execv(sys.executable, [sys.executable, '-c', 'import mod_b'])\nos.waitpid(pid, 0)\n")
        self.assertIn(module("lib/mod_b.py"), got)
        self.assertNotIn("*", got)

    def test_a_child_it_cant_follow_is_the_whole_tree(self):
        for code in ("subprocess.run([os.path.realpath(sys.executable), '-c', 'pass'])",  # a python without the hook
                     "subprocess.run([sys.executable, '-S', '-c', 'pass'])",  # no site: no .pth
                     "subprocess.run([sys.executable, '-X', 'utf8', '-Sc', 'pass'])",
                     "subprocess.run([sys.executable, '-c', 'pass'], env={'PATH': os.environ['PATH']})",  # no file
                     "subprocess.run(['git', '-C', 'lib', 'log'], capture_output=True)",
                     "subprocess.run(['cat', 'data.txt'], capture_output=True)",  # a program given a repo path
                     "subprocess.run('cat data.txt', shell=True, capture_output=True)",
                     "subprocess.run('python3 -c pass', shell=True)",  # a python the shell finds
                     "subprocess.run(['tool'], env=dict(os.environ, PATH='bin' + os.pathsep + os.environ['PATH']))",
                     "subprocess.run(['ls'], capture_output=True)",  # works in the repo: reads it by its folder
                     "subprocess.run(['env', 'python3', '-c', 'pass'], cwd=tempfile.mkdtemp())",  # another python
                     "subprocess.run([sys.executable, '-c', 'pass'], env=dict(os.environ, RUNPY_COVER=os.devnull))",
                     "os.system('true')"):
            with self.subTest(code):
                self.assertIn("*", self.record(code + "\n"))

    def test_a_python_found_on_path_a_python_script_and_a_wrapper_of_this_python_are_followed(self):
        venv = "env=dict(os.environ, PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ['PATH'])"
        for code in (f"subprocess.run(['python3', '-c', 'import mod_b'], cwd='lib', check=True, {venv})",
                     "subprocess.run('python3 -c \"import mod_b\"', shell=True, cwd=tempfile.mkdtemp(), check=True, "
                     "env=dict(os.environ, PYTHONPATH=os.path.abspath('lib'),"  # a gate's command line, in a scratch project
                     " PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ['PATH']))",
                     f"subprocess.run([os.path.abspath('bin/pytool')], cwd='lib', check=True, {venv})",  # fm, the hook
                     "subprocess.run(['env', sys.executable, '-c', 'import mod_b'], cwd='lib', check=True)",
                     "t = tempfile.mkdtemp()\nos.symlink(sys.executable, os.path.join(t, 'claude'))\n"  # a stand-in
                     "subprocess.run([os.path.join(t, 'claude'), '-c', 'import subprocess, sys; subprocess.run(sys.argv[1:])',"
                     " sys.executable, '-c', 'import mod_b'], cwd='lib', check=True)"):
            with self.subTest(code):
                got = self.record(code + "\n")
                self.assertIn(module("lib/mod_b.py"), got)
                self.assertNotIn("*", got)
        self.assertIn("*", self.record("subprocess.run([os.path.abspath('bin/pytool')], cwd='lib')\n"),
                      "its #! python, found on this PATH, isn't this venv's")
        self.assertIn("*", self.record("subprocess.run(['bwrap', '--ro-bind', '/', '/', sys.executable, '-c', 'pass'])\n"),
                      "a sandbox hides the record file")

    def test_git_on_the_repo_also_keys_on_head_and_tags_other_blind_children_on_files_alone(self):
        for code in ("subprocess.run(['git', 'status'], capture_output=True)",
                     "subprocess.run(['git', '-C', 'lib', 'log'], capture_output=True)"):
            with self.subTest(code):
                self.assertLessEqual({"*", run.GIT_STATE}, self.record(code + "\n"))
        got = self.record("subprocess.run(['cat', 'data.txt'], capture_output=True)\n")
        self.assertIn("*", got)
        self.assertNotIn(run.GIT_STATE, got)

    def test_children_that_read_nothing_of_the_repo_stay_followed(self):
        got = self.record("t = tempfile.mkdtemp()\nsubprocess.run(['git', 'init', '-q', t], check=True)\n"
                          "subprocess.run(['git', '-C', t, 'status'], capture_output=True)\n"
                          "subprocess.run(['true'], check=True, cwd=t)\nsubprocess.run([sys.executable, '-I', '-c', 'pass'])\n"
                          "subprocess.run('echo hi', shell=True, capture_output=True, cwd=t)\n")
        self.assertNotIn("*", got)

    def test_a_program_that_reads_no_files_stays_followed_in_the_repo_but_not_given_a_repo_path(self):
        """T-3623: sleep, nvidia-smi and a one-shot browser run from a test's working folder (the repo) without reading it."""
        for name in sorted(runcover.QUIET):
            with self.subTest(name):
                t = f"t = os.path.join(tempfile.mkdtemp(), {name!r})\nos.symlink({TRUE!r}, t)\n"
                self.assertNotIn("*", self.record(t + "subprocess.run([t], check=True)\n"))
        self.assertIn("*", self.record("subprocess.run(['./bin/sleep', '0'])\n"), "a program of the repo's own")
        b = f"t = os.path.join(tempfile.mkdtemp(), 'chromium')\nos.symlink({TRUE!r}, t)\n"
        shot = "subprocess.run([t, '--headless', '--dump-dom', %s], check=True)\n"
        self.assertNotIn("*", self.record(b + shot % "'http://127.0.0.1:1/'"))
        for arg in ("'data.txt'", "'file://' + os.path.abspath('data.txt')", "'file://localhost' + os.path.abspath('data.txt')",
                    "'file://' + os.path.abspath('data.txt') + '?q=1#x'", "'--app=file://' + os.path.abspath('data.txt')",
                    "'--user-data-dir=' + os.path.abspath('folder')", "'file://' + os.path.abspath('d%61ta.txt')"):
            with self.subTest(arg):
                self.assertIn("*", self.record(b + shot % ("'http://127.0.0.1:1/', " + arg)))
        for args in ("'--headless', '--remote-debugging-pipe', 'about:blank'", "'--headless', 'http://127.0.0.1:1/'"):
            with self.subTest(args):  # pages loaded later by a devtools no hook hears (not helpers'), or none to judge
                self.assertIn("*", self.record(b + f"subprocess.run([t, {args}], check=True)\n"))

    def assertBounded(self, code, keys=()):
        got = self.record(code + "\n")
        self.assertNotIn("*", got)
        self.assertEqual({p for p in got if p[:1] == "\0"}, set(keys))

    def test_git_asked_where_the_repo_is_its_branch_or_a_file_at_head_keys_on_that_alone(self):  # T-3691
        for args, keys in (("'-c', 'core.quotePath=false', '-C', os.getcwd(), 'rev-parse', '--path-format=absolute',"
                            " '--git-common-dir'", ()),
                           ("'rev-parse', '--show-toplevel'", ()),
                           ("'--no-pager', 'rev-parse', '-q', '--is-inside-work-tree', '--show-prefix'", ()),
                           ("'-C', 'lib', 'remote', 'get-url', 'origin'", ()),  # .git/config: '*' never held it
                           ("'-C', '.', 'rev-parse', '--abbrev-ref', 'HEAD'", ("\0ref:HEAD",)),
                           ("'rev-parse', '--symbolic-full-name', 'HEAD'", ("\0ref:HEAD",)),
                           ("'symbolic-ref', '--short', 'refs/remotes/origin/HEAD'", ("\0ref:refs/remotes/origin/HEAD",)),
                           ("'symbolic-ref', '-q', 'HEAD'", ("\0ref:HEAD",)),
                           ("'show', 'HEAD:lib/mod_a.py'", ("\0HEAD:lib/mod_a.py",)),
                           ("'-C', 'lib', 'show', 'HEAD:lib/mod_a.py'", ("\0HEAD:lib/mod_a.py",)),
                           ("'rev-parse', '--verify', 'HEAD'", ("\0HEAD",)),
                           ("'rev-parse', 'HEAD'", ("\0HEAD",))):
            with self.subTest(args):
                self.assertBounded(f"subprocess.run(['git', {args}], capture_output=True)", keys)

    def test_git_reading_more_or_another_way_is_still_the_whole_tree(self):  # T-3691: never lose a needed rerun
        for args in ("'status'", "'-C', 'lib', 'log'", "'ls-files'", "'--git-dir=.git', 'rev-parse', '--abbrev-ref', 'HEAD'",
                     "'--work-tree', '.', 'rev-parse', 'HEAD'", "'-c', 'core.abbrev=12', 'rev-parse', '--abbrev-ref', 'HEAD'",
                     "'--namespace', 'x', 'rev-parse', '--abbrev-ref', 'HEAD'", "'--paginate', 'show', 'HEAD:data.txt'",
                     "'show', 'HEAD:../x'", "'show', 'HEAD~1:lib/mod_a.py'", "'show', 'HEAD:./lib/mod_a.py'",
                     "'show', 'HEAD:lib//mod_a.py'", "'show', 'HEAD:lib/'", "'show', '--stat', 'HEAD:lib/mod_a.py'",
                     "'show', 'HEAD:lib/mod_a.py', 'HEAD:data.txt'", "'show', 'HEAD'", "'show', 'main:data.txt'",
                     "'rev-parse', '--short', 'HEAD'", "'rev-parse', 'HEAD~1'", "'rev-parse', '--abbrev-ref', 'main'",
                     "'rev-parse', '--abbrev-ref', 'HEAD', 'HEAD'", "'-C', '.git', 'rev-parse', '--abbrev-ref', 'HEAD'",
                     "'symbolic-ref', '-d', 'refs/remotes/origin/HEAD'", "'symbolic-ref', 'refs/remotes/a/b/HEAD'",
                     "'symbolic-ref', 'HEAD', 'refs/heads/main'", "'remote', '-v'", "'remote', 'get-url', '--all', 'origin'"):
            with self.subTest(args):
                self.assertLessEqual({"*", run.GIT_STATE},
                                     self.record(f"subprocess.run(['git', {args}], capture_output=True)\n"))
        for code in ("subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True,"
                     " env=dict(os.environ, GIT_DIR='.git'))",
                     "subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True,"
                     " env=dict(os.environ, GIT_CONFIG_PARAMETERS=\"'core.abbrev'='12'\"))",
                     "t = os.path.join('lib', 'inner')\nsubprocess.run(['git', 'init', '-q', t], check=True)\n"  # its own repo
                     "subprocess.run(['git', '-C', t, 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True)",
                     "t = tempfile.mkdtemp()\nsubprocess.run(['git', 'init', '-q', t], check=True)\nos.symlink(t, 'lnk')\n"
                     "subprocess.run(['git', '-C', 'lnk', 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True)",
                     "subprocess.run(['git', '-C', 'nowhere', 'rev-parse', 'HEAD'], capture_output=True)"):
            with self.subTest(code):
                self.assertLessEqual({"*", run.GIT_STATE}, self.record(code + "\n"))

    def test_a_browser_a_helper_drives_counts_by_the_pages_it_is_sent(self):  # T-3691: helpers.headless_chrome's probe
        b = f"t = os.path.join(tempfile.mkdtemp(), 'chromium')\nos.symlink({TRUE!r}, t)\nimport helpers\n"
        for url in ("'about:blank'", "'http://127.0.0.1:1/'", "'HTTPS://example.test/'", "'data:text/html,hi'"):
            with self.subTest(url):
                self.assertNotIn("*", self.record(b + f"helpers.devtools(t, {url})\n"))
        for url in ("'file://' + os.path.abspath('data.txt')", "'data.txt'", "'FILE:///x'", "'chrome://version'",
                    "'file://' + tempfile.mkdtemp() + '/x.html'"):  # a page outside the repo loads the repo's files
            with self.subTest(url):
                self.assertIn("*", self.record(b + f"helpers.devtools(t, {url})\n"))
        self.assertIn("*", self.record(b + "subprocess.run([t, '--headless', '--remote-debugging-pipe', 'about:blank'])\n"),
                      "a browser no helper told the recorder about")

    def test_the_browser_drivers_tell_each_page_they_load(self):  # T-3691: what lets their browser be followed
        import helpers
        from sampo import shot
        for name, fn in (("helpers.py", helpers.devtools), ("shot.py", shot.Browser.__init__)):
            self.assertIn((name, fn.__qualname__), runcover.DRIVERS)
            self.assertIn("os.posix_spawn(", inspect.getsource(fn), "the frame the recorder knows the browser by")
        self.assertIn('sys.audit("sampo.page", params["url"])', inspect.getsource(helpers.devtools))
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        with mock.patch.object(sys, "audit") as audit:
            shot.Browser.send(types.SimpleNamespace(n=0, feed=w), "Page.navigate", "s", url="file:///x.html")
            shot.Browser.send(types.SimpleNamespace(n=0, feed=w), "Page.enable", "s")
        audit.assert_called_once_with("sampo.page", "file:///x.html")

    def test_a_quiet_program_s_name_on_a_script_is_judged_as_any_program(self):  # T-3691: a stub reads what it likes
        self.assertIn("*", self.record("t = os.path.join(tempfile.mkdtemp(), 'cat')\nwith open(t, 'w') as f:\n"
                                       "    f.write('#!/bin/sh\\nexec /bin/cat data.txt\\n')\nos.chmod(t, 0o755)\n"
                                       "subprocess.run([t], check=True, capture_output=True)\n"))


def fake_classes(cids, tests=1, matched=None):
    """run.classes for main(): each class has `tests` tests, of which `matched` (default all) match the names."""
    def classes(patterns, k, hit, names, ids, parts, whole):
        for cid in cids:
            for i in range(tests):
                ids.append(f"{cid}.test_{i}")
                if i < (tests if matched is None else matched):
                    parts.setdefault(cid, []).append(f"{cid}.test_{i}")
        hit.update(names)
        return list(cids)
    return classes


class Main(unittest.TestCase):
    """main() with the classes, the tree and runs faked: what it runs, skips, records and says. Class X reads x.py."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        env = mock.patch.dict(os.environ, outer(XDG_CACHE_HOME=self.d, FM_TEST_TIMES=os.path.join(self.d, "t.json")),
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        cover = mock.patch.object(run, "cover_python", return_value=None)  # no venv; sys.executable stays
        cover.start()
        self.addCleanup(cover.stop)
        self.blobs, self.git_dir = {"a.py": "ka", "b.py": "kb", "c.py": "kc", "other.py": "ko"}, "/repo"
        self.fail_ids, self.late, self.skips, self.during, self.blind = set(), set(), set(), {}, set()

    def main(self, *argv, cids=("test_m.A", "test_m.B"), tests=1, matched=None, sample=0.5):
        ran, err = [], io.StringIO()

        def run_one(c, *a):  # a class job runs its matched tests, a split class's job one test
            ran.append(c)
            n = (tests if matched is None else matched) if c.count(".") == 1 else 1
            if c.split(".")[1] not in self.blind:
                run.COVERED[c] = f"f /repo/{c.split('.')[1].lower()}.py\n"
            self.blobs.update(self.during.pop(c, {}))  # a file edited while the class runs
            if c in self.late:
                return c, 1, run.NOT_STARTED, 0, 0.0
            if c in self.skips:
                return c, 0, f"\nRan {n} tests in 0.0s\n\nOK (skipped=1)\n", n, 0.0
            return c, int(c in self.fail_ids), "boom" if c in self.fail_ids else "", n, 0.0
        rnd = sample if callable(sample) else (lambda: sample)
        with mock.patch.object(run, "classes", fake_classes(list(cids), tests, matched)), \
                mock.patch.object(run, "run_one", run_one), \
                mock.patch.object(run, "tree", lambda: ("/repo", dict(self.blobs), self.git_dir)), \
                mock.patch.object(run.random, "random", rnd), \
                mock.patch.object(sys, "argv", ["run.py", *argv]), contextlib.redirect_stderr(err):
            code = run.main()
        return code, ran, err.getvalue()

    def cache(self):
        p = run.cache_path(self.git_dir)
        return run.load_cache(p)[1] if os.path.exists(p) else None

    def test_a_named_rerun_skips_the_classes_green_at_the_same_code(self):
        code, ran, _ = self.main("test_m")
        self.assertEqual((code, sorted(ran)), (0, ["test_m.A", "test_m.B"]))
        code, ran, err = self.main("test_m")
        self.assertEqual((code, ran), (0, []), err)
        self.assertIn("skipped 2 classes green at the same code; FOREMAN_NO_TEST_CACHE=1 runs them", err)
        self.assertRegex(err, r"\nRan 0 tests in [\d.]+s \(0 jobs, [^\n]*\); 2 more green in the cache\n")
        self.assertEqual(err.strip().splitlines()[-1], "OK")
        lib = os.path.join(os.path.dirname(run.HERE), "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        from sampo import core
        self.assertEqual(core.judged(0, err)[0], 0, "a fully cached run is evidence, not a run of no tests")
        self.assertTrue(core.passed_summary(err))
        self.assertEqual(core.judged(0, "Ran 0 tests in 0.000s\n\nOK\n")[0], 5, "a run of nothing still proves nothing")

    def test_a_partly_cached_run_counts_ran_and_cached_apart(self):
        self.main("test_m")
        draws = iter([0.05, 0.9])  # A drawn, B cached
        _, ran, err = self.main("test_m", sample=lambda: next(draws))
        self.assertEqual(ran, ["test_m.A"])
        self.assertRegex(err, r"\nRan 1 tests in [\d.]+s \(1 jobs, [^\n]*\); 1 more green in the cache\n")

    def test_a_change_to_a_file_a_class_read_reruns_only_that_class(self):
        self.main("test_m")
        self.blobs["b.py"] = "kb2"
        _, ran, err = self.main("test_m")
        self.assertEqual(ran, ["test_m.B"])
        self.assertIn("skipped 1 classes", err)

    def test_a_change_no_class_read_reruns_none(self):
        self.main("test_m")
        self.blobs.update({"other.py": "ko2", "new.py": "kn"})
        self.assertEqual(self.main("test_m")[1], [])

    def test_a_new_or_renamed_class_and_one_nothing_was_recorded_for_run(self):
        self.blind = {"B"}
        self.main("test_m")
        _, ran, _ = self.main("test_m", cids=("test_m.A", "test_m.B", "test_n.C"))
        self.assertEqual(sorted(ran), ["test_m.B", "test_n.C"])
        self.assertEqual(sorted(self.cache()), ["test_m.A", "test_n.C"])

    def test_the_switch_the_full_gate_k_and_a_nested_run_never_skip(self):
        self.main("test_m")
        for argv, env in ((["test_m"], {"FOREMAN_NO_TEST_CACHE": "1"}), (["test_m"], {"SAMPO_NO_TEST_CACHE": "1"}),
                          ([], {}), (["test_m", "-k", "test_0"], {}), (["test_m"], {"FOREMAN_SLOT": "3"})):
            with self.subTest(argv=argv, env=env), mock.patch.dict(os.environ, env):
                code, ran, err = self.main(*argv)
                self.assertEqual((code, sorted(ran)), (0, ["test_m.A", "test_m.B"]), err)
                self.assertNotIn("skipped", err)

    def test_the_post_merge_opt_in_lets_a_full_run_skip_and_reaches_no_child(self):  # T-3638
        self.main()  # a plain full run records
        code, ran, err = self.main()
        self.assertEqual((code, sorted(ran)), (0, ["test_m.A", "test_m.B"]), "a plain full run never skips")
        with mock.patch.dict(os.environ, {"SAMPO_CACHE_FULL": "1"}):
            code, ran, err = self.main()
            self.assertEqual((code, ran), (0, []), err)
            self.assertNotIn("SAMPO_CACHE_FULL", os.environ, "children must not inherit the opt-in")
            self.assertNotIn("FOREMAN_CACHE_FULL", os.environ)
        with mock.patch.dict(os.environ, {"SAMPO_CACHE_FULL": "0"}):  # review: "0" is off, as for NO_CACHE
            code, ran, err = self.main()
            self.assertEqual((code, sorted(ran)), (0, ["test_m.A", "test_m.B"]), err)

    def test_the_full_gate_records_for_the_next_named_run(self):
        self.main()
        self.assertEqual([r[1:] for r in self.cache()["test_m.A"]], [[1, ["a.py"]]])
        _, ran, _ = self.main("test_m")
        self.assertEqual(ran, [])

    def test_the_cache_is_the_shared_git_folder_s(self):
        self.git_dir = "/repo/.git"
        self.main("test_m")
        self.assertEqual(sorted(self.cache()), ["test_m.A", "test_m.B"])

    def test_each_class_keeps_its_last_three_codes(self):
        for v in ("1", "2", "3", "4"):
            self.blobs["a.py"] = v
            self.main("test_m")
        self.assertEqual(len(self.cache()["test_m.A"]), run.KEEP)
        self.blobs["a.py"] = "2"
        self.assertEqual(self.main("test_m")[1], [], "a lane's code beside main's: both still cached")
        self.blobs["a.py"] = "1"
        self.assertEqual(self.main("test_m")[1], ["test_m.A"], "the oldest was let go")

    def test_only_a_fully_run_green_class_is_recorded(self):
        self.fail_ids = {"test_m.B"}
        self.assertEqual(self.main("test_m")[0], 1)
        self.assertEqual(sorted(self.cache()), ["test_m.A"])
        shutil.rmtree(os.path.join(self.d, "sampo"))
        self.fail_ids = set()
        self.main("test_m", tests=3, matched=2)  # the names matched 2 of each class's 3 tests
        self.assertIsNone(self.cache())

    def test_a_class_whose_files_changed_during_the_run_is_not_recorded(self):
        self.during = {"test_m.A": {"a.py": "ka2"}}  # A's file edited while A ran: it ran the old or the new
        self.main("test_m")
        self.assertEqual(sorted(self.cache()), ["test_m.B"])

    def test_a_class_that_skipped_a_test_is_not_recorded(self):  # skipUnless(chrome): green here, maybe not elsewhere
        self.skips = {"test_m.A"}
        self.assertEqual(self.main("test_m")[0], 0)
        self.assertEqual(sorted(self.cache()), ["test_m.B"])

    def test_a_split_class_is_recorded_once_all_its_tests_passed(self):
        seed = {"test_m.A": 99}  # slow last time: one job per test
        with open(os.environ["FM_TEST_TIMES"], "w") as f:
            json.dump(seed, f)
        _, ran, _ = self.main("test_m", cids=("test_m.A",), tests=2)
        self.assertEqual(sorted(ran), ["test_m.A.test_0", "test_m.A.test_1"])
        self.assertEqual([r[1:] for r in self.cache()["test_m.A"]], [[2, ["a.py"]]])

    def test_a_sampled_class_reruns_and_its_failure_drops_the_cache_and_runs_the_rest(self):
        self.main("test_m")
        _, ran, _ = self.main("test_m", sample=0.0)  # every cached class drawn
        self.assertEqual(sorted(ran), ["test_m.A", "test_m.B"])
        draws = iter([0.05, 0.9])  # A drawn, B skipped
        self.fail_ids = {"test_m.A"}
        code, ran, err = self.main("test_m", sample=lambda: next(draws))
        self.assertEqual(code, 1)
        self.assertEqual(ran, ["test_m.A", "test_m.B"], "the cache is wrong: what it skipped runs too")
        self.assertIn("test_m.A failed though the cache had it green", err)
        self.assertEqual(self.cache(), {}, "dropped, and nothing recorded from this run")

    def test_a_sampled_class_the_deadline_stopped_keeps_the_cache(self):
        self.main("test_m")
        self.late = {"test_m.A"}
        code, _, err = self.main("test_m", sample=0.0)
        self.assertEqual(code, 124, err)
        self.assertNotIn("the cache is wrong", err)
        self.assertEqual(sorted(self.cache()), ["test_m.A", "test_m.B"])

    def test_a_corrupt_cache_is_ignored(self):
        p = run.cache_path("/repo")
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as f:
            f.write("{not json")
        code, ran, _ = self.main("test_m")
        self.assertEqual((code, len(ran)), (0, 2))
        self.assertEqual(sorted(self.cache()), ["test_m.A", "test_m.B"])


def rec(key, n=1, paths=("x.py",)):
    return [key, n, list(paths)]


class Store(unittest.TestCase):
    def test_the_path_is_per_repo_under_xdg_cache_home(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/c"}):
            a, b = run.cache_path("/r/one"), run.cache_path("/r/two")
        self.assertTrue(a.startswith("/c/sampo/run-cache/") and a.endswith(".json"), a)
        self.assertNotEqual(a, b)
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": ""}):
            self.assertTrue(run.cache_path("/r").startswith(os.path.expanduser("~/.cache/sampo/run-cache/")))

    def test_a_write_is_atomic_and_never_follows_a_planted_link(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "c.json")
            victim = os.path.join(d, "victim")
            with open(victim, "w") as f:
                f.write("mine")
            os.makedirs(os.path.dirname(p))
            os.symlink(victim, f"{p}.{os.getpid()}")
            old = os.path.join(d, "sub", "gone-lane.json")
            open(old, "w").close()
            os.utime(old, (1, 1))
            run.save_cache(p, {"m.C": rec("k")}, 0)
            with open(victim) as f:
                self.assertEqual(f.read(), "mine")
            self.assertEqual(run.load_cache(p), (0, {"m.C": [rec("k")]}))
            self.assertFalse(os.path.exists(old), "a repo's cache unused for weeks (a removed lane's) is pruned")

    def test_paths_are_stored_once_however_many_classes_read_them(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            run.save_cache(p, {"m.A": rec("ka", 1, ["a.py", "core.py"]), "m.B": rec("kb", 2, ["core.py"])}, 0)
            with open(p) as f:
                self.assertEqual(sorted(json.load(f)["paths"]), ["a.py", "core.py"])
            self.assertEqual(run.load_cache(p)[1], {"m.A": [rec("ka", 1, ["a.py", "core.py"])],
                                                    "m.B": [rec("kb", 2, ["core.py"])]})

    def test_saves_merge_and_a_drop_is_not_undone_by_a_save_that_read_before_it(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            gen, _ = run.load_cache(p)  # two runs read the cache
            run.save_cache(p, {"m.A": rec("ka")}, gen)
            run.save_cache(p, {"m.B": rec("kb")}, gen)
            self.assertEqual(run.load_cache(p), (gen, {"m.A": [rec("ka")], "m.B": [rec("kb")]}), "merged")
            run.save_cache(p, {"m.A": rec("ka2")}, gen)
            self.assertEqual(run.load_cache(p)[1]["m.A"], [rec("ka2"), rec("ka")], "the newest first")
            run.drop_cache(p)  # a third found it wrong
            run.save_cache(p, {"m.C": rec("kc")}, gen)  # a run that read it before the drop
            self.assertEqual(run.load_cache(p), (gen + 1, {}))
            run.save_cache(p, {"m.D": rec("kd")}, gen + 1)  # one that read it after
            self.assertEqual(run.load_cache(p)[1], {"m.D": [rec("kd")]})

    def test_t3502_keys_read_as_an_empty_cache(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w") as f:
                json.dump({"gen": 2, "classes": {"m.A": ["k", 1]}}, f)
            self.assertEqual(run.load_cache(p), (0, {}))

    @unittest.skipUnless(run.fcntl, "no flock here")
    def test_a_save_waits_for_the_lock(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            fd = os.open(p + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            run.fcntl.flock(fd, run.fcntl.LOCK_EX)
            t = threading.Thread(target=run.save_cache, args=(p, {"m.A": rec("ka")}, 0))
            t.start()
            t.join(0.3)
            self.assertTrue(t.is_alive(), "blocked on the lock another run holds")
            self.assertFalse(os.path.exists(p))
            os.close(fd)
            t.join(10)
            self.assertEqual(run.load_cache(p)[1], {"m.A": [rec("ka")]})


ONE = ("import os, sys, unittest\nHERE = os.path.dirname(os.path.abspath(__file__))\n"
       "sys.path.insert(0, os.path.join(HERE, '..', 'lib'))\nimport mod_a\n"
       "class One(unittest.TestCase):\n    def test_a(self):\n"
       "        open(os.path.join(HERE, 'fixtures', 'data.txt')).read()\n")
TWO = ("import os, subprocess, sys, unittest\nLIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib')\n"
       "class Two(unittest.TestCase):\n    def test_a(self):\n"
       "        subprocess.run([sys.executable, os.path.join(LIB, 'tool.py')], cwd=LIB, check=True)\n"
       "    def test_b(self):\n        pass\n")


class EndToEnd(unittest.TestCase):
    """The real run.py and runcover.py in a scratch repo: a rerun skips; a change reruns the classes that read it,
    in a child process too; a helper beside run.py reruns all; a renamed test runs."""

    def repo(self, files, names):
        """A scratch repo holding files, run.py and runcover.py; go(*names) runs run.py there (names by default) and
        returns its stderr; stale() names the classes the cache would rerun now (no record matches the tree)."""
        top = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, top, True)
        d = os.path.join(top, "repo")
        tests = os.path.join(d, "plugin", "tests")
        os.makedirs(tests)
        for name in ("run.py", "runcover.py"):
            shutil.copy(os.path.join(run.HERE, name), os.path.join(tests, name))
        scratch(d, files)
        env = outer(XDG_CACHE_HOME=os.path.join(top, "cache"), FOREMAN_SLOTS="0", FM_TEST_TIMES=os.path.join(top, "t.json"))

        def go(*more):
            r = subprocess.run([sys.executable, os.path.join(tests, "run.py"), *(more or names)],
                               capture_output=True, text=True, timeout=300, env=env, cwd=d)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stderr

        def stale():
            with mock.patch.object(run, "HERE", tests), mock.patch.dict(os.environ, {"XDG_CACHE_HOME": env["XDG_CACHE_HOME"]}):
                got = run.tree()
                value = run.values(*got[:2])
                recs = run.load_cache(run.cache_path(got[2]))[1]
            return sorted(c for c, rs in recs.items() if not any(r[0] == run.digest(r[2], value) for r in rs))
        return d, go, stale

    @unittest.skipUnless(MON, "sys.monitoring needs Python 3.12")
    def test_an_edited_function_reruns_the_classes_that_ran_it_and_no_other(self):  # T-3603
        use = "import os, sys, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))\n"
        d, go, stale = self.repo({
            "plugin/lib/mod_a.py": "A = 1\n\n\ndef used():\n    return 1\n\n\ndef unused():\n    return 2\n",
            "plugin/tests/test_one.py": use + "import mod_a\nclass One(unittest.TestCase):\n    def test_a(self):\n"
                                              "        self.assertTrue(mod_a.used())\n",
            "plugin/tests/test_two.py": use + "import mod_a\nclass Two(unittest.TestCase):\n    def test_a(self):\n"
                                              "        self.assertTrue(mod_a.A)\n",
            "plugin/tests/test_three.py": use + "class Three(unittest.TestCase):\n    def test_a(self):\n        pass\n"},
            ("test_one", "test_two", "test_three"))

        def edit(old, new):
            p = os.path.join(d, "plugin/lib/mod_a.py")
            with open(p) as f:
                text = f.read()
            self.assertIn(old, text)
            with open(p, "w") as f:
                f.write(text.replace(old, new, 1))
        self.assertRegex(go(), r"Ran 3 tests")
        self.assertEqual(stale(), [])
        edit("return 2", "return 22")  # a function no class ran
        self.assertEqual(stale(), [], "an edit to a function no class called reruns none")
        self.assertTrue(any(re.search(r"skipped [23] classes", go()) for _ in range(3)))
        edit("return 1", "return 11")  # the function test_one ran
        self.assertEqual(stale(), ["test_one.One"])
        edit("A = 1", "A = 2")  # module level: every class that imported it
        self.assertEqual(stale(), ["test_one.One", "test_two.Two"])
        self.assertRegex(go("test_one", "test_two", "test_three"), r"Ran (2 tests .*; 1 more green|3 tests)")

    @unittest.skipUnless(MON, "sys.monitoring needs Python 3.12")
    def test_a_hook_run_through_its_json_in_a_child_is_selected_by_the_function_it_ran(self):  # T-3603: T-3670's miss
        """test_firewall.OutlineFirst drives the Read hook only through its JSON payload in another process, so no map
        link or symbol search tied it to hooks._outline_first, and merge 888c3d75d's change to that function (and to the
        constant beside it) went red in build. The record follows the hook into the child, and the affected pick reads
        it: the class that ran the function is selected, the one that ran another handler of the same hook is not."""
        hooks = ("import json, sys\n\n\nOUTLINE_LINES, OUTLINE_ROWS = 600, 80\n\n\n"
                 "def _outline_first(pl):\n    ti = pl.get('tool_input') or {}\n"
                 "    if any(ti.get(k) is not None for k in ('offset', 'limit')):\n        return None\n"
                 "    return 'outline: ' + str(OUTLINE_ROWS)\n\n\n"
                 "def _bash(pl):\n    return 'bash'\n\n\n"
                 "HANDLERS = {'Read': _outline_first, 'Bash': _bash}\n\n\n"
                 "def run():\n    pl = json.load(sys.stdin)\n    print(HANDLERS[pl['tool_name']](pl))\n")
        drive = ("import json, os, subprocess, sys, unittest\n"
                 "HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib', 'hook.py')\n\n\n"
                 "def pre(tool, ti):\n    return subprocess.run([sys.executable, HOOK], input=json.dumps({'tool_name': tool, "
                 "'tool_input': ti}), capture_output=True, text=True, check=True).stdout\n\n\n")
        d, go, stale = self.repo({
            "plugin/lib/hooks.py": hooks, "plugin/lib/hook.py": "import hooks\nhooks.run()\n",  # the hook's entry script
            "plugin/tests/test_firewall.py": drive + "class OutlineFirst(unittest.TestCase):\n    def test_a(self):\n"
                                             "        self.assertIn('outline', pre('Read', {'file_path': 'big.py'}))\n",
            "plugin/tests/test_shell.py": drive + "class Shell(unittest.TestCase):\n    def test_a(self):\n"
                                          "        self.assertEqual('bash\\n', pre('Bash', {}))\n",
            "plugin/tests/test_plain.py": "import unittest\nclass Plain(unittest.TestCase):\n    def test_a(self):\n"
                                          "        pass\n"},
            ("test_firewall", "test_shell", "test_plain"))
        self.assertRegex(go(), r"Ran 3 tests")
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(os.path.dirname(d), "cache")}):
            recs = run.load_cache(run.cache_path(os.path.join(d, ".git")))[1]
        self.assertIn("plugin/lib/hooks.py::_outline_first", recs["test_firewall.OutlineFirst"][0][2],
                      "the record follows the hook into the child the test runs it in")
        path = os.path.join(d, "plugin/lib/hooks.py")
        with open(path, "w") as f:  # 888c3d75d's change: the constant, the early-out and the text returned
            f.write(hooks.replace("600, 80", "600, 6  # packed rows").replace(
                "    if any(", "    if ti.get('allow_large') is True or any(").replace(
                "'outline: ' + str(OUTLINE_ROWS)", "'outline; pass allow_large: ' + str(OUTLINE_ROWS)"))
        git(d, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qam", "outline rows")
        self.assertEqual(stale(), ["test_firewall.OutlineFirst", "test_shell.Shell"], "run.py: the module's rest changed")
        lib = os.path.realpath(os.path.join(run.HERE, "..", "lib"))
        if lib not in sys.path:
            sys.path.insert(0, lib)
        from sampo import map as smap
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(os.path.dirname(d), "cache")}):
            self.assertEqual(["plugin/tests/test_firewall.py"],
                             smap.ran_tests(d, "HEAD~1", "HEAD", ["plugin/lib/hooks.py"]))

    def test_a_change_reruns_the_classes_that_read_it(self):
        d, go, _ = self.repo({"plugin/tests/test_one.py": ONE, "plugin/tests/test_two.py": TWO, "plugin/tests/common.py": "",
                              "plugin/tests/fixtures/data.txt": "x\n", "plugin/lib/mod_a.py": "A = 1\n",
                              "plugin/lib/mod_b.py": "B = 1\n", "plugin/lib/tool.py": "import mod_b\n", "README.md": "hi\n"},
                             ("test_one", "test_two"))

        def edit(path):  # a statement, not a comment: a module's comment changes no code it ran (T-3603)
            with open(os.path.join(d, path), "a") as f:
                f.write("edited = 1\n")
        self.assertRegex(go(), r"Ran 3 tests")
        # test_one has 1 test, test_two 2; each cached class reruns 1 time in 10, so a skip shows within 3 runs (1e-6)
        self.assertTrue(any("classes green at the same code" in go() for _ in range(3)))
        edit("README.md")  # read by neither
        self.assertTrue(any("classes green at the same code" in go() for _ in range(3)))
        edit("plugin/lib/mod_b.py")  # only test_two's child imports it
        self.assertRegex(go(), r"Ran (2 tests .*; 1 more green|3 tests)")
        edit("plugin/tests/fixtures/data.txt")  # only test_one reads it
        self.assertRegex(go(), r"Ran (1 tests .*; 2 more green|3 tests)")
        edit("plugin/tests/common.py")  # a helper beside run.py: every class's
        self.assertRegex(go(), r"Ran 3 tests in [^\n]*\)\n")
        tests = os.path.join(d, "plugin", "tests")
        os.rename(os.path.join(tests, "test_one.py"), os.path.join(tests, "test_uno.py"))
        self.assertRegex(go("test_uno", "test_two"), r"Ran (1 tests .*; 2 more green|3 tests)")


if __name__ == "__main__":
    unittest.main()
