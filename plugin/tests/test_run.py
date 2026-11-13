"""T-0808: run.py takes test module names positionally, so fm check affected can hand it {names}."""
import atexit
import contextlib
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import run  # noqa: E402


_CACHE = tempfile.mkdtemp(prefix="run-test-cache-")  # T-4268: a run's record never lands in the real run-cache folder
atexit.register(shutil.rmtree, _CACHE, True)


def in_process(**env):
    """T-3572: env for run.main() called in this process: no fork server of its own (it would outlive the call until
    the class exits) and no FOREMAN_FORK_SERVER left behind for the class's later tests. T-4268: its own cache folder
    (run.main writes the run's record there)."""
    return mock.patch.dict(os.environ, {"FOREMAN_FORK_CLI": "0", "XDG_CACHE_HOME": _CACHE, **env})


def nested(args, seed=None, env=None):
    """T-3166: run.py as a child with its own times file (never the real one) and no test cache (T-3502: its runs
    differ by environment, not code); returns (result, the file's times after)."""
    import json
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.json")
        if seed is not None:
            with open(p, "w") as f:
                json.dump(seed, f)
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "run.py"), *args],
                           capture_output=True, text=True, timeout=300,
                           env=dict(os.environ, FM_TEST_TIMES=p, FOREMAN_NO_TEST_CACHE="1", XDG_CACHE_HOME=d,  # T-4268: its record too
                                    **(env or {})))
        return r, (json.load(open(p)) if os.path.exists(p) else None)


class NoCore(unittest.TestCase):
    def test_run_py_drops_core_dumps_for_itself_and_its_workers(self):  # T-3503
        import subprocess
        code = ("import sys, resource, run\nsys.argv = ['run.py', '-h']\n"
                "try:\n    run.main()\nexcept SystemExit:\n    pass\nprint(resource.getrlimit(resource.RLIMIT_CORE))")
        r = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(__file__), capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=os.path.dirname(__file__)))
        self.assertEqual(r.stdout.strip().splitlines()[-1], "(0, 0)", r.stderr)


class Args(unittest.TestCase):
    def test_names_are_k_patterns(self):
        a = run.parse(["test_lanes", "-k", "Stop", "test_cli"])
        self.assertEqual((sorted(a.names), a.k), (["test_cli", "test_lanes"], ["Stop"]))
        self.assertEqual(run.parse([]).k, [])

    def test_paths_into_the_tests_dir_name_their_module(self):  # T-3428
        import io
        rel = os.path.relpath(run.HERE)
        for p in (f"{rel}/test_genesis.py", f"./{rel}/test_genesis.py", "test_genesis.py",
                  os.path.join(run.HERE, "test_genesis.py"), "test_genesis"):
            self.assertEqual(run.parse([p, "-k", "Memo"]).names, ["test_genesis"], p)
        for bad in ("/etc/passwd.py", "plugin/test_x.py", "../x/test_y.py", f"{rel}/other.py", f"{rel}/"):
            with self.assertRaises(SystemExit, msg=bad), contextlib.redirect_stderr(io.StringIO()) as e:
                run.parse([bad])
            self.assertIn("not a test name", e.getvalue())

    def test_a_name_and_a_k_select_only_the_tests_matching_both(self):
        # T-1994: "run.py test_run -k shards" ran every test_run test (names and -k were unioned)
        hit = set()
        self.assertEqual(run.classes(["test_run.py"], ["shards"], hit, names=["test_run"]), ["test_run.Args"])
        self.assertEqual(hit, {"shards", "test_run"})
        self.assertEqual(run.classes(["test_run.py"], ["no_such_test"], set(), names=["test_run"]), [])
        self.assertGreater(len(run.classes(["test_run.py"], [], set(), names=["test_run"])), 1)  # a name alone: all

    def test_shards_split_the_classes_disjointly_and_cover_them_all(self):  # T-1842: one CI job ran out of time
        import contextlib
        import io
        from unittest import mock
        self.assertEqual(run.parse(["--shard", "2/3"]).shard, (2, 3))
        self.assertIsNone(run.parse([]).shard)
        for bad in ("0/2", "3/2", "2", "a/b", "1/0"):
            with self.assertRaises(SystemExit, msg=bad), contextlib.redirect_stderr(io.StringIO()):
                run.parse(["--shard", bad])
        ids = [f"test_m.C{i}" for i in range(7)]
        got = []
        for k in (1, 2):
            ran = []
            with mock.patch.object(run, "classes", return_value=list(ids)), \
                    mock.patch.object(run, "run_one", lambda c, *a: ran.append(c) or (c, 0, "", 1, 0.0)), \
                    mock.patch.object(sys, "argv", ["run.py", "--shard", f"{k}/2"]), in_process(), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run.main(), 0)
            got.append(ran)
        self.assertEqual(sorted(got[0] + got[1]), sorted(ids))
        self.assertFalse(set(got[0]) & set(got[1]))
        self.assertLessEqual(abs(len(got[0]) - len(got[1])), 1)


