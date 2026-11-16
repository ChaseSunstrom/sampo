"""T-0805: the hook benchmark's verdict: load alone never fails it, a code regression always does."""
import os
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(__file__))
import bench_hooks  # noqa: E402
from helpers import SampoTestCase  # noqa: E402


class Verdict(unittest.TestCase):
    def test_load_alone_never_fails_the_gate(self):
        # three gate runs on one machine (load 23-74) put every hook over 150 ms; alone they were 30-45 ms
        ok = bench_hooks.within
        self.assertTrue(ok(wall_p95=40, cpu_p95=30, budget=150, load=0.3))
        self.assertFalse(ok(wall_p95=400, cpu_p95=30, budget=150, load=0.3), "quiet machine: wall time decides")
        self.assertTrue(ok(wall_p95=400, cpu_p95=30, budget=150, load=4.0), "busy: CPU time decides")
        self.assertFalse(ok(wall_p95=400, cpu_p95=200, budget=150, load=4.0), "a real regression shows in CPU time")

    def test_a_busy_machine_judges_the_cpu_median(self):
        # T-3821: two runs of one commit at load 2.2-2.4 per core: SessionStart CPU p95 404 then 106 ms (budget 150);
        # the spike left a good lane out of a merge train. The median holds; a regression moves it
        ok = bench_hooks.within
        self.assertTrue(ok(wall_p95=900, cpu_p95=404, budget=150, load=2.3, cpu_p50=95), "a spike alone passes")
        self.assertFalse(ok(wall_p95=900, cpu_p95=404, budget=150, load=2.3, cpu_p50=180), "a slower median fails")
        self.assertFalse(ok(wall_p95=400, cpu_p95=30, budget=150, load=0.3, cpu_p50=20), "quiet: wall p95 still decides")

    def test_cpu_pressure_judges_by_cpu_time_where_the_load_average_lags(self):
        # T-4029: the land stopped six times in a day on a red base bench that passed at once by hand; the 1-minute
        # load read under 1 a core while CPU pressure (tasks waiting for a CPU) stood at 51%
        ok = bench_hooks.within
        self.assertTrue(ok(wall_p95=400, cpu_p95=60, budget=150, load=0.9, cpu_p50=50, pressure=51), "contended")
        self.assertFalse(ok(wall_p95=400, cpu_p95=60, budget=150, load=0.9, cpu_p50=50, pressure=2), "quiet: wall")
        self.assertFalse(ok(wall_p95=400, cpu_p95=200, budget=150, load=0.9, cpu_p50=180, pressure=51), "a regression")

    def test_cpu_pressure_is_the_higher_of_the_cgroup_and_the_machine_and_none_reads_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "app.scope"))
            for rel, text in (("self.cgroup", "0::/app.scope\n"), ("system", "some avg10=40.00 avg60=9.00 avg300=1.00 total=9\n"),
                              ("app.scope/cpu.pressure", "some avg10=12.50 avg60=3.00 avg300=1.00 total=1\n"
                                                         "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")):
                with open(os.path.join(root, rel), "w") as f:
                    f.write(text)
            cg, system, none = (os.path.join(root, n) for n in ("self.cgroup", "system", "none"))
            self.assertEqual(bench_hooks.cpu_pressure(cg, root, system), 40.0)
            self.assertEqual(bench_hooks.cpu_pressure(cg, root, none), 12.5)
            self.assertEqual(bench_hooks.cpu_pressure(none, root, none), 0.0)  # no PSI (macOS, an old kernel)


class CappedScope(unittest.TestCase):  # T-4018
    def test_load_counts_against_the_cores_a_cgroup_cap_leaves(self):
        # a session capped at 4 of 16 cores: machine load 7.4 is 0.46 a core, yet the scope ran throttled (wall p95
        # 398 ms, CPU p95 101 ms) and the land stopped on a red base; against the 4 cores it may use, the load is busy
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as root:
            for rel, quota in (("user.slice", "max 100000"), ("user.slice/app.scope", "400000 100000")):
                os.makedirs(os.path.join(root, rel))
                with open(os.path.join(root, rel, "cpu.max"), "w") as f:
                    f.write(quota + "\n")
            with open(os.path.join(root, "self.cgroup"), "w") as f:
                f.write("0::/user.slice/app.scope\n")
            with mock.patch.object(os, "getloadavg", return_value=(7.4, 7, 7)), \
                    mock.patch.object(os, "cpu_count", return_value=16):
                self.assertAlmostEqual(bench_hooks.load_per_core(os.path.join(root, "self.cgroup"), root), 1.85)
                self.assertAlmostEqual(bench_hooks.load_per_core(os.path.join(root, "none"), root), 7.4 / 16)  # no cgroup


