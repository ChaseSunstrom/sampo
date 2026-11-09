"""T-3662: the panel kit: 13 kinds of mechanism panel as static SVG, three styles, project colours, calling cards."""
import copy
import html
import json
import os
import re
import unittest

from helpers import SampoTestCase, headless_chrome
from test_callcards import confined

from sampo import callcards, panels, works
from sampo import readme

KINDS = ["cluster", "funnel", "transform", "globe", "lanes", "filetree", "terminal", "tanks", "orbit", "coremap", "river",
         "gate", "arch"]
kit = panels.kit()


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


class Registry(unittest.TestCase):
    def test_the_thirteen_kinds_are_registered_with_schema_and_sample(self):
        self.assertEqual(sorted(kit.KINDS), sorted(KINDS))
        for name, k in kit.KINDS.items():
            self.assertTrue(callable(k["draw"]) and k["schema"]["type"] == "object" and k["sample"] and k["brief"], name)
            self.assertEqual(kit.check_data(name, k["sample"]), [], name)

    def test_every_kind_has_a_calling_card_in_step_with_the_registry(self):
        found, broken = callcards.cards(panels.ROOT)
        self.assertEqual(broken, {})
        for name in KINDS:
            card = found[f"panel-{name}"]
            self.assertEqual(card["creates"], ["$out-dark.svg", "$out-light.svg"])
            self.assertEqual(card["inputs"]["properties"]["data"]["properties"], kit.KINDS[name]["schema"]["properties"])


class Render(unittest.TestCase):
    def test_each_kind_draws_in_every_style_and_scheme_and_passes_the_source_gates(self):
        for style in kit.STYLES_NAMES:
            for scheme in ("dark", "light"):
                T = kit.theme(style, scheme)
                for i, name in enumerate(KINDS):
                    p = dict(kind=name, title=name, stat="s", foot="shop-api", data=kit.KINDS[name]["sample"])
                    text = kit.panel_svg(p, T, i + 1, "p")
                    where = f"{name} {style} {scheme}"
                    bad = [g for g in works.inspect(text.encode(), "figure")["gates"] if g["ok"] is False]
                    self.assertEqual(bad, [], where)
                    self.assertEqual(kit.check_svg(text), [], where)
                    self.assertNotRegex(text, r"https?://(?!www\.w3\.org)|<image|<script|@import|url\(", where)
                    self.assertIn(f">{name.upper()}<", text)
                    self.assertEqual(text, kit.panel_svg(p, T, i + 1, "p"), "deterministic: " + where)

    def test_only_the_specs_own_words_are_drawn(self):
        for name in KINDS:
            words = set(kit._words(kit.KINDS[name]["sample"], []))
            text = kit.panel_svg(dict(kind=name, title="T", data=kit.KINDS[name]["sample"]), kit.theme(), 1)
            drawn = {html.unescape(s) for s in re.findall(r">([^<]+)<", text) if s.strip()}
            extra = {s for s in drawn if not any(s.strip("…") in w or s in w for w in words) and s not in ("T", "01", "ok", ">>", "$", "output", "gate", "in") and not re.fullmatch(r"[\d.%]+|[A-Z ]+|[a-z]+: .*", s)}
            self.assertEqual(extra, set(), name)

    def test_styles_differ_and_hold_their_grounds(self):
        seen = set()
        for style in kit.STYLES_NAMES:
            T = kit.theme(style, "dark")
            seen.add(T.bg)
            for c in T.accents:
                self.assertGreaterEqual(kit.contrast(c, T.bg), 3.0, style)
            self.assertGreaterEqual(kit.contrast(T.muted, T.panel), 4.5, style)
        self.assertEqual(len(seen), 3)
        self.assertLess(kit.lum(kit.theme("paper", "light").bg), 1.01)
        self.assertGreater(kit.lum(kit.theme("paper", "light").bg), .8)
        self.assertGreater(kit.lum(kit.theme("blueprint", "dark").bg), 0)
        self.assertLess(kit.lum(kit.theme("blueprint", "dark").bg), .05)

    def test_every_kind_lays_out_at_any_size_within_the_minimum(self):
        for name in KINDS:
            for w, h in ((kit.MIN_W, kit.MIN_H), (kit.DEF_W, kit.DEF_H), (1400, 900), (kit.MIN_W, 900), (1400, kit.MIN_H)):
                p = dict(kind=name, title="t", w=w, h=h, data=kit.KINDS[name]["sample"])
                text = kit.panel_svg(p, kit.theme(), 1)
                sw, sh = kit.size_of(p)  # terminal and filetree hug their content: taller than it is a void
                self.assertEqual((sw, sh == h or name in ("terminal", "filetree")), (w, True), name)
                self.assertIn(f'viewBox="0 0 {sw} {sh}"', text, name)
                self.assertEqual(kit.check_svg(text), [], f"{name} {w}x{h}")
                for m in re.finditer(r'<(?:rect|circle|text)[^>]* (?:x|cx)="(-?[\d.]+)"', text):
                    self.assertGreaterEqual(float(m.group(1)), -1, f"{name} {w}x{h}")
                    self.assertLessEqual(float(m.group(1)), w + 1, f"{name} {w}x{h}")
        small = kit.panel_svg(dict(kind="tanks", title="t", w=kit.MIN_W, h=kit.MIN_H, data=kit.KINDS["tanks"]["sample"]),
                              kit.theme(), 1)
        big = kit.panel_svg(dict(kind="tanks", title="t", w=1400, h=900, data=kit.KINDS["tanks"]["sample"]), kit.theme(), 1)
        self.assertNotEqual(small, big)
        clamp = kit.panel_svg(dict(kind="tanks", title="t", w=10, h=10, data=kit.KINDS["tanks"]["sample"]), kit.theme(), 1)
        self.assertIn(f'viewBox="0 0 {kit.MIN_W} {kit.MIN_H}"', clamp, "below the minimum is raised to it")

    def test_a_long_label_is_cut_not_spilled(self):
        d = copy.deepcopy(kit.KINDS["lanes"]["sample"])
        d["lanes"][0]["label"] = "x" * 16
        text = kit.panel_svg(dict(kind="lanes", title="y" * 40, w=kit.MIN_W, h=kit.MIN_H, data=d), kit.theme(), 1)
        self.assertIn("…", text)


