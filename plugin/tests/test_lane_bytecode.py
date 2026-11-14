"""T-4655: lanes each keep their own Python bytecode. On 2026-11-14 the lanes held 949 MB of *.pyc on a disk at 100%,
and the merge train was refused. fm tidy lists the bytecode of an idle lane (no process stands in it, no task names it,
no live agent holds it) with its size, and --apply removes only the *.pyc files inside that lane's __pycache__ folders;
doctor's reclaim text names the idle lanes' bytecode. The main checkout's bytecode is never touched.

Safety: every test builds its own scratch repo and lanes beside it; a busy lane is one with a sleeping process standing
in it; no test touches a real lane or the real ~/.claude tree."""
import collections
import json
import os
import subprocess
import sys
import unittest
import unittest.mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import doctor as d
from sampo import lanes

Usage = collections.namedtuple("Usage", "total used free")
PROCFS = os.path.isdir("/proc/self")


def git(root, *a):
    return subprocess.run(["git", "-C", root, *a], check=True, capture_output=True, text=True).stdout.strip()


def proc_start(pid):
    """A process's start time as a Claude Code lock reason writes it (field 22 of /proc/PID/stat)."""
    with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
        return int(f.read().rsplit(")", 1)[1].split()[19])


class LaneBytecodeCase(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fake_home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.fake_home, ".claude"))
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.lane_root = self.repo + ".lanes"

    def tidy(self, *args):
        return json.loads(self.fm("tidy", "--json", *args, env={"HOME": self.fake_home}).stdout)

    def items(self, report):
        return [f for f in report["findings"] if f["kind"] == "lane_bytecode"]

    def lane(self, name):
        path = os.path.join(self.lane_root, name)
        git(self.repo, "worktree", "add", "-q", "-b", f"foreman/{name}", path)
        return os.path.realpath(path)

    def pyc(self, lane, rel, size=50_000):
        full = os.path.join(lane, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(os.urandom(size))
        return full

    def stand_in(self, path):
        """A sleeping process whose working directory is the lane: a process is working in it."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=path,
                                stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        return proc

    def name_lane(self, path, status="planned"):
        """A task that names the lane (its status decides whether the lane is still work)."""
        self.fm("task", "new", "lane owner", "--type", "FIX", "--tier", "S")
        b = c.find_brief(self.p, "T-0001")
        b.meta.update(lane=path, status=status)
        c.save_brief(self.p, b, touch=False)


@unittest.skipUnless(PROCFS, "a busy lane is read from /proc")
class FinderLists(LaneBytecodeCase):
    def test_an_idle_lanes_bytecode_is_listed_with_its_size_and_a_busy_lane_is_left(self):
        idle = self.lane("T-4001")
        busy = self.lane("T-4002")
        a = self.pyc(idle, "plugin/lib/__pycache__/a.cpython-311.pyc", 300_000)
        b = self.pyc(idle, "plugin/lib/__pycache__/b.cpython-311.pyc", 100_000)
        kept = self.pyc(busy, "plugin/lib/__pycache__/a.cpython-311.pyc", 300_000)
        self.stand_in(busy)
        report = self.tidy()
        got = self.items(report)
        self.assertEqual([f["path"] for f in got], [idle], "the busy lane is left out")
        self.assertEqual(got[0]["files"], 2)
        self.assertGreaterEqual(got[0]["bytes"], 400_000)
        self.assertTrue(got[0]["auto"] and got[0]["severity"] == "action", "an APPLY item")
        self.assertIn("T-4001", got[0]["detail"])
        self.assertIn(lanes.human(got[0]["bytes"]), got[0]["detail"], "the size is named")
        for f in (a, b, kept):
            self.assertTrue(os.path.exists(f), "a dry run removes nothing")
        self.assertFalse(report["applied"])

    def test_apply_removes_only_the_idle_lanes_pyc_in_its_pycache_folders(self):
        idle = self.lane("T-4003")
        busy = self.lane("T-4004")
        gone = [self.pyc(idle, "plugin/lib/__pycache__/a.cpython-311.pyc"),
                self.pyc(idle, "plugin/lib/__pycache__/b.cpython-311.pyc")]
        notes = self.pyc(idle, "plugin/lib/__pycache__/notes.txt", 100)
        src = self.pyc(idle, "plugin/lib/x.py", 100)
        outside = self.pyc(idle, "plugin/lib/keep.pyc")  # a .pyc that is not in a __pycache__ folder
        tracked = self.pyc(idle, "plugin/lib/__pycache__/tracked.cpython-311.pyc")
        git(idle, "add", "-f", "plugin/lib/__pycache__/tracked.cpython-311.pyc")
        git(idle, "commit", "-qm", "a tracked bytecode file")
        busy_pyc = self.pyc(busy, "plugin/lib/__pycache__/a.cpython-311.pyc")
        main_pyc = self.pyc(self.repo, "plugin/lib/__pycache__/main.cpython-311.pyc")
        self.stand_in(busy)
        self.tidy("--apply")
        for f in gone:
            self.assertFalse(os.path.exists(f), f"{f} is an idle lane's bytecode and goes")
        for f in (notes, src, outside, tracked, busy_pyc, main_pyc):
            self.assertTrue(os.path.exists(f), f"{f} stays")
        self.assertEqual(self.items(self.tidy("--apply")), [], "a second run finds nothing")
        self.assertEqual(self.items(self.tidy()), [])

    def test_an_open_task_or_a_live_agent_lock_leaves_the_lane_and_a_dead_agents_lock_does_not(self):
        named = self.lane("T-4010")
        self.name_lane(named, status="planned")
        locked = self.lane("T-4011")
        gone = self.lane("T-4012")
        for lane in (named, locked, gone):
            self.pyc(lane, "plugin/lib/__pycache__/a.cpython-311.pyc")
        me = os.getpid()
        git(self.repo, "worktree", "lock", "--reason", f"claude agent agent-live (pid {me} start {proc_start(me)})", locked)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()  # reaped: no process holds this pid any more
        git(self.repo, "worktree", "lock", "--reason", f"claude agent agent-dead (pid {dead.pid} start 1)", gone)
        got = [f["path"] for f in self.items(self.tidy())]
        self.assertEqual(got, [gone], "an open task and a live agent's lock keep their lanes; a dead agent's lock does not")

    def test_a_lane_a_closed_task_names_is_stale_lanes_item_and_its_bytecode_is_not_listed_twice(self):
        done = self.lane("T-4020")
        self.name_lane(done, status="done")
        self.pyc(done, "plugin/lib/__pycache__/a.cpython-311.pyc")
        report = self.tidy()
        self.assertIn("stale_lane", {f["kind"] for f in report["findings"]})
        self.assertEqual(self.items(report), [], "stale_lane removes that lane whole")

    def test_a_finished_lane_is_listed_once_as_a_finished_lane_not_also_as_bytecode(self):
        fin = self.lane("T-4030")
        self.pyc(fin, "plugin/lib/__pycache__/a.cpython-311.pyc")
        git(fin, "add", "-f", "plugin/lib/__pycache__/a.cpython-311.pyc")
        git(fin, "commit", "-qm", "lane work")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "trunk moves on")
        self.assertEqual(self.items(self.tidy()), [], "a finished lane is finished_lane's item alone")

    def test_a_lane_of_an_open_task_whose_builder_returned_is_listed_and_applied_and_a_builder_out_is_not(self):
        # T-4661: the lanes of open tasks waiting for a merge hold most of the bytecode; only a returned builder's lane goes
        waiting = self.lane("T-4090")
        out = self.lane("T-4091")
        relaunched = self.lane("T-4092")  # its builder returned, then a new one was launched and has not started yet
        for title in ("waiting on a merge", "builder still out", "relaunched"):
            self.fm("task", "new", title, "--type", "FIX", "--tier", "S")
        old = "2000-01-01T00:00:00Z"
        for tid, lane, extra in (("T-0001", waiting, {"builder": old, "builder_returned": c.now()}),
                                 ("T-0002", out, {"builder": c.now()}),
                                 ("T-0003", relaunched, {"builder": c.now(), "builder_returned": old})):
            b = c.find_brief(self.p, tid)
            b.meta.update(lane=lane, status="active", **extra)
            c.save_brief(self.p, b, touch=False)
        for lane in (waiting, out, relaunched):
            self.pyc(lane, "plugin/lib/__pycache__/a.cpython-311.pyc", 200_000)
        got = self.items(self.tidy())
        self.assertEqual([f["path"] for f in got], [waiting], "the returned builder's lane is idle; the others are work")
        self.assertIn("builder returned", got[0]["detail"], "the finding says why an open task's lane is idle")
        self.tidy("--apply")
        self.assertFalse(os.path.exists(os.path.join(waiting, "plugin/lib/__pycache__/a.cpython-311.pyc")))
        for lane in (out, relaunched):
            self.assertTrue(os.path.exists(os.path.join(lane, "plugin/lib/__pycache__/a.cpython-311.pyc")),
                            "a builder out, or relaunched after its return, keeps its bytecode")
        self.assertEqual(self.items(self.tidy("--apply")), [], "a second run finds nothing")


class Safety(LaneBytecodeCase):
    def test_symlinks_are_never_followed_and_what_they_point_at_survives(self):
        idle = self.lane("T-4040")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        target = os.path.join(outside, "victim.cpython-311.pyc")
        with open(target, "wb") as f:
            f.write(b"x" * 1000)
        os.makedirs(os.path.join(idle, "plugin", "lib"))
        os.symlink(outside, os.path.join(idle, "plugin", "lib", "__pycache__"))  # a __pycache__ that is a link
        real = self.pyc(idle, "plugin/other/__pycache__/real.cpython-311.pyc")
        os.symlink(target, os.path.join(idle, "plugin", "other", "__pycache__", "link.cpython-311.pyc"))
        self.tidy("--apply")
        self.assertTrue(os.path.exists(target), "a link's target outside the lane is never removed")
        self.assertFalse(os.path.exists(real), "a real .pyc in a real __pycache__ is removed")
        self.assertTrue(os.path.islink(os.path.join(idle, "plugin", "other", "__pycache__", "link.cpython-311.pyc")))

    def test_the_main_checkouts_bytecode_is_untouched_by_the_finder_and_apply(self):
        main_pyc = self.pyc(self.repo, "plugin/lib/__pycache__/main.cpython-311.pyc")
        self.lane("T-4050")
        self.tidy("--apply")
        self.assertTrue(os.path.exists(main_pyc))

    def test_a_git_failure_leaves_the_lane_alone(self):
        idle = self.lane("T-4060")
        pyc = self.pyc(idle, "plugin/lib/__pycache__/a.cpython-311.pyc")
        real = lanes._git

        def refuse(root, *a, **kw):
            if a[:1] == ("ls-files",):
                return subprocess.CompletedProcess(a, 128, "", "fatal: index unreadable")
            return real(root, *a, **kw)
        with unittest.mock.patch.object(lanes, "_git", refuse):
            self.assertEqual(lanes.lane_bytecode(self.p, budget=30), [], "an unreadable index is not guessed at")
        self.assertTrue(os.path.exists(pyc))

    def test_a_spent_budget_lists_nothing_and_removes_nothing(self):
        idle = self.lane("T-4070")
        pyc = self.pyc(idle, "plugin/lib/__pycache__/a.cpython-311.pyc")
        self.assertEqual(lanes.lane_bytecode(self.p, budget=0), [], "an unfinished scan lists no row")
        self.assertTrue(os.path.exists(pyc))


class DoctorNamesIt(LaneBytecodeCase):
    def test_the_reclaim_text_names_the_idle_lanes_bytecode_with_its_size(self):
        idle = self.lane("T-4080")
        self.pyc(idle, "plugin/lib/__pycache__/a.cpython-311.pyc", 300_000)
        r = d.check_disk(self.tmp, lambda _: Usage(900e9, 870e9, 30e9), [(self.p, {})])
        self.assertEqual(r.status, "WARN")
        self.assertRegex(r.detail, r"lane bytecode \(1 idle lane\) \d{3} KB")
        self.assertIn("sampo tidy --apply", r.detail)


if __name__ == "__main__":
    unittest.main()