class ScriptBudget(unittest.TestCase):  # T-1777
    def test_the_script_case_is_a_hook_plus_the_guards_time_box(self):
        # a cold script read costs at most the guard's CPU box (T-4758: the bench's script now finishes inside it), so the
        # case costs a hook's startup plus that box: a hook's budget plus the box, moving with it
        sys.path.insert(0, os.path.join(bench_hooks.PLUGIN, "lib"))  # the test's own process; the bench no longer does
        import fmguard
        self.assertEqual(bench_hooks.BUDGET_MS["PreToolUse:Bash:script"],
                         bench_hooks.BUDGET_MS["default"] + round(fmguard._script_budget() * 1000))


class ScriptCase(unittest.TestCase):  # T-4758
    def test_the_benched_script_is_one_the_guard_reads_to_the_end(self):
        # doctor's "hook exit codes" expects the script case to exit 0: a script the guard cannot read inside its budgets
        # (the old 37 KB one took 44 s to parse; 4 KB takes 1.9 s: the cost grows with the square of the size) is refused,
        # so the probe must be one the guard reads and allows, cold (the run number in it makes each run a new digest)
        import tempfile
        sys.path.insert(0, os.path.join(bench_hooks.PLUGIN, "lib"))
        import fmguard
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "app")
            os.makedirs(os.path.join(repo, "src"))
            fhome = os.path.join(tmp, "fhome")
            os.makedirs(os.path.join(fhome, "state", "projects", "x"))
            ctx = fmguard.Ctx(cwd=repo, project_root=repo, home=tmp, foreman_home=fhome, scratch=["/tmp"], allow=set(),
                              task_id="T-0007")
            bench_hooks._write_script(repo, 0)
            fmguard.take_unread()
            self.assertIsNone(fmguard.check("Bash", {"command": "bash src/build.sh"}, ctx))
            self.assertEqual(fmguard.take_unread(), [], "the guard ran out of its budgets reading the bench's script")


