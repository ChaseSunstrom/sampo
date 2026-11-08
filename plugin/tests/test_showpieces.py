"""T-1653: showpieces, one-file 3D pages built on the scene template's survival contract. fm page city: the repo as a
city from fm map's data, each file a building (height by lines, lit windows by churn) on a district per folder."""
import html
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helpers import PLUGIN, SPY, ForemanTestCase, anim_time, at_rest, busy_factor, devtools, headless_chrome, polls, read_text

from sampo import core as c
from sampo import page as spage

FILES = [  # fixture: what fmpage.city_files reads from a repo (path, lines, commits in 180 days)
    {"path": "plugin/lib/fmcore.py", "lines": 9000, "churn": 120},
    {"path": "plugin/lib/fmcli.py", "lines": 6200, "churn": 300},
    {"path": "plugin/lib/fmpage.py", "lines": 280, "churn": 4},
    {"path": "plugin/tests/test_core.py", "lines": 3100, "churn": 40},
    {"path": "plugin/tests/test_page.py", "lines": 160, "churn": 0},
    {"path": "site/build.py", "lines": 700, "churn": 9},
    {"path": "README.md", "lines": 400, "churn": 2},
    {"path": "MASTER.md", "lines": 90, "churn": 0},
] + [{"path": f"plugin/skills/library/cards/card{i}.md", "lines": 30 + i, "churn": i % 3} for i in range(24)]
# The template's survival code, which the city copies (the site hero once did too): neither may drift alone.
SURVIVAL = (r'"webglcontextlost", e => \{ e\.preventDefault\(\)', r'"webglcontextrestored"',
            r'Math\.min\(window\.devicePixelRatio \|\| 1, matchMedia\("\(pointer: coarse\)"\)\.matches \? 1\.5 : 2\)',
            r'on\(document, "visibilitychange"',
            r"new IntersectionObserver\(entries => \{ onscreen = entries\.at\(-1\)\.isIntersecting",
            r'matchMedia\("\(prefers-reduced-motion: reduce\)"\)', r"failIfMajorPerformanceCaveat: true")
TEMPLATE = os.path.join(PLUGIN, "skills", "playbooks", "references", "ui", "templates", "scene.html")


def data_of(page, fig="city"):
    return json.loads(re.search(rf'<script type="application/json" id="{fig}">(.*?)</script>', page, re.S).group(1))


