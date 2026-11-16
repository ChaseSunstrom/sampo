"""T-3106: sampo works check FILE... renders each SVG headless (light and dark) and checks it by its type's gates:
xml, safe, size, shapes, palette, grid and set (icons), blank, clip, contrast, motion, loop and unique (a perceptual
thumb within the bar of an earlier work's makes the later one a duplicate)."""
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from helpers import ForemanTestCase, cpu_scale, headless_chrome
from sampo import works

NS = 'xmlns="http://www.w3.org/2000/svg"'
ICON = f'<svg {NS} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' \
       '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>'
NEAR = ICON.replace('r="8"', 'r="8.2"')  # one hair wider: the same work
BLUE = ICON.replace('stroke="currentColor"', 'stroke="#2f6fb0"')  # recoloured only: the same work
ROTATED = ICON.replace("l3 2", "l-3 2")  # the hand points another way: another work
STOPWATCH = ICON.replace("</svg>", '<path d="M10 2h4"/></svg>')  # one added element: another work
OTHER = f'<svg {NS} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' \
        '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 12h8M12 8v8"/></svg>'
BLANK = f'<svg {NS} viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" fill="none"/></svg>'
CLIPPED = f'<svg {NS} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' \
          '<circle cx="20" cy="12" r="9"/></svg>'
STILL = f'<svg {NS} viewBox="0 0 24 24"><circle cx="12" cy="12" r="6" fill="currentColor">' \
        '<animate attributeName="r" values="6;6" dur="1s" repeatCount="indefinite"/></circle></svg>'
PULSE = f'<svg {NS} viewBox="0 0 24 24"><circle cx="12" cy="12" r="4" fill="currentColor">' \
        '<animate attributeName="r" values="4;10;4" dur="0.6s" repeatCount="indefinite"/></circle></svg>'
CARD = f'<svg {NS} viewBox="0 0 320 120"><rect width="320" height="120" rx="12" fill="#14213d"/>' \
       '<text x="16" y="72" font-size="32" font-family="sans-serif" fill="#f8f9fa">Clear words</text>' \
       '<circle cx="280" cy="60" r="24" fill="#fca311"/></svg>'
FAINT = CARD.replace('fill="#14213d"', 'fill="#ffffff"').replace('fill="#f8f9fa"', 'fill="#d0d0d0"')
SCRIPTED = f'<svg {NS} viewBox="0 0 24 24" onload="alert(1)"><script>alert(2)</script>' \
           '<image href="https://example.com/x.png" width="24" height="24"/>' \
           '<circle cx="12" cy="12" r="6" fill="currentColor"/></svg>'
OVERSIZED = f'<svg {NS} viewBox="0 0 24 24"><path stroke="currentColor" d="M2 2' + ' L22 22 L2 22' * 250 + '"/></svg>'


def gates(result):
    return {g["gate"]: g for g in result["gates"]}


def failed(result):
    return sorted(g["gate"] for g in result["gates"] if g["ok"] is False)


class TextRatio(unittest.TestCase):
    """T-3247: raster noise between the two renders (a level per channel at a shape's edge) is no text colour."""

    def test_a_one_level_raster_difference_isnt_read_as_faint_text(self):
        class Shot:
            def __init__(self, words):
                self.rgba = b"".join(w.to_bytes(4, sys.byteorder) for w in words)
        white, black, grey = works._word("#ffffff"), works._word("#000000"), works._word("#a9b1c0")
        noise = works._word("#aab1c0")  # the bar's own grey, one level off where the tiles rasterised apart
        with_text = [black] * 40 + [noise] * 30 + [white] * 30
        without = Shot([white] * 40 + [grey] * 30 + [white] * 30)
        self.assertGreater(works._text_ratio(with_text, without), 20)
        faint = [works._word("#f4f4f4")] * 40  # faint text is still read
        self.assertLess(works._text_ratio(faint, Shot([white] * 40)), 1.2)


