"""T-4851: the lane check's log. `check started ... log check/ID.log` named a file the check process's own stdout was
(sampo lane accept starts it so), but _run_check held the check's output in memory and printed none of it: every such log
was 0 bytes (T-4147, T-4139, T-4189), and a check cut off at its deadline (60 of 1546 classes in 1200 s) left nothing to
read. Now the check's lines are written to that stdout as they arrive, the result is still read from the same text, and
a hold for a cut-off check names the log."""
# map-links: plugin/lib/sampo/train.py
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_accept_check import AcceptCase, stub
from test_express_lane import ExpressBase

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
        self.assertEqual(self.trains(r)[0], pid, r.stderr)
        self.assertEqual([self.brief(t).status for t in (*ids, pid)], ["done"] * 21, r.stdout + r.stderr)
        self.assertIn(f"{pid} touches the pipeline", r.stderr)

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

    def test_pipeline_lanes_live_train_together_capped_at_the_batch(self):
        ids, pid = self.made_with_pipeline(3)
        second = self.lane(9, "plugin/lib/sampo/lanes.py", text="y = 2\n", when="2026-01-01T00:00:00")
        self.audited(second)
        self.returned(second, "Lane lanes")
        self.fm("lane", "mode", "live")
        r = self.land("--batch", "5")
        self.assertEqual(self.trains(r), [f"{pid}, {second}", ", ".join(ids)], r.stderr)

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
    at the batch size, ahead of the ordinary lanes; gated, each still trains alone (T-5023)."""

    def test_express_live_lanes_train_together_ahead_of_the_ordinary_ones(self):
        ids = self.made(5)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[1], "type=PERFORMANCE")
        self.fm("task", "set", ids[3], "type=SECURITY")
        self.fm("task", "set", ids[4], "priority=urgent")
        self.queue(ids[0], ids[1], ids[3], ids[4])
        r = self.land("--batch", "5")
        self.assertEqual(self.trains(r), [f"{ids[1]}, {ids[3]}, {ids[4]}", f"{ids[0]}, {ids[2]}"], r.stderr)
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 5, r.stdout + r.stderr)

    def test_express_live_train_is_capped_at_the_batch_size(self):
        ids = self.made(4)
        self.fm("lane", "mode", "live")
        for t in ids[:3]:
            self.fm("task", "set", t, "type=PERFORMANCE")
        self.queue(*ids[:3])
        r = self.land("--batch", "2")
        self.assertEqual(self.trains(r), [f"{ids[0]}, {ids[1]}", ids[2], ids[3]], r.stderr)

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
        self.assertIn(f"{ids[1]} is express (performance, security or urgent): it trains next, with the other express lanes",
                      r.stderr)