class Load(unittest.TestCase):
    """T-1730: --load N runs the tests beside N busy processes, the way a load flake reproduces on demand."""

    def test_load_parses_and_defaults_to_zero(self):
        self.assertEqual(run.parse(["--load", "3", "test_cli"]).load, 3)
        self.assertEqual(run.parse([]).load, 0)

    def test_burn_keeps_n_busy_children_and_kills_them_after(self):
        import time
        with run.burn(2) as procs:
            self.assertEqual(len(procs), 2)
            self.assertTrue(all(p.poll() is None for p in procs))
            if os.path.isdir("/proc"):  # T-1828: macOS has no /proc; the children running is checked above
                from helpers import read_text
                # T-3629: wait for each child's first CPU tick (up to 60 s) instead of a fixed 0.3 s window a loaded host starves
                deadline = time.monotonic() + 60
                while True:
                    cpu = [float(read_text(f"/proc/{p.pid}/stat").rsplit(")", 1)[1].split()[11]) for p in procs]
                    if all(t > 0 for t in cpu) or time.monotonic() > deadline:
                        break
                    time.sleep(0.1)
                self.assertTrue(all(t > 0 for t in cpu), "each child spins")
            self.assertTrue(all(p.poll() is None for p in procs))
        self.assertTrue(all(p.poll() is not None for p in procs), "none outlives the run")

    def test_hang_guards_stretch_with_the_load_and_stay_bounded(self):
        # T-1719: a hook call's 10 s guard tripped beside 24 busy processes; it scales with runnable tasks per core
        from unittest import mock
        import helpers
        from sampo import core
        cores = 16  # pinned: usable_cpus reads cgroup files, and mock_open would hand them this test's text (T-4531)
        cases = [("0.50 0.4 0.3 1/900 1\n", 1.0), (f"0.1 0.1 0.1 {3 * cores}/900 1\n", 3.0), ("garbage\n", None)]
        for text, want in cases:
            with mock.patch("builtins.open", mock.mock_open(read_data=text)), \
                    mock.patch.object(core, "usable_cpus", lambda: cores), \
                    mock.patch.object(core.os, "cpu_count", lambda: cores), \
                    mock.patch.dict(os.environ, {"FOREMAN_HOST": helpers.CALM_HOST}), \
                    mock.patch("os.getloadavg", return_value=(0.5, 0, 0)):
                got = helpers.busy_factor()
            self.assertGreaterEqual(got, 1.0, text)
            self.assertLessEqual(got, 8.0, text)
            if want is not None:
                self.assertAlmostEqual(got, want, msg=text)
        case = helpers.ForemanTestCase()
        case.home, case.repo = "/nonexistent-home", "/"
        import forkcli  # T-3297: fm() runs through the fork server; T-3572: hooks too
        with mock.patch.object(helpers, "busy_factor", return_value=3.0), \
                mock.patch.object(helpers.subprocess, "run") as run_, mock.patch.object(forkcli, "run") as fork_:
            case.hook("Stop", {})
            case.fm("status", check=False)
        self.assertEqual([c.kwargs["timeout"] for c in run_.call_args_list + fork_.call_args_list], [30.0, 90.0])

    def test_frames_are_judged_by_animation_time_and_rest_not_by_wall_windows(self):
        # T-1728: a loaded machine's slow frames (a 2 s gap) count as one capped step, as the pages count them
        import helpers
        self.assertAlmostEqual(helpers.anim_time([0, 16, 32, 2032, 2048]), 0.016 + 0.016 + 0.1 + 0.016)
        self.assertAlmostEqual(helpers.anim_time([0, 100, 200, 300], since=200), 0.2)
        self.assertEqual(helpers.anim_time([]), 0)
        rest = {"raf": 5, "waiting": 0}
        self.assertTrue(helpers.at_rest([None, rest, rest, rest]))
        self.assertFalse(helpers.at_rest([rest, rest]), "too few polls")
        self.assertFalse(helpers.at_rest([None, rest, rest]), "a failed poll is no rest")
        self.assertFalse(helpers.at_rest([rest, dict(rest, waiting=1), rest]), "a frame was asked for")
        self.assertFalse(helpers.at_rest([rest, rest, dict(rest, raf=6)]), "a frame was drawn")
        self.assertFalse(helpers.at_rest([dict(rest, raf=0)] * 3), "no frames yet is not rest")

    def test_a_killed_run_leaves_no_spinner(self):
        import signal
        import subprocess
        import time
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import run\n"
                "with run.burn(1) as p:\n    print(p[0].pid, flush=True); time.sleep(60)")
        parent = subprocess.Popen([sys.executable, "-c", code, os.path.dirname(os.path.abspath(__file__))],
                                  stdout=subprocess.PIPE, text=True)
        child = int(parent.stdout.readline())
        parent.send_signal(signal.SIGKILL)
        parent.wait()
        parent.stdout.close()
        def alive():  # a zombie waiting for its new parent to reap it spins no more
            try:
                with open(f"/proc/{child}/stat") as f:
                    return f.read().rsplit(")", 1)[1].split()[0] != "Z"
            except OSError:
                return False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and alive():
            time.sleep(0.05)
        left = alive()
        if left:
            os.kill(child, signal.SIGKILL)
        self.assertFalse(left, "the spinner outlived its run")

    def test_burn_zero_starts_nothing(self):
        with run.burn(0) as procs:
            self.assertEqual(procs, [])

    def test_load_tops_the_host_up_to_twice_its_cpus_counting_running_spinners(self):
        # T-3441: three --load 32 runs on 16 CPUs held 96 busy loops (load 100) for 12 min and starved every merge
        self.assertEqual(run.load_room(32, cpus=16, load=1.0, spinners=0), 31)
        self.assertEqual(run.load_room(32, cpus=16, load=5.0, spinners=31), 1, "the 1-minute average lags its spinners")
        self.assertEqual(run.load_room(32, cpus=16, load=90.0, spinners=0), 0)
        self.assertEqual(run.load_room(4, cpus=16, load=1.0, spinners=0), 4)
        self.assertGreaterEqual(run.load_room(0), 0)


