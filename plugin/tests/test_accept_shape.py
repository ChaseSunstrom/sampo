"""T-4542, T-4563: the repo-shape tests are one list (core.REPO_SHAPE: its modules, then its module.Class entries). The builder
contract names the modules before the report (fm-builder.md through the <SHAPE> placeholder, lanes.contract() substitutes
them). sampo lane accept starts the lane's check and returns at once (T-4563): the check's child runs the modules first,
before its long part. Red records the refusal on the lane and ends the child; green is recorded at the tip and the hold
check runs without the classes (--shape-green), a fresh run without them holds the lane clean at that tip; cut or a tree
without the modules goes on as before, and a retry runs no module again. The runs here are stubbed (c.run_command,
c.run_record, train._shape_run, train._run_check); none starts a real test run."""
# map-links: plugin/lib/sampo/train.py
import fcntl
import os
import threading
import time
import unittest
from unittest import mock

from test_accept_check import AcceptCase, OLD, FRESH, stub

from sampo import cli
from sampo import core as c
from sampo import lanes as slanes
from sampo import train

AGENT = os.path.join(os.path.dirname(__file__), "..", "agents", "fm-builder.md")
MODULES = ("test_docs", "test_left_out", "test_rename_aliases", "test_rename_scan", "test_run", "test_fast_equiv")
CLASSES = ("test_docs.PluginFiles", "test_left_out.LeftOut", "test_rename_aliases.RenameAliases", "test_run.EnvClassified")


class ContractShape(unittest.TestCase):
    def test_the_shape_modules_and_classes_are_one_list(self):  # T-4563
        self.assertEqual(c.REPO_SHAPE, MODULES + CLASSES)
        self.assertEqual(c.repo_shape_modules(), MODULES)
        self.assertEqual(c.repo_shape_classes(), CLASSES)
        self.assertFalse(hasattr(c, "REPO_SHAPE_MODULES"), "one list, not two constants")
        self.assertFalse(hasattr(c, "REPO_SHAPE_CLASSES"), "one list, not two constants")

    def test_the_shape_modules_are_named_in_the_builder_contract(self):  # T-4542
        with open(AGENT, encoding="utf-8") as f:
            agent = f.read()
        self.assertIn("<SHAPE>", agent)
        self.assertIn("Before your report", agent)
        text = slanes.contract("T-9", "abc123", "/main")
        self.assertNotIn("<SHAPE>", text)
        self.assertIn("python3 plugin/tests/run.py " + " ".join(MODULES), text)
        self.assertIn("Before your report", text)


class ShapeRun(unittest.TestCase):
    def run_shape(self, code, out="", record=None):
        with mock.patch.object(c, "run_command", return_value=(code, out)) as run, \
                mock.patch.object(c, "run_record", return_value=record):
            verdict = train._shape_run("/lane")
        return verdict, run

    def test_green_run_goes_on_with_no_names(self):  # T-4542
        verdict, run = self.run_shape(0, "Ran 65 tests in 3s\nOK")
        self.assertEqual(verdict, ("green", ""))
        argv, kw = run.call_args[0], run.call_args[1]
        self.assertIn("python3 plugin/tests/run.py " + " ".join(MODULES), argv[1])
        self.assertTrue(kw.get("niced"))
        self.assertEqual(argv[2], train.SHAPE_WALL_S)

    def test_red_run_names_the_failing_classes_from_the_record(self):  # T-4542
        rec = {"ok": False, "failed": [{"class": "test_docs.PluginFiles", "test": "t", "first": "x"},
                                       {"class": "test_run.EnvClassified", "test": "u", "first": "y"},
                                       {"class": "test_docs.PluginFiles", "test": "v", "first": "z"}]}
        verdict, _ = self.run_shape(1, "failed", rec)
        self.assertEqual(verdict, ("red", "test_docs.PluginFiles, test_run.EnvClassified"))

    def test_red_run_with_no_record_names_the_classes_its_output_names(self):  # T-4542
        verdict, _ = self.run_shape(1, "  failed class: test_left_out.LeftOut\n")
        self.assertEqual(verdict, ("red", "test_left_out.LeftOut"))

    def test_a_cut_run_or_one_with_no_named_class_is_no_verdict(self):  # T-4542
        self.assertEqual(self.run_shape(124, "timed out after 900s")[0], ("cut", ""))
        self.assertEqual(self.run_shape(1, "could not run bash", None)[0], ("cut", ""))
        self.assertEqual(self.run_shape(1, "", {"ok": False, "not_run": ["x"], "failed": []})[0], ("cut", ""))


