"""fm hotfn (T-3271, moon C-001076): Sampo's own functions ranked by churn x size x (1 + FIX touches); the top rows are
captured as refactor tasks, once per function. Fixed fixtures for rank(); a tmp git repo for collect() and the CLI."""
import copy
import json
import subprocess

from helpers import ForemanTestCase, git_repo

from sampo import cli
from sampo import core as c
from sampo import hotfn

MOD_V1 = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
MOD_FIX = "def a():\n    return 10\n\n\ndef b():\n    return 2\n"
MOD_TWEAK = "def a():\n    return 5\n\n\ndef b():\n    return 2\n"


def write(root, rel, text):
    with open(f"{root}/{rel}", "w") as f:
        f.write(text)


class RankFixtures(ForemanTestCase):
    ROWS = [
        {"file": "m.py", "func": "small", "lines": 2, "churn": 9, "fixes": 0},
        {"file": "m.py", "func": "big", "lines": 10, "churn": 3, "fixes": 1},
        {"file": "m.py", "func": "tie_b", "lines": 4, "churn": 2, "fixes": 0},
        {"file": "a.py", "func": "tie_a", "lines": 4, "churn": 2, "fixes": 0},
        {"file": "x.py", "func": "cold", "lines": 50, "churn": 0, "fixes": 0},
    ]

    def test_rank_orders_by_score_and_breaks_ties_by_path(self):
        # scores: big 3*10*2=60, small 9*2*1=18, tie_a 2*4*1=8, tie_b 2*4*1=8; cold has no churn and drops out
        ranked = hotfn.rank(self.ROWS)
        self.assertEqual([r["func"] for r in ranked], ["big", "small", "tie_a", "tie_b"])
        self.assertEqual([r["score"] for r in ranked], [60, 18, 8, 8])

    def test_rank_is_pure(self):
        before = copy.deepcopy(self.ROWS)
        first = hotfn.rank(self.ROWS)
        self.assertEqual(self.ROWS, before)
        self.assertEqual(hotfn.rank(self.ROWS), first)


class Collect(ForemanTestCase):
    def repo_with(self, *commits):
        """commits: (subject, text) pairs written to m.py, in order."""
        root = git_repo(self.tmp, "hf")
        for subject, text in commits:
            write(root, "m.py", text)
            subprocess.run(["git", "-C", root, "add", "-A"], check=True)
            subprocess.run(["git", "-C", root, "commit", "-qm", subject], check=True)
        return root

    def rows(self, root, p=None):
        return {r["func"]: r for r in hotfn.collect(root, p)}

    def test_churn_and_fix_commits_per_function(self):
        root = self.repo_with(("add mod", MOD_V1), ("fix: a returns ten", MOD_FIX))
        rows = self.rows(root)
        self.assertEqual(rows["a"], {"file": "m.py", "func": "a", "lines": 2, "churn": 2, "fixes": 1})
        self.assertEqual(rows["b"], {"file": "m.py", "func": "b", "lines": 2, "churn": 1, "fixes": 0})

    def test_methods_are_named_through_their_class(self):
        root = self.repo_with(("add mod", "class K:\n    def m(self):\n        return 1\n"))
        self.assertEqual(self.rows(root)["K.m"]["churn"], 1)

    def done_fix(self, root, scope):
        self.fm("init", cwd=root)
        return cli._create(c.find_project(root), "a returns five", "FIX", "S", "done", scope=scope)

    def test_commit_naming_a_done_fix_brief_is_a_fix(self):
        # the subject says nothing about a fix; the commit names the done FIX brief, so the brief makes it one
        root = self.repo_with(("add mod", MOD_V1))
        b = self.done_fix(root, ("m.py",))
        write(root, "m.py", MOD_TWEAK)
        subprocess.run(["git", "-C", root, "commit", "-qam", f"tweak a ({b.id})"], check=True)
        rows = self.rows(root, c.find_project(root))
        self.assertEqual((rows["a"]["churn"], rows["a"]["fixes"]), (2, 1))
        self.assertEqual((rows["b"]["churn"], rows["b"]["fixes"]), (1, 0))

    def test_done_fix_brief_no_commit_names_counts_its_scope_files(self):
        root = self.repo_with(("add mod", MOD_V1), ("tweak a", MOD_TWEAK))
        self.done_fix(root, ("m.py",))
        rows = self.rows(root, c.find_project(root))
        self.assertEqual((rows["a"]["fixes"], rows["b"]["fixes"]), (1, 1))

    def test_not_a_git_repo_is_none(self):
        self.assertIsNone(hotfn.collect(self.tmp))


class Cli(ForemanTestCase):
    def test_table_and_json(self):
        root = git_repo(self.tmp, "hcli")
        write(root, "m.py", MOD_V1)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "add mod"], check=True)
        write(root, "m.py", MOD_FIX)
        subprocess.run(["git", "-C", root, "commit", "-qam", "fix: a"], check=True)
        table = self.fm("hotfn", "--top", "1", cwd=root).stdout
        self.assertIn("m.py", table)
        self.assertIn("a", table)
        self.assertNotIn(" b ", table)  # --top 1 keeps only the hottest function
        top = json.loads(self.fm("hotfn", "--json", cwd=root).stdout)["functions"]
        self.assertEqual(top[0]["func"], "a")
        self.assertEqual(top[0]["score"], 8)

    def test_capture_files_the_top_once_per_function(self):
        root = git_repo(self.tmp, "hcap")
        write(root, "m.py", MOD_V1)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "add mod"], check=True)
        write(root, "m.py", MOD_FIX)
        subprocess.run(["git", "-C", root, "commit", "-qam", "fix: a"], check=True)
        self.fm("init", cwd=root)
        self.fm("hotfn", "--capture", "1", cwd=root)  # a: the best
        self.fm("hotfn", "--capture", "1", cwd=root)  # b: the best not yet filed; a is never filed again
        self.fm("hotfn", "--capture", "1", cwd=root)  # nothing left to file
        p = c.find_project(root)
        filed = [b for b in c.load_briefs(p, include_archive=True) if (b.meta or {}).get("hotfn")]
        self.assertEqual(sorted(b.meta["hotfn"] for b in filed), ["m.py::a", "m.py::b"])
        self.assertEqual({b.type for b in filed}, {"CLEAN"})

    def test_capture_refuses_another_checkout(self):
        root = git_repo(self.tmp, "hcapx")
        write(root, "m.py", MOD_V1)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "add mod"], check=True)
        self.fm("init", cwd=root)
        other = git_repo(self.tmp, "hother")
        write(other, "n.py", MOD_V1)
        subprocess.run(["git", "-C", other, "add", "-A"], check=True)
        subprocess.run(["git", "-C", other, "commit", "-qm", "add n"], check=True)
        r = self.fm("hotfn", other, "--capture", "1", cwd=root, check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("own checkout", r.stderr)
        self.assertEqual([b for b in c.load_briefs(c.find_project(root), include_archive=True)
                          if (b.meta or {}).get("hotfn")], [])

