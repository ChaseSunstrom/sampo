"""T-4528: the process that takes the accept-check turn checks the other accepted lanes that wait their turn with its
own, as one candidate (their branches merged on a scratch copy of trunk, one affected run over the union of their
changes): green marks every member green at its tip and queues them all, so N waiting lanes cost one run. A red or cut
off batch falls back to each lane's own check in turn (a red lane is named, the others land); a lane that conflicts with
the others is left out of the batch and checked alone; a lane whose tip moved is judged by none of it. None of these
runs the suite: the run is one function (train._run_check) replaced by a stand-in that sees the candidate's tree."""
# map-links: plugin/lib/sampo/train.py
import os
import subprocess
import sys
import unittest
from unittest import mock

from test_accept_check import ASKED_ROW, STALE, AcceptCase, stub  # noqa: F401

from sampo import cli
from sampo import train


def done(argv, code=0, failed=(), cut_off=False, timed_out=False):
    """What _run_check returns for a finished stand-in run: (CompletedProcess carrying the check's JSON, timed_out)."""
    r = subprocess.run(stub(code, failed, cut_off), capture_output=True, text=True)
    return subprocess.CompletedProcess(argv, r.returncode, r.stdout, ""), timed_out


class BatchCase(AcceptCase):
    def setUp(self):
        super().setUp()
        self.ran = []  # (argv, cwd, the files at the top of the tree it ran in)
        self.red_on = set()  # a run whose tree holds one of these files is red
        self.cut_batches = False  # a run in a scratch tree (the candidate) is cut off at its deadline
        self.during = None  # called once, inside the first run: a lane moves its tip meanwhile
        run = mock.patch.object(train, "_run_check", side_effect=self._run)
        run.start()
        self.addCleanup(run.stop)

    def _run(self, argv, cwd):
        files = set(os.listdir(cwd))
        self.ran.append((argv, cwd, files))
        if self.during:
            hook, self.during = self.during, None
            hook()
        scratch = not os.path.basename(cwd).startswith("lane")
        if self.cut_batches and scratch:
            return done(argv, 124, cut_off=True, timed_out=True)
        if files & self.red_on:
            return done(argv, 1, failed=["test_x.X"])
        return done(argv, 0)

    def waiting(self, *ns, files=None, texts=None):
        """Lanes 1..n accepted (their checks claimed, no process behind them): the ids, in order."""
        ids = []
        for n in ns:
            name = (files or {}).get(n, f"f{n}.txt")
            tid = self.lane(n, name, text=(texts or {}).get(n), when="2025-12-31T00:00:00Z")
            self.accept(tid)
            ids.append(tid)
        return ids

    def turn(self, tid, n, **kw):
        return self.child(tid, n, stub(0), **kw)

    def states(self, *ids):
        return [self.meta_of(t).get("check_state") for t in ids]

    def scratch_trees(self):
        return [l for l in self.git("worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")
                and not os.path.basename(l).startswith(("lane", os.path.basename(self.repo)))]


