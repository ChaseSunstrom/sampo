"""The landing page's layout at phone widths (T-1683): measured in a real headless Chromium, skipped with a reason
when there is none."""
import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from helpers import PLUGIN, ForemanTestCase, read_doc, read_text

from sampo import shot

SITE = os.path.join(os.path.dirname(PLUGIN), "site")
RECTS = """JSON.stringify([...document.querySelectorAll('header.bar > *')]
  .filter(e => getComputedStyle(e).display !== 'none')
  .map(e => { const r = e.getBoundingClientRect(), g = document.createRange(); g.selectNodeContents(e);
              const t = g.getBoundingClientRect();  // T-2089: the text, which can spill past a shrunk box
              return [e.textContent.trim() || e.className, Math.min(r.left, t.left), Math.max(r.right, t.right),
                      r.top, r.bottom]; })
  .concat([['page', 0, document.documentElement.scrollWidth, 0, 0]]))"""


class Header(unittest.TestCase):
    """T-1683: at 320 px the brand overlapped the Install link."""

    @classmethod
    def setUpClass(cls):
        cls.browser = shot.find_browser("test_site")[0]
        if not cls.browser:
            raise unittest.SkipTest("no headless Chromium (FOREMAN_CHROME, PATH or Playwright's)")
        cls.out = tempfile.mkdtemp(prefix="test-site-")  # T-3996: the built pages, whose header the build writes
        p = subprocess.run([sys.executable, os.path.join(SITE, "build.py"), cls.out], capture_output=True, text=True,
                           timeout=300)
        if p.returncode:
            raise AssertionError(p.stderr)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=cls.out)
        handler.log_message = lambda *a: None
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.out, ignore_errors=True)

    def rects(self, width, page="index.html"):
        b = shot.shared(self.browser, (width, 640))
        try:
            shot.load(b, f"http://127.0.0.1:{self.server.server_address[1]}/{page}")
            got = b.call("Runtime.evaluate", b.sid, expression=RECTS, returnByValue=True)
            return json.loads(got["result"]["value"])
        finally:
            b.close()

    def test_nothing_in_the_header_overlaps_or_overflows_at_phone_widths(self):
        for width, name in ((w, n) for w in (320, 360, 390) for n in ("index.html", "genesis/index.html", "features/index.html")):
            with self.subTest(width=width, page=name):  # T-2089: without Barlow the wider fallback spilled at 360
                *items, page = self.rects(width, name)
                self.assertGreaterEqual(len(items), 3, f"{name}: no header drawn")  # brand, menu, theme at least
                self.assertLessEqual(page[2], width, "the page scrolls sideways")
                for (a, al, ar, at, ab), (b, bl, br, bt, bb) in zip(items, items[1:]):
                    self.assertLessEqual(ar, bl + 0.5, f"{a!r} overlaps {b!r} at {width} px")
                for name, left, right, *_ in items:
                    self.assertGreaterEqual(left, 0, f"{name!r} starts off screen at {width} px")
                    self.assertLessEqual(right, width, f"{name!r} ends off screen at {width} px")


class CodePageCoversFmCode(unittest.TestCase):
    """T-2071: every fm code verb and long flag, read from its own argparse, is on the Foreman Code page, or here with
    a reason; the page drifted 131 commits behind once."""
    SKIP = {  # flag or verb -> why the page leaves it out
        "--project": "fm's own project selector, the same on every verb",    }

    def test_every_verb_and_flag_is_on_the_page(self):
        import argparse
        import re
        from sampo import cli
        import test_site_build
        sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        code = sub.choices["code"]
        flags = {o for a in code._actions for o in a.option_strings
                 if o.startswith("--") and o != "--help" and a.help != argparse.SUPPRESS}  # --tui is internal
        request = next(a for a in code._actions if a.dest == "request").help
        m = re.search(r"\(or: ([^)]*)\)", request)
        self.assertIsNotNone(m, "fm code request help lost its '(or: …)' verb list")
        verbs = set(re.split(r"[ ,]+", m.group(1)))
        self.assertGreaterEqual(len(flags), 30)
        self.assertLessEqual({"models", "runners", "config", "why", "bench", "tui"}, verbs)
        page = next(p for p in test_site_build.site("pages").PAGES if p["slug"] == "code")
        text = " ".join(x for b in page["blocks"] for x in b if isinstance(x, str)) + page["lede"]
        text = re.sub(r"<[^>]+>", "", text)
        missing = sorted(f for f in flags if f not in self.SKIP and not re.search(re.escape(f) + r"(?![\w-])", text))
        missing += sorted("sampo code " + v for v in verbs if v not in self.SKIP and not re.search(
            r"sampo code (?:\w+ )?" + re.escape(v) + r"\b", text))
        self.assertEqual(missing, [], "on fm code's --help but not on the Foreman Code page (site/pages.py): add a "
                                      "sentence, or an entry with a reason in SKIP")
        for k in self.SKIP:  # a stale entry hides nothing
            self.assertTrue(k in flags or k in verbs, k + " is no longer an fm code flag or verb")


