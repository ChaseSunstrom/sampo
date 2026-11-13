"""T-4540: a train's trunk is not moved under it. A post-merge revert and a land's hand close wait for a running train's
lock (bounded, then retried later) instead of committing beside it; a train whose trunk moved stops within a minute of
the move, naming the commit that moved it, instead of finishing a gate whose result is thrown away."""
import os
import threading
import time
from unittest import mock

from test_lane_train import TrainCase  # the fixtures (and helpers' sys.path) come first
from test_lane_land import report
from sampo import core as c
from sampo import lanes
from sampo import train


class TrunkWaits(TrainCase):
    """A red merge on trunk, and a running train's lock held by the test the way train() holds it."""
    def setUp(self):
        super().setUp()
        self.p = c.find_project(self.repo)
        self.tid = self.lane(1, "a.txt")
        patch = mock.patch.dict(os.environ, {"FM_POSTMERGE_CMD": "true"})  # a suite command: pm_record keeps a record
        patch.start()
        self.addCleanup(patch.stop)

    def red_merge(self, name):
        self.git("checkout", "-qb", f"b-{name}")
        with open(os.path.join(self.repo, f"{name}.txt"), "w") as f:
            f.write(name)
        self.git("add", "-A")
        self.git("commit", "-qm", name)
        self.git("checkout", "-q", "-")
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {name}", f"b-{name}")
        sha = self.git("rev-parse", "HEAD")
        with mock.patch.object(lanes, "pm_spawn"):
            lanes.pm_record(self.p, sha, self.tid, self.repo)
        lanes.pm_set(self.p, sha, state="red", classes=["x.Y"])
        return sha

    def held_train_lock(self):
        """The lock train() takes for the whole of a run (train.train: c.lock on the project's train folder)."""
        return c.lock(os.path.join(self.p.dir, "train"), timeout=1)

    def test_a_revert_waits_for_a_running_train_and_commits_nothing_meanwhile(self):
        sha = self.red_merge("a")
        before = self.git("rev-parse", "HEAD")
        with self.held_train_lock(), mock.patch.object(lanes, "TRUNK_WAIT_S", 0.2):
            self.assertFalse(lanes._pm_revert(self.p, self.repo, sha), "a revert under a running train waits")
        self.assertEqual(self.git("rev-parse", "HEAD"), before, "no revert commit landed beside the train")
        self.assertEqual(lanes.pm_records(self.p)[sha]["state"], "red", "still red: retried after the train")
        self.assertTrue(lanes._pm_revert(self.p, self.repo, sha), "the lock free: the revert goes through")
        self.assertIn(f"This reverts commit {sha}", self.git("log", "-1", "--format=%B"))

    def test_a_land_hand_close_waits_for_a_running_train_and_is_left_for_the_next_land(self):
        self.fm("task", "set", self.tid, "--section", "Close fields", "--text", report(self.tid))
        self.fm("task", "audit", self.tid, "self", "one fm-reviewer pass on the branch", "no findings")
        with self.held_train_lock(), mock.patch.object(lanes, "TRUNK_WAIT_S", 0.2), \
                mock.patch.object(train, "_sampo", wraps=train._sampo) as sampo:
            why, title = train._close(self.p, self.repo, self.tid)
        finishes = [call.args[0][:2] for call in sampo.call_args_list if call.args and call.args[0][:1] == ["task"]]
        self.assertNotIn(["task", "finish"], finishes, "the hand close did not commit beside the train")
        self.assertIn("train", why, why)
        self.assertEqual(title, "")

    def test_a_pass_that_waited_for_the_lock_settles_a_revert_the_other_pass_made(self):
        """Review T-4540 (2): two passes wait on the lock; the second must not revert again and write conflict."""
        sha = self.red_merge("a")
        before = self.git("rev-parse", "HEAD")
        real = lanes.train_lock

        def other_pass_first(p, timeout=None):
            self.git("revert", "-m", "1", "--no-edit", sha)  # the other pass reverts and settles, then releases
            lanes.pm_set(self.p, sha, state="reverted")
            return real(p, timeout)
        with mock.patch.object(lanes, "train_lock", side_effect=other_pass_first):
            self.assertTrue(lanes._pm_revert(self.p, self.repo, sha))
        self.assertEqual(self.git("rev-list", "--count", f"{before}..HEAD"), "1", "one revert, not two")
        self.assertEqual(lanes.pm_records(self.p)[sha]["state"], "reverted", "not conflict over reverted")

    def test_a_pass_that_finds_the_revert_on_trunk_before_its_record_settles_it(self):
        """Review T-4540 (2): the revert landed but the record still says red: the pass settles it, no second revert."""
        sha = self.red_merge("a")
        before = self.git("rev-parse", "HEAD")
        real = lanes.train_lock

        def revert_landed_first(p, timeout=None):
            self.git("revert", "-m", "1", "--no-edit", sha)  # the record still reads red
            return real(p, timeout)
        with mock.patch.object(lanes, "train_lock", side_effect=revert_landed_first):
            self.assertTrue(lanes._pm_revert(self.p, self.repo, sha))
        self.assertEqual(self.git("rev-list", "--count", f"{before}..HEAD"), "1", "one revert, not two")
        self.assertEqual(lanes.pm_records(self.p)[sha]["state"], "reverted")

    def test_a_refused_second_train_spawns_no_post_merge_pass(self):
        """Review T-4540 (3): a train that never took the lock runs no pass in its finally."""
        self.red_merge("a")
        with self.held_train_lock(), mock.patch.object(lanes, "pm_spawn") as spawn:
            with self.assertRaises(c.LockTimeout):
                train.train(self.p, self.repo, [self.tid])
        self.assertFalse(spawn.called, "a refused train must not spawn a verify")

    def test_a_failing_post_merge_pass_never_raises_out_of_a_train(self):
        """Review T-4540 (3): the pass after a train that did run cannot replace the train's result."""
        with mock.patch.object(train, "pm_after_train", side_effect=RuntimeError("spawn broke")):
            self.assertIsNone(train._after_train_run(self.p, self.repo))

    def test_a_train_that_starts_during_a_hand_close_waits_for_the_close(self):
        """Review T-4540 (1): a close holds trunk across its finish; a train waits for it, not refused at 1 s."""
        closing = lanes.train_lock_closing(self.p)
        closing.__enter__()
        entered = []

        def train_side():
            with train._train_run_lock(self.p):
                entered.append(True)
        t = threading.Thread(target=train_side)
        t.start()
        try:
            time.sleep(1.5)  # past the 1 s a plain train waits
            self.assertFalse(entered, "the train runs only once the close is done")
        finally:
            closing.__exit__(None, None, None)
        t.join(60)
        self.assertTrue(entered, "the train got in after the close")
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, "train", "closing")), "the close's mark is cleared")

    def test_a_red_merge_refused_at_the_merge_starts_a_pass_that_retries_its_revert(self):
        """Review T-4540 (4): a revert that lost the wait is retried by the next verify, even if its train was killed."""
        sha = self.red_merge("a")
        with self.held_train_lock(), mock.patch.object(lanes, "pm_spawn") as spawn:
            with self.assertRaises(c.PolicyError):
                lanes._pm_check(self.p, self.repo)
            self.assertTrue(spawn.called, "the refusal starts a pass that retries the revert")
        lanes.pm_run(self.p, self.repo)  # the lock is free: the next verify pass reverts it
        self.assertEqual(lanes.pm_records(self.p)[sha]["state"], "reverted")