class OneFile:
    """T-1654: the checks every showpiece is held to, whatever it draws: one file with the poster first and nothing
    from another host, a named canvas over an aria-hidden poster, a short intro any input ends, reduced motion and no
    WebGL2 keeping the poster with no context made, the scene template's survival code, a clean design lint and
    a script node parses. A subclass sets FIG (the figure's class and the data block's id) and self.page."""
    FIG = None

    def module(self):
        return re.search(r'<script type="module">(.*?)</script>', self.page, re.S).group(1)

    def test_one_file_with_the_poster_first_and_nothing_from_another_host(self):
        page = self.page
        self.assertNotRegex(page, r"__[A-Z]+__", "every placeholder filled")
        fig = re.search(rf'<figure class="{self.FIG}"[^>]*>', page).group(0)
        self.assertIn('data-state="poster"', fig, "the poster is the first paint")
        self.assertLess(page.index('<svg class="poster"'), page.index("<canvas"), "the canvas sits over the poster")
        self.assertIsNone(re.search(r"""<(?:link|script|img|iframe)\b[^>]*\b(?:href|src)=["']?(?:https?:)?//""", page))
        self.assertNotRegex(page, r"<script[^>]*\bsrc=", "inlined: no script file")
        self.assertNotIn("@import", page)
        self.assertNotIn("url(http", page)
        self.assertEqual(page.count("</script>"), 2, "the data block and the module only")
        self.assertIsInstance(data_of(page, self.FIG), dict)

    def test_the_canvas_is_named_and_the_poster_hidden(self):
        canvas = re.search(r"<canvas[^>]*>", self.page).group(0)  # the template's way: named, focusable once live
        self.assertRegex(canvas, r'\brole="img"')
        self.assertNotIn("aria-hidden", canvas)
        self.assertGreater(len(html.unescape(re.search(r'aria-label="([^"]*)"', canvas).group(1))), 40)
        self.assertRegex(canvas, rf'aria-describedby="{self.FIG}-twin"')
        self.assertRegex(self.page, rf'<figcaption id="{self.FIG}-twin">')
        self.assertRegex(re.search(r"<svg class=\"poster\"[^>]*>", self.page).group(0), r'aria-hidden="true"')

    def test_the_intro_is_short_and_any_input_ends_it(self):
        script = self.module()
        self.assertLessEqual(float(re.search(r"\bPASS = ([\d.]+)", script).group(1)), 5, "WCAG 2.2.2: 5 s or a control")
        self.assertRegex(script, r"const turn = [^\n]*pass = PASS", "the first drag or key ends the intro")
        key = re.search(r'<p class="key"[^>]*>(.*?)</p>', self.page, re.S).group(1)
        self.assertIn("drag or arrow keys", key)
        self.assertRegex(self.page, rf'\.{self.FIG}\[data-state="live"\] canvas \{{[^}}]*cursor: grab')

    def test_reduced_motion_no_webgl2_and_the_templates_survival_code(self):
        script = self.module()
        template = read_text(TEMPLATE)
        for marker in SURVIVAL:
            for name, text in ((f"{self.FIG}.html", script), ("templates/scene.html", template)):
                self.assertTrue(re.search(marker, text), f"{name} lost {marker}")
        self.assertIn('setState("still")', script, "reduced motion keeps the poster, with no context")
        self.assertIn('setState("no-webgl2")', script)
        self.assertLess(script.index('setState("still")'), script.index('getContext("webgl2"'),
                        "reduced motion returns before any context is made")
        self.assertRegex(self.page, r"@media \(prefers-reduced-motion: reduce\)")
        if shutil.which("node"):
            js = os.path.join(self.tmp, f"{self.FIG}.mjs")
            with open(js, "w", encoding="utf-8") as f:
                f.write(script)
            r = subprocess.run(["node", "--check", js], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_design_lint_is_clean(self):
        from sampo import design
        path = os.path.join(self.tmp, f"{self.FIG}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.page)
        self.assertEqual(design.lint([path]), [])


class City(OneFile, ForemanTestCase):
    """reruns: OneFile's checks on this figure: every showpiece is held to them (T-1654)."""
    FIG = "city"

    def setUp(self):
        super().setUp()
        self.page = spage.city_html(FILES, "Foreman")

    def test_a_hostile_folder_and_file_name_is_escaped_everywhere_it_shows(self):
        # review: the hostile name sits where it reaches the output: the second-biggest district, the hottest file
        folder, name = 'evil"><b>dir', 'a"><img src=x onerror=1>.py'
        page = spage.city_html(FILES + [{"path": f"{folder}/{name}", "lines": 5000, "churn": 999},
                                         {"path": f"{folder}/</script><b>.py", "lines": 4000, "churn": 1}], "Foreman")
        for raw in ("<img src=x", "<b>dir", "</script><b>"):
            self.assertNotIn(raw, page)
        self.assertEqual(page.count("</script>"), 2, "a path can't close a tag")
        label = re.search(r'<canvas[^>]*aria-label="([^"]*)"', page).group(1)
        twin = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", page, re.S).group(1)
        poster = re.search(r'<svg class="poster".*?</svg>', page, re.S).group(0)
        for where, text in (("label", label), ("twin", twin)):
            self.assertIn(html.escape(name), text, where)
            self.assertIn(html.escape(folder), text, where)
        self.assertIn('<text class="tag" ', poster)
        self.assertIn(f">{html.escape(folder)}</text>", poster)
        self.assertIn(folder, [d["name"] for d in data_of(page)["d"]], "the JSON keeps the name, '<' escaped")
        self.assertIn("evil\\\">\\u003cb>dir", page)

    def test_a_leak_names_the_path(self):
        with self.assertRaisesRegex(spage.Leak, r"/home/someone/x\.py"):
            spage.city_html(FILES + [{"path": "/home/someone/x.py", "lines": 3, "churn": 0}], "x")

    def test_history_unavailable_is_not_every_window_dark(self):
        page = spage.city_html([dict(f, churn=0) for f in FILES], "Foreman", history=False)
        label = html.unescape(re.search(r'<canvas[^>]*aria-label="([^"]*)"', page).group(1))
        self.assertIn("history unavailable", label)
        self.assertNotIn("every window is dark", label)

    def test_estimated_lines_past_the_read_budget_are_said_on_the_page(self):
        page = spage.city_html(FILES, "Foreman", estimated=7)
        twin = html.unescape(re.search(r"<figcaption[^>]*>(.*?)</figcaption>", page, re.S).group(1))
        self.assertRegex(twin, r"7 files.*estimated from their size.*256 MB")

    def test_the_label_summarises_the_biggest_areas_and_the_text_twin_holds_them(self):
        label = html.unescape(re.search(r'<canvas[^>]*aria-label="([^"]*)"', self.page).group(1))
        self.assertIn(f"{len(FILES)} files", label)
        self.assertIn("plugin/lib", label, "the biggest district by lines comes first")
        self.assertLess(label.index("plugin/lib"), label.index("plugin/tests"))
        self.assertIn("fmcli.py", label, "the most changed file is named")
        twin = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", self.page, re.S).group(1)
        for name in ("plugin/lib", "plugin/tests", "site", "plugin/skills/library/cards"):
            self.assertIn(name, html.unescape(twin))

    def test_a_building_per_file_sized_by_lines_on_its_folders_district(self):
        d = data_of(self.page)
        self.assertEqual(len(d["b"]), len(FILES))
        names = [x["name"] for x in d["d"]]
        self.assertEqual(sorted(names), sorted({"plugin/lib", "plugin/tests", "site", "plugin/skills/library/cards",
                                                "."}), "one district per folder; root files on '.'")
        heights = [b[3] for b in d["b"]]
        self.assertEqual(heights, sorted(heights, reverse=True), "biggest file first, tallest")
        self.assertAlmostEqual(heights[0], d["hmax"], places=1)
        self.assertGreater(heights[0], heights[2] * 1.2, "9000 lines stands taller than 3100")
        heat = [b[4] for b in d["b"]]
        self.assertEqual(max(heat), heat[1], "fmcli.py, the most changed, burns brightest")
        self.assertEqual(heat[[f["path"] for f in sorted(FILES, key=lambda f: -f["lines"])].index("MASTER.md")], 0)
        for b in d["b"]:  # every building stands inside its district's plate
            x, z, w, _, _, delay, di = b
            plate = d["d"][di]
            self.assertLessEqual(abs(x - plate["x"]) + w / 2, plate["w"] / 2 + 1e-6, (b, plate))
            self.assertLessEqual(abs(z - plate["z"]) + w / 2, plate["d"] / 2 + 1e-6, (b, plate))
            self.assertTrue(0 <= delay <= 1)
        plates = [(p["x"] - p["w"] / 2, p["x"] + p["w"] / 2, p["z"] - p["d"] / 2, p["z"] + p["d"] / 2) for p in d["d"]]
        for i, a in enumerate(plates):  # districts never overlap
            for b in plates[i + 1:]:
                self.assertTrue(a[1] <= b[0] or b[1] <= a[0] or a[3] <= b[2] or b[3] <= a[2], (a, b))
        self.assertEqual(len(re.findall(r'<path class="b', self.page)), 2 * len(FILES), "the poster draws each one")

    def test_an_absolute_path_is_refused(self):
        with self.assertRaises(spage.Leak):
            spage.city_html(FILES + [{"path": "/home/someone/x.py", "lines": 3, "churn": 0}], "x")

    def test_fm_page_city_reads_the_repo_and_never_overwrites_a_foreign_file(self):
        self.fm("init")
        for path, lines in (("src/app.py", 120), ("src/util.py", 30), ("docs/guide.md", 60), ("setup.py", 8)):
            os.makedirs(os.path.join(self.repo, os.path.dirname(path) or "."), exist_ok=True)
            with open(os.path.join(self.repo, path), "w") as f:
                f.write("x\n" * lines)
        with open(os.path.join(self.repo, "logo.png"), "wb") as f:
            f.write(b"\x89PNG\0\0\0binary")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "files"], check=True)
        with open(os.path.join(self.repo, "src", "app.py"), "a") as f:
            f.write("y\n")
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "again"], check=True)
        r = json.loads(self.fm("page", "city", "--json").stdout)
        p = c.find_project(self.repo)
        self.assertEqual(r["path"], os.path.join(p.dir, "city.html"))
        d = data_of(read_text(r["path"]))
        tracked = subprocess.run(["git", "-C", self.repo, "ls-files"], capture_output=True, text=True).stdout.split()
        self.assertIn("logo.png", tracked)
        self.assertEqual(r["files"], len(tracked) - 1, "the binary file is no building")
        self.assertEqual(len(d["b"]), r["files"])
        self.assertEqual(sorted(x["name"] for x in d["d"]), [".", "docs", "src"])
        self.assertEqual(max(b[4] for b in d["b"]), d["b"][0][4], "app.py: the tallest and the most changed")
        self.assertNotIn(self.tmp, read_text(r["path"]))
        self.fm("page", "city")  # a second run replaces its own page
        mine = os.path.join(self.tmp, "mine.html")
        with open(mine, "w") as f:
            f.write("<p>mine</p>")
        r = self.fm("page", "city", "--out", mine, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(read_text(mine), "<p>mine</p>")

    def _commit(self, path, text, msg):
        os.makedirs(os.path.dirname(os.path.join(self.repo, path)), exist_ok=True)
        with open(os.path.join(self.repo, path), "a") as f:
            f.write(text)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", msg], check=True)

    def test_a_project_in_a_subfolder_gets_its_own_churn(self):
        # review: ls-files paths are relative to the project, git log's to the repo root without --relative
        self._commit("sub/app.py", "x\n", "one")
        self._commit("sub/app.py", "y\n", "two")
        self._commit("other.py", "z\n", "three")
        files, info = spage.city_files(os.path.join(self.repo, "sub"))
        self.assertEqual([(f["path"], f["lines"], f["churn"]) for f in files], [("app.py", 2, 2)])
        self.assertTrue(info["history"])

    def test_a_failed_git_log_is_history_unavailable(self):
        from unittest import mock
        from sampo import map as smap
        self._commit("app.py", "x\n", "one")
        real = smap._git
        broken = lambda root, *a, **kw: (subprocess.CompletedProcess(a, 124, "", "timed out") if "log" in a
                                         else real(root, *a, **kw))
        with mock.patch.object(smap, "_git", broken):
            files, info = spage.city_files(self.repo)
        self.assertFalse(info["history"])
        self.assertEqual([f["path"] for f in files if f["path"] == "app.py"], ["app.py"])

    def test_past_the_read_budget_lines_are_estimated_from_size(self):
        from unittest import mock
        for i in range(3):  # 10, 20 and 30 KB: past the 8 KB head every file gets
            self._commit(f"f{i}.txt", ("x" * 99 + "\n") * (100 * (i + 1)), f"c{i}")
        with mock.patch.object(spage, "READ_BUDGET", 16000):
            files, info = spage.city_files(self.repo)
        lines = {f["path"]: f["lines"] for f in files}
        self.assertEqual(lines["f2.txt"], 300, "read in full: inside the budget")
        self.assertGreater(info["estimated"], 0)
        self.assertAlmostEqual(lines["f0.txt"], 100, delta=5, msg="estimated from bytes per line so far")


class OneFileBrowser:
    """A showpiece in a real headless Chromium over DevTools, real time (virtual time never runs frame callbacks).
    T-1654: shared by every showpiece; a subclass sets FIG and page()."""
    FIG = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, f"{cls.FIG}.html")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(cls.page())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def spied(self, until, media=(), init="", expr=None):
        """T-1728: polled until `until` holds, not read at fixed seconds a loaded machine's slow frames outran."""
        browser = headless_chrome()
        if not browser:
            self.skipTest("no headless Chromium (FOREMAN_CHROME, chrome-headless-shell or Playwright's download)")
        try:
            return devtools(browser, "file://" + self.path, tempfile.mkdtemp(dir=self.tmp), SPY + init, media, polls(),
                            expr or f"({{ ...window.__spy, state: document.querySelector('.{self.FIG}').dataset.state }})",
                            until=until)
        except (EOFError, BrokenPipeError, FileNotFoundError, PermissionError) as e:  # a root sandbox, no libs
            self.skipTest(f"{browser} didn't take DevTools over a pipe: {e!r}")

    def settled(self, media=(), init=""):
        """The last poll once the page has left its poster for two polls in a row."""
        return self.spied(lambda g: len(g) >= 2 and all(s and s["state"] != "poster" for s in g[-2:]), media, init)[-1]

    def test_it_draws_frames_in_real_time_then_rests(self):
        got = self.spied(at_rest)
        end = got[-1]
        self.assertTrue(at_rest(got), f"the frames never stopped: {len(got)} polls, last {end and end['raf']} frames")
        self.assertEqual(end["state"], "live", end)
        self.assertIn("webgl2", end["contexts"])
        self.assertGreater(end["raf"], 10 / busy_factor(), "the rise and the turn draw frames in real time")
        stamps = end["stamps"]
        self.assertGreaterEqual(stamps[-1] - stamps[0], 4500, "the 4.5 s pass ran out in real time before the rest")

    def test_the_first_key_ends_the_intro(self):
        # review (WCAG 2.2.2): input stops the auto motion; frames stop once the turn it asked for settles.
        # T-1728: the key goes in at the first poll that finds the scene live, and the turn is judged in the page's
        # animation time (each frame's step capped at 0.1 s), which a loaded machine's slow frames don't stretch
        probe = f"""(() => {{ const f = document.querySelector('.{self.FIG}'), s = window.__spy;
          if (f.dataset.state === 'live' && !s.keyAt) {{ s.keyAt = performance.now();
            f.querySelector('canvas').dispatchEvent(new KeyboardEvent('keydown', {{ key: 'ArrowRight' }})); }}
          return {{ ...s, state: f.dataset.state }}; }})()"""
        got = self.spied(lambda g: bool(g[-1] and g[-1].get("keyAt")) and at_rest(g) and "keyAt" in g[-3], expr=probe)
        end = got[-1]
        self.assertTrue(end and end.get("keyAt"), "the scene never went live to take the key")
        self.assertTrue(at_rest(got), f"the frames never stopped after the key: {end['raf']} frames")
        # the step settles in about 1 s of animation time; the intro, had the key not ended it, runs 4.5 s
        self.assertLess(anim_time(end["stamps"], since=end["keyAt"]), 2.0, "no frames past the turn the key asked for")

    def test_reduced_motion_and_no_webgl2_keep_the_poster_still(self):
        still = self.settled([("prefers-reduced-motion", "reduce")])
        self.assertEqual((still["state"], still["raf"]), ("still", 0))
        self.assertNotIn("webgl2", still["contexts"], "reduced motion asks for no WebGL context at all")
        none = """const get = HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext = function (t, ...r) { return t === "webgl2" ? null : get.call(this, t, ...r); };"""
        flat = self.settled(init=none)
        self.assertEqual((flat["state"], flat["raf"]), ("no-webgl2", 0))

    def test_a_lost_context_shows_the_poster_and_comes_back(self):
        # T-1728: lost once the scene is live, restored once it shows lost, read until it is live again
        probe = f"""(() => {{ const f = document.querySelector('.{self.FIG}'), s = window.__spy, was = f.dataset.state;
          if (was === 'live' && !s.lost) {{ s.lost = 1;  // kept: a lost context hands out no extension
            s.ext = f.querySelector('canvas').getContext('webgl2').getExtension('WEBGL_lose_context'); s.ext.loseContext(); }}
          else if (was === 'lost' && s.lost === 1) {{ s.lost = 2; s.ext.restoreContext(); }}
          return was; }})()"""
        seq = lambda g: [k for k, _ in itertools.groupby(g) if k not in (None, "poster")]
        got = self.spied(lambda g: seq(g)[-3:] == ["live", "lost", "live"], expr=probe)
        self.assertEqual(seq(got), ["live", "lost", "live"], got)


