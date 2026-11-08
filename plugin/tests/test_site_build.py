"""T-1541: the site builds byte for byte the same twice from one commit; each recorded fm code run becomes a case-study
page; the Releases page has a wall of generated covers; a task proved red then green gets a title card."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import PLUGIN, ForemanTestCase, headless_chrome, read_text

ROOT = os.path.dirname(PLUGIN)


def site(name):
    sys.path.insert(0, os.path.join(ROOT, "site"))
    try:
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "site", name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.pop(0)


RUN = [{"event": "start", "request": "FIX: <b>total</b> ignores qty", "worker": "claude", "ts": 100.0},
       {"event": "plan", "plan": {"type": "FIX", "tier": "S", "title": "total() counts qty", "steps": ["test", "fix"],
                                  "criteria": [{"text": "qty counts", "verify": "python3 -m unittest"}]}, "ts": 101.0},
       {"event": "verify", "criterion": 1, "label": "before", "exit": 1, "cmd": "python3 -m unittest", "ts": 102.0},
       {"event": "step", "step": 1, "text": "step 1: write the test", "ts": 103.0},
       {"event": "changed", "step": 1, "files": ["cart.py"], "stat": {"cart.py": [1, 1]}, "ts": 110.0},
       {"event": "verify", "criterion": 1, "label": "verify", "exit": 0, "cmd": "python3 -m unittest", "ts": 170.0},
       {"event": "closed", "task": "T-0001", "text": "T-0001 closed; committed abc1234", "ts": 171.0}]


class Reproducible(unittest.TestCase):
    def test_two_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            outs = [os.path.join(tmp, d) for d in ("a", "b")]
            for out in outs:
                p = subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), out], capture_output=True,
                                   text=True, timeout=300)
                self.assertEqual(p.returncode, 0, p.stderr)
            files = [sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs)
                     for out in outs]
            self.assertEqual(files[0], files[1])
            self.assertTrue(any(f.startswith("stories" + os.sep) for f in files[0]))
            # T-3059: the source assets are links into design/; the published tree must hold real files
            for d, ds, fs in os.walk(outs[0]):
                for n in ds + fs:
                    self.assertFalse(os.path.islink(os.path.join(d, n)), f"{n} is published as a symlink")
            share = os.path.join(outs[0], "assets", "share.png")
            self.assertGreater(os.path.getsize(share), 10_000)
            with open(share, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
            for rel in files[0]:
                with open(os.path.join(outs[0], rel), "rb") as a, open(os.path.join(outs[1], rel), "rb") as b:
                    self.assertTrue(a.read() == b.read(), f"{rel} differs between two builds")


class SharedNav(unittest.TestCase):
    """T-2903: the landing, the tower and the section pages draw their main nav from one list in build.py."""

    def test_every_page_has_the_same_links_with_install_and_github(self):
        with tempfile.TemporaryDirectory() as out:
            p = subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), out], capture_output=True,
                               text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr)
            seen = {}
            for rel in ("index.html", "genesis/index.html", "cards.html", "features/benchmarks.html", "features/index.html"):
                head = re.search(r"<header.*?</header>", read_text(os.path.join(out, rel)), re.S).group(0)
                nav = re.search(r'<nav class="nav".*?</nav>', head, re.S).group(0)
                seen[rel] = set(re.findall(r">([^<]+)</a>", nav))
                landing = read_text(os.path.join(out, "index.html"))
                for href in re.findall(r'href="([^"]+)"', nav):  # every link lands on a file, and an anchor on a landing id
                    if "//" in href:
                        continue
                    path, _, anchor = href.partition("#")
                    target = os.path.normpath(os.path.join(out, os.path.dirname(rel), path)) if path else os.path.join(out, rel)
                    self.assertTrue(os.path.isfile(target), f"{rel}: {href} has no file")
                    if anchor:
                        self.assertEqual(os.path.basename(target), "index.html", f"{rel}: {href}")
                        self.assertIn(f'id="{anchor}"', landing, f"{rel}: {href} has no anchor on the landing")
                seen[rel] |= set(re.findall(r'class="gh"[^>]*>([^<]+)<', head))  # GitHub: its own button on every page
                self.assertLessEqual({"Install", "GitHub", "Cards", "Features", "Rune"}, seen[rel], rel)
            for rel in ("genesis/index.html", "features/benchmarks.html"):  # no menu on the landing: it has its own
                self.assertIn('<details class="menu">', read_text(os.path.join(out, rel)), rel)
            self.assertEqual(len({frozenset(v) for v in seen.values()}), 1, seen)
            self.assertIn('aria-current="page">Rune<', read_text(os.path.join(out, "genesis", "index.html")))


class ModelBench(unittest.TestCase):
    def test_the_models_section_shows_the_last_good_run_stale_beside_the_baselines(self):  # T-3093
        sys.path.insert(0, os.path.join(PLUGIN, "lib"))
        from sampo import louhi
        pages = site("pages")
        row = lambda run, gen, backend, acc, **kw: {"run": run, "gen": gen, "arm": "decision", "use": "intake-type",  # noqa: E731
                                                    "backend": backend, "accuracy": acc, "brier": 0.2, "p50_ms": 120,
                                                    "p95_ms": 900, "n": 250, "errors": 0, **kw}
        rows = [row("m1", 4, "kev", 0.8), row("m1", 4, "orchestrator", 0.6), row("m1", 4, "majority", 0.5, ceiling=0.9),
                row("m2", 5, "kev", 0.1, errors=200, n=3), {"arm": "skipped", "use": "*", "backend": "*", "gen": 6, "run": "m3"}]
        html = pages._model_bench({"model_bench": louhi.models_view(rows, {})})
        for want in ("80.0%", "stale since gen 5", "rules path", "majority baseline", "Noise ceiling 90.0%", "120 ms", "900 ms"):
            self.assertIn(want, html)
        self.assertNotIn("10.0%", html, "a failed run never replaces the last good one")
        self.assertIn("No model bench run yet", pages._model_bench({}))


class Stories(unittest.TestCase):
    def test_a_case_study_renders_from_a_fixture_ledger(self):
        build = site("build")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fix.jsonl")
            with open(path, "w") as f:
                f.write("".join(json.dumps(e) + "\n" for e in RUN))
            page = build.case_study_html("fix", "Fix a bug", build.load_example(path))
        self.assertIn("&lt;b&gt;total&lt;/b&gt; ignores qty", page)
        self.assertNotIn("<b>total</b>", page)
        self.assertIn('<li class="fail"><span class="at">2s</span> <b>before</b> fails: <code>python3 -m unittest', page)
        self.assertIn('<li class="pass"><span class="at">1m10s</span> <b>check</b> passes', page)
        self.assertIn("<b>changed</b> <code>cart.py</code>", page)
        self.assertIn("<b>closed</b> T-0001 closed; committed abc1234", page)

    def test_the_releases_wall_draws_a_cover_per_release(self):
        wall = site("pages")._wall({"releases": [{"version": "1.2.3", "day": "2026-01-01", "tests": 50},
                                                 {"version": "1.2.4", "day": "2026-01-02", "tests": 100}]})
        self.assertEqual(wall.count("<svg "), 2)
        self.assertIn('width="63.0"', wall)
        self.assertIn('width="126.0"', wall)
        self.assertEqual(site("pages")._wall({"releases": []}), "")


class Motion(unittest.TestCase):
    """T-1615: the bench and stats charts draw themselves once when seen, only with motion allowed and JavaScript on."""

    def test_charts_carry_their_motion_and_are_whole_without_it(self):
        build = site("build")
        self.assertIn('class="viz grow-x"', build.hbars("t", "", [("a", 1), ("b", 2)]))
        rows = [{"d": "x", "v": 1}, {"d": "y", "v": 3}]
        self.assertIn('class="viz draw"', build.line("t", "", rows, "v", "d"))
        self.assertIn('pathLength="1"', build.line("t", "", rows, "v", "d"))
        css, js = read_text(os.path.join(ROOT, "site", "style.css")), read_text(os.path.join(ROOT, "site", "site.js"))
        at = css.index("T-1615")
        self.assertIn("scaleX(0)", css[at:])
        self.assertTrue(css[:at].rstrip().endswith("@media (prefers-reduced-motion: no-preference) { /*"),
                        "the motion only where it's allowed")
        self.assertLess(js.index("if (still ||"), js.index('classList.add("armed")'), "armed after the still check")


class TitleCard(ForemanTestCase):
    def test_a_proved_task_gets_its_card_and_an_unproved_one_doesnt(self):
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "Totals count the <qty> of every line item", "--type", "FIX", "--tier",
                                 "S", "--ac", "it counts :: test -f proved.txt", "--step", "fix it", "--json").stdout)["id"]
        self.fm("focus", tid)
        p = self.fm("task", "card", tid, check=False)
        self.assertNotEqual(p.returncode, 0, "no proof yet")
        self.fm("task", "evidence", tid, "--step", "1", "--run", "test -f proved.txt", check=False)
        with open(os.path.join(self.repo, "proved.txt"), "w") as f:
            f.write("ok\n")
        self.fm("task", "evidence", tid, "--step", "1", "--run", "test -f proved.txt")
        out = os.path.join(self.tmp, "card.svg")
        self.fm("task", "card", tid, "--out", out)
        svg = read_text(out)
        self.assertTrue(svg.startswith("<svg "))
        self.assertIn("&lt;qty&gt;", svg)
        self.assertNotIn("<qty>", svg)
        self.assertIn("✗ before · ✓ after: test -f proved.txt", svg)
        self.assertIn("cursive", svg)


def build_esc(text):
    import html
    return html.escape(text)


def media_blocks(css, query):
    """The text of each @media block whose query contains `query`, braces matched."""
    out, at = [], css.find(query)
    while at >= 0:
        start = css.index("{", at)
        depth, i = 1, start + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[start:i])
        at = css.find(query, i)
    return out


class Landing(unittest.TestCase):
    """T-2040: the landing page types no count by hand and no inline style, every animation has a still form, and the
    reel opens on the session, not on Claude Code's folder-trust question."""

    def test_asset_urls_change_with_their_content(self):  # T-2060, T-2063: a browser keeps a file by its URL
        import hashlib
        def v(rel):
            with open(os.path.join(ROOT, "site", rel), "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:8]
        tags = (rf'<link rel="icon" href="(\.\./)?assets/sampo\.svg\?v={v("assets/sampo.svg")}"',
                rf'<link rel="icon" href="(\.\./)?assets/favicon\.ico\?v={v("assets/favicon.ico")}"',
                rf'<img src="(\.\./)?assets/sampo\.svg\?v={v("assets/sampo.svg")}"',
                rf'<link rel="stylesheet" href="(\.\./)?style\.css\?v={v("style.css")}"',
                rf'<script src="(\.\./)?site\.js\?v={v("site.js")}"')
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "o")
            p = subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), out], capture_output=True,
                               text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr)
            for rel in ("index.html", os.path.join("features", "index.html"), os.path.join("features", "benchmarks.html"),
                        os.path.join("stories", "fix.html")):
                page = read_text(os.path.join(out, rel))
                for tag in tags:
                    self.assertRegex(page, tag, rel)
            page = read_text(os.path.join(out, "genesis", "index.html"))  # T-2087: it shares style.css, not site.js
            for tag in tags[:3]:
                self.assertRegex(page, tag, "genesis")

    def test_the_code_page_is_drawn_from_todays_tui(self):  # T-2061: a committed recording goes stale
        sys.path.insert(0, os.path.join(PLUGIN, "lib"))
        from sampo import smithtui
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "o")
            p = subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), out], capture_output=True,
                               text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr)
            page = read_text(os.path.join(out, "features", "code.html"))
            self.assertIn("<animate", read_text(os.path.join(out, "assets", "code-tui-tour.svg")))
        self.assertNotIn(".mp4", page, "a video recorded once shows an old fm code")
        self.assertIn('src="../assets/code-tui-tour.svg"', page)
        self.assertIn('media="(prefers-reduced-motion: reduce)" srcset="../assets/code-tui.svg"', page)
        labels = dict(smithtui.COMMAND_LABELS)
        for cmd in set(smithtui.DEFAULTS["keys"].values()):
            self.assertIn(build_esc(labels.get(cmd, cmd)), page, f"the key for {cmd} isn't on the page")

    def test_the_landing_runs_shader_cards_live(self):  # T-2062: the shaders only ran on their own page
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "o")
            p = subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), out], capture_output=True,
                               text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr)
            page = read_text(os.path.join(out, "index.html"))
        tiles = re.findall(r'<figure class="sketch" data-shader="(sh-shader-[a-z-]+)"', page)
        self.assertEqual(len(tiles), 4, "four live tiles on the landing")
        for sid in tiles:
            self.assertIn(f'<script type="x-shader/x-fragment" id="{sid}">', page, f"{sid} has no source")
        self.assertEqual(page.count('class="fallback"'), 4, "each tile keeps its card's words for no WebGL2")
        self.assertIn("prefers-reduced-motion: reduce", page, "one still frame for a reader who asked for less motion")
        self.assertIn('href="features/shaders.html"', page)

    def test_its_counts_come_from_the_build_and_its_styles_from_the_sheet(self):
        src = read_text(os.path.join(ROOT, "site", "index.html"))
        self.assertNotRegex(src, r"\sstyle=", "a class in style.css, not an inline style")
        for stale in ("744", "2,000+", "All 15 feature", "27 areas", "Over a thousand"):
            self.assertNotIn(stale, src, "a count typed by hand goes stale; build.py fills it from the data")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "o")
            p = subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), out], capture_output=True,
                               text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr)
            page = read_text(os.path.join(out, "index.html"))
        with open(os.path.join(ROOT, "site", "data", "stats.json"), encoding="utf-8") as f:
            stats = json.load(f)
        build = site("build")
        self.assertNotIn("{{", page, "a count left unfilled")
        self.assertIn(build.fmt(stats["totals"]["tasks_done"]), page)
        self.assertIn(build.fmt(stats["code"]["tests"]), page)
        self.assertIn(build.fmt(sum(stats["cards"].values())) + " stack cards", page)
        self.assertIn(f"{len(stats['cards'])} areas", page)
        self.assertIn(f"All {len(site('pages').PAGES)} feature pages", page)

    def test_every_animation_runs_only_where_motion_is_allowed(self):
        css = read_text(os.path.join(ROOT, "site", "style.css"))
        allowed = "".join(media_blocks(css, "@media (prefers-reduced-motion: no-preference)"))
        uses = re.findall(r"(?m)^\s*[^{}]*\{[^}]*\banimation(?:-name|-timeline)?:\s*(?!none)[^;}]+", css)
        self.assertTrue(uses, "the page has motion")
        for use in uses:
            self.assertIn(use.strip(), allowed, "an animation outside @media (prefers-reduced-motion: no-preference)")
        self.assertRegex(allowed, r"animation-timeline:\s*view\(\)", "reveals ride the scroll, not a script")
        js = read_text(os.path.join(ROOT, "site", "site.js"))  # T-2063: script motion too waits past the still return
        guard = js.index("prefers-reduced-motion: reduce")
        for motion in ("requestAnimationFrame", "pointermove", "setTimeout(function () { li.classList"):
            self.assertGreater(js.index(motion), js.index("return;", guard), motion)

    def test_the_reel_opens_on_the_session_not_the_trust_question(self):
        spec = importlib.util.spec_from_file_location("record_live", os.path.join(ROOT, "demo", "record_live.py"))
        rec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rec)
        trust = "\x1b[1mDo you trust the files in this folder?\x1b[0m\n> 1. Yes, I trust this folder"
        self.assertEqual(rec.kept([(0.0, trust), (1.0, "? for shortcuts")]), [(1.0, "? for shortcuts")])
        self.assertEqual(rec.kept([[0.5, "o", trust], [1.0, "o", "x"]]), [[1.0, "o", "x"]], "--from-cast too")
        with open(os.path.join(ROOT, "demo", "live.cast"), encoding="utf-8") as f:
            self.assertNotIn("trust this folder", f.read().lower(), "the committed reel still opens on the question")
        svg = read_text(os.path.join(ROOT, "demo", "live.svg"))  # the README's reel, one <text> per word
        self.assertNotIn(">trust?<", svg, "live.svg is rendered from the cast: render it again")


