"""T-4996 (a slice of T-3112): SVG technique cards. A grid of technique x work type, a writer's prompt per cell, and an
admit that keeps a card only when its example SVG passes the works gates for the cell's type and shows the technique.
Every model call is faked."""
# map-links: plugin/lib/sampo/svgcards.py plugin/lib/sampo/smallcards.py plugin/lib/sampo/works.py
import json
import os
import unittest
from unittest import mock

from helpers import ForemanTestCase

NS = 'xmlns="http://www.w3.org/2000/svg"'
GRADIENT_ICON = (f'<svg {NS} viewBox="0 0 24 24"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
                 '<stop offset="0" stop-color="#4f8cff"/><stop offset="1" stop-color="#1b4fd1"/></linearGradient></defs>'
                 '<circle cx="12" cy="12" r="9" fill="url(#g)"/></svg>')
FLAT_ICON = f'<svg {NS} viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#4f8cff"/></svg>'
ONE_STOP = GRADIENT_ICON.replace('<stop offset="1" stop-color="#1b4fd1"/>', "")
UNUSED = GRADIENT_ICON.replace('fill="url(#g)"', 'fill="#4f8cff"')
STROKE_ICON = (f'<svg {NS} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
               'stroke-linecap="round"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>')
TWO_STROKES = STROKE_ICON.replace('<path d=', '<path stroke-width="1.5" d=')
PULSE = (f'<svg {NS} viewBox="0 0 24 24"><circle cx="12" cy="12" r="4" fill="currentColor">'
         '<animate attributeName="r" values="4;10;4" dur="0.6s" repeatCount="indefinite"/></circle></svg>')
ONCE = PULSE.replace(' repeatCount="indefinite"', "")
LAYERS = (f'<svg {NS} viewBox="0 0 320 120"><rect width="320" height="120" rx="12" fill="#14213d"/>'
          '<circle cx="120" cy="60" r="40" fill="#fca311" opacity="0.5"/>'
          '<circle cx="150" cy="60" r="40" fill="#e5e5e5"/></svg>')
FLAT_CARD = LAYERS.replace(' opacity="0.5"', "")
SCRIPTED = GRADIENT_ICON.replace("<defs>", "<script>alert(1)</script><defs>")


def good(svg=GRADIENT_ICON, **kw):
    card = {"title": "Shade a round icon with a linear gradient",
            "when": "An icon needs a sense of light from above without a second shape.",
            "steps": [{"do": "Add a linearGradient with two stops inside defs.", "cmd": ""},
                      {"do": "Fill the circle with url of the gradient id.", "cmd": "grep -c linearGradient icon.svg"},
                      {"do": "Run the works gates on the icon and read each gate.",
                       "cmd": "sampo works check icon.svg --type icon"}],
            "example": "A 24 px circle goes from #4f8cff at the top to #1b4fd1 at the bottom, two stops in all.",
            "check": "sampo works check prints every gate as passed for icon.svg.",
            "traps": ["Giving the gradient one stop, which draws a flat fill.", "Naming a gradient id the fill never uses."],
            "svg": svg}
    card.update(kw)
    return card