class GoalPage(unittest.TestCase):
    def test_the_planning_page_describes_the_goal_verbs_and_the_across_days_line(self):  # T-2141
        sys.path.insert(0, SITE)
        try:
            import pages
        finally:
            sys.path.pop(0)
        page = next(x for x in pages.PAGES if x["slug"] == "planning")
        text = json.dumps(page["blocks"], default=str)
        for word in ("Goal to finish", "sampo goal set", "sampo goal item", "sampo goal link", "sampo goal show",
                     "sampo goal verdict", "sampo task new", "--goal", "across days"):
            self.assertIn(word, text)


class LacksPage(unittest.TestCase):  # T-2378: #5 on the site, its figures from the ledger (site/stats.py)
    def test_the_tools_it_lacks_page_names_every_surface_and_renders_from_state(self):
        sys.path.insert(0, SITE)
        try:
            import pages
            import build
        finally:
            sys.path.pop(0)
        page = next(x for x in pages.PAGES if x["slug"] == "lacks")
        text = json.dumps(page["blocks"], default=str)
        for word in ("sampo which", "sampo new tool", "sampo ask", "--later", "never", "sampo code", "Codex", "MCP", "band"):
            self.assertIn(word, text)
        tools = {"misses": 41, "resolved": 30, "proved": 7, "searches": 55, "queued": 3, "refused": 1}
        html = build.render_page(page, {"tools": tools}, pages.PAGES)
        for figure in ("41", "30", "55"):
            self.assertIn(figure, html)
        self.assertIn('class="tiles"', build.render_page(page, {}, pages.PAGES))  # no data yet: zeros, still a figure
        root = os.path.dirname(SITE)
        for name in ("README.md", "MASTER.md", "CHANGELOG.md"):  # each surface named where a reader looks
            doc = read_doc(os.path.join(root, name))
            for word in ("`grants`", "PostToolUseFailure"):
                self.assertIn(word, doc, f"{name}: {word}")
            self.assertRegex(doc, r"\b(?:sampo|fm) which\b", name)  # either name while the docs move to Sampo


class ProofPage(unittest.TestCase):  # T-2395: #6/#10 on the site, built from Foreman's own code and data
    def test_proof_page_names_every_kind_from_the_code_and_renders_counts_over_time(self):
        sys.path.insert(0, SITE)
        try:
            import pages
            import build
        finally:
            sys.path.pop(0)
        from sampo import core
        page = next(x for x in pages.PAGES if x["slug"] == "verifying")
        text = json.dumps(page["blocks"], default=str)
        for kind in core.PROOF_KINDS:
            self.assertIn(kind, text)
        for word in ("sampo prove --rollup", "proof", "beyond", "unproven"):
            self.assertIn(word, text)
        proof = {"n": 12, "counts": {"typed": 5, "meaning": 4}, "history": [{"day": "2026-10-30", "n": 15},
                                                                             {"day": "2026-10-31", "n": 12}]}
        html = build.render_page(page, {"proof": proof}, pages.PAGES)
        self.assertIn("12", html)
        self.assertIn("meaning", html)
        self.assertIn('class="tiles"', build.render_page(page, {}, pages.PAGES))  # no data: still a figure