class UnknownNames(unittest.TestCase):
    """T-1287: a module name that matches no test was dropped silently while another name matched, so a criterion
    naming a test file that doesn't exist passed."""

    def run_py(self, *names):
        return nested(names)[0]

    def test_a_name_matching_no_test_fails_the_run_and_is_named(self):
        r = self.run_py("test_run.Args", "test_nosuch_module")  # not test_run itself: that would run this test
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("test_nosuch_module", r.stderr)

    def test_a_name_whose_tests_a_k_filters_out_names_the_k(self):
        # T-1999: "no test matches test_run.Args" read as if the module were missing
        r = self.run_py("test_run.Args", "-k", "no_such_test")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no test matches test_run.Args with -k no_such_test", r.stderr)
        r = self.run_py("test_run.Args", "-k", "names_are_K_PATTERNS")  # T-2003: case and underscores aside, it exists
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("did you mean -k test_names_are_k_patterns?", r.stderr)
        self.assertNotIn("did you mean", self.run_py("test_run.Args", "-k", "no_such_test").stderr)

    def test_names_that_all_match_still_pass(self):
        self.assertEqual(self.run_py("test_run.Args").returncode, 0)

    def test_a_red_runs_last_line_names_the_failing_class(self):
        # T-2319: 'FAILED (1 classes)' alone sent a builder into a full re-run to learn which class failed
        r = nested(["test_run.AskedToFail"], env={"FM_RUN_ASKED_TO_FAIL": "1"})[0]
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stderr.strip().splitlines()[-1], "FAILED (1 classes: test_run.AskedToFail)")


class AskedToFail(unittest.TestCase):
    """Fails only when test_a_red_runs_last_line_names_the_failing_class asks it to, through the environment."""

    def test_fails_when_asked(self):
        if os.environ.get("FM_RUN_ASKED_TO_FAIL") != "1":
            self.skipTest("fails only inside the summary test")
        self.fail("asked to fail")


class Tmp(unittest.TestCase):
    """T-2019: classes leaked 45k temp dirs, some with 68k files, until /tmp ran out of inodes and git failed."""

    def test_tmp_a_class_leaves_nothing_behind(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "made")
            with open(os.path.join(d, "test_leak.py"), "w") as f:
                f.write(f"import tempfile, unittest\nclass S(unittest.TestCase):\n    def test_s(self):\n"
                        f"        open({out!r}, 'w').write(tempfile.mkdtemp())\n")
            with mock.patch.object(run, "HERE", d):
                _, code, err, _, _ = run.run_one("test_leak.S", [])
            self.assertEqual(code, 0, err)
            made = open(out).read()
        self.assertNotEqual(os.path.dirname(made), tempfile.gettempdir())  # in the class's own dir
        self.assertFalse(os.path.exists(made))
        self.assertFalse(os.path.exists(os.path.dirname(made)))

    def test_tmp_a_class_whose_folder_cant_be_made_fails_alone(self):  # T-3236: ENOSPC (inodes) crashed the run
        import errno
        from unittest import mock
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(run.tempfile, "mkdtemp", side_effect=full):
            cid, code, err, _, _ = run.run_one("test_x.C", [])
        self.assertEqual((cid, code), ("test_x.C", 1))
        self.assertIn("No space left on device", err)
        self.assertIn("temp folder", err)

    def test_tmp_a_dead_runs_dirs_are_swept_and_a_live_ones_kept(self):
        import subprocess
        import tempfile
        p = subprocess.Popen(["true"])
        p.wait()  # its pid is now a dead run's
        with tempfile.TemporaryDirectory() as base:
            dead = os.path.join(base, f"{run.TMP_PREFIX}{p.pid}-abc")
            live = os.path.join(base, f"{run.TMP_PREFIX}{os.getpid()}-def")
            other = os.path.join(base, "tmpkeepme1")
            fork_dead, fork_live = (os.path.join(base, f"fmfork-{n}-x") for n in (p.pid, os.getpid()))  # T-3572
            for x in (dead, live, other, fork_dead, fork_live):
                os.makedirs(os.path.join(x, "sub"))
            run.sweep(base)
            self.assertEqual(sorted(os.listdir(base)), sorted(os.path.basename(x) for x in (live, other, fork_live)))


