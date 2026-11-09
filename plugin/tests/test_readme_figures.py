"""T-3247: demo/readme_figures.py draws every README table and four measured charts as light and dark SVGs."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ForemanTestCase, headless_chrome

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "demo", "readme_figures.py")
FIGS = os.path.join(REPO, "demo", "figures")
sys.path.insert(0, os.path.join(REPO, "plugin", "lib"))


def load():
    spec = importlib.util.spec_from_file_location("readme_figures", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Tables(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.rf = load()
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            self.readme = f.read()

    def test_every_table_has_a_figure_in_both_themes(self):
        tables = self.rf.tables(self.readme)
        self.assertGreaterEqual(len(tables), 8)
        for t in tables:
            for theme in ("dark", "light"):
                path = os.path.join(FIGS, f"{t['slug']}-{theme}.svg")
                self.assertTrue(os.path.isfile(path), path)
                self.assertIn(f'srcset="demo/figures/{t["slug"]}-{theme}.svg"', self.readme, t["slug"])

    def test_cells_drop_markdown_and_escape(self):
        runs = self.rf.inline("**Bold** `sampo do` [a link](x.md) & <b>")
        text = "".join(r[0] for r in runs)
        self.assertEqual(text, "Bold sampo do a link & <b>")
        self.assertEqual([r[1] for r in runs if r[0].strip()][:3], ["b", "code", "link"])
        svg = self.rf.table_svg({"slug": "t", "head": ["A", "B"], "rows": [["x & y", "<z>"]]}, "dark")
        self.assertIn("x &amp; y", svg)
        self.assertIn("&lt;z&gt;", svg)

    def test_long_cells_wrap_inside_their_column(self):
        long = "word " * 80
        lines = self.rf.wrap(self.rf.inline(long), 300, 14)
        self.assertGreater(len(lines), 3)
        for line in lines:
            self.assertLessEqual(self.rf.width(line, 14), 300)

    def test_wide_glyphs_are_estimated_wide(self):  # review: a row of dashes overran its column
        self.assertGreaterEqual(self.rf.width([("—" * 40, "")], 14), 40 * 14 * 0.9)
        self.assertGreaterEqual(self.rf.width([("漢" * 10, "")], 14), 10 * 14 * 0.95)

    def test_a_hand_edited_picture_never_eats_the_text_above(self):
        text = ("# Top\n\nkeep this line\n\n## Area\n\n<p align=center><picture>\n  <img src=x.svg>\n</picture></p>\n"
                + self.rf.SUMMARY + "\n\n| A | B |\n|---|---|\n| a | b |\n\n</details>\n")
        got = self.rf.wrapped(text)
        self.assertIn("keep this line", got)
        self.assertIn("# Top", got)

    def test_figures_pass_the_source_gates(self):
        from sampo import works
        names = sorted(n for n in os.listdir(FIGS) if n.endswith(".svg"))
        self.assertTrue(names)
        for n in names:
            with open(os.path.join(FIGS, n), "rb") as f:
                got = works.inspect(f.read(), "card")
            bad = [g for g in got["gates"] if g["ok"] is False]
            self.assertEqual(bad, [], n)


@unittest.skipUnless(headless_chrome(), "no headless Chromium here")
class Rendered(ForemanTestCase):
    def test_every_figure_passes_works_check_as_a_figure(self):
        names = sorted(os.path.join(FIGS, n) for n in os.listdir(FIGS) if n.endswith(".svg"))
        got = subprocess.run([sys.executable, os.path.join(REPO, "plugin", "bin", "sampo"), "works", "check",
                              "--type", "figure", *names], capture_output=True, text=True, timeout=900)
        self.assertEqual(got.returncode, 0, got.stdout[-3000:] + got.stderr[-2000:])


class Charts(ForemanTestCase):
    def test_four_charts_from_measured_data(self):
        with open(os.path.join(FIGS, "data.json"), encoding="utf-8") as f:
            data = json.load(f)
        charts = data["charts"]
        self.assertGreaterEqual(len(charts), 4)
        for name, c in charts.items():
            self.assertRegex(c["measured"], r"^\d{4}-\d\d-\d\d", name)
            self.assertGreaterEqual(len(c["points"]), 2, name)
            for theme in ("dark", "light"):
                with open(os.path.join(FIGS, f"chart-{name}-{theme}.svg"), encoding="utf-8") as f:
                    svg = f.read()
                self.assertIn(c["measured"][:10], svg, name)
                self.assertIn('role="img"', svg, name)


class Refresh(ForemanTestCase):
    """T-3956: --refresh names a failure and exits 1 instead of a traceback."""

    def setUp(self):
        super().setUp()
        self.rf = load()
        self.root = tempfile.mkdtemp(dir=self.tmp)
        os.makedirs(os.path.join(self.root, "research", "models"))
        os.makedirs(os.path.join(self.root, "plugin", "tests"))
        with open(os.path.join(self.root, "README.md"), "w") as f:
            f.write("# T\n")

    def refresh(self, chart):
        return subprocess.run([sys.executable, SCRIPT, "--root", self.root, "--refresh", chart],
                              capture_output=True, text=True, timeout=120)

    def test_no_backend_at_500_names_the_failure(self):
        with open(os.path.join(self.root, "research", "models", "bench.jsonl"), "w") as f:
            f.write(json.dumps({"arm": "loo", "use": "intake-type", "accuracy": 0.5, "n": 10, "backend": "x",
                                "run": "r", "at": "2026-11-01T00:00Z"}) + "\n")
        got = self.refresh("models")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("500", got.stderr)
        self.assertNotIn("Traceback", got.stderr)

    def test_a_failed_bench_names_the_failure(self):
        with open(os.path.join(self.root, "plugin", "tests", "bench_hooks.py"), "w") as f:
            f.write("import sys\nsys.exit(3)\n")
        got = self.refresh("hooks")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("bench_hooks", got.stderr)
        self.assertNotIn("Traceback", got.stderr)

    def test_empty_points_name_the_failure(self):
        subprocess.run(["git", "init", "-q", self.root], check=True)  # no commits: nothing to count
        got = self.refresh("tests")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("git log failed", got.stderr)
        self.assertNotIn("Traceback", got.stderr)

    def test_the_per_day_window_ends_yesterday(self):
        import datetime
        from unittest import mock
        seen = []
        with mock.patch.object(self.rf, "measure_closed", lambda root, end: seen.append(end) or
                               {"kind": "columns", "sub": "s", "points": [["2026-01-01", 1]]}):
            self.rf.refresh(self.root, None, ["closed"])
        self.assertEqual(seen, [datetime.date.today() - datetime.timedelta(days=1)])


class Names(ForemanTestCase):
    def test_models_chart_and_hooks_claim_read_plainly(self):
        with open(os.path.join(FIGS, "data.json"), encoding="utf-8") as f:
            charts = json.load(f)["charts"]
        for label, *_ in charts["models"]["points"]:
            self.assertNotRegex(label, r"[:_]|^jeff$|^jeff-", label)  # no raw backend id
        self.assertEqual(len({p[0] for p in charts["models"]["points"]}), len(charts["models"]["points"]))
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            text = f.read()
        hooks = text.index("chart-hooks-dark.svg")
        self.assertEqual(text.count("<details>", 0, hooks), text.count("</details>", 0, hooks))  # not inside one
        self.assertNotIn("about 20 ms", text)
        self.assertIn("p95", text[text.rindex("hooks tank", 0, hooks):hooks])


class Check(ForemanTestCase):
    def copy(self):
        root = tempfile.mkdtemp(dir=self.tmp)
        shutil.copy(os.path.join(REPO, "README.md"), root)
        os.makedirs(os.path.join(root, "demo"))
        shutil.copytree(FIGS, os.path.join(root, "demo", "figures"))
        return root

    def run_check(self, root):
        return subprocess.run([sys.executable, SCRIPT, "--root", root, "--check"], capture_output=True, text=True,
                              timeout=120)

    def test_check_passes_when_current_and_names_stale_figures(self):
        root = self.copy()
        got = self.run_check(root)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        path = os.path.join(root, "README.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("| **Deletes** |", "| **Deletes, edited** |", 1))
        got = self.run_check(root)
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        self.assertIn("the-ward", got.stdout)

    def test_orphans_missing_data_and_unknown_pictures_are_stale(self):
        root = self.copy()
        figs = os.path.join(root, "demo", "figures")
        with open(os.path.join(figs, "old-table-dark.svg"), "w") as f:
            f.write("<svg/>")
        got = self.run_check(root)
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("old-table-dark.svg", got.stdout)
        os.remove(os.path.join(figs, "old-table-dark.svg"))
        os.remove(os.path.join(figs, "data.json"))
        got = self.run_check(root)
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("data.json", got.stdout)

    def test_a_new_table_gets_its_picture(self):
        root = self.copy()
        path = os.path.join(root, "README.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n## Extra bit\n\n| A | B |\n|---|---|\n| one | two |\n")
        got = subprocess.run([sys.executable, SCRIPT, "--root", root], capture_output=True, text=True, timeout=120)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('srcset="demo/figures/extra-bit-dark.svg"', text)
        self.assertTrue(os.path.isfile(os.path.join(root, "demo", "figures", "extra-bit-light.svg")))
        again = subprocess.run([sys.executable, SCRIPT, "--root", root], capture_output=True, text=True, timeout=120)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)  # idempotent: a wrapped table isn't wrapped twice
        self.assertEqual(self.run_check(root).returncode, 0, again.stdout)


if __name__ == "__main__":
    unittest.main()
