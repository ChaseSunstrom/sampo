"""T-3290 (moon C-006917): sampo seams FILE ranks the cleanest split clusters of a big module (hubs and mutable state left
out, fewest boundary edges first, then the most recent edits), and the top louhi.py cluster, the scratch store and the
generation grow that fills it, lives in louhi_scratch.py behind the same names. The finder tests build their own tiny
module in a temp git repo; the real louhi.py is only read."""
import ast
import builtins
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
LIB = os.path.join(REPO, "plugin", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from sampo import seams  # noqa: E402

LOUHI = os.path.join(LIB, "sampo", "louhi.py")
SCRATCH = os.path.join(LIB, "sampo", "louhi_scratch.py")
TOP = ("_scratch", "_gen_grow")  # the top-ranked louhi.py cluster that the finder reports (T-3290 criterion 2)

# two clusters that share nothing, a hub that links both (hub=3 makes it one), and a function that writes a mutable global
MODULE = '''\
import re

STATE = {}


def helper_a(x):
    return re.sub("a", "b", str(x))


def parse_a(x):
    return helper_a(x) + "!"


def helper_b(y):
    return y + 1


def parse_b(y):
    return helper_b(y) * 2


def hub(z):
    return parse_a(z) + parse_b(z) + helper_a(z)


def remember(k):
    STATE[k] = helper_b(k)
    return k
'''

# two independent clusters, no hub: the boundary is 0 on both sides, so the edit history alone ranks them
EDITED = '''\
def helper_a(x):
    return x + 1


def parse_a(x):
    return helper_a(x) * 2


def helper_b(y):
    return y - 1


def parse_b(y):
    return helper_b(y) * 3
'''


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=60, check=True).stdout


def _repo(files):
    """A temp git checkout holding `files` ({rel: text}), one commit; returns its path."""
    root = tempfile.mkdtemp(prefix="seams-")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _commit(root, rel, text, msg):
    with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
        f.write(text)
    _git(root, "commit", "-q", "-am", msg)


class TestFinder(unittest.TestCase):
    def test_two_clusters_with_the_hub_and_the_mutable_state_left_out(self):
        got = seams.analyse(MODULE, hub=3)
        self.assertEqual(got["hubs"], ["hub"])
        self.assertIn("STATE", got["tainted"])
        self.assertIn("remember", got["tainted"])
        funcs = [k["funcs"] for k in got["clusters"]]
        self.assertEqual(funcs, [["helper_a", "parse_a"], ["helper_b", "parse_b"]])
        for k in got["clusters"]:
            self.assertEqual(k["back"], 0)  # nothing a cluster names lies outside it, once the hub is gone
            self.assertNotIn("hub", k["funcs"])
            self.assertNotIn("remember", k["funcs"])
        self.assertEqual(got["clusters"][0]["uses"], ["re"])  # the module import the cluster needs travels with it

    def test_a_single_definition_is_not_a_cluster(self):
        got = seams.analyse("def lone(x):\n    return x\n", hub=3)
        self.assertEqual(got["clusters"], [])

    def test_rank_puts_fewest_boundary_edges_first_then_the_most_edited(self):
        far = {"back": 1, "into": 0, "churn": 9, "lines": 5, "funcs": ["far"]}
        near = {"back": 0, "into": 0, "churn": 1, "lines": 5, "funcs": ["near"]}
        busy = {"back": 0, "into": 0, "churn": 4, "lines": 5, "funcs": ["busy"]}
        self.assertEqual([k["funcs"][0] for k in seams.rank([far, near, busy])], ["busy", "near", "far"])

    def test_churn_counts_the_commits_that_touched_each_cluster(self):
        root = _repo({"mod.py": EDITED})
        _commit(root, "mod.py", EDITED.replace("* 3", "* 4"), "bump b")
        _commit(root, "mod.py", EDITED.replace("* 3", "* 5"), "bump b again")
        _commit(root, "mod.py", EDITED.replace("* 3", "* 6").replace("* 2", "* 7"), "bump both")
        got = seams.collect(root, "mod.py", hub=3)
        self.assertIsNotNone(got)
        by = {k["funcs"][0]: k["churn"] for k in got["clusters"]}
        self.assertEqual(by, {"helper_a": 2, "helper_b": 4})  # its creation, then each later commit that touched its lines
        self.assertEqual([k["funcs"][0] for k in got["clusters"]], ["helper_b", "helper_a"])

    def test_a_folder_that_is_not_a_checkout_has_no_seams(self):
        root = tempfile.mkdtemp(prefix="seams-nogit-")
        with open(os.path.join(root, "mod.py"), "w", encoding="utf-8") as f:
            f.write(MODULE)
        self.assertIsNone(seams.collect(root, "mod.py", hub=3))


class TestVerb(unittest.TestCase):
    def test_the_verb_prints_clusters_with_lines_and_churn_and_names_the_hubs(self):
        root = _repo({"mod.py": MODULE})
        cwd = os.getcwd()
        out = io.StringIO()
        try:
            os.chdir(root)
            with contextlib.redirect_stdout(out):
                rc = seams.cmd_seams(Namespace(file="mod.py", top=10, hub=3, json=False))
        finally:
            os.chdir(cwd)
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("churn", text)
        self.assertIn("helper_a, parse_a", text)
        self.assertIn("helper_b, parse_b", text)
        self.assertIn("hubs (left out): hub", text)

    def test_a_file_outside_the_checkout_is_refused(self):
        root = _repo({"mod.py": MODULE})
        cwd = os.getcwd()
        err = io.StringIO()
        try:
            os.chdir(root)
            with contextlib.redirect_stderr(err):
                rc = seams.cmd_seams(Namespace(file="../elsewhere.py", top=10, hub=3, json=False))
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 2)
        self.assertIn("not a file inside the checkout", err.getvalue())

    def test_sampo_seams_runs_from_the_checkout_on_louhi(self):
        r = subprocess.run([sys.executable, os.path.join(REPO, "plugin", "bin", "sampo"), "seams",
                            "plugin/lib/sampo/louhi.py", "--top", "3"], cwd=REPO, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("churn", r.stdout)


class TestLouhi(unittest.TestCase):
    def test_the_real_louhi_clusters_are_ranked_and_leave_the_hubs_out(self):
        got = seams.collect(REPO, "plugin/lib/sampo/louhi.py")
        self.assertIsNotNone(got)
        self.assertGreaterEqual(len(got["clusters"]), 1)
        self.assertEqual(got["clusters"], seams.rank(got["clusters"]))
        for k in got["clusters"]:
            self.assertFalse(set(k["funcs"]) & set(got["hubs"]), k["funcs"])
            self.assertIn("churn", k)

    def test_the_top_cluster_lives_in_louhi_scratch_and_louhi_keeps_every_name(self):
        from sampo import louhi, louhi_scratch
        for name in TOP:
            self.assertIs(getattr(louhi, name), getattr(louhi_scratch, name), name)
        with open(LOUHI, encoding="utf-8") as f:
            top = {n.name for n in ast.parse(f.read()).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for name in TOP:
            self.assertNotIn(name, top)  # moved, not copied
        with open(SCRATCH, encoding="utf-8") as f:
            text = f.read()
        self.assertLessEqual(len(text.splitlines()), 250)

    def test_louhi_scratch_loads_no_name_it_does_not_bind(self):
        with open(SCRATCH, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bound = set(dir(builtins))
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                bound.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                bound.add(n.id)
            elif isinstance(n, ast.arg):
                bound.add(n.arg)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0] for a in n.names}
        loads = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        self.assertEqual(loads - bound, set())


if __name__ == "__main__":
    unittest.main()
