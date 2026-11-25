"""T-5197 (child c1 of T-4311, Other machines within a minute): what a change needs from a running session.
reach.needs(root, a, b) sorts the files that differ between commits a and b into now, reload, restart and read;
reach.code_id(root) moves only on a committed plugin change (tests and docs are no plugin change)."""
import os
import subprocess

from helpers import SampoTestCase

from sampo import reach

BUCKETS = ("now", "reload", "restart", "read")


def head(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def commit(repo, files, msg, removed=()):
    """Write, delete and commit exactly these paths (the rest of the worktree stays as it is); returns the new HEAD."""
    for rel, body in files.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    for rel in removed:
        os.remove(os.path.join(repo, rel))
    subprocess.run(["git", "-C", repo, "add", "--", *files, *removed], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg], check=True)
    return head(repo)


def empty():
    return {k: [] for k in BUCKETS}


class NeedsBuckets(SampoTestCase):
    def test_a_two_commit_diff_sorts_into_now_reload_restart_and_read(self):
        a = head(self.repo)
        commit(self.repo, {"plugin/lib/sampo/x.py": "N = 1\n", "plugin/rules/r.md": "rule\n",
                           "plugin/tests/test_x.py": "N = 1\n"}, "code, a rule, a test")
        b = commit(self.repo, {"plugin/hooks/hooks.json": "{}\n", "plugin/monitors/monitors.json": "[]\n",
                               "docs/map/files-lib-6.md": "x\n", "MASTER.md": "x\n"}, "hooks, monitors, docs")
        self.assertEqual(reach.needs(self.repo, a, b), {
            "now": ["plugin/lib/sampo/x.py"], "reload": ["plugin/hooks/hooks.json"],
            "restart": ["plugin/monitors/monitors.json"], "read": ["plugin/rules/r.md"]})

    def test_each_kind_of_file_lands_in_its_bucket(self):
        cases = {"plugin/lib/sampo/x.py": "now", "plugin/lib/fmx.py": "now", "plugin/hooks/hook": "now",
                 "plugin/hooks/hooks.json": "reload", "plugin/monitors/monitors.json": "restart",
                 "plugin/rules/r.md": "read", "plugin/data/new.json": "restart", "plugin/README.md": None,
                 "plugin/README.md.sh": "restart", "plugin/README.mdx": "restart",
                 "plugin/hooks/hooks.json.bak": "now", "plugin/monitors/monitors.json.bak": "restart",
                 "plugin/tests/test_x.py": None, "docs/map/files-lib-6.md": None, "MASTER.md": None,
                 "MASTER.md.bak": None}
        for rel, want in cases.items():
            with self.subTest(rel=rel):
                self.assertEqual(reach.bucket_of(rel), want)

    def test_a_docs_only_commit_needs_nothing(self):
        a = head(self.repo)
        b = commit(self.repo, {"docs/map/files-lib-6.md": "x\n", "MASTER.md": "x\n"}, "docs")
        self.assertEqual(reach.needs(self.repo, a, b), empty())

    def test_a_deleted_plugin_file_counts_as_a_change(self):
        commit(self.repo, {"plugin/lib/sampo/gone.py": "N = 1\n"}, "seed")
        a = head(self.repo)
        b = commit(self.repo, {}, "remove it", removed=["plugin/lib/sampo/gone.py"])
        self.assertEqual(reach.needs(self.repo, a, b), {**empty(), "now": ["plugin/lib/sampo/gone.py"]})


class CodeId(SampoTestCase):
    def test_moves_only_on_a_committed_plugin_change(self):
        commit(self.repo, {"plugin/lib/sampo/x.py": "N = 1\n"}, "code")
        before = reach.code_id(self.repo)
        with open(os.path.join(self.repo, "plugin/lib/sampo/x.py"), "w", encoding="utf-8") as f:
            f.write("N = 2\n")
        self.assertEqual(reach.code_id(self.repo), before, "an uncommitted edit is no commit")
        commit(self.repo, {"docs/map/files-lib-6.md": "x\n"}, "docs only")
        self.assertEqual(reach.code_id(self.repo), before, "a docs-only commit")
        commit(self.repo, {"plugin/tests/test_x.py": "x\n"}, "a test only")
        self.assertEqual(reach.code_id(self.repo), before, "a test-only commit")
        commit(self.repo, {"plugin/lib/sampo/x.py": "N = 2\n"}, "a plugin change")
        self.assertNotEqual(reach.code_id(self.repo), before, "a committed plugin change")

    def test_a_ref_starting_with_a_dash_is_refused(self):
        with self.assertRaises(ValueError):
            reach.code_id(self.repo, "--output=/tmp/x")
        with self.assertRaises(ValueError):
            reach.needs(self.repo, "--output=/tmp/x", "HEAD")

    def test_the_same_commit_gives_the_same_id(self):
        self.assertEqual(reach.code_id(self.repo), reach.code_id(self.repo))
        self.assertEqual(len(reach.code_id(self.repo)), 12)
