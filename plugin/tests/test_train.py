"""T-4851: the lane check's log. `check started ... log check/ID.log` named a file the check process's own stdout was
(sampo lane accept starts it so), but _run_check held the check's output in memory and printed none of it: every such log
was 0 bytes (T-4147, T-4139, T-4189), and a check cut off at its deadline (60 of 1546 classes in 1200 s) left nothing to
read. Now the check's lines are written to that stdout as they arrive, the result is still read from the same text, and
a hold for a cut-off check names the log."""
# map-links: plugin/lib/sampo/train.py
import ast
import contextlib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import test_lane_land
from helpers import ForemanTestCase
from test_accept_check import AcceptCase, stub
from test_express_lane import ExpressBase
from test_lane_train import AffectedBase, TrainCase
from test_postmerge import PmCase

from sampo import cli
from sampo import core as c
from sampo import lanes as slanes
from sampo import map as smap
from sampo import train

LIB = os.path.dirname(os.path.dirname(os.path.abspath(train.__file__)))

# the process sampo lane accept starts: its stdout is the log file; it runs one check and leaves what _run_check gave back
RUNNER = (
    "import json, sys\n"
    "from sampo import train\n"
    "wall, out, cwd, *argv = sys.argv[1:]\n"
    "train.CHECK_WALL_S = float(wall)\n"
    "r, timed_out = train._run_check(argv, cwd)\n"
    "json.dump({'stdout': r.stdout, 'rc': r.returncode, 'timed_out': timed_out, 'result': train._result_of(r)}, open(out, 'w'))\n"
)
# a check that prints a class line, waits for the test to have seen it in the log, then prints the rest and its result
LINES = (
    "import json, os, sys, time\n"
    "print('class test_a.A ok', flush=True)\n"
    "end = time.time() + 60\n"
    "while not os.path.exists(sys.argv[1]) and time.time() < end:\n"
    "    time.sleep(0.05)\n"
    "print('class test_b.B ok', flush=True)\n"
    "print(json.dumps({'exit': 0, 'failed': [], 'cut_off': False, 'tests': [], 'changed': []}, indent=2))\n"
)
HANG = "import time\nprint('class test_a.A ok', flush=True)\nprint('class test_b.B ok', flush=True)\ntime.sleep(600)\n"


def read(path):
    with open(path) as f:
        return f.read()


class CheckLog(AcceptCase):
    def runner(self, argv, wall=300):
        """_run_check in a process of its own whose stdout is a log file, as the detached check's is: (Popen, log, result file)."""
        log, res = os.path.join(self.tmp, "check-run.log"), os.path.join(self.tmp, "check-run.json")
        with open(log, "w") as out:
            proc = subprocess.Popen([sys.executable, "-c", RUNNER, str(wall), res, self.repo, *argv], stdout=out,
                                    stderr=subprocess.STDOUT, env={**os.environ, "PYTHONPATH": LIB})
        self.addCleanup(proc.kill)
        return proc, log, res

    def until(self, done, seconds=30):
        end = time.time() + seconds
        while time.time() < end:
            if done():
                return True
            time.sleep(0.05)
        return False

    def test_check_log_lines_arrive_while_the_check_runs_and_the_result_is_read_as_before(self):  # check_log_lines
        gate = os.path.join(self.tmp, "gate")
        proc, log, res = self.runner([sys.executable, "-c", LINES, gate])
        seen = self.until(lambda: "class test_a.A ok" in read(log))
        text = read(log)
        open(gate, "w").close()
        self.assertEqual(proc.wait(timeout=60), 0)
        self.assertTrue(seen, "the first class line is in the log before the check ends")
        self.assertNotIn("class test_b.B ok", text, "the second line came after the gate: the first was read while it ran")
        done = read(log)
        self.assertIn("class test_a.A ok", done)
        self.assertIn("class test_b.B ok", done)
        self.assertIn('"exit": 0', done, "the result is in the log too")
        got = json.loads(read(res))
        self.assertEqual((got["rc"], got["timed_out"]), (0, False))
        self.assertEqual(got["result"]["exit"], 0, "_check_result's text is the whole output, as before")
        self.assertIn("class test_a.A ok", got["stdout"])

    def test_check_log_lines_an_empty_or_unterminated_last_line_is_kept(self):  # check_log_lines
        code = "import sys; sys.stdout.write('no newline at the end')"
        proc, log, res = self.runner([sys.executable, "-c", code])
        self.assertEqual(proc.wait(timeout=60), 0)
        self.assertEqual(read(log), "no newline at the end")
        self.assertEqual(json.loads(read(res))["stdout"], "no newline at the end")

    def test_check_log_cut_a_check_killed_at_its_deadline_leaves_its_lines_in_the_log(self):  # check_log_cut
        proc, log, res = self.runner([sys.executable, "-c", HANG], wall=3)
        t0 = time.time()
        self.assertEqual(proc.wait(timeout=90), 0)
        self.assertLess(time.time() - t0, 60, "the reader did not hold the runner past the kill")
        got = json.loads(read(res))
        self.assertTrue(got["timed_out"])
        self.assertEqual(got["rc"], 124)
        done = read(log)
        self.assertIn("class test_a.A ok", done)
        self.assertIn("class test_b.B ok", done)
        self.assertIn("class test_b.B ok", got["stdout"])

    def test_check_log_cut_the_hold_for_a_check_cut_off_twice_names_the_log(self):  # check_log_cut
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(124, cut_off=True))
        self.child(tid, 1, stub(124, cut_off=True), tries=2)
        log = os.path.join(self.tmp, "check.log")
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        self.assertIn(log, train._check_hold(self.brief(tid), self.tip(1), self.repo))
        self.assertIn(log, self.accept(tid))

    def test_check_log_cut_a_check_that_ended_without_a_result_names_the_log(self):  # check_log_cut
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, [sys.executable, "-c", "print('boom')"], tries=2)
        said = [x for x in self.brief(tid).section("Log").splitlines() if "no result" in x]
        self.assertTrue(said and os.path.join(self.tmp, "check.log") in said[-1], said)


class CullAllRed(unittest.TestCase):
    """T-5880: a class red in every lane's subset tree is no lane's: the cull reruns it once on the base alone and parks no
    lane for it (the 10:05 land parked 16 of 16 lanes for test_fast_equiv.Passthrough, a flake under load)."""

    @staticmethod
    def shape(extra=None):
        """Every subset is red on test_x.K; `extra` {lane: class} adds a red class when that lane is in the subset."""
        def run(sel, fresh=False):
            names = ["test_x.K"] + [v for k, v in (extra or {}).items() if k in sel]
            return train.ShapeVerdict("red", ", ".join(names), 1.0)
        return run

    def cull(self, rerun, ids=("a", "b", "c"), extra=None):
        asked, notes = [], []

        def base_rerun(names):
            asked.append(names)
            return train.ShapeVerdict(rerun, names if rerun == "red" else "", 1.0)
        got = train._shape_cull(list(ids), self.shape(extra), base_rerun=base_rerun, notes=notes)
        return got, asked, notes

    def test_cull_all_red_a_base_that_is_red_on_the_rerun_parks_no_lane_and_names_the_class(self):
        got, asked, notes = self.cull("red")
        self.assertEqual(got, {}, "nothing parked: the class is red without any lane")
        self.assertEqual(asked, ["test_x.K"], "one rerun of the class, on the base")
        self.assertEqual(notes, [("test_x.K", "red", 3)])

    def test_cull_all_red_a_base_that_is_green_on_the_rerun_is_a_flake_and_parks_no_lane_either(self):
        got, asked, notes = self.cull("green")
        self.assertEqual(got, {})
        self.assertEqual(asked, ["test_x.K"])
        self.assertEqual(notes, [("test_x.K", "green", 3)])

    def test_cull_all_red_a_lane_with_a_red_class_of_its_own_stays_parked_for_that_one_only(self):
        got, asked, notes = self.cull("green", extra={"b": "test_y.M"})
        self.assertEqual(list(got), ["b"])
        self.assertEqual(got["b"][1], "test_y.M")
        self.assertNotIn("test_x.K", got["b"][0])
        self.assertEqual(asked, ["test_x.K"])

    def test_cull_all_red_a_class_red_in_only_some_lanes_trees_runs_no_base_rerun(self):
        asked, notes = [], []

        def shape(sel, fresh=False):
            return train.ShapeVerdict("red", "test_x.K", 1.0) if "b" in sel else train.ShapeVerdict("green", "", 1.0)
        got = train._shape_cull(["a", "b", "c"], shape, base_rerun=lambda n: asked.append(n), notes=notes)
        self.assertEqual(list(got), ["b"])
        self.assertEqual((asked, notes), ([], []))

    def test_cull_all_red_no_base_rerun_given_parks_as_before_and_a_cut_rerun_parks_nothing(self):
        self.assertEqual(sorted(train._shape_cull(["a", "b"], self.shape())), ["a", "b"])
        with self.assertRaises(train.c.PolicyError) as e:
            train._shape_cull(["a", "b"], self.shape(), base_rerun=lambda n: train.ShapeVerdict("cut", "", 1.0))
        self.assertIn("no lane parked", str(e.exception))


class ShapeKnownRed(unittest.TestCase):
    """T-5982: a repo-shape class red on the base is forgiven (T-5805), and one red in every lane's tree dropped (T-5880),
    only when no lane changed or names its module; unknown own (None) forgives nothing."""

    def live(self, own, merged="test_x.K, test_y.M", base=("test_x.K",)):
        with mock.patch.object(train, "_shape_tree", return_value=True), \
                mock.patch.object(train, "_shape_run", return_value=("red", merged)), \
                mock.patch.object(train, "_base_shape_red", return_value=list(base)), \
                mock.patch.object(train, "_trunk_red_capture") as cap:
            return train._live_shape("wt", object(), "main", "base", own=own), cap

    def test_a_base_red_class_is_forgiven_when_no_lane_owns_its_module(self):
        got, cap = self.live([], merged="test_x.K")
        self.assertEqual(got.verdict, "green")
        cap.assert_called_once()

    def test_a_base_red_class_in_a_module_a_lane_changed_is_that_lanes_red(self):
        got, cap = self.live(["test_x"], merged="test_x.K")
        self.assertEqual((got.verdict, got.names), ("red", "test_x.K"))
        got, _ = self.live(["test_x"])
        self.assertEqual(got.names, "test_x.K, test_y.M", "the owned base-red class stays beside the new one")
        cap.assert_not_called()

    def test_an_unknown_own_forgives_nothing_and_a_callable_is_asked_only_for_a_red_tree(self):
        self.assertEqual(self.live(None, merged="test_x.K")[0].verdict, "red")
        asked = []
        self.assertEqual(self.live(lambda: asked.append(1) or [], merged="test_x.K")[0].verdict, "green")
        self.assertEqual(asked, [1])
        with mock.patch.object(train, "_shape_tree", return_value=True), \
                mock.patch.object(train, "_shape_run", return_value=("green", "")):
            train._live_shape("wt", object(), "main", "base", own=lambda: asked.append(2) or [])
        self.assertEqual(asked, [1], "a green tree needs no diff")

    def test_the_cull_never_drops_a_class_as_no_lanes_when_a_lane_owns_its_module(self):
        shape = CullAllRed.shape()
        for own, parked in (([], 0), (["test_x"], 2), (None, 2)):
            asked = []
            got = train._shape_cull(["a", "b"], shape, base_rerun=lambda k: asked.append(k) or train.ShapeVerdict("red", k, 1.0),
                                    own=own)
            self.assertEqual((len(got), bool(asked)), (parked, parked == 0), own)


PIPE_FILE = "plugin/lib/sampo/train.py"


class PipelineLanesTrainFirst(ExpressBase):
    """T-5735: a lane whose diff touches the pipeline's own files (train.py, lanes.py ...) is express whatever its type
    and trains ahead of the others with no --first, so a fix to the pipeline is in use within the hour."""

    def made_with_pipeline(self, n=4, type_=None):
        """n ordinary lanes, then one that writes a pipeline file (valid python: the live land compiles the tree)."""
        ids = [self.lane(i, f"{i}.txt", when="2026-01-01T00:00:00") for i in range(1, n + 1)]
        pid = self.lane(n + 1, PIPE_FILE, text="x = 1\n", when="2026-01-01T00:00:00")
        self.audited(*ids, pid)
        for tid in (*ids, pid):
            self.returned(tid, f"Lane {tid}")
        if type_:
            self.fm("task", "set", pid, f"type={type_}")
        return ids, pid

    def test_pipeline_lane_among_20_feature_lanes_trains_first_when_live(self):
        ids, pid = self.made_with_pipeline(20)
        self.fm("lane", "mode", "live")
        r = self.land("--batch", "10")
        # T-5932: it leads the first train (merged first); the ordinary lanes fill that train up to the batch
        self.assertEqual(self.trains(r)[0], ", ".join([pid] + ids[:9]), r.stderr)
        self.assertEqual([self.brief(t).status for t in (*ids, pid)], ["done"] * 21, r.stdout + r.stderr)
        self.assertIn(f"{pid} touches the pipeline", r.stderr)

    def test_live_default_batch_takes_one_express_and_twenty_ordinary_lanes_as_one_train(self):
        # T-5932: the live default is uncapped (one train for all): 21 lanes, past TRAIN_BATCH (8)
        ids, pid = self.made_with_pipeline(20)
        self.fm("lane", "mode", "live")
        r = self.land()
        self.assertEqual(self.trains(r), [", ".join([pid] + ids)], r.stderr)
        self.assertEqual([self.brief(t).status for t in (*ids, pid)], ["done"] * 21, r.stdout + r.stderr)

    def test_the_land_restarts_on_what_the_shared_code_watch_reports(self):
        # T-5735: the land's own restart (T-3973) asks the same CodeWatch the verify pass and the follow ask
        from sampo import core as c
        ids, pid = self.made_with_pipeline(2)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)
        p = c.find_project(self.repo)
        with mock.patch.object(train.CodeWatch, "changed", lambda self: True), mock.patch.object(sys, "stderr", io.StringIO()):
            got = train.land(p, self.repo, 1)
        self.assertTrue(got["restart"])
        self.assertEqual(got["trains"], 1, "restarted at the first boundary, the rest left for the new code")

    def test_pipeline_lane_merge_and_first_use_reach_the_ledger_through_the_real_land(self):
        from sampo import core as c
        ids, pid = self.made_with_pipeline(1)
        r = self.land("--batch", "2")
        self.assertEqual(self.trains(r)[0], pid, r.stderr)
        p = c.find_project(self.repo)
        self.assertEqual(c.ledger_tail(p, 50, ["pipeline_used"]), [], "merged, not yet on a process that started after it")
        self.land("--batch", "2")  # the next land starts on the merged code: the fix is first in use
        used = c.ledger_tail(p, 50, ["pipeline_used"])
        self.assertEqual([e["task"] for e in used], [pid], used)
        self.assertEqual(sorted(used[0]["data"]), ["filed", "merged", "used"])

    def test_pipeline_lane_of_feature_type_trains_alone_ahead_of_the_rest_when_gated(self):
        ids, pid = self.made_with_pipeline(3)
        self.assertEqual(self.brief(pid).type, "FEATURE")
        r = self.land("--batch", "4")
        self.assertEqual(self.trains(r), [pid, ", ".join(ids)], r.stderr)

    def test_pipeline_lanes_live_lead_one_train_with_the_ordinary_lanes_up_to_the_batch(self):
        ids, pid = self.made_with_pipeline(3)
        second = self.lane(9, "plugin/lib/sampo/lanes.py", text="y = 2\n", when="2026-01-01T00:00:00")
        self.audited(second)
        self.returned(second, "Lane lanes")
        self.fm("lane", "mode", "live")
        r = self.land("--batch", "5")
        self.assertEqual(self.trains(r), [", ".join([pid, second] + ids)], r.stderr)

    def test_pipeline_files_cover_the_train_ship_gates_hooks_and_test_runner_and_no_others(self):
        for f in (PIPE_FILE, "plugin/lib/sampo/lanes.py", "plugin/bin/sampo", "plugin/lib/sampo/release.py",
                  "plugin/lib/sampo/hooks.py", "plugin/hooks/hooks.json", "plugin/hooks/hook", "plugin/tests/run.py",
                  "plugin/lib/sampo/selfgate.py", "plugin/lib/sampo/hooksmoke.py"):
            self.assertTrue(train.touches_pipeline(["README.md", f]), f)
        # cli.py and core.py are monoliths every verb touches: only their named pipeline functions count (PIPELINE_FUNCS)
        self.assertFalse(train.touches_pipeline(["README.md", "plugin/lib/sampo/cli.py", "plugin/lib/sampo/core.py",
                                                 "plugin/tests/test_train.py", "plugin/lib/sampo/measure.py"]))
        self.assertFalse(train.touches_pipeline([]))

    def test_pipeline_paths_and_functions_name_files_and_functions_that_exist(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(train.__file__)))))
        for f in train.PIPELINE_PATHS:
            self.assertTrue(os.path.exists(os.path.join(root, f)), f"{f} is named in PIPELINE_PATHS and is not there")
        for f, names in train.PIPELINE_FUNCS.items():
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                text = fh.read()
            for n in names:
                self.assertRegex(text, rf"(?m)^def {n}\(", f"{f} has no top-level def {n}")

    def test_pipeline_lane_by_function_a_change_in_a_named_cli_function_counts_and_another_does_not(self):
        base = "def alpha(x):\n    return 1\n\n\ndef task_finish(p, args):\n    return 1\n"
        cli = os.path.join(self.repo, "plugin", "lib", "sampo", "cli.py")
        os.makedirs(os.path.dirname(cli), exist_ok=True)
        with open(cli, "w") as fh:
            fh.write(base)
        self.git("add", "plugin")
        self.git("commit", "-qm", "base cli")
        close = self.lane(1, "plugin/lib/sampo/cli.py", text=base.replace("(p, args):\n    return 1",
                                                                           "(p, args):\n    return 2"))
        other = self.lane(2, "plugin/lib/sampo/cli.py", text=base.replace("def alpha(x):\n    return 1",
                                                                           "def alpha(x):\n    return 5"))
        added = self.lane(3, "plugin/lib/sampo/cli.py", text=base + "\n\ndef _check_affected(p, args):\n    return 0\n")
        train._PIPE_TIPS.clear()
        tips = {n: self.git("rev-parse", f"foreman/lane{n}") for n in (1, 2, 3)}
        self.assertTrue(train.pipeline_lane(self.repo, tips[1]), "task_finish changed")
        self.assertFalse(train.pipeline_lane(self.repo, tips[2]), "alpha changed: not a pipeline function")
        self.assertTrue(train.pipeline_lane(self.repo, tips[3]), "a named function added")
        self.assertTrue(close and other and added)

    def test_pipeline_lane_is_judged_once_per_tip(self):
        from unittest import mock
        tid = self.lane(1, PIPE_FILE, text="x = 1\n")
        tip = self.git("rev-parse", "foreman/lane1")
        train._PIPE_TIPS.clear()
        real = train._git
        calls = []

        def counting(cwd, *a, **kw):
            calls.append(a)
            return real(cwd, *a, **kw)
        with mock.patch.object(train, "_git", counting):
            self.assertTrue(train.pipeline_lane(self.repo, tip))
            self.assertTrue(train.pipeline_lane(self.repo, tip))
            self.assertEqual(len(calls), 1, "the second ask for the same tip ran no git process")
            self.git("commit", "--allow-empty", "-qm", "again", cwd=os.path.join(self.tmp, "lane1"))
            new = self.git("rev-parse", "foreman/lane1")
            self.assertNotEqual(new, tip)
            self.assertTrue(train.pipeline_lane(self.repo, new))
            self.assertEqual(len(calls), 2, "a new tip is judged again")
        self.assertFalse(train.pipeline_lane(self.repo, ""), "no tip, no pipeline lane")
        self.assertTrue(tid)

    def test_pipeline_flag_makes_any_type_express_and_sorts_it_first(self):
        from test_express_lane import brief
        self.assertTrue(train.is_express(brief(), True))
        self.assertFalse(train.is_express(brief(), False))
        urgent, plain = brief(priority="urgent", id="T-1"), brief(id="T-2")
        plain_pipe = brief(id="T-9")
        self.assertLess(train.land_key(plain_pipe, True), train.land_key(urgent))
        self.assertLess(train.land_key(urgent), train.land_key(plain))