class ProductLines(unittest.TestCase):
    """T-2173: Foreman is a discipline layer for coding agents; Claude Code is one surface, so no product line (README
    headline, the site's title and fine print, the marketplace entry, INSTALL.md) says it is for Claude Code alone."""

    def test_no_product_line_is_claude_code_only(self):
        for path in ("README.md", "site/index.html", "site/build.py", "site/genesis/index.html",
                     ".claude-plugin/marketplace.json", "INSTALL.md", "MASTER.md"):
            text = read_text(os.path.join(ROOT, path))
            for line in ("discipline layer for Claude Code", "Foreman for Claude Code", "community plugin for Claude Code",
                         "local Claude Code plugin", "`claude` (Claude Code) and"):
                self.assertNotIn(line, text, path)
        self.assertIn("discipline layer for coding agents", read_text(os.path.join(ROOT, "README.md")))
        self.assertIn("<span>sampo code</span>", read_text(os.path.join(ROOT, "site", "index.html")))


class NoBareNulls(unittest.TestCase):
    """T-2057: stats.json's duel cases render on the benchmarks page, and no built page prints a bare empty value."""
    def test_cases_show_and_no_page_prints_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), tmp], capture_output=True,
                               text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr)
            stats = json.loads(read_text(os.path.join(ROOT, "site", "data", "stats.json")))
            names = {c["case"] for d in stats["duels"] for c in d.get("cases") or []}
            self.assertTrue(names, "stats.json has no duel cases, so the check below would pass empty")
            bench = os.path.join(tmp, "features", "benchmarks.html")
            if not os.path.exists(bench):
                bench = os.path.join(tmp, "features", "benchmarks", "index.html")
            page = read_text(bench)
            for n in sorted(names):
                self.assertIn(n, page, f"duel case {n} is missing from the benchmarks page")
            # a whole line (tags cut to line breaks) that is the word, or a value after a colon / in parentheses
            bare = re.compile(r"^(None|null|undefined|NaN)$|:\s*(None|null|undefined|NaN)\b|\((None|null|undefined|NaN)\)")
            for d, _, fs in os.walk(tmp):
                for fn in fs:
                    if fn.endswith(".html"):
                        html = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", read_text(os.path.join(d, fn)))
                        for line in re.sub(r"(?s)<[^>]*>", "\n", html).splitlines():
                            self.assertIsNone(bare.search(line.strip()), f"{fn} prints a bare empty value: {line.strip()[:80]}")