class CityBrowser(OneFileBrowser, unittest.TestCase):
    """reruns: OneFileBrowser's headless checks on this figure: every showpiece is held to them (T-1654)."""
    FIG = "city"

    @staticmethod
    def page():
        return spage.city_html(FILES, "Foreman")


TASKS = [  # fixture: what fmpage.graph_tasks reads from fm state (id, title, status, type, tier, deps)
    {"id": "T-0001", "title": "Baseline the suite", "status": "done", "type": "CLEAN", "tier": "S", "deps": []},
    {"id": "T-0002", "title": "Schema v2", "status": "done", "type": "FEATURE", "tier": "M", "deps": ["T-0001"]},
    {"id": "T-0003", "title": "The API on schema v2", "status": "active", "type": "FEATURE", "tier": "L",
     "deps": ["T-0002"]},
    {"id": "T-0004", "title": "Document the API", "status": "planned", "type": "FEATURE", "tier": "S",
     "deps": ["T-0002", "T-0003"]},
    {"id": "T-0005", "title": "Fix the login race", "status": "blocked", "type": "FIX", "tier": "S",
     "deps": ["T-0003", "T-9999"]},  # T-9999 isn't in the graph: no edge
    {"id": "T-0006", "title": "Look at caching", "status": "captured", "type": "RESEARCH", "tier": "M", "deps": []},
    {"id": "T-0007", "title": "Old idea", "status": "dropped", "type": "FEATURE", "tier": "S", "deps": ["T-0001"]},
] + [{"id": f"T-{100 + i:04d}", "title": f"Loose end {i}", "status": "planned", "type": "CLEAN", "tier": "S",
      "deps": []} for i in range(30)]