class CodeStampReexec(unittest.TestCase):
    """T-5735: a long-running loop (the verify pass, the watch follow) takes up new Sampo code at its next unit
    boundary: the code stamp it started on is compared there, and a changed one re-executes the same command."""

    def lib(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "lib", "sampo"))
        path = os.path.join(root, "lib", "sampo", "mod.py")
        with open(path, "w") as f:
            f.write("x = 1\n")
        return root, path

    def test_code_stamp_changes_when_a_module_changes(self):
        root, path = self.lib()
        with mock.patch.object(train.c, "PLUGIN_ROOT", root):
            watch = train.CodeWatch()
            self.assertFalse(watch.changed())
            with open(path, "w") as f:
                f.write("x = 22\n")
            self.assertTrue(watch.changed())
            self.assertFalse(train.CodeWatch().changed(), "a process started after the change has the new stamp")

    def test_code_stamp_reexec_runs_the_same_command_only_when_the_code_changed(self):
        root, path = self.lib()
        seen = []
        with mock.patch.object(train.c, "PLUGIN_ROOT", root), mock.patch.dict(os.environ, {"SAMPO_NO_REEXEC": ""}):
            watch = train.CodeWatch()
            self.assertFalse(train.reexec_on_new_code(watch, lambda *a: seen.append(a)))
            self.assertEqual(seen, [], "unchanged code: the loop goes on as it is")
            with open(path, "w") as f:
                f.write("x = 22\n")
            self.assertTrue(train.reexec_on_new_code(watch, lambda *a: seen.append(a)))
        self.assertEqual(len(seen), 1)
        exe, argv = seen[0]
        self.assertEqual(exe, sys.executable)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:], list(sys.orig_argv[1:]))

    def test_code_stamp_reexec_can_be_switched_off_and_a_missing_module_dir_is_no_change(self):
        root, path = self.lib()
        with mock.patch.object(train.c, "PLUGIN_ROOT", root):
            watch = train.CodeWatch()
            with open(path, "w") as f:
                f.write("x = 22\n")
            with mock.patch.dict(os.environ, {"SAMPO_NO_REEXEC": "1"}):
                self.assertFalse(train.reexec_on_new_code(watch, lambda *a: self.fail("exec while switched off")))
            shutil.rmtree(os.path.join(root, "lib"))
            self.assertFalse(watch.changed(), "a stamp that cannot be read is no change")

    def test_code_stamp_a_failing_execv_is_logged_once_and_the_loop_goes_on_on_the_old_code(self):
        root, path = self.lib()
        err, calls = io.StringIO(), []

        def boom(*a):
            calls.append(a)
            raise OSError(8, "Exec format error")
        with mock.patch.object(train.c, "PLUGIN_ROOT", root), mock.patch.dict(os.environ, {"SAMPO_NO_REEXEC": ""}), \
                mock.patch.object(sys, "stderr", err):
            watch = train.CodeWatch()
            with open(path, "w") as f:
                f.write("x = 22\n")
            self.assertFalse(train.reexec_on_new_code(watch, boom), "a refused exec is no re-exec")
            self.assertFalse(train.reexec_on_new_code(watch, boom), "and is not tried again at every boundary")
            self.assertEqual(len(calls), 1)
            self.assertIn("could not restart on new code", err.getvalue())
            self.assertIn("Exec format error", err.getvalue())
            with open(path, "w") as f:
                f.write("x = 333\n")  # a later change is a new reason to try
            self.assertFalse(train.reexec_on_new_code(watch, boom))
            self.assertEqual(len(calls), 2)

    def test_code_stamp_the_stamp_is_taken_at_import_so_a_change_during_start_up_is_seen(self):
        root, path = self.lib()
        with mock.patch.object(train.c, "PLUGIN_ROOT", root):
            at_import = train.CodeWatch._read()
            with open(path, "w") as f:
                f.write("x = 22\n")
            self.assertTrue(train.CodeWatch(at_import).changed(), "the code on disk moved after this process loaded")
        self.assertIsNotNone(train._BOOT_STAMP, "taken when train.py was imported")

    def _pm_run_with_probe(self, merges):
        """pm_run over `merges` pending merges (two or more: a HEAD probe is made) with the code changed all along and
        execv stubbed: how many times it exec'd."""
        from sampo import lanes
        execs = []
        pending = {f"m{i}": {"state": "pending"} for i in range(merges)}
        answers = iter([pending, pending, {}, {}, {}])
        probe = True
        with mock.patch.object(train.CodeWatch, "changed", lambda self: True), \
                mock.patch.dict(os.environ, {"SAMPO_NO_REEXEC": ""}), \
                mock.patch.object(train.os, "execv", lambda *a: execs.append(a)), \
                mock.patch.object(lanes, "pm_records", lambda p: next(answers)), \
                mock.patch.object(lanes, "_pm_settle", lambda *a: None), \
                mock.patch.object(lanes, "ship_yield", lambda *a: 0), \
                mock.patch.object(lanes, "_git", lambda *a, **k: mock.Mock(stdout="abc\n")), \
                mock.patch.object(lanes, "_pm_claim", lambda *a: probe), \
                mock.patch.object(lanes, "_pm_green_at", lambda *a, **k: False), \
                mock.patch.object(lanes, "pm_rerun_cmd", lambda *a: ""), \
                mock.patch.object(lanes, "pm_set", lambda *a, **k: None), \
                mock.patch.object(lanes, "pm_verify", lambda *a: True), \
                mock.patch("sampo.measure.note_pipeline_used", lambda p: None):
            lanes.pm_run(None, "main")
        return len(execs)

    def test_code_stamp_a_verify_pass_with_a_head_probe_behind_it_does_not_re_exec(self):
        # probed (the red HEAD runs already made) is lost on exec: a re-exec would rerun that full suite
        self.assertEqual(self._pm_run_with_probe(2), 1, "the boundary before the probe may exec; none after it")

    def test_code_stamp_a_verify_pass_with_no_probe_re_execs_at_every_boundary(self):
        self.assertEqual(self._pm_run_with_probe(1), 2)

    def test_code_stamp_is_asked_between_the_verify_pass_s_units_never_inside_one(self):
        from sampo import lanes
        events = []
        answers = iter([{}, {"r": {"state": "red"}}, {}, {}])
        with mock.patch.object(train, "reexec_on_new_code", lambda w: events.append("boundary")), \
                mock.patch.object(lanes, "pm_records", lambda p: next(answers)), \
                mock.patch.object(lanes, "_pm_settle", lambda *a: events.append("settle")), \
                mock.patch.object(lanes, "_pm_revert", lambda *a: events.append("unit") or True):
            lanes.pm_run(None, "main")
        self.assertEqual(events, ["boundary", "unit", "boundary"])

    def test_code_stamp_is_asked_between_the_watch_follow_s_rounds(self):
        from sampo import watch
        calls = []

        def boundary(w):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
        out = io.StringIO()
        with mock.patch.object(train, "reexec_on_new_code", boundary), mock.patch.object(sys, "stdout", out):
            watch._follow(lambda: {"a": 1}, lambda v: [], 0.01, refresh=0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(out.getvalue(), '{"a": 1}\n', "the first round was whole before the boundary after it")


class ExpressTrainsTogether(ExpressBase):
    """T-5726: live, the express lanes (performance, security, urgent) named --first train together as one train, capped
    at an explicit --batch; T-5857: the ordinary lanes ride in that train, after them; gated, each still trains alone
    (T-5023)."""

    def test_express_live_lanes_train_together_ahead_of_the_ordinary_ones(self):
        ids = self.made(5)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[1], "type=PERFORMANCE")
        self.fm("task", "set", ids[3], "type=SECURITY")
        self.fm("task", "set", ids[4], "priority=urgent")
        self.queue(ids[0], ids[1], ids[3], ids[4])
        r = self.land("--batch", "5")
        self.assertEqual(self.trains(r), [f"{ids[1]}, {ids[3]}, {ids[4]}, {ids[0]}, {ids[2]}"], r.stderr)
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 5, r.stdout + r.stderr)

    def test_express_live_train_is_capped_at_the_batch_size(self):
        ids = self.made(4)
        self.fm("lane", "mode", "live")
        for t in ids[:3]:
            self.fm("task", "set", t, "type=PERFORMANCE")
        self.queue(*ids[:3])
        r = self.land("--batch", "2")
        # T-5932: the batch is no larger than the express count: the first ordinary lane takes the last slot
        self.assertEqual(self.trains(r), [f"{ids[0]}, {ids[3]}", f"{ids[1]}, {ids[2]}"], r.stderr)

    def test_express_live_explicit_batch_2_keeps_a_slot_for_the_ordinary_lane(self):
        ids = self.made(3)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[1], "type=PERFORMANCE")
        self.fm("task", "set", ids[2], "type=SECURITY")
        self.queue(ids[1], ids[2])
        r = self.land("--batch", "2")
        self.assertEqual(self.trains(r), [f"{ids[1]}, {ids[0]}", ids[2]], r.stderr)
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 3, r.stdout + r.stderr)

    def test_express_live_explicit_batch_1_stays_strict_priority(self):
        # T-5932: no slot to keep at --batch 1: one lane per train, the express lanes first
        ids = self.made(3)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[1], "type=PERFORMANCE")
        self.fm("task", "set", ids[2], "type=SECURITY")
        self.queue(ids[1], ids[2])
        r = self.land("--batch", "1")
        self.assertEqual(self.trains(r), [ids[1], ids[2], ids[0]], r.stderr)
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 3, r.stdout + r.stderr)

    def _two_express_five_ordinary(self, *args):
        ids = self.made(7)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[5], "type=PERFORMANCE")
        self.fm("task", "set", ids[6], "priority=urgent")
        self.queue(ids[5], ids[6])
        r = self.land(*args)
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 7, r.stdout + r.stderr)
        return [ids[5], ids[6]] + ids[:5], r  # the order they train in: express first

    def test_express_live_train_fills_with_the_ordinary_lanes_up_to_the_batch(self):
        """T-5932: 2 express and 5 ordinary ready lanes are one train of 7, express first."""
        order, r = self._two_express_five_ordinary("--batch", "8")
        self.assertEqual(self.trains(r), [", ".join(order)], r.stderr)

    def test_express_live_train_of_an_explicit_batch_4_runs_4_then_3(self):
        order, r = self._two_express_five_ordinary("--batch", "4")
        self.assertEqual(self.trains(r), [", ".join(order[:4]), ", ".join(order[4:])], r.stderr)

    def test_express_live_default_batch_does_not_cap_the_train(self):
        order, r = self._two_express_five_ordinary()
        self.assertEqual(self.trains(r), [", ".join(order)], r.stderr)

    def test_express_gated_lanes_still_train_alone(self):
        ids = self.made(4)
        self.fm("task", "set", ids[1], "type=PERFORMANCE")
        self.fm("task", "set", ids[3], "type=SECURITY")
        self.queue(ids[0], ids[1], ids[3])
        r = self.land("--batch", "4")
        self.assertEqual(self.trains(r), [ids[1], ids[3], f"{ids[0]}, {ids[2]}"], r.stderr)

    def test_express_live_log_line_says_it_trains_with_the_other_express_lanes(self):
        ids = self.made(3)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[1], "type=PERFORMANCE")
        self.queue(ids[1])
        r = self.land("--batch", "3")
        self.assertIn(f"{ids[1]} is express (performance, security or urgent): it trains next, first in the train, ahead of the ordinary lanes",
                      r.stderr)


