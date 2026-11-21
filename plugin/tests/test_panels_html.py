"""T-3734: sampo panels render SPEC --html: one self-contained page of the panels, animated (packets run their flows,
clusters drift, the globe turns) at 30 fps or less, still under prefers-reduced-motion, live numbers from an optional
feed."""
import base64
import hashlib
import http.server
import json
import os
import re
import shutil
import tempfile
import threading
import unittest

from helpers import SampoTestCase, devtools, headless_chrome

from sampo import panelhtml, panels

kit = panels.kit()
KINDS = list(kit.KINDS)
THEMES = {s: kit.theme("board", s) for s in ("dark", "light")}


def page(feed=None, **kw):
    return panelhtml.page(kit.sample_spec(**kw), THEMES, feed)


def script(text):
    return re.search(r"<script>(.*?)</script>", text, re.S)[1]


def csp(text):
    return re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', text)[1]


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


class Page(unittest.TestCase):
    def test_every_panel_is_drawn_dark_and_light_with_ids_unique_on_the_page(self):
        text = page()
        self.assertEqual(text.count("<svg "), 2 * len(KINDS))
        self.assertEqual(text.count("<figure"), len(KINDS))
        ids = re.findall(r' id="([^"]+)"', text)
        self.assertEqual(len(ids), len(set(ids)), "an aria-labelledby id named twice reads the wrong title")
        self.assertIn("prefers-color-scheme: light", text)
        self.assertEqual(page(), text, "the same spec draws the same page")

    def test_it_is_self_contained_and_loads_nothing(self):
        text = page()
        self.assertEqual(text.count("<script"), 1)
        self.assertNotRegex(text, r"\b(?:src|href)=|url\(|@import|<link|<iframe|<img|<object|<embed|<base")
        policy = csp(text)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'none'", policy, "no feed: the page reaches nothing")
        digest = base64.b64encode(hashlib.sha256(script(text).encode()).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", policy, "only this page's own script runs")
        self.assertNotIn("unsafe-inline", policy.split("style-src")[0])

    def test_the_motion_is_capped_at_30_fps_and_rests_for_reduced_motion(self):
        js = script(page())
        self.assertLessEqual(int(re.search(r"\bFPS = (\d+)", js)[1]), 30)
        self.assertIn("prefers-reduced-motion: reduce", js)
        self.assertIn("cancelAnimationFrame", js)
        self.assertIn("visibilitychange", js, "a hidden tab draws nothing")

    def test_what_moves_is_marked_in_the_svg(self):
        text = page()
        packets = re.findall(r'<rect data-p="([^"]+)" data-t="([^"]+)"', text)
        self.assertGreaterEqual(len(packets), 2 * 8, "funnel, filetree, gate and arch carry packets")
        for p, t in packets:
            self.assertEqual(len(p.split()), 8, p)
            self.assertTrue(0 <= float(t) <= 1, t)
        sample = kit.KINDS["cluster"]["sample"]
        self.assertEqual(text.count("data-drift="), 2 * len(sample["clusters"]))
        self.assertEqual(text.count("data-globe="), 2)
        self.assertEqual(text.count("data-mer="), 2 * 5)
        self.assertIn("stroke-dasharray", text, "dashes march")

    def test_the_static_svgs_still_pass_the_kits_checks(self):
        for i, name in enumerate(KINDS, 1):
            p = kit.sample_spec(names=[name])["panels"][0]
            for s in ("dark", "light"):
                self.assertEqual(kit.check_svg(kit.panel_svg(p, THEMES[s], i, f"p{i}")), [], name)

    def test_a_feed_is_embedded_as_a_string_and_named_in_connect_src(self):
        text = page("https://example.org:8443/live.json?k=1&j=2")
        self.assertIn('const FEED = "https://example.org:8443/live.json?k=1&j=2"', script(text))
        self.assertIn("connect-src https://example.org:8443", csp(text))
        rel = page("live/numbers.json")
        self.assertIn('const FEED = "live/numbers.json"', script(rel))
        self.assertIn("connect-src 'self'", csp(rel))

    def test_a_feed_that_could_break_out_is_refused(self):
        for bad in ("javascript:alert(1)", "https://x.org/</script><script>", 'https://x.org/"x', "ftp://x.org/a",
                    "//x.org/a", "data:,1", "https://x .org/", "https://x.org/a\nb", "file:///etc/passwd", "a:b",
                    "https://x.org/" + "a" * 2100, "https://x.org/'x"):
            with self.assertRaises(ValueError, msg=bad):
                page(bad)


class Cli(SampoTestCase):
    def spec(self, **kw):
        write(self.repo, "panels.json", json.dumps(kit.sample_spec(**kw)))

    def read(self, rel):
        with open(os.path.join(self.repo, rel), encoding="utf-8") as f:
            return f.read()

    def test_html_writes_one_page_and_a_second_run_is_identical(self):
        self.spec()
        out = self.fm("panels", "render", "panels.json", "--html").stdout
        self.assertIn("docs/assets/panels/panels.html", out)
        self.assertEqual(os.listdir(os.path.join(self.repo, "docs/assets/panels")), ["panels.html"])
        first = self.read("docs/assets/panels/panels.html")
        self.assertEqual(first.count("<svg "), 2 * len(KINDS))
        self.fm("panels", "render", "panels.json", "--html")
        self.assertEqual(self.read("docs/assets/panels/panels.html"), first)

    def test_out_names_a_page_or_a_folder(self):
        self.spec()
        self.fm("panels", "render", "panels.json", "--html", "--out", "site/live.html")
        self.fm("panels", "render", "panels.json", "--html", "--out", "site/all")
        self.assertIn("<svg ", self.read("site/live.html"))
        self.assertIn("<svg ", self.read("site/all/panels.html"))

    def test_the_feed_flag_reaches_the_page(self):
        self.spec()
        self.fm("panels", "render", "panels.json", "--html", "--feed", "https://example.org/n.json")
        self.assertIn("connect-src https://example.org", self.read("docs/assets/panels/panels.html"))

    def test_private_text_in_the_spec_or_the_feed_writes_nothing(self):
        spec = kit.sample_spec()
        spec["title"] = "Build box admin@build.internal"
        write(self.repo, "panels.json", json.dumps(spec))
        got = self.fm("panels", "render", "panels.json", "--html", check=False)
        self.assertNotEqual(got.returncode, 0, "a private string in the spec")
        self.assertIn("private", got.stdout + got.stderr)
        self.spec()
        got = self.fm("panels", "render", "panels.json", "--html", "--feed", "https://build.internal/n.json", check=False)
        self.assertNotEqual(got.returncode, 0, "a private host in the feed")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "docs/assets/panels")))

    def test_a_bad_feed_and_a_flag_mix_write_nothing(self):
        self.spec()
        for argv in (["--html", "--feed", "javascript:alert(1)"], ["--html", "--term"], ["--feed", "https://x.org/n"],
                     ["--html", "--width", "90"]):
            got = self.fm("panels", "render", "panels.json", *argv, check=False)
            self.assertNotEqual(got.returncode, 0, argv)
            self.assertIn("sampo panels render", got.stdout + got.stderr, argv)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "docs/assets/panels")))


