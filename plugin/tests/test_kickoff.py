"""T-3291 (moon C-000930): sampo kickoff ID dry-runs a task's plan before its first edit. touch() lists per step the
existing files its scope globs and its step text name, with the def/class symbols grepped in them; collide() names the
other open tasks whose scope or steps overlap; report() adds the tests map.tests_for links and the gates cli.gates
names; the verb prints under 20 lines and edits nothing."""
# map-links: plugin/lib/sampo/kickoff.py
import json
import os
import subprocess

from helpers import ForemanTestCase
from sampo import core as c

first = lambda text: json.JSONDecoder().raw_decode(text)[0]

CALC = "def add(a, b):\n    return a + b\n\n\nclass Ledger:\n    pass\n"
OTHER = "def render(x):\n    return x\n"
SEED = ("seed", (("src/calc.py", CALC), ("src/other.py", OTHER), ("src/util.py", "def helper_zip():\n    pass\n"),
                 ("tests/test_calc.py", "from src import calc\n"), ("docs/a.md", "# a\n")))


class Kickoff(ForemanTestCase):
    repo_seed = SEED

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        # T-0001: the task being planned; T-0002 shares its file and a symbol; T-0003 is elsewhere; T-0004 is dropped
        self.fm("task", "new", "plan calc", "--type", "FEATURE", "--tier", "M", "--scope", "src/calc.py",
                "--scope", "src/fresh.py", "--step", "edit `add` in src/calc.py and class Ledger",
                "--step", "create src/fresh.py with def brand_new", "--step", "write the notes")
        self.fm("task", "new", "rival calc", "--type", "FEATURE", "--tier", "S", "--scope", "src/*.py",
                "--step", "rework class Ledger in src/calc.py")
        self.fm("task", "new", "docs only", "--type", "FEATURE", "--tier", "S", "--scope", "docs/**",
                "--step", "reword docs/a.md")
        self.fm("task", "new", "dropped calc", "--type", "FEATURE", "--tier", "S", "--scope", "src/calc.py",
                "--step", "edit class Ledger in src/calc.py")
        self.fm("task", "drop", "T-0004", "--reason", "not needed")
        self.b = c.find_brief(self.p, "T-0001")

    def test_touch_lists_per_step_the_existing_files_and_symbols_its_scope_and_text_name(self):
        from sampo import kickoff
        steps = kickoff.touch(self.p, self.b)
        self.assertEqual([s["n"] for s in steps], [1, 2, 3])
        one, two, three = steps
        self.assertEqual(one["files"], ["src/calc.py"])
        self.assertEqual(one["symbols"], {"src/calc.py": ["Ledger", "add"]})
        self.assertEqual(two["files"], [])  # src/fresh.py doesn't exist yet: it is created, not touched
        self.assertEqual(two["creates"], ["src/fresh.py"])
        self.assertEqual(three["files"], [])  # names nothing: the report falls back on the scope
        self.assertEqual(three["symbols"], {})

    def test_a_path_outside_the_tree_or_an_escape_is_not_a_file_of_the_step(self):
        from sampo import kickoff
        b = c.find_brief(self.p, "T-0003")
        b.set_section("Steps", "1. [ ] read ../../etc/passwd and /etc/hosts and docs/a.md\n")
        self.assertEqual(kickoff.touch(self.p, b)[0]["files"], ["docs/a.md"])

    def test_collide_names_open_tasks_sharing_a_file_or_symbol_and_skips_closed_and_disjoint_ones(self):
        from sampo import kickoff
        rows = kickoff.collide(self.p, self.b)
        self.assertEqual([r["id"] for r in rows], ["T-0002"])
        self.assertIn("src/calc.py", rows[0]["files"])
        self.assertIn("Ledger", rows[0]["symbols"])

    def test_report_holds_tests_gates_and_a_verdict_and_skips_a_missing_gatemap_silently(self):
        from unittest import mock
        from sampo import kickoff
        r = kickoff.report(self.p, self.b)
        self.assertEqual(r["tests"], ["tests/test_calc.py"])
        self.assertIn("lesson", [g["gate"] for g in r["gates"]])  # an M feature owes a lesson and docs impact
        self.assertIn("T-0002", r["verdict"])
        with mock.patch.dict("sys.modules", {"sampo.gatemap": None}):  # not importable: left out, no error
            r = kickoff.report(self.p, self.b)
        self.assertEqual(r["predicted"], [])

    def test_the_verb_prints_under_20_lines_edits_nothing_and_speaks_json(self):
        before = self.fm("task", "show", "T-0001", "--full").stdout
        files = subprocess.run(["git", "-C", self.repo, "status", "--porcelain"], capture_output=True, text=True).stdout
        out = self.fm("kickoff", "T-0001").stdout
        self.assertLess(len(out.strip().splitlines()), 20, out)
        for word in ("src/calc.py", "tests/test_calc.py", "T-0002"):
            self.assertIn(word, out)
        self.assertEqual(self.fm("task", "show", "T-0001", "--full").stdout, before)
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "status", "--porcelain"], capture_output=True,
                                        text=True).stdout, files)
        data = first(self.fm("kickoff", "T-0001", "--json").stdout)
        self.assertEqual(sorted(data), sorted(["id", "steps", "tests", "gates", "collisions", "predicted", "preview",
                                               "verdict", "capped"]))

    def test_a_lane_preview_is_used_when_the_lanes_module_has_one_and_a_failing_one_is_skipped(self):
        from unittest import mock
        from sampo import kickoff, lanes
        with mock.patch.object(lanes, "preview", create=True, new=lambda p, b: [f"lane for {b.id}"]):
            self.assertEqual(kickoff.report(self.p, self.b)["preview"], ["lane for T-0001"])
            self.assertIn("lanes: lane for T-0001", "\n".join(kickoff.lines(kickoff.report(self.p, self.b))))
        with mock.patch.object(lanes, "preview", create=True, new=lambda p, b: 1 / 0):
            self.assertEqual(kickoff.report(self.p, self.b)["preview"], [])

    def test_a_wide_plan_still_fits_20_lines(self):
        from sampo import kickoff
        r = kickoff.report(self.p, self.b)
        r["steps"] = r["steps"] * 5
        r["collisions"] = r["collisions"] * 6
        r["preview"] = ["a", "b"]
        self.assertLess(len(kickoff.lines(r)), 20)

    def test_an_unknown_task_is_refused_and_a_clear_task_says_so(self):
        self.assertNotEqual(self.fm("kickoff", "T-9999", check=False).returncode, 0)
        out = self.fm("kickoff", "T-0003").stdout
        self.assertIn("clear", out.splitlines()[-1])

    def test_a_long_step_keeps_its_files_on_the_screen(self):
        from sampo import kickoff
        b = c.find_brief(self.p, "T-0003")
        b.set_section("Steps", "1. [ ] " + "reword everything in the page " * 7 + "docs/a.md\n")
        out = "\n".join(kickoff.lines(kickoff.report(self.p, b)))
        self.assertIn("docs/a.md", out)

    def test_a_file_a_step_names_is_read_for_symbols_even_after_the_scan_cap_is_spent(self):
        from unittest import mock
        from sampo import kickoff
        b = c.find_brief(self.p, "T-0003")
        b.meta["scope"] = ["src/**"]
        b.set_section("Steps", "1. [ ] edit `add`\n2. [ ] change `render` in src/other.py\n")
        with mock.patch.object(kickoff, "MAX_SCAN", 1):
            r = kickoff.report(self.p, b)
        self.assertEqual(r["steps"][1]["symbols"], {"src/other.py": ["render"]})
        self.assertTrue(r["capped"])
        self.assertIn("scan capped", "\n".join(kickoff.lines(r)))

    def test_a_shared_word_no_file_defines_is_not_a_collision(self):
        from sampo import kickoff
        a, o = c.find_brief(self.p, "T-0003"), c.find_brief(self.p, "T-0002")
        a.meta["scope"], o.meta["scope"] = ["docs/a.md"], ["src/other.py"]
        a.set_section("Steps", "1. [ ] mention `git` and read_only in docs/a.md\n")
        o.set_section("Steps", "1. [ ] mention `git` and read_only in src/other.py\n")
        self.assertEqual(kickoff.collide(self.p, a, briefs=[o]), [])
        a.set_section("Steps", "1. [ ] call `render` in docs/a.md\n")
        o.set_section("Steps", "1. [ ] touch `render` in src/other.py\n")  # a file of its claim defines it: a collision
        self.assertEqual([r["symbols"] for r in kickoff.collide(self.p, a, briefs=[o])], [["render"]])

    def test_many_open_tasks_with_one_glob_scan_the_tree_once(self):
        from unittest import mock
        from sampo import kickoff
        briefs = []
        for i in range(20):
            o = c.find_brief(self.p, "T-0002")
            o.meta["id"] = f"T-9{i:03d}"
            briefs.append(o)
        calls = []
        real = c.glob_match
        with mock.patch.object(c, "glob_match", lambda f, g: calls.append(1) or real(f, g)):
            kickoff.collide(self.p, self.b, briefs=briefs)
        self.assertLess(len(calls), 40, len(calls))

    def test_a_control_sequence_in_a_scope_never_reaches_the_screen(self):
        from sampo import kickoff
        o = c.find_brief(self.p, "T-0002")
        o.meta["scope"] = ["\x1b]0;x\x07/a.py"]
        self.b.meta["scope"] = ["\x1b]0;x\x07/a.py"]
        rows = kickoff.collide(self.p, self.b, briefs=[o])
        self.assertTrue(rows)
        for f in rows[0]["files"]:
            self.assertTrue(f.isprintable(), repr(f))

    def test_a_failing_lane_preview_says_so_on_stderr(self):
        import contextlib
        import io
        from unittest import mock
        from sampo import kickoff, lanes
        err = io.StringIO()
        with mock.patch.object(lanes, "preview", create=True, new=lambda p, b: 1 / 0), contextlib.redirect_stderr(err):
            self.assertEqual(kickoff.report(self.p, self.b)["preview"], [])
        self.assertIn("kickoff: lanes.preview failed", err.getvalue())

    def test_an_empty_tree_is_nothing_to_compare_never_clear(self):
        from unittest import mock
        from sampo import kickoff
        b = c.find_brief(self.p, "T-0003")
        with mock.patch.object(kickoff, "_tree", return_value=kickoff._Tree([])):
            r = kickoff.report(self.p, b)
        self.assertEqual(r["collisions"], [])
        self.assertNotIn("clear", r["verdict"])
        self.assertIn("nothing to compare: no tracked files found", r["verdict"])
        self.assertIn("nothing to compare", kickoff.lines(r)[-1])

    def test_a_scope_glob_wider_than_the_scan_cap_stays_capped_in_collide_and_says_so(self):
        from unittest import mock
        from sampo import kickoff
        a, o = c.find_brief(self.p, "T-0003"), c.find_brief(self.p, "T-0002")
        a.meta["scope"], o.meta["scope"] = ["src/**"], ["src/**"]  # three files, a cap of one
        a.set_section("Steps", "1. [ ] call `render`\n")
        o.set_section("Steps", "1. [ ] touch `render`\n")
        tree, defs = kickoff._tree(self.p), kickoff._Defs(self.p.root)
        with mock.patch.object(kickoff, "MAX_SCAN", 1):
            kickoff.collide(self.p, a, tree, [o], defs)
            self.assertTrue(defs.capped)
            self.assertLessEqual(len(defs.seen), 1)  # no scoped file past the cap was read
            r = kickoff.report(self.p, a)
        self.assertTrue(r["capped"])
        self.assertIn("scan capped", "\n".join(kickoff.lines(r)))

    def test_a_task_with_no_steps_and_no_scope_still_reports(self):
        self.fm("task", "new", "bare", "--type", "FEATURE", "--tier", "S")
        out = self.fm("kickoff", "T-0005").stdout
        self.assertIn("no steps", out)