class InsertionUnionLib(AffectedBase):
    """T-5937: a lane whose conflicts are pure insertions in .py files of plugin/lib and plugin/tests (mixed with map
    files) merges without parking when each unioned file compiles, binds no top-level name twice, and the tests a lib
    file reaches pass on the union. The fixture's rule test (test_doctor) is the one a lib change reaches: it is red
    when plugin/lib/xthing.py holds BOOM."""
    LIB = "plugin/lib/xthing.py"
    BASE = "def old():\n    return 0\n"
    DOCTOR = ("import os\nimport unittest\n\n\nclass Doctor(unittest.TestCase):\n    def test_it(self):\n"
              "        if os.path.exists('plugin/lib/xthing.py'):\n"
              "            with open('plugin/lib/xthing.py') as f:\n                self.assertNotIn('BOOM', f.read())\n")

    def setUp(self):
        super().setUp()
        self.commit("plugin/tests/test_doctor.py", self.DOCTOR)

    @staticmethod
    def fn(name, body="1"):
        return f"\n\ndef {name}():\n    return {body}\n"

    def commit(self, name, text, cwd=None):
        path = os.path.join(cwd or self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        self.git("add", name, cwd=cwd)
        self.git("commit", "-qm", f"write {name}", cwd=cwd)

    def based(self, name=LIB, text=BASE):
        self.commit(name, text)

    def also(self, n, name, text):
        """One more file in lane n's commit history."""
        self.commit(name, text, cwd=os.path.join(self.tmp, f"lane{n}"))

    def read(self, name):
        with open(os.path.join(self.repo, name)) as f:
            return f.read()

    def parked(self, r, tid, files=LIB):
        self.assertIn(f"left out {tid}: conflicts with an earlier lane in the train: {files}", r.stdout + r.stderr)

    def test_two_lanes_that_each_append_a_function_to_a_lib_file_both_land(self):
        self.based()
        a = self.lane(1, self.LIB, self.BASE + self.fn("lane_one"))
        b = self.lane(2, self.LIB, self.BASE + self.fn("lane_two", "2"))
        r = self.train(a, b)
        self.assertNotIn("left out", r.stdout)
        for tid in (a, b):
            self.assertTrue(self.meta(tid)["lane_stop"].startswith("merged"), r.stdout)
        self.assertEqual(self.read(self.LIB), self.BASE + self.fn("lane_one") + self.fn("lane_two", "2"))
        self.assertIn(f"merged {b} keeping both sides' added lines in {self.LIB}", r.stdout)
        self.assertEqual(self.git("ls-files", "-u"), "")

    def test_a_lane_and_a_trunk_that_moved_under_it_each_adding_a_function_at_one_spot_merge(self):
        self.based()
        a = self.lane(1, self.LIB, self.BASE + self.fn("lane_one"))
        self.commit(self.LIB, self.BASE + self.fn("trunk_two", "2"))
        r = self.train(a)
        self.assertNotIn("left out", r.stdout)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), r.stdout + r.stderr)
        text = self.read(self.LIB)
        self.assertIn("def lane_one", text)
        self.assertIn("def trunk_two", text)
        self.assertEqual(self.git("ls-files", "-u"), "")

    def test_a_lib_file_a_test_file_and_a_map_entry_in_one_conflict_set_all_union(self):
        tf, mp = "plugin/tests/test_xthing.py", "docs/map/ops.md"
        entry = "- `ops.py`: does the work.\n"
        for name, text in ((self.LIB, self.BASE), (tf, "import unittest\n"), (mp, entry)):
            self.based(name, text)
        a = self.lane(1, self.LIB, self.BASE + self.fn("lane_one"))
        b = self.lane(2, self.LIB, self.BASE + self.fn("lane_two", "2"))
        for n, tag in ((1, "one"), (2, "two")):
            self.also(n, tf, "import unittest\n" + f"\n\nclass T{tag}(unittest.TestCase):\n    pass\n")
            self.also(n, mp, entry.rstrip("\n") + f" Lane {tag} adds this.\n")
        r = self.train(a, b)
        self.assertNotIn("left out", r.stdout)
        for tid in (a, b):
            self.assertTrue(self.meta(tid)["lane_stop"].startswith("merged"), r.stdout)
        self.assertIn("def lane_one", self.read(self.LIB))
        self.assertIn("def lane_two", self.read(self.LIB))
        self.assertIn("class Tone", self.read(tf))
        self.assertIn("class Ttwo", self.read(tf))
        self.assertIn("Lane one adds this. Lane two adds this.", self.read(mp))
        self.assertEqual(self.git("ls-files", "-u"), "")

    def test_a_same_line_edit_in_a_lib_file_still_parks(self):
        self.based()
        a = self.lane(1, self.LIB, self.BASE.replace("return 0", "return 1"))
        b = self.lane(2, self.LIB, self.BASE.replace("return 0", "return 2"))
        r = self.train(a, b, check=False)
        self.parked(r, b)
        self.assertIn("return 1", self.read(self.LIB))

    def test_a_union_that_does_not_compile_still_parks(self):
        """Each side compiles alone; `x = 1` then `global x` in one body is a SyntaxError no per-block parse sees."""
        self.based(self.LIB, "def f():\n    pass\n")
        a = self.lane(1, self.LIB, "def f():\n    pass\n    x = 1\n")
        b = self.lane(2, self.LIB, "def f():\n    pass\n    global x\n")
        r = self.train(a, b, check=False)
        self.parked(r, b)
        self.assertNotIn("global x", self.read(self.LIB))

    def test_a_def_both_lanes_added_still_parks(self):
        self.based()
        a = self.lane(1, self.LIB, self.BASE + self.fn("same", "1"))
        b = self.lane(2, self.LIB, self.BASE + self.fn("same", "2"))
        r = self.train(a, b, check=False)
        self.parked(r, b)
        self.assertEqual(self.read(self.LIB).count("def same"), 1)

    def test_one_file_that_is_not_a_pure_insertion_parks_the_whole_set(self):
        self.based()
        other = "plugin/lib/ything.py"
        self.based(other, "a = 1\n")
        a = self.lane(1, self.LIB, self.BASE + self.fn("lane_one"))
        b = self.lane(2, self.LIB, self.BASE + self.fn("lane_two", "2"))
        self.also(1, other, "a = 2\n")
        self.also(2, other, "a = 3\n")
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}: conflicts with an earlier lane in the train", r.stdout)
        self.assertNotIn("def lane_two", self.read(self.LIB))
        self.assertEqual(self.git("ls-files", "-u"), "")

    def test_a_compile_error_in_the_union_is_refused_by_the_union_itself(self):
        base = b"def f():\n    pass\n"
        self.assertIsNone(train._insertion_union(base + b"    x = 1\n", base, base + b"    global x\n"))
        self.assertIsNotNone(train._insertion_union(base + b"    x = 1\n", base, base + b"    y = 2\n"))

    def test_a_lib_union_whose_affected_test_fails_parks_the_lane(self):
        """The union compiles and repeats no name, but the test the lib file reaches is red on it: the lane is left
        out for that, the merge undone, the first lane's function still there."""
        self.based()
        a = self.lane(1, self.LIB, self.BASE + self.fn("lane_one"))
        b = self.lane(2, self.LIB, self.BASE + self.fn("lane_two", "'BOOM'"))
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}: its union keeping both sides' added lines in {self.LIB} fails the tests that file "
                      "reaches", r.stdout)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), r.stdout)
        self.assertNotIn("BOOM", self.read(self.LIB))
        self.assertEqual(self.git("ls-files", "-u"), "")

    def test_a_test_file_union_runs_no_affected_tests_of_its_own(self):
        """Only a union of a plugin/lib file is proved before the merge commit; the gate judges a test file's."""
        from sampo import core as c
        tf = "plugin/tests/test_xthing.py"
        self.based(tf, "import unittest\n")
        self.based()
        tids = []
        for n, who in ((1, "One"), (2, "Two")):
            tids.append(self.lane(n, tf, "import unittest\n\n\nclass %s(unittest.TestCase):\n    pass\n" % who))
            self.also(n, self.LIB, self.BASE + self.fn("lane_" + who.lower()))
        p, wt = c.find_project(self.repo), os.path.join(self.tmp, "union-wt")
        self.git("worktree", "add", "-q", "--detach", wt, "HEAD")
        briefs = {b.id: b for b in c.load_briefs(p)}
        seen = []
        with mock.patch.object(train, "_union_proof", side_effect=lambda *a: seen.append(a[3]) or ""):
            for n, tid in ((1, tids[0]), (2, tids[1])):
                self.assertTrue(train._merge_in(wt, briefs[tid], self.git("rev-parse", f"foreman/lane{n}"), [], kept={}, p=p))
        self.assertEqual(seen, [[self.LIB]], "the lib file's union was proved once, the test file's was not")

    def test_a_hook_or_bin_script_is_no_union_candidate(self):
        """Only plugin/lib and plugin/tests .py files are unioned: the same append clash in a hook script parks."""
        hook = "plugin/hooks/guardthing.py"
        self.based(hook, self.BASE)
        a = self.lane(1, hook, self.BASE + self.fn("lane_one"))
        b = self.lane(2, hook, self.BASE + self.fn("lane_two", "2"))
        r = self.train(a, b, check=False)
        self.parked(r, b, hook)
        self.assertEqual([train._unionable([f]) for f in (hook, "plugin/bin/x.py", "x.py", "plugin/lib/a/b.py",
                                                        "plugin/tests/test_a.py", "docs/map/ops.md", "MASTER.md")],
                         [False, False, False, True, True, True, True])

    def proof(self, affected, ran=None):
        """_union_proof's answer with cli.affected and the run of its command stubbed (no process is started)."""
        from sampo import cli, core as c
        p = c.find_project(self.repo)
        with mock.patch.object(cli, "affected", **({"side_effect": affected} if isinstance(affected, Exception)
                                                  else {"return_value": affected})), \
                mock.patch.object(train.c, "run_command", **({"side_effect": ran} if isinstance(ran, list)
                                                              else {"return_value": ran or (0, "")})):
            return train._union_proof(p, self.repo, "HEAD", [self.LIB])

    def test_a_class_red_on_the_base_too_is_not_blamed_on_the_union(self):
        """The red classes rerun once on the tree before the merge (as the probe does): red there is the base's."""
        aff, red = ("true", ["t.py"], set()), (1, "FAIL: test_it (test_a.A.test_it)\nFAILED (failures=1)")
        green = (0, "")
        self.assertEqual(self.proof(aff, [red, red, red]), "", "red on the base too")
        self.assertIn("test_a.A", self.proof(aff, [red, red, green]), "red again on the merge, green on the base: the union's")
        head = self.git("rev-parse", "HEAD")
        self.proof(aff, [red, red, red])
        self.assertEqual(self.git("rev-parse", "HEAD"), head, "the merged commit is restored after the base rerun")

    def test_a_class_red_once_and_green_on_a_rerun_of_the_merged_tree_is_a_flake_not_the_unions(self):
        """The red classes run once more on the merge before the base: green there, the union is not blamed."""
        aff, red = ("true", ["t.py"], set()), (1, "FAIL: test_it (test_a.A.test_it)\nFAILED (failures=1)")
        self.assertEqual(self.proof(aff, [red, (0, "")]), "")

    def test_a_proof_leaves_no_untracked_file_in_the_tree_the_gate_judges_next(self):
        """A test run may write files: they are cleaned after the proof, green or red, so no later gate sees them."""
        from sampo import core as c
        self.based()
        tids = [self.lane(n, self.LIB, self.BASE + self.fn(f"lane_{n}", str(n))) for n in (1, 2)]
        p, wt = c.find_project(self.repo), os.path.join(self.tmp, "union-wt")
        self.git("worktree", "add", "-q", "--detach", wt, "HEAD")
        briefs, start = {b.id: b for b in c.load_briefs(p)}, self.git("rev-parse", "HEAD")

        def proof(why):
            def run(*a):
                with open(os.path.join(a[1], "junk.txt"), "w") as f:
                    f.write("left by a test run")
                return why
            return run
        for why in ("", "failing t.A"):
            train._UNION_PROOFS.clear()
            self.git("reset", "-q", "--hard", start, cwd=wt)
            with mock.patch.object(train, "_union_proof", side_effect=proof(why)):
                for n, tid in ((1, tids[0]), (2, tids[1])):
                    train._merge_in(wt, briefs[tid], self.git("rev-parse", f"foreman/lane{n}"), [], kept={}, p=p)
            self.assertFalse(os.path.exists(os.path.join(wt, "junk.txt")), f"proof {why!r}")

    def test_a_rerun_that_names_only_other_classes_cleared_the_first_red_ones(self):
        """A flake: the first run failed A, the rerun of A fails B (not A). A is not blamed; a rerun that names no class
        at all (a crash) leaves the first classes red."""
        aff, red = ("true", ["t.py"], set()), (1, "FAIL: test_it (test_a.A.test_it)\nFAILED (failures=1)")
        other, crash = (1, "FAIL: test_it (test_b.B.test_it)\nFAILED (failures=1)"), (1, "Traceback: boom")
        self.assertEqual(self.proof(aff, [red, other]), "", "A cleared on its rerun")
        self.assertIn("test_a.A", self.proof(aff, [red, crash, (0, "")]), "a rerun that names no class stays red")

    def test_a_tree_that_wedges_under_a_red_proof_costs_that_lane_only_and_the_train_goes_on(self):
        """The lib test leaves a git lock in the scratch tree's git dir, so the reset after its red proof fails: the
        train rebuilds the tree once and retries, then leaves that one lane out; the lane before it still lands."""
        doctor = self.DOCTOR.replace("            with open", "            s = open('plugin/lib/xthing.py').read()\n"
                                     "            if 'BOOM' in s:\n"
                                     "                g = open('.git').read().split(':', 1)[1].strip()\n"
                                     "                open(os.path.join(g, 'index.lock'), 'w').close()\n"
                                     "            with open")
        self.commit("plugin/tests/test_doctor.py", doctor)
        self.based()
        a = self.lane(1, self.LIB, self.BASE + self.fn("lane_one"))
        b = self.lane(2, self.LIB, self.BASE + self.fn("lane_two", "'BOOM'"))
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}", r.stdout + r.stderr)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), r.stdout + r.stderr)
        self.assertNotIn("BOOM", self.read(self.LIB))

    def test_a_rebuild_reuses_the_proof_of_a_union_it_already_made(self):
        """build() merges again for each bisection rebuild: each distinct union is proved once, red or green."""
        from sampo import core as c
        self.based()
        tids = [self.lane(n, self.LIB, self.BASE + self.fn(f"lane_{n}", str(n))) for n in (1, 2)]
        p, wt = c.find_project(self.repo), os.path.join(self.tmp, "union-wt")
        self.git("worktree", "add", "-q", "--detach", wt, "HEAD")
        briefs = {b.id: b for b in c.load_briefs(p)}
        tips, start = [self.git("rev-parse", f"foreman/lane{n}") for n in (1, 2)], self.git("rev-parse", "HEAD")
        calls = []
        with mock.patch.object(train, "_union_proof", side_effect=lambda *a: calls.append(a[3]) or ""):
            for _ in range(3):
                self.git("reset", "-q", "--hard", start, cwd=wt)
                for tid, tip in zip(tids, tips):
                    self.assertTrue(train._merge_in(wt, briefs[tid], tip, [], kept={}, p=p))
        self.assertEqual(calls, [[self.LIB]], "three builds, one proof")
        train._UNION_PROOFS.clear()
        red = []
        with mock.patch.object(train, "_union_proof", side_effect=lambda *a: red.append(1) or "failing t.A"):
            for _ in range(2):
                self.git("reset", "-q", "--hard", start, cwd=wt)
                log = []
                self.assertTrue(train._merge_in(wt, briefs[tids[0]], tips[0], log, kept={}, p=p))
                self.assertIsNone(train._merge_in(wt, briefs[tids[1]], tips[1], log, kept={}, p=p))
                self.assertIn("failing t.A", log[0][1])
        self.assertEqual(len(red), 2, "a red union is proved again: the cache holds green answers only")

    def test_a_tree_that_will_not_reset_after_a_red_proof_is_lost_not_left_out(self):
        from sampo import core as c
        self.based()
        tids = [self.lane(n, self.LIB, self.BASE + self.fn(f"lane_{n}", str(n))) for n in (1, 2)]
        p, wt = c.find_project(self.repo), os.path.join(self.tmp, "union-wt")
        self.git("worktree", "add", "-q", "--detach", wt, "HEAD")
        briefs = {b.id: b for b in c.load_briefs(p)}
        real, log = train._git, []

        def git(cwd, *a, **k):
            if a[:2] == ("reset", "-q") or a[:1] == ("reset",):
                return mock.Mock(returncode=128, stdout="", stderr="boom")
            return real(cwd, *a, **k)
        self.assertTrue(train._merge_in(wt, briefs[tids[0]], self.git("rev-parse", "foreman/lane1"), log, kept={}, p=p))
        with mock.patch.object(train, "_union_proof", return_value="failing t.A"), \
                mock.patch.object(train, "_git", side_effect=git):
            with self.assertRaises(train._Lost):
                train._merge_in(wt, briefs[tids[1]], self.git("rev-parse", "foreman/lane2"), log, kept={}, p=p)
        self.assertEqual(log, [], "no lane was logged as left out")

    def test_the_proof_of_a_lib_union_is_green_only_when_its_affected_tests_ran_and_passed(self):
        self.assertEqual(self.proof(("true", ["t.py"], {self.LIB})), "")
        for name, args in {"no command": ((None, [], {self.LIB}),), "no tests": (("true", [], {self.LIB}),),
                           "no affected command": (RuntimeError("no affected-tests command"),),
                           "red": (("true", ["t.py"], set()), (1, "FAIL: test_a.A.test_it")),
                           "timeout": (("true", ["t.py"], set()), (124, "")),
                           "ran none": (("true", ["t.py"], set()), (5, ""))}.items():
            self.assertNotEqual(self.proof(*args), "", name)

    def test_a_same_line_clash_in_a_py_file_is_refused_before_any_run_and_says_why(self):
        """The up-front probe names a .py conflict that is no pure insertion at once; a pure insertion goes on."""
        from sampo import core as c
        self.based()
        a = self.lane(1, self.LIB, self.BASE.replace("return 0", "return 1"))
        self.commit(self.LIB, self.BASE.replace("return 0", "return 2"))
        p, base = c.find_project(self.repo), self.git("rev-parse", "HEAD")
        why = train._probe(p, self.repo, base, None, self.git("rev-parse", "foreman/lane1"), [], tests=False)
        self.assertIn(f"conflicts with the base: {self.LIB}", why)
        self.assertIn("not a pure insertion", why)
        r = self.train(a, check=False)
        self.assertIn(f"{a}: conflicts with the base: {self.LIB} (not a pure insertion", r.stdout + r.stderr)
        self.assertEqual(self.gate_runs(), 0, "no gate run for it")

    def test_a_pure_insertion_clash_with_the_base_is_not_refused_up_front(self):
        from sampo import core as c
        self.based()
        self.lane(1, self.LIB, self.BASE + self.fn("lane_one"))
        self.commit(self.LIB, self.BASE + self.fn("trunk_two", "2"))
        p, base = c.find_project(self.repo), self.git("rev-parse", "HEAD")
        self.assertEqual(train._probe(p, self.repo, base, None, self.git("rev-parse", "foreman/lane1"), [], tests=False), "")

    def test_a_non_compiling_or_repeating_clash_with_the_base_is_refused_up_front(self):
        from sampo import core as c
        self.based(self.LIB, "def f():\n    pass\n")
        self.lane(1, self.LIB, "def f():\n    pass\n    global x\n")  # the trunk's lines come first in the union
        self.commit(self.LIB, "def f():\n    pass\n    x = 1\n")
        p, base = c.find_project(self.repo), self.git("rev-parse", "HEAD")
        why = train._probe(p, self.repo, base, None, self.git("rev-parse", "foreman/lane1"), [], tests=False)
        self.assertIn(f"conflicts with the base: {self.LIB}", why)

class BehindBase(PmCase):
    """T-5936: the behind verify of a merged head ran the whole suite (13,000 tests, 100% of the CPU for hours) after every
    merge. Now it runs the shape modules and only the classes cli.affected picks from the head's own change, and the whole
    suite once per sweep: when PM_SWEEP_S or PM_SWEEP_EVERY merged heads have passed since the last one. A partial verify is
    recorded `partial`: it never stands as a full run (sampo ship's pass list, a close's proof, the sentinel). Over a fake
    map and stand-ins for the runners: run.py writes `full` (and its argv) to the mark, the shape pass `shape`, the affected
    template `part` and the module names it was given."""
    MAP = {"tests": {"plugin/tests/test_a.py": ["plugin/lib/sampo/a.py"], "plugin/tests/test_b.py": ["plugin/lib/sampo/b.py"]},
           "gates": [], "pairs": {}, "broke": {}}
    RUNNER = "import os, sys\nwith open(os.environ['PM_MARK'], 'a') as f:\n    f.write(' '.join(['full'] + sys.argv[1:]) + '\\n')\n" \
             "if os.environ.get('PM_FAIL'):\n    print('FAIL: t (test_a.A.t)')\n    sys.exit(1)\n"

    def setUp(self):
        super().setUp()
        self.env(FM_POSTMERGE_SHAPE=f"echo shape >> {self.mark}", PM_MARK=self.mark)
        for rel, text in {"plugin/tests/run.py": self.RUNNER, "plugin/tests/test_a.py": "a\n", "plugin/tests/test_b.py": "b\n",
                          "plugin/lib/sampo/a.py": "a\n", "plugin/lib/sampo/b.py": "b\n", "notes/readme.txt": "n\n"}.items():
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "suite")
        self.template("echo part {names} >> $PM_MARK")  # $PM_MARK, not the path: cli.affected reads a word that is a file as the runner
        load =mock.patch.object(smap, "load", return_value=self.MAP)
        load.start()
        self.addCleanup(load.stop)
        self.n = 0

    def template(self, cmd):
        self.fm("check", "affected", cmd)

    def head(self, rel):
        """A lane that appends a line to rel, merged on trunk with a --no-ff merge and recorded pending: the merged head."""
        self.n += 1
        self.git("checkout", "-qb", f"b-h{self.n}", self.trunk)
        with open(os.path.join(self.repo, rel), "a") as f:
            f.write(f"{self.n}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", f"h{self.n}")
        self.git("checkout", "-q", self.trunk)
        return self.merge(f"h{self.n}", self.task())

    def verify(self):
        """pm_run over what is pending; what ran, in order (the mark is cleared for the next one)."""
        slanes.pm_run(self.p, self.repo)
        ran = self.ran()
        if os.path.exists(self.mark):
            os.remove(self.mark)
        return ran

    def swept(self):
        """The first head is the sweep (no record of one: due); the next ones are the test's."""
        self.head("plugin/lib/sampo/a.py")
        self.assertEqual(self.verify(), ["shape", "full"], "the first head runs the whole suite")

    def sweep_state(self):
        with open(os.path.join(slanes._pm_dir(self.p), ".sweep")) as f:
            return json.load(f)


