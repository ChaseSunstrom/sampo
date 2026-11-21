"""T-3734: sampo panels render SPEC --term: the panels drawn in a terminal at a given width: fills as true-colour half
blocks, thin lines and dots in braille, the words as real text; readable at 80 and 160 columns."""
import json
import os
import re
import unicodedata
import unittest

from helpers import SampoTestCase

from sampo import panels, panelterm

kit = panels.kit()
KINDS = list(kit.KINDS)
DARK = kit.theme("board", "dark")
SGR = re.compile(r"\x1b\[[0-9;]*m")
BRAILLE = re.compile("[⠁-⣿]")


def draw(width=80, depth=24, names=None):
    return panelterm.draw(kit.sample_spec(names=names), DARK, width, depth)


def plain(text):
    return SGR.sub("", text)


def svg(body, w=200, h=100):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">{body}</svg>'


class Width(unittest.TestCase):
    def test_every_kind_fills_exactly_the_width_at_80_and_160(self):
        for w in (80, 160):
            for name in KINDS:
                lines = draw(w, names=[name]).split("\n")
                self.assertGreater(len(lines), 8, (w, name))
                for n, line in enumerate(lines):
                    self.assertEqual(len(plain(line)), w, f"{name} at {w}, line {n}: {plain(line)!r}")
                    self.assertTrue(line == plain(line) or line.endswith("\x1b[0m"), f"{name} line {n} leaks its colour")

    def test_the_width_is_clamped_to_what_reads(self):
        self.assertEqual({len(plain(x)) for x in draw(5, names=["funnel"]).split("\n")}, {20})
        self.assertEqual({len(plain(x)) for x in draw(5000, names=["funnel"]).split("\n")}, {400})

    def test_a_wide_terminal_sets_panels_side_by_side(self):
        rows80 = [x for x in plain(draw(80, names=["cluster", "funnel"])).split("\n") if "CLUSTER" in x or "FUNNEL" in x]
        rows160 = [x for x in plain(draw(160, names=["cluster", "funnel"])).split("\n") if "CLUSTER" in x and "FUNNEL" in x]
        self.assertEqual(len(rows80), 2)
        self.assertEqual(len(rows160), 1, "160 columns hold two panels a row, each as readable as at 80")

    def test_a_second_run_is_identical(self):
        self.assertEqual(draw(), draw())


class Look(unittest.TestCase):
    def test_fills_are_true_colour_half_blocks_and_thin_lines_braille(self):
        text = draw(80, 24)
        self.assertIn("▀", text)
        self.assertRegex(text, BRAILLE)
        self.assertIn(";38;2;", text)
        self.assertIn(";48;2;", text)
        self.assertNotIn(";38;5;", text)

    def test_titles_labels_and_stats_are_real_text(self):
        for w in (80, 160):
            text = plain(draw(w))
            for i, name in enumerate(KINDS, 1):
                self.assertIn(f"{i:02d}", text)
                self.assertIn(name.upper(), text, f"{name} at {w}")
            for word in ("orders", "billing", "validate", "events", "order.json"):
                self.assertIn(word, text, f"a label at {w}")

    def test_the_depth_follows_the_terminal(self):
        none, c256, c8 = draw(80, 0), draw(80, 256), draw(80, 8)
        self.assertNotIn("\x1b", none)
        self.assertRegex(none, "[▀▄█]", "no colour still draws the shapes")
        self.assertRegex(none, BRAILLE)
        self.assertIn(";38;5;", c256)
        self.assertNotIn(";38;2;", c256)
        self.assertIn("\x1b[", c8)
        self.assertNotRegex(c8, ";[34]8;[25];")

    def test_the_panel_colours_reach_the_cells(self):
        r, g, b = (int(DARK.panel[k:k + 2], 16) for k in (1, 3, 5))
        self.assertIn(f";48;2;{r};{g};{b}", draw(80, 24, names=["tanks"]), "the card's own fill")


