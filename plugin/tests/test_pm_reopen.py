"""T-5981: a merge that leaves trunk by a revert reopens its done task. T-3051 was reverted at 10:05 (red after merge) and
stayed done, so its feature (moonscale._kept) silently left trunk and T-5179 landed on top of it. Every path that takes a
merge off trunk reopens the task: the verify's own revert, a hand revert seen by the land's check (red or conflict record),
one seen before a revert (_pm_settled) and one seen while covering a pending merge. A revert that was itself reverted
(a hand re-land) leaves the feature on trunk, so it reopens nothing. All in temp repos with shell stand-ins for the suite."""
import json
import os
import subprocess
import unittest.mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c
from sampo import lanes as slanes

first = lambda text: json.JSONDecoder().raw_decode(text)[0]


class PmReopen(ForemanTestCase):
    # red while bad.txt holds a line "a": the merge that brings it is the red one
    SUITE = "grep -qx a bad.txt 2>/dev/null && echo 'FAIL: t (m.B.t)' && exit 1; exit 0"

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        for k in ("FM_POSTMERGE_CMD", "FM_POSTMERGE_RERUN"):
            os.environ[k] = self.SUITE
            self.addCleanup(os.environ.pop, k, None)
        self.tid = first(self.fm("task", "new", "Landed", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        self.status("done")

    def status(self, st, tid=None):
        cli.mutate(self.p, tid or self.tid, lambda x: x.meta.update(status=st), None)

    def brief(self):
        return c.find_brief(self.p, self.tid)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True).stdout.strip()

    def land(self, name, text=None, record=True):
        self.git("checkout", "-qb", f"b-{name}")
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text or name)
        self.git("add", "-A")
        self.git("commit", "-qm", name)
        self.git("checkout", "-q", "-")
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {name}", f"b-{name}")
        sha = self.git("rev-parse", "HEAD")
        if record:
            self.record(sha)
        return sha

    def record(self, sha):
        with unittest.mock.patch.object(slanes, "pm_spawn"):
            slanes.pm_record(self.p, sha, self.tid, self.repo)

    def rec(self, sha):
        return slanes.pm_records(self.p)[sha]

    def test_a_verify_revert_of_a_done_tasks_merge_reopens_it_with_the_reason(self):
        sha = self.land("bad.txt", "a\n")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sha)["state"], "reverted")
        b = self.brief()
        self.assertEqual(b.status, "planned", "the feature left trunk: the task is open again")
        self.assertIn(sha[:9], b.section("Log"))
        self.assertIn("m.B", b.section("Log"))
        self.assertTrue(c.ledger_events(self.p, ["lane_reopened_by_revert"]))

    def test_a_task_that_is_not_done_keeps_its_status_through_a_revert(self):
        self.status("planned")
        sha = self.land("bad.txt", "a\n")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sha)["state"], "reverted")
        self.assertEqual(self.brief().status, "planned")
        self.assertNotIn("reopened", self.brief().section("Log"))

    def test_a_task_that_landed_again_since_stays_done(self):
        old = self.land("bad.txt", "a\n")
        self.land("other.txt")  # the same task's later landing, still on trunk
        slanes.pm_set(self.p, old, state="red", classes=["m.B"])
        self.assertTrue(slanes._pm_revert(self.p, self.repo, old))
        self.assertEqual(self.rec(old)["state"], "reverted")
        self.assertEqual(self.brief().status, "done")

    def test_a_hand_revert_seen_by_the_lands_check_reopens_a_red_or_a_conflict_record(self):
        for state in ("red", "conflict"):
            with self.subTest(state=state):
                self.status("done")
                sha = self.land(f"{state}.txt")
                slanes.pm_set(self.p, sha, state=state, classes=["m.B"])
                self.git("revert", "-m", "1", "--no-edit", sha)
                slanes.pm_wait(self.p, self.repo)  # no PolicyError: the hand revert settles the record
                self.assertEqual(self.rec(sha)["state"], "reverted")
                self.assertEqual(self.brief().status, "planned")
                self.assertIn("reverted by hand", self.brief().section("Log"))
                self.assertFalse(self.rec(sha).get("reopen_due"), "done once")

    def test_a_hand_revert_seen_before_the_verifys_own_reopens_it(self):
        sha = self.land("bad.txt", "a\n")
        slanes.pm_set(self.p, sha, state="red", classes=["m.B"])
        self.git("revert", "-m", "1", "--no-edit", sha)
        self.assertTrue(slanes._pm_revert(self.p, self.repo, sha))
        self.assertEqual(self.rec(sha)["state"], "reverted")
        self.assertEqual(self.brief().status, "planned")

    def test_a_pending_merge_taken_off_trunk_by_hand_reopens_its_task(self):
        gone = self.land("one.txt")
        self.git("revert", "-m", "1", "--no-edit", gone)
        newest = self.land("two.txt")
        other = first(self.fm("task", "new", "Other", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                              "--step", "s", "--json").stdout)["id"]
        slanes.pm_set(self.p, newest, task=other)  # another task's merge: this task has not landed again
        slanes._pm_cover(self.p, self.repo, [gone, newest], newest)
        self.assertEqual(self.rec(gone)["state"], "reverted")
        self.assertEqual(self.brief().status, "planned")

    def test_a_verified_merge_reverted_by_hand_later_reopens_its_task_once(self):
        sha = self.land("fine.txt")
        slanes.pm_set(self.p, sha, state="green")
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual(self.brief().status, "done", "on trunk: nothing to reopen")
        self.git("revert", "-m", "1", "--no-edit", sha)
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual((self.rec(sha)["state"], self.brief().status), ("reverted", "planned"))
        self.status("done")  # landed again by hand: a later pass never reopens it for the old sha
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual(self.brief().status, "done")

    def test_a_revert_that_was_itself_reverted_leaves_the_task_done(self):
        sha = self.land("bad.txt", "a\n")
        slanes.pm_set(self.p, sha, state="red", classes=["m.B"])
        self.git("revert", "-m", "1", "--no-edit", sha)
        self.git("revert", "--no-edit", "HEAD")  # a hand re-land: the feature is back on trunk
        self.assertFalse(slanes._pm_off_trunk(self.repo, sha))
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual(self.brief().status, "done", "the merge's effect is on trunk: nothing to reopen")

    def test_a_missing_brief_never_stops_the_revert(self):
        sha = self.land("bad.txt", "a\n")
        slanes.pm_set(self.p, sha, task="T-9999")  # a record whose task is gone
        self.assertIs(slanes._pm_reopen(self.p, self.repo, sha, "red"), False, "gone for good: no retry")
        self.assertTrue(c.ledger_events(self.p, ["lane_reopen_failed"]))

    # the review round: off trunk is read from the trunk (ancestry, then the revert chain), a failed reopen is retried

    def other_task(self):
        tid = first(self.fm("task", "new", "Other", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                            "--step", "s", "--json").stdout)["id"]
        self.status("done", tid)
        return tid

    def land_for(self, tid, name):
        sha = self.land(name)
        slanes.pm_set(self.p, sha, task=tid)
        return sha

    def busy_once(self):
        """cli.mutate raising LockTimeout for the reopen's event only, while `fails` says so (the brief lock is busy)."""
        orig, fails = cli.mutate, {"on": True}

        def mutate(p, tid, fn, event, data=None):
            if event == "lane_reopened_by_revert" and fails["on"]:
                raise c.LockTimeout("busy")
            return orig(p, tid, fn, event, data)
        patch = unittest.mock.patch.object(cli, "mutate", mutate)
        patch.start()
        self.addCleanup(patch.stop)
        return fails

    def test_an_odd_chain_of_reverts_is_read_to_its_end(self):
        sha = self.land("bad.txt", "a\n")
        self.git("revert", "-m", "1", "--no-edit", sha)
        r1 = self.git("rev-parse", "HEAD")
        self.assertTrue(slanes._pm_off_trunk(self.repo, sha), "reverted")
        self.git("revert", "--no-edit", r1)
        r2 = self.git("rev-parse", "HEAD")
        self.assertFalse(slanes._pm_off_trunk(self.repo, sha), "re-landed")
        self.git("revert", "--no-edit", r2)
        self.assertTrue(slanes._pm_off_trunk(self.repo, sha), "reverted again: the re-land was undone")
        slanes.pm_set(self.p, sha, state="red", classes=["m.B"])
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual((self.rec(sha)["state"], self.brief().status), ("reverted", "planned"))

    def test_a_merge_reset_off_trunk_reopens_its_task_whichever_path_sees_it(self):
        a = self.land_for(self.other_task(), "a.txt")
        slanes.pm_set(self.p, a, state="green")
        slanes.pm_wait(self.p, self.repo)  # a warm sweep: its stored head is a's
        b = self.land("b.txt")
        slanes.pm_set(self.p, b, state="green")
        slanes.pm_wait(self.p, self.repo)
        self.git("reset", "--hard", "-q", "HEAD~1")
        self.assertTrue(slanes._pm_off_trunk(self.repo, b))
        slanes.pm_wait(self.p, self.repo)  # trunk no longer holds the stored head: every record is checked
        self.assertEqual((self.rec(b)["state"], self.brief().status), ("reverted", "planned"))
        self.assertEqual(self.rec(b)["note"], "not in main")
        self.assertEqual(c.find_brief(self.p, slanes.pm_records(self.p)[a]["task"]).status, "done", "a is on trunk")

    def test_a_red_merge_reset_off_trunk_reopens_in_the_revert_and_after_the_trains_lock(self):
        for how in ("revert", "settled"):
            with self.subTest(how=how):
                self.status("done")
                sha = self.land(f"{how}.txt")
                slanes.pm_set(self.p, sha, state="red", classes=["m.B"])
                self.git("reset", "--hard", "-q", "HEAD~1")
                if how == "revert":
                    self.assertTrue(slanes._pm_revert(self.p, self.repo, sha))
                else:  # a pass that finds it settled under the train's lock: the reopen waits for the lock to drop
                    self.assertTrue(slanes._pm_settled(self.p, self.repo, sha))
                    self.assertEqual(self.brief().status, "done", "no brief is written under the train's lock")
                    self.assertTrue(self.rec(sha)["reopen_due"])
                    slanes._pm_reopen_due(self.p, self.repo)
                self.assertEqual((self.rec(sha)["state"], self.brief().status), ("reverted", "planned"))
                self.assertFalse(self.rec(sha)["reopen_due"])

    def test_a_task_whose_newer_merge_is_off_trunk_too_is_reopened(self):
        old = self.land("old.txt")
        gone = self.land("gone.txt")  # the same task's newer merge: reset off trunk, so it is no landing "again"
        self.git("reset", "--hard", "-q", "HEAD~1")
        self.git("revert", "-m", "1", "--no-edit", old)
        slanes.pm_set(self.p, old, state="red", classes=["m.B"])
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual(self.brief().status, "planned")
        self.assertEqual(self.rec(gone)["state"], "reverted", "the sweep settled the record of the merge that is gone")

    def test_a_reopen_that_hit_a_busy_lock_is_retried_by_the_next_pass(self):
        fails = self.busy_once()
        sha = self.land("bad.txt", "a\n")
        slanes.pm_set(self.p, sha, state="red", classes=["m.B"])
        self.git("revert", "-m", "1", "--no-edit", sha)
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual((self.rec(sha)["state"], self.brief().status), ("reverted", "done"))
        self.assertTrue(self.rec(sha)["reopen_due"], "the flag stays while the reopen has not run")
        slanes.pm_wait(self.p, self.repo)  # still busy: stays flagged, no crash
        self.assertTrue(self.rec(sha)["reopen_due"])
        fails["on"] = False
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual((self.rec(sha)["reopen_due"], self.brief().status), (False, "planned"))

    def test_the_verifys_own_revert_that_hit_a_busy_lock_is_retried_too(self):
        fails = self.busy_once()
        sha = self.land("bad.txt", "a\n")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual((self.rec(sha)["state"], self.brief().status), ("reverted", "done"))
        self.assertTrue(self.rec(sha)["reopen_due"])
        fails["on"] = False
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual(self.brief().status, "planned")

    def test_a_flag_is_cleared_when_the_task_needs_no_reopen(self):
        sha = self.land("bad.txt", "a\n")
        self.status("planned")
        slanes.pm_set(self.p, sha, state="red", classes=["m.B"])
        self.git("revert", "-m", "1", "--no-edit", sha)
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual((self.rec(sha)["reopen_due"], self.brief().status), (False, "planned"))

    def test_the_sweep_resumes_from_its_stored_head_and_says_when_it_caps(self):
        tids = [self.other_task() for _ in range(3)]
        shas = [self.land_for(t, f"m{i}.txt") for i, t in enumerate(tids)]
        for s in shas:
            slanes.pm_set(self.p, s, state="green")
        slanes.pm_wait(self.p, self.repo)
        mark = os.path.join(slanes._pm_dir(self.p), "sweep.json")
        self.assertEqual(json.load(open(mark))["head"], self.git("rev-parse", "HEAD"), "an uneventful pass stores trunk's head")
        for s in shas:
            self.git("revert", "-m", "1", "--no-edit", s)
        status = lambda: [c.find_brief(self.p, t).status for t in tids]  # noqa: E731
        with unittest.mock.patch.object(slanes, "PM_SWEEP_REVERTS", 1):
            slanes.pm_wait(self.p, self.repo)
            self.assertEqual(status(), ["planned", "done", "done"], "one revert a pass, the oldest first")
            self.assertTrue(c.ledger_events(self.p, ["pm_sweep_capped"]))
            slanes.pm_wait(self.p, self.repo)
            self.assertEqual(status(), ["planned", "planned", "done"])
            slanes.pm_wait(self.p, self.repo)
        self.assertEqual(status(), ["planned"] * 3)
        self.assertEqual(json.load(open(mark))["head"], self.git("rev-parse", "HEAD"), "caught up: the head is stored")

    def test_a_running_record_keeps_the_stored_head_so_its_revert_is_read_again(self):
        sha = self.land("fine.txt")
        slanes.pm_set(self.p, sha, state="running")  # no pid: nothing here ever signals it
        slanes.pm_wait(self.p, self.repo)
        self.assertFalse(os.path.exists(os.path.join(slanes._pm_dir(self.p), "sweep.json")))
        slanes.pm_set(self.p, sha, state="green")
        self.git("revert", "-m", "1", "--no-edit", sha)
        slanes.pm_wait(self.p, self.repo)
        self.assertEqual(self.brief().status, "planned")

    # the final round: a git that fails is unknown, never "off trunk"; the guard is re-read under the lock; cost; the cursor

    def git_fails(self, code, when=lambda args: "--is-ancestor" in args):
        """Patch lanes._git so the calls `when` picks fail with exit `code` (124: timed out, 127: git can't run)."""
        real = slanes._git

        def git(root, *args, **kw):
            if when(args):
                return subprocess.CompletedProcess(args, code, "", "git: failed")
            return real(root, *args, **kw)
        return unittest.mock.patch.object(slanes, "_git", git)

    def stored(self):
        try:
            return json.load(open(os.path.join(slanes._pm_dir(self.p), "sweep.json")))["head"]
        except OSError:
            return None

    def test_a_git_that_fails_never_reads_as_a_merge_off_trunk(self):
        for code in (124, 127):
            with self.subTest(code=code):
                self.status("done")
                if self.stored():  # each code starts cold: the reset below puts trunk back on the head the last pass stored
                    os.remove(os.path.join(slanes._pm_dir(self.p), "sweep.json"))
                sha = self.land(f"g{code}.txt")
                with self.git_fails(code):
                    self.assertIsNone(slanes._pm_in_effect(self.repo, sha), "unknown, not off")
                    self.assertFalse(slanes._pm_off_trunk(self.repo, sha))
                with self.git_fails(code, lambda a: "log" in a and any(x.startswith("--grep") for x in a)):
                    self.assertIsNone(slanes._pm_in_effect(self.repo, sha), "the revert read failed: unknown too")
                self.git("reset", "--hard", "-q", "HEAD~1")  # truly off trunk now
                self.assertTrue(slanes._pm_off_trunk(self.repo, sha))
                slanes.pm_set(self.p, sha, state="green")
                mark = self.stored()
                with self.git_fails(code):
                    slanes.pm_wait(self.p, self.repo)  # the sweep cannot tell: it skips the record and keeps its cursor
                self.assertEqual((self.rec(sha)["state"], self.brief().status, self.stored()), ("green", "done", mark))
                slanes.pm_wait(self.p, self.repo)  # git works again: now it is read
                self.assertEqual((self.rec(sha)["state"], self.brief().status), ("reverted", "planned"))

    def test_a_red_or_flagged_record_whose_ancestry_cannot_be_read_waits_and_is_not_reverted(self):
        sha = self.land("bad.txt", "a\n")
        slanes.pm_set(self.p, sha, state="red", classes=["m.B"])
        self.git("reset", "--hard", "-q", "HEAD~1")
        head = self.git("rev-parse", "HEAD")
        with self.git_fails(124):
            self.assertIsNone(slanes._pm_settled(self.p, self.repo, sha), "unknown: neither settled nor to be reverted")
            self.assertFalse(slanes._pm_revert(self.p, self.repo, sha), "retried by a later pass")
        self.assertEqual((self.rec(sha)["state"], self.brief().status, self.git("rev-parse", "HEAD")), ("red", "done", head))
        slanes.pm_set(self.p, sha, state="reverted", reopen_due=True)  # a flagged record: the retry reads it again
        with self.git_fails(124):
            slanes._pm_reopen_due(self.p, self.repo)
        self.assertEqual((self.rec(sha)["reopen_due"], self.brief().status), (True, "done"))
        slanes._pm_reopen_due(self.p, self.repo)
        self.assertEqual((self.rec(sha)["reopen_due"], self.brief().status), (False, "planned"))

    def test_a_pending_merge_whose_ancestry_cannot_be_read_stays_pending(self):
        one, two = self.land("one.txt"), self.land("two.txt")
        with self.git_fails(124):
            slanes._pm_cover(self.p, self.repo, [one, two], two)
        self.assertEqual((self.rec(one)["state"], self.rec(two)["state"]), ("pending", "green"))

    def test_a_land_recorded_after_the_reopen_looked_is_not_reopened_under_the_lock(self):
        old = self.land("bad.txt", "a\n")
        self.git("revert", "-m", "1", "--no-edit", old)
        new = self.land("new.txt", record=False)  # the re-land: on trunk, its task done, its record not written yet
        orig = cli.mutate

        def mutate(p, tid, fn, event, data=None):
            self.record(new)  # the record lands between the first look and the brief's write
            return orig(p, tid, fn, event, data)
        with unittest.mock.patch.object(cli, "mutate", mutate):
            self.assertIs(slanes._pm_reopen(self.p, self.repo, old, "reverted by hand"), False)
        self.assertEqual(self.brief().status, "done", "a fresh re-land is not flipped back to planned")
        self.assertFalse(c.ledger_events(self.p, ["lane_reopened_by_revert"]))

    def test_mutate_logs_no_event_when_its_fn_changed_nothing(self):
        cli.mutate(self.p, self.tid, lambda x: False, "lane_reopened_by_revert", {})
        self.assertFalse(c.ledger_events(self.p, ["lane_reopened_by_revert"]))
        cli.mutate(self.p, self.tid, lambda x: True, "lane_reopened_by_revert", {})
        self.assertTrue(c.ledger_events(self.p, ["lane_reopened_by_revert"]))

    def test_the_revert_read_scans_only_what_came_after_the_merge(self):
        sha = self.land("x.txt")
        real, seen = slanes._git, []

        def git(root, *args, **kw):
            if "log" in args:
                seen.append(args)
            return real(root, *args, **kw)
        with unittest.mock.patch.object(slanes, "_git", git):
            self.assertTrue(slanes._pm_in_effect(self.repo, sha))
        self.assertTrue(seen)
        self.assertTrue(all(f"{sha}..HEAD" in a for a in seen), seen)

    def test_a_capped_sweep_stops_its_cursor_before_a_revert_that_names_a_running_record(self):
        tids = [self.other_task() for _ in range(3)]
        a, b, d = (self.land_for(t, f"m{i}.txt") for i, t in enumerate(tids))
        for s in (a, b, d):
            slanes.pm_set(self.p, s, state="green")
        slanes.pm_wait(self.p, self.repo)
        first_head = self.stored()
        self.assertEqual(first_head, self.git("rev-parse", "HEAD"))
        slanes.pm_set(self.p, a, state="running")  # no pid: nothing here ever signals it
        self.git("revert", "-m", "1", "--no-edit", b)
        rev_b = self.git("rev-parse", "HEAD")
        self.git("revert", "-m", "1", "--no-edit", a)  # names the running record
        self.git("revert", "-m", "1", "--no-edit", d)
        with unittest.mock.patch.object(slanes, "PM_SWEEP_REVERTS", 2):
            slanes.pm_wait(self.p, self.repo)
        self.assertEqual(c.find_brief(self.p, tids[1]).status, "planned", "b's revert was read")
        self.assertEqual(self.stored(), rev_b, "the cursor stops at the revert before the one naming the running record")
        slanes.pm_set(self.p, a, state="green")
        slanes.pm_wait(self.p, self.repo)  # a's revert is read again, and d's after it
        self.assertEqual([c.find_brief(self.p, t).status for t in tids], ["planned"] * 3)