class DecisionTape(unittest.TestCase):  # T-2348 (Mission 5 inbox): every fm decide row on one timeline
    def test_the_decisions_page_lists_rows_newest_first_with_kind_confidence_and_reversal(self):
        import shutil
        import tempfile
        sys.path.insert(0, SITE)
        try:
            import pages
            import build
            import stats
        finally:
            sys.path.pop(0)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "decisions.md"), "w", encoding="utf-8") as f:
            f.write("| Date | Decision | Why | Rejected |\n|---|---|---|---|\n"
                    "| 2026-10-30 | [costly] [confidence: 70%] Keep the file cache | fast | |\n"
                    "| 2026-10-31 | Use <b>SQLite</b> for the index | simpler | |\n"
                    "| 2026-11-01 | [reverses: file cache] Drop the cache | stale reads | |\n")
        tape = stats.decisions(d)
        self.assertEqual([x["date"] for x in tape], ["2026-11-01", "2026-10-31", "2026-10-30"])
        self.assertEqual((tape[-1]["kind"], tape[-1]["confidence"], tape[-1]["later"]),
                         ("costly", 70, {"date": "2026-11-01", "how": "reversed"}))
        self.assertIsNone(tape[1]["later"])
        page = next(x for x in pages.PAGES if x["slug"] == "decisions")
        html = build.render_page(page, {"decisions": tape}, pages.PAGES)
        for figure in ("Keep the file cache", "70%", "costly", "reversed 2026-11-01"):
            self.assertIn(figure, html)
        self.assertIn("&lt;b&gt;SQLite", html)  # a row's text is escaped
        self.assertNotIn("<b>SQLite", html)
        self.assertIn('class="tiles"', build.render_page(page, {}, pages.PAGES))


class SelfRunningPage(unittest.TestCase):  # T-2364: Mission 8 on the site, its figures from the ledger and books
    def test_self_running_page_names_each_surface_and_renders_from_state(self):
        sys.path.insert(0, SITE)
        try:
            import pages
            import build
        finally:
            sys.path.pop(0)
        page = next(x for x in pages.PAGES if x["slug"] == "self-running")
        text = json.dumps(page["blocks"], default=str)
        for word in ("sampo triggers", "precision", "route", "sampo night --tick", "install", "sampo lane recover",
                     "trial", "sampo outcomes --predictions", "baseline", "lean", "generation"):
            self.assertIn(word, text)
        sr = {"triggers": [{"trigger": "ci-red", "fired": 9, "precision": 0.78, "route": "inbox"}], "installs": 2,
              "recoveries": 3, "trials": {"held": 4, "didn't hold": 1}, "predictions": [{"kind": "next-request",
              "hit": 0.62, "base_hit": 0.48, "n": 40}], "lean": [{"gen": 1, "rules chars": 5200}]}
        html = build.render_page(page, {"self_running": sr}, pages.PAGES)
        for figure in ("ci-red", "0.78", "62%", "48%"):
            self.assertIn(figure, html)
        self.assertIn('class="tiles"', build.render_page(page, {}, pages.PAGES))
        root = os.path.dirname(SITE)
        for name in ("README.md", "MASTER.md", "CHANGELOG.md"):
            self.assertIn("Self-running", read_doc(os.path.join(root, name)), name)


class JudgmentPage(unittest.TestCase):  # T-2344: #2 and #8 on the site, its figures from fm outcomes --judgment --json
    def test_judgment_page_describes_the_plan_shape_races_doors_and_scores_from_calibration_data(self):
        sys.path.insert(0, SITE)
        try:
            import pages
            import build
        finally:
            sys.path.pop(0)
        page = next(x for x in pages.PAGES if x["slug"] == "judgment")
        text = json.dumps(page["blocks"], default=str)
        for word in ("Reading", "fails if", "Choice N (NN%)", "sampo lane race", "one-way", "sampo outcomes --judgment",
                     "Brier", "base rate", "MCP", "sampo code"):
            self.assertIn(word, text)
        cal = {"scored": 30, "pending": 4, "brier": {"stated": 0.18, "base": 0.24}, "bars": {"research": 70, "race": 55},
               "races": {"run": 5, "judged": 4, "first_lost": 1}, "doors_parked": ["T-1"],
               "kinds": {"choice": [{"lo": 60, "hi": 79, "n": 3, "held": 2, "pct": 67, "stated": 72, "gap": 5}]},
               "interim": {"task": [{"lo": 80, "hi": 94, "n": 9, "held": 9, "pct": 100}]}}  # T-2498
        html = build.render_page(page, {"calibration": cal}, pages.PAGES)
        for figure in ("0.18", "0.24", "1 of 4", "30", "Held, by stated confidence", "choice 60–79% · n 3", "so far"):
            self.assertIn(figure, html)
        empty = build.render_page(page, {}, pages.PAGES)
        self.assertIn("not scored yet", empty)  # no data: said, never a crash
        self.assertIn("no reliability chart", empty)