class Graph(OneFile, ForemanTestCase):
    """T-1654: fm page graph, the task graph as a one-file 3D page: tasks as nodes in layers by what they wait on,
    depends_on as edges, status as colour, held to the city's checks (OneFile).
    reruns: OneFile's checks on this figure, as the other showpieces run them (T-1654)."""
    FIG = "graph"

    def setUp(self):
        super().setUp()
        self.page = spage.graph_html(TASKS, "Foreman", when="2026-10-22")

    def nodes(self, page=None):
        d = data_of(page or self.page, "graph")
        return d, {t["id"]: n for t, n in zip(d["t"], d["n"])}

    def test_poster_labels_in_a_dense_cluster_never_overlap(self):  # T-1735: T-0352 and T-0337 overlapped
        blocked = spage.GRAPH_STATUSES.index("blocked")
        plan = {"layers": [{"x": 0, "w": 8, "d": 8}], "e": [],
                "n": [[0, 0, 1, blocked], [0.3, 0, 1, blocked], [0, 0.3, 1, blocked], [3, -3, 1, blocked]]}
        svg = spage._graph_poster(plan, {0: "T-0352", 1: "T-0337", 2: "T-0400", 3: "T-0500"})
        tags = [(float(x), float(y), t) for x, y, t in re.findall(r'<text class="tag" x="([\d.]+)" y="([\d.]+)">([^<]*)<', svg)]
        self.assertEqual([t for *_, t in tags], ["T-0352", "T-0500"], "the first of a cluster is named, the far one too")
        for (ax, ay, a), (bx, by, b) in itertools.combinations(tags, 2):
            self.assertFalse(abs(ax - bx) < 0.15 * (len(a) + len(b)) and abs(ay - by) < 0.6, (a, b))

    def test_a_node_per_task_in_layers_by_what_it_waits_on(self):
        d, at = self.nodes()
        self.assertEqual(len(d["n"]), len(TASKS))
        layer = {i: n[4] for i, n in at.items()}
        self.assertEqual([layer[f"T-000{i}"] for i in range(1, 6)], [0, 1, 2, 3, 3], "the longest chain sets the layer")
        for t in TASKS:
            for dep in t["deps"]:
                if dep in at:
                    self.assertGreater(layer[t["id"]], layer[dep], (t["id"], dep))
                    self.assertGreater(at[t["id"]][0], at[dep][0], "later layers stand further along x")
        ids = [t["id"] for t in d["t"]]
        edges = sorted((ids[a], ids[b]) for a, b in d["e"])
        self.assertEqual(edges, sorted([("T-0001", "T-0002"), ("T-0002", "T-0003"), ("T-0002", "T-0004"),
                                        ("T-0003", "T-0004"), ("T-0003", "T-0005"), ("T-0001", "T-0007")]),
                         "an edge per dependency in the graph, from what's waited on to the waiter")
        spots = [(n[0], n[1]) for n in d["n"]]
        self.assertEqual(len(spots), len(set(spots)), "no two nodes on one spot")
        first = [n for n in d["n"] if n[4] == 0]  # 32 tasks wait on nothing: a block, not a line of 32
        self.assertEqual(len(first), 32)
        self.assertLessEqual(len({n[1] for n in first}), 12, "a big layer wraps: at most 12 deep")
        self.assertGreaterEqual(len({n[0] for n in first}), 3, "into columns")
        self.assertEqual(spage.graph_html(TASKS, "Foreman", when="2026-10-22"), self.page, "deterministic")

    def test_status_is_the_colour_and_tier_the_height(self):
        d, at = self.nodes()
        self.assertEqual(d["s"][at["T-0003"][3]], "active")
        self.assertEqual(d["s"][at["T-0005"][3]], "blocked")
        self.assertGreater(at["T-0003"][2], at["T-0002"][2], "L stands over M")
        self.assertGreater(at["T-0002"][2], at["T-0001"][2], "M stands over S")
        for s in d["s"]:
            self.assertRegex(self.page, rf"--graph-{s}: #[0-9a-f]{{6}};", f"a colour token per status: {s}")
        self.assertEqual(len(re.findall(r'<path class="n ', self.page)), 2 * len(TASKS), "the poster draws each one")
        self.assertIn('class="n side st-active"', self.page)
        self.assertEqual(len(re.findall(r'<path class="e"', self.page)), len(d["e"]), "and each edge")

    def test_status_colours_read_apart_and_against_the_ground(self):
        # review: graphics need 3:1 on what they stand on; red/green and blue/violet pairs differ in lightness too
        from sampo import design
        tok = lambda k: design.parse_colour(re.search(rf"--graph-{k}: (#[0-9a-f]{{6}});", self.page).group(1))
        for s in spage.GRAPH_STATUSES:
            for under in ("floor", "bg"):
                self.assertGreaterEqual(design.contrast(tok(s), tok(under)), 3, (s, under))
        for a, b in (("blocked", "done"), ("planned", "captured")):
            self.assertGreaterEqual(design.contrast(tok(a), tok(b)), 1.8, f"{a} and {b} differ in lightness")

    def test_blocked_and_done_carry_a_mark_besides_colour(self):
        d, _ = self.nodes()
        st = [d["s"][n[3]] for n in d["n"]]
        self.assertEqual(len(re.findall(r'<path class="mark hatch"', self.page)), st.count("blocked"))
        self.assertEqual(len(re.findall(r'<path class="mark ring"', self.page)), st.count("done"))
        self.assertIn('<pattern id="hatch"', self.page)
        self.assertRegex(self.module(), r"mark == 1\.0", "the scene hatches a blocked roof")
        self.assertRegex(self.module(), r"mark == 2\.0", "and rings a done one")
        for s, cls in (("blocked", "hatch"), ("done", "ring")):
            self.assertRegex(self.page, rf'<span class="st-{s}"><i class="{cls}"></i>{s}</span>', "the legend too")

    def test_the_poster_names_the_active_and_blocked_tasks(self):
        poster = re.search(r'<svg class="poster".*?</svg>', self.page, re.S).group(0)
        names = re.findall(r'<text class="tag"[^>]*>([^<]*)</text>', poster)
        self.assertEqual(sorted(names), ["T-0003", "T-0005"], "the city's poster names its districts; this its work")
        many = TASKS + [{"id": f"T-{300 + i:04d}", "title": "x", "status": "blocked", "tier": "S", "deps": []}
                        for i in range(40)]
        poster = re.search(r'<svg class="poster".*?</svg>', spage.graph_html(many, "x"), re.S).group(0)
        n = len(re.findall(r'<text class="tag"', poster))  # T-1735: colliding labels in the 40 are dropped too
        self.assertTrue(0 < n <= spage.GRAPH_NAMED, f"capped: {n}")
        self.assertIn(">T-0003</text>", poster, "active work named first")

    def test_a_phone_keeps_a_compact_legend(self):
        self.assertNotRegex(self.page, r"\.key \{ display: none", "the legend shows at every width")
        self.assertRegex(self.page, r"@media \(max-width: 40rem\) \{[^\n]*\.key \.wide \{ display: none")

    def test_the_survival_block_matches_the_citys(self):
        # review: graph.html copies city.html's frame loop, budget, survival and input code; neither drifts alone
        # (the extraction to one shared fill is its own task)
        block = lambda text: re.sub(r"\b(city|graph)\b", "X", re.search(
            r"  // Budget: DPR capped.*?turn\(\.\.\.k\); \}\);\n", text, re.S).group(0))
        self.assertEqual(block(self.module()), block(read_text(os.path.join(PLUGIN, "lib", "city.html"))))

    def test_the_label_counts_by_status_and_names_the_active_work_and_the_longest_chain(self):
        label = html.unescape(re.search(r'<canvas[^>]*aria-label="([^"]*)"', self.page).group(1))
        self.assertIn(f"{len(TASKS)} tasks", label)
        self.assertIn("6 dependencies", label)
        self.assertIn("2 done", label)
        self.assertIn("31 planned", label)
        self.assertIn("T-0003 The API on schema v2", label, "the active task is named")
        self.assertRegex(label, r"longest chain: 4 tasks, T-0001 .*T-0004")
        twin = html.unescape(re.search(r"<figcaption[^>]*>(.*?)</figcaption>", self.page, re.S).group(1))
        for t in TASKS[:6]:
            self.assertIn(f'{t["id"]} {t["title"]}', twin, "every task lives in the text twin")
        self.assertIn("waits on T-0002, T-0003", twin)

    def test_a_cycle_still_lays_out_and_is_said(self):
        loop = [{"id": "T-0001", "title": "a", "status": "planned", "type": "FIX", "tier": "S", "deps": ["T-0002"]},
                {"id": "T-0002", "title": "b", "status": "planned", "type": "FIX", "tier": "S", "deps": ["T-0001"]},
                {"id": "T-0003", "title": "c", "status": "planned", "type": "FIX", "tier": "S", "deps": ["T-0002"]}]
        page = spage.graph_html(loop, "x")
        d, at = self.nodes(page)
        self.assertEqual(len(d["n"]), 3)
        self.assertEqual(len(d["e"]), 3)
        self.assertGreater(at["T-0003"][4], at["T-0002"][4])
        self.assertIn("2 tasks wait on each other in a cycle", html.unescape(page))

    def test_a_cycle_is_broken_at_its_own_lowest_id_not_at_a_waiter(self):
        # review: T-0001 waits on a cycle (T-0002 <-> T-0003); it must stand after it, not be forced to the front
        loop = [{"id": "T-0001", "title": "a", "status": "planned", "tier": "S", "deps": ["T-0002"]},
                {"id": "T-0002", "title": "b", "status": "planned", "tier": "S", "deps": ["T-0003"]},
                {"id": "T-0003", "title": "c", "status": "planned", "tier": "S", "deps": ["T-0002"]}]
        page = spage.graph_html(loop, "x")
        _, at = self.nodes(page)
        self.assertEqual(at["T-0002"][4], 0, "the cycle broken at its lowest id")
        self.assertGreater(at["T-0003"][4], at["T-0002"][4])
        self.assertGreater(at["T-0001"][4], at["T-0002"][4], "the waiter stands after what it waits on")
        self.assertIn("2 tasks wait on each other in a cycle", html.unescape(page))

    def test_a_closed_task_linked_only_to_an_unloaded_one_is_left_out(self):
        # review: archived briefs aren't loaded; a dep on one draws no edge, so it doesn't keep a closed task
        rows = [{"id": "T-0900", "status": "done", "deps": ["T-0100"]},
                {"id": "T-0901", "status": "done", "deps": ["T-0902"]},
                {"id": "T-0902", "status": "done", "deps": []},
                {"id": "T-0903", "status": "planned", "deps": ["T-0100"]}]
        kept = spage.graph_keep(rows)
        self.assertEqual([t["id"] for t in kept], ["T-0901", "T-0902", "T-0903"])
        self.assertEqual(kept[-1]["deps"], [], "a dep on a task not loaded is dropped")

    def test_a_long_chain_lays_out_without_recursion(self):
        chain = [{"id": f"T-{i:04d}", "title": "x", "status": "planned", "type": "FIX", "tier": "S",
                  "deps": [f"T-{i - 1:04d}"] if i else []} for i in range(1500)]
        d, at = self.nodes(spage.graph_html(chain, "x"))
        self.assertEqual(at["T-1499"][4], 1499)

    def test_past_the_cap_the_newest_are_kept_and_the_page_says_so(self):
        from unittest import mock
        with mock.patch.object(spage, "GRAPH_MAX", 10):
            page = spage.graph_html(TASKS, "Foreman")
        d, _ = self.nodes(page)
        self.assertEqual(len(d["n"]), 10)
        self.assertIn("T-0129", [t["id"] for t in d["t"]], "the newest kept")
        self.assertIn(f"{len(TASKS) - 10} older tasks are not drawn", html.unescape(page))

    def test_past_the_edge_cap_the_first_are_drawn_and_the_page_says_so(self):
        from unittest import mock
        with mock.patch.object(spage, "GRAPH_EDGES", 4):
            page = spage.graph_html(TASKS, "Foreman")
        d, _ = self.nodes(page)
        ids = [t["id"] for t in d["t"]]
        self.assertEqual([(ids[a], ids[b]) for a, b in d["e"]], [("T-0001", "T-0002"), ("T-0002", "T-0003"),
                                                                 ("T-0002", "T-0004"), ("T-0003", "T-0004")])
        self.assertEqual(len(re.findall(r'<path class="e"', page)), 4)
        text = html.unescape(page)
        self.assertIn("2 more dependencies are not drawn", text)
        self.assertIn("6 dependencies", text, "the count is all of them")
        self.assertIn("waits on T-0003", text, "the twin still lists every one")

    def test_a_hostile_title_is_escaped_everywhere_it_shows(self):
        bad = 'x"><img src=x onerror=1></script><b>'
        page = spage.graph_html(TASKS + [{"id": "T-0200", "title": bad, "status": "active", "type": "FIX",
                                           "tier": "<img src=x>", "deps": ["T-0003"]},
                                          {"id": "T-0201", "title": "odd status", "status": "<b>x", "type": "FIX",
                                           "tier": "S", "deps": []}], "Foreman")
        for raw in ("<img src=x", "</script><b>", "<b>x"):
            self.assertNotIn(raw, page)
        self.assertEqual(page.count("</script>"), 2, "a title can't close a tag")
        self.assertIn(bad, [t["title"] for t in data_of(page, "graph")["t"]], "the JSON keeps it, '<' escaped")
        label = re.search(r'<canvas[^>]*aria-label="([^"]*)"', page).group(1)
        twin = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", page, re.S).group(1)
        for where, text in (("label", label), ("twin", twin)):
            self.assertIn(html.escape(f"T-0200 {bad}"), text, where)
        self.assertIn(html.escape(f"T-0200 {bad}") + " (active)", twin, "an unknown tier is dropped, not shown")

    def test_a_leak_names_the_task(self):
        with self.assertRaisesRegex(spage.Leak, r"T-0300"):
            spage.graph_html(TASKS + [{"id": "T-0300", "title": "see /home/someone/x.py", "status": "planned",
                                        "type": "FIX", "tier": "S", "deps": []}], "x")

    def test_graph_scene_is_a_box_per_task_in_the_pages_colours(self):  # T-1655
        plan = spage.graph_layout(TASKS)
        scene = spage.graph_scene(plan)
        boxes = [m for m in scene["meshes"] if m["material"] != "floor"]
        self.assertEqual(len(boxes), len(TASKS))
        self.assertEqual(len(scene["meshes"]) - len(boxes), len(plan["layers"]))
        tpl = read_text(spage.GRAPH_TEMPLATE)
        for name, mat in scene["materials"].items():
            self.assertIn(f"--graph-{name}: {mat['color']};", tpl, "every status reads the page's token")
        self.assertIn(f"--graph-bg: {scene['background']};", tpl)
        tall = {m["geometry"]["size"][1] for m in boxes}
        self.assertEqual(tall, set(spage.TIER_HEIGHT.values()), "height by tier")
        lines = spage.graph_cells(TASKS, cols=48)
        self.assertTrue(all(ln.count("▀") == 48 for ln in lines[:-1]))
        self.assertGreater(len(set(re.findall(r"\d+;\d+;\d+", "".join(lines[:-1])))), 4, "the boxes are drawn")
        key = re.sub(r"\x1b\[[0-9;]*m", "", lines[-1])
        for words in ("2 done", "1 active", "1 blocked", "31 planned", "1 dropped"):
            self.assertIn(words, key, "status is named, not colour alone")
        self.assertEqual(lines, spage.graph_cells(TASKS, cols=48))
        with self.assertRaises(ValueError):
            spage.graph_cells(TASKS, cols=1)
        chain = [{"id": f"T-{i + 1:04d}", "title": "", "status": "planned", "tier": "M",
                  "deps": [f"T-{i:04d}"] if i else []} for i in range(12)]  # 40 layers in 64 columns: sub-pixel boxes
        bg = "13;16;19"
        cols = [{j for j, c in enumerate(re.findall(r"38;2;(\d+;\d+;\d+)m", ln)) if c != bg}
                for ln in spage.graph_cells(chain, cols=64)[:-1]]
        drawn = set().union(*cols)
        self.assertTrue(drawn and 0 not in drawn and 63 not in drawn, "a 12-layer chain is framed, not cropped")
        self.assertGreater(max(drawn) - min(drawn), 32, "and fills the width")
        many = [dict(t, id=f"T-{i:05d}", deps=[]) for i, t in enumerate(TASKS * 45)]
        self.assertIn(f"({len(many) - spage.GRAPH_MAX:,} older not drawn)", spage.graph_cells(many, cols=8)[-1])

    def test_fm_page_graph_cells_prints_and_writes_nothing(self):  # T-1655
        self.fm("init")
        self.fm_json("task", "new", "Base work", "--type", "FEATURE", "--tier", "S", "--ac", "x :: true")
        out = self.fm("page", "graph", "--cells", "--cols", "30").stdout
        self.assertEqual(out.count("▀"), 30 * 9)
        self.assertIn("1 planned", re.sub(r"\x1b\[[0-9;]*m", "", out))
        self.assertFalse(os.path.exists(os.path.join(c.find_project(self.repo).dir, "graph.html")))
        self.assertNotEqual(self.fm("page", "graph", "--cells", "--cols", "500", check=False).returncode, 0)
        self.assertNotEqual(self.fm("page", "city", "--cells", check=False).returncode, 0, "only the graph")

    def test_fm_page_graph_reads_fm_state_and_never_overwrites_a_foreign_file(self):
        self.fm("init")
        a = self.fm_json("task", "new", "Base work", "--type", "FEATURE", "--tier", "S", "--ac", "x :: true")["id"]
        b = self.fm_json("task", "new", "On top", "--type", "FEATURE", "--tier", "M", "--ac", "x :: true",
                         "--depends", a)["id"]
        self.fm("capture", "A loose idea")
        r = json.loads(self.fm("page", "graph", "--json").stdout)
        p = c.find_project(self.repo)
        self.assertEqual(r["path"], os.path.join(p.dir, "graph.html"))
        page = read_text(r["path"])
        d, at = self.nodes(page)
        self.assertEqual(r["tasks"], len(d["n"]))
        self.assertGreaterEqual(len(d["n"]), 3)
        self.assertGreater(at[b][4], at[a][4], "fm state's depends_on is an edge")
        self.assertNotIn(self.tmp, page)
        self.fm("page", "graph")  # a second run replaces its own page
        mine = os.path.join(self.tmp, "mine.html")
        with open(mine, "w") as f:
            f.write("<p>mine</p>")
        r = self.fm("page", "graph", "--out", mine, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(read_text(mine), "<p>mine</p>")
        out = os.path.join(self.tmp, "graph-copy.html")
        for _ in range(2):  # review: --out over a page fm page graph wrote is replaced, not refused
            self.assertEqual(self.fm_json("page", "graph", "--out", out)["path"], out)
        self.assertIn(b, read_text(out))


class GraphBrowser(OneFileBrowser, unittest.TestCase):
    """reruns: OneFileBrowser's headless checks on this figure: every showpiece is held to them (T-1654)."""
    FIG = "graph"

    @staticmethod
    def page():
        return spage.graph_html(TASKS, "Foreman")


if __name__ == "__main__":
    unittest.main()
