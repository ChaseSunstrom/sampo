"""T-3585: finished_lanes asked git three questions per lane (~257 processes at 83 lanes, ~2.5 s of git CPU) each time
its 60 s cache expired. One worktree list and one log walk now answer every lane, a lane's answer is kept on its tip and
main's, and a clean tree on its tip and index: the same ids as the per-lane version, and an unchanged lane costs no git
process."""
import os
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

import helpers  # noqa: F401 (puts plugin/lib on sys.path)

from sampo import core as c

OLD = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00"}


def git(cwd, *args, env=None):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout


def per_lane(p, briefs, quiet_min):
    """finished_lanes before T-3585, uncached: three git calls per lane."""
    base = c._git(c.main_worktree(p.root) or p.root, "rev-parse", "HEAD").strip()
    out = []
    for b in briefs:
        path = b.meta["lane"]
        last = c._git(path, "log", "-1", "--format=%ct").strip()
        if last.isdigit() and time.time() - int(last) >= quiet_min * 60 \
                and c._git(path, "rev-list", "--count", "HEAD", "--not", base).strip() not in ("", "0") \
                and not c._git(path, "status", "--porcelain", fail="?").strip():
            out.append(b.id)
    return out


class FinishedLanesBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())  # git names real paths (macOS: /var is /private/var)
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])
        os.environ["FM_STALL_CACHE_S"] = "60"
        self.addCleanup(os.environ.pop, "FM_STALL_CACHE_S", None)
        main = self.main = os.path.join(self.tmp, "main")
        os.makedirs(main)
        git(main, "init", "-q", "-b", "main")
        for k, v in (("user.email", "alice@devbox.example.test"), ("user.name", "alice")):
            git(main, "config", k, v)
        git(main, "commit", "-q", "--allow-empty", "-m", "root", env=OLD)
        self.paths = {}

        def lane(name, when=OLD, detach=False):
            path = self.paths[name] = os.path.join(self.tmp, "lanes", name)
            git(main, "worktree", "add", "-q", *(["--detach", path] if detach else ["-b", f"lane/{name}", path]))
            if when is not None:
                with open(os.path.join(path, f"{name}.py"), "w") as f:
                    f.write("x = 1\n")
                git(path, "add", "-A")
                git(path, "commit", "-q", "-m", name, env=when)
            return path

        lane("done")
        with open(os.path.join(lane("dirty"), "wip.py"), "w") as f:
            f.write("y = 2\n")
        lane("young", when={})
        lane("merged")
        git(main, "merge", "-q", "--no-ff", "-m", "merge", "lane/merged", env=OLD)
        lane("empty", when=None)
        with open(os.path.join(lane("staged"), "staged.py"), "w") as f:
            f.write("z = 3\n")
        git(self.paths["staged"], "add", "staged.py")
        lane("detached", detach=True)
        clone = self.paths["clone"] = os.path.join(self.tmp, "clone")  # not in main's worktree list: asked directly
        git(self.tmp, "clone", "-q", main, clone)
        for k, v in (("user.email", "alice@devbox.example.test"), ("user.name", "alice")):
            git(clone, "config", k, v)
        git(clone, "commit", "-q", "--allow-empty", "-m", "clone work", env=OLD)
        self.paths = {k: os.path.realpath(v) for k, v in self.paths.items()}
        state = os.path.join(self.tmp, "state")
        os.makedirs(state)
        self.p = types.SimpleNamespace(lane=None, root=main, dir=state)
        self.briefs = [types.SimpleNamespace(id=k, status="active", meta={"lane": v}) for k, v in self.paths.items()]

    def calls(self, quiet_min=30):
        """(ids, [(git subcommand, -C dir)]) of one finished_lanes call."""
        seen, real = [], subprocess.run

        def run(cmd, *a, **k):
            if cmd[:1] == ["git"]:
                seen.append((cmd[cmd.index("-C") + 2], os.path.realpath(cmd[cmd.index("-C") + 1])))
            return real(cmd, *a, **k)
        with mock.patch("subprocess.run", run):
            return sorted(c.finished_lanes(self.p, self.briefs, quiet_min=quiet_min)), seen

    def expire(self):
        old = time.time() - 3600
        os.utime(os.path.join(self.p.dir, "finished.json"), (old, old))

    def test_the_same_ids_as_the_per_lane_calls(self):
        os.environ["FM_STALL_CACHE_S"] = "0"
        for quiet in (30, 0):
            want = sorted(per_lane(self.p, self.briefs, quiet))
            self.assertEqual(self.calls(quiet)[0], want, f"quiet_min={quiet}")
        self.assertEqual(sorted(per_lane(self.p, self.briefs, 30)), ["clone", "detached", "done"])
        self.assertEqual(sorted(per_lane(self.p, self.briefs, 0)), ["clone", "detached", "done", "young"])

    def test_a_refresh_asks_git_nothing_about_an_unchanged_lane(self):
        ids, cold = self.calls()
        self.assertEqual(ids, ["clone", "detached", "done"])
        listed = [x for x in cold if x[1] != self.paths["clone"]]
        self.assertEqual(sum(1 for cmd, _ in listed if cmd != "status"), 3, "rev-parse, worktree list, one log walk")
        self.expire()
        ids, warm = self.calls()
        self.assertEqual(ids, ["clone", "detached", "done"])
        self.assertEqual(sorted(x for x in warm if x[1] != self.paths["clone"]),
                         sorted([("rev-parse", self.main), ("worktree", self.main), ("status", self.paths["dirty"]),
                                 ("status", self.paths["staged"])]), "only a dirty lane's tree is read again")
        self.assertEqual(self.calls(0)[0], ["clone", "detached", "done", "young"], "the cache holds for any quiet_min")

    def test_a_new_tip_main_or_index_is_read_again(self):
        self.calls()
        with open(os.path.join(self.paths["done"], "more.py"), "w") as f:
            f.write("w = 4\n")
        git(self.paths["done"], "add", "more.py")  # the index changes, the tip doesn't
        self.expire()
        self.assertEqual(self.calls()[0], ["clone", "detached"], "a staged file: the clean answer went with the index")
        git(self.paths["detached"], "commit", "-q", "--allow-empty", "-m", "now")
        self.expire()
        self.assertEqual(self.calls()[0], ["clone"], "a new tip, committed just now: not quiet yet")
        git(self.main, "merge", "-q", "--no-ff", "-m", "merge", "lane/young", env=OLD)
        self.assertEqual(self.calls(0)[0], ["detached"], "main moved: the merged lane waits on nothing (nor the clone, "
                                                         "which lacks main's new commit)")
        self.assertEqual(self.calls(0)[0], sorted(per_lane(self.p, self.briefs, 0)))


if __name__ == "__main__":
    unittest.main()
