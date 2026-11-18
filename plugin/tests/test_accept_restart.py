"""T-4404: sampo lane accept starts a held check again. A check cut off (or without a result) twice at its tip is
started once more as try 1 by an explicit accept; a red one only when the checker's code (cli.py, train.py, core.py,
map.py: the stamp the check's process records before its run) differs from the one recorded when it ran. At most
RESTART_MAX restarts per tip, none while other lanes' checks wait for a slot, and a restart that does not start keeps
the held record. The land never restarts one: it leaves the lane out and says which accept will. The detached start is
replaced as in test_accept_check: none of these starts a real check."""
# map-links: plugin/lib/sampo/train.py
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from test_accept_check import AcceptCase, stub

from sampo import cli
from sampo import core as c
from sampo import train

OTHER = "a-checker-that-changed"


class Restart(AcceptCase):
    def cut_off_twice(self, tid):
        self.accept(tid)
        self.child(tid, 1, stub(124, cut_off=True))
        self.child(tid, 1, stub(124, cut_off=True), tries=2)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        self.assertEqual(len(self.spawned), 2)

    def red(self, tid):
        self.accept(tid)
        self.child(tid, 1, stub(1, failed=["test_docs.PluginFiles"]))
        self.assertEqual(self.meta_of(tid)["check_state"], "red")
        self.assertEqual(len(self.spawned), 1)