class GenesisScorecard(unittest.TestCase):
    """T-2154: the Genesis page's scorecard is text from genesis.json: a hostile why never becomes markup."""
    def test_a_hostile_why_renders_as_text_and_a_missing_key_says_not_graded(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("no node")
        d = os.path.join(ROOT, "site", "genesis")
        r = subprocess.run([node, os.path.join(PLUGIN, "tests", "genesis_page.js"), d], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"text": True, "markup": False, "none": True, "docs": True,
                                                "long": True, "longReason": True, "longNone": True})
        js = read_text(os.path.join(d, "genesis.js"))
        self.assertNotIn("innerHTML", re.search(r"function scorecard\(\)(.*?)\n  }\n", js, re.S).group(1))
        self.assertNotIn("innerHTML", re.search(r"function longRow\(\)(.*?)\n  }\n", js, re.S).group(1))  # T-2263


class PortTools(unittest.TestCase):
    """T-2904: the port's tools write an output only when its content changes, so a rerun leaves no churn."""

    def test_an_unchanged_output_is_not_rewritten(self):
        spec = importlib.util.spec_from_file_location("writeif", os.path.join(ROOT, "site", "tools", "writeif.py"))
        w = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(w)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tokens.json")
            self.assertTrue(w.write_if_changed(p, '{"a": 1}\n'))
            os.utime(p, (1, 1))
            self.assertFalse(w.write_if_changed(p, '{"a": 1}\n'))
            self.assertEqual(os.stat(p).st_mtime, 1)
            self.assertTrue(w.write_if_changed(p, '{"a": 2}\n'))
            self.assertEqual(read_text(p), '{"a": 2}\n')