class Hang(unittest.TestCase):
    def test_a_class_that_hangs_is_killed_and_named_with_its_stack(self):  # T-1808: CI's job cap hid where
        import tempfile
        import time
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "test_stuck.py"), "w") as f:
                f.write("import subprocess, time, unittest\nclass S(unittest.TestCase):\n    def test_s(self):\n"
                        "        subprocess.Popen(['sleep', '60'])  # a child holding the pipes\n"
                        "        time.sleep(60)\n")
            t0 = time.time()
            with mock.patch.object(run, "HERE", d):
                cid, code, err, _, _ = run.run_one("test_stuck.S", [], timeout=2)
        self.assertLess(time.time() - t0, 30)  # wall clock: the timeout=2 kill deadline
        self.assertNotEqual(code, 0)
        self.assertIn("still running after 2s", err)
        self.assertIn("test_s", err)  # where it hung

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_PDEATHSIG is Linux's")
    def test_a_class_still_running_dies_with_an_orphaning_kill_of_the_run(self):  # T-2034: one spun a core for a day
        import contextlib
        import subprocess
        import tempfile
        import time

        def gone(pid):
            try:
                with open(f"/proc/{pid}/stat") as f:
                    return f.read().rsplit(")", 1)[1].split()[0] == "Z"  # dead, not yet reaped
            except (FileNotFoundError, ProcessLookupError):  # T-2115: /proc reads ESRCH while the task exits
                return True

        with tempfile.TemporaryDirectory() as d:
            pidf = os.path.join(d, "pid")
            with open(os.path.join(d, "test_orphan.py"), "w") as f:
                f.write(f"import os, time, unittest\nclass S(unittest.TestCase):\n    def test_s(self):\n"
                        f"        with open({pidf!r}, 'w') as f:\n            f.write(str(os.getpid()))\n"
                        "        time.sleep(60)\n")
            boss = subprocess.Popen([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import run; "
                                     "run.HERE = sys.argv[2]; run.run_one('test_orphan.S', [], timeout=60)",
                                     os.path.dirname(os.path.abspath(run.__file__)), d])
            pid = None
            try:
                for _ in range(400):
                    with contextlib.suppress(OSError, ValueError):
                        with open(pidf) as f:
                            pid = int(f.read())
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(pid, "the class never started")
                boss.kill()  # a Bash timeout's SIGKILL: run.py gets no chance to clean up
                boss.wait()
                for _ in range(100):
                    if gone(pid):
                        break
                    time.sleep(0.05)
                self.assertTrue(gone(pid), "the class outlived the run that started it")
            finally:
                boss.kill()
                if pid and not gone(pid):
                    with contextlib.suppress(ProcessLookupError):  # it may exit between the check and the kill
                        os.kill(pid, 9)

    def test_the_run_deadline_kills_a_running_class_and_skips_unstarted_ones(self):
        # T-1809: macOS hit its 35 min job cap with no annotation at all; the run now ends itself first
        import tempfile
        import time
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "test_slow.py"), "w") as f:
                f.write("import time, unittest\nclass S(unittest.TestCase):\n    def test_s(self):\n"
                        "        time.sleep(60)\n")
            t0 = time.time()
            with mock.patch.object(run, "HERE", d):
                _, code, err, _, _ = run.run_one("test_slow.S", [], timeout=480, deadline=time.time() + 2)
                self.assertLess(time.time() - t0, 30)  # wall clock: a kill deadline of a child
                self.assertNotEqual(code, 0)
                self.assertRegex(err, r"still running at the run's deadline \([12]s in\)")  # review: not the 480 s cap
                self.assertIn("test_s", err)  # the stack
                for late in (time.time() - 1, time.time() + 0.4):  # review: under 1 s left is too late to start
                    _, code, err, ran, _ = run.run_one("test_slow.S", [], deadline=late)
                    self.assertEqual((code, ran, err), (1, 0, run.NOT_STARTED))
        self.assertEqual(run.parse(["--deadline", "60"]).deadline, 60)
        self.assertIsNone(run.parse([]).deadline)

    def test_main_names_unstarted_classes_and_ci_gets_the_notes(self):
        # review: main's exit code, the not-started line (outside CI too) and the CI-only notes had no test
        import contextlib
        import io
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "test_late.py"), "w") as f:
                f.write("import unittest\nclass L(unittest.TestCase):\n    def test_l(self):\n        pass\n")
            out, err = io.StringIO(), io.StringIO()
            clock = iter([100.0])  # the run starts at 100; every later reading is past its 60 s deadline
            with mock.patch.object(run, "HERE", d), mock.patch.object(run, "skipped", return_value=set()), \
                    mock.patch.object(run.time, "time", side_effect=lambda: next(clock, 200.0)), \
                    mock.patch.object(sys, "argv", ["run.py", "--deadline", "60", "-p", "test_late.py"]), \
                    in_process(GITHUB_ACTIONS="true"), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = run.main()
        self.assertEqual(code, 124)  # T-2944: stopped at its deadline, nothing really failed: a timeout
        self.assertTrue(err.getvalue().rstrip().endswith("run.py: stopped at its deadline"))
        self.assertIn("not started (the run's 60s deadline): test_late.L", err.getvalue())
        self.assertIn("::error title=run.py::1 classes never started", out.getvalue())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run.parse(["--deadline", "0"])  # review: 0 read as falsy turned the deadline off