class Isolation(unittest.TestCase):  # T-3132
    def test_the_benched_trees_code_never_enters_the_timing_process(self):
        import json
        import shutil
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, "plugin", "lib"))
            os.makedirs(os.path.join(t, "plugin", "tests"))
            shutil.copy(os.path.join(os.path.dirname(__file__), "bench_hooks.py"), os.path.join(t, "plugin", "tests"))
            with open(os.path.join(t, "plugin", "lib", "fmguard.py"), "w") as f:
                f.write("import resource\nresource.getrusage = lambda *a: 1/0\n"
                        "def _script_budget():\n    return 0.7\n")
            code = ("import sys, json, resource; sys.path.insert(0, 'plugin/tests'); import bench_hooks as b; "
                    "print(json.dumps([[m for m in sys.modules if m.startswith('fmguard')], "
                    "resource.getrusage.__module__, b.BUDGET_MS['PreToolUse:Bash:script']]))")
            r = subprocess.run([sys.executable, "-I", "-c", code], cwd=t, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            mods, rusage, budget = json.loads(r.stdout)
            self.assertEqual(mods, [])
            self.assertEqual(rusage, "resource")
            self.assertEqual(budget, 150 + 700, "the child still reads the tree's budget")


    def test_a_branch_cant_loosen_its_own_script_limit(self):
        import tempfile
        for val, want in (("600.0", 1.0), ("0.4", 0.4), ("float('nan')", 0.2), ("-3", 0.2), ("0", 0.2),
                          ("float('inf')", 0.2)):
            with tempfile.TemporaryDirectory() as t:
                lib = os.path.join(t, "plugin", "lib")
                os.makedirs(lib)
                with open(os.path.join(lib, "fmguard.py"), "w") as f:
                    f.write(f"def _script_budget():\n    return {val}\n")
                orig = bench_hooks.PLUGIN
                bench_hooks.PLUGIN = os.path.join(t, "plugin")
                try:
                    self.assertEqual(bench_hooks.script_budget(), want, val)
                finally:
                    bench_hooks.PLUGIN = orig


class NoBackground(unittest.TestCase):  # T-4344
    """sampo doctor, sampo ship's record and the benchmark measure the UserPromptSubmit hook on an 8-word prompt. Run
    outside the test harness, the hook then started a detached `fm exam steer expand`, a paid model call whose record
    was deleted with the scratch state. A measurement starts no background work, whatever the caller's environment."""

    def test_no_background_work_is_started_from_the_scratch_project_whatever_the_callers_environment(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_NO_BACKGROUND", None)  # as sampo doctor runs it for a user
            _, env = bench_hooks._scratch_project(tmp)
            self.assertEqual(env.get("FOREMAN_NO_BACKGROUND"), "1")
            self.assertNotIn("FOREMAN_NO_BACKGROUND", os.environ, "the caller's own environment is left as it was")

    def test_no_background_every_measurement_builds_its_environment_from_the_scratch_project(self):
        import ast
        with open(bench_hooks.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        runs_hook = [fn.name for fn in tree.body if isinstance(fn, ast.FunctionDef)
                     and any(isinstance(n, ast.Name) and n.id == "HOOK" for n in ast.walk(fn))]
        self.assertTrue(runs_hook, "no function runs the hook: this test reads the wrong names")
        for fn in (x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name in runs_hook):
            calls = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            self.assertIn("_scratch_project", calls, f"{fn.name} runs the hook with an environment of its own")


class StageBench(unittest.TestCase):  # T-4780
    """The PreToolUse hook timed out (5 s, onFailure block) on short one-line commands while a merge train ran. Its
    own work is measured per stage, for 200 calls one after the other, beside a real `run.py` of test modules (started
    by the bench, stopped when the calls are done; no load of our own). The bound is on the hook's own stage clock
    (total p99 under a second); the wall figure is printed, never asserted."""

    def test_a_one_line_command_beside_a_test_run_is_under_a_second_at_p99(self):
        res = bench_hooks.stage_bench(runs=200, beside=["test_guard", "test_fast_equiv"])
        self.assertGreaterEqual(res["runs"], 50, "the test run ended before enough calls were made beside it")
        self.assertEqual(res["stage_rows"], res["runs"], "one stage row per counted call")
        # wall is recorded, not gated: on a loaded machine a fork's wait for a CPU says nothing about the hook's code
        print(f"T-4780 one-line hook beside test_guard + test_fast_equiv: wall p50/p99 {res['wall']['p50']}/"
              f"{res['wall']['p99']} ms, hook's own total p99 {res['stages']['total']['p99']} ms, load {res['load']}")
        self.assertLess(res["stages"]["total"]["p99"], 1000, res)
        for k in ("start", "imports", "state", "parse", "check", "rules", "asks", "snapshot", "after"):
            self.assertIn(k, res["stages"])
        # the cause the numbers name: the Python start and imports, CPU-bound, not a wait in the guard
        self.assertLess(res["stages"]["state"]["p50"] + res["stages"]["parse"]["p50"] + res["stages"]["check"]["p50"], 50)

    def test_the_bench_counts_only_calls_made_while_the_test_run_lived(self):
        res = bench_hooks.stage_bench(runs=3, beside=["test_nonexistent_module_4780"])  # run.py exits at once
        self.assertLessEqual(res["runs"], 3)
        self.assertEqual(res["stage_rows"], res["runs"])

    def test_the_nested_run_is_its_own_session_and_a_nested_slot_run(self):
        seen = {}
        real = bench_hooks.subprocess.Popen

        def popen(*a, **kw):  # subprocess.run goes through Popen too: keep the run.py one
            if any(str(x).endswith("run.py") for x in a[0]):
                seen.update(kw)
            return real(*a, **kw)
        with unittest.mock.patch.object(bench_hooks.subprocess, "Popen", popen):
            bench_hooks.stage_bench(runs=1, beside=["test_nonexistent_module_4780"])
        self.assertTrue(seen.get("start_new_session"), "its own session: the group can be stopped whole")
        self.assertEqual(seen["env"].get("SAMPO_SLOT"), "1", "nested: it never takes the machine's real test slots")

    def test_stopping_the_nested_run_stops_what_it_started_too(self):
        import signal
        import subprocess
        import sys
        import time
        code = ("import subprocess,sys,time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                "print(p.pid, flush=True); time.sleep(120)")
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
        grand = int(proc.stdout.readline())
        bench_hooks._stop_group(proc)
        for _ in range(50):
            try:
                os.kill(grand, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            os.kill(grand, signal.SIGKILL)
            self.fail("the grandchild outlived the group's stop")


class Modules(unittest.TestCase):  # T-3139
    def test_a_hook_run_records_the_plugin_files_its_process_imported(self):
        got = bench_hooks.hook_modules(only=lambda n: n == "Stop")
        self.assertEqual(list(got), ["Stop"])
        self.assertIn("plugin/lib/sampo/hooks.py", got["Stop"])
        self.assertTrue(all(f.startswith("plugin/") for f in got["Stop"]), got["Stop"])

    def test_the_hook_dumps_nothing_unless_asked(self):
        src = open(os.path.join(bench_hooks.HOOK), encoding="utf-8").read()
        self.assertIn("SAMPO_HOOK_MODULES", src)

    def test_the_hook_writes_only_under_the_temp_dir(self):
        import json
        import subprocess
        import shutil
        t = bench_hooks.fixed_tmp_dir()  # T-4780: a fixed temp root, whatever TMPDIR is
        try:
            ok = os.path.join(t, "ok.jsonl")
            subprocess.run([bench_hooks.HOOK, "Stop"], input="{}", text=True, capture_output=True,
                           env=dict(os.environ, SAMPO_HOOK_MODULES=ok))
            self.assertTrue(json.loads(open(ok).readline()))
            # T-4780: TMPDIR comes from the same environment as the path: pointing it at a folder changes nothing
            elsewhere = os.path.expanduser("~/.sampo-modules-tmpdir-test")
            os.makedirs(elsewhere, exist_ok=True)
            try:
                subprocess.run([bench_hooks.HOOK, "Stop"], input="{}", text=True, capture_output=True,
                               env=dict(os.environ, TMPDIR=elsewhere, SAMPO_HOOK_MODULES=os.path.join(elsewhere, "x.jsonl")))
                self.assertFalse(os.path.exists(os.path.join(elsewhere, "x.jsonl")), "TMPDIR is not honoured")
                link = os.path.join(t, "link.jsonl")
                os.symlink(os.path.join(elsewhere, "target.jsonl"), link)
                subprocess.run([bench_hooks.HOOK, "Stop"], input="{}", text=True, capture_output=True,
                               env=dict(os.environ, SAMPO_HOOK_MODULES=link))
                self.assertFalse(os.path.exists(os.path.join(elsewhere, "target.jsonl")), "a link is not followed")
            finally:
                shutil.rmtree(elsewhere, ignore_errors=True)
        finally:
            shutil.rmtree(t, ignore_errors=True)
        bad = os.path.expanduser("~/.sampo-modules-dump-test")
        subprocess.run([bench_hooks.HOOK, "Stop"], input="{}", text=True, capture_output=True,
                       env=dict(os.environ, SAMPO_HOOK_MODULES=bad))
        self.assertFalse(os.path.exists(bad))


# a meta path finder that notes the first importer of each heavy module: up to three frames outside importlib
WHO = """class _Who:
    def find_spec(self, name, path=None, target=None):
        if name in HEAVY and name not in first:
            f, at = sys._getframe(1), []
            while f and len(at) < 3:
                if "importlib" not in f.f_code.co_filename and "<frozen" not in f.f_code.co_filename:
                    at.append(f.f_code.co_filename.rsplit("/", 1)[-1] + ":" + str(f.f_lineno))
                f = f.f_back
            first[name] = "<".join(at)
        return None
sys.meta_path.insert(0, _Who())
"""


class HotPathImports(unittest.TestCase):  # T-3577
    """PreToolUse and PostToolUse run on every tool call of every agent: a Bash allow and an edit's PostToolUse load
    none of these (each costs a hook 0.5-3 ms of CPU; tempfile brings shutil and its compression modules)."""
    HEAVY = ("tempfile", "shutil", "hashlib", "threading", "subprocess", "statistics", "socket", "bz2", "lzma",
             "random", "signal", "heapq", "string", "contextlib")

    def test_the_hot_events_load_no_heavy_module(self):
        import json
        import subprocess
        import tempfile
        with open(os.path.join(bench_hooks.HERE, "fixtures", "hook_payloads.json")) as f:
            fixtures = json.load(f)
        with tempfile.TemporaryDirectory() as t:
            repo, env = bench_hooks._scratch_project(t)
            site, out = os.path.join(t, "site"), os.path.join(t, "loaded")
            os.makedirs(site)
            with open(os.path.join(site, "sitecustomize.py"), "w") as f:  # what the hook's process had at exit, each
                # with where it was first imported (the CI runner's macOS has only this message to go on)
                f.write(f"import atexit, sys\nHEAVY, first = {self.HEAVY!r}, {{}}\n" + WHO
                        + f"atexit.register(lambda: open({out!r}, 'a').write("
                        f"' '.join(m + '@' + first.get(m, '?') for m in HEAVY if m in sys.modules) + '\\n'))\n")
            env.update(PYTHONPATH=site, FOREMAN_NO_BACKGROUND="1")
            for name in ("PreToolUse:Bash", "PostToolUse"):
                event = name.split(":")[0]
                body = json.dumps(dict({"session_id": "lazy", "cwd": repo, "hook_event_name": event},
                                       **json.loads(json.dumps(fixtures[name]).replace("{cwd}", repo))))
                for _ in range(2):  # a later run reads what the first one wrote
                    if os.path.exists(out):
                        os.remove(out)
                    r = subprocess.run([bench_hooks.HOOK, event], input=body, capture_output=True, text=True,
                                       env=env, cwd=repo, timeout=120)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertNotIn('"deny"', r.stdout)
                    with open(out) as f:
                        self.assertEqual(f.read().split(), [], f"{name} loaded these")

    def test_core_finds_tempfiles_folder_and_makes_its_files_without_loading_it(self):
        import subprocess
        import tempfile
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); from sampo import core\n"
                "d = core.gettempdir(); fd, path = core.mkstemp(d, '.x.', '.tmp'); mode = os.fstat(fd).st_mode & 0o777\n"
                "os.close(fd); os.unlink(path)\n"
                "loaded = 'tempfile' in sys.modules; import tempfile\n"
                "print(loaded, d == tempfile.gettempdir(), os.path.dirname(path) == os.path.abspath(d), oct(mode))")
        with tempfile.TemporaryDirectory() as t:
            shut = os.path.join(t, "shut")
            os.makedirs(shut, mode=0o500)
            for tmpdir in (t, os.path.join(t, "missing"), shut):  # usable, absent, unwritable: the same answer
                env = {k: v for k, v in os.environ.items() if k not in ("TEMP", "TMP")}
                r = subprocess.run([sys.executable, "-c", code, os.path.join(bench_hooks.PLUGIN, "lib")],
                                   env=dict(env, TMPDIR=tmpdir), capture_output=True, text=True, cwd=t, timeout=60)
                self.assertEqual(r.stdout.split(), ["False", "True", "True", "0o600"], r.stderr)


class ActiveMemo(SampoTestCase):  # T-3577
    """A PostToolUse asked for the active task 4 times, each a scan of every brief (~30 ms at 88 active ones): within
    a hook run the scan is reused while the tasks folder is unchanged, and a write since shows at once."""

    def test_a_hook_run_scans_once_and_sees_a_write_made_since(self):
        from unittest import mock
        from sampo import core as c
        self.fm("init")
        p = c.find_project(self.repo)
        for i in range(3):
            self.fm("task", "new", f"t{i}", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.fm("focus", "T-0002")
        b = c.find_brief(p, "T-0003")
        b.meta.update(status="active", lane="/elsewhere")  # another lane's active task
        c.save_brief(p, b)
        tdir, old = os.path.join(p.dir, "tasks"), time.time() - 10
        os.utime(tdir, (old, old))  # settled: not written in the last 2 s
        self.addCleanup(setattr, c, "_ACTIVE_MEMO", None)
        c._ACTIVE_MEMO = {}  # what hooks.run sets
        with mock.patch.object(c, "_active_hits", wraps=c._active_hits) as scan:
            first = c.active_brief_fast(p)
            first.meta["mark"] = "a caller's change"
            again, there = c.active_brief_fast(p), c.active_brief_fast(p, "/elsewhere")
            self.assertEqual((first.id, again.id, there.id, scan.call_count), ("T-0002", "T-0002", "T-0003", 1))
            self.assertNotIn("mark", again.meta, "each caller gets its own copy")
            b = c.find_brief(p, "T-0002")
            b.meta["status"] = "done"
            c.save_brief(p, b)  # the folder's times move
            self.assertIsNone(c.active_brief_fast(p))
            self.assertIsNone(c.active_brief_fast(p))
            self.assertEqual(scan.call_count, 3, "a folder written in the last 2 s is scanned each time")
        c._ACTIVE_MEMO = None
        self.assertIsNone(c.active_brief_fast(p), "outside a hook run: no memo")


if __name__ == "__main__":
    unittest.main()
