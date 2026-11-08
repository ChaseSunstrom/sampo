"""T-3280: `fm lane race T --score` also reports each side's import time: the sum of each changed plugin/lib module's
median of three imports, each import in a fresh interpreter, with how many modules it covers. It is evidence only: the
ranking never reads it."""
import json
import os
import subprocess

from helpers import ForemanTestCase

from sampo import racemetrics


class RaceMetrics(ForemanTestCase):
    def write(self, root, rel, text):
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as h:
            h.write(text)

    def test_a_module_that_sleeps_at_import_measures_slower_than_a_trivial_one(self):
        root = os.path.join(self.tmp, "tree")
        self.write(root, "plugin/lib/slowmod.py", "import time\ntime.sleep(0.3)\n")
        self.write(root, "plugin/lib/quickmod.py", "X = 1\n")
        slow, n_slow, why = racemetrics.import_ms(root, ["plugin/lib/slowmod.py"])
        quick, n_quick, _ = racemetrics.import_ms(root, ["plugin/lib/quickmod.py"])
        self.assertIsInstance(slow, int)
        self.assertEqual((n_slow, n_quick, why), (1, 1, ""))
        self.assertGreaterEqual(slow, 200, "a 0.3 s sleep at import shows as hundreds of ms")
        self.assertLess(quick, slow)

    def test_the_time_is_the_sum_over_the_changed_modules_and_the_count_says_how_many(self):
        root = os.path.join(self.tmp, "tree")
        self.write(root, "plugin/lib/one.py", "import time\ntime.sleep(0.3)\n")
        for name in ("a", "b", "c"):  # the mixed case: three trivial modules are not slower than one sleeping one
            self.write(root, f"plugin/lib/{name}.py", "X = 1\n")
        sleeper, n_one, _ = racemetrics.import_ms(root, ["plugin/lib/one.py"])
        trio, n_trio, why = racemetrics.import_ms(root, ["plugin/lib/a.py", "plugin/lib/b.py", "plugin/lib/c.py"])
        self.assertEqual((n_one, n_trio, why), (1, 3, ""))
        self.assertLess(trio, sleeper, "three trivial modules sum to less than one sleeping module")

    def test_only_plugin_lib_python_counts_and_nothing_changed_measures_nothing(self):
        root = os.path.join(self.tmp, "tree")
        self.write(root, "plugin/tests/test_x.py", "X = 1\n")  # a test file is not a module the product imports
        self.write(root, "docs/notes.md", "# notes\n")
        self.assertEqual(racemetrics.import_ms(root, ["plugin/tests/test_x.py", "docs/notes.md"]),
                         (None, 0, "no plugin/lib module changed"))
        self.assertEqual(racemetrics.import_ms(root, [])[0], None)

    def test_a_changed_module_that_fails_to_import_or_has_a_hyphen_in_its_name_makes_the_side_unmeasured(self):
        root = os.path.join(self.tmp, "tree")
        self.write(root, "plugin/lib/broken.py", "def (:\n")
        self.write(root, "plugin/lib/bad-name.py", "X = 1\n")  # a hyphen is not a module name: skipped, not run
        self.write(root, "plugin/lib/good.py", "X = 1\n")
        ms, n, why = racemetrics.import_ms(root, ["plugin/lib/broken.py", "plugin/lib/good.py"])
        self.assertIsNone(ms)
        self.assertEqual(n, 2)
        self.assertIn("broken.py", why, "the reason names the module that couldn't be measured")
        self.assertIsNone(racemetrics.import_ms(root, ["plugin/lib/bad-name.py"])[0])
        self.assertFalse(os.path.exists(os.path.join(root, "plugin", "lib", "__pycache__")), "no bytecode written into the tree")

    def test_a_deleted_changed_module_makes_the_side_unmeasured_not_a_smaller_number(self):
        root = os.path.join(self.tmp, "tree")
        self.write(root, "plugin/lib/kept.py", "X = 1\n")
        ms, n, why = racemetrics.import_ms(root, ["plugin/lib/kept.py", "plugin/lib/gone.py"])
        self.assertIsNone(ms)
        self.assertIn("gone.py", why)

    def test_a_changed_module_named_like_a_stdlib_module_the_probe_already_loads_is_unmeasured_not_near_zero(self):
        root = os.path.join(self.tmp, "tree")
        self.write(root, "plugin/lib/time.py", "import time\ntime.sleep(0.3)\n")  # the probe loads time first: a cached import
        ms, n, why = racemetrics.import_ms(root, ["plugin/lib/time.py"])
        self.assertIsNone(ms, "a cached stdlib import would read as ~0 ms, a false fast")
        self.assertIn("time.py", why)

    def test_more_than_eight_changed_modules_is_too_many_to_measure(self):
        root = os.path.join(self.tmp, "tree")
        files = [f"plugin/lib/m{i}.py" for i in range(9)]
        for rel in files:
            self.write(root, rel, "X = 1\n")
        ms, n, why = racemetrics.import_ms(root, files)
        self.assertEqual((ms, n), (None, 9))
        self.assertIn("too many modules", why)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", *args],
                              capture_output=True, text=True, check=True).stdout

    def task(self):
        return json.loads(self.fm("task", "new", "Make it load", "--type", "FEATURE", "--tier", "S", "--ac",
                                  "it loads :: test -f win.txt", "--step", "s", "--json").stdout)["id"]

    def side(self, tid, name, files):
        """What a builder does: its own worktree, fm focus there, a committed change."""
        path = os.path.join(self.tmp, f"wt-{name}")
        self.git(self.repo, "worktree", "add", "-q", "-b", f"race-{name}", path, "HEAD")
        self.fm("focus", tid, cwd=path)
        for rel, text in files.items():
            self.write(path, rel, text)
        self.git(path, "add", "-A")
        self.git(path, "commit", "-qm", f"{tid}: side {name}")
        return path

    def test_race_score_prints_each_sides_import_time_and_module_count_and_the_ranking_keeps_its_order(self):
        self.fm("init")
        tid = self.task()
        other = json.loads(self.fm("lane", "race", tid, "--json").stdout)["race"]
        # the original's side: 3 lines, one module that sleeps at import; the other: 5 lines, a trivial module
        self.side(tid, "slow", {"win.txt": "ok\n", "plugin/lib/raceslow.py": "import time\ntime.sleep(0.3)\n"})
        self.side(other, "fast", {"win.txt": "ok\n", "plugin/lib/racefast.py": "X = 1\n", "extra.txt": "1\n2\n3\n"})
        r = self.fm("lane", "race", tid, "--score")
        self.assertRegex(r.stdout, r"import \d+ ms over 1 module", "the score line names each side's import time")
        self.assertIn("timing imports", r.stderr, "the imports are announced before their interpreters start")
        d = json.loads(self.fm("lane", "race", tid, "--score", "--json").stdout)
        rows = {r["id"]: r for r in d["sides"]}
        self.assertGreaterEqual(rows[tid]["import_ms"], 200)
        self.assertLess(rows[other]["import_ms"], rows[tid]["import_ms"])
        self.assertEqual((rows[tid]["import_modules"], rows[other]["import_modules"]), (1, 1))
        self.assertEqual(d["leader"], tid, "fewer lines still leads: the import time is evidence, not a rank key")

    def test_a_side_with_too_many_changed_modules_says_so_on_its_score_line(self):
        self.fm("init")
        tid = self.task()
        other = json.loads(self.fm("lane", "race", tid, "--json").stdout)["race"]
        self.side(tid, "few", {"win.txt": "ok\n", "plugin/lib/racefew.py": "X = 1\n"})
        self.side(other, "many", {"win.txt": "ok\n", **{f"plugin/lib/racem{i}.py": "X = 1\n" for i in range(9)}})
        out = self.fm("lane", "race", tid, "--score").stdout
        self.assertIn("import not measured: too many modules", out)
        self.assertRegex(out, r"import \d+ ms over 1 module")

    def test_a_side_that_changes_no_plugin_lib_module_shows_no_import_clause(self):
        self.fm("init")
        tid = self.task()
        other = json.loads(self.fm("lane", "race", tid, "--json").stdout)["race"]
        self.side(tid, "docs", {"win.txt": "ok\n", "notes.md": "# notes\n"})
        self.side(other, "docs2", {"win.txt": "ok\n", "notes.md": "# more\n"})
        out = self.fm("lane", "race", tid, "--score").stdout
        self.assertNotIn("import", out, "a side with no changed plugin/lib module has nothing to time")
        self.assertNotIn("timing imports", self.fm("lane", "race", tid, "--score").stderr)