class DeadlineIsNotFailure(unittest.TestCase):
    """T-3936: classes cut by the deadline read 'not run', and a load cap-kill gets one solo retry."""

    def main(self, results, load=0.0):
        import contextlib
        import io
        import tempfile
        calls = []

        def fake(cid, k, timeout=480, deadline=None, small=False, joins=None):  # T-4247, T-4267: main passes small and joins too
            calls.append(cid)
            r = results[cid]
            return r.pop(0) if isinstance(r, list) else r
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            for m in ({c.split(".")[0] for c in results}):
                with open(os.path.join(d, m + ".py"), "w") as f:
                    f.write("import unittest\n" + "".join(f"class {c.split('.')[1]}(unittest.TestCase):\n"
                            "    def test_a(self):\n        pass\n" for c in results if c.startswith(m + ".")))
            with mock.patch.object(run, "HERE", d), mock.patch.object(run, "skipped", return_value=set()), \
                    mock.patch.object(run, "run_one", side_effect=fake), \
                    mock.patch.object(run, "load_per_cpu", return_value=load), \
                    mock.patch.object(sys, "argv", ["run.py", "-j", "1", "-p", *[m + ".py" for m in {c.split(".")[0] for c in results}]]), \
                    in_process(FOREMAN_NO_TEST_CACHE="1", FM_TEST_TIMES=os.path.join(d, "t.json")), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = run.main()
            for m in {c.split(".")[0] for c in results}:  # the next call's folder holds another file of that name
                sys.modules.pop(m, None)
        return code, err.getvalue(), calls

    def test_cut_classes_are_not_run_not_failed(self):
        bad = ("test_t1.A", 1, "boom\n", 1, 1.0)
        cut = lambda c: (c, 1, run.NOT_STARTED, 0, 0.0)  # noqa: E731
        code, err, _ = self.main({"test_t1.A": bad, "test_t1.B": cut("test_t1.B"), "test_t1.C": cut("test_t1.C")})
        self.assertEqual(code, 1)
        self.assertIn("FAILED (1 classes: test_t1.A)", err)
        self.assertIn("not run (deadline): 2\n  not run class: test_t1.B\n  not run class: test_t1.C\n", err)
        self.assertEqual(re.findall(r"^  failed class: (\S+)", err, re.M), ["test_t1.A"])  # core's failing classes
        code, err, _ = self.main({"test_t1.B": cut("test_t1.B"), "test_t1.C": cut("test_t1.C")})
        self.assertEqual(code, 124)
        self.assertIn("INCOMPLETE", err)
        self.assertTrue(err.rstrip().endswith("run.py: stopped at its deadline"))  # cli._timed_out reads it
        self.assertNotIn("FAILED", err)

    def test_a_cap_kill_under_load_is_retried_once_alone(self):
        kill = ("test_t2.A", -9, "x\nrun.py: test_t2.A still running after 480s: killed (T-1808)\n", 0, 480.0)
        ok = ("test_t2.A", 0, "Ran 1 test\n", 1, 3.0)
        code, err, calls = self.main({"test_t2.A": [kill, ok]}, load=2.0)
        self.assertEqual((code, calls), (0, ["test_t2.A"] * 2))
        self.assertIn("test_t2.A passed alone (load 2.0 per CPU", err)
        code, err, calls = self.main({"test_t2.A": [kill, ok]}, load=0.5)  # an idle host: a real hang, not retried
        self.assertEqual((code, calls), (1, ["test_t2.A"]))
        self.assertIn("FAILED", err)
        code, err, calls = self.main({"test_t2.A": [kill, kill]}, load=2.0)  # still killed alone: failed, one retry only
        self.assertEqual((code, calls), (1, ["test_t2.A"] * 2))

    def test_a_red_class_under_load_is_a_flake_when_green_alone(self):
        """T-4442: a loaded host fails classes the code did not: red past the load bar runs once more alone."""
        red = ("test_t3.A", 1, "FAIL: test_a\nboom\n", 1, 2.0)
        ok = ("test_t3.A", 0, "Ran 1 test\n", 1, 2.0)
        code, err, calls = self.main({"test_t3.A": [red, ok]}, load=2.0)
        self.assertEqual((code, calls), (0, ["test_t3.A"] * 2))
        self.assertIn("test_t3.A load flake: failed once, passed alone (load 2.0 per CPU)", err)
        self.assertNotIn("FAILED", err)
        code, err, calls = self.main({"test_t3.A": [red, ok]}, load=0.5)  # a quiet host: the first red stands
        self.assertEqual((code, calls), (1, ["test_t3.A"]))
        self.assertIn("FAILED", err)
        code, err, calls = self.main({"test_t3.A": [red, red]}, load=2.0)  # red twice stands, one rerun only
        self.assertEqual((code, calls), (1, ["test_t3.A"] * 2))
        late = ("test_t3.A", 1, run.NOT_STARTED, 0, 0.0)  # a deadline's cut is not a failure to rerun
        code, err, calls = self.main({"test_t3.A": [late, ok]}, load=2.0)
        self.assertEqual((code, calls), (124, ["test_t3.A"]))
        with mock.patch.dict(os.environ, {"FOREMAN_LOAD_RERUN": "0"}):  # the switch: a red class is never run twice
            code, err, calls = self.main({"test_t3.A": [red, ok]}, load=2.0)
        self.assertEqual((code, calls), (1, ["test_t3.A"]))


class HangsWhenAsked(unittest.TestCase):
    """T-3338: hangs only when test_a_killed_class_is_timed_and_split_next_run asks, through the environment."""

    def test_a(self):
        if os.environ.get("FM_RUN_HANG"):
            import time
            time.sleep(120)

    def test_b(self):
        pass


class SplitMe(unittest.TestCase):
    """T-3166: no class-level setup, so run.py runs each of these as its own job."""

    def test_a(self):
        pass

    def test_b(self):
        self.assertFalse(os.environ.get("FM_RUN_SPLIT_FAIL"))

    def test_c(self):
        pass


class WithSetUpClass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = 1

    def test_a(self):
        self.assertEqual(self.shared, 1)

    def test_b(self):
        pass