def _card(n, title, when, cmds, **kw):
    return {"id": f"K-{n:06d}", "model": "secret-model", "batch": 1, "title": title, "when": when,
            "steps": [{"do": f"Step {i}", "cmd": c} for i, c in enumerate(cmds, 1)], "example": f"Example {n}",
            "check": f"Check {n}", "traps": [f"Trap {n}"], **kw}


CARDS = [_card(1, "Pin the Python test runner", "A repo runs its tests in CI", ["pytest -q", ""],
               cell={"kind": "bugs", "stack": "python", "stage": "verify", "surface": "code"}),
         _card(2, "Shell scripts quote their variables", "A script splits a path with spaces", ["shellcheck run.sh"],
               cell={"kind": "refactors", "stack": "shell", "stage": "review", "surface": "ops"}),
         _card(3, "Buttons keep a focus ring", "A React button lost its outline", ["npx playwright test focus.spec.ts"],
               cell={"component": "button", "platform": "react", "concern": "accessibility", "stage": "verify", "grid": "ui"}),
         _card(4, "<img src=x onerror=window.__pwned=1> wide tables scroll", "A table is wider than the phone",
               ["npx stylelint table.css"],
               cell={"component": "table", "platform": "html-css", "concern": "responsive", "stage": "build", "grid": "ui"}),
         _card(5, "Fix a SQL migration, small", "A migration drops a column", ["sqlite3 app.db .schema"],
               playbook="/home/someone/plugin/references/fix/sql.md", profile="gemma-12b", lift=0.2),
         _card(6, "A hostile grid name", "Never a path", ["ls"], cell={"grid": "../../etc", "part": "x"}),
         {"title": "no id at all"}, {"id": "K-000009", "title": ""}, "not a card"]


def card_pages(d):
    """(manifest, every card of its pages in page order) of an export in d."""
    man = json.loads(read_text(os.path.join(d, "index.json")))
    return man, [c for p in man["pages"] for c in json.loads(read_text(os.path.join(d, p["name"] + ".json")))["cards"]]