class StaticGates(unittest.TestCase):
    """What the source alone proves: no browser needed."""

    def test_a_clean_icon_passes_every_static_gate(self):
        got = works.inspect(ICON.encode(), "icon")
        self.assertEqual(failed(got), [])
        self.assertEqual(gates(got)["shapes"]["got"], "2")
        self.assertIn("24", gates(got)["grid"]["got"])

    def test_script_handlers_and_outside_references_fail_safe_and_are_stripped(self):
        got = works.inspect(SCRIPTED.encode(), "icon")
        self.assertIn("safe", failed(got))
        say = gates(got)["safe"]["got"]
        for what in ("script", "onload", "https://example.com"):
            self.assertIn(what, say)
        for bad in ("<script", "onload", "example.com"):
            self.assertNotIn(bad, got["svg"])  # the render never sees them
        self.assertIn("circle", got["svg"])

    def test_every_way_out_is_refused(self):
        for name, svg in {
            "css url": f'<svg {NS} viewBox="0 0 24 24"><style>rect{{fill:url(http://x.test/a.svg#p)}}</style>'
                       '<rect width="9" height="9"/></svg>',
            "import": f'<svg {NS} viewBox="0 0 24 24"><style>@import "http://x.test/a.css";</style>'
                      '<rect width="9" height="9"/></svg>',
            "escape": f'<svg {NS} viewBox="0 0 24 24"><style>rect{{fill:\\75rl(http://x.test/)}}</style>'
                      '<rect width="9" height="9"/></svg>',
            "attr url": f'<svg {NS} viewBox="0 0 24 24"><rect width="9" height="9" fill="url(//x.test/p)"/></svg>',
            "use": f'<svg {NS} viewBox="0 0 24 24"><use href="other.svg#a"/></svg>',
            "xlink": f'<svg {NS} xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 24 24">'
                     '<use xlink:href="data:image/svg+xml,x"/></svg>',
            "set href": f'<svg {NS} viewBox="0 0 24 24"><a href="#a"><set attributeName="href" '
                        'to="javascript:alert(1)"/><rect width="9" height="9"/></a></svg>',
            "foreign": f'<svg {NS} viewBox="0 0 24 24"><foreignObject width="9" height="9">'
                       '<div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject></svg>',
            "stylesheet": '<?xml-stylesheet href="http://x.test/a.css"?>' + f'<svg {NS} viewBox="0 0 24 24">'
                          '<rect width="9" height="9"/></svg>',
        }.items():
            with self.subTest(name):
                self.assertIn("safe", failed(works.inspect(svg.encode(), "icon")))

    def test_entities_are_never_expanded(self):
        bomb = '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>' \
               f'<svg {NS} viewBox="0 0 24 24"><text>&b;</text></svg>'
        got = works.inspect(bomb.encode(), "icon")
        self.assertIn("xml", failed(got))
        self.assertIsNone(got["svg"])  # nothing to render
        plain = '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
        self.assertEqual(failed(works.inspect((plain + ICON).encode(), "icon")), [])  # an export's plain DOCTYPE

    def test_not_an_svg_fails_xml(self):
        for bad in (b"<svg", b"<html/>", f"<svg {NS}/>".encode(), b"\xff\xfe junk",
                    f'<svg {NS} viewBox="0 0 24 0"><rect/></svg>'.encode(),
                    f'<svg {NS} viewBox="0 0 1 400"><rect/></svg>'.encode(),
                    f'<svg {NS} viewBox="0 0 1e-307 1e-307"><rect/></svg>'.encode()):  # its scale would be inf
            with self.subTest(bad):
                got = works.inspect(bad, "icon")
                self.assertIn("xml", failed(got))
                self.assertIsNone(got["svg"])

    def test_oversized_fails_size_with_its_number(self):
        got = works.inspect(OVERSIZED.encode(), "icon")
        self.assertIn("size", failed(got))
        self.assertEqual(gates(got)["size"]["got"], f"{len(OVERSIZED)} bytes")

    def test_too_many_shapes_or_colours_fail_their_gates(self):
        busy = f'<svg {NS} viewBox="0 0 24 24">' + "".join(
            f'<circle cx="{i % 24}" cy="12" r="1" fill="#{i:02x}{i:02x}40"/>' for i in range(30)) + "</svg>"
        got = works.inspect(busy.encode(), "icon")
        self.assertEqual(failed(got), ["palette", "shapes"])
        self.assertEqual(gates(got)["shapes"]["got"], "30")
        self.assertEqual(gates(got)["palette"]["got"], "30 colours")

    def test_an_icon_off_the_grid_or_with_two_strokes_fails_grid(self):
        off = ICON.replace('viewBox="0 0 24 24"', 'viewBox="0 0 23 25"')
        two = ICON.replace('<path ', '<path stroke-width="1.5" ')
        self.assertIn("grid", failed(works.inspect(off.encode(), "icon")))
        self.assertIn("grid", failed(works.inspect(two.encode(), "icon")))
        self.assertNotIn("grid", gates(works.inspect(CARD.encode(), "card")))  # a card has no grid

    def test_the_odd_icon_out_of_a_set_fails_set(self):
        thin = OTHER.replace('stroke-width="2"', 'stroke-width="1.5"')
        got = [works.inspect(s.encode(), "icon") for s in (ICON, OTHER, PULSE, thin)]
        works.set_gate(got)
        self.assertEqual([("set" in failed(r)) for r in got], [False, False, False, True])
        self.assertIn("1.5", gates(got[3])["set"]["got"])

    def test_a_declared_animation_that_plays_once_fails_loop(self):
        once = PULSE.replace(' repeatCount="indefinite"', "")
        self.assertIn("loop", failed(works.inspect(once.encode(), "icon")))
        self.assertNotIn("loop", failed(works.inspect(PULSE.encode(), "icon")))
        css = f'<svg {NS} viewBox="0 0 24 24"><style>@keyframes s{{to{{opacity:0}}}}' \
              'circle{animation:s 1s infinite}</style><circle cx="12" cy="12" r="6"/></svg>'
        self.assertEqual(gates(works.inspect(css.encode(), "icon"))["loop"]["ok"], True)
        commented = css.replace("1s infinite", "1s").replace("<style>", "<style>/* infinite */")
        self.assertIs(gates(works.inspect(commented.encode(), "icon"))["loop"]["ok"], False)  # a comment loops nothing
        self.assertNotIn("loop", gates(works.inspect(ICON.encode(), "icon")))  # nothing declared, no gate

    def test_a_colour_with_an_endless_number_is_no_colour_and_no_crash(self):
        big = "9" * 400  # float() makes it inf: rgb's channel -inf, hsl's hue NaN
        svg = f'<svg {NS} viewBox="0 0 24 24"><rect width="9" height="9" fill="rgb(-{big},0,0)"/>' \
              f'<rect width="9" height="9" fill="hsl({big},50%,50%)"/>' \
              '<rect width="9" height="9" fill="rgb(300,-20,0)"/></svg>'
        got = works.inspect(svg.encode(), "icon")
        self.assertEqual(gates(got)["palette"]["got"], "1 colours")  # rgb(300,-20,0) clamps to #FF0000

    def test_a_use_fan_out_or_cycle_fails_xml(self):
        groups = '<g id="g0"><circle r="1"/></g>' + "".join(
            f'<g id="g{i}"><use href="#g{i - 1}"/><use href="#g{i - 1}"/></g>' for i in range(1, 26))
        fan = f'<svg {NS} viewBox="0 0 24 24"><defs>{groups}</defs><use href="#g25"/></svg>'  # 2^25 instances
        loop = f'<svg {NS} viewBox="0 0 24 24"><g id="a"><use href="#b"/></g><g id="b"><use href="#a"/></g></svg>'
        for name, svg in (("fan", fan), ("cycle", loop)):
            with self.subTest(name):
                self.assertIn("xml", failed(works.inspect(svg.encode(), "icon")))
        reused = f'<svg {NS} viewBox="0 0 24 24"><defs><circle id="d" r="1"/></defs>' + \
            "".join(f'<use href="#d" x="{i}" y="{i}"/>' for i in range(12)) + "</svg>"
        self.assertNotIn("xml", failed(works.inspect(reused.encode(), "icon")))

    def test_paint_marker_and_stylesheet_fan_out_or_a_cycle_fails_xml(self):  # T-3198
        svg = f'<svg {NS} viewBox="0 0 320 120">{{}}</svg>'.format
        cell = '<rect width="1" height="1" {}/>'.format
        pats = '<pattern id="p0" width="1" height="1"><rect width="1" height="1"/></pattern>' + "".join(
            f'<pattern id="p{i}" width="1" height="1">{cell(f"fill=\'url(#p{i - 1})\'") * 2}</pattern>'
            for i in range(1, 26))
        pts = " ".join(f"L{i} {i % 2}" for i in range(1, 20))
        marks = '<marker id="m0"><circle r="1"/></marker>' + "".join(
            f'<marker id="m{i}"><path d="M0 0 {pts}" style="marker-mid: url(#m{i - 1})"/></marker>' for i in range(1, 5))
        sheet = "".join(f".k{i}{{fill:url(#q{i - 1})}}" for i in range(1, 27))
        qs = '<pattern id="q0" width="1" height="1"><rect width="1" height="1"/></pattern>' + "".join(
            f'<pattern id="q{i}" width="1" height="1">{cell(f"class=\'k{i}\'") * 2}</pattern>' for i in range(1, 26))
        bombs = {"pattern": svg(f'<defs>{pats}</defs><rect width="9" height="9" fill="url(#p25)"/>'),  # 2^25 tiles
                 "marker": svg(f'<defs>{marks}</defs><path d="M0 0 {pts}" marker-mid="url(#m4)"/>'),  # 18^5 marks
                 "stylesheet": svg(f'<style>{sheet}</style><defs>{qs}</defs><rect class="k26" width="9" height="9"/>'),
                 "cycle": svg('<defs><pattern id="a" width="1" height="1">' + cell("fill=\"url('#b')\"") +
                              '</pattern><pattern id="b" width="1" height="1">' + cell('fill="url(#a)"') +
                              '</pattern></defs><rect width="9" height="9" fill="url(#a)"/>')}
        for name, bomb in bombs.items():
            with self.subTest(name):
                self.assertIn("xml", failed(works.inspect(bomb.encode(), "card")))
        ok = svg('<style>.hatch{fill:url(#h)} .line{stroke:#333} .shade{fill:url(#g)}</style><defs>'
                 '<pattern id="h" width="4" height="4" patternUnits="userSpaceOnUse"><path class="line" d="M0 4L4 0"/>'
                 '</pattern><linearGradient id="g"><stop offset="0" stop-color="#fff"/><stop offset="1"/>'
                 '</linearGradient><marker id="arrow" markerWidth="4" markerHeight="4"><path d="M0 0L4 2L0 4z"/>'
                 '</marker></defs>' + "".join(f'<rect class="hatch" x="{i}" width="1" height="1"/>' for i in range(12))
                 + "".join(f'<circle class="shade" cx="{i}" cy="2" r="1"/>' for i in range(40))
                 + "".join(f'<line x1="0" y1="{i}" x2="5" y2="{i}" marker-end="url(#arrow)"/>' for i in range(30)))
        self.assertNotIn("xml", failed(works.inspect(ok.encode(), "card")))

    def test_stylesheet_rules_naming_a_url_are_capped_and_the_worst_allowed_stays_fast(self):  # T-3198
        def page(n, body):
            pats = "".join(f'<pattern id="p{i}" width="1" height="1"><rect width="1" height="1"/></pattern>' for i in range(n))
            return f'<svg {NS} viewBox="0 0 320 120"><style>{"".join(f"*{{fill:url(#p{i})}}" for i in range(n))}</style>' \
                   f'<defs>{pats}</defs>{body}</svg>'.encode()
        over = works.inspect(page(works.MAX_RULES + 1, "<g/>"), "scene")
        self.assertEqual([g["got"] for g in over["gates"] if g["gate"] == "xml"],
                         [f"over {works.MAX_RULES} stylesheet rules naming a url(#…)"])
        start = time.process_time()  # T-4758: in-process work, so CPU (a runner took 15.7 s of wall against 10)
        worst = works.inspect(page(works.MAX_RULES, "<g/>" * 19000), "scene")  # every rule on every element
        self.assertLess(time.process_time() - start, 5 * cpu_scale())  # 1.8 s on a quiet box; scaled by the runner's speed
        self.assertIn("xml", failed(worst))

    def test_the_harness_rules_come_after_the_work_and_hidden_text_is_gone(self):
        sneaky = CARD.replace("<rect", "<style>text{visibility:visible!important}</style><rect", 1)
        w = works.inspect(sneaky.encode(), "card")
        tile = works._tile(w, True, True, "#000")
        self.assertNotIn("<text", tile)
        self.assertGreater(tile.rindex(works.STATIC_CSS), tile.index("visibility:visible"))
        self.assertIn("<text", works._tile(w, True, False, "#000"))

    def test_a_hostile_megabyte_is_checked_in_seconds_not_hours(self):
        n = works.MAX_READ - 200  # each was over 30 s before its pattern was bounded (0.9 s at 128 KiB, ×4 per ×2)
        svg = f'<svg {NS} viewBox="0 0 24 24">'
        cases = {"doctype": f'{svg}</svg>' + "<!DOCTYPE " * (n // 10),
                 "rgb(": f'{svg}<rect width="9" height="9" fill="{"rgb(" * (n // 4)}"/></svg>',
                 "url(#": f'{svg}<rect width="9" height="9" fill="{"url(#" * (n // 5)}"/></svg>',
                 "style rgb(": f'{svg}<style>rect{{fill:{"rgb(" * (n // 4)}}}</style><rect/></svg>'}
        for name, text in cases.items():
            with self.subTest(name):
                start = time.monotonic()
                works.inspect(text.encode(), "icon")
                self.assertLess(time.monotonic() - start, 10)

    def test_the_type_is_inferred_from_the_view_box(self):
        self.assertEqual(works.inspect(ICON.encode())["type"], "icon")
        self.assertEqual(works.inspect(CARD.encode())["type"], "card")


class Dedupe(unittest.TestCase):
    """Criterion 2 on synthetic pixels: thumbs within the bar are one work; the later is the duplicate."""

    @staticmethod
    def picture(w, h, f):
        return bytes(v for y in range(h) for x in range(w) for v in (*([f(x, y)] * 3), 255))

    def test_near_renders_are_one_work_and_the_later_is_the_duplicate(self):
        ramp = self.picture(64, 64, lambda x, y: (x * 4) % 256)
        nudged = self.picture(64, 64, lambda x, y: min(255, (x * 4) % 256 + (3 if (x + y) % 7 == 0 else 0)))
        rings = self.picture(64, 64, lambda x, y: 255 if ((x - 32) ** 2 + (y - 32) ** 2) // 60 % 2 else 0)
        faded = self.picture(64, 64, lambda x, y: 128 if ((x - 32) ** 2 + (y - 32) ** 2) // 60 % 2 else 0)
        a, b, c, d = (works.thumb(64, 64, p) for p in (ramp, nudged, rings, faded))
        self.assertLessEqual(works.distance(a, b), works.BAR)
        self.assertGreater(works.distance(a, c), works.BAR)
        self.assertEqual(works.distance(c, d), 0)  # the same picture in another colour
        self.assertEqual(works.duplicates([("a.svg", a), ("b.svg", b), ("c.svg", c)]),
                         {"b.svg": ("a.svg", works.distance(a, b))})
        self.assertEqual(works.duplicates([("b.svg", b), ("a.svg", a)]), {"a.svg": ("b.svg", works.distance(a, b))})
        self.assertEqual(works.duplicates([("c.svg", c)], prior=[("kept.svg", c)]), {"c.svg": ("kept.svg", 0)})


class Contained(unittest.TestCase):
    """One bad work costs only itself: a hang, a crash or a full disk never loses the batch; the browser gets no
    secrets and no network."""

    def test_a_hung_work_fails_render_alone_and_three_in_a_row_stop_the_batch(self):
        from sampo import shot
        started, saved = [], []

        class Fake:
            def __init__(self, *a, **k):
                started.append(1)

            def close(self):
                pass

        def render(b, w, n):
            if w["path"].startswith("hang"):
                raise TimeoutError("no load event in 10 s")
            return {}

        def save(out, w, m, taken):
            if w["path"] == "ok1":
                raise OSError(28, "No space left on device")
            saved.append(w["path"])

        todo = [{"path": p, "svg": "<svg/>", "type": "icon", "animated": False, "gates": []}
                for p in ("ok1", "hang1", "ok2", "hang2", "hang3", "hang4", "ok3")]
        with mock.patch.object(shot, "Browser", Fake), mock.patch.object(works, "_render", render), \
                mock.patch.object(works, "_judge", lambda w, m, kept: None), mock.patch.object(works, "_save", save):
            works._render_all("chrome", todo, "renders")
        got = {w["path"]: w for w in todo}
        self.assertTrue(got["ok1"].get("rendered"))
        self.assertIn("No space left", got["ok1"]["error"])
        self.assertEqual(failed(got["hang1"]), ["render"])
        self.assertIn("no load event", gates(got["hang1"])["render"]["got"])
        self.assertTrue(got["ok2"].get("rendered"))
        self.assertEqual(saved, ["ok2"])
        self.assertEqual(failed(got["hang4"]), ["render"])
        self.assertFalse(got["ok3"].get("rendered"))
        self.assertIn("not tried", got["ok3"]["error"])
        self.assertEqual(len(started), 4)  # the first, then one after hang1, hang2 and hang3; hang4 is the third in a row

    def test_a_file_that_crashes_its_check_fails_xml_alone(self):
        from sampo import cli, shot
        real, seen = works.inspect, {}

        def inspect(data, kind):
            if b"boom" in data:
                raise OverflowError("cannot convert float infinity to integer")
            return real(data, kind)

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name, text in (("boom.svg", ICON.replace("<circle", "<!--boom--><circle")), ("clock.svg", ICON)):
                paths.append(os.path.join(tmp, name))
                with open(paths[-1], "w", encoding="utf-8") as f:
                    f.write(text)
            with mock.patch.object(works, "inspect", inspect), \
                    mock.patch.object(shot, "find_browser", lambda who: (None, "no browser here")), \
                    mock.patch.object(cli, "out", lambda a, data, text: seen.update(data)), \
                    self.assertRaises(SystemExit):
                works.cmd_works(types.SimpleNamespace(files=paths, type=None, out=None))
        got = {os.path.basename(w["path"]): w for w in seen["works"]}
        self.assertEqual(failed(got["boom.svg"]), ["xml"])
        self.assertIn("OverflowError", gates(got["boom.svg"])["xml"]["got"])
        self.assertEqual(failed(got["clock.svg"]), [])

    def test_the_renderer_gets_no_secrets_and_no_network(self):
        seen = {}

        def spawn(path, argv, env, **kw):
            seen.update(argv=argv, env=env)
            raise OSError("stopped before a browser ran")

        with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-not-for-the-browser"}), \
                mock.patch("os.posix_spawn", spawn), self.assertRaises(OSError):
            works._browser("/usr/bin/chromium")
        self.assertNotIn("LLM_API_KEY", seen["env"])
        self.assertEqual(seen["env"].get("PATH"), os.environ.get("PATH"))
        self.assertIn("--host-resolver-rules=MAP * ~NOTFOUND", seen["argv"])


@unittest.skipUnless(headless_chrome(), "no headless Chromium here")
class Rendered(ForemanTestCase):
    """Criterion 1 end to end: sampo works check renders each file and prints each gate's verdict with its number."""

    def put(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def check(self, *names, ok=False):
        paths = [self.put(n, s) for n, s in names]
        r = self.fm("works", "check", *paths, "--json", check=False, timeout=120)
        self.assertEqual(r.returncode, 0 if ok else 1, r.stdout[-3000:] + r.stderr[-3000:])
        return {os.path.basename(x["path"]): x for x in json.loads(r.stdout)["works"]}

    def test_icons_blank_clipped_still_and_duplicate_fail_their_gates(self):
        got = self.check(("clock.svg", ICON), ("blank.svg", BLANK), ("clipped.svg", CLIPPED), ("still.svg", STILL),
                         ("pulse.svg", PULSE), ("plus.svg", OTHER), ("clock2.svg", NEAR))
        for name in ("clock.svg", "pulse.svg", "plus.svg"):
            self.assertEqual(failed(got[name]), [], json.dumps(got[name], indent=1))
        self.assertEqual(failed(got["blank.svg"]), ["blank", "contrast"])
        self.assertEqual(failed(got["clipped.svg"]), ["clip"])
        self.assertEqual(failed(got["still.svg"]), ["motion"])
        self.assertEqual(failed(got["clock2.svg"]), ["unique"])
        self.assertIn("clock.svg", gates(got["clock2.svg"])["unique"]["got"])
        self.assertEqual(gates(got["pulse.svg"])["motion"]["ok"], True)
        self.assertNotIn("motion", gates(got["clock.svg"]))  # nothing declared, no motion gate
        self.assertRegex(gates(got["blank.svg"])["blank"]["got"], r"^0(\.0+)?% painted")
        self.assertRegex(gates(got["clipped.svg"])["clip"]["got"], r"^[1-9]\d* px outside")
        self.assertRegex(gates(got["still.svg"])["motion"]["got"], r"^1 of \d+ frames differ")

    def test_a_small_real_difference_is_an_original_and_a_hair_or_colour_copy_is_not(self):
        got = self.check(("clock.svg", ICON), ("rotated.svg", ROTATED), ("stopwatch.svg", STOPWATCH),
                         ("clock2.svg", NEAR), ("blue.svg", BLUE))
        for name in ("rotated.svg", "stopwatch.svg"):
            self.assertIs(gates(got[name])["unique"]["ok"], True, json.dumps(got[name]["gates"], indent=1))
        for name in ("clock2.svg", "blue.svg"):
            self.assertIs(gates(got[name])["unique"]["ok"], False, json.dumps(got[name]["gates"], indent=1))
            self.assertRegex(gates(got[name])["unique"]["got"], r"^\d+ cells from .*/clock\.svg$")

    def test_cards_low_contrast_and_scripted_fail_their_gates(self):
        got = self.check(("card.svg", CARD), ("faint.svg", FAINT), ("scripted.svg", SCRIPTED))
        self.assertEqual(failed(got["card.svg"]), [], json.dumps(got["card.svg"], indent=1))
        self.assertEqual(failed(got["faint.svg"]), ["contrast"])
        self.assertRegex(gates(got["faint.svg"])["contrast"]["got"], r"^1\.\d+:1")
        self.assertIn("safe", failed(got["scripted.svg"]))
        self.assertEqual(got["card.svg"]["type"], "card")

    def test_a_faint_badge_or_a_faint_line_fails_beside_clear_body_text(self):  # T-3226
        chip = '<rect x="200" y="88" width="64" height="24" rx="6" fill="#ffd166"/>' \
               '<text x="208" y="105" font-size="12" font-family="sans-serif" fill="#fff3c4">NEW</text></svg>'
        note = '<text x="16" y="104" font-size="11" font-family="sans-serif" fill="#3a1f1f">a faint note</text></svg>'
        got = self.check(("badge.svg", CARD.replace("</svg>", chip)), ("note.svg", CARD.replace("</svg>", note)))
        for name in ("badge.svg", "note.svg"):
            with self.subTest(name):
                self.assertEqual(failed(got[name]), ["contrast"], json.dumps(gates(got[name]), indent=1))

    def test_the_text_report_names_each_gate_with_its_number(self):
        good, faint = self.put("card.svg", CARD), self.put("faint.svg", FAINT)
        r = self.fm("works", "check", good, faint, "--out", os.path.join(self.tmp, "renders"), check=False,
                    timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"✓ contrast: \d+\.\d+:1 \(bar 4\.5:1\)")
        self.assertRegex(r.stdout, r"✗ contrast: 1\.\d+:1 \(bar 4\.5:1\)")
        self.assertRegex(r.stdout, r"✓ blank: \d+\.\d+% painted")
        self.assertRegex(r.stdout, r"1 of 2 works passed")
        shots = sorted(os.listdir(os.path.join(self.tmp, "renders")))
        self.assertEqual(shots, ["card-dark.png", "card-light.png", "faint-dark.png", "faint-light.png"])

    def test_a_document_figure_renders_near_its_size_and_is_told_apart_by_its_text(self):  # T-3247
        def doc(word):
            rows = "".join(f'<text x="20" y="{40 + 28 * i}" font-size="14" font-family="sans-serif" fill="#2F62E0">'
                           f'{word} row {i} of a long README table</text>' for i in range(48))
            return f'<svg {NS} viewBox="0 0 880 1400"><rect width="880" height="1400" rx="12" fill="#ffffff"/>' \
                   f'{rows}</svg>'
        got = self.check(("a.svg", doc("alpha")), ("b.svg", doc("beta")), ok=False)
        self.assertIn("contrast", failed(got["b.svg"]))  # as a card: drawn at 640 px, its 14 px text blurs to 6 px
        paths = [self.put(n, doc(w)) for n, w in (("a.svg", "alpha"), ("b.svg", "beta"))]
        r = self.fm("works", "check", "--type", "figure", *paths, "--json", check=False, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        for w in json.loads(r.stdout)["works"]:
            self.assertEqual(w["type"], "figure")
            self.assertIsNone(gates(w)["unique"]["ok"], json.dumps(w["gates"], indent=1))

    def test_a_good_animated_icon_passes_whole(self):
        self.check(("pulse.svg", PULSE), ok=True)


class WithoutBrowser(ForemanTestCase):
    def test_no_browser_proves_nothing_rendered_and_says_why(self):
        path = os.path.join(self.tmp, "clock.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(ICON)
        r = self.fm("works", "check", path, check=False, env={"FOREMAN_CHROME": "none"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no browser", r.stdout)
        self.assertRegex(r.stdout, r"- blank: not rendered")
        self.assertRegex(r.stdout, r"✓ safe: ")


if __name__ == "__main__":
    unittest.main()
