"""T-4596: 55 lanes held tasks still active though their work was on the trunk (a hand train, `sampo lane merge A B`,
lands lanes and closed nothing; closing one by hand ran the whole gate again). One shared close (train.close_landed, the
close sampo lane land makes for each landed lane) now serves three callers: land's trains, the multi-id lane merge, and
`sampo lane land --merged`, which sweeps every lane whose tip is already on the trunk: it closes each clean lane's task
on the gate pass already on record for the trunk (one gate run when none is), and leaves a dirty lane, a task with no
close fields or audits, and a task whose own verify fails on the trunk active, the reason on the task and in the output."""
# map-links: plugin/lib/sampo/train.py
import json
import os
import subprocess
import unittest

from helpers import FM
from test_lane_land import OLD, LandCase


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