class OptedOut(unittest.TestCase):
    run_whole = True  # T-3166: the marker for a class whose tests lean on each other

    def test_a(self):
        pass

    def test_b(self):
        pass


class PerTestJobs(unittest.TestCase):
    """T-3166: a class's tests are separate jobs in the pool, longest first, so no one class holds the run."""

    def walk(self):
        parts, whole = {}, set()
        run.classes(["test_run.py"], [], set(), names=["test_run"], parts=parts, whole=whole)
        return parts, whole

    def test_classes_with_setup_or_the_marker_stay_whole(self):
        parts, whole = self.walk()
        self.assertEqual(sorted(parts["test_run.SplitMe"]), [f"test_run.SplitMe.test_{x}" for x in "abc"])
        self.assertEqual(whole, {"test_run.WithSetUpClass", "test_run.OptedOut"})
        self.assertEqual(len(parts["test_run.OptedOut"]), 2)  # still counted, for the order

    def test_a_module_with_setUpModule_stays_whole(self):
        from unittest import mock
        with mock.patch.object(sys.modules[__name__], "setUpModule", lambda: None, create=True):
            self.assertIn("test_run.SplitMe", self.walk()[1])

    def test_plan_splits_known_slow_classes_and_orders_longest_first(self):
        parts = {"m.Slow": ["m.Slow.t1", "m.Slow.t2"], "m.Fast": ["m.Fast.t1", "m.Fast.t2"], "m.Held": ["m.Held.t1"]}
        times = {"m.Slow": 100.0, "m.Slow.t1": 30.0, "m.Slow.t2": 70.0, "m.Fast": 1.0, "m.Held": 500.0}
        got = run.plan(["m.Fast", "m.Slow", "m.Held"], parts, {"m.Held"}, times)
        self.assertEqual(got, ["m.Held", "m.Slow.t2", "m.Slow.t1", "m.Fast"])

    def test_plan_without_times_splits_only_the_seeded_slow_classes(self):
        # review: a shard (or a first run) must not start an interpreter per test of every class
        parts = {"m.LeanKind": ["m.LeanKind.t1", "m.LeanKind.t2"], "m.Big": [f"m.Big.t{i}" for i in range(50)]}
        got = run.plan(["m.Big", "m.LeanKind"], parts, set(), {})
        self.assertEqual(sorted(got), sorted(["m.Big", *parts["m.LeanKind"]]))
        self.assertEqual(run.plan(["m.Big"], parts, set(), {"m.Big": 0.5}), ["m.Big"])

    def test_work_time_leaves_out_each_jobs_start_up_so_a_class_is_not_ratcheted_split(self):
        self.assertEqual(run.work_time([run.START_S + 0.1] * 10), round(0.1 * 10, 2))
        self.assertLess(run.work_time([run.START_S] * 100), run.SPLIT_S)  # 100 trivial tests: back to one job
        self.assertEqual(run.work_time([]), 0.0)

    def test_a_k_run_keeps_a_whole_class_time_and_a_full_run_records_it(self):
        r, got = nested(["test_run.WithSetUpClass", "-k", "test_a"], seed={"test_run.WithSetUpClass": 99.0})
        self.assertEqual(got["test_run.WithSetUpClass"], 99.0, r.stderr)  # review: -k's partial time overwrote it
        r, got = nested(["test_run.WithSetUpClass"], seed={"test_run.WithSetUpClass": 99.0})
        self.assertLess(got["test_run.WithSetUpClass"], 50, r.stderr)

    def test_a_testcase_imported_into_a_second_module_is_planned_once(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "test_dedup_a.py"), "w") as f:
                f.write("import unittest\nclass Lake(unittest.TestCase):\n    def test_1(self): pass\n"
                        "    def test_2(self): pass\n")
            with open(os.path.join(d, "test_dedup_b.py"), "w") as f:
                f.write("from test_dedup_a import Lake  # noqa\n")
            parts = {}
            with mock.patch.object(run, "HERE", d), mock.patch.object(sys, "path", list(sys.path)):
                run.classes(["test_dedup_*.py"], [], set(), parts=parts)
            for m in ("test_dedup_a", "test_dedup_b"):
                sys.modules.pop(m, None)
        self.assertEqual(parts, {"test_dedup_a.Lake": ["test_dedup_a.Lake.test_1", "test_dedup_a.Lake.test_2"]})

    def test_a_non_classmethod_setUpClass_keeps_the_class_whole_not_a_crash(self):
        class Odd(unittest.TestCase):
            setUpClass = None

            def test_x(self):
                pass
        self.assertTrue(run.keeps_whole(Odd("test_x")))
        self.assertFalse(run.keeps_whole(SplitMe("test_a")))

    def test_an_unreadable_times_file_is_empty_and_a_planted_symlink_is_not_followed(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            p, victim = os.path.join(d, "t.json"), os.path.join(d, "victim")
            with mock.patch.dict(os.environ, {"FM_TEST_TIMES": p}):
                with open(p, "w") as f:
                    f.write('{"a": 1' + "0" * 400 + "}")  # float() of it is an OverflowError
                self.assertEqual(run.load_times(), {})
                os.remove(p)
                with open(victim, "w") as f:
                    f.write("mine")
                os.symlink(victim, f"{p}.{os.getpid()}")
                run.save_times({"a.B": 1.0})
                with open(victim) as f:
                    self.assertEqual(f.read(), "mine")
                self.assertEqual(run.load_times(), {"a.B": 1.0})
        if run._common():  # T-4268: the repo's file, shared by its worktrees (no git: the legacy temp file)
            self.assertTrue(run.times_path().endswith(".times.json"), run.times_path())
        self.assertIn(str(getattr(os, "getuid", lambda: 0)()), os.path.basename(run.legacy_times_path()))

    def test_times_round_trip_and_a_corrupt_file_is_empty(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            with mock.patch.dict(os.environ, {"FM_TEST_TIMES": p}):
                self.assertEqual(run.load_times(), {})
                run.save_times({"a.B": 2.5})
                run.save_times({"a.C": 1.0})
                self.assertEqual(run.load_times(), {"a.B": 2.5, "a.C": 1.0})
                with open(p, "w") as f:
                    f.write("{not json")
                self.assertEqual(run.load_times(), {})

    def test_a_split_class_runs_as_one_job_per_test_and_a_failure_names_the_test(self):
        # SplitMe is not a seeded name: the seeded time is what splits it
        seed = {"test_run.SplitMe": 99}
        r, got = nested(["test_run.SplitMe"], seed)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ran 3 tests", r.stderr)
        self.assertIn("(3 jobs", r.stderr)
        self.assertIn("test_run.SplitMe.test_a", got)  # the run left its times for the next
        r, _ = nested(["test_run.SplitMe"], seed, {"FM_RUN_SPLIT_FAIL": "1"})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stderr.strip().splitlines()[-1], "FAILED (1 classes: test_run.SplitMe.test_b)")
        self.assertIn("FAIL: test_b", r.stderr)

    def test_a_killed_class_is_timed_and_split_next_run(self):  # T-3338: Lanes and Proof were killed at 480 s on every rerun
        r, got = nested(["test_run.HangsWhenAsked", "--timeout", "1"], env={"FM_RUN_HANG": "1"})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("still running after", r.stderr)
        self.assertGreaterEqual(got.get("test_run.HangsWhenAsked", 0), run.SPLIT_S)
        r, _ = nested(["test_run.HangsWhenAsked", "--timeout", "1"], got, {"FM_RUN_HANG": "1"})
        self.assertIn("(2 jobs", r.stderr)
        self.assertEqual(r.stderr.strip().splitlines()[-1], "FAILED (1 classes: test_run.HangsWhenAsked.test_a)")

    def test_a_deadline_kill_is_not_recorded_as_slow_and_the_env_sets_the_deadline(self):  # T-3338 review
        r, got = nested(["test_run.HangsWhenAsked"], env={"FM_RUN_HANG": "1", "FM_RUN_DEADLINE": "3"})
        self.assertEqual(r.returncode, 124, r.stderr)  # T-2944: only the deadline stopped it: a timeout
        self.assertIn("deadline", r.stderr)  # killed at it, or still waiting for a CPU slot under load
        self.assertNotIn("test_run.HangsWhenAsked", got or {})

    def test_a_class_with_setup_runs_whole(self):
        r, _ = nested(["test_run.WithSetUpClass"], {"test_run.WithSetUpClass": 99})
        self.assertIn("(1 jobs", r.stderr)
        self.assertIn("Ran 2 tests", r.stderr)


class SuiteSlot(unittest.TestCase):
    """T-3541: three full suites at once (two builders' and the ship's: 38 workers, load 30 on 16 CPUs) each took 3x as
    long and slowed the whole machine; a full run now holds one machine-wide slot, and a second one waits for it."""

    def setUp(self):
        import shutil
        import tempfile
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        shutil.copy(run.__file__, self.d)  # its own copy: a full run of it discovers only the fake test below
        self.log, self.go = os.path.join(self.d, "log"), os.path.join(self.d, "go")
        with open(os.path.join(self.d, "test_fake.py"), "w") as f:  # holds its run until the test says go
            f.write(f"import os, time, unittest\nclass F(unittest.TestCase):\n    def test_f(self):\n"
                    f"        t = time.time()\n        while not os.path.exists({self.go!r}) and time.time() < t + 60:\n"
                    f"            time.sleep(0.05)\n        open({self.log!r}, 'a').write(f'{{t}} {{time.time()}}\\n')\n")
        self.lock = os.path.join(self.d, "cache", "sampo", "suite.lock")
        self.env = {k: v for k, v in os.environ.items() if not k.endswith("_SLOT")}  # not nested, slot not skipped
        self.env.update(XDG_CACHE_HOME=os.path.join(self.d, "cache"), SAMPO_SLOTS="0", FM_RUN_DEADLINE="16",
                        FM_TEST_TIMES=os.path.join(self.d, "t.json"))

    def start(self, *args, **env):
        import subprocess
        return subprocess.Popen([sys.executable, os.path.join(self.d, "run.py"), *args], cwd=self.d, text=True,
                                env=dict(self.env, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def held(self):
        """The slot taken here, as a full suite started 3 minutes ago in /elsewhere would hold it."""
        import fcntl
        import json
        import time
        os.makedirs(os.path.dirname(self.lock))
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, json.dumps({"pid": 4242, "cwd": "/elsewhere", "at": time.time() - 185}).encode())
        open(self.go, "w").close()

    def test_two_full_runs_take_turns_and_the_second_says_for_whom(self):
        import time
        first = self.start("--deadline", "120")
        end = time.time() + 60
        while time.time() < end and f'"pid": {first.pid}' not in (open(self.lock).read() if os.path.exists(self.lock) else ""):
            time.sleep(0.05)
        second = self.start("--deadline", "120")
        said = ""
        for line in second.stderr:  # its first lines, before its run can start
            said += line
            if "waiting for" in line:
                break
        open(self.go, "w").close()
        outs = [p.communicate(timeout=120) for p in (first, second)]
        self.assertEqual([p.returncode for p in (first, second)], [0, 0], outs)
        self.assertIn(f"run.py: waiting for the full suite running in {os.path.realpath(self.d)} (pid {first.pid}, 0 min)",
                      said)
        self.assertNotIn("waiting for", outs[0][1])
        (s1, e1), (s2, e2) = sorted(tuple(map(float, ln.split())) for ln in open(self.log))
        self.assertLessEqual(e1, s2, "the two full runs overlapped")

    def test_a_targeted_run_never_waits(self):
        self.held()
        r = self.start("test_fake")
        _, err = r.communicate(timeout=120)
        self.assertEqual(r.returncode, 0, err)
        self.assertNotIn("waiting for", err)

    def test_the_switch_skips_the_slot_and_a_wait_runs_out_beside_the_holder(self):
        self.held()
        r = self.start(FOREMAN_NO_SUITE_SLOT="1")
        _, err = r.communicate(timeout=120)
        self.assertEqual(r.returncode, 0, err)
        self.assertNotIn("waiting for", err)
        r = self.start()  # FM_RUN_DEADLINE 16: it waits half of it, then runs beside the holder
        _, err = r.communicate(timeout=120)
        self.assertEqual(r.returncode, 0, err)
        self.assertIn("run.py: waiting for the full suite running in /elsewhere (pid 4242, 3 min)", err)
        self.assertIn("going ahead beside it", err)


# T-3625: FOREMAN_/SAMPO_ names (prefix cut) that reach a test class on purpose: paths, identity and the harness's
# own isolation or test knobs. Any other name Sampo reads must be an operator switch (run.OPERATOR_SWITCHES).
HARNESS_ENV = set("""ABLATE AMBIENT_SYNC BENCH BENCH_SESSIONS CALLCARDS CALLS CARDS CGROUP CHECK_LOG CHROME CLAIMS_REMOTE CODE_ATTENDED
DISK_FREE DOCKER DOCTOR_BENCH FAULT_LANE GUARD_CASES HARNESS HOME HOOK_MODULES HOOK_SLACK HOST IDEAS_WARM JEFF_HOME LANE_CHILD MARATHON
MCP_TIMEOUT NO_CARDS NO_LOCAL_MODELS NO_RIVAL NO_SESSION_LANES NOW ONEOFF PG_DIRS PLAYBOOKS PLAYWRIGHT PROJECT PY_CHILD
RECORD_OUT RECORD_ROOT RELEASE_FM SCOUT_CHANGELOG SCOUT_INSTALLED SCOUT_LATEST SCOUT_QUICK_ONLY SECRETS SESSION_ID SKILLS
CRATES_URL NATIVE_BIN NPM_URL PYPI_URL SKIP_TESTS SLOW_CLOSE_S SPEND_AS SPEND_TOWER STACK_AFTER STATE STATE_FALLBACK THREE TODAY TOWER_READONLY_EXAM WORKER_OF""".split())


class EnvClassified(unittest.TestCase):
    def test_every_env_name_sampo_reads_is_an_operator_switch_or_a_named_harness_name(self):  # T-3625
        import re
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        pat = re.compile(r"""["'](?:FOREMAN|SAMPO)_([A-Z0-9_]+)["']""")  # any literal: environ, env.get, _env(...)
        found = set()
        for sub in ("lib", "hooks", "bin"):
            for d, _, files in os.walk(os.path.join(root, sub)):
                for f in files:
                    try:
                        with open(os.path.join(d, f), encoding="utf-8") as fh:
                            found |= set(pat.findall(fh.read()))
                    except (OSError, UnicodeDecodeError):
                        pass
        new = sorted(found - run.OPERATOR_SWITCHES - HARNESS_ENV)
        self.assertEqual(new, [], "".join(
            f"\n  {n}: add {n!r} to run.OPERATOR_SWITCHES in plugin/tests/run.py (if it changes behaviour under test) "
            "or to HARNESS_ENV in plugin/tests/test_run.py (a path, setting or identity tests may inherit)" for n in new))
        self.assertTrue(found, "the scan found nothing: its pattern is stale")


class ChildEnv(unittest.TestCase):
    def test_operator_switches_never_reach_a_test_class(self):  # T-3625: a runner's FOREMAN_LOAD_GATE=0 failed LoadGates
        keep = {"FOREMAN_LOAD_GATE": "0", "FOREMAN_FULL_GATE": "1", "SAMPO_SLOT_PRIORITY": "9", "SAMPO_LOAD_GATE": "0",
                "FOREMAN_SENTINEL": "1", "FOREMAN_NO_BACKGROUND": "1", "SAMPO_SLOT": "3", "FOREMAN_HOME": "/x/alice"}
        keep["PIP_QUIET"] = "1"  # another tool's variable of the same tail is not Sampo's switch
        saved = {k: os.environ.get(k) for k in keep}
        os.environ.update(keep)
        try:
            env = run.child_env("/tmp/x")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual({k for k in keep if k in env}, {"FOREMAN_HOME", "PIP_QUIET"})