MARKUP = "<img src=x onerror=document.title=1>" + "z" * 70  # 100 characters of markup


class _Feed(http.server.BaseHTTPRequestHandler):
    body = json.dumps({"panels": [{"stat": "42 live"}, {"stat": 7}, {"nope": 1}]}).encode()

    def do_GET(self):  # noqa: N802  (/n.json small, /markup.json a long markup stat, /big.json 1.2 MB, /moved.json a 302)
        body = self.body
        if self.path.startswith("/markup"):
            body = json.dumps({"panels": [{"stat": MARKUP}]}).encode()
        elif self.path.startswith("/big"):
            body = json.dumps({"panels": [{"stat": "x"}], "pad": "p" * 1_200_000}).encode()
        elif self.path.startswith("/moved"):
            self.send_response(302)
            self.send_header("Location", "/n.json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


PROBE = """(() => {
  const d = document.documentElement, scene = matchMedia('(prefers-color-scheme: light)').matches ? '.light' : '.dark';
  const e = document.querySelector(scene + ' [data-p]'), g = document.querySelector(scene + ' [data-drift]');
  const f = [...document.querySelectorAll('figure')];
  return { frames: +(d.dataset.frames || 0), now: performance.now(), motion: d.dataset.motion,
           x: e.getAttribute('x'), drift: g.getAttribute('transform'),
           live: f.slice(0, 3).map(x => x.querySelector('[data-live]').textContent),
           stat: f[0].querySelector(scene + ' [data-stat]').textContent,
           status: document.querySelector('[data-status]').textContent,
           imgs: document.querySelectorAll('img').length }; })()"""


class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def look(self, media=(), at=(1.5, 4.0), feed=None):
        browser = headless_chrome()
        if not browser:
            self.skipTest("no headless Chromium (FOREMAN_CHROME, chrome-headless-shell or Playwright's download)")
        path = os.path.join(self.tmp, f"p{len(os.listdir(self.tmp))}.html")
        write(self.tmp, path, page(feed))
        try:
            return devtools(browser, "file://" + path, tempfile.mkdtemp(dir=self.tmp), "", media, list(at), PROBE)
        except (EOFError, BrokenPipeError, FileNotFoundError, PermissionError) as e:
            self.skipTest(f"{browser} didn't take DevTools over a pipe: {e!r}")

    def test_packets_move_and_clusters_drift_at_30_fps_or_less(self):
        for scheme in ("dark", "light"):
            a, b = self.look([("prefers-color-scheme", scheme)])
            self.assertEqual(b["motion"], "moving", scheme)
            self.assertGreater(b["frames"], a["frames"], scheme)
            self.assertNotEqual(a["x"], b["x"], f"{scheme}: the packet stood still")
            self.assertNotEqual(a["drift"], b["drift"], f"{scheme}: the cluster stood still")
            fps = (b["frames"] - a["frames"]) / ((b["now"] - a["now"]) / 1000)
            self.assertLessEqual(fps, 31, f"{scheme}: {fps:.1f} fps")

    def test_nothing_moves_under_reduced_motion(self):
        a, b = self.look([("prefers-reduced-motion", "reduce")])
        self.assertEqual((a["frames"], b["frames"]), (0, 0))
        self.assertEqual(a["x"], b["x"])
        self.assertEqual(b["motion"], "still")

    def feed(self, path, at=(2.5,)):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Feed)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            return self.look([("prefers-reduced-motion", "reduce")], at, f"http://127.0.0.1:{server.server_port}{path}")
        finally:
            server.shutdown()
            server.server_close()

    def test_the_feeds_numbers_reach_the_page_as_text(self):
        got = self.feed("/n.json")
        self.assertEqual(got[0]["live"][:2], ["42 live", "7"], got)
        self.assertEqual(got[0]["stat"], "42 live")
        self.assertEqual(got[0]["live"][2], "", "a panel the feed doesn't name keeps its own")
        self.assertIn("live ", got[0]["status"])

    def test_a_feeds_markup_is_cut_to_40_characters_of_text(self):
        got = self.feed("/markup.json")[0]
        self.assertEqual(got["stat"], MARKUP[:40], got)
        self.assertEqual(got["live"][0], MARKUP[:40])
        self.assertEqual(got["imgs"], 0, "markup in a stat is never parsed")

    def test_a_feed_over_1_mb_is_refused_and_the_wait_doubles(self):
        got = self.feed("/big.json")[0]
        self.assertIn("over 1 MB", got["status"], got)
        self.assertIn("again in 10 s", got["status"], "5 s doubles to 10 s on the first failure")
        self.assertNotEqual(got["stat"], "x", "a refused body writes nothing")

    def test_a_redirecting_feed_is_refused(self):
        got = self.feed("/moved.json")[0]
        self.assertIn("feed: ", got["status"], got)
        self.assertIn("again in 10 s", got["status"])
        self.assertNotEqual(got["stat"], "42 live", "the 302 target was never fetched")


if __name__ == "__main__":
    unittest.main()