class CutOff(Restart):
    def test_cut_off_twice_then_accept_starts_the_check_again_as_try_1_and_says_so(self):  # cut_off
        tid = self.a_lane(1)
        self.cut_off_twice(tid)
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 3, out)
        self.assertEqual(self.spawned[2]["tries"], 1)
        self.assertEqual(self.spawned[2]["tip"], self.tip(1))
        self.assertIn("again", out)
        self.assertIn("cut off or ended without a result twice", out)
        m = self.meta_of(tid)
        self.assertEqual((m["check_state"], m["check_tries"]), ("running", "1"))
        self.assertEqual(self.queued(), [])

    def test_cut_off_then_restarted_gets_its_two_tries_again(self):  # cut_off
        tid = self.a_lane(1)
        self.cut_off_twice(tid)
        self.accept(tid)
        self.child(tid, 1, stub(124, cut_off=True))
        self.assertEqual(len(self.spawned), 4, "the restart's own cut-off is started once more by its process")
        self.assertEqual(self.spawned[3]["tries"], 2)

    def test_a_second_accept_while_the_restart_runs_starts_none_cut_off(self):  # cut_off
        tid = self.a_lane(1)
        self.cut_off_twice(tid)
        self.accept(tid)
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 3, out)
        self.assertIn("check is running", out)

    def test_a_check_with_no_result_twice_is_restarted_by_accept_cut_off(self):  # cut_off
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, [sys.executable, "-c", "print('boom')"], tries=2)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 2, out)
        self.assertEqual(self.spawned[1]["tries"], 1)

    def test_the_land_leaves_a_cut_off_lane_out_and_starts_no_check_cut_off(self):  # cut_off
        tid = self.a_lane(1)
        self.cut_off_twice(tid)
        r = self.land(check=False)
        self.assertIn(f"{tid}: held: its check was cut off or ended without a result twice", r.stdout + r.stderr)
        self.assertIn(f"sampo lane accept {tid}", r.stdout + r.stderr, "the hold says which accept restarts it")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "f1.txt")), "left out, not trained")
        self.assertEqual(len(self.spawned), 2, "the land started no check")
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")

    def test_a_restart_not_started_keeps_the_cut_off_record_and_the_land_still_leaves_it_out(self):  # cut_off
        tid = self.a_lane(1)
        self.cut_off_twice(tid)
        with mock.patch.object(train, "_spawn_check", return_value=None):
            out = self.accept(tid)
        self.assertIn("check not started", out)
        m = self.meta_of(tid)
        self.assertEqual((m["check_state"], m["check_tries"], m["check_tip"]), ("cut_off", "2", self.tip(1)),
                         "the claim is given back with the record as it was: a paused accept must not lose the hold")
        self.assertNotIn("check_restarts", m, "a restart that did not start is not counted")
        r = self.land(check=False)
        self.assertIn(f"{tid}: held: its check was cut off or ended without a result twice", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "f1.txt")), "left out, not trained")

    def test_a_dead_second_try_is_started_again_as_try_1_in_its_own_words(self):  # dead
        tid = self.a_lane(1)
        self.accept(tid)
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_ticks=1, check_tries="2"), "x", {})
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 2, out)
        self.assertEqual(self.spawned[1]["tries"], 1)
        self.assertIn("its check ended without a result twice", out)
        self.assertEqual(self.meta_of(tid)["check_tries"], "1")

    def test_a_restart_is_counted_per_tip_and_stops_at_the_cap_with_the_hand_command(self):  # cap
        tid = self.a_lane(1)
        self.cut_off_twice(tid)
        for n in (1, 2):
            out = self.accept(tid)
            self.assertIn(f"restart {n} of {train.RESTART_MAX}", out)
            self.assertEqual(self.meta_of(tid)["check_restarts"], f"{self.tip(1)}:{n}")
            self.child(tid, 1, stub(124, cut_off=True))
            self.child(tid, 1, stub(124, cut_off=True), tries=2)
            self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        spawned = len(self.spawned)
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), spawned, out)
        self.assertIn(f"run sampo check --affected --hold --evidence {tid} in the lane", out)
        self.assertIn("2 times", out)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")

    def test_the_restart_count_is_dropped_when_the_tip_moves(self):  # cap
        tid = self.a_lane(1)
        self.cut_off_twice(tid)
        self.accept(tid)
        self.assertEqual(self.meta_of(tid)["check_restarts"], f"{self.tip(1)}:1")
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_state="green"), "x", {})  # a check that ended
        lane = os.path.join(self.tmp, "lane1")
        with open(os.path.join(lane, "more.txt"), "w") as f:
            f.write("x\n")
        self.git("add", "more.txt", cwd=lane)
        self.git("commit", "-m", "more", cwd=lane)
        self.accept(tid)  # a first check at the new tip
        m = self.meta_of(tid)
        self.assertEqual(m["check_tip"], self.tip(1))
        self.assertNotIn("check_restarts", m)

    def test_a_failed_try_2_start_keeps_the_tip_so_the_cut_off_still_holds_the_lane(self):  # dead
        tid = self.a_lane(1)
        self.accept(tid)
        with mock.patch.object(train, "_spawn_check", return_value=None):  # paused at the second try's start
            self.child(tid, 1, stub(124, cut_off=True))
        m = self.meta_of(tid)
        self.assertEqual((m["check_state"], m["check_tip"]), ("cut_off", self.tip(1)))
        self.assertIsNotNone(train._check_hold(self.brief(tid), self.tip(1), self.repo), "the land leaves the lane out")
        r = self.land(check=False)
        self.assertIn(f"{tid}: held: its check was cut off", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "f1.txt")), "left out, not trained")

    def test_a_pause_at_the_try_2_start_does_not_reset_the_restart_cap(self):  # cap
        tid = self.a_lane(1)
        self.cut_off_twice(tid)
        for n in (1, 2):
            self.assertIn(f"restart {n} of {train.RESTART_MAX}", self.accept(tid))
            with mock.patch.object(train, "_spawn_check", return_value=None):
                self.child(tid, 1, stub(124, cut_off=True))
            self.assertEqual(self.meta_of(tid)["check_restarts"], f"{self.tip(1)}:{n}")
        spawned = len(self.spawned)
        self.assertIn("2 times", self.accept(tid))
        self.assertEqual(len(self.spawned), spawned)

    def test_a_discarded_run_drops_check_tip_but_not_the_count_of_its_tip(self):  # cap
        tip = self.tip(1)
        self.assertEqual(train._restart_count({"check_restarts": f"{tip}:2"}, tip), 2)
        self.assertEqual(train._restart_count({"check_restarts": f"{tip}:2"}, "another"), 0)
        self.assertEqual(train._restart_count({"check_restarts": "garbage"}, tip), 0)
        self.assertEqual(train._restart_count({}, tip), 0)

    def test_accept_waits_while_other_lanes_checks_are_queued_and_says_when(self):  # queue
        tid, other = self.a_lane(1), self.a_lane(2)
        self.cut_off_twice(tid)
        self.accept(other)
        cli.mutate(self.p, other, lambda b: b.meta.update(check_queued="1"), "x", {})
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 3, "only the other lane's first start: no restart of this one")
        self.assertIn(f"sampo lane accept {tid} restarts it when the check queue is empty", out)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        cli.mutate(self.p, other, lambda b: b.meta.pop("check_queued", None), "x", {})
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 4, out)