class BehindAffected(BehindBase):
    """T-5936: what the behind verify runs (BehindBase's fixture)."""

    def test_behind_affected_a_head_that_changes_one_module_runs_its_classes_and_the_shape_pass_only(self):
        self.swept()
        sha = self.head("plugin/lib/sampo/b.py")
        self.assertEqual(self.verify(), ["shape", "part", "test_b"], "test_a, which reaches nothing b.py has, does not run")
        r = self.rec(sha)
        self.assertEqual((r["state"], r.get("partial")), ("green", True))

    def test_behind_affected_the_tenth_head_since_the_last_full_run_is_a_full_run(self):
        self.swept()
        runs = [self.verify() if self.head("plugin/lib/sampo/a.py") else None for _ in range(10)]
        self.assertEqual(runs[:9], [["shape", "part", "test_a"]] * 9, "heads 2 to 10 run what they reach")
        self.assertEqual(runs[9], ["shape", "full"], "the 11th head: ten have passed since the last full run")
        self.assertEqual(self.head("plugin/lib/sampo/a.py") and self.verify(), ["shape", "part", "test_a"], "and the count starts over")

    def test_behind_affected_every_sweep_is_an_uncached_full_pass_sampo_ship_can_push(self):
        # round 2 (HIGH): a sweep that ran cached left ship no fully verified commit: it ran its own suite
        self.swept()
        self.assertEqual(len(slanes.pm_full_passes(self.p)), 1)
        for rounds in (2, 3):
            for _ in range(9):
                self.head("plugin/lib/sampo/a.py")
                self.assertEqual(self.verify(), ["shape", "part", "test_a"])
                self.assertEqual(len(slanes.pm_full_passes(self.p)), rounds - 1, "a partial verify adds no pass")
            sha = self.head("plugin/lib/sampo/a.py")
            self.assertEqual(self.verify(), ["shape", "full"])
            passes = slanes.pm_full_passes(self.p)
            self.assertEqual(len(passes), rounds, "the sweep (the 11th head, the 21st) is a pass ship can push")
            self.assertEqual(passes[-1]["sha"], sha)

    def test_behind_affected_a_partial_green_is_logged_partial_and_a_sweeps_is_not(self):
        self.swept()
        self.head("plugin/lib/sampo/b.py")
        self.verify()
        done = [e["data"] for e in c.ledger_tail(self.p, 500) if e.get("event") == "lane_verified"]
        self.assertEqual([bool(d.get("partial")) for d in done], [False, True], "the sentinel reads the sweep as a full run only")

    def test_behind_affected_the_probe_resets_the_sweep_counts_only_after_its_full_run_is_green(self):
        self.swept()
        for _ in range(9):
            self.head("plugin/lib/sampo/a.py")
            self.verify()
        self.assertEqual(self.sweep_state()["k"], 9)
        self.assertTrue(slanes._pm_sweep_due(self.p, 5), "ten heads: due")
        self.assertEqual(self.sweep_state()["k"], 9, "asking restarts nothing: a sweep that does not go green is due again")
        passes = len(slanes.pm_full_passes(self.p))
        shas = [self.head("plugin/lib/sampo/a.py"), self.head("plugin/lib/sampo/b.py")]
        self.assertEqual(self.verify(), ["shape", "full"], "two pending heads, a sweep due: one probe, the whole suite at HEAD")
        self.assertEqual(self.sweep_state()["k"], 0, "the probe's green full run restarted the counts")
        self.assertEqual(len(slanes.pm_full_passes(self.p)), passes + 1, "an uncached pass of HEAD for ship")
        for sha in shas:
            self.assertFalse(self.rec(sha).get("partial"))

    def test_behind_affected_a_requeued_merges_full_run_is_no_pass_so_it_leaves_the_sweep_counts(self):
        self.swept()
        for _ in range(4):
            self.head("plugin/lib/sampo/a.py")
            self.verify()
        self.assertEqual(self.sweep_state()["k"], 4)
        passes = len(slanes.pm_full_passes(self.p))
        sha = self.head("plugin/lib/sampo/a.py")
        slanes.pm_set(self.p, sha, after_reverts=[sha])
        self.assertEqual(self.verify(), ["shape", "full"], "a requeued merge runs the whole suite")
        self.assertEqual(len(slanes.pm_full_passes(self.p)), passes, "another tree: no pass for ship")
        self.assertEqual(self.sweep_state()["k"], 4, "and no pass, so the counts are not reset")

    def test_behind_affected_a_flaky_excused_sweep_leaves_the_counts_due_and_the_next_head_sweeps_again(self):
        # round 3: a sweep that ends flaky records no pm_full_pass; resetting left ship no new commit for 10 heads
        self.swept()
        for _ in range(9):
            self.head("plugin/lib/sampo/a.py")
            self.verify()
        passes = len(slanes.pm_full_passes(self.p))
        self.env(PM_FAIL="1", FM_POSTMERGE_RERUN="exit 0")  # the whole suite red on test_a.A, green alone
        sha = self.head("plugin/lib/sampo/b.py")
        self.assertEqual(self.verify(), ["shape", "full"], "the 11th head: the sweep")
        r = self.rec(sha)
        self.assertEqual((r["state"], r.get("flaky")), ("green", ["test_a.A"]), "excused as flaky")
        self.assertEqual(len(slanes.pm_full_passes(self.p)), passes, "an excused run is no pass")
        self.assertTrue(slanes._pm_sweep_due(self.p, 1), "so the counts stay due")
        self.env(PM_FAIL="")
        nxt = self.head("plugin/lib/sampo/b.py")
        self.assertEqual(self.verify(), ["shape", "full"], "the next head sweeps again")
        self.assertEqual(len(slanes.pm_full_passes(self.p)), passes + 1)
        self.assertEqual(slanes.pm_full_passes(self.p)[-1]["sha"], nxt)
        self.assertEqual(self.sweep_state()["k"], 0)

    def test_behind_affected_three_hours_since_the_last_full_run_make_the_next_head_a_full_run(self):
        self.swept()
        st = self.sweep_state()
        c.write_atomic(os.path.join(slanes._pm_dir(self.p), ".sweep"), json.dumps({**st, "t": st["t"] - 3 * 3600 - 5}))
        self.head("plugin/lib/sampo/a.py")
        self.assertEqual(self.verify(), ["shape", "full"])
        self.head("plugin/lib/sampo/a.py")
        self.assertEqual(self.verify(), ["shape", "part", "test_a"], "the sweep restarted the clock")

    def test_behind_affected_a_partial_verify_is_no_full_pass_for_ship_nor_proof_of_its_tree(self):
        self.swept()
        passes = slanes.pm_full_passes(self.p)
        self.assertEqual(len(passes), 1, "the sweep is the full pass sampo ship may push")
        sha = self.head("plugin/lib/sampo/b.py")
        self.verify()
        self.assertEqual(slanes.pm_full_passes(self.p), passes, "a partial verify adds none: ship keeps its fully verified commit")
        ctree = c.code_tree(self.repo, sha)
        self.assertIsNone(cli._pm_green_rec(self.p, ctree), "the partial record proves no full run on its tree")
        self.assertFalse(cli._pm_covers(self.p), "and the sentinel's full run is not skipped for it")
        self.assertEqual(self.rec(sha)["state"], "green")

    def test_behind_affected_a_head_no_test_reaches_runs_the_shape_pass_alone(self):
        self.swept()
        sha = self.head("notes/readme.txt")
        self.assertEqual(self.verify(), ["shape"])
        self.assertEqual((self.rec(sha)["state"], self.rec(sha).get("partial")), ("green", True))

    def test_behind_affected_no_affected_command_or_a_failing_selection_runs_the_whole_suite(self):
        self.swept()
        self.head("plugin/lib/sampo/a.py")
        with mock.patch.object(cli, "affected", side_effect=RuntimeError("no map")):
            self.assertEqual(self.verify(), ["shape", "full"])
        sha = self.head("plugin/lib/sampo/a.py")
        self.assertEqual(self.verify(), ["shape", "part", "test_a"], "a head that fell back to full counted as a sweep")
        self.assertTrue(self.rec(sha).get("partial"))

    def test_behind_affected_an_own_verify_command_is_never_narrowed(self):
        self.swept()
        self.env(FM_POSTMERGE_CMD=f"echo whole >> {self.mark}")
        self.head("plugin/lib/sampo/a.py")
        self.assertEqual(self.verify(), ["shape", "whole"], "the operator's command is the suite: cli.affected's template is not it")

    def test_behind_affected_several_pending_heads_are_covered_by_one_partial_probe_marked_partial(self):
        self.swept()
        a, b = self.head("plugin/lib/sampo/a.py"), self.head("plugin/lib/sampo/b.py")
        self.assertEqual(self.verify(), ["shape", "part", "test_a", "test_b"], "one probe of the range, both modules")
        for sha in (a, b):
            r = self.rec(sha)
            self.assertEqual((r["state"], r.get("partial"), r.get("narrowed")), ("green", True, False))
        self.assertIsNone(cli._pm_green_rec(self.p, c.code_tree(self.repo, b)))

    def test_behind_affected_a_red_affected_class_is_judged_and_reverted_as_a_full_run_red_is(self):
        self.swept()
        self.template("echo part {names} >> $PM_MARK; echo 'FAIL: t (test_a.A.t)'; exit 1")
        self.env(FM_POSTMERGE_RERUN="exit 1")
        sha = self.head("plugin/lib/sampo/a.py")
        self.verify()
        r = self.rec(sha)
        self.assertIn(r["state"], ("red", "reverted"))
        self.assertEqual(r.get("classes"), ["test_a.A"])
        self.assertTrue(r.get("partial"))

    def test_behind_affected_a_timed_out_sweep_is_a_sweep_again_on_its_retry(self):
        self.swept()
        sha = self.head("plugin/lib/sampo/a.py")
        slanes.pm_set(self.p, sha, sweep=True)
        self.assertEqual(self.verify(), ["shape", "full"], "a record that was to be a full run stays one")
        self.assertFalse(self.rec(sha).get("partial"))


class BehindKnownRed(BehindBase):
    """T-5982: the live train runs no gate of its own, so the behind run is where a lane's own criterion meets the merged
    head. 2026-11-22 15:01: `test_lane_land -k stale` (a lane's own criterion) went red on trunk because no map link and no
    run-cache class tied that module to the lane's diff, and the partial run never named it. The modules a landed lane's
    criteria name now always run, whole, beside what its change reaches."""

    def own_head(self, rel, ac):
        """A merge of a lane (touching rel) whose one criterion is `ac`'s command, recorded pending."""
        tid = json.JSONDecoder().raw_decode(self.fm("task", "new", "Own", "--type", "FEATURE", "--tier", "S", "--ac", ac,
                                                    "--step", "s", "--json").stdout)[0]["id"]
        self.n += 1
        self.git("checkout", "-qb", f"b-o{self.n}", self.trunk)
        with open(os.path.join(self.repo, rel), "a") as f:
            f.write(f"{self.n}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", f"o{self.n}")
        self.git("checkout", "-q", self.trunk)
        return self.merge(f"o{self.n}", tid)

    def test_a_landed_lanes_own_criterion_module_runs_though_nothing_links_it_to_the_change(self):
        self.swept()
        sha = self.own_head("plugin/lib/sampo/b.py", "stale :: python3 plugin/tests/run.py test_a -k stale")
        self.assertEqual(self.verify(), ["shape", "part", "test_a", "test_b"],
                         "test_a is the criterion's own, run whole; test_b is what b.py reaches")
        self.assertEqual(self.rec(sha)["state"], "green")

    def test_a_red_in_the_criterions_module_holds_the_merge_not_the_suite_that_ignored_it(self):
        self.swept()
        self.env(PM_FAIL="1", FM_POSTMERGE_RERUN="exit 1")  # the class stays red alone: no flake
        self.template("echo part {names} >> $PM_MARK; echo 'FAIL: t (test_a.A.t)'; test -z \"$PM_FAIL\"")
        sha = self.own_head("plugin/lib/sampo/b.py", "stale :: python3 plugin/tests/run.py test_a -k stale")
        self.assertEqual(self.verify()[:3], ["shape", "part", "test_a"])
        self.assertNotEqual(self.rec(sha)["state"], "green", "the red the criterion's module shows is not passed over")

    def test_two_lanes_landed_together_each_green_alone_run_every_lanes_criterion_on_the_combined_head(self):
        """A live train lands two lanes in one batch: the one behind run judges the merged head of both, so lane a's
        criterion module (test_b, linked to nothing a touches) runs beside what lane b's change reaches; a red there
        holds the merge (not green)."""
        self.swept()
        self.env(PM_FAIL="1", FM_POSTMERGE_RERUN="exit 1")  # the class stays red alone: no flake
        self.template("echo part {names} >> $PM_MARK; echo 'FAIL: t (test_b.B.t)'; test -z \"$PM_FAIL\"")
        first = self.own_head("notes/readme.txt", "alone :: python3 plugin/tests/run.py test_b -k alone")
        second = self.own_head("plugin/lib/sampo/a.py", "other :: true")
        ran = self.verify()
        self.assertEqual(ran[:2], ["shape", "part"], ran)
        self.assertIn("test_b", ran[2:], "lane one's own criterion module runs on the combined head")
        for sha in (first, second):
            self.assertNotEqual(self.rec(sha)["state"], "green", f"{ran} {self.rec(first)} {self.rec(second)}")

    def test_a_criterion_that_names_no_test_module_adds_nothing(self):
        self.swept()
        self.own_head("plugin/lib/sampo/b.py", "ok :: true")
        self.assertEqual(self.verify(), ["shape", "part", "test_b"])

    def test_pm_own_files_names_the_modules_of_every_listed_task_once_and_skips_a_missing_brief(self):
        a = json.JSONDecoder().raw_decode(self.fm("task", "new", "A", "--type", "FEATURE", "--tier", "S", "--ac",
                                                  "a :: python3 plugin/tests/run.py test_a test_b.Cls -k x", "--step", "s",
                                                  "--json").stdout)[0]["id"]
        self.assertEqual(slanes._pm_own_files(self.p, [a, a, "T-9999999"]),
                         ["plugin/tests/test_a.py", "plugin/tests/test_b.py"])
        self.assertEqual(slanes._pm_own_files(self.p, ()), [])