if __name__ == "__main__":
    unittest.main()


class ModelLayerPage(unittest.TestCase):  # T-2489: the gen-2 model layer on the site, its figures from the exam's replay
    def test_model_layer_page_states_the_rules_and_renders_the_newest_replay(self):
        import shutil
        import tempfile
        sys.path.insert(0, SITE)
        try:
            import pages
            import build
            import stats
        finally:
            sys.path.pop(0)
        page = next(x for x in pages.PAGES if x["slug"] == "model-layer")
        text = json.dumps(page["blocks"], default=str)
        for word in ("rule", "local", "cloud", "sensitive", "shadow", "exam", "model-live", "proof", "MCP", "Brier",
                     "install"):
            self.assertIn(word, text)
        mr = {"intake-type": {"n": 40, "path_hits": 31, "model_n": 12, "model_hits": 10, "brier": 0.14, "ms": 820}}
        html = build.render_page(page, {"models": {"run": "s2-1", "replay": mr}}, pages.PAGES)
        for figure in ("intake-type", "31 of 40", "10 of 12", "0.14", "820"):
            self.assertIn(figure, html)
        self.assertIn('class="tiles"', build.render_page(page, {}, pages.PAGES))
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "research", "genesis"))
        with open(os.path.join(d, "research", "genesis", "exam.jsonl"), "w", encoding="utf-8") as f:
            for row in ({"run": "s1-1", "model_replay": {"dedupe": {"n": 1}}}, {"run": "s2-1", "model_replay": mr},
                        {"run": "s2-2"}, "not json"):
                f.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
        self.assertEqual(stats.models(d), {"run": "s2-1", "replay": mr})
        self.assertEqual(stats.models(tempfile.gettempdir() + "/no-such-state-T2489"), {})
        root = os.path.dirname(SITE)
        for name in ("README.md", "MASTER.md", "CHANGELOG.md"):
            self.assertIn("Model layer", read_doc(os.path.join(root, name)), name)


