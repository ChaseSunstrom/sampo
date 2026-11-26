"""T-4995 (a slice of T-3311): two art cards in the panel-* format, art-abstract and art-landscape. Each takes at most 8
typed slots, its gen.py draws one picture as a dark and a light SVG from the design tokens, and its check.py renders
both through works: green only when rendered, never when no browser (or no works) can render it."""
import json
import os
import re
import subprocess
import tempfile
import unittest

from helpers import SampoTestCase, headless_chrome
from test_callcards import confined

from sampo import callcards, panels, works

ART = ("art-abstract", "art-landscape")
kit = panels.kit()
DURATION = re.compile(r"none|\d+s")
# T-5484: what a run reads when its only whole-tree read is a browser's (its launch and the page it sent): both card
# folders (check.py, gen.py, tests.json), the artkit they draw from, and the works and shot code that render the pair
RUN_READS = ["plugin/cards/art-abstract/", "plugin/cards/art-landscape/", "plugin/cards/_lib/artkit.py",
             "plugin/lib/sampo/works.py", "plugin/lib/sampo/shot.py"]
# every enum value of each card's own slots, and every style and motion, at least once
SAMPLES = {
    "art-abstract": [dict(composition=c, density=d, accent=a, seed=s, motion=m, style=st) for c, d, a, s, m, st in (
        ("orbits", 12, 0, 7, "24s", "board"), ("drift", 6, 2, 41, "48s", "blueprint"), ("grid", 24, 4, 1234, "12s",
                                                                                          "paper"),
        ("burst", 3, 5, 9999, "none", "board"), ("drift", 24, 1, 5, "12s", "board"), ("burst", 24, 2, 6, "12s", "paper"), ("grid", 3, 1, 0, "none", "blueprint"), ("orbits", 24, 3, 77, "12s",
                                                                                        "paper"))],
    "art-landscape": [dict(time=t, terrain=r, layers=n, seed=s, motion=m, style=st) for t, r, n, s, m, st in (
        ("dawn", "hills", 3, 5, "24s", "board"), ("day", "mountains", 6, 812, "48s", "blueprint"),
        ("dusk", "dunes", 2, 31, "12s", "paper"), ("night", "sea", 4, 9999, "24s", "board"), ("night", "sea", 6, 3, "12s", "blueprint"),
        ("night", "mountains", 5, 0, "none", "paper"), ("day", "sea", 2, 64, "none", "blueprint"))],
}


def found():
    cards, broken = callcards.cards(panels.ROOT)
    return cards, broken


def run(card, script, payload, cwd, env=None):
    return subprocess.run(callcards._argv(card, script), cwd=cwd, input=json.dumps(payload), capture_output=True,
                          text=True, timeout=300, env=env)


def draw(card, inputs, cwd):
    """(clean inputs, {scheme: svg text}) from the card's own gen.py, run as callcards runs it (unconfined here)."""
    clean, errors = callcards.check_inputs(card, inputs, cwd)
    assert not errors, errors
    r = run(card, "gen.py", {"inputs": clean, "files": []}, cwd)
    assert r.returncode == 0, r.stderr[-2000:]
    out = {}
    for scheme in ("dark", "light"):
        with open(os.path.join(cwd, f"{clean['out']}-{scheme}.svg"), encoding="utf-8") as f:
            out[scheme] = f.read()
    return clean, out