class Grouping(BatchCase):
    def test_three_waiting_lanes_are_checked_as_one_candidate_in_one_run(self):  # batch
        ids = self.waiting(1, 2, 3)
        self.assertEqual(self.turn(ids[0], 1), 0)
        self.assertEqual(len(self.ran), 1, "one affected run for three lanes")
        argv, cwd, files = self.ran[0]
        self.assertTrue({"f1.txt", "f2.txt", "f3.txt"} <= files, f"the union of the three lanes' changes: {sorted(files)}")
        self.assertNotEqual(os.path.basename(cwd), "lane1", "a scratch tree, not one lane's")
        self.assertIn("--since", argv)
        self.assertEqual(argv[argv.index("--since") + 1], self.git("rev-parse", "HEAD"), "since the trunk the candidate is on")
        self.assertIn("--hold", argv, "the same repo-shape classes as a lane's own check")
        self.assertNotIn("--evidence", argv, "no member's evidence is written by a run that is not its own")
        for n, tid in enumerate(ids, 1):
            m = self.meta_of(tid)
            self.assertEqual(m["check_state"], "green", tid)
            self.assertEqual(m["check_tip"], self.tip(n))
            self.assertIn("accepted_at", m)
            self.assertIn("batch of 3", self.brief(tid).section("Log"))
        self.assertEqual(self.queued(), ids, "queued in order")
        self.assertEqual(self.scratch_trees(), [], "the candidate's scratch tree is removed")

    def test_the_waiting_lanes_skip_their_own_run_on_taking_the_turn(self):  # batch
        ids = self.waiting(1, 2, 3)
        self.turn(ids[0], 1)
        self.assertEqual(self.turn(ids[1], 2), 0)
        self.assertEqual(self.turn(ids[2], 3), 0)
        self.assertEqual(len(self.ran), 1, "a member's own process finds its green result and runs nothing")
        self.assertEqual(self.queued(), ids, "queued once each")
        self.assertEqual(self.states(*ids), ["green"] * 3)
        self.assertEqual(self.land_started.call_count, 1)

    def test_a_single_waiting_lane_behaves_as_today(self):  # batch
        (tid,) = self.waiting(1)
        self.assertEqual(self.turn(tid, 1), 0)
        self.assertEqual(len(self.ran), 1)
        argv, cwd, _ = self.ran[0]
        self.assertEqual(os.path.basename(cwd), "lane1", "run in its own lane")
        self.assertEqual(argv, stub(0), "the lane's own command, as accept built it")
        self.assertEqual(self.meta_of(tid)["check_state"], "green")
        self.assertEqual(self.queued(), [tid])

    def test_a_lane_whose_check_is_not_waiting_is_not_in_the_batch(self):  # batch
        ids = self.waiting(1, 2, 3)
        cli.mutate(self.p, ids[1], lambda b: b.meta.update(check_ticks=1), "x", {})  # its process is gone
        self.turn(ids[0], 1)
        self.assertTrue(self.ran[0][2] >= {"f1.txt", "f3.txt"} and "f2.txt" not in self.ran[0][2], self.ran[0][2])
        self.assertEqual(self.states(*ids), ["green", "running", "green"])

    def test_a_lane_whose_tip_moved_is_not_in_the_batch(self):  # batch
        ids = self.waiting(1, 2, 3)
        lane2 = os.path.join(self.tmp, "lane2")
        with open(os.path.join(lane2, "more.txt"), "w") as f:
            f.write("more\n")
        self.git("add", "more.txt", cwd=lane2)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "more", cwd=lane2)
        self.turn(ids[0], 1)
        self.assertNotIn("f2.txt", self.ran[0][2])
        self.assertEqual(self.states(*ids), ["green", "running", "green"])

    def test_a_tip_that_moves_during_the_run_discards_only_that_members_result(self):  # batch
        ids = self.waiting(1, 2, 3)

        def move():
            lane3 = os.path.join(self.tmp, "lane3")
            with open(os.path.join(lane3, "more.txt"), "w") as f:
                f.write("more\n")
            self.git("add", "more.txt", cwd=lane3)
            self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "more", cwd=lane3)
        self.during = move
        self.turn(ids[0], 1)
        self.assertEqual(self.states(*ids), ["green", "green", "running"], "lane 3's result is not written")
        self.assertEqual(self.queued(), ids[:2])

    def test_a_retried_check_and_a_retrying_lane_do_not_batch(self):  # batch
        ids = self.waiting(1, 2)
        cli.mutate(self.p, ids[1], lambda b: b.meta.update(check_tries=2), "x", {})  # lane 2 was cut off once already
        self.turn(ids[0], 1)
        self.assertNotIn("f2.txt", self.ran[0][2], "a lane that was cut off once is checked alone")
        self.assertEqual(self.states(*ids), ["green", "running"])

    def test_the_batch_is_capped(self):  # batch
        n = train.BATCH_MAX + 2
        ids = self.waiting(*range(1, n + 1))
        self.turn(ids[0], 1)
        files = self.ran[0][2]
        self.assertEqual(len([f for f in files if f.startswith("f") and f.endswith(".txt")]), train.BATCH_MAX)
        self.assertEqual(self.states(*ids).count("green"), train.BATCH_MAX)
        self.turn(ids[-1], n)  # the rest take their own turn: they batch among themselves, in one more run
        self.assertEqual(self.states(*ids), ["green"] * n)
        self.assertEqual(len(self.ran), 2)