class _Cards(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        from sampo import core as c
        from sampo import smallcards, svgcards
        self.sv, self.sc = svgcards, smallcards
        self.p = c.find_project(self.repo)
        patch = mock.patch.dict(os.environ, {"SAMPO_CARDS": os.path.join(self.tmp, "cards")})
        patch.start()
        self.addCleanup(patch.stop)

    def cell(self, technique="gradients and lighting", kind="icon"):
        return {"technique": technique, "type": kind}


class Grid(_Cards):
    def test_the_grid_is_every_technique_by_every_works_type(self):
        from sampo import works
        cells = self.sv.cells()
        self.assertEqual(self.sv.TECHNIQUES, ("gradients and lighting", "layering and depth", "icon strokes and grids",
                                              "motion"))
        self.assertEqual(len(cells), len(self.sv.TECHNIQUES) * len(works.TYPES))
        self.assertEqual({(x["technique"], x["type"]) for x in cells},
                         {(t, k) for t in self.sv.TECHNIQUES for k in works.TYPES})
        self.assertTrue(all(x["grid"] == "svg" for x in cells))

    def test_each_cell_has_its_own_prompt_naming_its_bars_and_the_gate_command(self):
        from sampo import works
        prompts = {}
        for cell in self.sv.cells():
            text = self.sv.prompt(cell, 3)
            cap, (lo, hi), colours, _ = works.TYPES[cell["type"]]
            with self.subTest(cell=cell):
                self.assertIn(cell["technique"], text)
                self.assertIn(f"type={cell['type']}", text)
                self.assertIn(f"{min(cap, self.sv.SVG_CAP)} bytes", text)
                self.assertIn(f"{lo} to {hi} shapes", text)
                self.assertIn(f"{colours} colours", text)
                self.assertIn(self.sv.SHOWS[cell["technique"]], text)
                self.assertIn(f"sampo works check example.svg --type {cell['type']}", text)
                self.assertIn('"svg"', text)
            prompts[text] = cell
        self.assertEqual(len(prompts), len(self.sv.cells()), "two cells share a prompt")
        self.assertIn("square whole-number viewBox", self.sv.prompt(self.cell(kind="icon"), 3))

    def test_a_cell_off_the_grid_is_refused(self):
        for bad in ({"technique": "sparkles", "type": "icon"}, {"technique": "motion", "type": "poster"}, None, "icon"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.sv.prompt(bad, 3)


class Admit(_Cards):
    def test_a_card_whose_example_passes_the_gates_and_shows_the_technique_is_kept(self):
        self.assertEqual(self.sv.admit(good(), self.cell()), (True, ""))
        steps = good()["steps"][:2] + [{"do": "Run the works gates on the icon and read each gate.",
                                        "cmd": "sampo works check icon.svg --type icon"}]
        for technique, kind, svg in (("icon strokes and grids", "icon", STROKE_ICON), ("motion", "icon", PULSE),
                                     ("layering and depth", "card", LAYERS)):
            with self.subTest(technique=technique):
                self.assertEqual(self.sv.admit(good(svg, steps=steps), self.cell(technique, kind)), (True, ""))

    def test_an_example_failing_a_works_gate_is_rejected_naming_the_gate(self):
        cases = {"safe": SCRIPTED, "xml": "<svg", "size": GRADIENT_ICON.replace("</svg>", "<!--" + "x" * 3000 + "--></svg>"),
                 "loop": ONCE}
        for gate, svg in cases.items():
            cell = self.cell("motion" if gate == "loop" else "gradients and lighting")
            with self.subTest(gate=gate):
                ok, why = self.sv.admit(good(svg), cell)
                self.assertFalse(ok)
                self.assertTrue(why.startswith(f"gate: {gate}"), why)

    def test_an_icon_off_its_grid_fails_the_grid_gate(self):
        ok, why = self.sv.admit(good(TWO_STROKES), self.cell("icon strokes and grids"))
        self.assertFalse(ok)
        self.assertTrue(why.startswith("gate: grid"), why)

    def test_an_example_that_passes_the_gates_but_lacks_the_technique_is_rejected(self):
        cases = [("gradients and lighting", "icon", FLAT_ICON), ("gradients and lighting", "icon", ONE_STOP),
                 ("gradients and lighting", "icon", UNUSED), ("motion", "icon", FLAT_ICON),
                 ("layering and depth", "card", FLAT_CARD), ("icon strokes and grids", "icon", FLAT_ICON)]
        for technique, kind, svg in cases:
            with self.subTest(technique=technique, svg=svg[60:110]):
                ok, why = self.sv.admit(good(svg), self.cell(technique, kind))
                self.assertFalse(ok)
                self.assertTrue(why.startswith("technique:"), why)

    def test_a_missing_or_oversized_example_is_rejected_before_it_is_parsed(self):
        for svg in ("", None, 42, ["<svg/>"], GRADIENT_ICON.replace("</svg>", "<!--" + "x" * 9000 + "--></svg>")):
            with self.subTest(svg=str(svg)[:20]):
                ok, why = self.sv.admit(good(svg), self.cell("gradients and lighting", "scene"))
                self.assertFalse(ok)
                self.assertTrue(why.startswith("svg:"), why)

    def test_the_small_card_rules_still_hold_and_one_step_runs_the_gates(self):
        ok, why = self.sv.admit(good(title="Gradient"), self.cell())
        self.assertFalse(ok)
        self.assertTrue(why.startswith("title"), why)
        steps = [dict(s, cmd=s["cmd"].replace("sampo works check", "grep -c circle").replace(" --type icon", ""))
                 for s in good()["steps"]]
        ok, why = self.sv.admit(good(steps=steps), self.cell())
        self.assertFalse(ok)
        self.assertTrue(why.startswith("measure:"), why)
        self.assertFalse(self.sv.admit("not a card", self.cell())[0])

    def test_every_admission_branch_has_a_passing_example_and_the_off_by_one_fails(self):
        card = lambda body, w=320, h=120: f'<svg {NS} viewBox="0 0 {w} {h}"><rect width="{w}" height="{h}" fill="#14213d"/>{body}</svg>'
        three = '<circle cx="90" cy="60" r="30" fill="#fca311"/><circle cx="130" cy="60" r="30" fill="#e5e5e5"/>'
        steps = [good()["steps"][0], good()["steps"][1], {"do": "Run the works gates on the card and read each gate.",
                                                          "cmd": "sampo works check card.svg --type card"}]
        icon_href = (f'<svg {NS} xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 24 24"><defs>'
                     '<linearGradient id="a"><stop offset="0" stop-color="#4f8cff"/><stop offset="1" stop-color="#1b4fd1"/>'
                     '</linearGradient><linearGradient id="g" xlink:href="#a"/></defs>'
                     '<circle cx="12" cy="12" r="9" fill="url(#g)"/></svg>')
        cases = (
            ("gradients and lighting", "icon", icon_href, True),
            ("gradients and lighting", "icon", icon_href.replace('<stop offset="1" stop-color="#1b4fd1"/>', ""), False),
            ("gradients and lighting", "card",
             card('<filter id="f"><feDiffuseLighting lighting-color="#fff"><feDistantLight azimuth="45"/>'
                  '</feDiffuseLighting></filter><circle cx="90" cy="60" r="30" fill="#fca311" filter="url(#f)"/>'), True),
            ("gradients and lighting", "card",
             card('<filter id="f"><feOffset dx="1"/></filter><circle cx="90" cy="60" r="30" fill="#fca311" filter="url(#f)"/>'),
             False),
            ("layering and depth", "card",
             card('<filter id="f"><feGaussianBlur stdDeviation="3"/></filter>' + three.replace("<circle", '<circle filter="url(#f)"', 1)),
             True),
            ("layering and depth", "card", card("<style>circle{opacity:.5}</style>" + three), True),
            ("layering and depth", "card", card(three.replace('fill="#fca311"', 'fill="#fca311" style="opacity:.5"')), True),
            ("layering and depth", "card", card(three), False),
            ("motion", "card", card('<style>@keyframes s{from{opacity:0}to{opacity:1}}circle{animation:s 1s infinite}</style>'
                                    + three), True),
            ("motion", "card", card('<style>@keyframes s{from{opacity:0}to{opacity:1}}circle{animation:s 1s}</style>' + three),
             False),
        )
        for technique, kind, svg, expect in cases:
            with self.subTest(technique=technique, svg=svg[:90]):
                st = [dict(s, cmd=s["cmd"].replace("card.svg --type card", f"card.svg --type {kind}")) for s in steps]
                ok, why = self.sv.admit(good(svg, steps=st), self.cell(technique, kind))
                self.assertEqual(ok, expect, why)
                if not expect:
                    self.assertRegex(why, r"^(technique|gate):")

    def test_draft_refuses_a_bad_batch_size_or_writer(self):
        for kw in ({"k": 0}, {"k": 21}, {"writer": "gpt"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.sv.draft(self.p, self.cell(), ask=lambda *a, **k: ("", "m"), **kw)


class Store(_Cards):
    def test_kept_cards_carry_their_cell_and_clean_example_and_repeats_and_rejects_are_not_stored(self):
        cell = self.cell()
        out = self.sv.add(self.p, [good(), good(), good(FLAT_ICON, title="Another flat round icon here")], cell, "gemma", 1)
        self.assertEqual([v for _, v, _ in out], ["kept", "dup", "rejected"])
        self.assertRegex(out[0][0], r"^K-\d{6}$")
        rows = self.sv.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cell"], {"technique": "gradients and lighting", "type": "icon", "grid": "svg"})
        self.assertIn("linearGradient", rows[0]["svg"])
        self.assertIn("safe", rows[0]["gates"])
        self.assertEqual(rows[0]["model"], "gemma")
        self.assertIn("Example SVG:", self.sv.show(rows[0]["id"]))
        # the same title in another cell is its own card; the general grid never counts SVG cards
        spot = GRADIENT_ICON.replace("</svg>", '<circle cx="12" cy="12" r="3" fill="#ffffff"/></svg>')  # two shapes at least
        self.assertEqual(self.sv.add(self.p, [good(spot)], self.cell(kind="spot"), "gemma", 2)[0][1], "kept")
        self.assertEqual(self.sc._grid(), [])
        cov = self.sv.coverage()
        self.assertEqual((cov["total"], cov["filled"], cov["grid"]), (2, 2, len(self.sv.cells())))
        self.assertEqual(cov["cells"]["gradients and lighting/icon"], 1)

    def test_draft_asks_the_local_writer_with_the_cells_prompt_and_stores_what_passes(self):
        seen = []

        def ask(p, system, prompt, writer=None):
            seen.append((prompt, writer))
            return "```json\n" + "\n".join(json.dumps(x) for x in (good(), good(FLAT_ICON, title="A flat icon card"))) \
                + "\n```", "gemma4:12b"
        r = self.sv.draft(self.p, self.cell(), k=2, ask=ask)
        self.assertEqual(seen, [(self.sv.prompt(self.cell(), 2), "local")])
        self.assertEqual((r["kept"], r["rejected"], r["model"]), (1, 1, "gemma4:12b"))
        self.assertTrue(r["reasons"][0].startswith("technique:"), r)
        self.assertEqual(self.sv.rows()[0]["model"], "gemma4:12b")

    def test_draft_picks_the_thinnest_cell_when_none_is_given(self):
        self.sv.add(self.p, [good()], self.cell(), "gemma", 1)
        asked = []
        self.sv.draft(self.p, ask=lambda p, s, prompt, writer=None: (asked.append(prompt), ("", "gemma"))[1])
        self.assertNotIn("technique=gradients and lighting type=icon", asked[0])


if __name__ == "__main__":
    unittest.main()