class AncestorExitCodes(ForemanTestCase):
    """T-5981: `git merge-base --is-ancestor` exits 0 (yes), 1 (no) and anything else (124 a timeout, 127 no git, 128 an unknown
    object) for git's own fault. A site that reads the exit code as a truth value takes that fault for "not an ancestor": a
    merge a timed-out git could not place reads as off trunk, and its task is reopened. This walks plugin/lib for every call
    and asserts each compares the exit code to 0 and 1 explicitly (`== 0`, `== 1`, `!= 1`, `in/not in (0, 1)`); a site that
    does not is named in ALLOWED with the reason its failure is safe, so a new one is a decision, never an accident."""
    # (file under plugin/lib, enclosing function) -> why a git failure read as "not an ancestor" does no harm there
    ALLOWED = {
        # the code is assigned first and compared explicitly on the next line, which the walk does not follow
        ("sampo/lanes.py", "_pm_anc"): "True on 0, False on 1, None on any other code",
        ("sampo/lanepilot.py", "fix_delta_base"): "0 returns the prior base, any code but 1 returns None: no delta base",
        ("sampo/lanes.py", "ancestor"): "focus: a code outside (0, 1) raises, nothing is focused",
        ("sampo/reverify.py", "scan"): "124 and 127 retry next time, 0 reads the range, 1 and 128 read none",
        # the failure lands on the side that keeps or refuses; none reopens, closes or removes anything on a git fault
        ("sampo/cli.py", "_merged_lane_tip"): "a fault returns None: the lane reads as unmerged, nothing is diffed or closed",
        ("sampo/cli.py", "_lane_merge"): "read-only diff bounds for a display: a fault diffs from the first parent instead",
        ("sampo/headroom.py", "raised"): "a fault drops the cached range and rescans by --since: slower, the same answer",
        ("sampo/lanes.py", "close_lane"): "a fault returns None: the lane is left open, not closed",
        ("sampo/lanes.py", "finished"): "a fault skips the lane: it is not swept as finished",
        ("sampo/lanes.py", "remove"): "a fault refuses the removal (the branch reads as unmerged); nothing is removed",
        ("sampo/release.py", "_push"): "a fault skips the fast-forward of the branch: the next release merges as before",
        ("sampo/release.py", "_verified"): "a fault skips that candidate commit: no verified base is claimed",
        ("sampo/release.py", "ship"): "a fault reads as rebased: git cherry alone / a re-pick, or the shared-file refusal",
        ("sampo/rune.py", "_gapmap_row"): "a fault adds a 'redraw it' complaint: a gate refusal, never a write",
        ("sampo/rune.py", "sweep"): "a fault lists the lane as unmerged: a report line, never a close",
        ("sampo/selfcheck.py", "unshipped_read"): "a fault returns None: 'not on this history', no count is claimed",
        ("sampo/train.py", "_merged_lanes"): "a fault reads as not merged: the sweep's business ends, nothing is closed",
        ("sampo/train.py", "merged"): "a fault reads as not merged: the lane is not closed",
        ("sampo/update.py", "_plan"): "a fault reads as diverged: the update refuses and changes nothing",
    }

    def sites(self):
        import ast
        lib = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
        found = []
        for root, _, files in os.walk(lib):
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path) as f:
                    tree = ast.parse(f.read(), path)
                parent = {ch: n for n in ast.walk(tree) for ch in ast.iter_child_nodes(n)}
                for n in ast.walk(tree):
                    if isinstance(n, ast.Call) and self.names_it(n):
                        up = n
                        while up in parent and not isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            up = parent[up]
                        found.append((os.path.relpath(path, lib), getattr(up, "name", "<module>"), n.lineno,
                                      self.explicit(n, parent)))
        return found

    @staticmethod
    def names_it(call):
        """The call's own arguments (a list literal or a `+` of them too, never a nested call's) hold "--is-ancestor"."""
        import ast
        todo = list(call.args)
        while todo:
            a = todo.pop()
            if isinstance(a, ast.Constant) and a.value == "--is-ancestor":
                return True
            if isinstance(a, (ast.List, ast.Tuple)):
                todo.extend(a.elts)
            elif isinstance(a, ast.BinOp):
                todo.extend((a.left, a.right))
        return False

    @staticmethod
    def explicit(call, parent):
        import ast
        node = call
        up = parent.get(node)
        if isinstance(up, ast.Attribute) and up.attr == "returncode":
            node, up = up, parent.get(up)
        elif isinstance(up, ast.Subscript) and isinstance(up.slice, ast.Constant) and up.slice.value == 0:
            node, up = up, parent.get(up)
        if not (isinstance(up, ast.Compare) and up.left is node and len(up.ops) == 1):
            return False
        op, to = up.ops[0], up.comparators[0]
        if isinstance(to, ast.Constant) and isinstance(to.value, int):
            return (isinstance(op, ast.Eq) and to.value in (0, 1)) or (isinstance(op, ast.NotEq) and to.value == 1)
        if isinstance(to, ast.Tuple) and isinstance(op, (ast.In, ast.NotIn)):
            return {0, 1} <= {e.value for e in to.elts if isinstance(e, ast.Constant)}
        return False

    def test_every_ancestor_test_reads_its_exit_code_explicitly_or_says_why_not(self):
        found = self.sites()
        self.assertGreater(len(found), 20, "the walk finds the calls")
        loose = {(f, fn) for f, fn, _, ok in found if not ok}
        unlisted = sorted((f, fn, ln) for f, fn, ln, ok in found if not ok and (f, fn) not in self.ALLOWED)
        self.assertFalse(unlisted, "an --is-ancestor whose exit code is read as a truth value (a git failure reads as "
                                   "'not an ancestor'): compare it to 0/1, or list it in ALLOWED with the reason")
        self.assertFalse(sorted(set(self.ALLOWED) - loose), "ALLOWED names a site that is now explicit or gone: drop it")
        self.assertTrue(all(self.ALLOWED.values()), "every entry says why")