class Fallback(BatchCase):
    def test_fallback_a_red_batch_names_the_red_lane_and_the_others_still_land(self):  # fallback
        ids = self.waiting(1, 2, 3)
        self.red_on = {"f2.txt"}
        self.turn(ids[0], 1)  # the batch: red; then lane 1's own check: green
        self.assertEqual(len(self.ran), 2, "the batch, then its own")
        self.assertEqual(os.path.basename(self.ran[1][1]), "lane1")
        self.assertEqual(self.states(*ids), ["green", "running", "running"], "a red batch writes nothing for the others")
        self.turn(ids[1], 2)
        self.turn(ids[2], 3)
        self.assertEqual(len(self.ran), 4, "the others each take their own turn and none retries the batch")
        self.assertEqual(self.states(*ids), ["green", "red", "green"])
        self.assertEqual(self.meta_of(ids[1])["check_failed"], "test_x.X", "the red lane is named")
        self.assertEqual(self.queued(), [ids[0], ids[2]])

    def test_fallback_a_cut_off_batch_checks_each_lane_alone(self):  # fallback
        ids = self.waiting(1, 2)
        self.cut_batches = True
        self.turn(ids[0], 1)
        self.assertEqual(len(self.ran), 2)
        self.assertEqual(self.states(*ids), ["green", "running"])
        self.turn(ids[1], 2)
        self.assertEqual(len(self.ran), 3)
        self.assertEqual(self.states(*ids), ["green", "green"])

    def test_fallback_a_conflicting_member_is_left_out_and_checked_alone(self):  # fallback
        ids = self.waiting(1, 2, 3, files={3: "same.txt", 2: "same.txt"}, texts={2: "two\n", 3: "three\n"})
        self.turn(ids[0], 1)
        self.assertEqual(len(self.ran), 1)
        files = self.ran[0][2]
        self.assertTrue({"f1.txt", "same.txt"} <= files)
        self.assertEqual(self.states(*ids), ["green", "green", "running"], "lane 3 conflicts with lane 2: left out")
        self.assertEqual(self.queued(), ids[:2])
        self.turn(ids[2], 3)
        self.assertEqual(len(self.ran), 2, "its own check")
        self.assertEqual(os.path.basename(self.ran[1][1]), "lane3")
        self.assertEqual(self.states(*ids), ["green"] * 3)
        self.assertEqual(self.scratch_trees(), [])

    def test_fallback_when_every_peer_conflicts_the_lane_runs_its_own_check_in_its_lane(self):  # fallback
        ids = self.waiting(1, 2, files={1: "same.txt", 2: "same.txt"}, texts={1: "one\n", 2: "two\n"})
        self.turn(ids[0], 1)
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(os.path.basename(self.ran[0][1]), "lane1")
        self.assertEqual(self.ran[0][0], stub(0), "the lane's own command, as accept built it")
        self.assertEqual(self.states(*ids), ["green", "running"])


class Outcome(unittest.TestCase):
    def test_outcome_names_green_red_and_cut_off(self):
        self.assertEqual(train._outcome(*done([], 0))[0], "green")
        self.assertEqual(train._outcome(*done([], 1, failed=["a.B"]))[0], "red")
        self.assertEqual(train._outcome(*done([], 124, cut_off=True))[0], "cut")
        self.assertEqual(train._outcome(*done([], 0, timed_out=True) if False else (done([], 0)[0], True))[0], "cut")
        self.assertEqual(train._outcome(subprocess.CompletedProcess([], 1, "boom\n", ""), False), ("cut", None))
        self.assertEqual(train._outcome(*done([], 1, failed=["a.B"]))[1]["failed"], ["a.B"])


if __name__ == "__main__":
    unittest.main()