class RedCheck(Restart):
    def test_a_red_check_is_started_again_when_the_checkers_code_changed(self):  # red
        tid = self.a_lane(1)
        self.red(tid)
        with mock.patch.object(train, "_checker_stamp", return_value=OTHER):
            out = self.accept(tid)
        self.assertEqual(len(self.spawned), 2, out)
        self.assertEqual(self.spawned[1]["tries"], 1)
        self.assertIn("again", out)
        self.assertIn("checker", out)
        m = self.meta_of(tid)
        self.assertEqual(m["check_state"], "running")
        self.assertNotIn("check_failed", m)
        self.assertEqual(self.queued(), [])

    def test_a_red_check_with_the_same_stamp_stays_held_in_the_same_words(self):  # red
        tid = self.a_lane(1)
        self.red(tid)
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 1, out)
        self.assertIn("held: check red: test_docs.PluginFiles; left out, no retry until the tip moves or a newer green run "
                      "holds the repo-shape classes", out)
        self.assertNotIn("again", out)

    def test_a_red_check_recorded_before_the_stamp_existed_is_started_again(self):  # red
        tid = self.a_lane(1)
        self.red(tid)
        cli.mutate(self.p, tid, lambda b: b.meta.pop("check_code", None), "x", {})
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 2, out)

    def test_the_check_process_records_the_stamp_just_before_its_run(self):  # red
        tid = self.a_lane(1)
        self.accept(tid)
        self.assertNotIn("check_code", self.meta_of(tid), "accept records none: the code that runs is the child's")
        with mock.patch.object(train, "_checker_stamp", return_value="s1"):
            self.child(tid, 1, stub(124, cut_off=True))
        self.assertEqual(self.meta_of(tid)["check_code"], "s1")
        with mock.patch.object(train, "_checker_stamp", return_value="s2"):
            self.child(tid, 1, stub(124, cut_off=True), tries=2)  # its own second try
        self.assertEqual(self.meta_of(tid)["check_code"], "s2")

    def test_check_notes_say_accept_again_for_a_red_check_only_when_the_checker_changed(self):  # red
        tid = self.a_lane(1)
        self.red(tid)
        note = train.check_notes([self.brief(tid)], self.repo)[tid]
        self.assertEqual(note, "check red: test_docs.PluginFiles")
        with mock.patch.object(train, "_checker_stamp", return_value=OTHER):
            note = train.check_notes([self.brief(tid)], self.repo)[tid]
        self.assertEqual(note, "check red: test_docs.PluginFiles (checker changed: accept again)")

    def test_the_land_leaves_a_red_lane_out_and_starts_no_check_even_when_the_stamp_changed(self):  # red
        tid = self.a_lane(1)
        self.red(tid)
        with mock.patch.object(train, "_checker_stamp", return_value=OTHER):
            r = self.land(check=False)
            note = train._check_hold(self.brief(tid), self.tip(1), self.repo)
        self.assertIn(f"{tid}: held: check red: test_docs.PluginFiles", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "f1.txt")), "left out, not trained")
        self.assertEqual(len(self.spawned), 1)
        self.assertIn(f"sampo lane accept {tid}", note, "a changed checker's hold names the accept that restarts it")

    def test_a_red_hold_with_the_same_stamp_does_not_promise_a_restart(self):  # red
        tid = self.a_lane(1)
        self.red(tid)
        self.assertNotIn("sampo lane accept", train._check_hold(self.brief(tid), self.tip(1), self.repo))


