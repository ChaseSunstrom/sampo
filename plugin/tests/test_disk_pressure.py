"""T-4543: disk pressure is seen before it breaks work. On 2026-11-12 free space fell from 36 to 32 GB in three hours
(97% used), a test that read the real free disk turned trunk red and a builder's writes failed. sampo doctor warns under
10% or 40 GB free and fails under 5 GB, naming Sampo's own largest reclaimables; the session's Next line carries a fail;
sampo tidy lists a finished lane (clean, its tip already in the trunk, over a day old, no agent in it, not the trunk
tip) with its bytes, largest first, and --apply removes those with git worktree remove, never forced.

Safety: every test builds its own scratch repo; the disk reading is pinned (FOREMAN_DISK_FREE) or injected, never the
machine's; no test touches a worktree outside its own temp folder."""
import collections
import glob
import json
import os
import re
import subprocess
import time
import types
import unittest.mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import doctor as d
from sampo import lanes
from sampo import selfcheck

Usage = collections.namedtuple("Usage", "total used free")
DAY = 86400


def git(root, *a, check=True):
    return subprocess.run(["git", "-C", root, *a], check=check, capture_output=True, text=True).stdout.strip()


def proc_start(pid):
    """A process's start time as a Claude Code lock reason writes it (field 22 of /proc/PID/stat); 0 without procfs."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return 0


class DiskCase(ForemanTestCase):
    """A scratch repo with lanes beside it (<repo>.lanes/NAME), the way fm lane new makes them."""

    def setUp(self):
        super().setUp()
        self.fake_home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.fake_home, ".claude"))
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.lane_root = self.repo + ".lanes"
        self.n = 0

    def tidy(self, *args):
        return json.loads(self.fm("tidy", "--json", *args, env={"HOME": self.fake_home}).stdout)

    def lane(self, name, size=0, merged=False, old=True, dirty=False, branch=None, parent=None):
        """A worktree of the scratch repo: `size` bytes of one committed file; merged: the trunk took its branch (a
        merge commit, so the tip is behind the trunk's, files left out); old: its folder is two days old; dirty: an untracked file."""
        path = os.path.join(parent or self.lane_root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        branch = branch or (f"worktree-{name}" if name.startswith("agent-") else f"foreman/{name}")
        git(self.repo, "worktree", "add", "-q", "-b", branch, path)
        if size:
            with open(os.path.join(path, f"{name}.bin"), "wb") as f:
                f.write(os.urandom(size))
            git(path, "add", "-A")
            git(path, "commit", "-qm", f"work of {name}")
        if merged:
            # -s ours: the branch counts as merged but its file does not reach the trunk, so a lane made later
            # does not carry it and every lane's size is its own
            git(self.repo, "merge", "--no-ff", "-s", "ours", "-qm", f"merge {name}", branch)
        if old:
            self.age(path)
        if dirty:
            with open(os.path.join(path, "scratch.txt"), "w") as f:
                f.write("not committed\n")
        return path

    def age(self, path, days=2):
        """A worktree's age is read from its .git file and its admin folder; push those back."""
        with open(os.path.join(path, ".git")) as f:
            admin = f.read().split("gitdir:", 1)[1].strip()
        then = time.time() - days * DAY
        for f in (os.path.join(path, ".git"), os.path.join(admin, "HEAD"), os.path.join(admin, "index"),
                  os.path.join(admin, "logs", "HEAD")):
            if os.path.exists(f):
                os.utime(f, (then, then))

    def registered(self):
        return [os.path.realpath(ln.split(" ", 1)[1]) for ln in git(self.repo, "worktree", "list", "--porcelain").splitlines()
                if ln.startswith("worktree ")]

    def finished(self, **kw):
        return {os.path.basename(r["path"]): r for r in lanes.finished(self.p, c.load_briefs(self.p), **kw)}


class DoctorThresholds(DiskCase):
    def check(self, free, total=900e9, **kw):
        return d.check_disk(self.tmp, lambda _: Usage(total, total - free, free), [], **kw)

    def test_warns_under_ten_percent_or_forty_gb_and_fails_under_five_gb(self):
        self.assertEqual(self.check(300e9).status, "PASS")
        self.assertEqual(self.check(45e9, 400e9).status, "PASS", "11% and 45 GB free is enough")
        self.assertEqual(self.check(39e9, 400e9).status, "WARN", "under 40 GB")
        self.assertEqual(self.check(45e9, 900e9).status, "WARN", "5% of 900 GB is 45 GB: under 10% free")
        self.assertEqual(self.check(5e9, 900e9).status, "WARN", "5 GB itself is a warning, not a failure")
        self.assertEqual(self.check(4.9e9, 900e9).status, "FAIL")
        self.assertEqual(self.check(1e9, 20e9).status, "FAIL")
        low = self.check(32e9, 1000e9)
        self.assertIn("32 GB free", low.detail)
        self.assertIn("3%", low.detail)

    def test_an_unreadable_disk_is_not_measured_not_a_failure(self):
        def boom(_):
            raise OSError("no such device")
        self.assertEqual(d.check_disk(self.tmp, boom, []).status, "PASS")

    def test_levels_come_from_one_function(self):
        self.assertEqual(lanes.disk_level(Usage(900e9, 0, 300e9)), "ok")
        self.assertEqual(lanes.disk_level(Usage(900e9, 0, 20e9)), "warn")
        self.assertEqual(lanes.disk_level(Usage(900e9, 0, 2e9)), "fail")


class NoRealDisk(DiskCase):
    """The 2026-11-12 incident: a test read the real free disk. Every test's reading is pinned."""

    def test_a_test_sees_a_pinned_roomy_disk_whatever_the_machine_holds(self):
        u = lanes.disk_usage("/")
        self.assertGreaterEqual(u.free, 100e9)
        self.assertEqual(lanes.disk_level(u), "ok")
        self.assertEqual(d.check_disk(self.tmp, None, []).status, "PASS")

    def test_the_pin_takes_free_bytes_and_optionally_a_total(self):
        with unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": "3e9"}):
            self.assertEqual(lanes.disk_usage("/").free, 3e9)
            self.assertEqual(lanes.disk_level(lanes.disk_usage("/")), "fail")
        with unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": "20e9/100e9"}):
            u = lanes.disk_usage("/")
            self.assertEqual((u.free, u.total), (20e9, 100e9))
        with unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": "lots"}), \
                unittest.mock.patch("shutil.disk_usage", lambda p: Usage(50e9, 10e9, 40e9)):
            self.assertEqual(lanes.disk_usage("/").free, 40e9, "an unreadable pin falls back to the disk")

    def test_a_pin_that_is_not_a_usable_reading_is_ignored_and_the_disk_is_read(self):
        for bad in ("nan", "inf", "-5e9", "5e9/nan", "5e9/inf", "5e9/0", "50e9/10e9", "/", "1/2/3", "0x10", "free"):
            with unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": bad}), \
                    unittest.mock.patch("shutil.disk_usage", lambda p: Usage(900e9, 860e9, 40e9)):
                self.assertEqual(lanes.disk_usage("/").free, 40e9, f"{bad!r} falls back to the disk")
                self.assertFalse(lanes.disk_pinned(), bad)
                self.assertEqual(lanes.disk_level(lanes.disk_usage("/")), "warn", bad)

    def test_a_usable_pin_is_reported_as_pinned_and_an_unset_one_is_not(self):
        self.assertTrue(lanes.disk_pinned(), "the test base pins the reading")
        with unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": "3e9"}):
            self.assertTrue(lanes.disk_pinned())
        with unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": ""}), \
                unittest.mock.patch("shutil.disk_usage", lambda p: Usage(50e9, 10e9, 40e9)):
            self.assertFalse(lanes.disk_pinned())
            self.assertEqual(lanes.disk_usage("/").free, 40e9)

    def test_doctor_says_when_the_reading_is_pinned_not_the_disk(self):
        with unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": "3e9/100e9"}):
            r = d.check_disk(self.tmp, None, [])
        self.assertEqual(r.status, "FAIL")
        self.assertIn("pinned by FOREMAN_DISK_FREE", r.detail)
        with unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": "nan"}), \
                unittest.mock.patch("shutil.disk_usage", lambda p: Usage(900e9, 300e9, 600e9)):
            r = d.check_disk(self.tmp, None, [])
        self.assertEqual(r.status, "PASS")
        self.assertNotIn("pinned", r.detail, "a pin that was rejected is not a pin")

    def test_no_test_file_reads_the_disk_itself(self):
        here = os.path.dirname(os.path.abspath(__file__))
        bad = []
        for f in sorted(glob.glob(os.path.join(here, "test_*.py"))):
            if os.path.basename(f) == os.path.basename(__file__):
                continue
            with open(f, encoding="utf-8") as h:
                for n, ln in enumerate(h, 1):
                    if re.search(r"\b(?:shutil\.)?disk_usage\(|\bstatvfs\(", ln) and "lambda" not in ln:
                        bad.append(f"{os.path.basename(f)}:{n}")
        self.assertEqual(bad, [], "a test that reads the real free disk goes red when the disk fills (T-4543)")


class FinishedLanes(DiskCase):
    def test_a_clean_merged_old_lane_is_finished_and_the_rest_are_not(self):
        done = self.lane("T-1001", size=300_000, merged=True)
        dirty = self.lane("T-1002", size=1000, merged=True, dirty=True)
        unmerged = self.lane("T-1003", size=1000)
        young = self.lane("T-1004", size=1000, merged=True, old=False)
        user_locked = self.lane("T-1005", size=1000, merged=True)
        git(self.repo, "worktree", "lock", "--reason", "keep this one", user_locked)
        at_tip = self.lane("T-1006")  # last: the trunk hasn't moved since, so its tip is the trunk's
        got = self.finished()
        self.assertEqual(sorted(got), ["T-1001"], f"only the first: {sorted(got)}")
        self.assertEqual(os.path.realpath(got["T-1001"]["path"]), os.path.realpath(done))
        self.assertGreaterEqual(got["T-1001"]["bytes"], 300_000)
        for gone in (dirty, unmerged, young, user_locked, at_tip):
            self.assertTrue(os.path.isdir(gone))

    def test_a_just_started_builder_at_the_trunk_tip_is_never_finished(self):
        # the failure the brief names: tip equal to the trunk's and clean, taken for finished
        fresh = self.lane("agent-fresh", old=False, parent=os.path.join(self.repo, ".claude", "worktrees"))
        self.assertEqual(self.finished(), {})
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "the trunk moves on")
        self.assertEqual(self.finished(), {}, "a day old is the bar: its folder is not")
        self.age(fresh)
        self.assertEqual(sorted(self.finished()), ["agent-fresh"], "clean, behind the trunk, a day old: finished")

    def test_a_lane_a_process_stands_in_is_not_finished(self):
        self.lane("T-1010", size=1000, merged=True)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "later")
        self.assertEqual(sorted(self.finished()), ["T-1010"])
        with unittest.mock.patch.object(lanes, "_busy_dirs", lambda: [os.path.join(self.lane_root, "T-1010", "src")]):
            self.assertEqual(self.finished(), {}, "an agent's shell is in it")

    def test_a_lane_an_open_task_names_is_not_finished_and_a_closed_ones_is_left_to_the_stale_rule(self):
        open_lane = self.lane("T-1020", size=1000, merged=True)
        closed_lane = self.lane("T-1021", size=1000, merged=True)
        orphan = self.lane("T-1022", size=1000, merged=True)
        for tid, lane, status in (("T-0001", open_lane, "active"), ("T-0002", closed_lane, "done")):
            self.fm("task", "new", f"task {tid}", "--type", "FIX", "--tier", "S")
            b = c.find_brief(self.p, tid)
            b.meta.update(lane=lane, status=status)
            c.save_brief(self.p, b, touch=False)
        got = self.finished()
        self.assertEqual(sorted(got), ["T-1021", "T-1022"], "open task: kept; closed task: reported with its kind")
        self.assertEqual(got["T-1021"]["kind"], "done_lane")
        self.assertEqual(got["T-1022"]["kind"], "no_task_lane")
        self.assertTrue(os.path.isdir(open_lane) and os.path.isdir(orphan))

    def test_a_builder_whose_process_is_alive_is_not_finished(self):
        lane = self.lane("T-1030", size=1000, merged=True)
        self.fm("task", "new", "t", "--type", "FIX", "--tier", "S")
        b = c.find_brief(self.p, "T-0001")
        b.meta.update(lane=lane, status="blocked", builder_proc="x|y|1|1")
        c.save_brief(self.p, b, touch=False)
        self.assertEqual(self.finished(), {}, "any open task naming it keeps it")

    def test_only_sampos_own_lane_folders_count(self):
        elsewhere = os.path.join(self.tmp, "somebody-elses-checkout")
        git(self.repo, "worktree", "add", "-q", "-b", "other", elsewhere)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "later")
        self.age(elsewhere)
        self.assertEqual(self.finished(), {}, "not under <repo>.lanes or <repo>/.claude/worktrees")
        self.assertEqual(lanes.reclaimables(self.p), [], "not named to the doctor either: it is not Sampo's")
        self.assertTrue(os.path.isdir(elsewhere))

    def test_sizing_stops_at_its_budget_and_a_lane_without_a_size_sorts_last(self):
        self.lane("T-1040", size=500_000, merged=True)
        self.lane("T-1041", size=100_000, merged=True)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "later")
        with unittest.mock.patch.object(lanes, "dir_bytes", lambda path, deadline=None: None):  # the clock ran out in it
            rows = lanes.finished(self.p, c.load_briefs(self.p))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["bytes"] is None for r in rows), "no time to measure: unsized, still listed")
        sized = lanes.finished(self.p, c.load_briefs(self.p))
        self.assertEqual([os.path.basename(r["path"]) for r in sized], ["T-1040", "T-1041"], "largest first")

    def test_one_deadline_bounds_the_whole_scan_and_only_verified_rows_come_back(self):
        for n in range(4):
            self.lane(f"T-106{n}", size=1000, merged=True)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "later")
        calls, real = [], lanes._git
        full = lanes.finished(self.p, c.load_briefs(self.p))
        self.assertEqual(len(full), 4)
        clock = [0.0]

        def slow(root, *a, **kw):  # each git call "takes" 10 s of the monotonic clock
            calls.append(a[:2])
            clock[0] += 10
            return real(root, *a, **kw)
        fake = types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time, sleep=time.sleep)
        with unittest.mock.patch.object(lanes, "_git", slow), unittest.mock.patch.object(lanes, "time", fake):
            part = lanes.finished(self.p, c.load_briefs(self.p), budget=65)
        # rev-parse, worktree list, four merge-bases (60 s: 5 left), then one status (70 s) and nothing after it
        self.assertLessEqual(len(calls), 7, f"it stopped at the deadline, not after every lane: {calls}")
        self.assertEqual(len(part), 1, "the one lane whose status was read; the other three are unverified")
        self.assertIn(part[0]["path"], {f["path"] for f in full})
        self.assertIsNone(part[0]["bytes"], "no time left to size it")
        # past the deadline before the first call: nothing is run at all
        calls.clear()
        with unittest.mock.patch.object(lanes, "_git", slow):
            self.assertEqual(lanes.finished(self.p, c.load_briefs(self.p), budget=0), [])
        self.assertEqual(calls, [])


