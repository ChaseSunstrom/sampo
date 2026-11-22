"""T-4851: the lane check's log. `check started ... log check/ID.log` named a file the check process's own stdout was
(sampo lane accept starts it so), but _run_check held the check's output in memory and printed none of it: every such log
was 0 bytes (T-4147, T-4139, T-4189), and a check cut off at its deadline (60 of 1546 classes in 1200 s) left nothing to
read. Now the check's lines are written to that stdout as they arrive, the result is still read from the same text, and
a hold for a cut-off check names the log."""
# map-links: plugin/lib/sampo/train.py
import contextlib
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
from test_accept_check import AcceptCase, stub
from test_express_lane import ExpressBase

from sampo import core as c
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