class ShapeRed(Restart):
    """The red the repo-shape stage writes carries the stamp too: a lane red there with no checker change stays held."""

    def setUp(self):
        super().setUp()
        for name, kw in (("_shape_run", {"return_value": ("red", "test_docs.PluginFiles, test_run.EnvClassified")}),
                         ("_shape_tree", {"return_value": True})):
            patch = mock.patch.object(train, name, **kw)
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_shape_red_is_held_in_the_old_words_when_the_checker_did_not_change(self):  # shape
        tid = self.a_lane(1)
        self.accept(tid)
        with mock.patch.object(train, "_run_check") as long_check:
            self.assertEqual(self.child(tid, 1, stub(0)), 1)
        long_check.assert_not_called()
        m = self.meta_of(tid)
        self.assertEqual(m["check_state"], "red")
        self.assertEqual(m["check_code"], train._checker_stamp(self.repo), "recorded by the stage, not by hand")
        for _ in range(2):
            out = self.accept(tid)
            self.assertEqual(len(self.spawned), 1, out)
            self.assertIn("held: check red: test_docs.PluginFiles test_run.EnvClassified; left out, no retry until the tip "
                          "moves", out)
            self.assertNotIn("checker's code has changed", out)
        self.assertNotIn("check_restarts", self.meta_of(tid))

    def test_a_shape_red_is_started_again_when_the_checker_changed_since(self):  # shape
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(0))
        with mock.patch.object(train, "_checker_stamp", return_value=OTHER):
            out = self.accept(tid)
        self.assertEqual(len(self.spawned), 2, out)
        self.assertIn("checker's code has changed", out)


class Stamp(unittest.TestCase):
    """The stamp is the content of the checker's own modules, not the mtime or any other module: a trunk commit that
    leaves the checker alone must not make a red check restartable."""

    FILES = ("lib/sampo/cli.py", "lib/sampo/train.py", "lib/sampo/core.py", "lib/sampo/map.py", "tests/run.py",
             "lib/sampo/smith.py")

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fill(self.root)
        patch = mock.patch.object(c, "PLUGIN_ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def fill(self, root, text="x = 1\n"):
        for rel in self.FILES:
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            self.write(rel, text, root)

    def write(self, rel, text, root=None):
        with open(os.path.join(root or self.root, rel), "w") as f:
            f.write(text)

    def test_same_content_same_stamp_even_when_touched(self):
        before = train._checker_stamp()
        os.utime(os.path.join(self.root, "lib/sampo/cli.py"), (1, 1))
        self.assertEqual(train._checker_stamp(), before)

    def test_another_module_changing_leaves_the_stamp(self):
        before = train._checker_stamp()
        self.write("lib/sampo/smith.py", "x = 2\n")
        self.assertEqual(train._checker_stamp(), before)

    def test_each_checker_module_changing_changes_the_stamp(self):
        seen = {train._checker_stamp()}
        for rel in ("lib/sampo/cli.py", "lib/sampo/train.py", "lib/sampo/core.py", "lib/sampo/map.py"):
            self.write(rel, f"x = '{rel}'\n")
            seen.add(train._checker_stamp())
        self.assertEqual(len(seen), 5)

    def test_the_test_runner_is_not_part_of_the_stamp(self):
        before = train._checker_stamp()
        self.write("tests/run.py", "x = 'the lane runs its own'\n")
        self.assertEqual(train._checker_stamp(), before)

    def test_a_missing_module_is_a_stamp_not_a_crash(self):
        os.remove(os.path.join(self.root, "lib/sampo/map.py"))
        self.assertTrue(train._checker_stamp())

    def test_the_stamp_comes_from_the_trunks_plugin_not_the_callers(self):
        trunk = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, trunk, ignore_errors=True)
        os.makedirs(os.path.join(trunk, "plugin"))
        self.fill(os.path.join(trunk, "plugin"), "x = 'trunk'\n")
        self.assertNotEqual(train._checker_stamp(trunk), train._checker_stamp(), "a lane's own copy is not the trunk's")
        self.write("lib/sampo/cli.py", "x = 'the lane changed it'\n")
        self.assertEqual(train._checker_stamp(trunk), train._checker_stamp(trunk))
        before = train._checker_stamp(trunk)
        self.write("lib/sampo/train.py", "x = 'and again'\n")
        self.assertEqual(train._checker_stamp(trunk), before, "the caller's plugin root does not move the trunk's stamp")

    def test_a_main_without_a_plugin_folder_falls_back_to_this_process(self):
        bare = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bare, ignore_errors=True)
        self.assertEqual(train._checker_stamp(bare), train._checker_stamp())

    def test_the_files_are_read_once_until_they_change(self):
        train._checker_stamp()
        with mock.patch("builtins.open", side_effect=AssertionError("read again")):
            train._checker_stamp()
        self.write("lib/sampo/cli.py", "x = 'changed, and longer'\n")
        self.assertTrue(train._checker_stamp())


if __name__ == "__main__":
    unittest.main()