class Raster(unittest.TestCase):
    def test_every_element_the_kit_can_draw_is_rasterized(self):
        body = ('<title>t</title><desc>d</desc><g transform="translate(4 6)">'
                '<rect x="0" y="0" width="60" height="30" rx="6" fill="#336699" stroke="#ffffff" stroke-width="2"/>'
                '<rect x="70" y="2" width="40" height="0.5" fill="#ff0000"/>'
                '<circle cx="140" cy="20" r="14" fill="#00ff00" opacity=".5"/><circle cx="170" cy="10" r="1" fill="#ffff00"/>'
                '<ellipse cx="40" cy="60" rx="30" ry="10" fill="none" stroke="#ff00ff"/>'
                '<path d="M80 40L120 40H130V60C130 70 150 70 150 80Q160 90 170 80Z" fill="#0000ff" stroke="#ffffff"/>'
                '<path d="m5 80l20 0h10v5l-5 5z" fill="none" stroke="#00ffff" stroke-width="12" stroke-dasharray="4 4"/>'
                '<line x1="0" y1="90" x2="190" y2="90" stroke="#ffffff"/>'
                '<polyline points="0,0 10,10 20,0" fill="none" stroke="#ffffff"/>'
                '<polygon points="180,40 195,60 165,60" fill="#ff8800"/>'
                '<text x="10" y="20" font-size="11" font-weight="700" fill="#ffffff">HEAD<tspan fill="#999999"> · sub</tspan></text>'
                '<text x="190" y="50" font-size="11" text-anchor="end" fill="#ffffff">right</text>'
                '<text x="100" y="86" font-size="11" text-anchor="middle" fill="#ffffff">mid</text></g>')
        lines = panelterm.raster(svg(body), 60, 24, "#101010")
        self.assertEqual({len(plain(x)) for x in lines}, {60})
        text = "\n".join(lines)
        self.assertIn("HEAD · sub", plain(text))
        for word in ("right", "mid"):
            self.assertIn(word, plain(text))
        self.assertIn(";48;2;51;102;153", text, "the rect's fill")
        self.assertRegex(text, BRAILLE)

    def test_what_it_cant_draw_fails_loudly_naming_it(self):
        for body, name in (('<image width="9" height="9"/>', "image"), ('<g transform="rotate(4)"><rect/></g>', "rotate"),
                           ('<path d="M0 0A5 5 0 0 1 9 9" stroke="#fff"/>', "A"),
                           ('<rect width="9" height="9" fill="url(#g)"/>', "url"), ('<foreignObject/>', "foreignObject"),
                           ('<rect width="9" height="9" fill="red"/>', "red")):
            with self.assertRaisesRegex(ValueError, re.escape(name), msg=body):
                panelterm.raster(svg(body), 40, 24, "#000000")

    def test_spec_text_never_reaches_the_terminal_as_a_control_or_a_wrong_width(self):
        for esc in ("\x1b", "&#27;"):  # XML itself refuses an ESC, so none can reach the cells
            with self.assertRaisesRegex(ValueError, "well-formed"):
                panelterm.raster(svg(f'<text x="0" y="20" font-size="11" fill="#ffffff">{esc}[2J</text>'), 40, 24, "#000000")
        body = ('<text x="0" y="20" font-size="11" fill="#ffffff">[31mX‮Y\u0085Z 中文 é!</text>')
        lines = panelterm.raster(svg(body), 40, 24, "#000000")
        raw = "\n".join(lines)
        self.assertEqual(SGR.sub("", raw).count("\x1b"), 0, "only the renderer's own SGR carries an escape")
        for c in "‮\u0085́":
            self.assertNotIn(c, raw)
        for line in lines:
            cells = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in plain(line))
            self.assertEqual(cells, 40, repr(plain(line)))
        self.assertIn("[31mX?Y Z", plain(raw))
        self.assertIn("中文", plain(raw))
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            panelterm.raster("<!DOCTYPE svg>" + svg("<rect/>"), 40, 24, "#000000")
        with self.assertRaisesRegex(ValueError, "ENTITY"):
            panelterm.raster('<!ENTITY x "y">' + svg("<rect/>"), 40, 24, "#000000")

    def test_text_never_overprints_text_and_a_cut_shows_it(self):
        body = ('<text x="0" y="20" font-size="11" fill="#ffffff">aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</text>'
                '<text x="20" y="20" font-size="11" fill="#ffffff">bbbb</text>')
        got = plain("\n".join(panelterm.raster(svg(body), 20, 24, "#000000")))
        self.assertIn("a" * 19 + "…", got)
        self.assertNotIn("b", got)


class Cli(SampoTestCase):
    def spec(self):
        p = os.path.join(self.repo, "panels.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(kit.sample_spec(names=["cluster", "funnel", "globe"]), f)

    def test_term_prints_the_panels_at_the_width_and_writes_nothing(self):
        self.spec()
        out = self.fm("panels", "render", "panels.json", "--term", "--width", "100", env={"NO_COLOR": "1"}).stdout
        lines = out.rstrip("\n").split("\n")
        self.assertNotIn("\x1b", out)
        self.assertEqual({len(x) for x in lines}, {100})
        for word in ("CLUSTER", "FUNNEL", "GLOBE", "orders"):
            self.assertIn(word, out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "docs/assets/panels")))

    def test_a_true_colour_terminal_gets_true_colour(self):
        self.spec()
        out = self.fm("panels", "render", "panels.json", "--term", "--width", "80",
                      env={"NO_COLOR": "", "COLORTERM": "truecolor"}).stdout
        self.assertIn(";38;2;", out)
        got = json.loads(self.fm("panels", "render", "panels.json", "--term", "--width", "80", "--json",
                                 env={"NO_COLOR": "", "COLORTERM": "truecolor"}).stdout)
        self.assertEqual((got["width"], got["depth"]), (80, 24))
        self.assertIn("CLUSTER", plain(got["text"]))


if __name__ == "__main__":
    unittest.main()
