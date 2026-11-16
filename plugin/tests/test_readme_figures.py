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


class BoardLook(ForemanTestCase):
    """T-4771: a table is drawn as one of the Board's numbered panels, in the Board's (and the mod's) tokens."""

    def setUp(self):
        super().setUp()
        self.rf = load()
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            self.tables = self.rf.tables(f.read())
        with open(os.path.join(REPO, "plugin", "web", "live", "board-tokens.json"), encoding="utf-8") as f:
            self.tokens = json.load(f)

    def svg(self, theme, rows, head=("Area", "What"), number=3):
        return self.rf.table_svg({"slug": "t", "heading": "The ward", "head": list(head), "rows": rows}, theme,
                                 number=number)

    def test_every_table_figure_uses_only_the_boards_tokens(self):
        import re
        for t in self.tables:
            for theme in ("dark", "light"):
                with open(os.path.join(FIGS, f"{t['slug']}-{theme}.svg"), encoding="utf-8") as f:
                    svg = f.read()
                used = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", svg)}
                palette = {v.lower() for v in self.tokens[theme].values()}
                self.assertLessEqual(used, palette, f"{t['slug']}-{theme}: {sorted(used - palette)}")
                self.assertIn(self.tokens[theme]["bracket"].lower(), used, t["slug"])

    def test_each_table_is_numbered_in_readme_order_and_titled_by_its_heading(self):
        for i, t in enumerate(self.tables, 1):
            with open(os.path.join(FIGS, f"{t['slug']}-dark.svg"), encoding="utf-8") as f:
                svg = f.read()
            self.assertIn(f">{i:02d}</text>", svg, t["slug"])
            self.assertIn(f">{self.rf.esc(t['heading'].upper())}</text>", svg, t["slug"])
            self.assertIn(f">{len(t['rows'])} ROW", svg, t["slug"])

    def test_code_sits_on_a_chip_and_text_after_it_starts_past_the_chip(self):
        import re
        svg = self.svg("dark", [["**Deletes**", "run `sampo find` then more words after it"]])
        chip = re.search(r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)" height="[\d.]+" rx="3"[^>]*/>\s*'
                         r'<text x="([\d.]+)"[^>]*class="c"[^>]*>sampo find</text>\s*<text x="([\d.]+)"', svg)
        self.assertTrue(chip, svg)
        rx, rw, tx, after = (float(chip.group(k)) for k in range(1, 5))
        self.assertGreater(tx, rx)
        self.assertGreater(after, rx + rw)

    def test_positioned_runs_and_chips_stay_inside_the_figure(self):
        import re
        long = " ".join(["words `sampo capture --incident` more text and `sampo prove --migration`"] * 4)
        for theme in ("dark", "light"):
            svg = self.svg(theme, [["**A label that is long enough to wrap**", long], ["`x`", "`" + "y" * 200 + "`"]])
            w = float(re.search(r'viewBox="0 0 ([\d.]+)', svg).group(1))
            for x, n in re.findall(r'<text x="([\d.]+)"[^>]*textLength="([\d.]+)"', svg):
                self.assertLessEqual(float(x) + float(n), w - 12, (x, n))
            for x, cw in re.findall(r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)" height="[\d.]+" rx="3"', svg):
                self.assertLessEqual(float(x) + float(cw), w - 12, (x, cw))

    def test_no_chip_touches_the_text_beside_it(self):
        """Review (rsvg -w 900): '(STANDARD AUTONOMY', 'it.[sampo bench', 'Board;[sampo lane max]': a chip's box covered
        the character before it. Each text run beside a chip, measured by the drawing's own width() (an upper bound of
        the real fonts, which renderers that ignore textLength draw at), ends a clear pixel before the chip and starts
        one past it, in every figure and in a source with no space between a bracket and a chip."""
        import html
        import re
        el = re.compile(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="[\d.]+" rx="3"[^>]*/>'
                        r'|<text x="([\d.]+)" y="([\d.]+)" font-size="14"(?: class="(\w+)")?[^>]*>([^<]*)</text>')
        sources = {}
        for t in self.tables:
            for theme in ("dark", "light"):
                with open(os.path.join(FIGS, f"{t['slug']}-{theme}.svg"), encoding="utf-8") as f:
                    sources[f"{t['slug']}-{theme}"] = f.read()
        sources["bracket"] = self.svg("dark", [["**A**", "one summary at the end (`STANDARD AUTONOMY` switches back); "
                                                 "it. `sampo bench duel`, Board;`sampo lane max`/`x`"]])
        seen = 0
        for name, svg in sources.items():
            items = []  # (kind, x0, x1, baseline, text)
            for m in el.finditer(svg):
                if m.group(1):
                    x, y, cw = (float(m.group(k)) for k in (1, 2, 3))
                    items.append(("chip", x, x + cw, round(y + 14 * self.rf.CODE + 1.5, 1), ""))
                else:
                    text, style = html.unescape(m.group(7)), {"b": "b"}.get(m.group(6) or "", "")
                    x = float(m.group(4))
                    items.append(("text", x, x + self.rf.width([(text, style)], 14), float(m.group(5)), text))
            for i, (kind, x0, x1, base, _) in enumerate(items):
                if kind != "chip":
                    continue
                seen += 1
                for j in (i - 1, i + 1):
                    if 0 <= j < len(items) and items[j][0] == "text" and abs(items[j][3] - base) < 0.3:
                        other = items[j]
                        if j < i:
                            self.assertLessEqual(other[2] + 1, x0, f"{name}: {other[4]!r} runs into the chip at {x0}")
                        else:
                            self.assertGreaterEqual(other[1], x1 + 1, f"{name}: {other[4]!r} starts inside the chip")
        self.assertGreater(seen, 40)

    def test_text_before_a_chip_drawn_in_noto_sans_ends_just_short_of_it(self):
        """Review (rsvg -w 900): 'main;     sampo ship', 'and      curl | sh', 'nothingenforcing': per-glyph classes erred
        10% either way, and librsvg ignores textLength, so a run before a chip left a hole or ran into it. Measured in
        Noto Sans (the font librsvg draws the stack in), every such run in every figure ends 1 px to one space + CLEAR
        + 2 px (+1% for kerning) before its chip."""
        import html
        import re
        try:
            from PIL import ImageFont
            fonts = {k: ImageFont.truetype(f"/usr/share/fonts/noto/NotoSans-{v}.ttf", 1000)
                     for k, v in (("", "Regular"), ("b", "Bold"))}
        except (ImportError, OSError) as e:
            self.skipTest(f"no PIL or Noto Sans here: {e}")
        pair = re.compile(r'<text x="([\d.]+)" y="[\d.]+" font-size="14"(?: class="(\w+)")?[^>]*>([^<]*)</text>\s*'
                          r'<rect x="([\d.]+)" y="[\d.]+" width="[\d.]+" height="[\d.]+" rx="3"')
        most = self.rf.width([(" ", "")], 14) + self.rf.CLEAR + 2
        seen = 0
        for t in self.tables:
            with open(os.path.join(FIGS, f"{t['slug']}-dark.svg"), encoding="utf-8") as f:
                svg = f.read()
            starts = set(re.findall(r'<text x="([\d.]+)" y="[\d.]+" font-size="10" class="h d"', svg))
            for x, cls, text, chip in pair.findall(svg):
                if chip in starts:  # the chip opens a line of the next column, nothing is before it
                    continue
                text = html.unescape(text)
                real = fonts["b" if cls == "b" else ""].getlength(text) * 14 / 1000
                gap = float(chip) - float(x) - real  # kerning narrows a long run by up to ~1%
                self.assertTrue(1 <= gap <= most + 0.01 * real, f"{t['slug']}: {text!r} ends {gap:.1f} px before it")
                seen += 1
        self.assertGreater(seen, 10)

    def test_a_command_that_fits_its_column_stays_on_one_chip(self):  # review: `sampo task prove` split over 2 lines
        lines = self.rf.wrap([("Gates that ", ""), ("sampo task prove", "code")], 160, 14)
        self.assertIn(("sampo task prove", "code"), lines[-1])
        self.assertEqual(sum(s == "code" for ln in lines for _, s in ln), 1)

    def test_dark_and_light_share_one_layout(self):
        import re
        rows = [["**Deletes**", "recursive `rm -rf` outside the project"], ["**Git**", "force-pushes"]]
        geo = lambda s: re.findall(r'\b(?:x|y|width|height|textLength)="([\d.]+)"', s)
        self.assertEqual(geo(self.svg("dark", rows)), geo(self.svg("light", rows)))

    def test_alt_text_names_the_rows(self):
        t = next(t for t in self.tables if t["slug"] == "the-ward")
        alt = self.rf._alt(t)
        for label in ("Deletes", "Git", "Secrets"):
            self.assertIn(label, alt)
        self.assertNotIn("*", alt)


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
