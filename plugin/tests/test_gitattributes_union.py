"""T-5445: the files every lane appends a line to merge without a conflict, so two lanes adding lines next to each other
never park. On 2026-11-21 16 of 31 parked lanes conflicted only on adjacent MASTER.md lines (one line per module, a new
module's line beside a neighbour's). CHANGELOG.md is a union; T-5472: MASTER.md and docs/map/*.md go through the
key-aware driver (sampo-map), since a union brought back lines trunk had changed or moved."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "plugin", "lib"))
from sampo import mapmerge  # noqa: E402

SHARED = {"CHANGELOG.md": "union", "MASTER.md": "sampo-map", "docs/map/files-lib-1.md": "sampo-map"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


class UnionMerge(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="fm-union-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        shutil.copy(os.path.join(ROOT, ".gitattributes"), os.path.join(self.repo, ".gitattributes"))
        git(self.repo, "init", "-q", "-b", "main")
        mapmerge.ensure(self.repo)

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_every_shared_append_file_has_its_merge(self):
        for rel, how in SHARED.items():
            out = git(self.repo, "check-attr", "merge", "--", rel).stdout.strip()
            self.assertTrue(out.endswith(f": {how}"), f"{rel}: {out}")

    def test_two_lanes_adding_master_lines_beside_the_same_neighbour_merge_clean(self):
        self.write("MASTER.md", "# Map\n- `fma` one\n- `fmb` two\n- `fmc` three\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "checkout", "-qb", "lane")
        self.write("MASTER.md", "# Map\n- `fma` one\n- `fmb` two\n- `fmlane` the lane's module\n- `fmc` three\n")
        git(self.repo, "commit", "-qam", "lane")
        git(self.repo, "checkout", "-q", "main")
        self.write("MASTER.md", "# Map\n- `fma` one\n- `fmb` two\n- `fmtrunk` trunk's module\n- `fmc` three\n")
        git(self.repo, "commit", "-qam", "trunk")
        r = git(self.repo, "merge", "--no-edit", "lane")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.repo, "MASTER.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("`fmlane`", text)
        self.assertIn("`fmtrunk`", text)
        self.assertNotIn("<<<<<<<", text)


if __name__ == "__main__":
    unittest.main()