class TidyLists(DiskCase):
    def setUp(self):
        super().setUp()
        self.big = self.lane("T-2001", size=1_500_000, merged=True)
        self.small = self.lane("T-2002", size=200_000, merged=True)
        self.dirty = self.lane("T-2003", size=1000, merged=True, dirty=True)
        self.unmerged = self.lane("T-2004", size=1000)
        self.young = self.lane("T-2005", size=1000, merged=True, old=False)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "later")
        self.tip = self.lane("T-2006")  # at the trunk's tip, made after its last commit

    def items(self, report):
        return [f for f in report["findings"] if f["kind"] == "finished_lane"]

    def test_lists_finished_lanes_with_bytes_largest_first_and_changes_nothing(self):
        report = self.tidy()
        got = self.items(report)
        self.assertEqual([os.path.basename(f["path"]) for f in got], ["T-2001", "T-2002"])
        self.assertTrue(all(f["severity"] == "action" and f["auto"] for f in got), "an APPLY item")
        self.assertGreaterEqual(got[0]["bytes"], 1_500_000)
        self.assertGreater(got[0]["bytes"], got[1]["bytes"])
        for path in (self.big, self.small, self.dirty, self.unmerged, self.young, self.tip):
            self.assertTrue(os.path.isdir(path), "a dry run removes nothing")
        self.assertFalse(report["applied"])

    def test_the_printed_list_names_the_size_and_sorts_by_it(self):
        out = self.fm("tidy", env={"HOME": self.fake_home}).stdout
        lines = [ln for ln in out.splitlines() if "finished_lane" in ln]
        self.assertEqual(len(lines), 2, out)
        self.assertIn("T-2001", lines[0])
        self.assertRegex(lines[0], r"1\.[5-6] MB")
        self.assertIn("T-2002", lines[1])
        self.assertRegex(lines[1], r"\d+ KB|0\.\d MB")
        self.assertLess(out.index("T-2001"), out.index("T-2002"))

    def test_findings_with_bytes_sort_largest_first_within_a_severity(self):
        out = self.fm("tidy", env={"HOME": self.fake_home}).stdout.splitlines()
        action = [ln for ln in out if ln.startswith("APPLY")]
        sizes = [float(m.group(1)) * {"KB": 1e3, "MB": 1e6, "GB": 1e9, "B": 1}[m.group(2)]
                 for ln in action if (m := re.search(r"\((\d+(?:\.\d)?) (B|KB|MB|GB)\)", ln))]
        self.assertGreaterEqual(len(sizes), 2)
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_apply_removes_only_the_listed_lanes_without_force_and_a_second_run_does_nothing(self):
        self.tidy("--apply")
        here = self.registered()
        for gone in (self.big, self.small):
            self.assertFalse(os.path.exists(gone))
            self.assertNotIn(os.path.realpath(gone), here)
        for kept in (self.dirty, self.unmerged, self.young, self.tip):
            self.assertTrue(os.path.isdir(kept), f"{kept} is not finished and stays")
            self.assertIn(os.path.realpath(kept), here)
        self.assertTrue(os.path.exists(os.path.join(self.dirty, "scratch.txt")), "uncommitted work is never touched")
        branches = git(self.repo, "branch", "--list", "foreman/*")
        self.assertNotIn("T-2001", branches, "a merged lane's branch goes with it (git branch -d)")
        self.assertIn("T-2004", branches, "an unmerged branch is never deleted")
        again = self.tidy("--apply")
        self.assertEqual(self.items(again), [])
        self.assertTrue(os.path.isdir(self.unmerged))

    def test_a_removal_git_refuses_is_reported_not_forced(self):
        from sampo import tidy
        real, seen = lanes._git, []

        def refuse(root, *a, **kw):
            seen.append(a)
            if a[:2] == ("worktree", "remove"):
                return subprocess.CompletedProcess(a, 1, "", "fatal: contains modified or untracked files")
            return real(root, *a, **kw)
        acts = []
        with unittest.mock.patch.object(lanes, "_git", refuse):
            out = lanes.finished_findings(self.p, c.load_briefs(self.p), True, acts, tidy.finding)
        self.assertEqual(acts, [])
        self.assertEqual([f["severity"] for f in out], ["warn", "warn"])
        self.assertTrue(all("contains modified" in f["fix"] and not f["auto"] for f in out))
        self.assertTrue(os.path.isdir(self.big))
        self.assertFalse(any("--force" in a or "-f" in a for a in seen), "git itself refuses; Sampo never forces")