class CardsExport(unittest.TestCase):
    """T-3101: the card store as a small manifest, pages of the search index and body shards, each written only when
    it changes."""

    def files(self, d):
        return {f: os.stat(os.path.join(d, f)).st_mtime for f in sorted(os.listdir(d))}

    def test_the_index_and_shards_hold_every_card_and_a_rerun_rewrites_nothing(self):
        build = site("build")
        with tempfile.TemporaryDirectory() as d:
            for name, text in (("ui-9.json", "{}"), ("index.7.json", "{}"), ("gen-1.json", "{}"), ("notes.txt", "mine"),
                               ("index.json", '{"v":2,"files":{"ui-9":"0","index.7":"0","../x":"0"}}')):
                with open(os.path.join(d, name), "w") as f:  # ui-9, index.7: files the last export wrote, now unfilled
                    f.write(text)
            n, wrote = build.export_cards(CARDS, d, block=1, page=4)
            self.assertEqual(n, 6)
            self.assertGreater(wrote, 0)
            index, cards = card_pages(d)
            self.assertEqual([c["id"] for c in cards], [f"K-{i:06d}" for i in range(1, 7)])
            self.assertEqual([(p["name"], p["count"], p["first"], p["last"]) for p in index["pages"]],
                             [("index.1", 4, "K-000001", "K-000004"), ("index.2", 2, "K-000005", "K-000006")])
            self.assertEqual(index["count"], 6)
            by = {c["id"]: c for c in cards}
            for c in cards:
                self.assertLessEqual({"id", "grid", "cell", "title", "when"}, set(c))
            self.assertEqual(by["K-000001"]["grid"], "general")
            self.assertEqual(by["K-000001"]["cell"], {"kind": "bugs", "stack": "python", "stage": "verify", "surface": "code"})
            self.assertEqual(by["K-000003"]["grid"], "ui")
            self.assertEqual(by["K-000003"]["cell"]["platform"], "react")
            self.assertIn("npx playwright test focus.spec.ts", by["K-000003"]["cmds"], "commands are searchable")
            self.assertEqual(by["K-000005"]["grid"], "variants")
            self.assertEqual(by["K-000006"]["grid"], "other", "a grid name becomes a file name, so it is never a path")
            self.assertEqual(index["grids"]["ui"]["count"], 2)
            self.assertEqual(index["grids"]["general"]["fields"], ["kind", "stack", "stage", "surface"])
            self.assertEqual(index["grids"]["ui"]["fields"], ["component", "platform", "concern", "stage"])
            self.assertEqual(index["grids"]["ui"]["cells"]["platform"], {"html-css": 1, "react": 1})
            names = set(os.listdir(d))
            self.assertNotIn("ui-9.json", names, "a stale shard goes")
            self.assertNotIn("index.7.json", names, "a stale page goes")
            self.assertLessEqual({"notes.txt", "gen-1.json"}, names, "a file the export never wrote stays, shard-like or not")
            self.assertEqual(set(index["files"]), {x[:-5] for x in names - {"notes.txt", "gen-1.json", "index.json"}})
            for name in names - {"notes.txt", "gen-1.json"}:
                self.assertRegex(name, r"^[a-z0-9.-]+\.json$")
                text = read_text(os.path.join(d, name))
                self.assertNotIn("secret-model", text, "which model wrote a card isn't published")
                self.assertNotIn("/home/someone", text, "no absolute path from this machine")
            self.assertEqual(len([x for x in names if x.startswith("ui-")]), 2, "block=1: one shard per card")
            for c in cards:
                body = json.loads(read_text(os.path.join(d, c["shard"] + ".json")))[c["id"]]
                self.assertEqual(set(body), {"steps", "example", "check", "traps"})
            self.assertEqual(json.loads(read_text(os.path.join(d, by["K-000001"]["shard"] + ".json")))["K-000001"]["steps"],
                             [{"do": "Step 1", "cmd": "pytest -q"}, {"do": "Step 2", "cmd": ""}])
            for f in os.listdir(d):
                os.utime(os.path.join(d, f), (1, 1))
            before = self.files(d)
            self.assertEqual(build.export_cards(CARDS, d, block=1, page=4), (6, 0), "nothing changed, nothing written")
            self.assertEqual(self.files(d), before)
            two = build.export_cards(CARDS[:2], d)
            self.assertEqual(two[0], 2)
            self.assertEqual(sorted(x for x in os.listdir(d) if x not in ("notes.txt", "gen-1.json")),
                             ["general-1.json", "index.1.json", "index.json"])

    def test_thousands_of_cards_page_behind_a_tiny_manifest_with_exact_counts(self):
        # the user wants tens of thousands of cards: one index of all of them would be 13-20 MB at 30-50k
        build = site("build")
        comps, plats = ("button", "form", "table"), ("html-css", "react")
        many = [_card(i, f"Card {i}", f"When {i}", [f"run {i}"], cell=(
            {"grid": "ui", "component": comps[i % 3], "platform": plats[i % 2], "concern": "focus", "stage": "build"}
            if i % 4 else {"kind": "bugs", "stack": "python", "stage": "verify", "surface": "code"})) for i in range(1, 4501)]
        ui = [c["cell"] for c in many if c["cell"].get("grid") == "ui"]
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(build.export_cards(many, d)[0], 4500)
            man, cards = card_pages(d)
            self.assertEqual([(p["name"], p["count"], p["first"], p["last"]) for p in man["pages"]],
                             [("index.1", 2000, "K-000001", "K-002000"), ("index.2", 2000, "K-002001", "K-004000"),
                              ("index.3", 500, "K-004001", "K-004500")])
            self.assertEqual([c["id"] for c in cards], [f"K-{i:06d}" for i in range(1, 4501)])
            self.assertEqual((man["count"], man["grids"]["ui"]["count"], man["grids"]["general"]["count"]),
                             (4500, len(ui), 4500 - len(ui)))
            for f, values in (("component", comps), ("platform", plats)):
                self.assertEqual(man["grids"]["ui"]["cells"][f], {v: sum(c[f] == v for c in ui) for v in values})
            self.assertEqual(man["grids"]["general"]["cells"]["stack"], {"python": 4500 - len(ui)})
            self.assertEqual(sum(p["grids"]["ui"] for p in man["pages"]), len(ui), "each page says what it holds")
            self.assertLess(os.path.getsize(os.path.join(d, "index.json")), 4096, "the manifest holds no card")
            for f in os.listdir(d):
                os.utime(os.path.join(d, f), (1, 1))
            before = self.files(d)
            self.assertEqual(build.export_cards(many, d), (4500, 0), "an unchanged rerun writes nothing")
            self.assertEqual(self.files(d), before)
            new = _card(4501, "Card 4501", "When 4501", ["run"], cell={"grid": "ui", "component": "form", "platform": "react"})
            self.assertEqual(build.export_cards(many + [new], d), (4501, 3))
            self.assertEqual({f for f, t in self.files(d).items() if t != 1} - {"index.json", "index.3.json"},
                             {card_pages(d)[1][-1]["shard"] + ".json"},
                             "a new card rewrites the manifest, the last page and its own body shard, nothing else")

    def test_the_cli_exports_the_store_and_an_empty_store_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            store, dest = os.path.join(d, "store"), os.path.join(d, "out")
            os.makedirs(store)
            env = dict(os.environ, SAMPO_CARDS=store)
            run = lambda: subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), "--cards", dest],  # noqa: E731
                                         capture_output=True, text=True, timeout=120, env=env)
            p = run()
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("0 cards", p.stdout)
            index = json.loads(read_text(os.path.join(dest, "index.json")))
            self.assertEqual((index["count"], index["pages"]), (0, []))
            with open(os.path.join(store, "cards.jsonl"), "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(c) + "\n" for c in CARDS) + "not json\n")
            p = run()
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("6 cards", p.stdout)
            self.assertEqual(json.loads(read_text(os.path.join(dest, "index.json")))["count"], 6)


EXAMPLES = {"K-000003": {"html": '<button type="button">Save draft</button>', "h": 64}}
KIT = {"button": {"html": "<button>Kit button</button>", "h": 60},
       "table": {"html": "<table><tr><td>Kit table</td></tr></table>", "h": 70}}


class CardsDemo(unittest.TestCase):
    """T-3102: each UI card's example, resolved at export: its own kept one, else its component's kit demo."""

    def test_a_ui_card_gets_its_example_or_its_components_kit_demo(self):
        build = site("build")
        with tempfile.TemporaryDirectory() as d:
            build.export_cards(CARDS, d, block=1, examples=EXAMPLES, kit=KIT, frame="<html data-theme=\"light\">"
                               "<head></head><body></body></html>")
            index, cards = card_pages(d)
            body = {c["id"]: json.loads(read_text(os.path.join(d, c["shard"] + ".json")))[c["id"]] for c in cards}
            self.assertEqual(body["K-000003"]["demo"], EXAMPLES["K-000003"])
            self.assertEqual(body["K-000004"]["demo"], {"kit": "table"})
            self.assertNotIn("demo", body["K-000001"], "a general card has none")
            kit = json.loads(read_text(os.path.join(d, "kit.json")))
            self.assertEqual(kit["kit"], KIT)
            self.assertIn("data-theme", kit["frame"])
            self.assertIn("kit", index["files"], "the kit is versioned like every file")
            build.export_cards(CARDS[:2], d)
            self.assertFalse(os.path.exists(os.path.join(d, "kit.json")), "no UI cards, no kit: the stale one goes")

    def test_the_cards_export_resolves_the_store_and_the_kit(self):
        src = read_text(os.path.join(ROOT, "site", "build.py"))
        self.assertIn("smallcards.examples()", src)
        self.assertIn("smallcards.kit()", src)


class CardsPage(unittest.TestCase):
    """T-3101: site/cards.html, built from the committed export: search, grid tabs, cell filters with counts, a card
    opened in place, a #K-id permalink; light and dark, and 390 px with no sideways scroll; newest first, the index
    fetched a page at a time."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = os.path.join(cls.tmp.name, "o")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), cls.out], capture_output=True,
                           text=True, timeout=300)
        if p.returncode:
            raise AssertionError(p.stderr)
        # the fixture in place of the committed export, two cards a page so every test crosses pages
        sys.path.insert(0, os.path.join(PLUGIN, "lib"))
        from sampo import smallcards
        examples = dict(EXAMPLES, **{"K-000003": {"html": EXAMPLES["K-000003"]["html"] + '<img src="x" '
                                                           'onerror="parent.__pwned=1">', "h": 64}})
        site("build").export_cards(CARDS, os.path.join(cls.out, "cards"), page=2, examples=examples, kit=KIT,
                                   frame=smallcards.doc("", "light"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_page_is_built_linked_and_writes_card_text_as_text(self):
        page = read_text(os.path.join(self.out, "cards.html"))
        self.assertRegex(page, r'<script src="cards\.js\?v=[0-9a-f]{8}"')
        self.assertIn('href="cards.html"', read_text(os.path.join(self.out, "index.html")), "the landing links it")
        stack = read_text(os.path.join(self.out, "features", "cards.html"))
        self.assertIn('href="../cards.html"', stack, "every section page's nav links it")
        self.assertNotIn(">Stack cards<", stack, "two 'cards' entries in one nav read as one page")
        self.assertIn(">Stack library<", stack)
        self.assertRegex(stack, r'small cards Sampo writes[^<]*<a href="\.\./cards\.html">', "the stack page points on")
        self.assertIn('id="cards-q"', page)
        js = read_text(os.path.join(ROOT, "site", "cards.js"))
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertIn("textContent", js)
        self.assertIn("AbortController", js, "a stalled fetch ends in a message, not 'loading' forever")

    def browse(self, size, hash="", scheme=None, steps=(), within=30, cards=None, slow=None):
        """Load cards.html, run each (js, until) of steps: evaluate js, then poll `until` (a JS boolean) to true.
        cards: a folder served as cards/ in place of the class's export; slow: a file name answered 2 s late."""
        browser = headless_chrome()
        if not browser:
            self.skipTest("no headless Chromium (FOREMAN_CHROME, chrome-headless-shell or Playwright's download)")
        import functools
        import threading
        import time
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        sys.path.insert(0, os.path.join(PLUGIN, "lib"))
        from sampo import shot

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def translate_path(self, path):
                got = super().translate_path(path)
                if slow and got.endswith("/" + slow):
                    time.sleep(2)
                if cards and got.startswith(os.path.join(self.directory, "cards") + os.sep):
                    return os.path.join(cards, os.path.basename(got))
                return got

        srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=self.out))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        b, errs, got = shot.shared(browser, size), [], []
        try:
            b.on["Runtime.exceptionThrown"] = lambda p: errs.append(json.dumps(p.get("exceptionDetails"))[:300])
            b.call("Runtime.enable", b.sid)
            if scheme:
                b.call("Emulation.setEmulatedMedia", b.sid, features=[{"name": "prefers-color-scheme", "value": scheme}])
            shot.load(b, f"http://127.0.0.1:{srv.server_address[1]}/cards.html{hash}")
            ev = lambda js: b.call("Runtime.evaluate", b.sid, expression=js, returnByValue=True)["result"].get("value")  # noqa: E731
            for js, until in steps:
                if js:
                    ev(js)
                deadline = time.monotonic() + within
                while not ev(until) and time.monotonic() < deadline:
                    b.wait("cards-test.never", 0.2)
                got.append(ev(PROBE_CARDS))
        finally:
            b.close()
            srv.shutdown()
            srv.server_close()
        self.assertEqual(errs, [])
        return got

    def test_search_filters_counts_and_a_card_opened_in_place(self):
        ready = "document.querySelectorAll('#cards-list details').length === 6"
        q = ("(v => { const i = document.getElementById('cards-q'); i.value = v; "
             "i.dispatchEvent(new Event('input', {bubbles: true})); })")
        got = self.browse((1280, 900), steps=[
            ("", ready),
            (q + "('shellcheck')", "document.querySelectorAll('#cards-list details').length === 1"),
            (q + "('phone')", "document.querySelector('#cards-list details') && "
                              "document.querySelector('#cards-list details').id === 'K-000004'"),
            (q + "('')", "document.querySelectorAll('#cards-list details').length === 6"),
            ("document.querySelector('#cards-grids [data-grid=\"ui\"]').click()",
             "document.querySelectorAll('#cards-list details').length === 2"),
            ("(s => { s.value = 'react'; s.dispatchEvent(new Event('change', {bubbles: true})); })"
             "(document.querySelector('#cards-facets select[data-field=\"platform\"]'))",
             "document.querySelectorAll('#cards-list details').length === 1"),
            ("document.querySelector('#K-000003 summary').click()",
             "(document.querySelector('#K-000003 .kbody') || {}).textContent.includes('npx playwright test')")])
        everything, shell, phone, cleared, ui, react, opened = got
        self.assertEqual(everything["ids"], [f"K-{i:06d}" for i in range(6, 0, -1)], "newest first")
        self.assertEqual(everything["grids"]["ui"], "2")
        self.assertEqual(everything["grids"]["all"], "6")
        self.assertIn("Showing 6 of 6 cards", everything["status"])
        self.assertEqual(shell["ids"], ["K-000002"], "a command is searchable")
        self.assertEqual(phone["ids"], ["K-000004"], "a when line is searchable")
        self.assertEqual(cleared["shown"], 6)
        self.assertEqual(ui["ids"], ["K-000004", "K-000003"])
        self.assertIn("react (1)", ui["options"]["platform"], "each cell value carries its count")
        self.assertIn("html-css (1)", ui["options"]["platform"])
        self.assertEqual(react["ids"], ["K-000003"])
        self.assertEqual(opened["hash"], "#K-000003", "an opened card is its own permalink")
        for want in ("Step 1", "npx playwright test focus.spec.ts", "Example 3", "Check 3", "Trap 3"):
            self.assertIn(want, opened["body"])
        self.assertFalse(everything["pwned"], "a card's title is text, never markup")
        self.assertIn("<img src=x", everything["text"])

    def test_a_ui_cards_example_shows_in_a_sandbox_with_no_scripts_in_the_readers_theme(self):
        frame = "document.querySelector('#K-000003 iframe')"
        got = self.browse((390, 844), hash="#K-000003", steps=[
            ("", f"{frame} && {frame}.srcdoc.includes('Save draft')"),
            ("document.documentElement.setAttribute('data-theme', 'dark')",
             f"{frame}.srcdoc.includes('data-theme=\"dark\"')"),
            ("location.hash = '#K-000004'", "document.querySelector('#K-000004 iframe')")])
        light, dark, kit = got
        demo = light["demos"][0]
        self.assertEqual(demo["sandbox"], "", "sandboxed with nothing allowed: no scripts, no same origin")
        self.assertIn("Content-Security-Policy", demo["doc"])
        self.assertIn("default-src 'none'", demo["doc"])
        self.assertEqual(demo["h"], "64")
        self.assertIn("Buttons keep a focus ring", demo["title"])
        self.assertIn('data-theme="dark"', dark["demos"][0]["doc"], "the example follows the page's theme")
        self.assertIn("Kit table", kit["demos"][0]["doc"], "a card with no example shows its component's kit demo")
        self.assertFalse(any(g["pwned"] for g in got), "an example's markup never runs in the page")
        self.assertLessEqual(light["wide"], 390, "the example fits a phone")

    def test_a_permalink_opens_its_card(self):
        got = self.browse((1280, 900), hash="#K-000002", steps=[
            ("", "(document.querySelector('#K-000002 .kbody') || {}).textContent.includes('shellcheck run.sh')")])
        self.assertTrue(got[0]["open"].get("K-000002"), "the permalinked card is open")
        self.assertIn("Trap 2", got[0]["body"])
        self.assertEqual(got[0]["q"], "K-000002", "it says why the list holds one card; clearing the search shows them all")

    def test_it_reads_in_light_and_dark_and_fits_a_phone(self):
        ready = "document.querySelectorAll('#cards-list details').length > 0"
        seen = {}
        for scheme in ("light", "dark"):
            for size in ((1280, 900), (390, 844)):
                got = self.browse(size, scheme=scheme, hash="#K-000004", steps=[
                    ("", ready + " && (document.querySelector('#K-000004 .kbody') || {textContent: ''}).textContent.length > 0")])[0]
                self.assertLessEqual(got["wide"], size[0], f"no sideways scroll at {size} {scheme}")
                self.assertEqual(got["ids"], ["K-000004"], "a permalink narrows the list to its card")
                seen[scheme] = got["bg"]
        self.assertNotEqual(seen["light"], seen["dark"], "the page follows the reader's scheme")

    def test_an_empty_store_says_so(self):
        cards = os.path.join(self.tmp.name, "none")
        site("build").export_cards([], cards)
        got = self.browse((390, 844), cards=cards, steps=[("", "document.getElementById('cards-empty') && "
                                                                "!document.getElementById('cards-empty').hidden")])[0]
        self.assertEqual(got["shown"], 0)
        self.assertIn("No cards yet", got["empty"])

    def test_the_newest_page_draws_first_and_a_search_reads_the_rest(self):
        cards = os.path.join(self.tmp.name, "many")
        site("build").export_cards([_card(i, "Zebra crossings" if i == 7 else f"Card {i}", f"When {i}", [f"run {i}"],
                                          cell={"grid": "ui", "component": "form" if i % 2 else "table"})
                                    for i in range(1, 2501)], cards, page=1000)
        ready = "document.querySelectorAll('#cards-list details').length > 0"
        q = ("(v => { const i = document.getElementById('cards-q'); i.value = v; "
             "i.dispatchEvent(new Event('input', {bubbles: true})); })")
        first, searching, found = self.browse((1280, 900), cards=cards, slow="index.1.json", steps=[
            ("", ready),
            (q + "('zebra')", "document.getElementById('cards-status').textContent.includes('Searching 1,500 of')"),
            ("", "document.querySelectorAll('#cards-list details').length === 1")])
        self.assertEqual((first["shown"], first["ids"][0]), (50, "K-002500"), "a page of the newest cards, at once")
        self.assertEqual(first["fetched"], ["index.3.json"], "the first page holds enough: no other is fetched")
        self.assertIn("Showing 50 of 2,500 cards", first["status"])
        self.assertEqual(first["grids"]["ui"], "2,500", "the counts come from the manifest, before a page loads")
        self.assertIn("Searching 1,500 of 2,500 cards", searching["status"], "it says how far the search has read")
        self.assertEqual(searching["shown"], 0, "the oldest page, still on its way, holds the match")
        self.assertEqual(searching["empty"], "", "a search still reading says so, not 'no match'")
        self.assertEqual(found["ids"], ["K-000007"], "a search reads every page, the oldest last")
        self.assertEqual(found["fetched"], ["index.3.json", "index.2.json", "index.1.json"])
        self.assertIn("1 of 1 match", found["status"])


