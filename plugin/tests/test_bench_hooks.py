"""T-0805: the hook benchmark's verdict: load alone never fails it, a code regression always does."""
import os
import sys
import time
import unittest

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
        # the bench's 37 KB script never finishes inside the guard's CPU box (a full read took 124 s), so the case
        # costs a hook's startup plus that box: a hook's budget plus the box, moving with it
        sys.path.insert(0, os.path.join(bench_hooks.PLUGIN, "lib"))  # the test's own process; the bench no longer does
        import fmguard
        self.assertEqual(bench_hooks.BUDGET_MS["PreToolUse:Bash:script"],
                         bench_hooks.BUDGET_MS["default"] + round(fmguard._script_budget() * 1000))


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
        for val, want in (("600.0", 1.0), ("0.4", 0.4), ("float('nan')", 0.2), ("-3", 0.2)):
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
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            ok = os.path.join(t, "ok.jsonl")
            subprocess.run([bench_hooks.HOOK, "Stop"], input="{}", text=True, capture_output=True,
                           env=dict(os.environ, SAMPO_HOOK_MODULES=ok))
            self.assertTrue(json.loads(open(ok).readline()))
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