class StandingShip(AcceptCase):
    """T-5938: `sampo ship auto on` is a standing publish grant: with no task holding one, the land's ship still runs, on
    a grant that is never spent; a task's own publish grant wins and is spent as before. Nothing here starts a process:
    the ship's command is stubbed, so no pid of a stub reaches os.kill."""
    SHIPPED = "shipped 1 commit to origin main at " + "ab" * 20 + " (the publish grant is spent)."

    def on(self, why="the user: keep pushing to main"):
        from sampo import core
        core.update_meta(self.p, standing_ship={"on": True, "by": "test-session", "why": why, "at": core.now()})

    def run_ship(self, keep=False):
        with mock.patch.object(train, "_sampo", return_value=(0, self.SHIPPED, "")) as ran:
            got = train._ship(self.p, keep, self.repo)
        return got, ran

    def spent(self, tid):
        return "publish" not in (self.brief(tid).meta.get("allow") or [])

    def test_off_there_is_no_grant_and_no_ship(self):
        self.assertIsNone(train._grant_of(self.p))
        got, ran = self.run_ship()
        self.assertIsNone(got)
        ran.assert_not_called()

    def test_on_a_standing_grant_is_given_when_no_task_holds_one(self):
        from sampo import core
        self.on()
        g = train._grant_of(self.p)
        self.assertEqual(g.id, core.STANDING_SHIP)
        self.assertNotIn("publish", g.meta.get("allow") or [], "it is no task's grant: nothing for a spend to clear")

    def test_the_standing_grant_is_never_spent_by_a_ship_of_either_kind(self):
        from sampo import core, release
        self.on()
        for keep in (True, False):
            got, ran = self.run_ship(keep)
            self.assertEqual(got[0], core.STANDING_SHIP)
            self.assertEqual(ran.call_args[0][0], ["ship", "--task", core.STANDING_SHIP])
            self.assertEqual(train._grant_of(self.p).id, core.STANDING_SHIP, "still given after the ship")
        train._settle(self.p, core.STANDING_SHIP, 0, self.SHIPPED, {"final": True})
        train._settle(self.p, core.STANDING_SHIP, 0, self.SHIPPED, {"final": False, "land_pid": os.getpid()})
        self.assertFalse(train._give_back(self.p, core.STANDING_SHIP))
        release._spend(self.p, core.STANDING_SHIP, "test")  # the push's own spend: a no-op
        self.assertTrue(core.read_meta(self.p)["standing_ship"]["on"])
        self.assertEqual(train._grant_of(self.p).id, core.STANDING_SHIP)

    def test_the_ship_names_the_standing_grant_only_while_it_is_on(self):
        from sampo import core, release
        with self.assertRaises(Exception):
            release._named_grant(self.p, core.STANDING_SHIP, land=True)
        self.on()
        self.assertEqual(release._named_grant(self.p, core.STANDING_SHIP, land=True).id, core.STANDING_SHIP)
        self.fm("ship", "auto", "off")
        with self.assertRaises(Exception):
            release._named_grant(self.p, core.STANDING_SHIP, land=True)

    def test_only_the_lands_own_ship_may_name_the_standing_grant(self):
        """Round 2: any caller naming it (an agent's `sampo ship --task standing-ship`) is refused: the grant is the land's."""
        from sampo import core, release
        self.on()
        with self.assertRaises(Exception) as e:
            release._named_grant(self.p, core.STANDING_SHIP)
        self.assertIn("land's own ship", str(e.exception))
        r = self.fm("ship", "--task", core.STANDING_SHIP, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("land's own ship", r.stderr + r.stdout)

    def test_a_standing_ship_takes_no_test_or_remote_override(self):
        from sampo import core
        self.on()
        for extra in (["--test", "true"], ["--remote", "/elsewhere"]):
            with self.subTest(extra=extra):
                r = self.fm("ship", "--task", core.STANDING_SHIP, *extra, env={core.LAND_SHIP_ENV: "land"}, check=False)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("takes no --test or --remote", r.stderr + r.stdout)
        r = self.fm("ship", "--task", core.STANDING_SHIP, env={core.LAND_SHIP_ENV: "land"}, check=False)
        self.assertNotIn("land's own ship", r.stderr + r.stdout, "the land's marker is honoured")
        self.assertNotIn("takes no --test", r.stderr + r.stdout)

    def test_the_lands_ship_carries_the_marker_for_the_standing_grant_alone(self):
        """In line and detached: the env the land gives its ship says the land runs it (a task's own grant needs none)."""
        import sys
        from sampo import core
        self.on()
        got, ran = self.run_ship(keep=False)
        self.assertEqual(ran.call_args.kwargs["env"].get(core.LAND_SHIP_ENV), "land")
        probe = [sys.executable, "-c", f"import os; print('marker=' + os.environ.get({core.LAND_SHIP_ENV!r}, 'none'))"]
        rc, out = train._ship_run(self.repo, core.STANDING_SHIP, "a" * 40, argv=probe)  # a real child: no stubbed pid
        self.assertEqual(rc, 0)
        self.assertIn("marker=land", out)
        rc, out = train._ship_run(self.repo, "T-1", "a" * 40, argv=probe)
        self.assertIn("marker=none", out)
        b = self.brief(self.a_lane(1))
        b.meta["allow"] = ["publish"]
        core.save_brief(self.p, b)
        got, ran = self.run_ship(keep=False)
        self.assertEqual(got[0], b.id)
        self.assertNotIn(core.LAND_SHIP_ENV, ran.call_args.kwargs.get("env") or {})

    def test_the_marker_is_taken_in_both_spellings_and_never_reaches_a_task_grants_ship_in_line(self):
        """T-5938 review: FOREMAN_ and its SAMPO_ mirror. release.ship pops both (the short-circuit popped one); the in-line
        ship hands a task's grant's ship neither."""
        from sampo import core, release
        both = {core.LAND_SHIP_ENV: "land", "SAMPO_" + core.LAND_SHIP_ENV.partition("_")[2]: "land"}
        with mock.patch.dict(os.environ, both):
            self.assertTrue(release._land_marker())
            self.assertFalse([k for k in both if k in os.environ], "both spellings are gone from the ship's own env")
            self.assertFalse(release._land_marker())
        with mock.patch.dict(os.environ, {"SAMPO_" + core.LAND_SHIP_ENV.partition("_")[2]: "land"}):
            self.assertTrue(release._land_marker(), "the SAMPO_ spelling alone is the marker too")
        b = self.brief(self.a_lane(1))
        b.meta["allow"] = ["publish"]
        core.save_brief(self.p, b)
        with mock.patch.dict(os.environ, both):
            got, ran = self.run_ship(keep=False)
        self.assertEqual(got[0], b.id)
        sent = ran.call_args.kwargs.get("env") or {}
        self.assertFalse([k for k in both if k in sent], "a task grant's ship carries no marker: " + str(sent.keys() & both.keys()))

    def test_auto_is_the_subcommand_only_for_exactly_auto_on_off_or_status(self):
        """Round 2: a branch named auto still ships."""
        import types
        from sampo import cli, release
        for commits, is_auto in ((["auto", "on"], True), (["auto", "off"], True), (["auto", "status"], True),
                                 (["auto"], False), (["auto", "x"], False), (["auto", "on", "extra"], False),
                                 (["x", "auto", "on"], False), ([], False)):
            with self.subTest(commits=commits), mock.patch.object(cli, "cmd_ship_auto") as auto, \
                    mock.patch.object(release, "ship") as ship, mock.patch.object(cli, "resolve"):
                release.cmd_ship(types.SimpleNamespace(commits=commits, why=None))
                self.assertEqual((auto.called, ship.called), (is_auto, not is_auto))

    def say(self, text, session="s-1"):
        """The user's prompt as the prompt hook records it: a signed ledger event (the existing record of their words)."""
        from sampo import core
        core.log_event(self.p, "prompt", data={"text": text, "mode": "default", "headless": False}, session=session,
                       seal=core.prompt_sealer()[0])

    def test_a_prompt_with_no_flag_is_a_quote_only_when_it_was_logged_before_the_hook_wrote_one(self):
        """T-5938 review: the old shape (no `headless`) is accepted for a prompt older than PROMPT_FLAG_FROM alone: a
        flag-less prompt logged now (an older plugin copy, a hand-built call) is no quote, signed or not."""
        from sampo import core
        core.log_event(self.p, "prompt", data={"text": self.QUOTE, "mode": "default"}, session="s-1", seal=core.prompt_sealer()[0])
        self.assertTrue(self.ship_auto("on", why=self.QUOTE)[0])
        self.assertIsNone(core.standing_ship(self.p))
        with mock.patch.object(core, "PROMPT_FLAG_FROM", "2999-01-01T00:00:00Z"):  # as if the event predates the change
            err, _ = self.ship_auto("on", why=self.QUOTE)
        self.assertEqual(err, "")
        self.assertTrue(core.standing_ship(self.p))

    QUOTE = "make sure to keep pushing to main"

    def ship_auto(self, *words, why=None, p=None):
        """`sampo ship auto WORDS --why WHY` run in this process (the signed prompts are verified under this test's own key
        file, which a child process never has): (the refusal's text or "", what it printed)."""
        import argparse
        import contextlib
        from sampo import cli
        buf = io.StringIO()
        args = argparse.Namespace(commits=["auto", *words], why=why, json=False, project=None)
        with mock.patch.object(cli, "resolve", return_value=p or self.p), contextlib.redirect_stdout(buf):
            try:
                cli.cmd_ship_auto(args)
                return "", buf.getvalue()
            except cli.UsageError as e:
                return str(e), buf.getvalue()

    def test_on_needs_a_verbatim_quote_of_the_users_words_and_keeps_it(self):
        from sampo import core
        for why in (None, "keep pushing to main now", "make sure to keep pushing to main"):  # none; not said; not said
            with self.subTest(why=why):
                err, _ = self.ship_auto("on", why=why)
                self.assertIn("--why", err)
                self.assertIsNone(core.standing_ship(self.p))
        self.assertIn("prompt", err, "it names where it looked: the prompts on the ledger")
        self.say("and can you make sure to keep pushing to main?")  # a question is no ask: it is not a quote either
        self.assertTrue(self.ship_auto("on", why="can you make sure to keep pushing to main")[0])
        self.assertIsNone(core.standing_ship(self.p))
        self.say("please make sure to keep pushing to main")
        for why in ("keep pushing", "the user said hi there friend"):  # under four words; not in their words
            self.assertTrue(self.ship_auto("on", why=why)[0], why)
        err, _ = self.ship_auto("on", why="make sure  to keep pushing to main")
        self.assertEqual(err, "")  # whitespace and case don't matter, the words do
        self.assertEqual(core.standing_ship(self.p)["why"], "make sure to keep pushing to main")
        self.assertTrue(core.standing_ship(self.p)["by"])

    def test_the_quote_must_be_about_the_push_not_any_four_words_the_user_said(self):
        from sampo import core
        self.say("please make sure to keep pushing to main")
        self.assertTrue(self.ship_auto("on", why="please make sure")[0])
        self.assertIsNone(core.standing_ship(self.p))

    def test_an_earlier_phrase_with_push_or_main_in_it_is_no_keep_pushing_ask(self):
        """Round 3: the quote is found in a prompt that itself asks to keep main pushed, not in any prompt that holds the words."""
        from sampo import core
        for said in ("what does the push to main do for this task", "we should not push to main yet, ok", "never keep pushing to main",
                     "is it fine to keep pushing to main?", "keep pushing to main branches only"):
            self.say(said)
            for why in (said.rstrip("?"), "push to main for this task", "keep pushing to main", "to main yet, ok"):
                self.assertTrue(self.ship_auto("on", why=why)[0], (said, why))
        self.assertIsNone(core.standing_ship(self.p))

    def test_a_prompt_the_lane_pilot_sent_is_no_quote(self):
        from sampo import core
        core.log_event(self.p, "prompt", data={"text": "keep pushing to main", "mode": "default", "headless": True},
                       session="s-9", seal=core.prompt_sealer()[0])
        self.assertTrue(self.ship_auto("on", why="keep pushing to main")[0])
        self.assertIsNone(core.standing_ship(self.p))

    def test_a_quote_from_a_machine_text_or_an_unsigned_line_is_no_quote(self):
        from sampo import core
        core.log_event(self.p, "prompt", data={"text": "please always " + self.QUOTE}, session="s-1")  # no seal: not genuine
        self.assertTrue(self.ship_auto("on", why=self.QUOTE)[0])
        self.assertIsNone(core.standing_ship(self.p))

    def test_why_is_refused_on_anything_but_auto_on(self):
        self.say(self.QUOTE)
        for args in (["ship", "auto", "off"], ["ship", "auto", "status"], ["ship"], ["ship", "HEAD"], ["ship", "--check"]):
            with self.subTest(args=args):
                r = self.fm(*args, "--why", self.QUOTE, check=False)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("--why is for", r.stderr + r.stdout)

    def test_on_and_off_are_refused_in_a_lane_and_for_the_headless_pilot(self):
        from sampo import core
        self.say(self.QUOTE)
        self.a_lane(1)
        lane = os.path.join(self.tmp, "lane1")
        lane_p = core.find_project(lane, create=False)
        self.assertTrue(getattr(lane_p, "lane", None), "the lane's own project says it is a lane")
        err, _ = self.ship_auto("on", why=self.QUOTE, p=lane_p)
        self.assertIn("main checkout", err)
        with mock.patch.dict(os.environ, {"FOREMAN_PILOT_HEADLESS": "1"}):
            err, _ = self.ship_auto("on", why=self.QUOTE)
        self.assertIn("main checkout", err)
        self.assertIsNone(core.standing_ship(self.p))
        self.assertEqual(self.ship_auto("on", why=self.QUOTE)[0], "", "the main session may")
        self.assertIn("main checkout", self.ship_auto("off", p=lane_p)[0], "round 3: off is the main session's too")
        with mock.patch.dict(os.environ, {"FOREMAN_PILOT_HEADLESS": "1"}):
            self.assertIn("main checkout", self.ship_auto("off")[0])
        self.assertTrue(core.standing_ship(self.p), "still on: a lane's off did nothing")
        self.assertEqual(self.ship_auto("status", p=lane_p)[0], "", "status stays open to any session")
        self.assertEqual(self.ship_auto("off")[0], "", "the main session may")

    def test_the_prompt_hook_turns_it_on_from_plain_words(self):
        """1c: 'keep pushing to main' in the user's own short sentence, like the other plain-word verbs."""
        for words in ("keep pushing to main", "Keep main pushed.", "please keep shipping to main"):
            self.assertEqual((self.find_ship_verb(words) or {}).get("on"), True, words)
        for words in ("don't keep pushing to main", "should we keep pushing to main?", "keep pushing to main branches only",
                      "keep pushing to main if CI is green", "do not keep main pushed", "set the budget to unlimited",
                      "```keep pushing to main```", "and can you make sure to keep pushing to main?"):
            self.assertIsNone(self.find_ship_verb(words), words)

    def find_ship_verb(self, words):
        from sampo import userwords
        for v, args in userwords.find(words):
            if v.name == "standing-ship":
                return args
        return None

    def test_the_hook_verb_records_the_users_words_as_the_quote(self):
        from sampo import core, userwords
        self.say("keep pushing to main")
        lines = userwords.apply(self.p, "keep pushing to main", session="s-1")
        self.assertTrue(any("standing ship" in x for x in lines), lines)
        rec = core.standing_ship(self.p)
        self.assertEqual(rec["why"], "keep pushing to main")
        self.assertIn("prompt hook", rec["by"])

    def test_a_tasks_own_grant_wins_and_is_spent_as_today(self):
        self.on()
        tid = self.a_lane(1)
        b = self.brief(tid)
        b.meta["allow"] = ["publish"]
        from sampo import core
        core.save_brief(self.p, b)
        self.assertEqual(train._grant_of(self.p).id, tid)
        got, ran = self.run_ship(keep=False)
        self.assertEqual(got[0], tid)
        self.assertEqual(ran.call_args[0][0], ["ship", "--task", tid])
        self.assertTrue(self.spent(tid), "the task's own grant is spent by its last ship")
        self.assertEqual(train._grant_of(self.p).id, core.STANDING_SHIP, "the task's grant is gone: the standing one remains")

    def test_cli_on_records_who_and_why_and_off_and_status_follow(self):
        from sampo import core
        self.say("please keep pushing to main now")
        err, text = self.ship_auto("on", why="keep pushing to main")
        self.assertEqual(err, "")
        self.assertIn("on", text)
        rec = core.read_meta(self.p)["standing_ship"]
        self.assertTrue(rec["on"])
        self.assertEqual(rec["why"], "keep pushing to main")
        self.assertTrue(rec["by"] and rec["at"])
        self.assertIn("keep pushing to main", self.ship_auto("status")[1])
        self.ship_auto("off")
        self.assertIsNone(train._grant_of(self.p))
        self.assertIn("off", self.ship_auto("status")[1])


class SlimGate(TrainCase):
    """T-5935: the live front gate does only what the train's changed files call for: the hook bench runs when a lane
    changes a file the hooks load, the repo-shape run takes the classes whose inputs changed, the base bench runs once
    per base, and each land_train event says where its seconds went."""

    def hook_map(self, *files):
        from sampo import core as c
        p = c.find_project(self.repo)
        with open(os.path.join(p.dir, "hook-modules.json"), "w", encoding="utf-8") as f:
            json.dump({"sha": self.git("rev-parse", "HEAD"), "map": {"Stop": list(files)}}, f)
        return p

    def timed(self, p, name, text="x = 1\n"):
        """(the rows _live_timing gave, the files the self-change gate was asked about) for a commit that adds `name`."""
        from sampo import selfgate
        base = self.git("rev-parse", "HEAD")
        os.makedirs(os.path.dirname(os.path.join(self.repo, name)), exist_ok=True)
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        self.git("add", name)
        self.git("commit", "-qm", f"change {name}")
        calls, self.quick = [], []  # the gate asked with its bench / without it (timing=False)
        with mock.patch.object(selfgate, "gate", lambda *a, **k: (calls if k.get("timing", True) else self.quick).append(a[4]) or []):
            rows = train._live_timing(p, self.repo, self.repo, base)
        return rows, calls

    def test_slim_a_train_that_touches_no_hook_loaded_file_runs_no_bench(self):
        mapped = "plugin/lib/sampo/core.py"
        p = self.hook_map(mapped)
        for name, called in (("docs/board.md", False), ("plugin/web/page.js", False),
                             ("plugin/lib/sampo/unloaded.py", False), (mapped, True)):
            rows, calls = self.timed(p, name)
            self.assertEqual(bool(calls), called, f"{name}: the self-change gate (and its bench) ran: {calls}")
            self.assertEqual(rows, [])
            self.assertEqual(bool(self.quick), name.startswith("plugin/") and not called,
                             f"{name}: a skipped bench still ran the footprint and verb rows on a plugin change: {self.quick}")

    def test_slim_two_lanes_each_under_the_footprint_ratchet_are_held_when_the_merged_tree_crosses_it(self):
        from sampo import selfgate
        os.makedirs(os.path.join(self.repo, "plugin", "lib"))
        with open(os.path.join(self.repo, "plugin", "lib", "fmguard.py"), "w") as f:
            f.write("x = 1\n")
        self.git("add", "plugin")
        self.git("commit", "-qm", "a Sampo tree")
        p = self.hook_map("plugin/lib/sampo/core.py")  # the bench is skipped: neither new file is one a hook loads
        base = self.git("rev-parse", "HEAD")
        for name in ("plugin/web/a.js", "plugin/web/b.js"):  # two lanes, each +1.5% alone (under the 2% ratchet)
            os.makedirs(os.path.join(self.repo, os.path.dirname(name)), exist_ok=True)
            with open(os.path.join(self.repo, name), "w") as f:
                f.write("1\n")
        self.git("add", "plugin")
        self.git("commit", "-qm", "both lanes merged")
        sizes = iter([{"rules chars": 1000}, {"rules chars": 1030}])  # base, then the merged tree: +3%
        with mock.patch.object(selfgate, "_footprint", lambda *a: next(sizes)), \
                mock.patch.object(selfgate, "_corpus", lambda: []), mock.patch.object(selfgate, "_verdicts", lambda *a: []), \
                mock.patch.object(selfgate, "_paired", side_effect=AssertionError("the bench ran")):
            rows = train._live_timing(p, self.repo, self.repo, base)
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0].startswith("footprint rules chars 1000 -> 1030"), rows)
        from sampo import core as c
        self.assertTrue(c.read_meta(p).get(train.LIVE_TIMING_HOLD), "the next land judges each lane")

    def test_slim_with_no_hook_map_a_docs_only_train_runs_no_bench_but_a_plugin_file_does(self):
        from sampo import core as c
        p = c.find_project(self.repo)
        self.assertFalse(self.timed(p, "docs/board.md")[1])
        self.assertTrue(self.timed(p, "plugin/lib/sampo/unknown.py")[1], "an unknown map times as before")

    def test_slim_a_skipped_bench_is_logged_and_clears_a_standing_timing_hold(self):
        from sampo import core as c
        p = self.hook_map("plugin/lib/sampo/core.py")
        c.update_meta(p, **{train.LIVE_TIMING_HOLD: "old"})
        self.timed(p, "docs/board.md")
        self.assertFalse(c.read_meta(p).get(train.LIVE_TIMING_HOLD))
        self.assertTrue(c.ledger_tail(p, 50, ["live_timing_skipped"]))

    def test_slim_only_the_shape_classes_whose_inputs_changed_run(self):
        # test_rename_scan git-greps the whole repo for the old namespace: it runs for every narrowed train
        self.assertEqual(train.shape_classes(["docs/board.md", "research/x.md"]), ["test_rename_scan"])
        self.assertEqual(train.shape_classes(["docs/map/ops-1.md"]), ["test_docs", "test_rename_scan"])
        self.assertEqual(train.shape_classes(["CHANGELOG.md", "docs/board.md"]), ["test_docs", "test_rename_scan"])
        self.assertEqual(train.shape_classes(["research/models/frozen.json"]), ["test_docs", "test_rename_scan"],
                         "the site build reads the frozen models")
        self.assertIsNone(train.shape_classes(["MASTER.md"]), "test_rename_scan scans the front docs too")
        self.assertIsNone(train.shape_classes(["docs/board.md", "bench/a.txt"]), "a file no rule knows may reach any class")
        self.assertIsNone(train.shape_classes(["plugin/lib/sampo/x.py"]), "the plugin is every class's input")
        self.assertIsNone(train.shape_classes(["docs/board.md", "plugin/tests/test_a.py"]))
        self.assertIsNone(train.shape_classes([]), "no file list: every class, as before")
        self.assertIsNone(train.shape_classes(None))

    def test_slim_live_shape_runs_no_class_for_docs_and_the_subset_for_a_map_page(self):
        from sampo import core as c
        p = c.find_project(self.repo)
        ran = []

        def shape_run(wt, wall=None, classes=None):
            ran.append(classes)
            return "green", ""
        with mock.patch.object(train, "_shape_tree", lambda wt: True), mock.patch.object(train, "_shape_run", shape_run):
            self.assertEqual(train._live_shape(self.repo, p, self.repo, "b", files=["docs/board.md"]).verdict, "green")
            self.assertEqual(ran, ["test_rename_scan"], "a docs-only train still runs the old-namespace grep, nothing else")
            train._live_shape(self.repo, p, self.repo, "b", files=["docs/map/ops-1.md"])
            self.assertEqual(ran[1:], ["test_docs, test_rename_scan"])
            train._live_shape(self.repo, p, self.repo, "b", files=["plugin/lib/sampo/x.py"])
            train._live_shape(self.repo, p, self.repo, "b")
            self.assertEqual(ran[2:], [None, None], "a plugin change, or no list, runs the whole set")

    def test_slim_the_base_bench_runs_once_per_base(self):
        from sampo import selfgate
        benches = []
        slow = {"Stop": {"p95": 10.0, "cpu_p95": 10.0}}
        with mock.patch.object(selfgate, "_bench", lambda plugin: benches.append(plugin) or (slow, {})), \
                mock.patch.object(selfgate, "_load_pc", lambda: 0.5):
            cache = {}
            selfgate._paired("base", "head-1", cache)
            first = len(benches)
            self.assertEqual(first, 2 * selfgate.PAIRS, "the first train benches base and head")
            selfgate._paired("base", "head-2", cache)  # a second train on the same base: only the head again
            self.assertEqual(len(benches) - first, selfgate.PAIRS)
            self.assertEqual(benches.count("base"), selfgate.PAIRS, "the base's bench ran once")

    def test_slim_each_land_train_event_carries_per_phase_seconds(self):
        from sampo import core as c
        p = c.find_project(self.repo)
        got = {"merged": ["T-1"], "left_out": [], "minutes": {},
               "phases": {"holds": 3.21, "smoke": 0.4, "shape": 12.0, "timing": 0.0}}
        train._train_times(p, "L-1", 1, got, {}, 20.0, closes=5.5)
        ev = c.ledger_tail(p, 50, ["land_train"])[-1]["data"]
        self.assertEqual(ev["phases"], {"holds": 3.2, "smoke": 0.4, "shape": 12.0, "timing": 0.0, "closes": 5.5})
        train._train_times(p, "L-1", 2, {"merged": ["T-2"], "left_out": [], "minutes": {}}, {}, 20.0)
        self.assertEqual(c.ledger_tail(p, 50, ["land_train"])[-1]["data"]["phases"],
                         {"holds": 0.0, "smoke": 0.0, "shape": 0.0, "timing": 0.0, "closes": 0.0},
                         "an old-shaped result still records every phase")

    def test_slim_a_live_train_hands_its_changed_files_to_the_shape_and_timing_phases(self):
        from sampo import core as c
        self.fm("lane", "mode", "live")
        tid = self.lane(1, "docs/board.md")
        p = c.find_project(self.repo)
        seen = {}

        def shape(wt, p=None, main=None, base=None, classes=None, files=None, own=()):
            seen.setdefault("shape", files)
            return train.ShapeVerdict("green")

        def timing(p, main, wt, base, files=None):
            seen.setdefault("timing", files)
            return []
        with mock.patch.dict(os.environ, {"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"}), \
                mock.patch.object(train, "_live_shape", shape), mock.patch.object(train, "_live_timing", timing):
            got = train.train(p, self.repo, [tid])
        self.assertEqual(seen["shape"], ["docs/board.md"])
        self.assertEqual(seen["timing"], ["docs/board.md"])
        self.assertEqual(sorted(got["phases"]), ["holds", "shape", "smoke", "timing"])
        self.assertTrue(all(isinstance(v, float) and v >= 0 for v in got["phases"].values()), got["phases"])