class TrunkMoves(TrainCase):
    """A running gate, and a commit on trunk made while it runs."""
    def trunk_commit(self, name):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(name)
        self.git("add", name)
        self.git("commit", "-qm", f"trunk: {name}")
        return self.git("rev-parse", "HEAD")

    def test_a_running_gate_stops_within_a_minute_of_a_trunk_commit_and_names_it(self):
        tid = self.lane(1, "a.txt")
        started, go = os.path.join(self.tmp, "gate-started"), os.path.join(self.tmp, "gate-go")
        # the gate waits for `go`, which this test never makes: it can only end by being stopped (or after 150 s, red)
        self.fm("check", "add", f"touch {started}; for i in $(seq 1 300); do test -e {go} && exit 0; sleep 0.5; done; exit 1")
        out = {}
        t = threading.Thread(target=lambda: out.update(r=self.train(tid, check=False)))
        t.start()
        try:
            deadline = time.monotonic() + 60
            while not os.path.exists(started) and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertTrue(os.path.exists(started), "the gate never started")
            moved = self.trunk_commit("moved.txt")
            t0 = time.monotonic()
            t.join(90)
            elapsed = time.monotonic() - t0
        finally:
            open(go, "w").close()  # releases a gate the test failed to stop
            t.join(60)
        self.assertFalse(t.is_alive(), "the train went on with its gate after trunk moved")
        self.assertLess(elapsed, 60, f"stopped {elapsed:.0f} s after the move")
        r = out["r"]
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(moved[:9], r.stdout + r.stderr, "the refusal names the commit that moved trunk")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")), "nothing landed")