class KalevalaExport(ForemanTestCase):
    """T-3266: the Rune page shows Kalevala from genesis.json alone. rune.export writes a kalevala block: each tower's
    aspect, generation, stage, exam trend, state, pause and spend; Kalevala's own gens; Väinämöinen's newest acts."""

    def setUp(self):
        super().setUp()
        from sampo import core as c
        self.fm("init")
        gen0 = os.path.join(self.tmp, "gen0.md")
        with open(gen0, "w") as f:
            f.write("root charter\n\n## 2. Capabilities\n1. **Bugs.** finds and fixes broken code. Exam: bug.\n")
        self.fm("genesis", "init", "--file", gen0)
        self.p, self.out = c.find_project(self.repo), os.path.join(self.tmp, "site")

    def export(self):
        from sampo import rune
        rune.export(self.p, self.out)
        return json.loads(read_text(os.path.join(self.out, "genesis.json")))

    def grade(self, tower, run, train):
        from sampo import rune
        rune.set_tower(tower)
        try:
            with open(os.path.join(rune.home(self.p), "exam.jsonl"), "a") as f:
                f.write(json.dumps({"agent": "foreman", "run": run, "model": "sonnet", "split": "train",
                                    "closed": {"train": list(train), "holdout": []}}) + "\n")
        finally:
            rune.set_tower(None)

    def test_current_names_the_exported_next_command_teaches_sampo_rune(self):  # T-4842, finding 13
        nxt = self.export()["next"]
        self.assertRegex(nxt["command"], r"^sampo rune ")
        self.assertNotRegex(nxt["command"], r"\bfm\b")

    def test_no_block_before_kalevala_opens_a_tower(self):
        self.assertNotIn("kalevala", self.export())

    def test_towers_gens_and_acts_come_from_the_ledger_and_the_manifest(self):
        from sampo import kalevala
        self.fm("kalevala", "start")  # efficiency, memory, problem-solving
        self.fm("kalevala", "gen")
        self.grade("memory", "r1", ["a"])
        self.grade("memory", "r2", ["a", "b"])
        home = os.path.expanduser("~")
        kalevala.pause(self.p, f"<img src=x onerror=alert(1)> see {home}/notes", "memory")
        k = self.export()["kalevala"]
        towers = {t["name"]: t for t in k["towers"]}
        self.assertEqual(sorted(towers), ["efficiency", "memory", "problem-solving"])
        mem, eff = towers["memory"], towers["efficiency"]
        self.assertEqual((mem["aspect"], mem["generation"], mem["trend"], mem["state"]), ("memory", 0, [1, 2], "improving"))
        self.assertEqual(mem["paused"], "<img src=x onerror=alert(1)> see ~/notes")  # raw text; the page writes text
        self.assertEqual((eff["trend"], eff["closed"], eff["state"], eff["paused"]), ([], None, "unscored", None))
        self.assertEqual(eff["stage"]["stage"], "spec")  # its next action, read only
        self.assertIn("action", eff["stage"])
        for t in towers.values():
            self.assertNotIn("newest", t)  # an absolute path the page has no use for
            self.assertIn("spend", t)
            self.assertIn("stuck", t)
        self.assertEqual([(g["n"], g["set"]) for g in k["gens"]], [(1, sorted(towers))])
        self.assertEqual(k["gens"][0]["based_on"], "root gen-0")
        act = k["acts"][-1]
        self.assertEqual((act["act"], act["tower"], act["undo"]), ("pause", "memory", "fm kalevala resume memory"))
        self.assertTrue(act["at"])
        self.assertIsNone(k["halted"])
        self.assertIn("share", k)
        self.assertIn("spent_today", k)
        self.assertNotIn(home + "/", json.dumps(k))

    def test_a_damaged_manifest_is_named_not_fatal(self):
        from sampo import kalevala
        self.fm("kalevala", "start")
        with open(os.path.join(kalevala.home(self.p), "manifest.json"), "w") as f:
            f.write("{not json")
        k = self.export()["kalevala"]
        self.assertIn("doesn't parse", k["error"])
        self.assertNotIn("towers", k)

    def test_one_bad_tower_never_aborts_the_export(self):
        """Review of T-3266: a manifest name failing TOWER_NAME is skipped (as rune.towers skips such dirs); one
        tower's stage that won't read is that tower's error; a watch that won't read is the block's error; the export
        still writes genesis.json, and the caller's tower is back after it."""
        from sampo import kalevala, rune
        self.fm("kalevala", "start")
        path = os.path.join(kalevala.home(self.p), "manifest.json")
        m = json.loads(read_text(path))
        m["towers"]["Bad Name"] = {"aspect": "x"}
        m["towers"]["../up"] = {"aspect": "y"}
        with open(path, "w") as f:
            json.dump(m, f)
        stage, calls = kalevala._stage, []

        def flaky(p, name):
            if name == "memory":
                raise OSError("exam.jsonl: permission denied")
            return stage(p, name)

        runs = kalevala._tower_runs

        def counted(p, name):
            calls.append(name)
            return runs(p, name)
        rune.set_tower("efficiency")
        try:
            with mock.patch.object(kalevala, "_stage", flaky), mock.patch.object(kalevala, "_tower_runs", counted):
                k = self.export()["kalevala"]
            self.assertEqual(rune._TOWER, "efficiency")  # set_tower(None) inside kalevala doesn't leak out
        finally:
            rune.set_tower(None)
        towers = {t["name"]: t for t in k["towers"]}
        self.assertEqual(sorted(towers), ["efficiency", "memory", "problem-solving"])
        self.assertIn("permission denied", towers["memory"]["stage"]["error"])
        self.assertEqual(towers["efficiency"]["stage"]["stage"], "spec")
        self.assertEqual(sorted(calls), sorted(towers))  # watch's runs are the trend: one read per tower
        self.assertNotIn("runs", towers["memory"])
        with mock.patch.object(kalevala, "watch", side_effect=OSError("towers/memory: permission denied")):
            k = self.export()["kalevala"]
        self.assertIn("permission denied", k["error"])
        self.assertNotIn("towers", k)
        m["towers"] = ["not", "a", "map"]
        with open(path, "w") as f:
            json.dump(m, f)
        self.assertIn("error", self.export()["kalevala"])