class LeftoverBase(test_lane_land.LandCase):
    """T-5912: 2026-11-22 from 11:33 a land spent its time on about twenty leftover closes that could not pass (each failed
    on the same trunk-red classes) before its first train. Leftovers now wait for the first train's merges, run up to
    `_close_par` at once, and one whose last refusal names only classes red on trunk is skipped until they go green."""
    RED = "test_x.RedOne"
    GATE = "python3 plugin/tests/run.py test_x"

    def red_why(self, *names):
        """A gate's refusal as _why_red writes it (the form a close's Log line carries)."""
        return f"{self.GATE} on 3 affected test module(s), failing {', '.join(names or (self.RED,))}; blamed: nothing"

    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)
        self.p = c.find_project(self.repo)

    def refuse(self, tid, why):
        """The Log line a refused close writes (train._close's own format), for the brief and lane tip as they are now."""
        b = c.find_brief(self.p, tid)
        sha = train._git(b.meta["lane"], "rev-parse", "--short", "HEAD").stdout.strip()
        b.append_log(f"land: close refused: {why} @ {train._brief_hash(b)} {sha} {train._code_id()}")
        c.save_brief(self.p, b)

    def new_lane(self, n=9):
        tid = self.lane(n, f"new{n}.txt", when=test_lane_land.OLD)
        self.audited(tid)
        self.returned(tid, f"Lane {n}")
        return tid

    def run_land(self, known=None, par=1, close=None):
        """A land in this process; `known(main, classes)` replaces the known-red question, `close` the close itself.
        Returns (result, stderr text, events: ('train', ids) / ('trained', ids) / ('close', id) in the order they came)."""
        events, err = [], io.StringIO()
        real_close, real_train = train._close, train.train

        def closing(p, main, tid, *a, **k):
            events.append(("close", tid))
            return close(p, main, tid, *a, **k) if close else real_close(p, main, tid, *a, **k)

        def trained(p, main, ids, *a, **k):
            events.append(("train", list(ids)))
            try:
                return real_train(p, main, ids, *a, **k)
            finally:
                events.append(("trained", list(ids)))
        with self.in_project(), contextlib.redirect_stderr(err), mock.patch.object(train, "_close", closing), \
                mock.patch.object(train, "train", trained), mock.patch.object(train, "_close_par", par if callable(par) else (lambda: par)), \
                mock.patch.object(train, "KNOWN_RED_POLL_S", 0.0), \
                mock.patch.object(train, "_known_red", known or (lambda main, classes: {})):
            got = train.land(self.p, self.repo, 1)
        return got, err.getvalue(), events


class LeftoverKnownRed(LeftoverBase):
    def red_now(self, main, classes):
        return {k: 7.0 for k in classes if k == self.RED}

    def test_a_leftover_refused_only_on_known_red_classes_is_skipped_with_one_line(self):
        a, b = self.leftovers(2)
        self.refuse(a, self.red_why())
        self.new_lane()
        got, err, events = self.run_land(self.red_now)
        closed = [t for k, t in events if k == "close"]
        self.assertNotIn(a, closed, err)
        self.assertIn(b, closed, "a leftover with no such refusal closes as before")
        self.assertEqual(len(re.findall(rf"(?m)^sampo lane land: {a}: close skipped: .*{self.RED}", err)), 1, err)
        hand = dict(got["hand"])
        self.assertIn("red on trunk", hand[a], hand)
        self.assertEqual(c.find_brief(self.p, a).status, "active", "left merged and open: no verdict on it")

    def test_the_skipped_leftover_is_requeued_when_the_classes_go_green(self):
        a, b = self.leftovers(2)
        self.refuse(a, self.red_why())
        self.new_lane()
        asked = []

        def red_then_green(main, classes):
            asked.append(set(classes))
            return self.red_now(main, classes) if len(asked) == 1 else {}
        got, err, events = self.run_land(red_then_green)
        closed = [t for k, t in events if k == "close"]
        self.assertIn(a, closed, err)
        self.assertIn(f"sampo lane land: {a}: its failing classes are no longer red on trunk: the close is queued again", err)
        self.assertNotIn(a, [t for t, why in got["hand"] if "red on trunk" in why], got["hand"])
        self.assertEqual(asked[0], {self.RED}, "only the skipped lane's classes are asked about")

    def own_leftover(self, n):
        """T-5982: a merged, open leftover whose own diff is the red class's test file (test_x.py)."""
        tid = self.lane(n, "test_x.py", when=test_lane_land.OLD)
        self.audited(tid)
        self.returned(tid, f"Lane {n}")
        self.train(tid)
        return tid

    def test_a_leftover_that_changed_the_red_test_itself_is_closed_not_waited_out(self):
        a, = self.leftovers(1)
        o = self.own_leftover(2)
        self.refuse(a, self.red_why())
        self.refuse(o, self.red_why())
        self.new_lane()
        got, err, events = self.run_land(self.red_now)
        closed = [t for k, t in events if k == "close"]
        self.assertNotIn(a, closed, err)
        self.assertIn(o, closed, "its own test is red: no known-red wait, the close runs and holds the verdict")
        self.assertNotIn(f"{o}: close skipped", err)

    def test_a_fix_the_refusal_waits_for_never_owns_a_lanes_own_red_test(self):
        a, = self.leftovers(1)
        o = self.own_leftover(2)
        fix = self.fm_json("task", "new", "Trunk red", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")["id"]
        b = c.find_brief(self.p, fix)
        b.meta["trunk_red"] = [self.RED]
        c.save_brief(self.p, b)
        why = self.red_why()
        self.assertEqual(train._fix_open(self.p, why, a), fix, "a lane that left the test alone waits for the FIX")
        self.assertEqual(train._fix_open(self.p, why, o), "", "a lane that changed the test itself does not")
        self.assertFalse(train._fix_wait(self.p, self.repo, o, why))
        self.assertNotIn("close_waits_on", c.find_brief(self.p, o).meta)

    def test_a_refusal_naming_a_class_that_is_not_red_is_closed_as_before(self):
        a, b = self.leftovers(2)
        self.refuse(a, self.red_why(self.RED, "test_y.Other"))
        self.new_lane()
        got, err, events = self.run_land(self.red_now)
        self.assertIn(a, [t for k, t in events if k == "close"], err)
        self.assertNotIn("close skipped", err)

    def test_a_refusal_the_brief_or_tip_has_outlived_is_not_trusted(self):
        a, = self.leftovers(1)
        self.refuse(a, self.red_why())
        b = c.find_brief(self.p, a)
        sha = train._git(b.meta["lane"], "rev-parse", "--short", "HEAD").stdout.strip()
        self.assertEqual(train._refusal_classes(b, sha), {self.RED})
        self.assertEqual(train._refusal_classes(b, "0" * 7), set(), "the lane moved")
        b = c.find_brief(self.p, a)
        b.sections = [(h, t if h.strip() == "Log" else t + "\nmore words\n") for h, t in b.sections]
        c.save_brief(self.p, b)
        self.assertEqual(train._refusal_classes(c.find_brief(self.p, a), sha), set(), "the brief changed")
        self.refuse(a, self.red_why() + " tail…")
        self.assertEqual(train._refusal_classes(c.find_brief(self.p, a), sha), set(), "a line cut short may hide a class")
        self.refuse(a, "the audit asks for changes")
        self.assertEqual(train._refusal_classes(c.find_brief(self.p, a), sha), set(), "no failing class named")
        self.refuse(a, self.red_why())
        b = c.find_brief(self.p, a)
        b.append_log(f"{train.QUEUED_AGAIN}: T-1 landed")
        c.save_brief(self.p, b)
        self.assertEqual(train._refusal_classes(c.find_brief(self.p, a), sha), set(), "a released FIX ends the refusal")

    def test_the_classes_come_only_from_the_structured_failed_test_parts(self):
        R, G = self.RED, self.GATE
        both = {R, "test_y.Other"}
        for why in (  # the gate's reason (a test note with a comma and a nested parenthesis), finish's evidence line, run.py's lines
                f"{G} on 3 affected test module(s), failing {R} (test_a: AssertionError: x (y), z), test_y.Other; "
                f"blamed: it changed test_x.py tail",
                f"✗ exit 1 · 2 of 2 failed: {R}, test_y.Other · {G} → ✗ exit 1 · Ran 4 tests",
                f"criterion 1: {G} → ✗ exit 1 · failed class: {R} failed class: test_y.Other"):
            self.assertEqual(train._failed_named(why), both, why)
        for why in (
                f"make lint → ✗ exit 2 · test_x.RedOne is not formatted",  # a lint failure that mentions a red class
                f"make build failed: cannot import test_x.RedOne",  # a build failure's text
                f"make build on 3 affected test module(s), failing {R}",  # a gate that is no run.py
                f"✗ exit 1 · 2 of 2 failed: {R} · {G} → ✗ exit 1 · failed class: {R}; make lint → ✗ exit 2 · {R} is long",
                f"{G} on 3 affected test module(s), failing {R}, 3 left out",  # not every class is known
                f"{G} on 3 affected test module(s), failing {R} (test_a: cut",  # a note that never closes
                f"2 of 2 failed: {R}, 3 left out · {G} → ✗ exit 1",
                f"the audit asks for changes; {R} is red"):
            self.assertEqual(train._failed_named(why), set(), why)

    def test_a_refusal_that_failed_a_build_or_lint_command_is_closed_though_it_names_a_red_class(self):
        a, b = self.leftovers(2)
        self.refuse(a, f"✗ exit 1 · 2 of 2 failed: {self.RED} · {self.GATE} → ✗ exit 1 · failed class: {self.RED}; "
                       f"make lint → ✗ exit 2 · {self.RED} is long")
        self.new_lane()
        got, err, events = self.run_land(self.red_now)
        self.assertIn(a, [t for k, t in events if k == "close"], err)
        self.assertNotIn("close skipped", err)

    def test_run_py_s_answer_is_read_and_a_run_py_that_cannot_answer_knows_nothing_red(self):
        main = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, main, True)
        self.assertEqual(train._known_red(main, {self.RED}), {}, "no run.py there")
        os.makedirs(os.path.join(main, "plugin", "tests"))
        run_py = os.path.join(main, "plugin", "tests", "run.py")
        for src, want in (
                ("import json, sys\nargs = sys.argv[1:]\nassert args[0] == '--known-red'\n"
                 "print(json.dumps({k: 3.5 for k in args[1::2]} | {'test_z.Extra': 1}))\n", {self.RED: 3.5}),
                ("import sys\nsys.exit(3)\n", {}), ("print('not json')\n", {}), ("print('[1]')\n", {})):
            with open(run_py, "w") as f:
                f.write(src)
            self.assertEqual(train._known_red(main, {self.RED}), want, src)


class LeftoverOrder(LeftoverBase):
    def test_the_leftovers_close_after_the_first_train_not_in_front_of_it(self):
        left = self.leftovers(3)
        new = self.new_lane()
        got, err, events = self.run_land()
        kinds = [k for k, _ in events]
        self.assertEqual(events[0], ("train", [new]), events)
        done = kinds.index("trained")
        closes = [i for i, k in enumerate(kinds) if k == "close"]
        self.assertTrue(closes and min(closes) > done, f"no close begins before the first train has merged: {events}")
        self.assertEqual(sorted(t for k, t in events if k == "close"), sorted(left + [new]), events)
        self.assertEqual(events[done + 1], ("close", new), "the train's own lane closes first")

    def test_with_nothing_to_train_the_leftovers_close_at_once(self):
        left = self.leftovers(2)
        got, err, events = self.run_land()
        self.assertEqual(sorted(t for k, t in events if k == "close"), sorted(left), events)
        self.assertNotIn("train", [k for k, _ in events])

    def land_concurrency(self, par, n=4):
        self.leftovers(n)
        lock, now, most = threading.Lock(), [0], [0]

        def slow(p, main, tid, *a, **k):
            with lock:
                now[0] += 1
                most[0] = max(most[0], now[0])
            time.sleep(0.4)
            with lock:
                now[0] -= 1
            return "", f"closed {tid}"
        self.run_land(par=par, close=slow)
        return most[0]

    def test_leftover_closes_run_in_parallel_up_to_the_budget(self):
        self.assertEqual(self.land_concurrency(3), 3)

    def test_one_close_at_a_time_when_the_host_has_no_room(self):
        self.assertEqual(self.land_concurrency(1), 1)

    def test_the_close_budget_is_asked_again_when_the_leftovers_are_drained(self):
        self.leftovers(2)
        asked = []
        self.run_land(par=lambda: asked.append(1) or 1)
        self.assertGreaterEqual(len(asked), 2, "once for the closer, once at the drain: the host's load changes in an hour")

    def test_the_budget_follows_the_hosts_idle_cores_and_is_never_below_one_nor_above_the_cap(self):
        for cpus, load, want in ((16, 0.5, 3), (16, 9.0, 1), (16, 15.9, 1), (4, 0.0, 1), (64, 0.0, train.LEFTOVER_CLOSERS)):
            with mock.patch("os.cpu_count", return_value=cpus), mock.patch("os.getloadavg", return_value=(load, 0, 0)):
                self.assertEqual(train._close_par(), want, (cpus, load))
        with mock.patch("os.getloadavg", side_effect=OSError):
            self.assertGreaterEqual(train._close_par(), 1)


class CloserParallel(unittest.TestCase):
    """T-5912: the closer's extra threads take only leftovers' batches, and only after one leftover has finished."""

    def test_the_extra_threads_wait_for_the_first_leftover_and_take_no_train_lane(self):
        go, began, lock = threading.Event(), [], threading.Lock()

        def work(ids):
            with lock:
                began.append(ids[0])
            go.wait(10)
            return True  # the close passed: its gate pass is on record for the others to reuse
        closer = train._Closer(3)
        closer.work = work
        closer.put(["T-1"], left=True)
        closer.put(["T-2"], left=True)
        closer.put(["T-3"], left=True)
        closer.put(["T-9"])  # a lane the train merged: the worker thread's, never an extra one's
        time.sleep(0.3)
        self.assertEqual(began, ["T-1"], "none beside the first leftover until it has finished")
        go.set()
        closer.join()
        self.assertEqual(sorted(began), ["T-1", "T-2", "T-3", "T-9"], began)
        self.assertTrue(closer.idle())

    def test_a_refused_leftover_starts_no_extra_thread_only_a_passing_close_does(self):
        # T-5912 round 2: a refused close records no gate pass, so letting the others begin beside it ran the full gate up to 3 times
        lock, now, most, order = threading.Lock(), [0], [0], []

        def work(ids):
            with lock:
                now[0] += 1
                most[0] = max(most[0], now[0])
                order.append(ids[0])
            time.sleep(0.1)
            with lock:
                now[0] -= 1
            return ids[0] == "T-3"  # T-1 and T-2 are refused, T-3 passes
        closer = train._Closer(3)
        closer.work = work
        for t in ("T-1", "T-2", "T-3", "T-4", "T-5"):
            closer.put([t], left=True)
        closer.join()
        self.assertEqual(order[:3], ["T-1", "T-2", "T-3"], "one at a time while every close is refused")
        self.assertEqual(sorted(order), ["T-1", "T-2", "T-3", "T-4", "T-5"], order)
        self.assertEqual(most[0], 2, "beside each other only after T-3 passed (T-4 and T-5), never before")

    def test_the_budget_is_set_again_at_a_drain_and_wakes_the_extras(self):
        closer = train._Closer(1)
        closer.set_par(3)
        self.assertEqual(closer.par, 3)
        closer.set_par(0)
        self.assertEqual(closer.par, 1, "never below one")

    def test_a_closer_of_one_never_runs_two_at_once(self):
        lock, now, most = threading.Lock(), [0], [0]

        def work(ids):
            with lock:
                now[0] += 1
                most[0] = max(most[0], now[0])
            time.sleep(0.05)
            with lock:
                now[0] -= 1
        closer = train._Closer(1)
        closer.work = work
        for t in ("T-1", "T-2", "T-3"):
            closer.put([t], left=True)
        closer.join()
        self.assertEqual(most[0], 1)


