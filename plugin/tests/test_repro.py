"""T-3279: sampo repro builds the site and the README figures twice from the working tree and once from a git worktree
of REF, names the files that differ between the two working-tree builds (nondeterministic) and between the working tree
and REF (changed), and exits 1 only when a build is nondeterministic."""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import repro as r

# The fake builders run with the stage (a copy of the repo) as cwd, like the real ones; they read VERSION from it.
FAKE_SITE = ("import os, sys\n"
             "out = sys.argv[1]\n"
             "os.makedirs(out)\n"
             "with open(os.path.join(out, 'index.html'), 'w') as f:\n"
             "    f.write(open('VERSION').read())\n")
NONDET_SITE = FAKE_SITE.replace("f.write(open('VERSION').read())",
                                "f.write(open('VERSION').read() + str(__import__('time').time_ns()))")
FAKE_README = ("with open('README.md', 'w') as f:\n"
               "    f.write('readme ' + open('VERSION').read())\n")
BUILDERS = [["site/build.py", "{out}"], ["demo/readme_figures.py"]]
EMPTY = {"added": [], "removed": [], "changed": []}


def git(path, *args):
    return subprocess.run(["git", "-C", path, "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout


def write(root, files):
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(body)


def make_repo(parent, commits, name="app"):
    """One commit per dict of files: commit 1 is the parent of commit 2 and so on; HEAD is the last one."""
    root = os.path.join(parent, name)
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    for files in commits:
        write(root, files)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "c")
    return root


def base(version, site=FAKE_SITE, readme=FAKE_README):
    return {"site/build.py": site, "demo/readme_figures.py": readme, "VERSION": version}


class DiffTrees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def tree(self, name, files):
        root = os.path.join(self.tmp, name)
        write(root, files)
        return root

    def test_names_added_removed_and_changed_files_by_relative_path(self):
        a = self.tree("a", {"same.txt": "s", "gone.txt": "g", "edit/x.txt": "1", "deep/keep.txt": "k"})
        b = self.tree("b", {"same.txt": "s", "new/y.txt": "n", "edit/x.txt": "2", "deep/keep.txt": "k"})
        self.assertEqual(r.diff_trees(a, b), {"added": ["new/y.txt"], "removed": ["gone.txt"],
                                              "changed": ["edit/x.txt"]})

    def test_identical_trees_differ_in_nothing(self):
        a = self.tree("a", {"x/y.txt": "1"})
        b = self.tree("b", {"x/y.txt": "1"})
        self.assertEqual(r.diff_trees(a, b), EMPTY)

    def test_a_symlink_is_compared_by_its_target(self):
        a = self.tree("a", {"t1": "", "t2": ""})
        b = self.tree("b", {"t1": "", "t2": ""})
        os.symlink("t1", os.path.join(a, "link"))
        os.symlink("t2", os.path.join(b, "link"))
        self.assertEqual(r.diff_trees(a, b)["changed"], ["link"])


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.scratch = os.path.join(self.tmp, "scratch")
        os.makedirs(self.scratch)
        patch = mock.patch.object(tempfile, "tempdir", self.scratch)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_builder_whose_output_moves_between_two_runs_is_named_nondeterministic(self):
        repo = make_repo(self.tmp, [base("1\n", site=NONDET_SITE)])
        got = r.run(repo, "HEAD", builders=BUILDERS)
        self.assertEqual(got["nondeterministic"], ["built-site/index.html"])

    def test_a_deterministic_build_names_what_differs_from_the_parent_commit(self):
        repo = make_repo(self.tmp, [base("1\n"), base("2\n")])
        got = r.run(repo, "HEAD~1", builders=BUILDERS)
        self.assertEqual(got["nondeterministic"], [])
        self.assertEqual(got["changed"], ["README.md", "built-site/index.html"])
        self.assertEqual(r.run(repo, "HEAD", builders=BUILDERS)["changed"], [])

    def test_an_uncommitted_edit_is_built_from_the_working_tree_and_shows_as_changed(self):
        repo = make_repo(self.tmp, [base("1\n")])
        write(repo, {"VERSION": "3\n"})
        got = r.run(repo, "HEAD", builders=BUILDERS)
        self.assertEqual(got["changed"], ["README.md", "built-site/index.html"])

    def test_a_file_a_builder_deletes_is_named_changed(self):
        # readme_figures deletes orphan SVGs: a removal is a change too, and the artifacts can't show it
        readme = FAKE_README + ("import os\n"
                                "if open('VERSION').read() == '2\\n' and os.path.exists('old.svg'):\n"
                                "    os.remove('old.svg')\n")
        repo = make_repo(self.tmp, [{**base("1\n", readme=readme), "old.svg": "x"},
                                    {**base("2\n", readme=readme), "old.svg": "x"}])
        got = r.run(repo, "HEAD~1", builders=BUILDERS)
        self.assertEqual(got["nondeterministic"], [])
        self.assertEqual(got["changed"], ["README.md", "built-site/index.html", "old.svg"])

    def test_the_run_leaves_no_worktree_and_no_temp_folder_behind(self):
        repo = make_repo(self.tmp, [base("1\n"), base("2\n")])
        r.run(repo, "HEAD~1", builders=BUILDERS)
        self.assertEqual([l for l in git(repo, "worktree", "list", "--porcelain").splitlines()
                          if l.startswith("worktree ")], [f"worktree {os.path.realpath(repo)}"])  # macOS: /var is /private/var
        self.assertEqual(os.listdir(self.scratch), [])

    def test_a_failing_builder_raises_naming_it(self):
        repo = make_repo(self.tmp, [base("1\n", site="raise SystemExit('boom')\n")])
        with self.assertRaisesRegex(r.ReproError, "site/build.py"):
            r.run(repo, "HEAD", builders=BUILDERS)
        self.assertEqual(os.listdir(self.scratch), [], "a failed run still cleans up")

    def test_an_unknown_ref_raises_before_anything_is_built(self):
        repo = make_repo(self.tmp, [base("1\n")])
        with self.assertRaisesRegex(r.ReproError, "no-such-ref"):
            r.run(repo, "no-such-ref", builders=BUILDERS)



class Git(unittest.TestCase):
    def test_a_hung_git_is_a_repro_error_when_checked_and_a_failed_result_when_not(self):
        # a timeout must not look like "nondeterministic" (exit 1), nor escape the worktree cleanup
        hung = subprocess.TimeoutExpired(["git"], 1)
        with mock.patch.object(r.subprocess, "run", side_effect=hung):
            with self.assertRaisesRegex(r.ReproError, "timed out"):
                r._git("/nowhere", "worktree", "add")
            self.assertNotEqual(r._git("/nowhere", "worktree", "remove", check=False).returncode, 0)


class Cmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.old = os.getcwd()
        self.addCleanup(os.chdir, self.old)
        patch = mock.patch.object(r, "BUILDERS", BUILDERS)
        patch.start()
        self.addCleanup(patch.stop)

    def cmd(self, args, cwd):
        os.chdir(cwd)
        ref = args[args.index("--ref") + 1] if "--ref" in args else "HEAD"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = r.cmd(argparse.Namespace(ref=ref, json=False))
        return code, out.getvalue(), err.getvalue()

    def test_a_nondeterministic_build_prints_the_count_and_names_and_exits_1(self):
        repo = make_repo(self.tmp, [base("1\n", site=NONDET_SITE)])
        code, out, _ = self.cmd([], repo)
        self.assertEqual(code, 1)
        self.assertIn("nondeterministic: 1 file(s)", out)
        self.assertIn("built-site/index.html", out)
        self.assertIn("changed vs HEAD: ", out)

    def test_a_change_against_ref_alone_exits_0_and_names_the_ref(self):
        repo = make_repo(self.tmp, [base("1\n"), base("2\n")])
        code, out, _ = self.cmd(["--ref", "HEAD~1"], repo)
        self.assertEqual(code, 0)
        self.assertIn("nondeterministic: 0 file(s)", out)
        self.assertIn("changed vs HEAD~1: 2 file(s)", out)
        self.assertIn("README.md", out)

    def test_it_runs_from_a_subdirectory_of_the_repo(self):
        repo = make_repo(self.tmp, [base("1\n"), {**base("2\n"), "sub/keep.txt": "k"}])
        code, out, _ = self.cmd(["--ref", "HEAD~1"], os.path.join(repo, "sub"))
        self.assertEqual(code, 0)
        self.assertIn("changed vs HEAD~1: 2 file(s)", out)

    def test_a_failing_builder_exits_2_not_1(self):
        repo = make_repo(self.tmp, [base("1\n", site="raise SystemExit('boom')\n")])
        code, _, err = self.cmd([], repo)
        self.assertEqual(code, 2)
        self.assertIn("site/build.py", err)

    def test_an_unknown_ref_exits_2(self):
        repo = make_repo(self.tmp, [base("1\n")])
        code, _, err = self.cmd(["--ref", "no-such-ref"], repo)
        self.assertEqual(code, 2)
        self.assertIn("no-such-ref", err)


class Verb(ForemanTestCase):
    def test_the_verb_is_registered_and_sampo_which_lists_it(self):
        self.assertIn("fm repro", self.fm("which", "check the build is reproducible").stdout)

    def test_fm_repro_reaches_the_verb(self):
        repo = make_repo(self.tmp, [base("1\n")], name="rr")
        p = self.fm("repro", "--ref", "no-such-ref", cwd=repo, check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("no-such-ref", p.stderr)

    def test_fm_repro_json_prints_the_result_as_json(self):
        repo = make_repo(self.tmp, [base("1\n")], name="rj")
        p = self.fm("repro", "--json", cwd=repo, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        got = json.loads(p.stdout)
        self.assertEqual(got, {"ref": "HEAD", "nondeterministic": [], "changed": []})

    def test_the_help_states_what_a_run_costs(self):
        self.assertIn("three copies", self.fm("repro", "--help").stdout)


class Smoke(unittest.TestCase):
    def test_head_is_built_by_the_real_builders_in_copies_of_the_tree(self):
        root = c.git_root(os.path.dirname(os.path.abspath(__file__)))
        seen = []
        real = r._run_builder

        def spy(argv, tree, site):
            seen.append((argv[0], tree))
            return real(argv, tree, site)

        with mock.patch.object(r, "_run_builder", side_effect=spy):
            got = r.run(root, "HEAD")
        self.assertEqual({name for name, _ in seen}, {"site/build.py", "demo/readme_figures.py"})
        self.assertTrue(all(os.path.realpath(tree) != os.path.realpath(root) for _, tree in seen))
        self.assertEqual(sorted(got), ["changed", "nondeterministic"])
