"""T-3757: run.py measures each class's CPU (its process, the children it waited for, the orphans it adopts as their
subreaper, and its forked fm calls) and, with SAMPO_CPU_BASE, holds it to the median of the class's last green runs.
With SAMPO_CPU_GATE=1 too (the merge train) a class over its margin after one rerun fails the run like a red test, so
the train's probes leave out the lane that made it; a green run adds its numbers to the baseline and the day's suite
estimate, which sampo cpu shows."""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import forkcli  # noqa: E402
import run  # noqa: E402
from sampo import train  # noqa: E402

NESTED = ("SAMPO_SLOT", "FOREMAN_SLOT", "SAMPO_SLOTS", "FOREMAN_SLOTS", "SAMPO_NO_TEST_CACHE", "FOREMAN_NO_TEST_CACHE",
          "SAMPO_CACHE_FULL", "FOREMAN_CACHE_FULL", "SAMPO_CPU_BASE", "SAMPO_CPU_GATE", "SAMPO_CPU_FLOOR_S",
          forkcli.SHARED)


class Verdict(unittest.TestCase):
    """cpu_over: the margins, with the default floor (20 CPU s)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SAMPO_CPU_FLOOR_S", None)

    def test_a_class_over_twice_its_usual_and_the_floor_is_named(self):
        got = run.cpu_over({"m.A": [10, 11, 12], "m.B": [10, 10, 10]}, {"m.A": 40, "m.B": 25})
        self.assertEqual([(c, s, u) for c, s, u, _ in got], [("m.A", 40, 11)])  # B: 2.5x but only 15 s more
        self.assertIn("3.6x its usual", got[0][3])

    def test_a_new_class_over_the_new_class_floor_is_named_unless_its_budget_covers_it(self):
        seen = {"m.Old": [5.0]}
        self.assertEqual([c for c, *_ in run.cpu_over(seen, {"m.New": 95, "m.Small": 30})], ["m.New"])
        self.assertEqual(run.cpu_over(seen, {"m.New": 95}, {"m.New": 120}), [])

    def test_an_empty_baseline_names_nothing_so_the_first_green_run_seeds_it(self):
        self.assertEqual(run.cpu_over({}, {"m.Big": 400, "m.Small": 30}), [])

    def test_a_class_compares_only_with_samples_split_the_same_way(self):
        hist = {"m.A": [[10.0, 1], [11.0, 1], [10.5, 1]]}
        self.assertEqual(run.cpu_over(hist, {"m.A": [70.0, 200]}), [])  # one job per test: fixtures paid 200 times
        self.assertEqual([c for c, *_ in run.cpu_over(hist, {"m.A": [70.0, 1]})], ["m.A"])
        self.assertEqual([c for c, *_ in run.cpu_over({"m.A": [10.0, 11.0]}, {"m.A": [70.0, 200]})], ["m.A"])

    def test_a_broad_rise_names_the_classes_that_grew(self):
        hist = {f"m.C{i}": [10.0] for i in range(30)}
        now = {f"m.C{i}": 16.0 if i < 20 else 10.5 for i in range(30)}  # 425 s vs 300: 1.42x, under the run's 1.5x
        self.assertEqual(run.cpu_over(hist, now), [])
        now.update({f"m.C{i}": 19.0 for i in range(10)})  # 455 s vs 300: 1.52x and +155 s; no class at 2x
        got = run.cpu_over(hist, now)
        self.assertEqual(sorted(c for c, *_ in got), sorted(f"m.C{i}" for i in range(20)))  # the ones that grew
        self.assertIn("1.5x their usual", got[0][3])

    def test_the_floor_knob_scales_every_absolute_margin(self):
        os.environ["SAMPO_CPU_FLOOR_S"] = "0.2"
        self.assertEqual([c for c, *_ in run.cpu_over({"m.A": [0.1]}, {"m.A": 0.5, "m.N": 1.0})], ["m.A", "m.N"])


class Env(unittest.TestCase):
    def test_the_verify_records_without_gating_and_the_train_gates(self):
        from sampo import lanes
        p = mock.Mock(dir=os.path.join(os.sep, "x", "state"))
        with mock.patch.dict(os.environ, {"SAMPO_CPU_GATE": "1", "FOREMAN_SKIP_TESTS": "test_a"}):
            verify, gate = lanes.cpu_env(p, gate=False), lanes.cpu_env(p)
        self.assertEqual(verify["SAMPO_CPU_BASE"], os.path.join(os.sep, "x", "state", "test-cpu.json"))
        self.assertNotIn("SAMPO_CPU_GATE", verify)
        self.assertEqual(gate["SAMPO_CPU_GATE"], "1")
        self.assertNotIn("FOREMAN_SKIP_TESTS", gate)


class Baseline(unittest.TestCase):
    def path(self, data=None):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "test-cpu.json")
        if data is not None:
            with open(p, "w") as f:
                f.write(data if isinstance(data, str) else json.dumps(data))
        return p

    def test_green_runs_keep_the_last_nine_per_class_and_a_suite_estimate_per_day(self):
        p = self.path()
        for i in range(12):
            run.record_cpu(p, {"m.A": [float(i), 1], "m.B": 2.0})
        got = run.load_cpu(p)
        self.assertEqual(got["classes"]["m.A"], [[float(i), 1] for i in range(3, 12)])
        self.assertEqual(got["classes"]["m.B"][-1], [2.0, None])
        self.assertEqual(got["days"][time.strftime("%Y-%m-%d")], 9.0)  # A's median 7 + B's 2

    def test_a_class_unseen_for_two_weeks_leaves_the_estimate(self):
        p = self.path({"classes": {"m.Gone": [50.0]}, "seen": {"m.Gone": "2000-01-01"}, "days": {}})
        run.record_cpu(p, {"m.A": 3.0})
        got = run.load_cpu(p)
        self.assertNotIn("m.Gone", got["classes"])
        self.assertEqual(got["days"][time.strftime("%Y-%m-%d")], 3.0)

    def test_a_corrupt_file_is_an_empty_baseline(self):
        self.assertEqual(run.load_cpu(self.path("{nope")), {"classes": {}, "seen": {}, "days": {}})


class ForkedCalls(unittest.TestCase):
    def test_a_forked_calls_cpu_is_returned_to_the_caller(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        script = os.path.join(d, "burn.py")
        with open(script, "w") as f:
            f.write("import time\nend = time.process_time() + 0.3\nwhile time.process_time() < end:\n    pass\n")
        self.addCleanup(forkcli.stop)
        before = forkcli.SPENT[0]
        r = forkcli.run(script, [], cwd=d, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(forkcli._SERVER.get(os.getpid(), (None,))[0] or os.environ.get(forkcli.SHARED),
                        "the call went through a fork server")
        self.assertGreaterEqual(forkcli.SPENT[0] - before, 0.25)


BURN = ("import os, time, unittest\n"
        "class Burn(unittest.TestCase):\n    def test_it(self):\n"
        "        mark = os.path.join(os.environ['BURN_DIR'], 'ran')\n"
        "        secs = float(open(os.path.join(os.environ['BURN_DIR'], 'secs')).read())\n"
        "        if os.environ.get('BURN_ONCE') and os.path.exists(mark):\n            secs = 0.01\n"
        "        open(mark, 'a').close()\n"
        "        end = time.process_time() + secs\n        while time.process_time() < end:\n            pass\n")
QUICK = "import unittest\nclass Quick(unittest.TestCase):\n    def test_it(self):\n        pass\n"
ORPHAN = ("import os, subprocess, sys, time, unittest\n"
          "class Orphan(unittest.TestCase):\n    def test_it(self):\n"
          "        d = os.environ['BURN_DIR']\n"
          "        with open(os.path.join(d, 'g.py'), 'w') as f:\n"
          "            f.write('import os, time\\nend = time.process_time() + 0.5\\nwhile time.process_time() < end:\\n'\n"
          "                    '    pass\\nopen(os.path.join(os.path.dirname(__file__), \"done\"), \"w\").close()\\n')\n"
          "        subprocess.run(['sh', '-c', f'{sys.executable} {d}/g.py & exit 0'], check=True)  # the burner outlives sh\n"
          "        end = time.time() + 60\n"
          "        while not os.path.exists(os.path.join(d, 'done')) and time.time() < end:\n            time.sleep(0.05)\n")


class EndToEnd(unittest.TestCase):
    """The real run.py in a scratch repo, with the floor knob at 0.2 CPU s so a class burning under a second shows."""

    def setUp(self):
        top = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, top, True)
        self.repo = os.path.join(top, "repo")
        self.tests = os.path.join(self.repo, "plugin", "tests")
        os.makedirs(self.tests)
        for name in ("run.py", "runcover.py"):
            shutil.copy(os.path.join(run.HERE, name), os.path.join(self.tests, name))
        for name, text in (("test_burn.py", BURN), ("test_quick.py", QUICK), ("test_orphan.py", ORPHAN)):
            with open(os.path.join(self.tests, name), "w") as f:
                f.write(text)
        git = ["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.test"]
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        self.burn = os.path.join(top, "burn")
        os.makedirs(self.burn)
        self.base = os.path.join(top, "test-cpu.json")
        self.env = dict({k: v for k, v in os.environ.items() if k not in NESTED},
                        XDG_CACHE_HOME=os.path.join(top, "cache"), FOREMAN_SLOTS="0", BURN_DIR=self.burn,
                        FM_TEST_TIMES=os.path.join(top, "t.json"), SAMPO_CPU_FLOOR_S="0.2")

    def go(self, secs, *names, **env):
        with open(os.path.join(self.burn, "secs"), "w") as f:
            f.write(str(secs))
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(self.burn, "ran"))
        return subprocess.run([sys.executable, os.path.join(self.tests, "run.py"), *(names or ("test_burn", "test_quick"))],
                              capture_output=True, text=True, timeout=300, cwd=self.repo, env=dict(self.env, **env))

    def usual(self, secs):
        with open(self.base, "w") as f:
            json.dump({"classes": {"test_burn.Burn": [secs] * 3, "test_quick.Quick": [0.05] * 3}}, f)

    def test_the_last_line_gives_the_runs_cpu(self):
        r = self.go(0.3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stderr, r"Ran 2 tests in [\d.]+s \(.*; cpu [\d.]+ s\)")

    def test_a_gated_class_over_its_margin_fails_by_name_and_the_train_reads_it(self):
        self.usual(0.05)
        r = self.go(0.6, SAMPO_CPU_BASE=self.base, SAMPO_CPU_GATE="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("==== test_burn.Burn\nrun.py: CPU over its margin (T-3757):", r.stderr)
        self.assertIn("FAILED (1 classes: test_burn.Burn)", r.stderr)
        self.assertEqual(train._red(r.stderr), ["test_burn.Burn"])  # so the train's probes narrow to it
        self.assertEqual(run.load_cpu(self.base)["classes"]["test_burn.Burn"], [[0.05, None]] * 3)  # red: none recorded

    def test_ungated_it_warns_passes_and_records(self):
        self.usual(0.05)
        r = self.go(0.6, SAMPO_CPU_BASE=self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("run.py: CPU over its margin, not gated: test_burn.Burn", r.stderr)
        self.assertEqual(len(run.load_cpu(self.base)["classes"]["test_burn.Burn"]), 4)

    def test_a_one_off_spike_reruns_and_the_lower_count_stands(self):
        self.usual(0.05)
        r = self.go(0.6, SAMPO_CPU_BASE=self.base, SAMPO_CPU_GATE="1", BURN_ONCE="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(run.load_cpu(self.base)["classes"]["test_burn.Burn"][-1][0], 0.3)

    def test_a_class_the_cache_would_skip_still_runs_when_its_cached_cpu_is_over(self):
        r = self.go(0.6, "test_burn", "test_quick")  # green, ungated: cached at this code with its CPU
        self.assertEqual(r.returncode, 0, r.stderr)
        self.usual(0.05)  # a drawn class (1 in 10) runs anyway; either way it is measured and fails
        r = self.go(0.6, "test_burn", SAMPO_CPU_BASE=self.base, SAMPO_CPU_GATE="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("failed class: test_burn.Burn", r.stderr)

    def test_an_orphan_the_class_left_burning_counts_toward_it(self):
        r = self.go(0.0, "test_orphan", SAMPO_CPU_BASE=self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(run.load_cpu(self.base)["classes"]["test_orphan.Orphan"][-1][0], 0.4)

    def test_a_class_killed_by_a_signal_still_reads_as_killed(self):
        with open(os.path.join(self.tests, "test_dies.py"), "w") as f:
            f.write("import os, signal, unittest\nclass Dies(unittest.TestCase):\n    def test_it(self):\n"
                    "        os.kill(os.getpid(), signal.SIGKILL)\n")
        r = self.go(0.0, "test_dies", SAMPO_CPU_BASE=self.base)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("failed class: test_dies.Dies", r.stderr)
        self.assertNotIn("Traceback", r.stderr.split("==== test_dies.Dies")[-1].split("failed class")[0])
        with mock.patch.object(run, "CPU", {}), mock.patch.object(run, "HERE", self.tests):
            got = run._run_one("test_dies.Dies", [], 120, None, None)
        self.assertEqual(got[1], -9, got[2])  # run.py's kill code (T-3338's slow-class rule reads it), not 1

    def test_without_a_base_nothing_is_compared_or_recorded(self):
        self.usual(0.05)
        r = self.go(0.6)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("CPU over", r.stderr)
        self.assertEqual(len(run.load_cpu(self.base)["classes"]["test_burn.Burn"]), 3)


if __name__ == "__main__":
    unittest.main()