# T-5982: a lane's own tests are never excused as known red (a class red on the base too, a trunk red, a baseline red)
KNOWN_ENV = ("# map-links: a.txt\nimport os, unittest\n\n\nclass {0}(unittest.TestCase):\n    def test_it(self):\n"
             "        self.assertFalse(os.environ.get('GATE_ENV_RED'), 'known red here')\n")


class OwnTestsKnownRed(unittest.TestCase):
    # every criterion shape the real briefs hold (state/projects/*/tasks/*.md verify commands), and the modules each names
    FORMS = [
        ("python3 plugin/tests/run.py test_a", ["test_a"]),
        ("python3 plugin/tests/run.py test_a test_b test_c", ["test_a", "test_b", "test_c"]),
        ("python3 plugin/tests/run.py test_a -k stale", ["test_a"]),
        ("python3 plugin/tests/run.py -k test_a", []),
        ("python3 plugin/tests/run.py test_a.PostMergeVerify", ["test_a"]),
        ("python3 plugin/tests/run.py test_a.Lanes.test_x", ["test_a"]),
        ("python3 plugin/tests/run.py test_a::Lanes test_b::C::test_y", ["test_a", "test_b"]),
        ("nice -n 10 python3 plugin/tests/run.py test_a 2>&1 | tail -30", ["test_a"]),
        ("env FOREMAN_NO_TEST_CACHE=1 python3 plugin/tests/run.py test_a", ["test_a"]),
        ("python/cpython-3.12-linux-x86_64-gnu/bin/python3.12 plugin/tests/run.py test_a", ["test_a"]),
        ("python3 plugin/tests/run.py test_a test_b && cd mods/foreman-ui && ~/.local/bin/claude plugin test .", ["test_a", "test_b"]),
        ("python3 -m unittest test_a", ["test_a"]),
        ("python3 -m unittest -q test_a test_b", ["test_a", "test_b"]),
        ("python3 -m unittest plugin.tests.test_a.Cls.test_x", ["test_a"]),
        (".venv/bin/python -m unittest discover -s ../tests -p test_a.py", ["test_a"]),
        (".venv/bin/python -m unittest discover -s ../tests -p 'test_a*.py'", ["test_a*"]),
        (".venv/bin/python -m unittest discover -s ../tests -p 'test_*.py'", []),
        (".venv/bin/python test_a.py", ["test_a"]),
        ("bash -c 'cd app && ../.venv/bin/python -m unittest discover -s ../tests -p test_a.py -q'", ["test_a"]),
        ("pytest plugin/tests/test_a.py::C::test_y -k word", ["test_a"]),
        ("pytest plugin/tests/test_a.py plugin/tests/test_b.py", ["test_a", "test_b"]),
        ("python3 -m pytest plugin.tests.test_a", ["test_a"]),
        ("true", []),
        ("", []),
    ]

    def test_every_criterion_form_in_the_real_briefs_names_its_whole_modules(self):
        for cmd, want in self.FORMS:
            self.assertEqual(c.own_test_modules([], [cmd]), want, cmd)

    def test_own_modules_come_from_the_diff_and_the_verify_commands(self):
        own = c.own_test_modules(["plugin/tests/test_a.py", "plugin/lib/sampo/x.py", "docs/a.md", "tests/b_test.py"],
                                 ["python3 plugin/tests/run.py test_b test_run -k KnownRed", "true", "pytest t/test_c.py -k test_x"])
        self.assertEqual(own, ["b_test", "test_a", "test_b", "test_c", "test_run"])
        self.assertIsNone(c.own_test_modules(None, ["x"]), "a diff that could not be listed: unknown")

    def test_a_name_is_own_by_its_module_whatever_a_k_word_says(self):
        """-k words never narrow: the train's reds are class-level (`mod.Class`), which no word matches (`-k stale` against
        test_lane_land.LaneLand), so a module a criterion names is the lane's whole."""
        own = c.own_test_modules(["plugin/tests/test_a.py"], ["python3 plugin/tests/run.py test_lane_land -k stale"])
        for n in ("test_a.Any.test_x", "plugin/tests/test_a.py::Any::test_x", "test_lane_land.LaneLand", "test_a",
                  "test_lane_land::LaneLand", "plugin.tests.test_lane_land.LaneLand", "test_lane_land.LaneLand.test_other"):
            self.assertTrue(c.is_own_test(n, own), n)
        for n in ("test_run.Other", "test_b.KnownRed", "test_ab.Any", "test_lane.LaneLand"):
            self.assertFalse(c.is_own_test(n, own), n)
        self.assertTrue(c.is_own_test("test_zzz.Any", None), "unknown own: every test counts as the lane's")
        self.assertTrue(c.is_own_test("test_a_more.X", c.own_test_modules([], ["run.py test_a*"])), "a glob names its modules")


class GateKnownRed(__import__("test_train_flaky").Gate):
    """The train gate with two base-red (known red) tests: the lane that edits one is held with it named; the one it
    left alone is the base's and does not hold the gate."""
    def setUp(self):
        super().setUp()
        self.red = {"GATE_ENV_RED": "1"}
        self.base("plugin/tests/test_mine.py", KNOWN_ENV.format("Mine"))
        self.base("plugin/tests/test_idle.py", KNOWN_ENV.format("Idle"))
        self.green_env()

    def test_a_lane_that_edits_a_known_red_test_is_held_with_it_named_and_an_untouched_one_passes(self):
        a = self.lane(1, "plugin/tests/test_mine.py", KNOWN_ENV.format("Mine") + "# the lane's own edit\n")
        b = self.lane(2, "b.txt")
        r = self.train(a, b, check=False, env=self.red)
        out = r.stdout + r.stderr
        held = self.fm("task", "show", a).stdout
        self.assertIn("left out of a merge train: its merge turns the gates red", held, out)
        self.assertIn("test_mine.Mine", held)
        self.assertIn("blamed: it changed plugin/tests/test_mine.py, the failing test", held)
        self.assertNotIn("put back", held, "its own edit of a red test is never put back as the base's")
        self.assertNotIn("left out of a merge train", self.fm("task", "show", b).stdout)
        self.assertNotIn("own edit", open(os.path.join(self.repo, "plugin/tests/test_mine.py")).read(), out)
        self.assertFalse(self.landed("b.txt") and self.landed("a.txt"), out)

    def test_two_lanes_each_green_alone_and_red_combined_are_held_by_the_new_test_of_one_of_them(self):
        """The 15:01 shape: lane a adds a.txt, lane b adds the test that is red once a.txt is in the tree. Neither is red
        alone; the combined tree's gate runs b's own new test (a changed test file is the lane's own, whatever links it)
        and the red holds the pair back: not both land."""
        both = ("# map-links: b.txt\nimport os, unittest\n\n\nclass Both(unittest.TestCase):\n    def test_it(self):\n"
                "        self.assertFalse(os.path.exists('a' + '.txt'), 'red with the other lane in')\n")
        a = self.lane(1, "a.txt")
        b = self.lane(2, "plugin/tests/test_both.py", both)
        r = self.train(a, b, check=False)
        out = r.stdout + r.stderr
        held = self.fm("task", "show", b).stdout
        self.assertIn("left out of a merge train", held, out)
        self.assertIn("test_both.Both", held + out)
        self.assertFalse(self.landed("a.txt") and os.path.exists(os.path.join(self.repo, "plugin/tests/test_both.py")), out)

    def test_the_known_red_test_no_lane_touched_is_the_bases_and_blames_no_lane(self):
        b = self.lane(1, "a.txt")
        r = self.train(b, check=False, env=self.red)
        out = r.stdout + r.stderr
        self.assertNotIn("its merge turns the gates red", out)
        self.assertNotIn("left out of a merge train", self.fm("task", "show", b).stdout)


class CloseKnownRed(ForemanTestCase):
    """T-5982: a close refused for linked tests that were already red in the last full run is excused as its baseline's,
    except a test the task itself changed: that red is its own."""
    SRC = "def add(a, b):\n    return a + b\n"
    TEST = ("import unittest\nimport sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\nimport calc\n\n\n"
            "class Calc(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n")

    def setUp(self):
        super().setUp()
        self.fm("init")
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py"), os.path.join(self.repo, "tests", "run.py"))
        for rel, text in (("calc.py", self.SRC), ("tests/test_calc.py", self.TEST), (".gitignore", "__pycache__/\n")):
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "suite"], check=True)
        self.fm("check", "add", "python3 tests/run.py")
        self.fm("check", "affected", "python3 tests/run.py {names}")
        with open(os.path.join(self.repo, "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a - b\n")  # red before the task, recorded by a full run
        self.fm("check", check=False, env={"FOREMAN_SENTINEL": "off"})

    def finish(self, rel, text):
        tid = self.fm_json("task", "new", "Change", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", rel)["id"]
        self.fm("focus", tid)
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)
        return self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False,
                       env={"FOREMAN_SENTINEL": "off"})

    def test_a_task_that_edits_the_red_test_itself_is_refused_not_excused(self):
        r = self.finish("tests/test_calc.py", self.TEST + "# the task's own edit\n")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertNotIn("its baseline task owns them", out)
        self.assertIn("test_calc", out)
        self.assertIn("never a baseline's", out)

    def test_a_task_that_leaves_the_red_test_alone_is_still_excused_as_the_baselines(self):
        r = self.finish("calc.py", "def add(a, b):\n    return a - b  # still\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already red", r.stdout + r.stderr)


class ExcuseSitesKnownRed(unittest.TestCase):
    """T-5982: every function of train.py, lanes.py and cli.py that excuses or forgives a red (inherited, base red,
    known red, baseline, forgiven) consults the lane's own tests, or is listed here with the reason it need not. A new
    excuse site fails this until it does one or the other, so a lane's own tests are never excused by a path nobody swept."""
    EXCUSE = re.compile(r"inherit|base_red|known_red|base_bad|base_green|base_shape|forgiv|excus|trunk_red|based\b|"
                        r"known_bad|baseline|known_flaky")
    OWN = re.compile(r"is_own_test|own_test_modules|_lane_own|owned_of|own_tests|_known_own|_pm_own_mods|_pm_own_files|"
                     r"^owns?$")
    SEEDS = {
        "train.py": ["base_split", "base_red", "_base_red_once", "_probe_once", "_fix_open", "_red_waiting", "_live_shape",
                     "_shape_cull"],
        "lanes.py": ["_pm_judge", "pm_verify"],
        "cli.py": ["_close_affected"],
    }
    ALLOW = {  # (file, function): why it needs no own set
        ("train.py", "_base_red"): "runs the base once and hands the lane's own set to _base_red_once, which decides",
        ("train.py", "_base_red_capture"): "captures a FIX for a red already decided inherited by base_split/_base_red_once",
        ("train.py", "_base_shape_cached"): "caches the base's own red list; the forgiveness is _live_shape/_shape_cull",
        ("train.py", "_base_shape_red"): "computes the base's red list only; _live_shape/_shape_cull decide who it forgives",
        ("train.py", "_base_shape_rerun"): "reruns the base's shape classes only; the forgiveness stays in _live_shape",
        ("train.py", "_trunk_red_capture"): "files a FIX for a red already decided trunk's; it excuses nothing",
        ("train.py", "_union_proof"): "'based' here is a git base, not an excuse",
        ("train.py", "base_check"): "delegates to base_split, which takes the lane's own set",
        ("train.py", "shape_split"): "calls _live_shape/_shape_cull with own=own_tests",
        ("train.py", "split"): "calls the shape split with own=own_tests",
        ("train.py", "_known_red"): "asks run.py which classes are red on trunk; _red_waiting removes the lane's own first",
        ("lanes.py", "merge"): "'based' here is a git base, not an excuse",
        ("lanes.py", "based"): "nested in merge: a git base, not an excuse",
        ("lanes.py", "_pm_trunk_red"): "files a FIX for the classes _pm_judge already decided inherited (own excluded)",
        ("cli.py", "_concern_links"): "inherit_concerns is a concern-link option, not a red excuse",
        ("cli.py", "_cmd_check"): "sampo check (the main thread's tree), not a lane gate; it only calls _baseline",
        ("cli.py", "_baseline"): "captures a baseline FIX from an unchanged tree sampo check ran; no lane's tests are judged",
        ("cli.py", "_known_flaky"): "reads the flaky list for _baseline's capture; no lane's tests are judged",
    }

    @classmethod
    def funcs(cls):
        import ast
        out = {}
        for f in cls.SEEDS.keys() | {"train.py", "lanes.py", "cli.py"}:
            with open(os.path.join(LIB, "sampo", f), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                ids = {fn.name}
                for n in ast.walk(fn):
                    if isinstance(n, ast.Name):
                        ids.add(n.id)
                    elif isinstance(n, ast.Attribute):
                        ids.add(n.attr)
                    elif isinstance(n, ast.arg):
                        ids.add(n.arg)
                    elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        ids.add(n.name)
                out.setdefault((f, fn.name), set()).update(ids)
        return out

    def test_every_function_that_excuses_a_red_consults_the_lanes_own_tests_or_says_why_not(self):
        funcs = self.funcs()
        bad = sorted(k for k, ids in funcs.items()
                     if any(self.EXCUSE.search(i) for i in ids) and not any(self.OWN.search(i) for i in ids)
                     and k not in self.ALLOW)
        self.assertEqual(bad, [], "an excuse site that never asks which tests the lane owns: take the lane's own set "
                                  "(c.is_own_test) or list it in ExcuseSitesKnownRed.ALLOW with the reason")

    def test_the_named_excuse_sites_each_consult_the_lanes_own_tests(self):
        funcs = self.funcs()
        for f, names in self.SEEDS.items():
            for n in names:
                self.assertIn((f, n), funcs, f"{f}:{n} is gone: update SEEDS")
                self.assertTrue(any(self.OWN.search(i) for i in funcs[(f, n)]), f"{f}:{n} no longer consults own tests")

    def test_the_allowlist_holds_no_stale_or_reasonless_entry(self):
        funcs = self.funcs()
        for k, why in self.ALLOW.items():
            self.assertIn(k, funcs, f"{k} is gone: drop it from ALLOW")
            self.assertTrue(why.strip(), f"{k} has no reason")
            self.assertTrue(any(self.EXCUSE.search(i) for i in funcs[k]),
                            f"{k} is no excuse site any more: drop it from ALLOW")

    def test_the_base_red_put_back_and_the_base_split_ask_the_lane_not_a_frozen_list(self):
        funcs = self.funcs()
        self.assertIn("owned_of", funcs[("train.py", "base_red")])
        self.assertIn("_known_own", funcs[("train.py", "base_split")])
        self.assertNotIn("own_tests", funcs[("train.py", "base_split")])  # the None-collapsing list is not what it reads

    def test_a_lane_whose_diff_is_unknown_is_unlisted_and_adds_no_module_to_the_others(self):
        owned = {"a": ["test_a"], "b": None, "c": ["test_c", "test_a"]}.get
        self.assertEqual(train._unlisted(["a", "b", "c"], owned), ["b"])
        self.assertEqual(train._known_own(["a", "b", "c"], owned), ["test_a", "test_c"])
        self.assertEqual(train._known_own(["b"], owned), [])
        self.assertEqual(train._unlisted(["a", "c"], owned), [])


class PmKnownRed(ForemanTestCase):
    """T-5982: the behind-verify excuses a class red on the merge's first parent too (inherited, no revert), except a test
    file the merge itself changed: that class stays the merge's and the merge is reverted."""
    SUITE = "[ -f base_red.txt ] && echo 'FAIL: t (test_m.A.t)' && exit 1; exit 0"  # test_m.A is red while base_red.txt exists

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        for k in ("FM_POSTMERGE_CMD", "FM_POSTMERGE_RERUN"):
            os.environ[k] = self.SUITE
            self.addCleanup(os.environ.pop, k, None)
        self.tid = self.fm_json("task", "new", "Landed", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                "--step", "s")["id"]

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True).stdout.strip()

    def land(self, name):
        self.git("checkout", "-qb", f"b-{name}")
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(name)
        self.git("add", "-A")
        self.git("commit", "-qm", name)
        self.git("checkout", "-q", "-")
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {name}", f"b-{name}")
        sha = self.git("rev-parse", "HEAD")
        with mock.patch.object(slanes, "pm_spawn"):
            slanes.pm_record(self.p, sha, self.tid, self.repo)
        return sha

    def test_a_merge_that_changed_the_red_test_file_is_reverted_not_inherited(self):
        slanes.pm_set(self.p, self.land("base_red.txt"), state="green")  # test_m.A red on trunk before the merge
        sha = self.land("test_m.py")  # the merge's own test file: its class is the merge's
        slanes.pm_run(self.p, self.repo)
        r = slanes.pm_records(self.p)[sha]
        self.assertEqual((r["state"], r["classes"]), ("reverted", ["test_m.A"]))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "test_m.py")), "reverted")

    def test_a_merge_whose_task_criterion_names_the_red_module_is_reverted_not_inherited(self):
        """A merge that changed no test file, its task's own criterion runs the module: red there is the merge's."""
        self.tid = self.fm_json("task", "new", "Names it", "--type", "FEATURE", "--tier", "S", "--ac",
                                "ok :: python3 plugin/tests/run.py test_m -k stale", "--step", "s")["id"]
        slanes.pm_set(self.p, self.land("base_red.txt"), state="green")
        sha = self.land("other.txt")
        slanes.pm_run(self.p, self.repo)
        r = slanes.pm_records(self.p)[sha]
        self.assertEqual((r["state"], r["classes"]), ("reverted", ["test_m.A"]))
        self.assertEqual(slanes._pm_own_mods(self.p, [self.tid]), ["test_m"])

    def test_a_merge_that_changed_no_test_file_inherits_the_trunks_red(self):
        slanes.pm_set(self.p, self.land("base_red.txt"), state="green")
        sha = self.land("other.txt")
        slanes.pm_run(self.p, self.repo)
        r = slanes.pm_records(self.p)[sha]
        self.assertEqual((r["state"], r["classes"]), ("inherited", ["test_m.A"]))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "other.txt")), "not reverted")