class Schema(unittest.TestCase):
    def test_both_cards_load_in_the_panel_format_with_at_most_eight_typed_slots(self):
        cards, broken = found()
        for name in ART:
            self.assertNotIn(name, broken, broken.get(name))
            card = cards[name]
            self.assertEqual((card["kind"], card["deterministic"]), ("py", True), name)
            self.assertEqual(card["creates"], ["$out-dark.svg", "$out-light.svg"], name)
            schema = card["inputs"]
            self.assertIs(schema["additionalProperties"], False, name)
            self.assertEqual(sorted(schema["required"]), ["out", "title"], name)
            props = schema["properties"]
            self.assertLessEqual(len(props), 8, name)
            self.assertEqual(props["out"]["format"], "newpath", name)
            for slot, s in props.items():
                where = f"{name}.{slot}"
                self.assertIn(s.get("type"), ("string", "integer"), where)
                bounded = "enum" in s or s.get("format") == "newpath" or "maxLength" in s or (
                    "minimum" in s and "maximum" in s)
                self.assertTrue(bounded, f"{where} is unbounded")
                if "default" in s:
                    self.assertEqual(callcards.check_inputs(card, {"out": "x", "title": "t", slot: s["default"]},
                                                            panels.ROOT)[1], [], where)
            self.assertEqual(props["style"]["enum"], list(kit.STYLES_NAMES), name)
            self.assertTrue(all(DURATION.fullmatch(v) for v in props["motion"]["enum"]), props["motion"]["enum"])

    def test_the_samples_cover_every_value_of_each_cards_own_enums(self):
        cards, _ = found()
        for name in ART:
            for slot, s in cards[name]["inputs"]["properties"].items():
                if "enum" in s:
                    self.assertEqual({x[slot] for x in SAMPLES[name]} | {s.get("default")} >= set(s["enum"]), True,
                                     f"{name}.{slot}")


class Draw(unittest.TestCase):
    def setUp(self):
        self.cards = found()[0]
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_every_sample_draws_a_dark_and_a_light_svg_that_pass_the_source_gates(self):
        for name in ART:
            for i, sample in enumerate(SAMPLES[name]):
                clean, pair = draw(self.cards[name], dict(sample, out=f"{name}-{i}", title=f"{name} {i}"), self.tmp)
                for scheme, text in pair.items():
                    where = f"{name} {sample} {scheme}"
                    gates = works.inspect(text.encode(), "card")["gates"]
                    self.assertEqual([g for g in gates if g["ok"] is False], [], where)
                    self.assertEqual({g["gate"] for g in gates} >= {"xml", "safe", "size", "shapes", "palette"}, True)
                    if clean["motion"] == "none":
                        self.assertNotIn("animation", text, where)
                    else:
                        self.assertIn(f"animation:", text, where)
                        self.assertIn(clean["motion"], text, where)
                        self.assertIn("loop", {g["gate"] for g in gates}, where)
                    self.assertRegex(text, r'<svg[^>]* role="img"', where)
                    self.assertIn("<title", text)
                    self.assertIn("<desc", text)
                    self.assertNotRegex(text, r"https?://(?!www\.w3\.org)|<image|<script|@import|<text", where)
                    # the palette is the design tokens': the ground is the style's own background
                    self.assertIn(f'fill="{kit.theme(clean["style"], scheme).bg}"', text, where)
                self.assertNotEqual(pair["dark"], pair["light"], name)

    def test_the_same_slots_draw_the_same_bytes_and_another_seed_draws_another_picture(self):
        for name in ART:
            sample = SAMPLES[name][0]
            _, a = draw(self.cards[name], dict(sample, out="a", title="t"), self.tmp)
            _, b = draw(self.cards[name], dict(sample, out="b", title="t"), self.tmp)
            _, c = draw(self.cards[name], dict(sample, out="c", title="t", seed=sample["seed"] + 1), self.tmp)
            self.assertEqual(a, b, name)
            self.assertNotEqual(a["dark"], c["dark"], name)

    def test_a_bad_slot_is_refused_by_the_schema_and_by_the_generator(self):
        for name, bad, field in (("art-abstract", {"density": 99}, "density"), ("art-abstract", {"motion": "1s"}, "motion"),
                                 ("art-landscape", {"terrain": "lava"}, "terrain"), ("art-landscape", {"layers": 1},
                                                                                    "layers")):
            card = self.cards[name]
            errors = callcards.check_inputs(card, dict(bad, out="x", title="t"), self.tmp)[1]
            self.assertTrue(errors and field in errors[0], (name, errors))
            r = run(card, "gen.py", {"inputs": dict(bad, out="x", title="t"), "files": []}, self.tmp)
            self.assertEqual(r.returncode, 1, (name, bad))
            self.assertIn(field, r.stderr, name)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "x-dark.svg")), name)


