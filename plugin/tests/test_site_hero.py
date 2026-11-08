"""T-1609, T-2040, T-2851: the landing page's proof ledger of the four totals site/build.py reads from
site/data/stats.json (in the record, the forge's own readings), and the Smith's session drawn as text in the site's
palette that forge.js sets down a blow at a time (a recorded video of a themed terminal smeared its colours). No JS or
reduced motion shows the whole transcript."""
import html
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

from helpers import PLUGIN, headless_chrome, read_json, read_text

ROOT = os.path.dirname(PLUGIN)
KEYS = ("tasks_done", "evidence", "proofs", "commits")


def build_module():
    spec = importlib.util.spec_from_file_location("site_build_hero", os.path.join(ROOT, "site", "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_copy(tmp, totals=None, edit_index=None, edit_stats=None, edit_genesis=None):
    """Build a copy of site/ the way the workflow does, its stats.json totals updated with `totals`, its index.html
    passed through `edit_index` and its genesis/genesis.json through `edit_genesis`; the finished process."""
    shutil.copytree(os.path.join(ROOT, "site"), os.path.join(tmp, "site"), ignore=shutil.ignore_patterns("__pycache__"))
    for d in ("demo", "plugin", "design", "docs"):  # design: the tokens and the logo (T-2753); docs: its pictures (T-3996)
        os.symlink(os.path.join(ROOT, d), os.path.join(tmp, d))
    if totals or edit_stats:
        path = os.path.join(tmp, "site", "data", "stats.json")
        stats = read_json(path)
        stats["totals"].update(totals or {})
        (edit_stats or (lambda s: s))(stats)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stats, f)
    if edit_index:
        path = os.path.join(tmp, "site", "index.html")
        page = edit_index(read_text(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
    if edit_genesis:
        path = os.path.join(tmp, "site", "genesis", "genesis.json")
        data = read_json(path)
        edit_genesis(data)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    return subprocess.run([sys.executable, os.path.join(tmp, "site", "build.py"), os.path.join(tmp, "out")],
                          capture_output=True, text=True, timeout=120)


def built_index(tmp, totals=None):
    r = build_copy(tmp, totals)
    if r.returncode:
        raise AssertionError(r.stdout + r.stderr)
    return read_text(os.path.join(tmp, "out", "index.html"))


def section(page, opening):
    """The landing's section that opens with `opening`, through its </section>."""
    start = page.index(opening)
    return page[start:page.index("</section>", start)]


def hero(page):
    return section(page, '<section class="hero"')


def record(page):
    """The record: the forge's own readings, where the proof ledger stands (T-2851)."""
    return section(page, '<section class="record" id="record"')


def ledger(page):
    """The record's proof ledger as {label: figure}."""
    box = re.search(r'<div class="proof">.*?</dl></div>', record(page), re.S)
    return {html.unescape(dt): html.unescape(dd) for dt, dd in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", box.group(0))}


class Proof(unittest.TestCase):
    def test_the_built_hero_shows_the_real_totals_inline(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = built_index(tmp)
        totals = read_json(os.path.join(ROOT, "site", "data", "stats.json"))["totals"]
        want = {label: f"{totals.get(k) or 0:,}" for k, label in build_module().STATIONS}
        self.assertEqual(ledger(page), want)
        self.assertNotIn("hero-scene", page)
        self.assertNotRegex(hero(page), r'(?:src|href)="https?:')

    def test_a_changed_stats_json_changes_the_ledger_and_zero_shows_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = ledger(built_index(tmp, {"tasks_done": 123456, "proofs": 0}))
        self.assertEqual((got["tasks done"], got["proofs"]), ("123,456", "0"))
        self.assertEqual(build_module().proof({}).count("<dd>0</dd>"), 4)

    def test_an_index_without_the_proof_mark_fails_the_build(self):
        mark = build_module().PROOF_MARK
        with tempfile.TemporaryDirectory() as tmp:
            r = build_copy(tmp, edit_index=lambda s: s.replace(mark, ""))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("lost the proof mark", r.stdout + r.stderr)

    def test_a_stats_json_without_the_test_count_fails_the_build(self):
        # review: the hero's "{{tests}} tests" line would have shipped as "0 tests"
        with tempfile.TemporaryDirectory() as tmp:
            r = build_copy(tmp, edit_stats=lambda s: s.pop("code", None))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no code.tests", r.stdout + r.stderr)


STATIONS = ["Capture", "Expand", "Ground", "Plan", "Execute", "Verify", "Reflect", "Record"]


class Clash(unittest.TestCase):
    """T-2964: site/style.css's generic .hero and .rules rules reach the forge's hero and the tower's rule cards; the
    forge and genesis sheets must override them (the cold review shot the page 90px low and the cards title-only)."""

    def test_the_forge_hero_drops_style_css_padding_and_the_eyebrow_holds_a_line(self):
        css = read_text(os.path.join(ROOT, "site", "forge.css"))
        self.assertRegex(css, r"\.forge \.hero\{padding:0\}")
        self.assertIn(".forge .hero .eyebrow{white-space:nowrap}", css)

    def test_the_towers_rule_cards_override_the_site_rules_grid(self):
        css = read_text(os.path.join(ROOT, "site", "genesis", "genesis.css"))
        self.assertIn(".rules>.rule{display:block;grid-template-columns:none;", css)

    def test_the_hammer_smear_canvas_reaches_above_the_old_clip(self):
        js, css = (read_text(os.path.join(ROOT, "site", n)) for n in ("forge.js", "forge.css"))
        self.assertIn("const FXB = [340, -160, 840, 1120];", js)
        self.assertIn("top:-16%;width:70%;height:112%", css)


class Session(unittest.TestCase):
    """T-2040, T-2851: the Smith's session is text, so it is crisp at any size and follows the theme; the forge only
    dims the lines it hasn't struck yet, so no script and reduced motion still read the whole of it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.page = built_index(cls.tmp)
        cls.out = os.path.join(cls.tmp, "out")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_smith_draws_the_session_as_text_with_no_video(self):
        s = section(self.page, 'id="smith"')
        self.assertIn('<figure class="log" id="log">', s)
        self.assertIn("<pre", s)
        self.assertGreaterEqual(s.count('class="ln'), 8)
        for gone in ("<video", "{{"):
            self.assertNotIn(gone, s)
        self.assertFalse(os.path.exists(os.path.join(self.out, "hero-scene.js")))

    def test_the_loop_walks_the_eight_stations_in_order(self):
        box = self.page[self.page.index('id="stations"'):]
        self.assertEqual(re.findall(r"<h3>(\w+)</h3>", box[:box.index("</ol>")]), STATIONS)

    def test_only_script_hides_lines(self):
        css = read_text(os.path.join(self.out, "forge.css"))
        hiders = re.findall(r"([^{}@;]*\.(?:ln|rise)\b[^{}]*)\{[^}]*opacity:\s*0[;}]", css)
        self.assertTrue(hiders)
        for sel in hiders:  # no JS, no hidden transcript or copy
            self.assertRegex(sel, r"\.(?:anim|js)\b", sel)

    def test_the_newest_playwright_build_is_the_one_used(self):
        # review: sorted as text, a build 999 would beat 1243; with nothing on PATH the newest download runs
        from unittest import mock
        with tempfile.TemporaryDirectory() as home:
            for build in ("999", "1243", "1100", "beta"):
                d = os.path.join(home, ".cache", "ms-playwright", f"chromium_headless_shell-{build}", "chrome-headless-shell-linux64")
                os.makedirs(d)
                with open(os.path.join(d, "chrome-headless-shell"), "w") as f:
                    f.write("#!/bin/sh\n")
                os.chmod(os.path.join(d, "chrome-headless-shell"), 0o755)
            with mock.patch.dict(os.environ, {"HOME": home, "PATH": home, "FOREMAN_CHROME": ""}), \
                    mock.patch("helpers._starts", return_value=True):  # the fakes can't start; the order is under test
                self.assertIn("chromium_headless_shell-1243", headless_chrome())

    def browse(self, *flags, budget=4000):
        """The built index in a headless Chromium at 1280x1100 after `budget` ms of virtual time: the dumped DOM."""
        browser = headless_chrome()
        if not browser:
            self.skipTest("no headless Chromium (FOREMAN_CHROME, chrome-headless-shell or Playwright's download)")
        try:  # its own process group: a timeout kills the renderer children too
            p = subprocess.Popen([browser, "--headless", "--window-size=1280,1100", f"--virtual-time-budget={budget}",
                                  *flags, "--user-data-dir=" + tempfile.mkdtemp(dir=self.tmp), "--dump-dom",
                                  "file://" + os.path.join(self.out, "index.html")],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            out, err = p.communicate(timeout=120)
        except OSError as e:
            self.skipTest(f"{browser} didn't run: {e}")
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            self.fail("headless Chromium timed out after 120 s")
        if '<section class="hero"' not in out:  # a root sandbox, no display libs
            self.skipTest(f"{browser} printed no page: {err[-300:]}")
        return out

    def test_the_forge_moves_and_reduced_motion_shows_it_still(self):
        def classes(dom):
            return re.search(r'<html[^>]*\bclass="([^"]*)"', dom).group(1).split()
        dom = self.browse()
        self.assertIn("anim", classes(dom))
        self.assertIn("js", classes(dom))
        still = classes(self.browse("--force-prefers-reduced-motion"))
        self.assertIn("js", still)
        self.assertNotIn("anim", still)  # every .anim rule is off: 12's stills


if __name__ == "__main__":
    unittest.main()