PROBE_CARDS = """(() => {
  const list = [...document.querySelectorAll('#cards-list details')];
  const grids = {}, options = {}, open = {};
  document.querySelectorAll('#cards-grids [data-grid]').forEach(b => {
    grids[b.dataset.grid] = (b.querySelector('.n') || {textContent: ''}).textContent.trim(); });
  document.querySelectorAll('#cards-facets select[data-field]').forEach(s => {
    options[s.dataset.field] = [...s.options].map(o => o.textContent.trim()); });
  list.forEach(d => { open[d.id] = d.open; });
  const empty = document.getElementById('cards-empty');
  return {shown: list.length, ids: list.map(d => d.id), grids, options, open, hash: location.hash,
    q: (document.getElementById('cards-q') || {}).value,
    body: list.filter(d => d.open).map(d => d.textContent).join(' | '), text: (document.getElementById('cards-list') || {}).textContent || '',
    pwned: !!window.__pwned || !!document.querySelector('#cards-list img'),
    demos: [...document.querySelectorAll('#cards-list iframe')].map(f => ({sandbox: f.getAttribute('sandbox'),
      doc: f.srcdoc, h: f.getAttribute('height'), title: f.title})),
    empty: empty && !empty.hidden ? empty.textContent : '', wide: document.documentElement.scrollWidth,
    bg: getComputedStyle(document.body).backgroundColor,
    status: (document.getElementById('cards-status') || {}).textContent || '',
    fetched: performance.getEntriesByType('resource').map(e => e.name.split('?')[0].split('/').pop())
      .filter(n => /^index\\.\\d+\\.json$/.test(n))};
})()"""


if __name__ == "__main__":
    unittest.main()