class ReviewBeforeTrain(LeftoverBase):
    """T-5968: the 15:01 live land trained lanes whose builder had only returned, unreviewed (T-5706's round 6 and T-5884
    reached trunk that way and turned tests red). A lane trains only once its review is recorded at its tip, in both modes."""
    HELD = "held: not reviewed at this tip"
    merge_reviewed = False  # the fixture never stamps a merge's lane: that is the hold under test

    def unreviewed(self, n=1, **kw):
        tid = self.lane(n, f"rev{n}.txt", when=test_lane_land.OLD, reviewed=False, **kw)
        self.returned(tid, f"Lane {n}")
        return tid

    def hold(self, tid):
        b = self.brief(tid)
        with self.in_project():
            return train._lane_hold(self.p, b, train._lane_tip(self.repo, b), self.repo)

    def test_a_live_lane_with_no_review_is_held_and_asked_of_the_pilot(self):
        self.fm("lane", "mode", "live")
        tid = self.unreviewed()
        self.assertTrue((self.hold(tid) or "").startswith(self.HELD), self.hold(tid))
        asked = []
        env = {k: v for k, v in os.environ.items() if k != "FOREMAN_NO_BACKGROUND"}  # the ask is made only where a land may background
        with mock.patch.object(train, "_ask_pilot", lambda p, t, again=False: asked.append(t)), mock.patch.dict(os.environ, env, clear=True):
            got, err, events = self.run_land()
        self.assertEqual(asked, [tid], "the land asks the lane pilot once")
        self.assertNotIn(("train", [tid]), events)
        self.assertNotIn("rev1.txt", self.git("ls-files"), "nothing reached trunk")

    def test_the_gated_train_holds_it_too(self):
        tid = self.unreviewed()
        self.assertTrue((self.hold(tid) or "").startswith(self.HELD))
        r = self.train(tid, check=False)
        self.assertNotIn("rev1.txt", self.git("ls-files"), r.stdout + r.stderr)
        self.assertIn(self.HELD, r.stdout + r.stderr)

    def test_lane_train_and_first_and_merge_hold_it_in_live_mode(self):
        self.fm("lane", "mode", "live")
        tid = self.unreviewed()
        for argv in (("lane", "train", tid), ("lane", "train", tid, "--first"), ("lane", "merge", tid),
                     ("lane", "merge", tid, "--unverified")):
            r = self.fm(*argv, check=False)
            self.assertNotIn("rev1.txt", self.git("ls-files"), f"{argv}: {r.stdout + r.stderr}")
        self.assertIn("not reviewed at this tip", self.fm("lane", "merge", tid, check=False).stderr)

    def test_audits_on_the_lane_tree_or_an_accept_release_it(self):
        self.fm("lane", "mode", "live")
        tid = self.unreviewed()
        path = self.brief(tid).meta["lane"]
        self.assertTrue(self.hold(tid))
        self.fm("task", "audit", tid, "self", "one fm-reviewer pass on the branch", "no findings", cwd=path)
        self.assertFalse(self.hold(tid), self.hold(tid))
        accepted = self.unreviewed(2)
        self.stamp_accepted(accepted)
        self.assertFalse(self.hold(accepted))

    def test_audits_for_a_later_tree_never_release_an_earlier_tip(self):  # T-5968 review 1: the tip argument is read
        tid = self.unreviewed()
        path = self.brief(tid).meta["lane"]
        old = self.git("rev-parse", "HEAD", cwd=path)
        with open(os.path.join(path, "later.txt"), "w") as f:
            f.write("later\n")
        self.git("add", "later.txt", cwd=path)
        self.git("commit", "-qm", "later", cwd=path)
        self.fm("task", "audit", tid, "self", "one fm-reviewer pass on the branch", "no findings", cwd=path)
        new = self.git("rev-parse", "HEAD", cwd=path)
        b = self.brief(tid)
        self.assertEqual(slanes.review_hold(b, new), "", "the audits cover the tip they were recorded at")
        self.assertTrue(slanes.review_hold(b, old).startswith(self.HELD), "but never the tip before it")

    def test_an_accepted_lane_trains_as_before(self):
        self.fm("lane", "mode", "live")
        tid = self.lane(1, "ok.txt", when=test_lane_land.OLD)
        self.returned(tid)
        self.assertFalse(self.hold(tid))
        self.train(tid)
        self.assertIn("ok.txt", self.git("ls-files"))

    def test_a_tip_that_moved_after_the_accept_is_held_until_reviewed_again(self):
        self.fm("lane", "mode", "live")
        tid = self.lane(1, "moved.txt", when=test_lane_land.OLD)
        path = self.brief(tid).meta["lane"]
        self.assertFalse(self.hold(tid))
        with open(os.path.join(path, "more.txt"), "w") as f:
            f.write("later\n")
        self.git("add", "more.txt", cwd=path)
        self.git("commit", "-qm", "after the accept", cwd=path)
        self.assertTrue((self.hold(tid) or "").startswith(self.HELD))
        self.fm("lane", "train", tid, check=False)
        self.assertNotIn("moved.txt", self.git("ls-files"))
        self.stamp_accepted(tid)
        self.assertFalse(self.hold(tid))

    def test_the_ask_goes_to_pilotsig_once_and_never_raises(self):
        from sampo import pilotsig
        with mock.patch.object(pilotsig, "spawn", return_value=None) as sp:
            train._ask_pilot(self.p, "T-1")
        sp.assert_called_once_with(self.p, "T-1", again=False)
        err = io.StringIO()
        with mock.patch.object(pilotsig, "spawn", side_effect=OSError("no fork")), contextlib.redirect_stderr(err):
            train._ask_pilot(self.p, "T-1")
        self.assertIn("no review started", err.getvalue())

    def test_a_returned_but_unaccepted_lane_is_never_trained_by_the_live_land(self):
        # the 16:44 land trained T-5968's own first commit this way: the builder had returned, nothing had accepted it
        self.fm("lane", "mode", "live")
        tid = self.unreviewed()
        self.assertFalse(self.brief(tid).meta.get("accepted_tip"))
        got, err, events = self.run_land()
        self.assertEqual([e for e in events if e[0] in ("train", "trained")], [], err)
        self.assertNotIn("rev1.txt", self.git("ls-files"), "nothing reached trunk")
        self.assertIn("not reviewed at this tip", err + str(got))

    def ask_state(self, tid, **meta):
        b = self.brief(tid)
        b.meta.update(meta)
        return b

    def test_the_hold_line_says_what_became_of_the_pilot(self):  # T-5968 review 3
        from sampo import pilotsig
        self.fm("lane", "mode", "live")
        tid = self.unreviewed()
        tip = train._lane_tip(self.repo, self.brief(tid))
        env = {k: v for k, v in os.environ.items() if k != "FOREMAN_NO_BACKGROUND"}
        with self.in_project(), mock.patch.dict(os.environ, env, clear=True):
            b = self.brief(tid)
            self.assertEqual(train._review_ask(self.p, b, tip, False, set())[0], " [lane pilot: off; review it by hand]")
            with mock.patch.object(pilotsig, "held", return_value=True):
                note, reviewing = train._review_ask(self.p, b, tip, True, set())
            self.assertEqual((note, reviewing), (" [lane pilot: reviewing it now]", True))
            asked = set()
            with mock.patch.object(pilotsig, "held", return_value=False), \
                    mock.patch.object(pilotsig, "spawn", return_value=4242) as sp:
                self.assertEqual(train._review_ask(self.p, b, tip, True, asked), (" [lane pilot: asked]", True))
                sp.assert_called_once_with(self.p, tid, again=False)
                note, reviewing = train._review_ask(self.p, b, tip, True, asked)
                self.assertEqual((note, reviewing), (" [lane pilot: asked, none running now]", False))
                self.assertEqual(sp.call_count, 1, "once per land")
            with mock.patch.object(pilotsig, "held", return_value=False), mock.patch.object(pilotsig, "spawn", return_value=None):
                self.assertEqual(train._review_ask(self.p, b, tip, True, set()), (" [lane pilot: not started]", False))
            ret = "2026-11-22T16:00:00"
            reported = self.ask_state(tid, builder_returned=ret, pilot_done_for=ret, pilot_done_tip=tip, pilot_outcome="changes")
            with mock.patch.object(pilotsig, "held", return_value=False), mock.patch.object(pilotsig, "spawn") as sp:
                note, reviewing = train._review_ask(self.p, reported, tip, True, set())
            self.assertEqual((note, reviewing), (" [lane pilot: reported changes at this tip and recorded no review]", False))
            sp.assert_not_called()
            moved = self.ask_state(tid, builder_returned=ret, pilot_done_for=ret, pilot_done_tip="0" * 40, pilot_outcome="changes")
            with mock.patch.object(pilotsig, "held", return_value=False), \
                    mock.patch.object(pilotsig, "spawn", return_value=4243) as sp:
                note, reviewing = train._review_ask(self.p, moved, tip, True, set())
            self.assertEqual((note, reviewing), (" [lane pilot: asked again, the tip moved since it reported]", True))
            sp.assert_called_once_with(self.p, tid, again=True)

    def test_a_land_of_only_review_held_lanes_waits_for_the_pilot_and_trains_the_lane_it_accepted(self):  # T-5968 review 3
        self.fm("lane", "mode", "live")
        tid = self.unreviewed()
        env = {k: v for k, v in os.environ.items() if k != "FOREMAN_NO_BACKGROUND"}
        looks = []

        def ask(p, b, sha, background, asked):
            looks.append(len(looks))
            if len(looks) == 3:
                self.stamp_accepted(tid)  # the pilot's accept lands while the land waits
            return " [lane pilot: reviewing it now]", True
        with mock.patch.object(train, "_review_ask", ask), mock.patch.object(train, "STALE_POLL_S", 0.0), \
                mock.patch.object(train, "CLOSE_POLL_S", 0.01), mock.patch.dict(os.environ, env, clear=True):
            got, err, events = self.run_land()
        self.assertGreaterEqual(len(looks), 3, "the land kept looking while the pilot was at work")
        self.assertIn(("train", [tid]), events, err)
        self.assertIn("rev1.txt", self.git("ls-files"))

    def test_the_review_look_is_made_once_per_lane_and_tip_in_a_land(self):  # T-5968 review 4
        tid = self.unreviewed()
        b = self.brief(tid)
        tip = train._lane_tip(self.repo, b)
        memo, calls = {}, []
        real = slanes._review_hold
        with self.in_project(), mock.patch.object(slanes, "_review_hold", lambda *a, **k: calls.append(1) or real(*a, **k)):
            first = train._lane_hold(self.p, b, tip, self.repo, memo)
            for _ in range(5):
                train._lane_hold(self.p, b, tip, self.repo, memo)
        self.assertTrue(first.startswith(self.HELD))
        self.assertEqual(len(calls), 1, "five more polls read the memo")
        self.stamp_accepted(tid)
        with self.in_project():
            self.assertFalse(train._lane_hold(self.p, self.brief(tid), tip, self.repo, memo), "an accept is seen at once")


class EveryPathToTrunkIsReviewed(unittest.TestCase):
    """T-5968 class sweep: every call site that moves lane commits onto a trunk (a git merge, a fast-forward, the train's
    merge, lanes.merge, a land_holds call) is in one of two tables, so a new path fails here until it is placed: GATED
    (the function reaches lanes.review_hold, directly or through land_holds without review=False) or ALLOWED with the
    reason it needs no lane review."""
    GATED = {
        ("lanes.py", "merge"): "land_holds, the shared hold, before its git merge (lane merge, race pick, hand close)",
        ("lanes.py", "pick"): "lanes.merge",
        ("lanes.py", "cmd_lane"): "lanes.merge",
        ("lanes.py", "land_holds"): "calls review_hold first",
        ("train.py", "_held"): "land_holds: every train hold (--first, lane train, the land) runs it",
        ("train.py", "_lane_hold"): "review_hold: the land's lane selection, so an unreviewed lane is never batched",
        ("train.py", "_run"): "trains only lanes _holds (_held) let through",
        ("train.py", "_run.merge1"): "trains only lanes _holds (_held) let through",
        ("train.py", "_batch_once"): "trains only lanes _holds (_held) let through",
        ("train.py", "_shape_batch_once"): "trains only lanes _holds (_held) let through",
        ("train.py", "_merge_in"): "trains only lanes _holds (_held) let through",
        ("train.py", "_fast_forward"): "fast-forwards only the batch _run built from held-checked lanes",
        ("serve.py", "_integrate_lane"): "review_hold on the tip the builder left, before its rebase",
    }
    ALLOWED = {
        ("lanepilot.py", "gate"): "the pilot's own gate runs before the review it records (review=False)",
        ("big.py", "task_close"): "big/ID has no lane tip: it integrates children each reviewed when they merged in",
        ("big.py", "_merge"): "task_close's merge of big/ID",
        ("pr.py", "train"): "pull requests are reviewed on GitHub, not as lanes (review=False)",
        ("pr.py", "_retest"): "a scratch worktree testing a PR head, nothing reaches a trunk",
        ("lanes.py", "start"): "fast-forwards the lane's own new branch to its base",
        ("louhi.py", "_lanes_drill.do"): "a drill's scratch repo",
        ("mapmerge.py", "main"): "merges map files, not lane commits",
        ("release.py", "_push"): "fast-forwards a release branch from trunk",
        ("train.py", "_probe_once"): "a scratch probe, nothing reaches a trunk",
        ("update.py", "_apply"): "updates the installed plugin, not a lane",
    }
    GITMERGE = {"--ff-only", "--no-ff", "--no-commit", "--no-edit", "--squash"}
    NAMES = {"_fast_forward", "_merge_in", "land_holds", "review_hold"}
    FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
    SRC_OF = {"lanes.py": {"land_holds", "merge"}, "train.py": {"_lane_hold", "_held"}, "serve.py": {"_integrate_lane"}}

    @classmethod
    def _qual(cls, fn, parents):
        names = [fn.name]
        while fn in parents:
            fn = parents[fn]
            if isinstance(fn, cls.FUNCS):
                names.append(fn.name)
        return ".".join(reversed(names))

    _memo = []

    @classmethod
    def _sites(cls):
        """({(file, function): [lines]} for each call site, {(file, function): its source}); scanned once per run."""
        if not cls._memo:
            cls._memo.append(cls._scan())
        return cls._memo[0]

    @classmethod
    def _scan(cls):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "sampo")
        out, src = {}, {}
        for f in sorted(glob.glob(os.path.join(root, "*.py"))):
            with open(f, encoding="utf-8") as fh:
                text = fh.read()
            tree = ast.parse(text)
            parents = {ch: n for n in ast.walk(tree) for ch in ast.iter_child_nodes(n)}
            base = os.path.basename(f)
            for n in ast.walk(tree):
                if isinstance(n, cls.FUNCS) and n.name in cls.SRC_OF.get(base, ()):  # (the source of every function is slow)
                    src[(base, cls._qual(n, parents))] = ast.get_source_segment(text, n) or ""
                if not isinstance(n, ast.Call):
                    continue
                fn = n.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                cs = [a.value for a in ast.walk(n) if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                lane_merge = name == "merge" and (isinstance(fn, ast.Name) or (
                    isinstance(fn.value, ast.Name) and fn.value.id == "lanes"))
                if name in cls.NAMES or lane_merge or ("merge" in cs and set(cs) & cls.GITMERGE):
                    up = n
                    while up in parents and not isinstance(up, cls.FUNCS):
                        up = parents[up]
                    out.setdefault((base, cls._qual(up, parents) if isinstance(up, cls.FUNCS) else "<module>"),
                                   []).append(n.lineno)
        return out, src

    def test_every_call_site_is_placed(self):
        sites, _ = self._sites()
        known = set(self.GATED) | set(self.ALLOWED)
        self.assertFalse(set(self.GATED) & set(self.ALLOWED))
        self.assertEqual(sorted(set(sites) - known), [], "a new path to a trunk: place it in GATED or ALLOWED (T-5968)")
        self.assertEqual(sorted(known - set(sites)), [], "a placed site that is gone: drop it from the table")

    def test_the_gated_roots_really_reach_the_review_hold(self):
        _, src = self._sites()
        lanes_src = src[("lanes.py", "land_holds")]
        self.assertIn("review_hold(b, tip)", lanes_src)
        self.assertIn("land_holds(p, b, main, tip)", src[("lanes.py", "merge")])
        self.assertIn("lanes.review_hold(b, sha", src[("train.py", "_lane_hold")])
        self.assertIn("land_holds(", src[("train.py", "_held")])
        self.assertNotIn("review=False", src[("train.py", "_held")])
        self.assertIn("review_hold(", src[("serve.py", "_integrate_lane")])

    # T-5968 review 8: the paths a reader asks about that never put a lane's commits on a trunk, each with its reason; the
    # test below keeps the reason true (no call site in the file, no review stamp written by it)
    NOT_PATHS = {
        "smith.py": "smith undo puts a worker's own edits and commits back in its scratch tree; it merges nothing to a trunk",
        "recover.py": "recover and its holds (release, refuse_held) relaunch a builder in the lane, whose work is reviewed again "
                      "(the reset pops accepted_tip and reviewed_tip) before any path above lets it through",
        "pilotsig.py": "the resolver and the stopped-lane builder commit in the lane's worktree: the lane tip moves, so the "
                       "review hold holds it until the pilot or a lead accepts that tip; it stamps no review itself",
    }

    def test_the_paths_that_reach_no_trunk_stay_that_way(self):
        sites, _ = self._sites()
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "sampo")
        for f, why in self.NOT_PATHS.items():
            self.assertEqual([k for k in sites if k[0] == f], [], f"{f}: {why}")
        with open(os.path.join(root, "pilotsig.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        stamps = {n.slice.value for n in ast.walk(tree) if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                  and n.slice.value in ("accepted_tip", "reviewed_tip")
                  and isinstance(n.ctx, ast.Store)}
        self.assertEqual(stamps, set(), "a pilot's resolver or stopped-lane run never stamps a review: only an accept does")
        with open(os.path.join(root, "recover.py"), encoding="utf-8") as fh:
            self.assertNotIn("accepted_tip", fh.read(), "recover never stamps one")

    def test_the_bypasses_are_only_the_allowed_ones(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "sampo")
        off = set()
        for f in glob.glob(os.path.join(root, "*.py")):
            with open(f, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for n in ast.walk(tree):
                if (isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) == "land_holds"
                        and any(k.arg == "review" and isinstance(k.value, ast.Constant) and k.value.value is False
                                for k in n.keywords)):
                    off.add(os.path.basename(f))
        self.assertEqual(off, {"lanepilot.py", "big.py", "pr.py", "serve.py"}, off)
