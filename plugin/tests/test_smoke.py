"""T-0417: fm smoke checks the product the way its user uses it (JARVIS closed 34 tasks with green tests while its web
UI had panels that never loaded, an error toast on every load and overlaps at phone width)."""
import functools
import http.server
import json
import os
import sys
import threading
import unittest

from helpers import ForemanTestCase

from sampo import core as c
from sampo import smoke

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


class Smoke(ForemanTestCase):
    def runner(self, defects):
        path = os.path.join(self.tmp, "runner.py")
        with open(path, "w") as f:
            f.write(f"import json\nprint(json.dumps({{'views': ['desktop start', 'phone start'], "
                    f"'defects': {defects!r}}}))\n")
        return {"FOREMAN_SMOKE_RUNNER": f"{sys.executable} {path}", "PATH": BIN + os.pathsep + os.environ["PATH"]}

    def test_smoke_reports_each_defect_and_fails(self):
        self.fm("init")
        self.fm("smoke", "set", "web", "http://127.0.0.1:8199/")
        env = self.runner([{"where": "desktop tab Telemetry", "what": "loading placeholder still showing after 5 s"},
                           {"where": "phone start", "what": "page wider than the screen (612 px > 390 px)"}])
        r = self.fm("smoke", env=env, check=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("2 defects", r.stdout)
        self.assertIn("desktop tab Telemetry: loading placeholder still showing", r.stdout)
        self.assertIn("phone start: page wider than the screen", r.stdout)

    def test_smoke_passes_on_a_healthy_product(self):
        self.fm("init")
        self.fm("smoke", "set", "web", "http://127.0.0.1:8199/")
        r = self.fm("smoke", env=self.runner([]), check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no defects", r.stdout)

    def test_check_runs_smoke(self):
        # setting a smoke check makes it a gate: every fm check (so every task close) looks at the real product
        self.fm("init")
        self.fm("check", "add", "true")
        self.fm("smoke", "set", "web", "http://127.0.0.1:8199/")
        self.assertIn("fm smoke", self.fm("check", "list").stdout)
        self.fm("smoke", "set", "web", "http://127.0.0.1:8200/")
        self.assertEqual(self.fm("check", "list").stdout.count("fm smoke"), 1, "set twice, one gate")
        r = self.fm("check", env=self.runner([{"where": "desktop start", "what": "console error: boom"}]), check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fm("check", env=self.runner([]), check=False).returncode, 0)

    def test_set_refuses_an_empty_command_and_a_target_with_no_address(self):
        """T-6023: a blank cmd, or a web/http with no host, stored a target that has nothing to run, so the product check
        read as set (and advice named sampo smoke) while the run exited 1 with nothing to check."""
        self.fm("init")
        for words in (["cmd", ""], ["cmd", "  "], ["cmd", "", ""], ["web", "http://"], ["web", "https:///x"],
                      ["http", "http://"], ["http", "https://?a=1"], ["web"], ["http"], ["web", "not-a-url"]):
            r = self.fm("smoke", "set", *words, check=False)
            self.assertNotEqual(r.returncode, 0, f"{words}: {r.stdout}")
            self.assertIn("fm smoke set web", r.stderr, words)
            self.assertFalse(c.read_meta(c.find_project(self.repo)).get("smoke"), f"{words} stored nothing")
        self.assertEqual(self.fm("smoke", "set", "cmd", "true").returncode, 0)
        self.assertEqual(self.fm("smoke", "set", "http", "http://127.0.0.1:8199/").returncode, 0)

    def test_smoke_without_a_target_says_how(self):
        self.fm("init")
        r = self.fm("smoke", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("fm smoke set web <url>", r.stderr)
        r = self.fm("smoke", "set", "web", "file:///etc/passwd", check=False)
        self.assertIn("http(s) url", r.stderr, "http(s) only")

    def test_doctor_nudges_a_web_ui_without_smoke(self):
        self.assertEqual(smoke.nudge(["web UI"], {}), "this project has a web UI and no product check: "
                                                         "fm smoke set web <url>")
        self.assertIsNone(smoke.nudge(["web UI"], {"smoke": {"web": "http://x/"}}))
        self.assertIsNone(smoke.nudge(["server"], {}))
        self.assertIsNone(smoke.nudge(["web UI"], {"smoke": {"off": "a Claude Code UI mod, no URL"}}))

    def test_smoke_off_for_a_ui_no_url_serves(self):
        self.fm("init")
        self.fm("smoke", "set", "web", "http://127.0.0.1:8199/")
        self.fm("smoke", "set", "off", "a Claude Code UI mod, no URL")
        self.assertNotIn("fm smoke", self.fm("check", "list").stdout, "off drops the gate")


BAD = """<!doctype html><html><head><title>bad</title></head><body>
<div role="tablist"><button role="tab" id="a">Home</button><button role="tab" id="b">Telemetry</button></div>
<div id="panel"><p>Hello</p></div>
<div role="alert">The microphone is unavailable.</div>
<div style="width:600px;height:20px">wide</div>
<div style="position:fixed;bottom:0;left:0;width:200px;height:60px" aria-label="toast">toast</div>
<button style="position:fixed;bottom:10px;left:50px;width:60px;height:40px" aria-label="mic">mic</button>
<script>
console.error("boom");
document.getElementById("b").onclick = () => {
  document.getElementById("panel").innerHTML = '<div class="skeleton" style="width:200px;height:20px"></div>';
  fetch("/missing.json");
};
</script></body></html>"""

GOOD = """<!doctype html><html><head><meta name="viewport" content="width=device-width"><title>good</title></head>
<body><header style="position:sticky;top:0;height:40px">JARVIS</header>
<button style="position:fixed;top:0;left:0;width:40px;height:40px" aria-label="menu">≡</button>
<div role="tablist"><button role="tab" id="a">Home</button><button role="tab" id="b">Telemetry</button></div>
<div id="panel"><p>Hello</p></div>
<input class="placeholder:text-gray-400" placeholder="Ask">
<div role="alert" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">good</div>
<script>document.getElementById("b").onclick = () => { document.getElementById("panel").textContent = "42 W"; };
</script></body></html>"""


@unittest.skipUnless(smoke.playwright(os.environ.get("FOREMAN_SMOKE_ROOT") or os.getcwd()),
                     "no Playwright here (FOREMAN_PLAYWRIGHT or a node_modules/playwright)")
class SmokeInABrowser(unittest.TestCase):
    """The crawl itself, against two local pages: it must name each kind of defect, and pass a healthy page."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.dir = tempfile.mkdtemp()
        nav = '<!doctype html><body><nav><a href="/good.html">Good</a></nav><p>Start</p></body>'
        hang = ('<!doctype html><body><div role="tablist"><button role="tab">Home</button><button role="tab" '
                'onclick="setTimeout(() => { while (true) {} }, 0)">Freeze</button></div></body>')
        for name, body in (("bad.html", BAD), ("good.html", GOOD), ("nav.html", nav), ("hang.html", hang)):
            with open(os.path.join(cls.dir, name), "w") as f:
                f.write(body)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=cls.dir)
        handler.log_message = lambda *a: None
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def crawl(self, page):
        pw = smoke.playwright(os.environ.get("FOREMAN_SMOKE_ROOT") or os.getcwd())
        res = smoke.crawl(pw, f"{self.base}/{page}", os.path.join(self.dir, "shots-" + page))
        if res.get("skipped"):  # T-1509: a Playwright without its browser
            self.skipTest(res["skipped"])
        return res

    def test_smoke_names_each_defect_in_a_real_browser(self):
        found = "\n".join(f"{d['where']}: {d['what']}" for d in self.crawl("bad.html")["defects"])
        for want in ("console error: boom", "The microphone is unavailable", "tab Telemetry: loading placeholder",
                     "HTTP 404", "wider than the screen", "overlap"):
            self.assertIn(want, found)

    def test_smoke_survives_a_page_that_stops_responding(self):
        # T-0420: JARVIS's first fm smoke ran past fm's 240 s and lost every result: a busy page hung the crawl
        import time
        t0 = time.time()
        res = self.crawl("hang.html")
        self.assertLess(time.time() - t0, 150)  # wall clock: the crawl's page-timeout deadline
        self.assertIn("desktop start", res["views"])
        self.assertIn("stopped responding", json.dumps(res["defects"]))

    def test_smoke_passes_a_healthy_page_in_a_real_browser(self):
        # review: a Tailwind placeholder: class, a clipped route announcer and a sticky header under a fixed button
        # are a healthy page
        res = self.crawl("good.html")
        self.assertEqual(res["defects"], [], json.dumps(res, indent=1))
        self.assertIn("desktop tab Telemetry", res["views"])
        self.assertIn("desktop page /good.html", self.crawl("nav.html")["views"], "no tabs: its nav links instead")
