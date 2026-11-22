"""T-4596: 55 lanes held tasks still active though their work was on the trunk (a hand train, `sampo lane merge A B`,
lands lanes and closed nothing; closing one by hand ran the whole gate again). One shared close (train.close_landed, the
close sampo lane land makes for each landed lane) now serves three callers: land's trains, the multi-id lane merge, and
`sampo lane land --merged`, which sweeps every lane whose tip is already on the trunk: it closes each clean lane's task
on the gate pass already on record for the trunk (one gate run when none is), and leaves a dirty lane, a task with no
close fields or audits, and a task whose own verify fails on the trunk active, the reason on the task and in the output."""
# map-links: plugin/lib/sampo/train.py
import contextlib
import io
import json
import os
import subprocess
import threading
import time
import unittest
from unittest import mock

from helpers import FM
from test_lane_land import OLD, LandCase

from sampo import core as c
from sampo import lanes
from sampo import train


class Merged(LandCase):
    def lanes_ready(self, n, bare=(), per=None):
        """n lanes each with one commit, audited and reported: what a finished lane's close needs (`bare`: lane
        numbers whose builder never reported, so their briefs hold no close fields; `per`: lane number -> lane()
        keywords)."""
        ids = [self.lane(i, f"f{i}.txt", when=OLD, **((per or {}).get(i) or {})) for i in range(1, n + 1)]
        self.audited(*ids)
        for i, tid in enumerate(ids, 1):
            if i not in bare:
                self.returned(tid, f"Lane {i}")
        return ids

    def hand_merge(self, *ns):
        """The trunk takes these lanes by plain git: no gate ran, no pass is on record (the 55 stuck lanes)."""
        for n in ns:
            self.git("merge", "--no-ff", "-q", "-m", f"merge lane {n}", f"foreman/lane{n}")

    def sweep(self, *a, check=True):
        return self.land("--merged", *a, check=check)

    def done(self, tid):
        return self.brief(tid).status == "done"

    def folder(self, n):
        return os.path.isdir(os.path.join(self.tmp, f"lane{n}"))

    def pm_file(self, sha, state, tid, **kv):
        """A post-merge verify record the way lanes.pm_record writes it (state: pending, running (this pid), green …)."""
        p = c.find_project(self.repo)
        os.makedirs(os.path.join(p.dir, "postmerge"), exist_ok=True)
        with open(os.path.join(p.dir, "postmerge", f"{sha}.json"), "w") as f:
            json.dump({"sha": sha, "task": tid, "state": state, "n": time.time_ns(), "at": c.now(), "pid": os.getpid(),
                       **kv}, f)
        return p

    def sweep_here(self):
        """The sweep in this process (its waiting is patched short): (the result, what it said on stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.in_project():
            got = train.land_merged(c.find_project(self.repo), self.repo)
        return got, err.getvalue()

    def test_merged_waits_for_a_running_post_merge_verify_of_its_lanes_and_says_so(self):  # verify, T-5368
        ids = self.lanes_ready(2)
        self.hand_merge(1, 2)
        sha = self.git("rev-parse", "HEAD")
        p = self.pm_file(sha, "running", ids[0])  # the merge's verify is still running: its tests are the closes' tests
        flip = threading.Timer(0.8, lanes.pm_set, (p, sha), {"state": "green"})
        flip.start()
        self.addCleanup(flip.cancel)
        t0 = time.monotonic()
        with mock.patch.object(train, "PM_POLL_S", 0.1), mock.patch.object(train, "PM_WAIT_S", 60):
            got, err = self.sweep_here()
        self.assertGreaterEqual(time.monotonic() - t0, 0.7, "the closes waited for the verify to end")
        self.assertIn("post-merge verify", err)
        self.assertIn(ids[0], err, "it names the task whose verify it waits for")
        self.assertEqual(sorted(got["closed"]), sorted(ids), got)
        self.assertIn("waited", got["gate"], "the result says it waited")

    def test_merged_waits_for_the_verify_with_no_land_lock_held(self):  # verify, T-5368 review
        ids = self.lanes_ready(1)
        self.hand_merge(1)
        p = c.find_project(self.repo)
        seen = []

        def probe(pp, want, who):  # a second sweep (or a hand land) must be able to take the lock while this one waits
            try:
                with c.lock(os.path.join(p.dir, "land"), timeout=0.5):
                    seen.append("free")
            except c.LockTimeout:
                seen.append("held")
            return ""
        with mock.patch.object(train, "_wait_pm", probe), self.in_project():
            got = train.land_merged(p, self.repo)
        self.assertEqual(seen, ["free"], "the land lock is not held through up to 30 minutes of waiting")
        self.assertEqual(got["closed"], ids, got)

    def test_merged_says_another_land_runs_before_it_waits_for_a_verify(self):  # verify, T-5368 re-review
        ids = self.lanes_ready(1)
        self.hand_merge(1)
        self.pm_file(self.git("rev-parse", "HEAD"), "running", ids[0])  # a verify it would wait up to 30 minutes for
        p = c.find_project(self.repo)
        waited = []
        with c.lock(os.path.join(p.dir, "land"), timeout=1), self.in_project(), \
                mock.patch.object(train, "_wait_pm", lambda *a: waited.append(a) or ""):
            with self.assertRaises(c.PolicyError) as ctx:
                train.land_merged(p, self.repo)
        self.assertIn("another sampo lane land is running", str(ctx.exception))
        self.assertEqual(waited, [], "refused before any waiting")

    def test_merged_does_not_wait_for_a_verify_of_another_task_a_dead_one_or_a_finished_one(self):  # verify, T-5368
        ids = self.lanes_ready(1)
        self.hand_merge(1)
        sha = self.git("rev-parse", "HEAD")
        p = c.find_project(self.repo)
        for what in (("running", "T-9999", {}), ("running", ids[0], {"pid": 4194305}), ("green", ids[0], {}),
                     ("red", ids[0], {}), ("pending", ids[0], {"at": "2020-01-01T00:00:00Z"})):
            with self.subTest(what):
                self.pm_file(sha, what[0], what[1], **what[2])
                self.assertEqual(train._pm_unsettled(p, ids), [], "nothing of its lanes' to wait for")
        self.pm_file(sha, "pending", ids[0])  # just landed: its runner is about to take it
        self.assertEqual([t for t, _, _ in train._pm_unsettled(p, ids)], ids)
        self.pm_file(sha, "running", ids[0])
        self.assertEqual([t for t, _, _ in train._pm_unsettled(p, ids)], ids)

    def test_merged_waits_for_a_verify_only_up_to_a_deadline_then_closes_and_says_it_was_still_running(self):  # verify
        ids = self.lanes_ready(1)
        self.hand_merge(1)
        self.pm_file(self.git("rev-parse", "HEAD"), "running", ids[0])  # never ends
        t0 = time.monotonic()
        with mock.patch.object(train, "PM_POLL_S", 0.05), mock.patch.object(train, "PM_WAIT_S", 0.4):
            got, err = self.sweep_here()
        self.assertLess(time.monotonic() - t0, 30, "bounded by its deadline")
        self.assertIn("still running", err)
        self.assertEqual(got["closed"], ids, "the close goes on without it")

    def test_merged_closes_every_merged_lane_on_one_gate_run_when_no_pass_is_on_record(self):
        ids = self.lanes_ready(3)
        self.hand_merge(1, 2, 3)
        self.assertEqual(self.gate_runs(), 0)
        r = self.sweep()
        for n, tid in enumerate(ids, 1):
            self.assertTrue(self.done(tid), r.stdout + r.stderr)
            self.assertFalse(self.folder(n), "its lane goes once its task is closed")
            self.assertRegex(r.stdout, rf"(?m)^  closed {tid}: ")
            self.assertEqual(self.log().count(f"Lane {n} lands with one gate run ({tid})"), 1)
        self.assertEqual(self.gate_runs(), 1, "three closes, one gate run (not one each)")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")

    def test_the_pass_survives_each_close_s_changelog_line_for_tasks_whose_own_verify_is_the_full_check(self):
        per = {i: {"ac": f"{FM} check"} for i in (1, 2, 3)}  # each close re-runs the full gates unless a pass holds
        ids = self.lanes_ready(3, per=per)
        self.hand_merge(1, 2, 3)
        r = self.sweep()
        self.assertTrue(all(self.done(t) for t in ids), r.stdout + r.stderr)
        self.assertEqual(self.gate_runs(), 1, "the sweep's one check served the three finishes: " + r.stdout)

    def test_merged_reuses_the_trains_gate_pass_so_no_gate_runs_at_all(self):
        ids = self.lanes_ready(3)
        self.train(*ids)
        self.assertEqual(self.gate_runs(), 1)
        r = self.sweep()
        self.assertTrue(all(self.done(t) for t in ids), r.stdout + r.stderr)
        self.assertEqual(self.gate_runs(), 1, "the train's pass held for all three closes: " + r.stdout)

    def test_merged_leaves_a_dirty_lane_a_failing_verify_and_a_task_with_no_close_fields_active_with_the_reason(self):
        ids = self.lanes_ready(4, bare=(4,), per={3: {"ac": "false"}})
        okay, dirty, failing, bare = ids
        with open(os.path.join(self.tmp, "lane2", "wip.txt"), "w") as f:  # uncommitted work in a merged lane
            f.write("work in progress\n")
        self.hand_merge(1, 2, 3, 4)
        r = self.sweep()
        self.assertTrue(self.done(okay), r.stdout + r.stderr)
        self.assertFalse(self.folder(1))
        for tid, n, why in ((dirty, 2, r"uncommitted"), (failing, 3, r"."), (bare, 4, r"close fields")):
            self.assertFalse(self.done(tid), tid)
            self.assertTrue(self.folder(n), "a lane left open keeps its folder")
            self.assertTrue(self.brief(tid).meta.get("lane"), tid)
            self.assertRegex(r.stdout, rf"(?m)^  left {tid}: .*{why}", r.stdout)
            self.assertIn("land --merged: left active", self.brief(tid).section("Log"), tid)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "lane2", "wip.txt")), "nothing was discarded")
        self.assertNotIn(okay, r.stdout.split("left")[-1])

    def test_merged_runs_a_second_time_without_repeating_what_the_first_did(self):
        ids = self.lanes_ready(2)
        with open(os.path.join(self.tmp, "lane2", "wip.txt"), "w") as f:
            f.write("wip\n")
        self.hand_merge(1, 2)
        self.sweep()
        log = self.brief(ids[1]).section("Log")
        r = self.sweep()
        self.assertEqual(self.brief(ids[1]).section("Log").count("land --merged: left active"),
                         log.count("land --merged: left active"), "the same reason is logged once")
        self.assertRegex(r.stdout, rf"(?m)^  left {ids[1]}: ")
        self.assertEqual(self.log().count(f"({ids[0]})"), 1, "a closed task is not closed twice")

    def test_merged_never_touches_a_lane_with_a_commit_the_trunk_lacks(self):
        ids = self.lanes_ready(2)
        self.hand_merge(1)
        path = os.path.join(self.tmp, "lane2")
        r = self.sweep()
        self.assertFalse(self.done(ids[1]))
        self.assertTrue(self.folder(2))
        self.assertIn("foreman/lane2", self.git("branch", "--list", "foreman/lane2"))
        self.assertNotIn(ids[1], r.stdout, "a lane still ahead of the trunk is not the sweep's business")
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(self.done(ids[0]))
        # a merged lane that then took a new commit is not merged any more
        self.hand_merge(2)
        with open(os.path.join(self.tmp, "lane2", "more.txt"), "w") as f:
            f.write("more\n")
        self.git("add", "more.txt", cwd=path)
        subprocess.run(["git", "-C", path, "commit", "-qm", "more"], check=True, capture_output=True)
        r = self.sweep()
        self.assertFalse(self.done(ids[1]), r.stdout)
        self.assertTrue(self.folder(2))

    def test_merged_with_a_red_gate_closes_nothing(self):
        ids = self.lanes_ready(3)
        self.hand_merge(1, 2, 3)
        with open(os.path.join(self.repo, "BREAK"), "w") as f:  # the trunk's gate is red: no close rides on it
            f.write("x\n")
        r = self.sweep(check=False)
        self.assertFalse(any(self.done(t) for t in ids), r.stdout + r.stderr)
        self.assertTrue(self.folder(1) and self.folder(2) and self.folder(3))
        self.assertLessEqual(self.gate_runs(), 2, "one red check (it retries once), not one per task")
        self.assertIn("gate", r.stdout + r.stderr)
        for tid in ids:
            self.assertIn("land --merged: left active", self.brief(tid).section("Log"))

    def test_merged_refuses_beside_an_active_main_task(self):
        ids = self.lanes_ready(1)
        self.hand_merge(1)
        self.fm("task", "new", "Main work", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        with open(self.changelog, "a") as f:  # T-4692: held only by uncommitted work in a file every close writes
            f.write("- the focused task's own line (T-9990).\n")
        r = self.sweep(check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.done(ids[0]))
        self.assertTrue(self.folder(1))

    def test_merged_reads_json_and_names_the_flag_in_its_help(self):
        ids = self.lanes_ready(1)
        self.hand_merge(1)
        got = json.loads(self.sweep("--json").stdout)
        self.assertEqual(got["closed"], ids)
        self.assertIn("--merged", self.fm("lane", "--help").stdout)

    def test_a_multi_id_lane_merge_closes_what_it_lands(self):
        ids = self.lanes_ready(3)
        r = self.fm("lane", "merge", *ids)
        for n, tid in enumerate(ids, 1):
            self.assertTrue(self.done(tid), r.stdout + r.stderr)
            self.assertFalse(self.folder(n))
            self.assertRegex(r.stdout, rf"(?m)^  closed {tid}: ")
        self.assertEqual(self.gate_runs(), 1, "the train's one gate run served all three closes")

    def test_a_multi_id_lane_merge_leaves_a_lane_it_cannot_close_for_a_hand_close(self):
        ids = self.lanes_ready(2, bare=(2,))
        r = self.fm("lane", "merge", *ids)
        self.assertTrue(self.done(ids[0]), r.stdout + r.stderr)
        self.assertFalse(self.done(ids[1]))
        self.assertTrue(self.folder(2))
        self.assertRegex(r.stdout, rf"(?m)^  hand close {ids[1]}: .*close fields")

    def test_the_hand_train_alone_still_closes_nothing(self):
        ids = self.lanes_ready(2)
        self.train(*ids)
        self.assertFalse(any(self.done(t) for t in ids), "sampo lane train lands; closing is land's and merge's")


if __name__ == "__main__":
    unittest.main()