class ApplySafety(DiskCase):
    """What the apply path must leave alone: a live agent's lock, an operation in progress, a branch with its own name."""

    def test_a_claude_agent_lock_is_lifted_only_when_its_agent_is_dead_and_a_user_lock_never(self):
        scratch = os.path.join(self.repo, ".claude", "worktrees")
        dead = subprocess.Popen(["true"])
        dead.wait()
        alive = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(alive.wait)  # cleanups run last-in first: kill, then wait
        self.addCleanup(alive.kill)
        start = proc_start(alive.pid)
        cases = {
            "agent-dead": f"claude agent agent-dead (pid {dead.pid} start 1)",
            "agent-alive": f"claude agent agent-alive (pid {alive.pid} start {start})",
            "agent-nopid": "claude agent agent-nopid",
            "agent-user": "mine, keep",
        }
        if start:  # procfs: a pid that now has another start time was reused, its agent is gone
            cases["agent-reused"] = f"claude agent agent-reused (pid {alive.pid} start {start + 7})"
        path = {}
        for name, reason in cases.items():
            path[name] = self.lane(name, size=1000, merged=True, parent=scratch)
            git(self.repo, "worktree", "lock", "--reason", reason, path[name])
        # the reason's real shape: git lists it verbatim, and that is what the code reads
        listed = git(self.repo, "worktree", "list", "--porcelain")
        self.assertIn(f"locked claude agent agent-dead (pid {dead.pid} start 1)", listed.splitlines())
        self.assertEqual(self.finished_names(), sorted(n for n in ("agent-dead", "agent-reused") if n in cases),
                         "listed: only the lanes whose agent is gone")
        self.tidy("--apply")
        self.assertFalse(os.path.exists(path["agent-dead"]), "its agent is dead: the lock is lifted and the lane goes")
        self.assertTrue(os.path.isdir(path["agent-alive"]), "its agent is running: the lock stands")
        self.assertTrue(os.path.isdir(path["agent-nopid"]), "no readable pid: skipped, as a user's lock is")
        self.assertTrue(os.path.isdir(path["agent-user"]))
        if start:
            self.assertFalse(os.path.exists(path["agent-reused"]), "the pid is another process now")

    def finished_names(self):
        return sorted(self.finished())

    def test_agent_lock_dead_reads_the_real_reason(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.assertTrue(lanes.agent_lock_dead(f"claude agent agent-x (pid {dead.pid} start 5)"))
        self.assertFalse(lanes.agent_lock_dead("claude agent agent-x"))
        self.assertFalse(lanes.agent_lock_dead("claude agent agent-x (pid abc start 5)"))
        self.assertFalse(lanes.agent_lock_dead(f"someone's (pid {dead.pid} start 5)"), "only a Claude agent's lock")
        self.assertFalse(lanes.agent_lock_dead(None))
        self.assertFalse(lanes.agent_lock_dead("claude agent agent-x (pid 0 start 5)"))
        me = proc_start(os.getpid())
        if me:
            self.assertFalse(lanes.agent_lock_dead(f"claude agent agent-x (pid {os.getpid()} start {me})"))

    def test_an_agent_that_woke_between_the_scan_and_the_removal_keeps_its_lane(self):
        locked = self.lane("agent-ccc", size=1000, merged=True, parent=os.path.join(self.repo, ".claude", "worktrees"))
        dead = subprocess.Popen(["true"])
        dead.wait()
        git(self.repo, "worktree", "lock", "--reason", f"claude agent agent-ccc (pid {dead.pid} start 1)", locked)
        from sampo import tidy
        with unittest.mock.patch.object(lanes, "agent_lock_dead", side_effect=[True, False]):
            out = lanes.finished_findings(self.p, c.load_briefs(self.p), True, [], tidy.finding)
        self.assertEqual([f["severity"] for f in out if "agent-ccc" in f["detail"]], ["warn"])
        self.assertTrue(os.path.isdir(locked))
        self.assertIn("locked", git(self.repo, "worktree", "list", "--porcelain"), "the lock was never lifted")

    def test_a_failed_relock_after_a_refused_removal_is_reported(self):
        from sampo import tidy
        locked = self.lane("agent-ddd", size=1000, merged=True, parent=os.path.join(self.repo, ".claude", "worktrees"))
        dead = subprocess.Popen(["true"])
        dead.wait()
        git(self.repo, "worktree", "lock", "--reason", f"claude agent agent-ddd (pid {dead.pid} start 1)", locked)
        real = lanes._git

        def refuse(root, *a, **kw):
            if a[:2] == ("worktree", "remove"):
                return subprocess.CompletedProcess(a, 1, "", "fatal: refused")
            if a[:2] == ("worktree", "lock"):
                return subprocess.CompletedProcess(a, 128, "", "fatal: cannot lock")
            return real(root, *a, **kw)
        with unittest.mock.patch.object(lanes, "_git", refuse):
            out = lanes.finished_findings(self.p, c.load_briefs(self.p), True, [], tidy.finding)
        got = [f for f in out if "agent-ddd" in f["detail"]]
        self.assertEqual([f["severity"] for f in got], ["warn"])
        self.assertIn("refused", got[0]["fix"])
        self.assertIn("lock back failed", got[0]["fix"])
        self.assertIn("cannot lock", got[0]["fix"])

    def test_a_worktree_in_the_middle_of_a_rebase_merge_cherry_pick_or_bisect_is_skipped(self):
        for n, marker in enumerate(("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "BISECT_LOG")):
            path = self.lane(f"T-50{n}", size=1000, merged=True)
            admin = open(os.path.join(path, ".git")).read().split("gitdir:", 1)[1].strip()
            target = os.path.join(admin, marker)
            if marker.startswith("rebase"):
                os.makedirs(target)
            else:
                open(target, "w").write("x\n")
            self.age(path)
        free = self.lane("T-509", size=1000, merged=True)
        self.assertEqual(self.finished_names(), ["T-509"], "only the one with no operation in progress")
        self.assertTrue(os.path.isdir(free))

    def test_a_branch_goes_with_its_lane_only_when_it_is_a_lanes_own_name(self):
        scratch = os.path.join(self.repo, ".claude", "worktrees")
        self.lane("T-510", size=1000, merged=True)  # foreman/T-510
        self.lane("agent-eee", size=1000, merged=True, parent=scratch)  # worktree-agent-eee
        self.lane("T-511", size=1000, merged=True, branch="release-2.0")  # a branch with its own meaning
        self.lane("T-512", size=1000, merged=True, branch="feature/mine")
        self.lane("T-513", size=1000, merged=True, branch="draft/x")
        self.tidy("--apply")
        branches = git(self.repo, "branch", "--list").split()
        for gone in ("foreman/T-510", "worktree-agent-eee", "draft/x"):
            self.assertNotIn(gone, branches)
        for kept in ("release-2.0", "feature/mine"):
            self.assertIn(kept, branches, "not a lane's own branch name: the worktree goes, the branch stays")


class DoctorNamesReclaimables(DiskCase):
    def test_a_warn_names_the_largest_reclaimables_with_sizes(self):
        self.lane("T-3001", size=1_500_000, merged=True)  # no task names it: a no-task lane
        done = self.lane("T-3002", size=600_000, merged=True)
        self.lane("T-3003", size=1000, merged=True, dirty=True)  # not removable: not named
        self.fm("task", "new", "closed", "--type", "FIX", "--tier", "S")
        b = c.find_brief(self.p, "T-0001")
        b.meta.update(lane=done, status="done")
        c.save_brief(self.p, b, touch=False)
        out = os.path.join(c.state_dir(), "outputs")
        os.makedirs(out)
        old = os.path.join(out, "old.txt")
        with open(old, "wb") as f:
            f.write(os.urandom(300_000))
        os.utime(old, (time.time() - 30 * DAY,) * 2)
        r = d.check_disk(self.tmp, lambda _: Usage(900e9, 870e9, 30e9), [(self.p, {})])
        self.assertEqual(r.status, "WARN")
        self.assertIn("30 GB free", r.detail)
        for want in ("T-3001", "T-3002", "no-task lane", "done lane", "old output"):
            self.assertIn(want, r.detail)
        self.assertRegex(r.detail, r"no-task lane T-3001 1\.[5-6] MB")
        self.assertRegex(r.detail, r"done lane T-3002 6\d\d KB")
        self.assertNotIn("T-3003", r.detail, "a dirty lane is not reclaimable")
        self.assertLess(r.detail.index("T-3001"), r.detail.index("T-3002"), "largest first")
        self.assertIn("sampo tidy --apply", r.detail)

    def test_a_fail_names_them_too(self):
        self.lane("T-3010", size=800_000, merged=True)
        r = d.check_disk(self.tmp, lambda _: Usage(900e9, 897e9, 3e9), [(self.p, {})])
        self.assertEqual(r.status, "FAIL")
        self.assertIn("T-3010", r.detail)

    def test_a_pass_measures_nothing(self):
        with unittest.mock.patch.object(lanes, "reclaimables", side_effect=AssertionError("sized on a roomy disk")):
            self.assertEqual(d.check_disk(self.tmp, lambda _: Usage(900e9, 300e9, 600e9), [(self.p, {})]).status, "PASS")

    def test_a_project_that_cannot_be_read_costs_the_check_nothing(self):
        with unittest.mock.patch.object(lanes, "reclaimables", side_effect=OSError("gone")):
            r = d.check_disk(self.tmp, lambda _: Usage(900e9, 893e9, 7e9), [(self.p, {})])
        self.assertEqual(r.status, "WARN")


class NextLineCarriesAFail(DiskCase):
    def pin(self, free):
        return unittest.mock.patch.dict(os.environ, {"FOREMAN_DISK_FREE": free})

    def test_a_fail_is_a_self_check_line_with_the_reclaimable_bytes(self):
        self.lane("T-4001", size=700_000, merged=True)
        with self.pin("3e9"):
            hit = selfcheck.disk_low(self.p, c.load_briefs(self.p), time.time())
        self.assertTrue(hit)
        for want in ("3 GB free", "sampo tidy --apply", "1 finished lane"):
            self.assertIn(want, hit)
        self.assertTrue(hit.tell, "a fail reaches the user once a day")

    def test_the_hook_scans_at_most_once_per_window(self):
        self.lane("T-4002", size=400_000, merged=True)
        briefs, now = c.load_briefs(self.p), time.time()
        real = lanes.reclaimables
        with self.pin("3e9"), unittest.mock.patch.object(lanes, "reclaimables", wraps=real) as scan:
            first = selfcheck.disk_low(self.p, briefs, now)
            again = selfcheck.disk_low(self.p, briefs, now + 30)
            self.assertEqual(scan.call_count, 1, "the second call inside the window reuses the stamp")
            self.assertEqual(str(first), str(again))
            self.assertIn("1 finished lane", again)
            selfcheck.disk_low(self.p, briefs, now + selfcheck.CACHE_S + 1)
            self.assertEqual(scan.call_count, 2, "a new window scans again")

    def test_a_stamp_that_cannot_be_written_means_no_scan_and_the_bare_reading(self):
        self.lane("T-4003", size=400_000, merged=True)
        briefs, now = c.load_briefs(self.p), time.time()
        with self.pin("3e9"), unittest.mock.patch.object(c, "write_atomic", side_effect=OSError("no space left")), \
                unittest.mock.patch.object(lanes, "reclaimables", side_effect=AssertionError("scanned with no stamp")):
            for k in range(3):
                hit = selfcheck.disk_low(self.p, briefs, now + k)
                self.assertTrue(hit)
                self.assertTrue(hit.tell)
                self.assertIn("3 GB free", hit)
                self.assertNotIn("finished lane", hit, "nothing was measured, so nothing is claimed")

    def test_a_damaged_stamp_is_rewritten_not_trusted(self):
        with open(os.path.join(self.p.dir, "disk_scan.json"), "w") as f:
            f.write("{not json")
        with self.pin("3e9"):
            self.assertTrue(selfcheck.disk_low(self.p, c.load_briefs(self.p), time.time()))
        with open(os.path.join(self.p.dir, "disk_scan.json")) as f:
            self.assertIn("at", json.load(f))

    def test_a_warn_or_a_roomy_disk_adds_no_line(self):
        for free in ("20e9", "300e9"):
            with self.pin(free):
                self.assertFalse(selfcheck.disk_low(self.p, c.load_briefs(self.p), time.time()), free)

    def test_next_carries_it(self):
        with self.pin("3e9"):
            action = c.next_for(self.p)[2]
        self.assertIn("self-check:", action)
        self.assertIn("3 GB free", action)

    def test_a_roomy_disk_leaves_next_alone(self):
        self.assertNotIn("GB free", c.next_for(self.p)[2])

    def test_the_check_is_in_the_set(self):
        self.assertIn(selfcheck.disk_low, selfcheck.CHECKS)


if __name__ == "__main__":
    import unittest
    unittest.main()
