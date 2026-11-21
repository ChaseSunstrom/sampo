"""T-5082 (child of T-4308): the green ref names the last tree whose hook smoke passed. It is a local ref outside
refs/heads and refs/tags, so pushing the branch or the tags never carries it; mark refuses a sha the repo lacks."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from sampo import greenref  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.test", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def git(root, *argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    r = subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, env=dict(env, **ENV), timeout=60)
    assert r.returncode == 0, (argv, r.stderr)
    return r.stdout.strip()


class GreenRef(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.first = self.commit("one")
        self.second = self.commit("two")

    def commit(self, text):
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write(text)
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-q", "-m", text)
        return git(self.repo, "rev-parse", "HEAD")

    def test_the_ref_is_named_outside_heads_and_tags(self):
        self.assertEqual(greenref.HOOK_REF, "refs/sampo/green/hook")
        for ns in ("refs/heads/", "refs/tags/"):
            self.assertFalse(greenref.HOOK_REF.startswith(ns))

    def test_read_is_none_with_no_ref(self):
        self.assertIsNone(greenref.read(self.repo))

    def test_mark_then_read_gives_the_sha(self):
        self.assertEqual(greenref.mark(self.repo, self.first), self.first)
        self.assertEqual(greenref.read(self.repo), self.first)
        self.assertEqual(git(self.repo, "rev-parse", greenref.HOOK_REF), self.first)

    def test_a_second_mark_moves_the_ref(self):
        greenref.mark(self.repo, self.first)
        greenref.mark(self.repo, self.second)
        self.assertEqual(greenref.read(self.repo), self.second)

    def test_a_short_sha_is_stored_whole(self):
        self.assertEqual(greenref.mark(self.repo, self.first[:9]), self.first)
        self.assertEqual(greenref.read(self.repo), self.first)

    def test_marking_a_sha_the_repo_lacks_is_refused_and_leaves_the_ref(self):
        greenref.mark(self.repo, self.first)
        for bad in ("0123456789abcdef0123456789abcdef01234567", "deadbee"):
            with self.subTest(bad=bad), self.assertRaises(greenref.RefusedSha):
                greenref.mark(self.repo, bad)
            self.assertEqual(greenref.read(self.repo), self.first)

    def test_a_name_that_is_not_a_sha_is_refused(self):
        # a branch, a flag, a ref path or an empty string must never reach git as a revision
        for bad in ("main", "HEAD", "--help", "-x", "", " ", self.first + "\n--x", self.first + "^{tree}", None, 5):
            with self.subTest(bad=bad), self.assertRaises(greenref.RefusedSha):
                greenref.mark(self.repo, bad)
        self.assertIsNone(greenref.read(self.repo))

    def test_a_tree_or_blob_sha_is_refused(self):
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        with self.assertRaises(greenref.RefusedSha):
            greenref.mark(self.repo, tree)
        self.assertIsNone(greenref.read(self.repo))

    def test_read_gives_none_when_the_commit_is_gone(self):
        greenref.mark(self.repo, self.first)
        git(self.repo, "reset", "-q", "--hard", self.first)
        git(self.repo, "reflog", "expire", "--expire=now", "--all")
        git(self.repo, "gc", "-q", "--prune=now")  # the second commit is now unreachable and pruned
        self.assertEqual(greenref.read(self.repo), self.first)
        path = os.path.join(self.repo, ".git", greenref.HOOK_REF)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(self.second + "\n")  # a ref left pointing at a pruned commit
        self.assertIsNone(greenref.read(self.repo))

    def test_no_repo_is_none_for_read_and_refused_for_mark(self):
        elsewhere = os.path.join(self._tmp.name, "plain")
        os.makedirs(elsewhere)
        self.assertIsNone(greenref.read(elsewhere))
        self.assertIsNone(greenref.read(os.path.join(self._tmp.name, "missing")))
        with self.assertRaises(greenref.RefusedSha):
            greenref.mark(elsewhere, self.first)

    def test_the_ref_is_shared_by_a_linked_worktree(self):
        wt = os.path.join(self._tmp.name, "wt")
        git(self.repo, "worktree", "add", "-q", "-b", "side", wt)
        greenref.mark(self.repo, self.first)
        self.assertEqual(greenref.read(wt), self.first)

    def test_a_push_never_carries_it(self):
        remote = os.path.join(self._tmp.name, "remote.git")
        git(self._tmp.name, "init", "-q", "--bare", remote)
        git(self.repo, "tag", "v1")
        greenref.mark(self.repo, self.first)
        git(self.repo, "push", "-q", remote, "main")
        git(self.repo, "push", "-q", "--all", remote)
        git(self.repo, "push", "-q", "--tags", remote)
        pushed = git(remote, "for-each-ref", "--format=%(refname)").split()
        self.assertIn("refs/heads/main", pushed)
        self.assertEqual([r for r in pushed if r.startswith("refs/sampo/")], [])
        # and the ref sits in neither namespace a branch or tag push walks
        local = git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags").split()
        self.assertNotIn(greenref.HOOK_REF, local)


if __name__ == "__main__":
    unittest.main()
