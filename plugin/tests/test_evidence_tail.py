"""T-4564: a task's evidence record keeps the last lines of a long test run (the Ran line and OK or FAILED with the failed
classes), head and tail when it is cut, and a run served whole from the test cache says how many tests were green there."""
import json
import os

from helpers import ForemanTestCase


def _lib():
    import sys
    lib = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from sampo import core
    return core


CACHED_RAN = ("Ran 0 tests in 0.0s (0 jobs, 4 at a time; slowest: none; cpu 0.1 s); 2 more green in the cache\n\nOK\n")


class RunResult(ForemanTestCase):
    def test_a_long_green_run_keeps_its_ran_line_and_its_ok(self):
        c = _lib()
        noise = "".join(f"test_module_{i}.py::test_case_{i} PASSED with a long descriptive line of output\n"
                        for i in range(60))
        slowest = ", ".join(f"test_package.test_module_{i}.TestClass{i}.test_method_name {9.5 - i / 10:.1f}s"
                            for i in range(3))
        ran = (f"Ran 1234 tests in 312.4s (420 jobs, 4 at a time; slowest: {slowest}; cpu 1234.5 s; waited 12.0s); "
               f"245 more green in the cache; joined 3 (120 tests green in a run beside this one)")
        res = c.run_result(0, noise + ran + "\n\nOK\n")
        self.assertTrue(res.startswith("exit 0 · "), res)
        self.assertIn("245 more green in the cache", res)
        self.assertTrue(res.endswith(" / OK"), res)
        self.assertLessEqual(len(res), 700, "the record stays a line of bounded size")

    def test_a_long_failed_run_keeps_the_failed_classes(self):
        c = _lib()
        names = ", ".join(f"test_package.test_module_{i}.TestClass{i}" for i in range(5))
        slowest = ", ".join(f"test_package.test_module_{i}.TestClass{i}.test_method_name {9.5 - i / 10:.1f}s"
                            for i in range(5))
        ran = (f"Ran 3210 tests in 900.0s (640 jobs, 4 at a time; slowest: {slowest}; cpu 2000.0 s); "
               f"10 more green in the cache")
        out = "  failed class: test_a.A\n" * 12 + ran + f"\n\nFAILED (12 classes: {names}, +7 more)\n"
        res = c.run_result(1, out)
        self.assertTrue(res.startswith("✗ exit 1 · "), res)
        self.assertTrue(res.endswith(f"FAILED (12 classes: {names}, +7 more)"), res)
        self.assertIn("…", res, "a cut record shows where it was cut")

    def test_a_short_run_is_recorded_whole(self):
        c = _lib()
        self.assertEqual(c.run_result(0, "Ran 1 tests in 0.1s\n\nOK\n"), "exit 0 · Ran 1 tests in 0.1s / OK")
        self.assertEqual(c.run_result(1, "AssertionError: 1 != 2"), "✗ exit 1 · AssertionError: 1 != 2")
        self.assertEqual(c.run_result(0, ""), "exit 0 · (no output)")

    def test_a_secret_in_the_kept_lines_is_redacted_before_the_cut(self):
        c = _lib()
        res = c.run_result(1, "x" * 400 + "\ntoken ghp_" + "A" * 36 + "\nFAILED (1 classes: test_a.A)")
        self.assertNotIn("ghp_", res)
        self.assertIn("[REDACTED]", res)
        self.assertTrue(res.endswith("FAILED (1 classes: test_a.A)"), res)

    def test_a_run_served_whole_from_the_cache_says_how_many_were_green(self):
        c = _lib()
        res = c.run_result(0, CACHED_RAN)
        self.assertIn("2 more green in the cache", res)
        self.assertTrue(res.endswith(" / OK"), res)
        self.assertIsNone(c._NO_TEST.search(res), "a green run of cached classes is not a run of nothing")
        self.assertIsNone(c._NO_TEST.search("Ran 0 tests in 0.0s; 2 more green in the cache"))
        self.assertIsNotNone(c._NO_TEST.search("Ran 0 tests in 0.0s\n\nNO TESTS RAN"), "a run of nothing still says so")

    def test_the_cached_count_survives_a_long_ran_line(self):
        c = _lib()
        slowest = ", ".join(f"test_package.test_module_{i}.TestClass{i}.test_method_name {9.5 - i / 10:.1f}s"
                            for i in range(3))
        ran = (f"Ran 0 tests in 0.0s (0 jobs, 4 at a time; slowest: {slowest}; cpu 0.1 s); 1234 more green in the cache; "
               f"joined 9 (900 tests green in a run beside this one)")
        res = c.run_result(0, ran + "\n\nOK\n")
        self.assertIsNone(c._NO_TEST.search(res), res)
        self.assertIn("1234 more green", res)


class EvidenceRecord(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = json.loads(self.fm("task", "new", "Long run", "--type", "FIX", "--tier", "S",
                                      "--ac", "it runs :: true", "--step", "run it", "--json").stdout)["id"]
        self.fm("focus", self.tid)

    def _script(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def test_a_long_failed_run_keeps_its_last_lines_in_the_record_that_task_show_prints(self):
        self._script("long_run.py", "import sys\n"
                     "for i in range(400):\n"
                     "    print(f'test_case_{i} ... ok with a long descriptive line of output')\n"
                     "print('  failed class: test_a.A')\n"
                     "print('  failed class: test_b.B')\n"
                     "slowest = ', '.join(f'test_package.test_module_{i}.TestClass{i}.test_method_name {9 - i}.0s' for i in range(4))\n"
                     "print(f'Ran 402 tests in 9.9s (400 jobs, 4 at a time; slowest: {slowest}; cpu 99.0 s); 77 more green in the cache')\n"
                     "print()\n"
                     "print('FAILED (2 classes: test_a.A, test_b.B)')\n"
                     "sys.exit(1)\n")
        r = self.fm("task", "evidence", self.tid, "--step", "1", "--run", "python3 long_run.py", check=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        shown = self.fm("task", "show", self.tid).stdout
        record = [ln for ln in shown.splitlines() if "long_run.py" in ln and "→" in ln]
        self.assertEqual(len(record), 1, shown)
        self.assertIn("Ran 402 tests in 9.9s", record[0])
        self.assertIn("77 more green in the cache", record[0])
        self.assertIn("FAILED (2 classes: test_a.A, test_b.B)", record[0])

    def test_a_green_run_served_from_the_cache_keeps_the_count_in_the_record(self):
        self._script("cached_run.py", "print('Ran 0 tests in 0.0s (0 jobs, 4 at a time; cpu 0.1 s); 3 more green in the cache')\n"
                     "print()\n"
                     "print('OK')\n")
        r = self.fm("task", "evidence", self.tid, "--step", "1", "--run", "python3 cached_run.py", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        shown = self.fm("task", "show", self.tid).stdout
        record = [ln for ln in shown.splitlines() if "cached_run.py" in ln and "→" in ln]
        self.assertEqual(len(record), 1, shown)
        self.assertIn("3 more green in the cache", record[0])
        self.assertIn("/ OK", record[0])