def stub_shape(case):
    """_shape_run stubbed green (case.shape_run) in a lane that has the modules (a Sampo tree)."""
    shape = mock.patch.object(train, "_shape_run", return_value=("green", ""))
    case.shape_run = shape.start()
    case.addCleanup(shape.stop)
    tree = mock.patch.object(train, "_shape_tree", return_value=True)
    tree.start()
    case.addCleanup(tree.stop)


class AcceptShape(AcceptCase):
    def setUp(self):
        super().setUp()
        stub_shape(self)

    def test_accept_returns_at_once_and_runs_no_module_itself(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        out = self.accept(tid)
        self.shape_run.assert_not_called()
        self.assertEqual(len(self.spawned), 1, "the lane's check starts; its child runs the modules first")
        self.assertIn("check started", out)
        self.assertEqual(self.queued(), [])

    def test_a_red_module_run_ends_the_child_before_the_long_check_and_is_recorded_on_the_lane(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        self.shape_run.return_value = ("red", "test_docs.PluginFiles, test_run.EnvClassified")
        with mock.patch.object(train, "_run_check") as long_check:
            self.assertEqual(self.child(tid, 1, stub(0)), 1)
        long_check.assert_not_called()
        m = self.meta_of(tid)
        self.assertEqual((m["check_state"], m["check_failed"]), ("red", "test_docs.PluginFiles test_run.EnvClassified"))
        self.assertIn("repo-shape modules are red", self.brief(tid).section("Log"))
        self.assertEqual(self.queued(), [])
        self.land_started.assert_not_called()
        out = self.accept(tid)  # held at its tip: named, no retry
        self.assertIn("test_docs.PluginFiles", out)
        self.assertEqual(len(self.spawned), 1, out)

    def test_a_green_module_run_is_recorded_at_the_tip_and_the_hold_check_runs_without_the_classes(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        with mock.patch.object(train, "_run_check", wraps=train._run_check) as long_check:
            self.assertEqual(self.child(tid, 1, stub(0)), 0)
        self.assertIn("--shape-green", long_check.call_args[0][0])
        self.assertEqual(self.meta_of(tid)["check_shape"], self.tip(1))
        self.shape_run.assert_called_once()
        self.assertEqual(self.meta_of(tid)["check_state"], "green")
        self.assertEqual(self.queued(), [tid])

    def test_a_fresh_run_without_the_classes_is_clean_once_the_module_run_is_green_at_its_tip(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.record_run(tid, FRESH, cmd="python3 plugin/tests/run.py test_x")
        b = c.find_brief(self.p, tid)
        b.meta["check_shape"] = self.tip(1)
        c.save_brief(self.p, b)
        self.accept(tid)
        self.assertEqual(self.spawned, [], "the reused green holds the classes: no check again")
        self.assertEqual(self.queued(), [tid])

    def test_a_cut_module_run_is_no_verdict_and_the_long_check_runs_the_classes_itself(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        self.shape_run.return_value = ("cut", "")
        with mock.patch.object(train, "_run_check", wraps=train._run_check) as long_check:
            self.assertEqual(self.child(tid, 1, stub(0)), 0)
        self.assertNotIn("--shape-green", long_check.call_args[0][0])
        self.assertNotIn("check_shape", self.meta_of(tid))
        self.assertEqual(self.queued(), [tid])

    def test_a_retry_runs_no_module_again(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        self.assertEqual(self.child(tid, 1, stub(0), tries=2), 0)
        self.shape_run.assert_not_called()
        self.assertEqual(self.queued(), [tid])

    def test_a_lane_with_no_shape_modules_in_its_tree_runs_none_and_its_check_goes_on(self):  # T-4542
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        with mock.patch.object(train, "_shape_tree", return_value=False):
            self.assertEqual(self.child(tid, 1, stub(0)), 0)
        self.shape_run.assert_not_called()
        self.assertEqual(self.queued(), [tid], "a tree without the modules (not Sampo's) goes on as before")

    def test_a_check_already_running_at_the_tip_starts_no_module_run(self):  # T-4542
        tid = self.lane(1, "f1.txt", when=OLD)
        b = c.find_brief(self.p, tid)
        b.meta.update(check_state="running", check_tip=self.tip(1), check_pid=self.pid, check_ticks=train._ticks(self.pid))
        c.save_brief(self.p, b)
        self.accept(tid)
        self.shape_run.assert_not_called()

    def test_the_parser_takes_shape_green_on_a_hold_check(self):  # T-4563
        args = cli.build_parser().parse_args(["check", "--affected", "--hold", "--shape-green", "--evidence", "T-1"])
        self.assertTrue(args.shape_green)
        self.assertTrue(args.hold)


class ShapeQueued(AcceptCase):
    """T-4947: at most SAMPO_SHAPE_RUNS lanes' repo-shape stages run at once (default 3). T-4563 ran them before the
    turn with no cap: 15 lanes accepted together ran 15 at once (2026-11-17, load 76 on 16 cores). A further one waits
    as 'shape queued' and starts when one ends."""

    def setUp(self):
        super().setUp()
        stub_shape(self)
        env = mock.patch.dict(os.environ, {"SAMPO_SHAPE_RUNS": "1"})
        env.start()
        self.addCleanup(env.stop)
        one = mock.patch.object(train, "BATCH_MAX", 1)  # T-5380: no batch here: each lane runs its own stage (ShapeBatch)
        one.start()
        self.addCleanup(one.stop)

    def test_three_lanes_with_one_slot_run_their_shape_stages_one_at_a_time_and_say_shape_queued(self):
        tids = [self.a_lane(n, name=f"f{n}.txt") for n in (1, 2, 3)]
        for tid in tids:
            self.accept(tid)
        d = os.path.join(self.p.dir, "check")
        os.makedirs(d, exist_ok=True)
        held = open(os.path.join(d, "shape-0.lock"), "a")  # the one slot is another lane's shape stage
        fcntl.flock(held, fcntl.LOCK_EX)
        self.addCleanup(held.close)
        running, peak, lock = [0], [0], threading.Lock()

        def shape(path):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.2)
            with lock:
                running[0] -= 1
            return "green", ""
        self.shape_run.side_effect = shape
        threads = [threading.Thread(target=self.child, args=(tid, n, stub(0)), daemon=True)
                   for n, tid in zip((1, 2, 3), tids)]
        for t in threads:
            t.start()
        notes = lambda: train.check_notes(c.load_briefs(self.p), self.repo)
        end = time.time() + 30
        while time.time() < end and not all(notes().get(t) == "shape queued" for t in tids):
            time.sleep(0.05)
        self.assertEqual([notes().get(t) for t in tids], ["shape queued"] * 3, "sampo lane list says shape queued")
        self.assertIn("shape queued", train._check_hold(c.find_brief(self.p, tids[0]), self.tip(1), self.repo))
        held.close()
        for t in threads:
            t.join(60)
        self.assertEqual(self.shape_run.call_count, 3)
        self.assertEqual(peak[0], 1, "two shape stages ran at once with one slot")
        self.assertEqual([self.meta_of(t)["check_state"] for t in tids], ["green"] * 3)

    def test_the_slot_count_defaults_to_three_and_reads_the_env(self):
        self.assertEqual(train.shape_runs(), 1)
        with mock.patch.dict(os.environ, {"SAMPO_SHAPE_RUNS": ""}):
            self.assertEqual(train.shape_runs(), 3)
        with mock.patch.dict(os.environ, {"SAMPO_SHAPE_RUNS": "x"}):
            self.assertEqual(train.shape_runs(), 3)
        with mock.patch.dict(os.environ, {"SAMPO_SHAPE_RUNS": "0"}):
            self.assertEqual(train.shape_runs(), 1)


class ShapeBatch(AcceptCase):
    """T-5380: the lane that takes a shape slot runs the repo-shape modules once for itself and the lanes waiting as
    'shape queued' (their branches merged on a scratch copy of the trunk, as T-4528 does for the long check). Green marks
    every member's check_shape at its tip, so each skips its own stage on taking a slot; red or cut off marks nothing
    green and each lane runs its own (a solo mark keeps the others from batching again); a lane that conflicts is left
    out; a lane whose tip moved is judged by none of it. The module run is stubbed: it sees the tree it runs in."""

    def setUp(self):
        super().setUp()
        self.ran = []  # (the folder the modules ran in, the files at its top)
        self.walls = []  # the wall each run was asked for (None: the stage's own)
        self.verdict = ("green", "")
        self.red_on = set()  # a run whose tree holds one of these files is red
        self.during = None  # called once, inside the first run
        shape = mock.patch.object(train, "_shape_run", side_effect=self._shape)
        shape.start()
        self.addCleanup(shape.stop)
        tree = mock.patch.object(train, "_shape_tree", return_value=True)
        tree.start()
        self.addCleanup(tree.stop)

    def _shape(self, path, wall=None):
        files = set(os.listdir(path))
        self.ran.append((path, files))
        self.walls.append(wall)
        if self.during:
            hook, self.during = self.during, None
            hook()
        if files & self.red_on:
            return "red", "test_docs.PluginFiles"
        return self.verdict

    def waiting(self, *ns, files=None, texts=None, queued="shape"):
        """Lanes accepted (their checks claimed, no process behind them), each waiting for a shape slot: the ids."""
        ids = []
        for n in ns:
            tid = self.lane(n, (files or {}).get(n, f"f{n}.txt"), text=(texts or {}).get(n), when=OLD)
            self.accept(tid)
            cli.mutate(self.p, tid, lambda b: b.meta.update(check_queued=queued), "check", {})
            ids.append(tid)
        return ids

    def move(self, n):
        lane = os.path.join(self.tmp, f"lane{n}")
        with open(os.path.join(lane, "more.txt"), "w") as f:
            f.write("more\n")
        self.git("add", "more.txt", cwd=lane)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "more", cwd=lane)

    def shaped(self, *ids):
        return [self.meta_of(t).get("check_shape") for t in ids]

    def scratch_trees(self):
        return [x for x in self.git("worktree", "list", "--porcelain").splitlines() if x.startswith("worktree ")
                and not os.path.basename(x).startswith(("lane", os.path.basename(self.repo)))]

    def test_waiting_lanes_are_cleared_by_one_run_on_a_merged_tree_and_every_member_is_marked(self):  # T-5380
        ids = self.waiting(1, 2, 3)
        self.assertEqual(self.child(ids[0], 1, stub(0)), 0)
        self.assertEqual(len(self.ran), 1, "one module run for three lanes")
        path, files = self.ran[0]
        self.assertNotEqual(os.path.basename(path), "lane1", "a scratch tree, not one lane's")
        self.assertTrue({"f1.txt", "f2.txt", "f3.txt"} <= files, f"the union of the three lanes' changes: {sorted(files)}")
        self.assertEqual(self.shaped(*ids), [self.tip(1), self.tip(2), self.tip(3)])
        for tid in ids[1:]:
            self.assertEqual(self.meta_of(tid)["check_state"], "running", "a member's own check is not judged by it")
            self.assertIn("batch of 3", self.brief(tid).section("Log"))
        self.assertEqual(self.scratch_trees(), [], "the scratch tree is removed")

    def test_a_cleared_lane_runs_no_module_when_it_takes_its_slot(self):  # T-5380
        ids = self.waiting(1, 2, 3)
        self.child(ids[0], 1, stub(0))
        with mock.patch.object(train, "_shape_wanted", return_value=True):  # its process asked before the batch cleared it
            with mock.patch.object(train, "_run_check", wraps=train._run_check) as long_check:
                self.assertEqual(self.child(ids[1], 2, stub(0)), 0)
        self.assertEqual(len(self.ran), 1, "the batch's run was the only one")
        self.assertIn("--shape-green", long_check.call_args[0][0], "the long check runs without the classes")
        self.assertEqual(self.child(ids[2], 3, stub(0)), 0)
        self.assertEqual(len(self.ran), 1)
        self.assertEqual([self.meta_of(t)["check_state"] for t in ids], ["green"] * 3)

    def test_a_red_batch_marks_nothing_green_and_each_lane_runs_its_own_stage_once(self):  # T-5380
        ids = self.waiting(1, 2, 3)
        self.red_on = {"f2.txt"}
        self.assertEqual(self.child(ids[0], 1, stub(0)), 0)  # the batch: red; then lane 1's own stage: green
        self.assertEqual(len(self.ran), 2)
        self.assertEqual(os.path.basename(self.ran[1][0]), "lane1")
        self.assertEqual(self.shaped(*ids), [self.tip(1), None, None], "a red batch clears no one")
        self.assertEqual(self.meta_of(ids[1])["check_shape_solo"], self.tip(2))
        self.assertEqual(self.child(ids[1], 2, stub(0)), 1)  # lane 2's own stage names its red
        self.assertEqual(self.meta_of(ids[1])["check_state"], "red")
        self.assertEqual(self.child(ids[2], 3, stub(0)), 0)
        self.assertEqual(len(self.ran), 4, "the others run their own stage and none batches again")
        self.assertEqual(self.shaped(ids[2]), [self.tip(3)])
        self.assertEqual(self.scratch_trees(), [])

    def test_a_cut_off_batch_marks_nothing_and_each_lane_runs_its_own_stage(self):  # T-5380
        ids = self.waiting(1, 2)
        self.verdict = ("cut", "")
        self.child(ids[0], 1, stub(0))
        self.assertEqual(len(self.ran), 2, "the batch, then lane 1's own")
        self.assertEqual(self.shaped(*ids), [None, None])
        self.child(ids[1], 2, stub(0))
        self.assertEqual(len(self.ran), 3, "lane 2's own stage, no second batch")

    def test_a_conflicting_lane_is_left_out_of_the_batch(self):  # T-5380
        ids = self.waiting(1, 2, 3, files={2: "same.txt", 3: "same.txt"}, texts={2: "two\n", 3: "three\n"})
        self.child(ids[0], 1, stub(0))
        self.assertEqual(len(self.ran), 1)
        self.assertTrue({"f1.txt", "same.txt"} <= self.ran[0][1])
        self.assertEqual(self.shaped(*ids), [self.tip(1), self.tip(2), None], "lane 3 conflicts with lane 2")
        self.child(ids[2], 3, stub(0))
        self.assertEqual(len(self.ran), 2, "lane 3 runs its own stage in its lane")
        self.assertEqual(os.path.basename(self.ran[1][0]), "lane3")

    def test_every_peer_conflicting_means_no_batch(self):  # T-5380
        ids = self.waiting(1, 2, files={1: "same.txt", 2: "same.txt"}, texts={1: "one\n", 2: "two\n"})
        self.child(ids[0], 1, stub(0))
        self.assertEqual([os.path.basename(x[0]) for x in self.ran], ["lane1"])
        self.assertEqual(self.shaped(*ids), [self.tip(1), None])

    def test_a_lane_whose_tip_moved_is_not_in_the_batch_and_judged_by_none_of_it(self):  # T-5380
        ids = self.waiting(1, 2, 3)
        old2 = self.tip(2)
        self.move(2)
        self.child(ids[0], 1, stub(0))
        self.assertNotIn("f2.txt", self.ran[0][1])
        self.assertEqual(self.shaped(*ids), [self.tip(1), None, self.tip(3)])
        self.assertEqual(train._accept_check_child({"project": [self.p.slug, self.p.root, self.p.dir, self.p.lane],
                                                    "main": self.repo, "id": ids[1], "tip": old2, "tries": 1,
                                                    "argv": stub(0), "since": None}), 0)
        self.assertEqual(len(self.ran), 1, "the old tip's process runs no module")
        self.assertNotEqual(self.meta_of(ids[1]).get("check_state"), "green")

    def test_a_tip_that_moves_during_the_run_is_not_marked(self):  # T-5380
        ids = self.waiting(1, 2, 3)
        self.during = lambda: self.move(3)
        self.child(ids[0], 1, stub(0))
        self.assertEqual(self.shaped(*ids), [self.tip(1), self.tip(2), None])

    def test_a_peer_that_took_a_slot_meanwhile_or_is_not_waiting_for_one_is_not_marked(self):  # T-5380
        ids = self.waiting(1, 2) + self.waiting(3, queued="1") + self.waiting(4, queued=None)
        self.during = lambda: cli.mutate(self.p, ids[1], lambda b: b.meta.pop("check_queued", None), "x", {})
        self.child(ids[0], 1, stub(0))
        self.assertEqual(self.shaped(*ids), [self.tip(1), None, None, None])
        self.assertNotIn("f3.txt", self.ran[0][1], "a lane waiting for the long check's turn is not a shape peer")

    def test_a_retried_peer_or_one_already_cleared_is_not_a_peer(self):  # T-5380
        ids = self.waiting(1, 2)
        cli.mutate(self.p, ids[1], lambda b: b.meta.update(check_tries=2), "x", {})
        self.child(ids[0], 1, stub(0))
        self.assertEqual([os.path.basename(x[0]) for x in self.ran], ["lane1"])

    def test_the_batch_is_capped(self):  # T-5380
        n = train.BATCH_MAX + 2
        ids = self.waiting(*range(1, n + 1))
        self.child(ids[0], 1, stub(0))
        self.assertEqual(len([x for x in self.ran[0][1] if x.startswith("f") and x.endswith(".txt")]), train.BATCH_MAX)
        self.assertEqual(len([s for s in self.shaped(*ids) if s]), train.BATCH_MAX)
        self.child(ids[-1], n, stub(0))  # the rest batch among themselves
        self.assertEqual(len(self.ran), 2)
        self.assertTrue(all(self.shaped(*ids)))

    def test_a_lane_marked_solo_runs_its_own_stage_and_leads_no_batch(self):  # T-5380
        ids = self.waiting(1, 2)
        cli.mutate(self.p, ids[0], lambda b: b.meta.update(check_shape_solo=self.tip(1)), "x", {})
        self.child(ids[0], 1, stub(0))
        self.assertEqual([os.path.basename(x[0]) for x in self.ran], ["lane1"])
        self.assertEqual(self.shaped(*ids), [self.tip(1), None])

    def peers_of(self, tid, n):
        return [x.id for x, _ in train._shape_peers(self.p, self.repo, tid, self.tip(n))]

    def test_a_second_leader_does_not_batch_the_peers_a_running_leader_holds(self):  # T-5380 review
        ids = self.waiting(1, 2, 3) + self.waiting(4, queued=None)  # lane 4's process took a second slot: it leads too
        cli.mutate(self.p, ids[0], lambda b: b.meta.pop("check_queued"), "x", {})  # the first leader holds its slot
        seen = []

        def second():  # a second slot's leader asks for its peers while the first one's run is going
            seen.append(self.peers_of(ids[3], 4))
            seen.append(self.meta_of(ids[1]).get("check_shape_lead"))
        self.during = second
        self.child(ids[0], 1, stub(0))
        self.assertEqual(seen[0], [], "lanes 2 and 3 are claimed by the first leader")
        self.assertTrue(seen[1].startswith(ids[0] + ":"), seen[1])
        self.assertEqual(self.shaped(*ids), [self.tip(1), self.tip(2), self.tip(3), None])
        for tid in ids:
            self.assertNotIn("check_shape_lead", self.meta_of(tid), "the claim is dropped when the batch ends")
        self.assertEqual(self.peers_of(ids[3], 4), [], "cleared lanes are no peers either")

    def test_a_red_batch_drops_its_claims(self):  # T-5380 review
        ids = self.waiting(1, 2)
        self.verdict = ("red", "test_docs.PluginFiles")
        self.child(ids[0], 1, stub(0))
        for tid in ids:
            self.assertNotIn("check_shape_lead", self.meta_of(tid))

    def test_a_claim_of_a_dead_leader_is_no_claim(self):  # T-5380 review
        ids = self.waiting(1, 2)
        cli.mutate(self.p, ids[1], lambda b: b.meta.update(check_shape_lead="T-9:4194999:1"), "x", {})
        self.assertEqual(self.peers_of(ids[0], 1), [ids[1]])
        live = f"T-9:{os.getpid()}:{train._ticks(os.getpid())}"
        cli.mutate(self.p, ids[1], lambda b: b.meta.update(check_shape_lead=live), "x", {})
        self.assertEqual(self.peers_of(ids[0], 1), [])
        self.assertEqual(set(self.peers_of("T-9", 1)), set(ids), "the claiming leader still sees its own claims")

    def test_the_batch_run_gets_a_wall_of_its_own_and_a_lanes_own_stage_the_default(self):  # T-5380 review
        self.assertEqual(train.SHAPE_BATCH_WALL_S, 2 * train.SHAPE_WALL_S)
        ids = self.waiting(1, 2)
        self.child(ids[0], 1, stub(0))
        self.assertEqual(self.walls, [train.shape_batch_wall()])
        self.assertGreater(train.shape_batch_wall(), train.SHAPE_WALL_S)
        with mock.patch.dict(os.environ, {"SAMPO_SHAPE_BATCH_WALL_S": "77"}):
            self.assertEqual(train.shape_batch_wall(), 77)
        for bad in ("x", "0", "-5"):
            with mock.patch.dict(os.environ, {"SAMPO_SHAPE_BATCH_WALL_S": bad}):
                self.assertEqual(train.shape_batch_wall(), train.SHAPE_BATCH_WALL_S, bad)
        self.verdict = ("cut", "")
        ids = self.waiting(3, 4)
        self.walls.clear()
        self.child(ids[0], 3, stub(0))
        self.assertEqual(self.walls, [train.shape_batch_wall(), None], "the batch's wall, then the lane's own stage's default")

    def test_a_cleared_lane_leaves_the_shape_queue_at_once_and_goes_to_its_long_check(self):  # T-5380 review
        ids = self.waiting(1, 2)
        env = mock.patch.dict(os.environ, {"SAMPO_SHAPE_RUNS": "1"})
        env.start()
        self.addCleanup(env.stop)
        d = os.path.join(self.p.dir, "check")
        os.makedirs(d, exist_ok=True)
        held = open(os.path.join(d, "shape-0.lock"), "a")  # the one slot is another lane's stage
        fcntl.flock(held, fcntl.LOCK_EX)
        self.addCleanup(held.close)
        w = mock.patch.object(train, "_shape_wanted", return_value=True)  # its process asked before a batch cleared it
        w.start()
        self.addCleanup(w.stop)
        t = threading.Thread(target=self.child, args=(ids[1], 2, stub(0)), daemon=True)
        t.start()
        time.sleep(1.5)  # the child polls the held slot
        self.assertTrue(t.is_alive())
        cli.mutate(self.p, ids[1], lambda b: b.meta.update(check_shape=self.tip(2)), "x", {})  # a batch cleared it
        t.join(30)
        self.assertFalse(t.is_alive(), "it waited on for a slot that stayed held")
        self.assertEqual(self.ran, [], "no module run")
        self.assertEqual(self.meta_of(ids[1])["check_state"], "green")
        self.assertIsNone(self.meta_of(ids[1]).get("check_queued"))


class CheckTurnUntil(AcceptCase):
    """T-5380 review: _check_turn takes an `until` predicate polled with the slot wait; true, the wait ends with no slot."""

    def hold(self):
        d = os.path.join(self.p.dir, "check")
        os.makedirs(d, exist_ok=True)
        held = open(os.path.join(d, "shape-0.lock"), "a")
        fcntl.flock(held, fcntl.LOCK_EX)
        self.addCleanup(held.close)

    def test_until_ends_the_wait_for_a_held_slot_and_says_it_started(self):
        self.hold()
        calls = []
        polls = iter([False, False, True])
        with mock.patch.object(train, "TURN_POLL_S", 0.01):
            with train._check_turn(self.p, lambda: calls.append("waits"), lambda: calls.append("started"), name="shape",
                                   n=1, until=lambda: next(polls)) as got:
                self.assertFalse(got, "no slot was taken")
        self.assertEqual(calls, ["waits", "started"])

    def test_a_free_slot_is_taken_and_until_is_never_asked(self):
        with train._check_turn(self.p, name="shape", n=1, until=lambda: self.fail("asked")) as got:
            self.assertTrue(got)

    def test_with_no_until_the_turn_yields_a_slot(self):
        with train._check_turn(self.p, name="shape", n=1) as got:
            self.assertTrue(got)


if __name__ == "__main__":
    unittest.main()
