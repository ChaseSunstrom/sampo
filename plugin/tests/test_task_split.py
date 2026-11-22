"""T-5830: fm task split reads the repo's files only. A builder's worktree copy (.claude/worktrees/<lane>/...) is a copy of
the repo, its hook touches land in the ledger under the repo root, and the graph ranks them: none of them may become a
part of a split."""
import json
import os
import subprocess

from helpers import ForemanTestCase

from sampo import core as c

CLUSTERS = {"a": ["alpha_core.py", "alpha_view.py"],
            "b": ["beta_core.py", "beta_view.py"]}
COPY = os.path.join(".claude", "worktrees", "agent-x", "alpha_core.py")  # a lane's copy of a repo file


class Worktree(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        for files in CLUSTERS.values():
            for f in files:  # each file names the others in its cluster: the code graph's 'uses' edges
                others = " ".join(os.path.splitext(o)[0] for o in files if o != f)
                self.write(f, f"# uses {others}\nX = 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.fm("task", "new", "Rework everything", "--type", "FEATURE", "--tier", "L", "--ac", "all fine :: true")

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def git(self, *a):
        subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True)

    def touch(self, rel, task="T-0001"):
        """What the hooks log when an agent edits a file: the absolute path, under the project root."""
        c.log_event(c.find_project(self.repo), "touched", task=task,
                    data={"file": os.path.join(self.repo, rel), "tool": "Edit"})


class Copies(Worktree):
    def test_a_worktree_copy_never_becomes_a_part_file(self):
        for files in CLUSTERS.values():
            for f in files:
                self.touch(f)
        self.write(COPY, "X = 2\n")  # the copy exists and the hooks touched it, as a builder's edits do
        self.touch(COPY)
        data = json.loads(self.fm("task", "split", "T-0001", "--parts", "2", "--dry-run", "--json").stdout)
        files = sorted(f for x in data["children"] for f in x["files"])
        self.assertEqual([f for f in files if ".claude/worktrees/" in f], [], files)
        self.assertEqual(set(files), {f for fs in CLUSTERS.values() for f in fs}, files)