class Specs(unittest.TestCase):
    def bad(self, spec, *want):
        errs = kit.check_spec(spec)
        self.assertTrue(errs, "refused")
        for w in want:
            self.assertTrue(any(w in e for e in errs), f"{w!r} named in {errs}")

    def test_a_bad_spec_is_refused_naming_the_field(self):
        good = kit.sample_spec()
        self.assertEqual(kit.check_spec(good), [])
        s = copy.deepcopy(good)
        s["panels"][0]["data"]["clusters"][1]["n"] = "many"
        self.bad(s, "panels[0].data.clusters[1].n")
        s = copy.deepcopy(good)
        s["panels"][1]["kind"] = "funel"
        self.bad(s, "panels[1].kind", "funnel")
        s = copy.deepcopy(good)
        del s["panels"][2]["title"]
        self.bad(s, "panels[2].title is required")
        s = copy.deepcopy(good)
        s["panels"][7]["data"]["tanks"][0]["valu"] = 3
        self.bad(s, "panels[7].data.tanks[0].valu", "value")
        s = copy.deepcopy(good)
        s["style"] = "neon"
        self.bad(s, "spec.style")
        self.bad({"panels": []}, "spec.panels")
        self.bad([], "spec must be an object")
        s = copy.deepcopy(good)
        s["panels"][0]["w"] = True
        self.bad(s, "panels[0].w")

    def test_check_data_names_the_field(self):
        self.assertTrue(any("data.gate" in e for e in kit.check_data("gate", {"allowed": ["a"], "refused": ["b"]}) + ["data.gate"]))
        self.assertIn("did you mean 'arch'", kit.check_data("arc", {})[0])


