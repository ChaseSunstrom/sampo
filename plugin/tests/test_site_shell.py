"""T-3996: every page of the site is one site. The user had to ask twice for the features pages to match the landing:
they carried their own header (another nav, a sun icon for the theme, no GitHub button) and never loaded the site's
fonts, so they fell back to the system's serif and sans. So: every built page carries the landing's one header and
loads the fonts from the one stylesheet; every page sits in one group of the features index; and every feature the
README names has a page, or a reason here, so a new feature can't ship with no page."""
# map-links: site/build.py site/pages.py site/style.css
import os
import re
import subprocess
import sys
import tempfile
import unittest

from helpers import PLUGIN, read_text

SITE = os.path.join(os.path.dirname(PLUGIN), "site")
ROOT = os.path.dirname(PLUGIN)
sys.path.insert(0, SITE)

# README headings (## and ###) and feature summaries (<summary><b>…</b>) -> the features page that covers them
COVERED = {"Kalevala: it improves itself": "kalevala", "By the numbers": "benchmarks", "Talking to it": "planning",
           "The Ward": "guard", "Where you see it": "panel", "Every harness: headless, other agents and Smith": "drive",
           "Headless on a server": "drive", "Smith: any model, Sampo's loop": "code",
           "Model layer: models advise, proof decides": "model-layer", "Plans": "planning", "Knows your stack": "cards",
           "Verifies": "proof", "Shows you what happened": "tools", "Runs unattended": "drive",
           "Learns a fix once": "learning", "Changes many spots in one call": "tools", "Improves itself": "learning",
           "Improves itself, per aspect (Rune and Kalevala)": "kalevala", "Stays light": "speed"}
# README headings that are not a feature -> why no features page covers them
NOT_A_FEATURE = {"What it does": "the overview; the landing's hero says it", "How it works": "the landing's own section",
                 "Quick start": "the landing's install block", "Features": "the heading over the feature summaries",
                 "When not to use Sampo": "the landing's closing note", "Documentation": "links to docs/",
                 "Updating and removing": "install upkeep, in docs/", "Development": "for contributors, in docs/"}


def built():
    out = tempfile.TemporaryDirectory(prefix="site-shell-")
    p = subprocess.run([sys.executable, os.path.join(SITE, "build.py"), out.name], capture_output=True, text=True,
                       timeout=300)
    if p.returncode:
        raise AssertionError(p.stderr)
    return out


class OneShell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = built()
        cls.out = cls.tmp.name
        cls.pages = sorted(os.path.relpath(os.path.join(d, f), cls.out) for d, _, fs in os.walk(cls.out) for f in fs
                           if f.endswith(".html") and not os.path.relpath(d, cls.out).startswith(("gallery", "demo")))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def plain(head):
        """The header with what differs by page taken out: the current mark, the way up and the landing's own anchors."""
        head = head.replace(' aria-current="page"', "").replace('href="#top"', 'href=""')
        head = re.sub(r'(href|src)="(?:\.\./)*', r'\1="', head)
        return head.replace('href="index.html', 'href="')

    def test_every_page_carries_the_landings_header(self):
        self.assertGreaterEqual(len(self.pages), 35, self.pages)  # the landing, the tower, cards, 29 features, stories
        landing = None
        for rel in self.pages:
            with self.subTest(page=rel):
                page = read_text(os.path.join(self.out, rel))
                heads = re.findall(r'<header class="bar">.*?</header>', page, re.S)  # an article's own <header> is fine
                self.assertEqual(len(heads), 1, "one site header")
                for other in re.findall(r'<header\b(?! class="bar").*?</header>', page, re.S):
                    self.assertNotRegex(other, r'class="brand"|<nav\b', "another site header beside the bar")
                for part in ('<details class="menu">', 'id="theme"', "Forge<", 'class="gh"'):
                    self.assertIn(part, heads[0])
                landing = landing or self.plain(heads[0])
                self.assertEqual(self.plain(heads[0]), landing, "the same header as the landing's")

    def test_every_page_loads_the_fonts_its_tokens_name(self):
        css = read_text(os.path.join(SITE, "style.css"))
        faces = dict(re.findall(r'@font-face\{font-family:"([^"]+)";src:url\(([^)]+)\)', css))
        for var in ("--font-display", "--font-text", "--font-mono"):
            family = re.search(rf"{var}:\s*\"?([^\",;]+)", css).group(1).strip()
            with self.subTest(token=var):
                self.assertIn(family, faces, f"{var} names {family}, which style.css never loads")
                self.assertTrue(os.path.isfile(os.path.join(SITE, faces[family])), faces[family])
        for rel in self.pages:
            with self.subTest(page=rel):
                self.assertRegex(read_text(os.path.join(self.out, rel)), r'<link rel="stylesheet" href="(\.\./)*style\.css')

    def test_the_panel_page_shows_the_board_and_kalevala_has_a_page(self):
        panel = read_text(os.path.join(self.out, "features", "panel.html"))
        for picture in ("board-light.png", "board-dark.png", "claude-code.png"):  # the pane as it is now, not a 2025 mock
            self.assertIn(f"assets/docs/{picture}", panel)
            self.assertTrue(os.path.isfile(os.path.join(self.out, "assets", "docs", picture)), picture)
        self.assertIn("kalevala-dark.svg", read_text(os.path.join(self.out, "features", "kalevala.html")))

    def test_the_nav_stays_short(self):
        import build
        self.assertLessEqual(len(build.NAV), 7, "a long nav wrapped and hid the theme switch; group pages under Features")


class Features(unittest.TestCase):
    def test_every_page_is_in_one_group_and_the_index_draws_them(self):
        import build
        import pages as P
        slugs = [s for _, _, group in P.GROUPS for s in group]
        self.assertEqual(sorted(slugs), sorted(set(slugs)), "a page in two groups")
        self.assertEqual(sorted(slugs), sorted(p["slug"] for p in P.PAGES))
        index = build.render_index(P.PAGES)
        self.assertNotIn('class="side"', index, "the index is the list; no second list beside it")
        for slug in slugs:
            self.assertIn(f'href="{slug}.html"', index)

    def test_every_feature_the_readme_names_has_a_page(self):
        import pages as P
        readme = read_text(os.path.join(ROOT, "README.md"))
        features = readme.split("\n## Features\n", 1)[1].split("\n## ", 1)[0]  # the feature list; other summaries are details
        named = re.findall(r"(?m)^#{2,3} (.+)$", readme) + re.findall(r"<summary><b>(.+?)</b></summary>", features)
        slugs = {p["slug"] for p in P.PAGES}
        for name in named:
            with self.subTest(heading=name):
                if name in NOT_A_FEATURE:
                    continue
                self.assertIn(name, COVERED, "a README feature with no features page: add a page to site/pages.py "
                                             "(and its group), or say here why it has none")
                self.assertIn(COVERED[name], slugs)
        for name in list(COVERED) + list(NOT_A_FEATURE):
            self.assertIn(name, named, f"{name!r} is no longer in the README: drop it here")


if __name__ == "__main__":
    unittest.main()