class KalevalaPage(unittest.TestCase):
    """T-3266: the Rune page's Kalevala view: a city of towers beside the forge tower, a Genesis ⇄ Kalevala switch kept in
    the URL hash, and each tower's aspect, gen, stage, trend, stuck or paused state, Kalevala's gens and the newest acts,
    all from genesis.json's kalevala block, written as text."""
    gdir = os.path.join(SITE, "genesis")

    def test_the_switch_the_city_and_the_section_are_in_the_page(self):
        page = read_text(os.path.join(self.gdir, "index.html"))
        for s in ('data-view="genesis"', 'data-view="kalevala"', 'aria-pressed="true"', 'id="k-towers"', 'id="city"',
                  'id="city-art"', 'id="k-key"', "taller = more generations", "beacon is its state"):
            self.assertIn(s, page)  # the city's key shows, not only in the SVG's title
        css = read_text(os.path.join(self.gdir, "genesis.css"))
        self.assertRegex(css, r"\.city a:focus-visible \.kt-t\{filter:[^}]*drop-shadow")  # a ring Firefox draws on SVG
        reduced = css[css.index("prefers-reduced-motion"):]
        self.assertIn(".city", reduced)  # the pull-back is a cut when motion is reduced
        js = read_text(os.path.join(self.gdir, "genesis.js"))
        for s in ("function kalevala(", "tower|", '"kalevala"', "data-ktower"):
            self.assertIn(s, js)

    def test_the_view_renders_the_block_as_text(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("no node")
        out = subprocess.run([node, os.path.join(PLUGIN, "tests", "genesis_page.js"), self.gdir, "kalevala"],
                             capture_output=True, text=True, timeout=60)
        got = json.loads(out.stdout or "{}")
        want = {"text": True, "markup": False, "city": True, "early": True, "honest": True, "trend": True, "stuck": True,
                "paused": True, "stage": True,
                "spend": True, "gens": True, "acts": True, "share": True, "drawer": True, "none": True, "error": True}
        self.assertEqual(got, want, out.stderr[-2000:])

    def test_current_names_the_page_footer_and_seed_box_teach_sampo_rune(self):  # T-4842, finding 13
        js = read_text(os.path.join(self.gdir, "genesis.js"))
        page = read_text(os.path.join(self.gdir, "index.html"))
        for name, text in (("genesis.js", js), ("genesis/index.html", page)):
            self.assertNotRegex(text, r"\b(?:fm|sampo) genesis\b", name)  # the product's command is sampo rune
        self.assertIn('"Exported by sampo rune export', js)
        self.assertIn("Exported by sampo rune export", page)


STALE_RECORD = ("414 lanes", "570</span> commands", "1,662 tasks", "7,390 runs", "58.5 ms", "3 min 14 s", "1,053 runs",
                "wildest of 58 moonshots", "to 2 November 2026", 'class="x135"', "Nine more")


class RecordFromData(unittest.TestCase):
    """T-4842, findings 25 and 26: the Record, the Ward's blocks and the Moons caption were figures baked into
    site/index.html from a design-time run; they now come from stats.json and genesis.json at every build."""
    ROWS = [{"id": "M-01", "wild": 5, "stage": "idea"}, {"id": "M-02", "wild": 4, "stage": "kill"},
            {"id": "M-03", "wild": 4, "stage": "idea"}, {"id": "M-04", "wild": None, "stage": "idea"},
            {"id": "M-05", "wild": 2, "stage": "idea"}]

    @staticmethod
    def day(d, tasks, ev, lanes=0):
        return {"day": d, "FEATURE": tasks, "FIX": 0, "CLEAN": 0, "PERFORMANCE": 0, "SECURITY": 0, "RESEARCH": 0,
                "lanes": lanes, "evidence": ev}

    def stats(self):
        return {"generated": "2026-12-09",
                "totals": {"tasks_done": 30, "evidence": 90, "proofs": 15, "commits": 5, "gate_runs": 7,
                           "lanes_merged": 7, "guard_blocks": 40},
                "daily": [self.day("2026-12-07", 10, 30, 1), self.day("2026-12-08", 15, 40), self.day("2026-12-09", 5, 20)],
                "guard": {"core": 25, "brief": 10}, "gate_seconds": {"runs": 7, "median": 61.0},
                "bench": {"PreToolUse:Bash": {"p50": 33.3, "p95": 40.0, "budget": 150}}}

    def setUp(self):
        sys.path.insert(0, SITE)
        try:
            import build
        finally:
            sys.path.pop(0)
        self.build = build
        self.template = read_text(os.path.join(SITE, "index.html"))

    def test_counts_from_data_the_template_holds_no_figure_the_build_can_fill(self):
        for stale in STALE_RECORD:
            self.assertNotIn(stale, self.template)
        self.assertNotIn('<li class="x13', self.template)  # the Ward's kinds are rows the build writes
        self.assertNotIn("<figure class=\"gauge\">", self.template)
        self.assertNotIn("<g class=\"d\"", self.template)  # the ledger's ingots

    def test_counts_from_data_the_record_the_ward_and_the_moons_follow_the_data(self):
        page = self.build.fill_record(self.template, self.stats(), self.ROWS)
        for stale in STALE_RECORD:
            self.assertNotIn(stale, page)
        for key in ("lanes", "blocks", "asof", "moons", "kinds", "gauges", "ledger"):
            self.assertNotIn("{{" + key + "}}", page)
        for want in ("7 lanes merged", '<span class="big">40</span> commands blocked', "Five more blocks",
                     "<span>Edits to Sampo itself</span><b>25</b>", "<span>Edits with no task brief</span><b>10</b>",
                     "<b>3.0</b>", "evidence runs per task: 90 runs over 30 tasks", "<b>50</b>", "<b>1 min 1 s</b>",
                     "the median full check, over 7 runs", "<b>33.3 ms</b>", "red is over its 150 ms budget",
                     "Tasks closed each day, 7 to 9 December 2026: 3 working days, 30 tasks in all, "
                     "the most on 8 December with 15.", "Peak: 15 on 8 December",
                     "<tr><td>8 December</td><td>15</td><td>40</td></tr>", "to 9 December 2026",
                     "wildest of 5 moonshots along the ecliptic, wildest first, each numbered",
                     "The twenty wildest of 5 moonshots along the ecliptic over the forge"):
            self.assertIn(want, page)
        self.assertEqual(page.count("1 more"), 0)

    def test_counts_from_data_no_unrecorded_remainder_says_nothing_and_a_gap_in_the_data_is_named(self):
        s = self.stats()
        s["guard"] = {"core": 30, "brief": 10}
        page = self.build.fill_record(self.template, s, self.ROWS)
        self.assertNotIn("more blocks", page)
        del s["bench"]
        with self.assertRaises(SystemExit) as why:
            self.build.fill_record(self.template, s, self.ROWS)
        self.assertIn("bench", str(why.exception))

    def test_counts_from_data_the_wards_budget_mark_stays_inside_the_gauge_when_p50_is_far_under_it(self):
        s = self.stats()
        s["bench"]["PreToolUse:Bash"] = {"p50": 58.0, "p95": 90.0, "budget": 300}
        sys.path.insert(0, SITE)
        try:
            import record
        finally:
            sys.path.pop(0)
        svg = record.gauges(s).split('<figure class="gauge">')[4]
        zone = re.search(r'<path d="M([\d.]+) 35 H600 V55 H[\d.]+ Z" fill="#B12E16"', svg)
        self.assertTrue(zone, "no red over-budget zone drawn")
        self.assertLess(float(zone.group(1)), 600)
        self.assertIn("red is over its 300 ms budget", svg)

    def test_counts_from_data_a_missing_total_fails_the_build_naming_the_key(self):
        for key in ("lanes_merged", "guard_blocks", "tasks_done", "evidence", "proofs"):
            s = self.stats()
            del s["totals"][key]
            with self.assertRaises(SystemExit, msg=key) as why:
                self.build.fill_record(self.template, s, self.ROWS)
            self.assertIn("totals." + key, str(why.exception))

    def test_counts_from_data_a_stale_stats_file_is_named_at_build_time(self):
        note = self.build.stale_note("2026-11-02", today="2026-11-16")
        self.assertIn("14 days old", note)
        self.assertIn("site/stats.py", note)
        self.assertEqual(self.build.stale_note("2026-11-15", today="2026-11-16"), "")
        self.assertEqual(self.build.stale_note("", today="2026-11-16"), "")

    def test_counts_from_data_the_built_page_carries_the_committed_figures(self):
        out = tempfile.mkdtemp(prefix="test-site-record-")
        try:
            self.build.build(out)
            page = read_text(os.path.join(out, "index.html"))
        finally:
            shutil.rmtree(out, ignore_errors=True)
        stats = json.loads(read_text(os.path.join(SITE, "data", "stats.json")))
        rows = json.loads(read_text(os.path.join(SITE, "genesis", "genesis.json")))["moonshots"]["rows"]
        t = stats["totals"]
        self.assertIn(f"{self.build.fmt(t['lanes_merged'])} lanes merged", page)
        self.assertIn(f'<span class="big">{self.build.fmt(t["guard_blocks"])}</span> commands blocked', page)
        self.assertIn(f"wildest of {len(rows)} moonshots", page)
        self.assertIn(f"{self.build.fmt(t['tasks_done'])} tasks in all", page)


class DesignTemplateKeepsDataPlaceholders(unittest.TestCase):
    """T-4868: the design-time generator (design/site/12-forge-illustrated/_src) baked the figures that T-4842 moved to
    site/index.html's {{...}} placeholders: page.html carried the literals and build.py wrote stations.py's drawings of
    them over the slots. A rebuild there and a copy into site/ re-published the two-week-old numbers."""
    SRC = os.path.join(os.path.dirname(PLUGIN), "design", "site", "12-forge-illustrated", "_src")
    KEYS = ("lanes", "blocks", "asof", "kinds", "gauges", "ledger")

    def src(self, name):
        return read_text(os.path.join(self.SRC, name))

    def test_the_template_carries_the_data_placeholders_and_none_of_the_stale_figures(self):
        page = self.src("page.html")
        for key in self.KEYS:
            self.assertIn("{{" + key + "}}", page, key)
        for stale in STALE_RECORD:
            self.assertNotIn(stale, page)
        self.assertNotIn('<div class="totals">', page)  # the landing page's Record has no totals strip of baked counts
        self.assertNotIn('<li style="--v:', page)  # the Ward's kinds are rows the build writes from stats.json

    def test_the_generator_leaves_the_data_keys_alone_and_never_draws_the_record_from_stations(self):
        build = self.src("build.py")
        self.assertNotIn("stations.gauges", build)
        self.assertNotIn("stations.ledger", build)
        self.assertNotIn("stations.LEDGER", build)
        self.assertIn("DATA_KEYS", build)  # the leftover-brace check lets the keys site/build.py:fill_record fills through
        self.assertRegex(build, r"for k, v in rep\.items\(\):")
        for key in ("GAUGES", "LEDGER", "LEDGER_ROWS"):
            self.assertNotIn("'" + key + "'", build, key)  # no replacement is offered for the page's slots
        for key in self.KEYS:
            self.assertNotIn("'" + key + "':", build, key)

    def test_the_split_page_is_checked_for_a_leaked_placeholder_besides_the_data_keys(self):
        build = self.src("build.py")
        tail = build[build.index("split = page.replace"):build.index("open(os.path.join(site, 'index.html')")]
        self.assertIn("for k in DATA_KEYS", tail)  # the six keys are lifted out, and only they
        self.assertRegex(tail, r"assert '\{\{' not in kept, 'split page still has a placeholder")

    def test_stations_says_its_record_drawings_are_sketches_the_page_never_takes(self):
        stations = self.src("stations.py")
        self.assertIn("T-4868", stations)
        self.assertIn("never written into the page", stations)


class CurrentNames(unittest.TestCase):
    """T-4842, findings 10 and 11: the diagrams say the Ward and sampo oops, never Guard or fm oops."""
    DIAGRAMS = os.path.join(SITE, "assets", "diagrams")

    def test_current_names_the_loop_and_guard_diagrams_say_the_ward_and_sampo_oops(self):
        for name in ("loop", "loop-light", "loop-dark", "guard", "guard-light", "guard-dark"):
            svg = read_text(os.path.join(self.DIAGRAMS, name + ".svg"))
            self.assertIn("The Ward", svg, name)
            self.assertNotRegex(svg, r"\bGuard\b|\bfm oops\b|\bForeman\b", name)
            if name.startswith("guard"):
                self.assertIn("sampo oops", svg, name)