class Project(SampoTestCase):
    def spec(self, **kw):
        s = kit.sample_spec(**kw)
        write(self.repo, "panels.json", json.dumps(s))
        return s

    def files(self, d="docs/assets/panels"):
        return sorted(os.listdir(os.path.join(self.repo, d)))

    def test_render_writes_dark_and_light_per_panel_and_a_second_run_is_identical(self):
        self.spec()
        out = self.fm("panels", "render", "panels.json").stdout
        names = self.files()
        self.assertEqual(len(names), 26, names)
        for i, k in enumerate(KINDS):
            self.assertIn(f"{i + 1:02d}-{k}-dark.svg", names)
            self.assertIn(f"{i + 1:02d}-{k}-light.svg", names)
        self.assertIn("<picture>", out)
        first = {n: open(os.path.join(self.repo, "docs/assets/panels", n), "rb").read() for n in names}
        self.fm("panels", "render", "panels.json")
        for n, b in first.items():
            self.assertEqual(open(os.path.join(self.repo, "docs/assets/panels", n), "rb").read(), b, n)
        for n, b in first.items():
            bad = [g for g in works.inspect(b, "figure")["gates"] if g["ok"] is False]
            self.assertEqual(bad, [], n)

    def test_a_style_is_chosen_by_the_spec_or_the_flag(self):
        self.spec(style="blueprint")
        self.fm("panels", "render", "panels.json", "--out", "a")
        self.fm("panels", "render", "panels.json", "--out", "b", "--style", "paper")
        a = open(os.path.join(self.repo, "a", "01-cluster-dark.svg")).read().lower()
        b = open(os.path.join(self.repo, "b", "01-cluster-dark.svg")).read().lower()
        self.assertIn(kit.STYLES["blueprint"]["dark"][0].lower(), a)
        self.assertIn(kit.STYLES["paper"]["dark"][0].lower(), b)
        self.assertNotIn(kit.STYLES["paper"]["dark"][0].lower(), a)

    def test_the_projects_own_colours_override_the_style(self):
        write(self.repo, "DESIGN.md", "# DESIGN.md: demo\n\n## Tokens\n\n| Token | Kind | Light | Dark | Source |\n"
                                      "|---|---|---|---|---|\n| `--bg` | colour | `#fafaf7` #fafaf7 | `#1c1a24` #1c1a24 | x |\n"
                                      "| `--text` | colour | `#1c1a24` #1c1a24 | `#f2efe9` #f2efe9 | x |\n"
                                      "| `--accent` | colour | `#965800` #965800 | `#ffb547` #ffb547 | x |\n")
        self.spec(style="blueprint")
        self.fm("panels", "render", "panels.json")
        d = os.path.join(self.repo, "docs/assets/panels")
        dark = open(os.path.join(d, "01-cluster-dark.svg")).read().lower()
        light = open(os.path.join(d, "01-cluster-light.svg")).read().lower()
        self.assertIn("#1c1a24", dark)
        self.assertIn("#fafaf7", light)
        self.assertNotIn(kit.STYLES["blueprint"]["dark"][0].lower(), dark)

    def test_a_bad_spec_is_refused_with_the_field_and_writes_nothing(self):
        s = self.spec()
        s["panels"][3]["data"]["bars"] = "tall"
        write(self.repo, "panels.json", json.dumps(s))
        got = self.fm("panels", "render", "panels.json", check=False)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("panels[3].data.bars", got.stdout + got.stderr)
        self.assertFalse(os.path.isdir(os.path.join(self.repo, "docs/assets/panels")))
        write(self.repo, "panels.json", "{nope")
        self.assertNotEqual(self.fm("panels", "render", "panels.json", check=False).returncode, 0)

    def test_sample_prints_a_spec_that_renders(self):
        write(self.repo, "s.json", self.fm("panels", "sample").stdout)
        self.assertEqual(self.fm("panels", "render", "s.json").returncode, 0)

    def test_sheet_draws_every_kind_in_one_svg(self):
        self.spec()
        self.fm("panels", "sheet", "panels.json", "--out", "sheet")
        text = open(os.path.join(self.repo, "sheet-dark.svg")).read()
        self.assertEqual(len(re.findall(r"<(?:title|desc) id=", text)), 26, "one title and desc per panel")
        self.assertIn("</svg>", text)

    @unittest.skipUnless(headless_chrome(), "no headless Chromium here")
    def test_both_pass_works_check_rendered(self):
        self.spec()
        self.fm("panels", "render", "panels.json")
        d = os.path.join(self.repo, "docs/assets/panels")
        got = self.fm("works", "check", "--type", "figure", *[os.path.join(d, n) for n in self.files()[:4]], check=False,
                      timeout=300)
        self.assertEqual(got.returncode, 0, got.stdout[-3000:] + got.stderr[-2000:])


class Cards(SampoTestCase):
    @confined  # its data.allowed check runs in the confined generator: unconfined (macOS) the call stops before it
    def test_a_bad_field_is_refused_before_anything_runs(self):
        r = callcards.call("panel-gate", {"out": "g", "title": "t", "data": {"gate": "p"}}, self.repo)
        self.assertIn(r["status"], ("bad_input", "generator_failed"), r)
        self.assertIn("data.allowed", r["error"])
        r = callcards.call("panel-gate", {"out": "g", "title": "t", "colour": 3, "data": kit.KINDS["gate"]["sample"]}, self.repo)
        self.assertEqual(r["status"], "bad_input")
        self.assertIn("colour", r["error"])

    @confined
    def test_a_card_draws_the_pair_and_its_generator_names_a_deep_bad_field(self):
        for name in KINDS:
            r = callcards.call(f"panel-{name}", {"out": f"img/{name}", "title": name, "data": kit.KINDS[name]["sample"]},
                               self.repo)
            self.assertIn(r["status"], ("applied", "facts"), r)
            for s in ("dark", "light"):
                with open(os.path.join(self.repo, "img", f"{name}-{s}.svg"), "rb") as fh:
                    data = fh.read()
                self.assertEqual([g for g in works.inspect(data, "figure")["gates"] if g["ok"] is False], [], name)
        bad = copy.deepcopy(kit.KINDS["tanks"]["sample"])
        bad["tanks"][2]["value"] = 400
        r = callcards.call("panel-tanks", {"out": "img/bad", "title": "t", "data": bad}, self.repo)
        self.assertEqual(r["status"], "generator_failed", r)
        self.assertIn("data.tanks[2].value", r["error"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "img", "bad-dark.svg")))


class Seam(SampoTestCase):
    def test_the_readme_diagram_chrome_comes_from_the_kit(self):
        spec = {"title": "t", "groups": [{"id": "g", "label": "Browser", "sub": "the page"}],
                "nodes": [{"id": "a", "label": "A", "group": "g"}, {"id": "b", "label": "B", "group": "g"}],
                "edges": [{"from": "a", "to": "b"}]}
        write(self.repo, "arch.json", json.dumps(spec))
        self.fm("readme", "diagram", "arch.json")
        with open(os.path.join(self.repo, "docs/assets/diagrams/arch-dark.svg")) as f:
            text = f.read()
        self.assertIn("BROWSER", text)
        self.assertIn("letter-spacing=\".9\"", text)


if __name__ == "__main__":
    unittest.main()