class Check(unittest.TestCase):
    """check.py: the source gates first, then works' render of both files; unrendered is never green."""

    def setUp(self):
        self.cards = found()[0]
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def check(self, name, clean, **env):
        return run(self.cards[name], "check.py", {"inputs": clean, "files": [], "changed": [], "created": [
            f"{clean['out']}-dark.svg", f"{clean['out']}-light.svg"], "orig": self.tmp, "facts": None}, self.tmp,
            dict(os.environ, **env))

    def render_sample(self, name, i, sample):
        clean, _ = draw(self.cards[name], dict(sample, out=f"p{i}", title="t"), self.tmp)
        r = self.check(name, clean, FOREMAN_CHROME=headless_chrome())
        self.assertEqual(r.returncode, 0, f"{name} {sample}: {r.stdout[-2000:]} {r.stderr[-2000:]}")
        for scheme in ("dark", "light"):
            self.assertRegex(r.stdout, rf"p{i}-{scheme}\.svg: rendered, passed", name)

    def test_no_browser_is_reported_unrendered_and_never_green(self):
        for name in ART:
            clean, _ = draw(self.cards[name], {"out": "n", "title": "t"}, self.tmp)
            r = self.check(name, clean, FOREMAN_CHROME="none")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertRegex(r.stderr, r"^environment: missing ", name)
            self.assertIn("not rendered", r.stderr, name)

    def test_a_missing_or_unsafe_file_fails_before_any_render(self):
        name = "art-abstract"
        clean, _ = draw(self.cards[name], {"out": "m", "title": "t"}, self.tmp)
        os.remove(os.path.join(self.tmp, "m-light.svg"))
        r = self.check(name, clean, FOREMAN_CHROME="none")
        self.assertEqual(r.returncode, 1)
        self.assertIn("m-light.svg wasn't written", r.stderr)
        self.assertNotIn("environment:", r.stderr)
        with open(os.path.join(self.tmp, "m-light.svg"), "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 400" role="img"><title>t</title><desc>d'
                    '</desc><rect width="640" height="400" fill="#000"/><script>alert(1)</script></svg>')
        r = self.check(name, clean, FOREMAN_CHROME="none")
        self.assertEqual(r.returncode, 1)
        self.assertIn("m-light.svg", r.stderr)
        self.assertIn("safe", r.stderr)
        self.assertNotIn("environment:", r.stderr)


def _render_test(name, i, sample):
    def test(self):
        # no skip: a run without a browser must fail, never pass with zero real renders
        if not headless_chrome():
            self.fail(f"{name}: no headless Chromium, so the card's real render cannot run")
        self.render_sample(name, i, sample)
    return test


# T-6566: one real Chrome render per card (its defaults), not one per sample: 17 renders took 63 s wall under the runner
# on a loaded box. Every sample still draws and passes the source gates in Draw, with no browser.
for _name in ART:
    setattr(Check, f"test_render_{_name.replace('-', '_')}_default", _render_test(_name, 0, {}))


@confined
class Confined(SampoTestCase):
    def test_in_the_card_jail_the_pair_is_drawn_but_never_applied_unrendered(self):
        for name in ART:
            r = callcards.call(name, {"out": f"art/{name}", "title": "t"}, self.repo, cache=False)
            self.assertEqual(r["status"], "environment", r)
            self.assertIn("not rendered", r["error"])
            self.assertIn("cannot deliver output inside the card jail", r["error"])
            self.assertNotIn("render the pair outside", r["error"])
            self.assertNotIn("install it", r["error"])
            self.assertFalse(os.path.exists(os.path.join(self.repo, "art", f"{name}-dark.svg")))
            r = callcards.call(name, {"out": "x", "title": "t", "seed": -1}, self.repo)
            self.assertEqual(r["status"], "bad_input", r)
            self.assertIn("seed", r["error"])

    def test_each_cards_own_test_cases_pass(self):
        cards = found()[0]
        for name in ART:
            res = callcards.run_tests(cards[name])
            self.assertTrue(res, f"{name} has test cases")
            self.assertEqual([(n, why) for n, why in res if why], [], name)


if __name__ == "__main__":
    unittest.main()
