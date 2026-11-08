"""T-2999: the live page (plugin/web/live) under the server's strict CSP. Static checks on the page's three files, then
recorded snapshots (fixtures/live) played through a stand-in for `sampo live`: every panel filled, sparks that travel
from caller to model, reduced motion shown by highlights, a dropped stream shown as reconnecting, no console errors,
at 1920x1080 and 390 wide."""
import glob
import json
import os
import re
import secrets
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from helpers import PLUGIN, headless_chrome

WEB = os.path.join(PLUGIN, "web", "live")
FIXTURES = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live", "snap-*.json")))
PANELS = ("scene", "focus", "gpu", "jobs", "queue", "closes", "counters", "bench", "ticker")
KINDS = {"write", "judge", "embed", "train", "plan", "act", "review"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".woff2": "font/woff2", ".json": "application/json"}
FLAT = r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$"  # the names `sampo live` serves under /static (T-2998's _static)


def text(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def snaps():
    out = []
    for path in FIXTURES:
        with open(path, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


class Files(unittest.TestCase):
    """What the CSP and the offline promise need, read from the files themselves."""

    def test_fixtures_follow_the_schema_and_carry_flows(self):
        got = snaps()
        self.assertGreaterEqual(len(got), 3)
        keys = {"at", "project", "focus", "queue", "closed_today", "lanes", "jobs", "models", "gpu", "flows", "events",
                "stats", "board"}
        self.assertTrue([s for s in got if s["flows"]], "some snapshot carries flows")
        last = got[-1]  # bench rows don't exist yet; the funnel and training report progress (T-3083, T-3044), the rest none
        self.assertEqual(last["stats"]["bench"], [])
        self.assertEqual(sorted(j["progress"] is None for j in last["jobs"]), [False, False, True, True, True])
        for s in got:
            self.assertEqual(set(s), keys)
            self.assertLessEqual({f["kind"] for f in s["flows"]}, KINDS)
            self.assertLessEqual({m["family"] for m in s["models"]}, {"claude", "ollama", "jeff", "jarvis", "embed"})
            self.assertLessEqual({j["kind"] for j in s["jobs"]}, {"funnel", "grow", "exam", "train", "bench", "check",
                                                                   "other"})
            self.assertLessEqual(len(s["queue"]), 12)
            self.assertLessEqual(len(s["events"]), 60)

    def test_every_tag_that_loads_code_or_style_carries_the_nonce(self):
        page = text("index.html")
        tags = re.findall(r"<(?:script|style)\b[^>]*>|<link\b[^>]*\bstylesheet\b[^>]*>", page, re.I)
        self.assertTrue(tags)
        for tag in tags:
            self.assertIn('nonce="__NONCE__"', tag, tag)
        for tag in re.findall(r"<script\b[^>]*>", page, re.I):
            self.assertRegex(tag, r'\bsrc="/static/[\w.-]+\.js"', "scripts load from /static, never inline")

    def test_no_inline_handlers_style_attributes_or_script_urls(self):
        page = text("index.html")
        self.assertNotRegex(page, r"(?i)\son[a-z]+\s*=", "an inline handler breaks under the CSP")
        self.assertNotRegex(page, r"(?i)\sstyle\s*=", "a style attribute breaks under the CSP")
        self.assertNotIn("javascript:", page.lower())

    def test_no_external_urls_anywhere(self):
        for name in ("index.html", "live.css", "live.js", "charts.js", "board.css", "board.js", "board-tokens.json"):
            body = text(name)
            for pat in (r"(?i)\bhttps?://", r"(?i)url\(\s*['\"]?//", r"(?i)(?:src|href)\s*=\s*['\"]//", r"(?i)@import"):
                self.assertNotRegex(body, pat, f"{name}: everything works offline")

    def test_the_script_stays_inside_the_csp(self):
        js = text("live.js") + text("charts.js") + text("board.js")
        for pat in (r"\beval\s*\(", r"\bnew\s+Function\b", r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML",
                    r"document\.write", r"setAttribute\(\s*['\"]style", r"set(?:Timeout|Interval)\(\s*['\"`]"):
            self.assertNotRegex(js, pat)

    def test_every_panel_has_its_element(self):
        page = text("index.html")
        for panel in PANELS + ("link", "motion", "net", "view-forge", "view-net", "board", "view-board"):
            self.assertRegex(page, rf'\bid="{panel}"', panel)

    def test_scrolling_lists_take_the_keyboard(self):  # T-2999 review: keyboard users could not scroll long lists
        html = text("index.html")
        for i in ("jobs-list", "queue-list", "closes-list", "sessions-list", "projects-list"):
            self.assertRegex(html, rf'id="{i}" tabindex="0" aria-label="[^"]+"')

    def test_phone_controls_are_big_enough(self):  # T-3026 review: iOS zooms into a field under 16px
        css = text("live.css")
        phone = css[css.index("max-width:700px"):]
        self.assertRegex(phone, r"\.note-in\{[^}]*font-size:16px")
        self.assertRegex(phone, r"\.act\{[^}]*min-height:28px")

    def test_reduced_motion_is_honoured_by_the_style_and_the_script(self):
        self.assertIn("prefers-reduced-motion", text("live.css"))
        self.assertIn("prefers-reduced-motion", text("live.js"))

    def test_the_board_kit_loads_like_the_rest(self):  # T-3659: its two files, each with the nonce, before live.js
        page = text("index.html")
        self.assertRegex(page, r'<link rel="stylesheet" href="/static/board\.css" nonce="__NONCE__">')
        self.assertRegex(page, r'<script src="/static/board\.js" nonce="__NONCE__" defer></script>')
        self.assertLess(page.index("board.js"), page.index("live.js"), "the kit is there when live.js mounts it")
        self.assertIn("prefers-reduced-motion", text("board.css"))
        urls = re.findall(r"url\(\s*['\"]?([^'\")]+)", text("board.css"))
        for u in urls:
            self.assertRegex(u, FLAT, u)
            self.assertTrue(os.path.isfile(os.path.join(WEB, u)), u)

    def test_fonts_are_bundled_files(self):
        urls = re.findall(r"url\(\s*['\"]?([^'\")]+)", text("live.css"))
        self.assertTrue(urls, "the display face comes with the page")
        for u in urls:
            self.assertRegex(u, FLAT, f"{u}: /static serves flat names only")
            self.assertTrue(os.path.isfile(os.path.join(WEB, u)), u)


class Stand(BaseHTTPRequestHandler):
    """`sampo live` as the page meets it: / with a fresh nonce and a strict CSP, /static, /api/state (the first
    snapshot) and /api/stream (the rest, then a dropped connection; the second connection answers late)."""
    server_version, sys_version = "live-test", ""

    def log_message(self, *a):
        pass

    def send(self, code, body, kind, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # T-3026: /api/act as the page meets it: record headers and body; "forbid" in a note is a 403
        srv = self.server
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        srv.posts.append({"path": self.path, "type": self.headers.get("Content-Type"), "nonce": self.headers.get("X-Sampo-Nonce"),
                          "body": body})
        if self.path != "/api/act" or not isinstance(body, dict):
            return self.send(404, b"", "text/plain")
        if body.get("action") == "resume":
            time.sleep(0.6)  # in flight long enough for a second press to be seen refused
        if body.get("text") == "forbid":
            return self.send(403, json.dumps({"ok": False, "error": "bad nonce"}).encode(), TYPES[".json"])
        self.send(200, json.dumps({"ok": True, "exit": 0, "action": body.get("action")}).encode(), TYPES[".json"])

    def do_GET(self):
        srv, path = self.server, self.path.split("?")[0]
        if path == "/":
            nonce = secrets.token_urlsafe(16)
            csp = (f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'nonce-{nonce}'; "
                   "img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                   "frame-ancestors 'none'")
            page = text("index.html").replace("__NONCE__", nonce).replace(
                "</head>", f'<meta name="sampo-act" content="{srv.token}">\n</head>').encode()
            return self.send(200, page, TYPES[".html"], [("Content-Security-Policy", csp)])
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            full = os.path.join(WEB, name)
            if not re.match(FLAT, name) or os.path.splitext(name)[1] not in TYPES or not os.path.isfile(full):
                return self.send(404, b"", "text/plain")
            with open(full, "rb") as f:
                return self.send(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))
        srv.log.append(self.path)
        slug = (parse_qs(urlparse(self.path).query).get("project") or [""])[0]
        if path == "/api/machine":
            return self.send(200, json.dumps(srv.machine).encode(), TYPES[".json"])
        if slug == "all" and srv.all_snaps:  # T-3730: the All snapshot (a test that wants it fills srv.all_snaps)
            if path == "/api/state":
                return self.send(200, json.dumps(srv.all_snaps[0]).encode(), TYPES[".json"])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for _ in range(30):
                    self.wfile.write(b"event: state\ndata: " + json.dumps(srv.all_snaps[-1]).encode() + b"\n\n")
                    self.wfile.flush()
                    if srv.stop.wait(0.7):
                        return
            except OSError:
                pass
            return
        if slug == "all":  # no All fixture: the default project answers, so a page that opens in All still has its data
            slug = ""
        if slug and slug != srv.snaps[0]["project"]["slug"] and (path in ("/api/state", "/api/stream")):
            if slug != srv.other["project"]["slug"]:
                return self.send(404, b"", "text/plain")
            if path == "/api/state":
                return self.send(200, json.dumps(srv.other).encode(), TYPES[".json"])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for _ in range(30):
                    self.wfile.write(b"event: state\ndata: " + json.dumps(srv.other).encode() + b"\n\n")
                    self.wfile.flush()
                    if srv.stop.wait(0.7):
                        return
            except OSError:
                pass
            return
        if path == "/api/state":
            return self.send(200, json.dumps(srv.snaps[0]).encode(), TYPES[".json"])
        if path == "/api/stream":
            with srv.lock:
                srv.streams += 1
                nth = srv.streams
            if nth > 1:
                time.sleep(1.2)  # the page shows "reconnecting" meanwhile
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(b"retry: 300\n\n")
                rest = srv.snaps[1:] if nth == 1 else srv.snaps[-1:] * 30
                for s in rest:
                    if srv.stop.wait(0.7):
                        return
                    self.wfile.write(b"event: state\ndata: " + json.dumps(s).encode() + b"\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            return  # HTTP/1.0: returning closes the stream
        self.send(404, b"", "text/plain")


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stand)
    srv.daemon_threads = True
    srv.snaps, srv.streams, srv.lock, srv.stop = snaps(), 0, threading.Lock(), threading.Event()
    srv.log, srv.posts, srv.token = [], [], secrets.token_hex(16)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live", "machine.json"), encoding="utf-8") as f:
        srv.machine = json.load(f)
    srv.all_snaps = []
    srv.other = json.loads(json.dumps(srv.snaps[-1]))  # another project: its own slug and task at the anvil
    srv.other["project"] = {"slug": "other-1b2c3d", "drive": False, "autonomy": "standard"}
    srv.other["focus"] = {"id": "T-9001", "title": "The other project's task", "step": "1/2", "stage": "building"}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# Before the page: watch data-link change over time (live -> reconnecting -> live) without trusting the page's word.
WATCH = """(() => { window.__links = [];
  new MutationObserver(() => { const v = document.documentElement && document.documentElement.getAttribute("data-link");
    if (v && window.__links[window.__links.length - 1] !== v) window.__links.push(v); })
  .observe(document, {attributes: true, subtree: true, attributeFilter: ["data-link"]}); })()"""

PROBE = """(() => { const $ = id => document.getElementById(id), n = (id, sel) => $(id) ? $(id).querySelectorAll(sel).length : -1;
  const c = $("scene"), r = c ? c.getBoundingClientRect() : {width: 0, height: 0};
  return {links: window.__links, motion: document.documentElement.getAttribute("data-motion"),
    filled: Object.fromEntries(%s.map(id => [id, $(id) ? $(id).textContent.trim().length : -1])),
    focus: $("focus") ? $("focus").textContent : "", gpu: $("gpu") ? $("gpu").textContent : "",
    counters: $("counters") ? $("counters").textContent : "",
    gpuModels: n("gpu", "[data-model]"), jobs: n("jobs", "li"), queue: n("queue", "li"), closes: n("closes", "li"),
    bench: n("bench", "tbody tr"), ticker: n("ticker", "li"),
    benches: c ? c.dataset.benches || "" : "", nodes: +(c && c.dataset.nodes || 0), sparks: +(c && c.dataset.sparks || 0), flown: +(c && c.dataset.flown || 0),
    lit: +(c && c.dataset.lit || 0), scene: [Math.round(r.width), Math.round(r.height)], canvas: c ? [c.width, c.height] : [0, 0],
    hash: location.hash, sessions: n("sessions-list", "li"), projects: n("projects-list", "li"),
    picks: n("rail", "button[data-project]"), rail: $("rail") ? $("rail").textContent : "",
    railhtml: $("rail") ? $("rail").innerHTML : "", project: $("project") ? $("project").textContent : "",
    view: document.documentElement.getAttribute("data-view"), store: (() => { try { return localStorage.getItem("sampo-live-view"); } catch (e) { return "x"; } })(),
    net: (() => { const q = $("net"); if (!q) return null; const r = q.getBoundingClientRect(), d = q.dataset;
      let cols = []; try { cols = JSON.parse(d.cols || "[]"); } catch (e) {}
      return {cols, nodes: +(d.nodes || 0), edges: +(d.edges || 0), pulses: +(d.pulses || 0), lit: +(d.lit || 0),
              size: [Math.round(r.width), Math.round(r.height)], shown: r.width > 0, painted: d.painted === "1", frames: +(d.frames || 0)}; })(),
    netlinks: Array.from(document.querySelectorAll("#net-list li")).map(x => x.textContent), stagelabel: $("stage") ? $("stage").getAttribute("aria-label") : "",
    scrolly: Math.round(window.scrollY), stagetop: $("stage") ? Math.round($("stage").getBoundingClientRect().top) : -1, vh: window.innerHeight,
    wide: document.documentElement.scrollWidth,
    theme: document.documentElement.getAttribute("data-theme"), text: document.body.innerText,
    fonts: ["h2", ".word", ".cv", "body"].map(q => (document.querySelector(q) ? getComputedStyle(document.querySelector(q)).fontFamily : "")).join("|"),
    board: (() => { const q = $("board"); if (!q) return null; const r = q.getBoundingClientRect(), d = q.dataset;
      const ps = Array.from(q.querySelectorAll(".bd-p")).map(p => { const cv = p.querySelector("canvas"), b = p.getBoundingClientRect(), t = s => { const e = p.querySelector(s); return e ? e.textContent.trim() : ""; };
        return {k: p.dataset.k, chip: t(".bd-chip"), title: t(".bd-t"), label: t(".bd-l"), foot: t(".bd-f"), x: Math.round(b.left), w: Math.round(b.width), h: Math.round(b.height),
                painted: !!cv && cv.dataset.painted === "1", canvas: cv ? [cv.width, cv.height] : [0, 0], font: getComputedStyle(p.querySelector(".bd-t") || p).fontFamily}; });
      return {shown: r.width > 0, panels: ps, cols: new Set(ps.filter(p => p.w).map(p => p.x)).size, frames: +(d.frames || 0), running: d.running || "",
              painted: ps.length > 0 && ps.every(p => p.painted)}; })(),
    shown: Object.fromEntries(["scene", "views", "theme-btn", "focus", "net", "board", "view-board", "view-forge", "view-net", "rail"].map(id => [id, !!$(id) && getComputedStyle($(id)).display !== "none"])),
    bg: getComputedStyle(document.body).backgroundColor, ink: getComputedStyle(document.body).color,
    stheme: (() => { try { return localStorage.getItem("sampo-live-theme"); } catch (e) { return "x"; } })(),
    themepressed: $("theme-btn") ? $("theme-btn").getAttribute("aria-pressed") : null, title: document.title,
    themelabel: $("theme-btn") ? $("theme-btn").textContent.trim() : "", active: document.activeElement ? document.activeElement.id : "",
    say: $("theme-say") ? $("theme-say").textContent : "", nettop: $("net") ? Math.round($("net").getBoundingClientRect().top) : -1,
    barh: Math.round(document.querySelector(".bar").getBoundingClientRect().height),
    ktok: Object.fromEntries(["amber", "violet", "green", "orange", "blue", "red", "pale", "other", "dflt"].map(k => [k, getComputedStyle(document.documentElement).getPropertyValue("--k-" + k).trim()])),
    plate: getComputedStyle(document.documentElement).getPropertyValue("--plate").trim(),
    night: getComputedStyle(document.documentElement).getPropertyValue("--night").trim(),
    charts: Object.fromEntries(["ch-calls", "ch-spend", "ch-funnel", "ch-gpu", "sp-calls"].map(id => { const e = $(id), s = e && e.querySelector("svg"), t = s && s.querySelector(".ch-t");
      return [id, { shown: !!e && e.getBoundingClientRect().width > 0, svg: !!s, label: s ? (s.getAttribute("aria-label") || "") : "", role: s ? s.getAttribute("role") : "",
        texts: s ? s.querySelectorAll("text").length : 0, fill: t ? getComputedStyle(t).fill : "", area: s ? s.querySelectorAll(".ch-area").length : 0,
        dots: s ? s.querySelectorAll(".ch-dot").length : 0, segs: s ? s.querySelectorAll(".ch-seg").length : 0}]; })),
    pgt: Array.from(document.querySelectorAll(".pg-t")).map(x => x.textContent), pgbars: document.querySelectorAll(".pg-bar[role=progressbar]").length,
    legs: document.querySelectorAll(".ch-leg li").length, tankshown: !!$("gpu") && getComputedStyle(document.querySelector(".tank")).display !== "none",
    netbg: (() => { const q = $("net"); try { const o = document.createElement("canvas").getContext("2d", {willReadFrequently: true}); o.canvas.width = o.canvas.height = 8; o.drawImage(q, 3, 3, 1, 1, 0, 0, 1, 1); const d = o.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3]]; } catch (e) { return null; } })(),
    tankflat: (() => { const s = document.querySelector("#tank-fill .seg"); return s ? getComputedStyle(s).backgroundImage : "none"; })()}; })()""" % json.dumps(list(PANELS))


class Page(unittest.TestCase):
    """The page in a real Chromium; skipped where there is none."""

    def setUp(self):
        self.browser = headless_chrome()
        if not self.browser:
            self.skipTest("no headless Chromium (FOREMAN_CHROME, chrome-headless-shell or Playwright's download)")
        sys.path.insert(0, os.path.join(PLUGIN, "lib"))
        self.last = snaps()[-1]

    def watch(self, size, reduced, done, within=45, step=None, hash="", theme="forge", scheme=None, all_snaps=None,
              motion=False):
        """Load the page at size, poll PROBE until done(got) or `within` s; (got, errors). step(b, got), if given, runs
        on every poll (it clicks, reloads); got["reqs"] lists the requests the server saw.
        A green run returns in about 8 s; the long ceiling is for a loaded machine (the gates run 16 at a time)."""
        from sampo import shot
        srv, errs = serve(), []
        srv.all_snaps = all_snaps or []
        b = shot.shared(self.browser, size, reduced=reduced or not motion)  # T-3715: still frame unless motion=True (the CPU was the animation)
        try:
            b.on["Runtime.exceptionThrown"] = lambda p: errs.append("exception: " + json.dumps(p.get("exceptionDetails"))[:400])
            b.on["Runtime.consoleAPICalled"] = lambda p: p.get("type") in ("error", "assert", "warning") and errs.append(
                "console: " + json.dumps(p.get("args"))[:400])
            b.on["Log.entryAdded"] = lambda p: p["entry"].get("level") in ("error", "warning") and errs.append(
                "log: " + p["entry"].get("text", "") + " " + p["entry"].get("url", ""))
            b.call("Runtime.enable", b.sid)
            b.call("Log.enable", b.sid)
            b.call("Page.addScriptToEvaluateOnNewDocument", b.sid, source=WATCH)
            if theme:  # T-3055: the page is modern by default; the older tests look at the forge (the viewer's remembered choice)
                b.call("Page.addScriptToEvaluateOnNewDocument", b.sid, source=f"try {{ if (!sessionStorage.getItem('t')) {{ "
                       f"sessionStorage.setItem('t', '1'); localStorage.setItem('sampo-live-theme', '{theme}'); }} }} catch (e) {{}}")
            if scheme:
                # T-3715: setEmulatedMedia replaces every feature, so the reduced-motion one rides along or the scheme drops it
                b.call("Emulation.setEmulatedMedia", b.sid, features=[{"name": "prefers-color-scheme", "value": scheme}]
                       + ([] if motion else [{"name": "prefers-reduced-motion", "value": "reduce"}]))
            shot.load(b, f"http://127.0.0.1:{srv.server_address[1]}/" + hash)
            deadline = time.monotonic() + within
            while True:
                got = b.call("Runtime.evaluate", b.sid, expression=PROBE, returnByValue=True)["result"].get("value") or {}
                got["reqs"] = list(srv.log)
                got["posts"], got["token"] = list(srv.posts), srv.token
                if step and not done(got):
                    step(b, got)
                if done(got) or time.monotonic() > deadline:
                    if not motion:  # T-3715: a test that doesn't check motion leaves no animation loop running
                        self.assertNotEqual(got.get("motion"), "full", "a still-frame test must run with reduced motion")
                        self.assertNotEqual((got.get("board") or {}).get("running"), "1", "the board's frame loop must not run")
                    return got, errs
                b.wait("live-test.never", 0.3)  # events (console, log) are read meanwhile
        finally:
            b.close()
            srv.stop.set()
            srv.shutdown()
            srv.server_close()

    def filled(self, got, size):
        last, where = self.last, f"at {size[0]}x{size[1]}"
        for panel in PANELS:
            self.assertGreater(got["filled"].get(panel, -1), 0 if panel != "scene" else -1, f"{panel} {where}")
        self.assertIn(last["focus"]["id"], got["focus"])
        self.assertIn(last["gpu"]["name"], got["gpu"])
        self.assertEqual(got["gpuModels"], sum(1 for m in last["models"] if m["loaded"] and (m["vram_mb"] or 0) > 0
                                                and m["where"] == "gpu"), where)
        self.assertEqual(got["jobs"], len(last["jobs"]), where)
        self.assertEqual(got["queue"], len(last["queue"]), where)
        self.assertEqual(got["closes"], len(last["closed_today"]), where)
        self.assertEqual(got["bench"], len(last["stats"]["bench"]), where)
        self.assertEqual(got["ticker"], len(last["events"]), where)
        self.assertIn(f"{last['stats']['cards']['general']:,}", got["counters"])
        self.assertIn(f"{last['stats']['moons']['candidates']:,}", got["counters"])
        self.assertGreater(got["nodes"], 0, where)
        self.assertGreaterEqual(got["scene"][1], 300, f"the scene is the hero {where}")
        self.assertLessEqual(got["wide"], size[0], f"no sideways scroll {where}")

    @staticmethod
    def back(got):
        """The stream dropped and came back: by then the page holds the last snapshot (earlier ones share its queue)."""
        links = got.get("links") or [""]
        return "reconnecting" in links and links[-1] == "live"

    def test_sparks_travel_from_caller_to_model_at_desktop_and_phone(self):
        for size in ((1920, 1080), (390, 844)):
            move = size[0] > 1000  # T-3715: sparks fly on the desktop run; the phone run checks the layout on a still frame
            got, errs = self.watch(size, False, lambda g: self.back(g) and (g.get("flown", 0) > 0 or not move), motion=move)
            self.assertEqual(errs, [], f"console at {size}")
            self.filled(got, size)
            self.assertEqual(got["motion"], "full" if move else "still")
            self.assertGreater(got["sparks"], 0, size) if move else None
            self.assertGreater(got["flown"], 0, f"a spark reached its model at {size}") if move else None
            links = got["links"]
            self.assertEqual(links[-1], "live", links)
            self.assertIn("reconnecting", links, "a dropped stream is shown")

    def test_the_rail_and_strip_show_the_machine_and_a_pick_switches_the_scene(self):
        pick = "document.querySelector('#projects-list button[data-project=\"other-1b2c3d\"]').click()"
        for size in ((1920, 1080), (390, 844)):
            ctx = {"stage": 0}

            def step(b, got):
                ev = lambda js: b.call("Runtime.evaluate", b.sid, expression=js, returnByValue=True)
                if ctx["stage"] == 0 and got.get("picks", 0) >= 3:  # the machine arrived: pick the other project
                    ev(pick)
                    ctx["stage"] = 1
                elif ctx["stage"] == 1 and "T-9001" in got.get("focus", "") and got.get("hash") == "#project=other-1b2c3d":
                    ctx["seen"] = dict(got)
                    b.call("Page.reload", b.sid)  # the pick survives a reload through the hash
                    ctx["stage"] = 2
                elif ctx["stage"] == 2 and "T-9001" in got.get("focus", "") and got.get("picks", 0) >= 3:
                    ctx["stage"] = 3
                    ev("history.back()")  # back goes to the default project: the hash is the one source of truth
            got, errs = self.watch(size, False, lambda g: ctx["stage"] == 3 and "T-2995" in g.get("focus", "")
                                   and g.get("hash", "x") == "", step=step, within=60)
            self.assertEqual(errs, [], f"console at {size}")
            self.assertEqual(ctx["stage"], 3, f"the pick, the reload and back all happened at {size}")
            seen = ctx["seen"]
            self.assertEqual((seen["sessions"], seen["projects"]), (3, 4), size)
            for word in (".config", "vault", "T-9001", "claude-opus-4-6", "41%", "verifying", "7"):
                self.assertIn(word, seen["rail"], word)
            for word in ("T-7777", "hush-model-9"):  # T-3024 review: a sensitive project's session shows its name only
                self.assertNotIn(word, seen["rail"], word)
                self.assertNotIn(word, seen["railhtml"], word)
            self.assertLessEqual(seen["wide"], size[0], f"no sideways scroll at {size}")
            self.assertLessEqual(got["wide"], size[0], f"no sideways scroll at {size}")
            reqs = got["reqs"]
            self.assertTrue([r for r in reqs if r.startswith("/api/state?project=other-1b2c3d")], reqs)
            self.assertTrue([r for r in reqs if r.startswith("/api/stream?project=other-1b2c3d")], reqs)
            self.assertTrue([r for r in reqs if r.startswith("/api/machine")], reqs)

    def test_an_unknown_project_in_the_hash_shows_unknown_not_old_data_and_stops_asking(self):  # T-3024 review
        ctx = {}

        def step(b, got):
            if "no project" in got.get("project", "") and "t" not in ctx:
                ctx["t"] = time.monotonic()
        got, errs = self.watch((1920, 1080), False, lambda g: "t" in ctx and time.monotonic() - ctx["t"] > 4,
                               step=step, hash="#project=nope-123456")
        self.assertEqual([e for e in errs if "404" not in e], [])
        self.assertIn("nope-123456", got["project"])
        self.assertIn("no project", got["project"])
        self.assertNotIn("T-2995", got["focus"])
        self.assertEqual((got["queue"], got["closes"], got["ticker"]), (0, 0, 0))
        streams = [r for r in got["reqs"] if r.startswith("/api/stream")]
        self.assertLessEqual(len(streams), 1, streams)  # the stream is closed after the 404, not retried

    def test_an_empty_hash_pins_the_project_and_a_reconnect_names_it(self):  # T-3024 review
        got, errs = self.watch((1920, 1080), False, self.back)
        self.assertEqual(errs, [])
        self.assertEqual(got["hash"], "")
        streams = [r for r in got["reqs"] if r.startswith("/api/stream")]
        self.assertEqual(streams[0], "/api/stream")
        self.assertTrue(all("project=" in r for r in streams[1:]) and len(streams) > 1, streams)

    def test_reduced_motion_lights_changes_and_sends_no_sparks(self):
        got, errs = self.watch((1920, 1080), True, lambda g: self.back(g) and g.get("lit", 0) > 0)
        self.assertEqual(errs, [])
        self.filled(got, (1920, 1080))
        self.assertEqual(got["motion"], "still")
        self.assertEqual(got["sparks"], 0)
        self.assertGreater(got["lit"], 0, "changes show as highlights")

    def test_the_controls_steer_through_api_act_by_mouse_and_keyboard(self):  # T-3026 part 2
        ctx = {"s": 0, "t": 0}
        FOREMAN, SESS = "foreman-4a056f", "e5f6a7b8"
        drive = f'#projects-list button[data-act^="drive_"][data-target="{FOREMAN}"]'
        cp = f'#projects-list button[data-act="checkpoint"][data-target="{FOREMAN}"]'
        note = f'#sessions-list button[data-act="note"][data-target="{SESS}"]'
        resume = f'#projects-list button[data-act="resume"][data-target="{FOREMAN}"]'

        def step(b, got):
            ev = lambda js: b.call("Runtime.evaluate", b.sid, expression=js, returnByValue=True)["result"].get("value")
            focus = lambda sel: ev(f"(document.querySelector({json.dumps(sel)}) || {{focus(){{}}}}).focus()")

            def key(k, code, vk, txt=""):
                for t in ("keyDown", "keyUp"):
                    b.call("Input.dispatchKeyEvent", b.sid, type=t, key=k, code=code, windowsVirtualKeyCode=vk,
                           **({"text": txt} if t == "keyDown" and txt else {}))
            enter = lambda: key("Enter", "Enter", 13, "\r")
            space = lambda: key(" ", "Space", 32, " ")
            status = lambda: ev("document.getElementById('act-status') ? document.getElementById('act-status').textContent : ''")
            posts, s = got["posts"], ctx["s"]
            if s == 0 and ev(f"!!document.querySelector({json.dumps(drive)}) && !!document.querySelector({json.dumps(note)})"):
                focus(f'#projects-list button.pick[data-project="{FOREMAN}"]')  # Tab from the plate's pick lands on a control
                key("Tab", "Tab", 9)
                ctx["tab"] = ev("(document.activeElement.dataset||{}).act || ''")
                ctx["nodrive"] = ev("document.querySelectorAll('#projects-list [data-target=\"broken-000000\"][data-act^=\"drive_\"]').length")
                ctx["fallback_acts"] = ev("document.querySelectorAll('#projects-list [data-target=\"broken-000000\"]').length")
                ctx["private"] = ev("document.querySelectorAll('.plate.private [data-act]').length")
                ctx["role"] = ev("document.getElementById('act-status').getAttribute('role')")
                ctx["live"] = ev("document.getElementById('act-status').getAttribute('aria-live')")
                ctx["pressed"] = ev(f"document.querySelector({json.dumps(drive)}).getAttribute('aria-pressed')")
                focus(drive); enter(); ctx["s"] = 1
            elif s == 1 and len(posts) >= 1:
                ctx["status1"] = status(); focus(cp); space(); ctx["s"] = 2
            elif s == 2 and len(posts) >= 2:  # a double press on resume starts one run: the button is disabled in flight
                ctx["dbl"] = ev(f"(() => {{ const r = document.querySelector({json.dumps(resume)}); r.click(); r.click(); "
                                "return r.disabled; })()")
                ctx["s"] = 21
            elif s == 21 and len(posts) >= 3 and ev(f"!document.querySelector({json.dumps(resume)}).disabled"):
                ctx["n_resume"] = len(posts)
                focus("#act-pause"); enter()  # the first press only arms it
                ctx["armed"] = ev("document.getElementById('act-pause').textContent")
                ctx["n_armed"] = len(posts); ctx["t"] = time.monotonic(); ctx["s"] = 3
            elif s == 3 and time.monotonic() - ctx["t"] > 4.8:
                ctx["disarmed"] = ev("document.getElementById('act-pause').textContent")
                ctx["n_disarmed"] = len(posts)
                focus("#act-pause"); enter(); enter()  # a double press (key repeat) arms but cannot confirm
                ctx["fast"] = ev("document.getElementById('act-pause').textContent")
                ctx["t"] = time.monotonic(); ctx["s"] = 31
            elif s == 31 and time.monotonic() - ctx["t"] > 0.9:
                ctx["n_fast"] = len(posts)
                focus("#act-pause"); enter(); ctx["s"] = 4  # after the gap the second press confirms
            elif s == 4 and len(posts) >= 4:
                ctx["status4"] = status(); focus("#act-pause-off"); space(); ctx["s"] = 5
            elif s == 5 and len(posts) >= 5:
                focus(note); enter()  # opens the field; Escape cancels it
                ctx["field"] = ev("!!document.querySelector('#sessions-list input')")
                key("Escape", "Escape", 27)
                ctx["gone"] = ev("!document.querySelector('#sessions-list input')")
                ctx["back"] = ev(f"document.activeElement === document.querySelector({json.dumps(note)})")
                focus(note); enter(); ctx["s"] = 6
            elif s == 6 and ev("!!document.querySelector('#sessions-list input')"):
                focus("#sessions-list input"); b.call("Input.insertText", b.sid, text="hello there"); enter(); ctx["s"] = 7
            elif s == 7 and len(posts) >= 6:
                focus(note); enter(); ctx["s"] = 8
            elif s == 8 and ev("!!document.querySelector('#sessions-list input')"):
                focus("#sessions-list input"); b.call("Input.insertText", b.sid, text="forbid"); enter(); ctx["s"] = 9
            elif s == 9 and "reload" in status().lower():
                ctx["status9"] = status()  # a refused note keeps its field and text
                ctx["kept"] = ev("(document.querySelector('#sessions-list input') || {}).value")
                ctx["s"] = 10

        got, errs = self.watch((1920, 1080), False, lambda g: ctx["s"] == 10, step=step, within=70)
        self.assertEqual([e for e in errs if "403" not in e], [])  # the forced 403 is the browser's own log line
        self.assertEqual(ctx["s"], 10, ctx)
        self.assertEqual(ctx["tab"], "drive_off", "Tab from a plate's pick reaches its first control")  # foreman drive is on
        self.assertEqual((ctx["role"], ctx["live"], ctx["pressed"]), ("status", "polite", "true"))
        self.assertEqual(ctx["private"], 0, "a sensitive project's plate has no controls")
        posts = got["posts"]
        self.assertEqual(len(posts), 7, posts)
        self.assertEqual((ctx["nodrive"], ctx["fallback_acts"]), (0, 2), "a row without a drive state has no drive toggle")
        self.assertTrue(ctx["dbl"], "the pressed button is disabled in flight")
        self.assertEqual(ctx["n_resume"], 3, "two presses on resume ran one")
        self.assertIn("press again", ctx["fast"])
        self.assertEqual(ctx["n_fast"], 3, "a double press cannot confirm pause now")
        self.assertEqual(ctx["kept"], "forbid", "a refused note keeps its text")
        self.assertEqual(ctx["status9"].lower().count("reload"), 1, ctx["status9"])
        for p in posts:
            self.assertEqual((p["path"], p["type"], p["nonce"]), ("/api/act", "application/json", got["token"]))
        self.assertEqual([p["body"] for p in posts], [
            {"action": "drive_off", "project": FOREMAN}, {"action": "checkpoint", "project": FOREMAN},
            {"action": "resume", "project": FOREMAN}, {"action": "pause_now", "confirmed": True}, {"action": "pause_off"},
            {"action": "note", "session": SESS, "text": "hello there"}, {"action": "note", "session": SESS, "text": "forbid"}])
        self.assertIn("drive", ctx["status1"].lower())
        self.assertEqual(ctx["n_armed"], 3, "one press of pause now sends nothing")
        self.assertIn("press again", ctx["armed"])
        self.assertEqual((ctx["disarmed"], ctx["n_disarmed"]), ("pause now", 3), "it disarms by itself")
        self.assertIn("pause", ctx["status4"].lower())
        self.assertEqual((ctx["field"], ctx["gone"], ctx["back"]), (True, True, True), "Esc cancels the note field")
        self.assertIn("reload", ctx["status9"].lower())
        self.assertIn("bad nonce", ctx["status9"])
        self.assertEqual(got["hash"], "", "a control press never switches the project")

    def netsee(self, g):
        return bool(g.get("net")) and g["net"]["nodes"] > 0 and g["net"]["painted"] and self.back(g)

    def test_the_network_view_draws_three_columns_and_pulses_on_a_new_flow_at_both_widths(self):  # T-3034
        for size in ((1440, 900), (390, 844)):
            ctx = {}

            def step(b, got):
                if got.get("net") and got["net"]["nodes"] > 0 and "p0" not in ctx:
                    ctx["p0"] = got["net"]["pulses"]  # what the first snapshot started; the stream adds more
            got, errs = self.watch(size, False, lambda g: self.netsee(g) and g["net"]["pulses"] > ctx.get("p0", 99), motion=True,
                                   step=step, hash="#view=net")
            self.assertEqual(errs, [], f"console at {size}")
            net = got["net"]
            self.assertEqual(got["view"], "net")
            self.assertTrue(net["shown"], size)
            self.assertGreaterEqual(net["size"][1], 300, size)
            self.assertLessEqual(got["wide"], size[0], f"no sideways scroll at {size}")
            agents, tools, models = net["cols"]
            for lab in ("T-2995", "T-2999", "T-2961", "T-9001", "private"):  # the focus, the lanes, the machine's sessions
                self.assertIn(lab, agents, size)
            for lab in ("exam run", "jeff train", "card grow", "audit prep"):  # running jobs and the verbs that called
                self.assertIn(lab, tools, size)
            for lab in ("haiku", "qwen3:14b", "claude"):
                self.assertIn(lab, models, size)
            self.assertTrue(all(len(c) <= 9 for c in net["cols"]), "a column is capped at 8 nodes and a +N group")
            self.assertTrue(any(m.startswith("+") for m in models), "the models past the cap are grouped")
            self.assertGreater(net["edges"], 4)
            self.assertGreater(net["pulses"], ctx["p0"], f"a new flow on the stream fired a pulse at {size}")
            self.assertEqual(got["hash"], "#view=net")
            self.assertGreater(got["filled"]["ticker"], 0, "the ledger keeps working in this view")
            self.assertEqual(got["ticker"], len(self.last["events"]), size)

    def test_the_net_reads_as_several_agents_not_one_hub_and_lists_its_edges_as_text(self):  # T-3034 review
        got, errs = self.watch((1440, 900), False, lambda g: self.netsee(g) and len(g["netlinks"]) > 4, hash="#view=net")
        self.assertEqual(errs, [])
        links = got["netlinks"]
        self.assertIn("network", got["stagelabel"].lower(), "the stage names the view it shows")
        self.assertTrue(any(l.startswith("T-2999 to opus") for l in links), links)  # a caller named by its T-id
        self.assertFalse([l for l in links if " to moon funnel" in l or " to card grow" in l or " to unknown" in l],
                         "a flow with no session id draws no agent edge, and no tool is called 'session' by default")
        self.assertGreaterEqual(len({l.split(" to ")[0] for l in links if l.split(" to ")[0].startswith("T-")}), 3)
        self.assertTrue(any("moon funnel to qwen3:14b" in l and "call" in l for l in links), "who called what, how many")
        tools = got["net"]["cols"][1]
        running = {"moon funnel", "card grow", "exam run", "jeff train", "bench embed"}
        self.assertEqual(set(tools[:5]), running, "running jobs come first, so none falls into the group")

    def test_the_net_redraws_only_while_something_moves(self):  # T-3034 review
        seen = []

        def step(b, got):
            if got.get("net") and got["net"]["painted"]:
                seen.append((time.monotonic(), got["net"]["frames"]))
        t0 = time.monotonic()

        def idle(g):  # some 1 s window drew at most 25 frames (60 fps is 60)
            return any(t2 - t1 >= 1 and f2 - f1 <= 25 for i, (t1, f1) in enumerate(seen) for t2, f2 in seen[i + 1:])
        got, errs = self.watch((1440, 900), False, idle, step=step, hash="#view=net", within=60, motion=True)
        self.assertEqual(errs, [])
        self.assertTrue(idle(got), seen[-6:])

    def test_a_phone_pressing_network_scrolls_the_net_into_view(self):  # T-3034 review
        ctx = {}

        def step(b, got):
            if "clicked" not in ctx and got.get("picks", 0) >= 3 and got.get("net"):
                ctx["top0"] = got["stagetop"]
                b.call("Runtime.evaluate", b.sid, expression="document.getElementById('view-net').click()")
                ctx["clicked"] = 1
        got, errs = self.watch((390, 844), False, lambda g: "clicked" in ctx and g["net"]["shown"] and g["stagetop"] < ctx["top0"] - 100,
                               step=step, within=40)
        self.assertEqual(errs, [])
        self.assertLess(got["stagetop"], ctx["top0"] - 100, "it scrolled the stage up")

    def test_the_net_labels_have_a_halo_a_width_fit_and_a_legend(self):  # T-3034 review
        js, html, css = text("live.js"), text("index.html"), text("live.css")
        self.assertIn("strokeText", js)
        self.assertIn("measureText", js[js.index("function netDraw") - 900:])
        self.assertIn("rgba(HEADC, modern ? 1 : 0.7)", js)  # T-3054 review: modern headings at full alpha (4.5:1)
        self.assertIn("title", js[js.index("function netHover"):][:900])
        self.assertRegex(html, r"thickness[^<]*calls[^<]*running[^<]*dashed|dashed[^<]*running")
        self.assertIn("scroll-snap-type:x proximity", css)

    def test_the_toggle_keeps_the_view_in_the_hash_beside_the_project_and_remembers_it(self):  # T-3034
        ctx = {"s": 0}

        def step(b, got):
            ev = lambda js: b.call("Runtime.evaluate", b.sid, expression=js, returnByValue=True)["result"].get("value")
            s = ctx["s"]
            if s == 0 and got.get("picks", 0) >= 3:
                ev("document.getElementById('view-net').click()"); ctx["s"] = 1
            elif s == 1 and got.get("view") == "net":
                ctx["hash1"], ctx["store1"] = got["hash"], got["store"]
                ev("document.querySelector('#projects-list button[data-project=\"other-1b2c3d\"]').click()"); ctx["s"] = 2
            elif s == 2 and "T-9001" in got.get("focus", "") and got.get("view") == "net":
                ctx["hash2"], ctx["streams2"] = got["hash"], len([r for r in got["reqs"] if r.startswith("/api/stream")])
                b.call("Page.reload", b.sid); ctx["s"] = 3
            elif s == 3 and got.get("picks", 0) >= 3 and "T-9001" in got.get("focus", ""):
                ctx["view3"] = got["view"]
                ev("document.getElementById('view-forge').click()"); ctx["s"] = 4
            elif s == 4 and got.get("view") == "forge":
                ctx["hash4"], ctx["store4"], ctx["pressed"] = got["hash"], got["store"], ev(
                    "document.getElementById('view-forge').getAttribute('aria-pressed')")
                ctx["scene"] = got["scene"]; ctx["s"] = 5
        got, errs = self.watch((1440, 900), False, lambda g: ctx["s"] == 5, step=step, within=70)
        self.assertEqual(errs, [])
        self.assertEqual(ctx["s"], 5, ctx)
        self.assertEqual((ctx["hash1"], ctx["store1"]), ("#view=net", "net"))
        self.assertEqual(ctx["hash2"], "#project=other-1b2c3d&view=net", "a project pick keeps the view")
        self.assertEqual(ctx["streams2"] <= 3, True, "switching views never reconnects the stream: only the project pick does")
        self.assertEqual(ctx["view3"], "net", "the hash survives a reload")
        self.assertEqual(ctx["hash4"], "#project=other-1b2c3d", "forge drops view= and keeps the project")
        self.assertEqual((ctx["store4"], ctx["pressed"]), ("forge", "true"))
        self.assertGreaterEqual(ctx["scene"][1], 300, "the forge scene is back")

    def test_the_view_is_remembered_without_a_hash(self):  # T-3034
        ctx = {"s": 0}

        def step(b, got):
            ev = lambda js: b.call("Runtime.evaluate", b.sid, expression=js, returnByValue=True)["result"].get("value")
            if ctx["s"] == 0 and got.get("picks", 0) >= 3:
                ev("document.getElementById('view-net').click()"); ctx["s"] = 1
            elif ctx["s"] == 1 and got.get("view") == "net":
                ev("history.replaceState(null, '', '/')"); b.call("Page.reload", b.sid); ctx["s"] = 2
            elif ctx["s"] == 2 and got.get("view") == "net" and got.get("picks", 0) >= 3:
                ctx["s"] = 3
        got, errs = self.watch((1440, 900), False, lambda g: ctx["s"] == 3, step=step, within=60)
        self.assertEqual(errs, [])
        self.assertEqual((ctx["s"], got["store"]), (3, "net"))

    def test_reduced_motion_in_the_network_view_starts_no_pulses_and_lights_the_edges(self):  # T-3034
        got, errs = self.watch((1440, 900), True, lambda g: self.netsee(g) and g["net"]["lit"] > 0, hash="#view=net")
        self.assertEqual(errs, [])
        self.assertEqual(got["net"]["pulses"], 0)
        self.assertGreater(got["net"]["lit"], 0, "a called edge is lit")
        self.assertEqual(got["motion"], "still")

    def test_network_shots(self):  # T-3034: the two pictures for a human; skipped unless asked for
        out = os.environ.get("SAMPO_NET_SHOTS") or os.environ.get("NET_SHOTS")  # a Claude Code shell strips SAMPO_*
        if not out:
            self.skipTest("SAMPO_NET_SHOTS=dir (or NET_SHOTS=dir) saves t3034-desk.png and t3034-phone.png")
        from sampo import shot
        for size, name in (((1440, 900), "t3034-desk.png"), ((390, 844), "t3034-phone.png")):
            srv = serve()
            b = shot.shared(self.browser, size, reduced=False)
            try:
                shot.load(b, f"http://127.0.0.1:{srv.server_address[1]}/#view=net")
                b.wait("live-test.never", 6)
                with open(os.path.join(out, name), "wb") as f:
                    f.write(b.png())
            finally:
                b.close(); srv.stop.set(); srv.shutdown(); srv.server_close()

    # ---- T-3055: the modern theme is the page's default; the forge look is a setting ----
    FORGE_WORDS = re.compile(r"furnace|anvil|ember|quench|struck|lantern|banked", re.I)

    def modern(self, size, scheme="dark", **kw):
        kw.setdefault("hash", "#view=net")  # T-3659: the board is the default; these look at the network, one click away
        got, errs = self.watch(size, True, lambda g: g.get("net") and g["net"]["painted"] and g.get("ticker", 0) > 0,
                               theme=None, scheme=scheme, **kw)
        self.assertEqual(errs, [], f"console at {size}")
        return got

    def test_modern_is_the_default_a_clean_dashboard_with_the_network_as_hero_in_dark_and_light(self):
        for size in ((1440, 900), (390, 844)):
            for scheme in ("dark", "light"):
                where = f"{size} {scheme}"
                got = self.modern(size, scheme)
                self.assertEqual(got["theme"], "modern", where)
                self.assertEqual(got["view"], "net", where)
                self.assertTrue(got["shown"]["net"] and not got["shown"]["scene"], f"the net is the hero, no scene {where}")
                self.assertTrue(got["shown"]["views"] and got["shown"]["view-board"], f"Board|Network {where}")
                self.assertFalse(got["shown"]["view-forge"] or got["shown"]["board"], f"no Forge button without a scene {where}")
                self.assertTrue(got["shown"]["theme-btn"], where)
                self.assertEqual(got["themepressed"], "false", where)
                self.assertEqual(got["themelabel"], "Forge look", f"one fixed name, pressed = forge on {where}")
                self.assertEqual(got["tankflat"], "none", f"flat tank segments {where}")
                lin = lambda h: [(int(h[i:i + 2], 16) / 255) for i in (1, 3, 5)]
                lum2 = lambda h: sum(w * (c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4) for w, c in zip((.2126, .7152, .0722), lin(h)))
                for k, hx in got["ktok"].items():  # kind text is AA on the plate and on the page ground
                    for ground in (got["plate"], got["night"]):
                        a, b = sorted((lum2(hx), lum2(ground)))
                        self.assertGreaterEqual((b + .05) / (a + .05), 4.5, f"--k-{k} {hx} on {ground} {where}")
                if size[0] < 700:
                    self.assertLess(got["nettop"], 340, f"the network starts near the top on a phone {where}")
                t = got["text"].replace("Forge look", "")
                m = self.FORGE_WORDS.search(t)
                self.assertIsNone(m, f"{where} ...{repr(t[max(0, m.start() - 70):m.end() + 40]) if m else ''}")  # the toggle names the other look
                self.assertNotIn("forge", got["title"].lower(), where)
                self.assertNotRegex(got["fonts"].lower(), r"bodoni|didot|georgia|martian", f"no serif, no forge faces {where}")
                self.assertRegex(got["fonts"].lower(), r"system-ui|sans-serif", where)
                for panel in PANELS[1:]:
                    self.assertGreater(got["filled"].get(panel, -1), 0, f"{panel} {where}")
                self.assertGreaterEqual(got["net"]["nodes"], 3, where)
                self.assertGreater(got["net"]["size"][1], 280, f"the hero has room {where}")
                self.assertLessEqual(got["wide"], size[0], f"no sideways scroll {where}")
                lum = lambda rgb: sum(map(int, re.findall(r"\d+", rgb)[:3])) / 3
                if scheme == "light":
                    self.assertGreater(lum(got["bg"]), 200, f"a light page {where}")
                    self.assertLess(lum(got["ink"]), 90, where)
                else:
                    self.assertLess(lum(got["bg"]), 50, f"a dark page {where}")
                    self.assertGreater(lum(got["ink"]), 200, where)

    def test_the_charts_are_drawn_from_one_kit_labelled_and_readable_in_both_schemes(self):  # T-3092
        lin = lambda h: [(int(h[i:i + 2], 16) / 255) for i in (1, 3, 5)]
        lum2 = lambda h: sum(w * (c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4) for w, c in zip((.2126, .7152, .0722), lin(h)))
        for size in ((1440, 900), (390, 844)):
            for scheme in ("dark", "light"):
                where = f"{size} {scheme}"
                got, errs = self.watch(size, True, lambda g: g.get("net") and g["net"]["painted"] and any("430 of" in t for t in g.get("pgt", [])),  # the last snapshot has landed
                                       theme=None, scheme=scheme, hash="#view=net")
                self.assertEqual(errs, [], where)
                for id, c in got["charts"].items():
                    self.assertTrue(c["shown"] and c["svg"], f"{id} drawn {where} {c}")
                    self.assertEqual(c["role"], "img", id)
                    self.assertGreater(len(c["label"]), 12, f"{id} has a text equivalent {where}")
                self.assertGreaterEqual(got["charts"]["ch-spend"]["texts"], 6, "a spend chart has axes: y and x labels")
                self.assertEqual(got["charts"]["ch-spend"]["dots"], 1, "the latest point is emphasised")
                self.assertEqual(got["charts"]["ch-spend"]["area"], 1, where)
                self.assertRegex(got["charts"]["ch-funnel"]["label"], r"\d", "the funnel's words carry its counts")
                self.assertGreaterEqual(got["charts"]["ch-gpu"]["segs"], 1, "GPU memory is a stacked bar")
                self.assertFalse(got["tankshown"], "no tank in the modern look")
                self.assertGreaterEqual(got["legs"], 3, "legends")
                m = re.findall(r"\d+", got["charts"]["ch-spend"]["fill"])
                self.assertEqual(len(m), 3, got["charts"]["ch-spend"]["fill"])
                txt = "#%02x%02x%02x" % tuple(map(int, m))
                a, b = sorted((lum2(txt), lum2(got["plate"])))
                self.assertGreaterEqual((b + .05) / (a + .05), 4.5, f"chart text {txt} on {got['plate']} {where}")
                joined = " | ".join(got["pgt"])
                self.assertRegex(joined, r"35% · 430 of 1220 cards · 0\.5/s|35% · 430 of 1,220 cards", where)
                self.assertIn("ETA 26m", joined, where)
                self.assertIn("no progress reported", joined, where)
                self.assertIn("2 of 5 steps", joined, f"a lane's steps show on its task {where}")
                self.assertRegex(joined, r"396 of 1,?200 steps · 15/min · ETA 54m · loss 0\.401", f"a training run's bar {where}")  # T-3044
                self.assertIn("2 of 5 steps · executing", joined, f"an active task's stage {where}")
                self.assertIn("1 of 4 steps · verifying", joined, where)
                if scheme == "light":
                    self.assertIsNotNone(got["netbg"], where)
                    self.assertGreater(sum(got["netbg"][:3]) / 3, 200, f"the network canvas is light too {where}")
                else:
                    self.assertLess(sum(got["netbg"][:3]) / 3, 80, where)

    def test_forge_shows_steps_stage_rate_and_eta_too(self):  # T-3044 review: they were modern-only
        got, errs = self.watch((1440, 900), True, lambda g: g.get("nodes", 0) > 0 and any("430 of" in t for t in g.get("pgt", [])), theme="forge")
        self.assertEqual(errs, [])
        joined = " | ".join(got["pgt"])
        self.assertRegex(joined, r"396 of 1,?200 steps · 15/min · ETA 54m · loss 0\.401")
        self.assertIn("2 of 5 steps · executing", joined)
        self.assertIn("1 of 4 steps · verifying", joined)
        self.assertIn("T-2999 2/5 · executing", got["benches"], "a lane's bench carries its steps and stage")

    def test_forge_hides_the_charts_and_keeps_its_tank(self):  # T-3092
        got, errs = self.watch((1440, 900), True, lambda g: g.get("nodes", 0) > 0 and g.get("ticker", 0) > 0, theme="forge")
        self.assertEqual(errs, [])
        self.assertFalse(got["charts"]["ch-calls"]["shown"])
        self.assertTrue(got["tankshown"])

    def test_forge_brings_back_the_scene_the_words_and_the_serif(self):
        for size in ((1440, 900), (390, 844)):
            got, errs = self.watch(size, True, lambda g: g.get("nodes", 0) > 0 and g.get("ticker", 0) > 0, theme="forge")
            self.assertEqual(errs, [], size)
            self.assertEqual(got["theme"], "forge")
            self.assertEqual(got["view"], "forge")
            self.assertTrue(got["shown"]["scene"] and got["shown"]["views"], size)
            self.assertEqual(got["themepressed"], "true")
            self.assertEqual(got["themelabel"], "Forge look")
            if size[0] < 700:
                self.assertLessEqual(got["barh"], 150, "the forge header keeps its rows on a phone")
            for word in ("Furnaces", "At the anvil", "Ledger", "burning"):
                self.assertIn(word.lower(), got["text"].lower(), f"{word} {size}")  # innerText carries the CSS caps
            self.assertRegex(got["fonts"], r"Bodoni Moda", size)
            self.assertIn("forge", got["title"])
            self.assertLessEqual(got["wide"], size[0], f"no sideways scroll {size}")

    def test_the_header_toggle_keeps_the_viewers_choice_across_a_reload(self):
        ctx = {"s": 0}

        def step(b, got):
            ev = lambda js: b.call("Runtime.evaluate", b.sid, expression=js, returnByValue=True)["result"].get("value")
            if ctx["s"] == 0 and got.get("board") and got["board"]["painted"]:
                ctx["first"] = got["theme"]; ev("document.getElementById('theme-btn').click()"); ctx["s"] = 1
            elif ctx["s"] == 1 and got.get("theme") == "forge" and got.get("nodes", 0) > 0:
                ctx["second"], ctx["kept"] = got["stheme"], got["stheme"]; ev("document.getElementById('theme-btn').click()"); ctx["s"] = 2
            elif ctx["s"] == 2 and got.get("theme") == "modern" and got["board"] and got["board"]["painted"] and got["stheme"] == "modern" and got["say"]:
                ctx["after"] = (got["active"], got["say"]); ctx["s"] = 3
        got, errs = self.watch((1440, 900), True, lambda g: ctx["s"] == 3, step=step, theme=None)
        self.assertEqual(errs, [])
        self.assertEqual((ctx.get("first"), ctx.get("second"), ctx["s"]), ("modern", "forge", 3), ctx)
        self.assertEqual(ctx["after"], ("theme-btn", "Forge look off"), "focus returns to the toggle and the switch is announced")

    def test_modern_keeps_the_rail_the_actions_and_the_stream_and_a_pick_switches_the_project(self):
        ctx = {"s": 0}

        def step(b, got):
            ev = lambda js: b.call("Runtime.evaluate", b.sid, expression=js, returnByValue=True)["result"].get("value")
            if ctx["s"] == 0 and got.get("picks", 0) >= 3:
                ev("document.getElementById('act-pause').focus()")
                for t in ("keyDown", "keyUp"):  # the keyboard: Enter on a focused control arms it (pause now confirms twice)
                    b.call("Input.dispatchKeyEvent", b.sid, type=t, key="Enter", code="Enter", windowsVirtualKeyCode=13,
                           **({"text": "\r"} if t == "keyDown" else {}))
                ctx["armed"] = ev("document.getElementById('act-pause').className")
                ev("document.querySelector('#projects-list button.pick[data-project=\"other-1b2c3d\"]').click()")
                ctx["s"] = 1
        got, errs = self.watch((1440, 900), True, lambda g: ctx["s"] == 1 and "other-1b2c3d" in g.get("hash", "")
                               and "The other project" in g.get("focus", "") and g.get("net") and g["net"]["painted"],
                               step=step, theme=None, hash="#view=net")
        self.assertEqual(errs, [])
        self.assertEqual(got["view"], "net", "the pick keeps the net")
        self.assertIn("other-1b2c3d", got["hash"])
        self.assertTrue([r for r in got["reqs"] if r.startswith("/api/stream?project=other-1b2c3d")], got["reqs"])

    # T-3659: one test per size, so the runner shoots the three in parallel (six loads in one test took 228 s under load)
    def test_the_board_is_the_modern_default_nine_painted_panels_in_three_columns_on_a_desktop(self):
        self.board_at((1600, 1000), 3, motion=True)

    def test_the_board_takes_two_columns_on_a_tablet(self):
        self.board_at((900, 1100), 2)

    def test_the_board_takes_one_column_on_a_phone(self):
        self.board_at((390, 844), 1)

    def board_at(self, size, cols, motion=False):  # T-3715: the desktop run checks the loop (a short window); the others the layout, still
        for scheme in ("dark", "light"):
            where = f"{size} {scheme}"
            got, errs = self.watch(size, False, lambda g: g.get("board") and g["board"]["painted"] and (g["board"]["frames"] > 3 or not motion),
                                   theme=None, scheme=scheme, motion=motion)
            self.assertEqual(errs, [], where)
            bd = got["board"]
            self.assertEqual((got["theme"], got["view"]), ("modern", "board"), where)
            self.assertTrue(got["shown"]["board"] and got["shown"]["views"] and got["shown"]["view-board"], where)
            self.assertFalse(got["shown"]["net"] or got["shown"]["view-forge"] or got["shown"]["rail"], f"the board alone {where}")
            self.assertEqual([p["k"] for p in bd["panels"]], ["lanes", "train", "cpu", "loop", "library", "models", "sampo", "state", "tests"])
            for i, p in enumerate(bd["panels"]):
                self.assertEqual(p["chip"], f"{i + 1:02}", where)
                self.assertTrue(p["title"] and p["label"] and p["foot"], f"{p} {where}")
                self.assertGreater(p["canvas"][0] * p["canvas"][1], 0, f"{p['k']} {where}")
                self.assertRegex(p["font"].lower(), r"martian|mono", where)
            self.assertEqual(bd["cols"], cols, where)
            self.assertEqual(bd["running"], "1" if motion else "0", where)
            self.assertLessEqual(got["wide"], size[0], f"no sideways scroll {where}")
            self.assertIsNone(self.FORGE_WORDS.search(got["text"].replace("Forge look", "")), where)
            self.assertNotIn("/home/", got["text"], where)

    def test_the_board_opens_in_all_when_two_projects_are_busy_with_chips_that_filter(self):  # T-3730
        base = json.loads(json.dumps(snaps()[-1]))
        lane = lambda slug, t: {"task": t, "title": "Task " + t, "state": "building", "steps": {"done": 1, "total": 3}, "more": 0,
                                "files": [], "project": slug}
        allsnap = dict(base, project={"slug": "all", "name": "All projects"}, all=True, projects=[
            {"slug": "shop-api", "name": "shop-api", "sensitive": False}, {"slug": "notes-app", "name": "notes-app", "sensitive": False}],
            board=dict(base["board"], lanes=[lane("shop-api", "T-1"), lane("notes-app", "T-2")], trains=[], loops=[]))
        ctx = {"s": 0}

        def step(b, got):
            ev = lambda js: b.call("Runtime.evaluate", b.sid, expression=js, returnByValue=True)["result"].get("value")
            chips = ev("document.querySelectorAll('#board .bd-chips button[data-chip]').length")
            if ctx["s"] == 0 and chips == 2:
                ctx["head"] = ev("document.getElementById('project').textContent")
                ctx["shown"] = ev("getComputedStyle(document.querySelector('#board .bd-chips')).display")
                ctx["colour"] = ev("document.querySelector('#board .bd-chips button').style.getPropertyValue('--pc')")
                ev("document.querySelector('#board .bd-chips button[data-chip=\"shop-api\"]').click()")
                ctx["s"] = 1
            elif ctx["s"] == 1:
                ctx["off"] = ev("document.querySelector('#board .bd-chips button[data-chip=\"shop-api\"]').getAttribute('aria-pressed')")
                ctx["s"] = 2

        got, errs = self.watch((1600, 1000), False, lambda g: ctx["s"] == 2, theme=None, all_snaps=[allsnap], step=step)
        self.assertEqual(errs, [])
        self.assertEqual(ctx["s"], 2, "two project chips drawn")
        self.assertIn("all projects", ctx["head"])
        self.assertEqual(ctx["shown"], "flex")
        self.assertTrue(ctx["colour"], "each chip carries its project's colour")
        self.assertEqual(ctx["off"], "false", "a pressed chip hides its project")
        self.assertTrue([r for r in got["reqs"] if r.startswith("/api/state?project=all")], got["reqs"])

    def test_the_board_holds_still_when_asked_and_when_the_tab_is_hidden(self):  # T-3659
        got, errs = self.watch((1600, 1000), True, lambda g: g.get("board") and g["board"]["painted"] and self.back(g), theme=None)
        self.assertEqual(errs, [])
        self.assertEqual(got["board"]["running"], "0", "reduced motion: one frame per change, no loop")
        self.assertLess(got["board"]["frames"], 40, got["board"])
        ctx = {"s": 0}

        def step(b, got):
            ev = lambda js: b.call("Runtime.evaluate", b.sid, expression=js, returnByValue=True)["result"].get("value")
            bd = got.get("board") or {}
            if ctx["s"] == 0 and bd.get("painted") and bd.get("running") == "1":
                ev("document.getElementById('motion').click()")
                ctx["s"] = 1
            elif ctx["s"] == 1 and bd.get("running") == "0":
                ctx["still"], ctx["t"] = bd["frames"], time.monotonic()
                ctx["s"] = 2
            elif ctx["s"] == 2 and time.monotonic() - ctx["t"] > 1.5:
                ctx["still2"] = bd["frames"]
                ev("document.getElementById('motion').click()")
                ctx["s"] = 3
            elif ctx["s"] == 3 and bd.get("running") == "1":
                ev("Object.defineProperty(document, 'hidden', {value: true, configurable: true});"
                   "Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});"
                   "document.dispatchEvent(new Event('visibilitychange'))")
                ctx["s"] = 4
            elif ctx["s"] == 4 and bd.get("running") == "0":
                ctx["hid"], ctx["t"] = bd["frames"], time.monotonic()
                ctx["s"] = 5
            elif ctx["s"] == 5 and time.monotonic() - ctx["t"] > 1.5:
                ctx["hid2"] = bd["frames"]
                ctx["s"] = 6
        got, errs = self.watch((1600, 1000), False, lambda g: ctx["s"] == 6, step=step, theme=None, motion=True)
        self.assertEqual(errs, [])
        self.assertEqual(ctx["s"], 6, ctx)
        self.assertLessEqual(ctx["still2"] - ctx["still"], 3, f"Still draws a frame per change, not 60 a second {ctx}")
        self.assertEqual(ctx["hid2"], ctx["hid"], f"a hidden tab draws nothing {ctx}")

    def test_the_theme_file_and_the_two_looks_are_in_the_page(self):
        page, css, js = text("index.html"), text("live.css"), text("live.js")
        self.assertIn('data-theme="modern"', page, "modern is what the file says by default")
        self.assertRegex(page, r'<script src="/static/theme\.js" nonce="__NONCE__"></script>', "applied before the first paint")
        self.assertLess(page.index("theme.js"), page.index("live.css"))
        self.assertIn('id="theme-btn"', page)
        self.assertIn('[data-theme="modern"]', css)
        self.assertIn("sampo-live-theme", text("theme.js"))
        self.assertIn("sampo-live-theme", js)
        self.assertRegex(css, r"prefers-color-scheme:\s*light")

    def test_theme_shots(self):  # T-3055: the pictures for a human; skipped unless asked for
        out = os.environ.get("THEME_SHOTS")
        if not out:
            self.skipTest("THEME_SHOTS=dir saves t3055-{modern,forge}-{1440,390}-{dark,light}.png")
        from sampo import shot
        for theme in ("modern", "forge"):
            for scheme in ("dark", "light"):
                for w, h in ((1440, 900), (390, 844)):
                    srv = serve()
                    b = shot.shared(self.browser, (w, h), reduced=False)
                    try:
                        b.call("Emulation.setEmulatedMedia", b.sid, features=[{"name": "prefers-color-scheme", "value": scheme}])
                        b.call("Page.addScriptToEvaluateOnNewDocument", b.sid,
                               source=f"try {{ localStorage.setItem('sampo-live-theme', '{theme}'); }} catch (e) {{}}")
                        shot.load(b, f"http://127.0.0.1:{srv.server_address[1]}/")
                        b.wait("live-test.never", 6)
                        with open(os.path.join(out, f"t3055-{theme}-{w}-{scheme}.png"), "wb") as f:
                            f.write(b.png())
                    finally:
                        b.close(); srv.stop.set(); srv.shutdown(); srv.server_close()

    def test_the_controls_fit_a_phone(self):
        got, errs = self.watch((390, 844), False, lambda g: g.get("picks", 0) >= 3)
        self.assertEqual(errs, [])
        self.assertLessEqual(got["wide"], 390)


if __name__ == "__main__":
    unittest.main()
