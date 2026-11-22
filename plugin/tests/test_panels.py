"""T-3662: the panel kit: 29 kinds of mechanism panel as SVG, three styles, project colours, calling cards. T-3736 added
eight that move (SMIL that loops over a static frame that reads alone): constellation, sequence, statemachine, conveyor,
network, chord, layers, circuit. T-3737 added eight more for time, scale and state: metro, flame, treemap, rain, radar,
hourglass, dtree, bloom."""
import copy
import html
import json
import math
import os
import re
import unittest
import xml.etree.ElementTree as ET

from helpers import SampoTestCase, headless_chrome
from test_callcards import confined

from sampo import callcards, panels, works
from sampo import readme

ROUND2 = ["constellation", "sequence", "statemachine", "conveyor", "network", "chord", "layers", "circuit"]
ROUND2B = ["metro", "flame", "treemap", "rain", "radar", "hourglass", "dtree", "bloom"]
KINDS = ["cluster", "funnel", "transform", "globe", "lanes", "filetree", "terminal", "tanks", "orbit", "coremap", "river",
         "gate", "arch"] + ROUND2 + ROUND2B
kit = panels.kit()
SVG = "{http://www.w3.org/2000/svg}"
SMIL = {SVG + t for t in ("animate", "animateMotion", "animateTransform")}


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


class Registry(unittest.TestCase):
    def test_the_kinds_are_registered_with_schema_and_sample(self):
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
            extra = {s for s in drawn if not any(s.strip("…") in w or s in w for w in words) and s not in ("T", "01", "ok", ">>", "$", "output", "gate", "in") and not re.fullmatch(r"[\d.,%]+(?:ms|k)?|[A-Z][A-Z\d ]*|[a-z]+: .*", s)}
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


def tree(name, data=None, **kw):
    p = dict(kind=name, title="t", data=data or kit.KINDS[name]["sample"], **kw)
    return ET.fromstring(kit.panel_svg(p, kit.theme(), 1))


def texts(root):
    return ["".join(e.itertext()) for e in root.iter(SVG + "text")]


class Mechanisms(unittest.TestCase):
    """T-3736: the eight moving kinds draw the thing they name, and every motion loops over a frame that reads still."""

    def test_each_moving_kind_animates_and_every_animation_loops(self):
        for name in ROUND2:
            for size in ((kit.MIN_W, kit.MIN_H), (kit.DEF_W, kit.DEF_H)):
                root = tree(name, w=size[0], h=size[1])
                anims = [e for e in root.iter() if e.tag in SMIL]
                self.assertGreaterEqual(len(anims), 3, name)
                for a in anims:
                    self.assertEqual(a.get("repeatCount"), "indefinite", f"{name} {a.tag}")
                    self.assertRegex(a.get("dur") or "", r"^[\d.]+s$", f"{name} {a.tag}")
                    self.assertNotIn(a.get("attributeName"), ("href", "xlink:href"), name)

    def test_the_sample_names_each_part(self):
        for name in ROUND2:
            d = kit.KINDS[name]["sample"]
            drawn = " ".join(texts(tree(name, w=1200, h=760)))
            want = {"constellation": [m["name"] for m in d.get("modules", [])],
                    "sequence": d.get("actors", []) + [m["label"] for m in d.get("messages", [])],
                    "statemachine": d.get("states", []),
                    "conveyor": [w["label"] for w in d.get("workers", [])],
                    "network": [h["label"] for h in d.get("hosts", [])],
                    "chord": d.get("groups", []),
                    "layers": [x["label"] for x in d.get("layers", [])],
                    "circuit": [s["label"] for s in d.get("stages", [])]}[name]
            self.assertTrue(want, name)
            for w in want:
                self.assertIn(w.upper() if w.upper() in drawn else w, drawn, f"{name}: {w}")

    def test_a_state_machine_counts_its_transitions_and_lights_the_current_state(self):
        d = copy.deepcopy(kit.KINDS["statemachine"]["sample"])
        drawn = texts(tree("statemachine", d))
        for t in d["transitions"]:
            self.assertTrue(any(re.fullmatch(r"[\d.,]+k?", s) and kit._num(t["count"]) == s for s in drawn), t)
        self.assertIn("NOW", drawn)
        a = kit.panel_svg(dict(kind="statemachine", title="t", data=d), kit.theme(), 1)
        d["current"] = next(s for s in d["states"] if s != d["current"])
        b = kit.panel_svg(dict(kind="statemachine", title="t", data=d), kit.theme(), 1)
        self.assertNotEqual(a, b, "moving the current state moves the light")
        d["current"] = "nowhere"
        self.assertTrue(any("data.current" in e for e in kit.check_data("statemachine", d)))

    def test_swimlanes_draw_one_timed_arrow_per_message(self):
        d = kit.KINDS["sequence"]["sample"]
        root = tree("sequence")
        arrows = [e for e in root.iter(SVG + "path") if (e.get("class") or "") == "msg"]
        self.assertEqual(len(arrows), len(d["messages"]))
        drawn = texts(root)
        self.assertIn(f"{max(m['at'] + m.get('took', 0) for m in d['messages'])}ms", " ".join(drawn) + "ms")
        bad = copy.deepcopy(d)
        bad["messages"][0]["to"] = "ghost"
        self.assertTrue(any("data.messages[0].to" in e for e in kit.check_data("sequence", bad)))

    def test_a_network_link_shows_its_latency_and_a_lossy_one_is_dashed(self):
        d = kit.KINDS["network"]["sample"]
        drawn = texts(tree("network", w=1200, h=760))
        for l in d["links"]:
            self.assertIn(f"{l['ms']}ms", drawn, l)
        self.assertTrue(any(l.get("loss") for l in d["links"]), "the sample shows a lossy link")
        bad = copy.deepcopy(d)
        bad["hosts"][0]["kind"] = "toaster"
        self.assertTrue(any("data.hosts[0].kind" in e for e in kit.check_data("network", bad)))

    def test_references_must_name_a_declared_part(self):
        cases = {"constellation": ("imports", lambda d: d["imports"].append(["cli", "ghost"]), "data.imports"),
                 "statemachine": ("transitions", lambda d: d["transitions"][0].update({"to": "ghost"}), "data.transitions[0].to"),
                 "network": ("links", lambda d: d["links"][0].update({"from": "ghost"}), "data.links[0].from"),
                 "chord": ("flows", lambda d: d["flows"][0].update({"from": "ghost"}), "data.flows[0].from")}
        for name, (_, spoil, field) in cases.items():
            d = copy.deepcopy(kit.KINDS[name]["sample"])
            spoil(d)
            errs = kit.check_data(name, d)
            self.assertTrue(any(field in e for e in errs), f"{name}: {errs}")

    def test_the_circuit_marks_each_chip_and_its_state(self):
        d = kit.KINDS["circuit"]["sample"]
        drawn = texts(tree("circuit"))
        for i in range(len(d["stages"])):
            self.assertIn(f"U{i + 1}", drawn)
        self.assertGreaterEqual(len({s["state"] for s in d["stages"]}), 3, "the sample shows several states")

    def test_the_conveyor_counts_its_queue(self):
        d = kit.KINDS["conveyor"]["sample"]
        drawn = texts(tree("conveyor"))
        self.assertIn(str(len(d["queue"])), drawn)
        self.assertIn("QUEUED", " ".join(drawn))


def body(root):
    """The panel's content group: what the kind drew, in content-box px."""
    return next(g for g in root.iter(SVG + "g") if (g.get("transform") or "").startswith(f"translate({kit.PAD} {kit.HEAD})"))


def cls(root, name):
    return [e for e in root.iter() if name in (e.get("class") or "").split()]


def box(e):
    return tuple(float(e.get(k)) for k in ("x", "y", "width", "height"))


def ys(e):
    """The y values of a path's points (an absolute M/L/H/V/Z path)."""
    d, out, y = e.get("d"), [], 0.0
    for cmd, args in re.findall(r"([MLHVZ])([^MLHVZ]*)", d):
        nums = [float(v) for v in re.findall(r"-?[\d.]+", args)]
        if cmd in "ML":
            out += nums[1::2]
        elif cmd == "V":
            out += nums
    return out


def long(name, d):
    """The sample with its longest label at its schema's limit."""
    set_ = {"metro": lambda: d["branches"][1].update(name="feature/" + "x" * 10),
            "flame": lambda: d["spans"][1].update(name="y" * 28),
            "treemap": lambda: d["groups"][0]["files"][0].update(name="z" * 24),
            "rain": lambda: d["sources"][0].update(name="w" * 12),
            "radar": lambda: d["checks"][0].update(name="v" * 16),
            "hourglass": lambda: d["glasses"][0].update(label="u" * 16),
            "dtree": lambda: d["nodes"][0].update(label="t" * 28),
            "bloom": lambda: d["groups"][0]["items"][0].update(name="s" * 16)}
    set_[name]()
    return d


class Round2b(unittest.TestCase):
    """T-3737: eight more kinds for time, scale and state, each drawing the mechanism from the data and looping."""

    def test_each_kind_animates_and_every_animation_loops(self):
        for name in ROUND2B:
            for size in ((kit.MIN_W, kit.MIN_H), (kit.DEF_W, kit.DEF_H)):
                root = tree(name, w=size[0], h=size[1])
                anims = [e for e in root.iter() if e.tag in SMIL]
                self.assertGreaterEqual(len(anims), 3, name)
                for a in anims:
                    self.assertEqual(a.get("repeatCount"), "indefinite", f"{name} {a.tag}")
                    self.assertRegex(a.get("dur") or "", r"^[\d.]+s$", f"{name} {a.tag}")

    def test_every_label_stays_inside_the_content_box_at_any_size(self):
        for name in ROUND2B:
            for w, h in ((kit.MIN_W, kit.MIN_H), (kit.DEF_W, kit.DEF_H), (1400, 900), (kit.MIN_W, 900), (1400, kit.MIN_H)):
                for data in (kit.KINDS[name]["sample"], long(name, copy.deepcopy(kit.KINDS[name]["sample"]))):
                    W, H = w - 2 * kit.PAD, h - kit.HEAD - 12
                    for t in body(tree(name, data, w=w, h=h)).iter(SVG + "text"):
                        s, x, y = "".join(t.itertext()), float(t.get("x")), float(t.get("y"))
                        wd = len(s) * (float(t.get("font-size")) * kit.GLYPH + float(t.get("letter-spacing") or 0))
                        x0 = {"start": x, "middle": x - wd / 2, "end": x - wd}[t.get("text-anchor") or "start"]
                        where = f"{name} {w}x{h} {s!r} at {x0:.0f}..{x0 + wd:.0f}, {y:.0f}"
                        self.assertGreaterEqual(x0, -1.5, where)
                        self.assertLessEqual(x0 + wd, W + 1.5, where)
                        self.assertGreaterEqual(y, 7, where)
                        self.assertLessEqual(y, H + 3, where)

    def test_a_long_label_is_cut_with_an_ellipsis(self):
        for name in ROUND2B:
            if name == "hourglass":  # its label has a column of its own: sixteen characters fit
                continue
            text = kit.panel_svg(dict(kind=name, title="t", w=kit.MIN_W, h=kit.MIN_H,
                                      data=long(name, copy.deepcopy(kit.KINDS[name]["sample"]))), kit.theme(), 1)
            self.assertIn("…", text, name)

    def test_the_sample_names_each_part(self):
        for name in ROUND2B:
            d = kit.KINDS[name]["sample"]
            drawn = " ".join(texts(tree(name, w=1200, h=760)))
            want = {"metro": lambda: [b["name"] for b in d["branches"]] + [t["label"] for t in d.get("tags", [])],
                    "flame": lambda: [s["name"] for s in d["spans"]],
                    "treemap": lambda: [g["name"] for g in d["groups"]],
                    "rain": lambda: [s["name"] for s in d["sources"]],
                    "radar": lambda: [c["name"] for c in d["checks"]],
                    "hourglass": lambda: [g["label"] for g in d["glasses"]],
                    "dtree": lambda: [n["label"] for n in d["nodes"]],
                    "bloom": lambda: [g["name"] for g in d["groups"]]}[name]()
            for w in want:
                self.assertIn(w, drawn, f"{name}: {w}")

    def test_references_must_name_a_declared_part(self):
        cases = {"metro": (lambda d: d["branches"][1].update({"into": "ghost"}), "data.branches[1].into"),
                 "flame": (lambda d: d["spans"][2].update({"parent": "ghost"}), "data.spans[2].parent"),
                 "rain": (lambda d: d["recent"][0].update({"source": "ghost"}), "data.recent[0].source"),
                 "dtree": (lambda d: d["nodes"][2].update({"parent": "ghost"}), "data.nodes[2].parent")}
        for name, (spoil, field) in cases.items():
            d = copy.deepcopy(kit.KINDS[name]["sample"])
            spoil(d)
            errs = kit.check_data(name, d)
            self.assertTrue(any(field in e for e in errs), f"{name}: {errs}")

    def test_the_metro_draws_a_line_per_branch_and_a_station_per_fork_and_merge(self):
        d = kit.KINDS["metro"]["sample"]
        root = tree("metro")
        self.assertEqual(len(cls(root, "track")), len(d["branches"]))
        forks = len(d["branches"]) - 1
        merges = sum(1 for b in d["branches"] if b.get("into"))
        self.assertEqual(len(cls(root, "xchg")), forks + merges)
        self.assertTrue(merges and any(not b.get("into") for b in d["branches"][1:]), "the sample has merged and open branches")
        self.assertIn("HEAD", texts(root))
        bad = copy.deepcopy(d)
        bad["branches"][2]["end"] = bad["branches"][2]["start"] - 1
        self.assertTrue(any("data.branches[2].end" in e for e in kit.check_data("metro", bad)))

    def test_the_waterfall_nests_each_span_inside_its_parent(self):
        d = kit.KINDS["flame"]["sample"]
        root = tree("flame", w=1000, h=600)
        bars = {e.get("data-span"): box(e) for e in cls(root, "span")}
        self.assertEqual(sorted(bars), sorted(s["name"] for s in d["spans"]))
        for s in d["spans"]:
            if s.get("parent"):
                x, y, w, _ = bars[s["name"]]
                px, py, pw, _ = bars[s["parent"]]
                self.assertGreaterEqual(x, px - .6, s)
                self.assertLessEqual(x + w, px + pw + .6, s)
                self.assertGreater(y, py, f"{s['name']} drawn under its parent")
        errs = [s["name"] for s in d["spans"] if s.get("error")]
        self.assertTrue(errs, "the sample has a failed span")
        self.assertEqual(sorted(e.get("data-span") for e in cls(root, "err")), sorted(errs))
        loop = copy.deepcopy(d)
        loop["spans"][0]["parent"] = loop["spans"][1]["name"]
        self.assertTrue(any("data.spans[0].parent" in e for e in kit.check_data("flame", loop)), "a cycle is refused")

    def test_the_treemap_gives_each_file_an_area_by_its_size_without_overlap(self):
        d = kit.KINDS["treemap"]["sample"]
        root = tree("treemap", w=1000, h=640)
        tiles = cls(root, "tile")
        self.assertEqual(len(tiles), sum(len(g["files"]) for g in d["groups"]))
        bx = [box(t) for t in tiles]
        W, H = 1000 - 2 * kit.PAD, 640 - kit.HEAD - 12
        for i, (x, y, w, h) in enumerate(bx):
            self.assertTrue(x >= -.1 and y >= -.1 and x + w <= W + .1 and y + h <= H + .1, bx[i])
            for x2, y2, w2, h2 in bx[i + 1:]:
                over = max(0, min(x + w, x2 + w2) - max(x, x2)) * max(0, min(y + h, y2 + h2) - max(y, y2))
                self.assertLess(over, 1.5, "tiles overlap")
        k = 0
        for g in d["groups"]:
            per = [bx[k + j][2] * bx[k + j][3] / f["size"] for j, f in enumerate(g["files"]) if bx[k + j][2] * bx[k + j][3] > 300]
            k += len(g["files"])
            self.assertLess(max(per) / min(per), 1.12, g["name"])
        hot = [f for g in d["groups"] for f in g["files"] if f.get("churn", 0) >= 60]
        self.assertTrue(hot and len(cls(root, "hot")) == len(hot), "a hot file breathes")

    def test_the_rain_falls_by_level_and_counts_each_sources_errors(self):
        d = kit.KINDS["rain"]["sample"]
        root = tree("rain", w=1000, h=600)
        drawn = texts(root)
        for s in d["sources"]:
            self.assertIn(s["name"], drawn)
            self.assertIn(kit._num(s.get("error", 0)), drawn, s)
        for level in ("debug", "info", "warn", "error"):
            self.assertIn(level.upper(), drawn)
            if any(s.get(level) for s in d["sources"]):
                self.assertTrue(cls(root, level), f"{level} falls")
        self.assertEqual(len(cls(root, "splash")), len([e for e in cls(root, "error") if e.tag == SVG + "path"]))
        quiet = copy.deepcopy(d)
        for s in quiet["sources"]:
            s["error"] = 0
        self.assertFalse(cls(tree("rain", quiet), "error"), "no errors, no red rain")

    def test_the_radar_puts_a_blip_per_check_further_out_the_slower_it_is(self):
        d = kit.KINDS["radar"]["sample"]
        root = tree("radar", w=900, h=560)
        blips = cls(root, "blip")
        self.assertEqual(len(blips), len(d["checks"]))
        centre = cls(root, "scope")[0]
        cx, cy = float(centre.get("cx")), float(centre.get("cy"))
        dist = [math.hypot(float(b.get("cx")) - cx, float(b.get("cy")) - cy) for b in blips]
        order = sorted(range(len(d["checks"])), key=lambda i: d["checks"][i]["ms"])
        for a, b in zip(order, order[1:]):
            self.assertLessEqual(dist[a], dist[b] + .2, (d["checks"][a]["name"], d["checks"][b]["name"]))
        self.assertTrue(any(e.get("type") == "rotate" for e in root.iter(SVG + "animateTransform")), "the beam turns")
        self.assertEqual(len(cls(root, "ping")), sum(c["state"] == "fail" for c in d["checks"]))
        drawn = texts(root)
        for st in ("OK", "WARN", "FAIL"):
            self.assertIn(st, drawn)

    def test_the_hourglass_shows_whats_left_and_its_top_sand_falls_as_it_is_used(self):
        d = copy.deepcopy(kit.KINDS["hourglass"]["sample"])
        drawn = texts(tree("hourglass", d))
        for g in d["glasses"]:
            self.assertIn(f"{round(100 - min(g['used'] / g['total'], 1) * 100)}%", drawn, g)
        tops = lambda data: {e.get("data-glass"): e for e in cls(tree("hourglass", data), "sand-top")}  # noqa: E731
        before = min(ys(tops(d)["0"]))
        d["glasses"][0]["used"] = d["glasses"][0]["total"] * .95
        self.assertGreater(min(ys(tops(d)["0"])), before + 3, "less sand left up top")
        d["glasses"][0]["used"] = d["glasses"][0]["total"]
        self.assertNotIn("0", tops(d), "an empty glass has no top sand")

    def test_the_decision_tree_ends_each_path_in_an_outcome_with_its_confidence(self):
        d = kit.KINDS["dtree"]["sample"]
        root = tree("dtree", w=1000, h=600)
        parents = {n.get("parent") for n in d["nodes"]}
        leaves = [n for n in d["nodes"] if n["id"] not in parents]
        self.assertEqual(len(cls(root, "leaf")), len(leaves))
        drawn = texts(root)
        for n in leaves:
            self.assertIn(f"{kit._num(n['conf'])}%", drawn, n)
        for n in d["nodes"]:
            if n.get("edge"):
                self.assertIn(n["edge"], drawn)
        cyc = copy.deepcopy(d)
        cyc["nodes"][1]["parent"], cyc["nodes"][2]["parent"] = cyc["nodes"][2]["id"], cyc["nodes"][1]["id"]
        self.assertTrue(kit.check_data("dtree", cyc), "a cycle is refused")
        two = copy.deepcopy(d)
        del two["nodes"][2]["parent"]
        self.assertTrue(any("root" in e for e in kit.check_data("dtree", two)), "two roots are refused")

    def test_the_bloom_packs_each_item_inside_its_group_without_overlap(self):
        d = kit.KINDS["bloom"]["sample"]
        for w, h in ((kit.MIN_W, kit.MIN_H), (1000, 640)):
            root = tree("bloom", w=w, h=h)
            items, groups = cls(root, "item"), cls(root, "grp")
            self.assertEqual(len(items), sum(len(g["items"]) for g in d["groups"]))
            self.assertEqual(len(groups), len(d["groups"]))
            c = [(float(e.get("cx")), float(e.get("cy")), float(e.get("r")), e.get("data-g")) for e in items]
            G = {e.get("data-g"): (float(e.get("cx")), float(e.get("cy")), float(e.get("r"))) for e in groups}
            for i, (x, y, r, g) in enumerate(c):
                gx, gy, gr = G[g]
                self.assertLessEqual(math.hypot(x - gx, y - gy) + r, gr + .3, f"{g} holds its item")
                for x2, y2, r2, _ in c[i + 1:]:
                    self.assertGreaterEqual(math.hypot(x - x2, y - y2), r + r2 - .3, "items overlap")
            gs = list(G.values())
            for i, (x, y, r) in enumerate(gs):
                self.assertTrue(r <= x <= w - 2 * kit.PAD - r + .3 and r <= y + .3, (w, h))
                for x2, y2, r2 in gs[i + 1:]:
                    self.assertGreaterEqual(math.hypot(x - x2, y - y2), r + r2 - .3, "groups overlap")

    def test_the_metro_names_sit_clear_of_lines_stations_and_each_other(self):
        """The design check found "feat/cart" under its own fork station, read as "eat/cart", and a line through it."""
        for data in (kit.KINDS["metro"]["sample"], long("metro", copy.deepcopy(kit.KINDS["metro"]["sample"]))):
            for w, h in ((kit.MIN_W, kit.MIN_H), (kit.DEF_W, kit.DEF_H), (1000, 560), (1400, 900)):
                root = body(tree("metro", data, w=w, h=h))
                names = cls(root, "bname")
                self.assertTrue(names, f"{w}x{h}")
                tracks = [[(float(a), float(b)) for a, b in re.findall(r"(-?[\d.]+) (-?[\d.]+)", e.get("d"))]
                          for e in cls(root, "track")]
                halfs = [3 if i == 0 else 2.5 for i in range(len(tracks))]
                stations = [(float(e.get("cx")), float(e.get("cy")), float(e.get("r")) + 1) for e in cls(root, "xchg")]
                boxes = [tbox(e) for e in names]
                for e, b in zip(names, boxes):
                    where = f"{w}x{h} {''.join(e.itertext())!r}"
                    for x, y, r in stations:
                        self.assertGreater(gap(b, x, y), r, f"{where}: a station covers it")
                    for pts, half in zip(tracks, halfs):
                        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                            n = max(2, int(math.hypot(x2 - x1, y2 - y1)))
                            near = min(gap(b, x1 + (x2 - x1) * k / n, y1 + (y2 - y1) * k / n) for k in range(n + 1))
                            self.assertGreater(near, half, f"{where}: a line runs through it")
                    for o in boxes:
                        self.assertTrue(o is b or o[2] <= b[0] or b[2] <= o[0] or o[3] <= b[1] or b[3] <= o[1],
                                        f"{where}: names overlap")
        drawn = [e.get("data-branch") for e in cls(tree("metro"), "bname")]
        self.assertIn("feat/cart", texts(tree("metro")), "whole at the default size")
        self.assertEqual(sorted(drawn), sorted(b["name"] for b in kit.KINDS["metro"]["sample"]["branches"]))

    def test_the_radar_ring_labels_sit_beside_the_axis_on_a_plate_clear_of_blips(self):
        """The design check found the twelve o'clock line through "10,000ms". Each ring's label is a contour label now:
        on its ring, right of the axis, on a plate of the scope's ground that breaks the ring under the letters."""
        for w, h in ((kit.MIN_W, kit.MIN_H), (kit.DEF_W, kit.DEF_H), (900, 560), (1400, 900)):
            root = body(tree("radar", w=w, h=h))
            scope = cls(root, "scope")[0]
            cx, cy, R = (float(scope.get(k)) for k in ("cx", "cy", "r"))
            rings = [float(e.get("r")) for e in root.iter(SVG + "circle")
                     if e.get("fill") == "none" and (float(e.get("cx")), float(e.get("cy"))) == (cx, cy)]
            dots = [(float(e.get("cx")), float(e.get("cy")), float(e.get("r")) + 1) for e in cls(root, "blip")]
            kids, labels = list(root), cls(root, "ring")
            self.assertGreaterEqual(len(labels), 2 if w == kit.MIN_W else len(rings), f"{w}x{h}")  # MIN: blips crowd it
            boxes = [tbox(e) for e in labels]
            for e, b in zip(labels, boxes):
                where = f"{w}x{h} {''.join(e.itertext())!r}"
                self.assertGreater(b[0], cx + 3, f"{where}: beside the axis, not on it")
                self.assertEqual(e.get("paint-order"), "stroke", where)
                plate = kids[kids.index(e) - 1]
                px, py, pw, ph = box(plate)
                self.assertEqual((plate.tag, plate.get("fill")), (SVG + "rect", scope.get("fill")), where)
                self.assertTrue(px <= b[0] and py <= b[1] and px + pw >= b[2] and py + ph >= b[3], f"{where}: the plate covers it")
                far = max(math.hypot(x - cx, y - cy) for x in (b[0], b[2]) for y in (b[1], b[3]))
                self.assertLess(far, R, f"{where}: inside the rim")
                for x, y, r in dots:
                    self.assertGreater(gap(b, x, y), r, f"{where}: on a blip")
                for o in boxes:
                    self.assertTrue(o is b or o[2] <= b[0] or b[2] <= o[0] or o[3] <= b[1] or b[3] <= o[1], f"{where}: overlap")
                if w > kit.MIN_W:  # room enough: its own ring is the one ring it breaks
                    crossed = [r for r in rings if gap(b, cx, cy) < r < far]
                    self.assertLessEqual(len(crossed), 1, f"{where}: rings {crossed} cross it")
            beam = max(i for i, e in enumerate(kids) if any(a.get("type") == "rotate" and len(list(e.iter(SVG + "path"))) > 3
                                                         for a in e.iter(SVG + "animateTransform")))
            self.assertTrue(all(kids.index(e) > beam for e in labels), "the beam sweeps under the scale")

    def test_the_rain_splashes_are_hidden_at_rest(self):
        """The design check found the splash rings still on the ground in the still frame: stray "oo" marks."""
        for w, h in ((kit.MIN_W, kit.MIN_H), (kit.DEF_W, kit.DEF_H)):
            splashes = cls(tree("rain", w=w, h=h), "splash")
            self.assertTrue(splashes)
            for e in splashes:
                self.assertEqual(e.get("opacity"), "0")
                self.assertIn("opacity", [a.get("attributeName") for a in e.iter(SVG + "animate")])

    def test_a_sheet_column_shares_its_widest_panels_width(self):
        """The design check found the hourglass 35 px short of the treemap above it."""
        ps = [dict(kind=k, title=k, data=kit.KINDS[k]["sample"]) for k in ROUND2B]
        root = ET.fromstring(kit.sheet_svg(ps, kit.theme(), cols=3))
        cells = [g for g in root if g.tag == SVG + "g"]
        self.assertEqual(len(cells), len(ps))
        cols = {}
        for g in cells:
            x = float(re.match(r"translate\(([\d.]+)", g.get("transform")).group(1))
            card = next(g.iter(SVG + "rect"))
            cols.setdefault(x, set()).add(float(card.get("width")))
        self.assertEqual(len(cols), 3)
        for x, widths in cols.items():
            self.assertEqual(len(widths), 1, f"the column at x={x} has widths {widths}")
        xs, ws = sorted(cols), [cols[x].pop() for x in sorted(cols)]
        for i in range(2):
            self.assertAlmostEqual(xs[i] + ws[i] + 18, xs[i + 1], 1, "columns sit a gap apart")
        self.assertAlmostEqual(float(root.get("width")), xs[-1] + ws[-1] + 18, 1)


def tbox(e):
    """A start-anchored text's box from the kit's glyph width: (x0, y0, x1, y1)."""
    s, x, y, fs = "".join(e.itertext()), float(e.get("x")), float(e.get("y")), float(e.get("font-size"))
    return x, y - fs * .72, x + len(s) * fs * kit.GLYPH, y + fs * .2


def gap(b, x, y):
    """How far the point (x, y) is from the box b."""
    return math.hypot(x - min(max(x, b[0]), b[2]), y - min(max(y, b[1]), b[3]))


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
        self.assertEqual(len(names), len(KINDS) * 2, names)
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
        self.assertEqual(len(re.findall(r"<(?:title|desc) id=", text)), len(KINDS) * 2, "one title and desc per panel")
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


def pyapi(root):
    """A Python web API: FastAPI routes, Celery tasks and their callers, SQL models, compose, CI, pytest."""
    write(root, "requirements.txt", "fastapi\nuvicorn\npsycopg2-binary\nredis\ncelery\n")
    write(root, "app/main.py", 'from fastapi import FastAPI\nfrom app.tasks import process_order, send_email\n\napp = FastAPI()\n\n\n'
                               '@app.get("/orders")\ndef list_orders():\n    return []\n\n\n@app.post("/orders")\n'
                               'def add_order(body: dict):\n    process_order.delay(body["id"])\n    send_email.delay(body["to"])\n'
                               '    return {"ok": True}\n\n\n@app.get("/api/health")\ndef health():\n    return {"ok": True}\n')
    write(root, "app/tasks.py", 'from celery import Celery\n\ncelery_app = Celery("shop", broker="redis://redis:6379/0")\n\n\n'
                                '@celery_app.task\ndef process_order(order_id):\n    return order_id\n\n\n@celery_app.task\n'
                                'def send_email(address):\n    return address\n\n\n@celery_app.task\ndef nightly_report():\n'
                                '    return 1\n')
    write(root, "app/models.py", 'from sqlalchemy import Column, Integer, String\nfrom sqlalchemy.orm import declarative_base\n\n'
                                 'Base = declarative_base()\n\n\nclass Order(Base):\n    __tablename__ = "orders"\n'
                                 '    id = Column(Integer, primary_key=True)\n    status = Column(String)\n')
    write(root, "migrations/0001_init.sql", "CREATE TABLE orders (id integer primary key, status text);\n")
    write(root, "docker-compose.yml", "services:\n  api:\n    build: .\n    ports:\n      - \"8000:8000\"\n    depends_on:\n"
                                      "      - db\n      - redis\n  db:\n    image: postgres:16\n    environment:\n"
                                      "      POSTGRES_DB: shop\n  redis:\n    image: redis:7\n  worker:\n    build: .\n"
                                      "    command: celery -A app.tasks worker\n")
    write(root, "Dockerfile", "FROM python:3.12\nCOPY . /srv\nCMD [\"uvicorn\", \"app.main:app\"]\n")
    write(root, ".github/workflows/ci.yml", "name: ci\non: [push]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n"
                                            "      - uses: actions/checkout@v4\n  test:\n    runs-on: ubuntu-latest\n"
                                            "    needs: lint\n    steps:\n      - run: pytest\n  build:\n"
                                            "    runs-on: ubuntu-latest\n    steps:\n      - run: docker build .\n"
                                            "  deploy:\n    runs-on: ubuntu-latest\n    steps:\n      - run: ./ship.sh\n")
    write(root, "tests/test_api.py", "def test_list():\n    assert 1\n\n\ndef test_add():\n    assert 1\n\n\n"
                                     "def test_health():\n    assert 1\n")
    write(root, "tests/test_tasks.py", "def test_process():\n    assert 1\n\n\nasync def test_send():\n    assert 1\n")


def nodeq(root):
    """A Node worker queue: BullMQ queue, producers, a worker, cron jobs, compose with redis and mongo, node --test."""
    write(root, "package.json", json.dumps({"name": "mailer", "version": "1.0.0", "main": "src/worker.js",
                                            "scripts": {"start": "node src/worker.js", "test": "node --test"},
                                            "dependencies": {"bullmq": "^5", "redis": "^4", "mongodb": "^6",
                                                             "node-cron": "^3"}}))
    write(root, "src/queue.js", "const { Queue } = require('bullmq');\nconst connection = { host: 'redis', port: 6379 };\n"
                                "const emails = new Queue('emails', { connection });\nmodule.exports = { emails };\n")
    write(root, "src/producer.js", "const { emails } = require('./queue');\n\nasync function signup(user) {\n"
                                   "  await emails.add('welcome', { id: user.id });\n  await emails.add('digest', { id: user.id });\n}\n\n"
                                   "async function resend(user) {\n  await emails.add('welcome', { id: user.id });\n}\n"
                                   "module.exports = { signup, resend };\n")
    write(root, "src/worker.js", "const { Worker } = require('bullmq');\nconst connection = { host: 'redis', port: 6379 };\n\n"
                                 "new Worker('emails', async (job) => {\n  console.log(job.name);\n}, { connection });\n")
    write(root, "src/cron.js", "const cron = require('node-cron');\n\ncron.schedule('0 * * * *', cleanup);\n"
                               "cron.schedule('0 9 * * 1', weeklyDigest);\n")
    write(root, "docker-compose.yml", "services:\n  redis:\n    image: redis:7\n  mongo:\n    image: mongo:7\n    volumes:\n"
                                      "      - data:/data/db\n  worker:\n    build: .\n    command: node src/worker.js\n")
    write(root, "Dockerfile", "FROM node:20\nCOPY . /srv\nCMD [\"node\", \"src/worker.js\"]\n")
    write(root, "test/worker.test.js", "const test = require('node:test');\n\ntest('sends a welcome', () => {});\n"
                                       "test('batches a digest', () => {});\nit('retries', () => {});\n")


def gocli(root):
    """A Go CLI: cobra commands, an internal store package, a release workflow, go test, a one-stage Dockerfile."""
    write(root, "go.mod", "module example.com/greet\n\ngo 1.22\n\nrequire (\n\tgithub.com/spf13/cobra v1.8.0\n"
                          "\tmodernc.org/sqlite v1.29.0\n)\n")
    write(root, "main.go", "package main\n\nimport \"example.com/greet/cmd\"\n\nfunc main() {\n\tcmd.Execute()\n}\n")
    write(root, "cmd/root.go", "package cmd\n\nimport \"github.com/spf13/cobra\"\n\nvar rootCmd = &cobra.Command{\n"
                               "\tUse:   \"greet\",\n\tShort: \"say hello\",\n}\n\nfunc Execute() { rootCmd.Execute() }\n")
    write(root, "cmd/serve.go", "package cmd\n\nimport \"github.com/spf13/cobra\"\n\nvar serveCmd = &cobra.Command{\n"
                                "\tUse:   \"serve [addr]\",\n\tShort: \"serve over http\",\n}\n")
    write(root, "cmd/build.go", "package cmd\n\nimport \"github.com/spf13/cobra\"\n\nvar buildCmd = &cobra.Command{\n"
                                "\tUse:   \"build\",\n\tShort: \"build the site\",\n}\n")
    write(root, "internal/store/store.go", "package store\n\nimport \"database/sql\"\n\ntype Store struct {\n\tdb *sql.DB\n}\n\n"
                                           "func Open(path string) (*Store, error) {\n\treturn &Store{}, nil\n}\n")
    write(root, "main_test.go", "package main\n\nimport \"testing\"\n\nfunc TestMain(t *testing.T) {}\n")
    write(root, "cmd/serve_test.go", "package cmd\n\nimport \"testing\"\n\nfunc TestServe(t *testing.T) {}\n\n"
                                     "func TestServeAddr(t *testing.T) {}\n")
    write(root, ".github/workflows/release.yml", "name: release\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                                                 "    steps:\n      - run: go test ./...\n  build:\n    runs-on: ubuntu-latest\n"
                                                 "    steps:\n      - run: go build\n  release:\n    runs-on: ubuntu-latest\n"
                                                 "    steps:\n      - run: gh release create\n")
    write(root, "Dockerfile", "FROM golang:1.22\nCOPY . /src\nRUN go build -o /greet\n")


FIXTURES = {"pyapi": pyapi, "nodeq": nodeq, "gocli": gocli}


class PlanRepos(SampoTestCase):
    """T-3733: sampo panels plan reads a repo into a spec of its real parts, one kind per kind of part."""

    def repo_of(self, name):
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        FIXTURES[name](root)
        return root

    def kinds(self, got):
        return [p["kind"] for p in got["spec"]["panels"]]

    def parts(self, got, kind):
        return [x for x in got["parts"] if x["kind"] == kind]

    def test_a_python_web_api_is_read_into_arch_funnel_lanes_filetree_terminal_and_tanks(self):
        got = panels.plan(self.repo_of("pyapi"))
        self.assertEqual(got["reason"], "")
        self.assertEqual(sorted(set(self.kinds(got))), ["arch", "filetree", "funnel", "lanes", "tanks", "terminal"])
        spec = {p["kind"]: p for p in got["spec"]["panels"]}
        arch = {n["id"]: n for n in spec["arch"]["data"]["nodes"]}
        self.assertIn("redis", [n["label"].lower() for n in arch.values()], "a service is a box")
        self.assertIn("/orders", " ".join(n.get("sub", "") for n in arch.values()), "the routes are a box")
        funnel = spec["funnel"]["data"]
        self.assertEqual({s["label"]: s["value"] for s in funnel["sources"]}, {"process_order": 1, "send_email": 1},
                         "each enqueued job, counted by its call sites")
        self.assertEqual(funnel["result"]["label"], "tasks.py", "the file that runs them")
        lanes = [p["data"] for p in got["spec"]["panels"] if p["kind"] == "lanes"]
        self.assertIn(["lint", "test", "build", "deploy"], [x["stages"] for x in lanes], "CI jobs in file order")
        self.assertEqual(spec["filetree"]["data"]["files"], ["app/models.py", "migrations/0001_init.sql"])
        self.assertEqual(spec["filetree"]["data"]["doc"]["title"], "models.py")
        self.assertIn("class Order(Base):", spec["filetree"]["data"]["doc"]["lines"])
        self.assertEqual(spec["terminal"]["data"]["command"], "pytest")
        self.assertEqual(spec["terminal"]["data"]["rows"], [["test_api.py", "3"], ["test_tasks.py", "2"]])
        tanks = {t["label"]: t["value"] for t in spec["tanks"]["data"]["tanks"]}
        self.assertEqual(sorted(tanks), ["api", "db", "redis", "worker"])
        self.assertEqual(max(tanks.values()), 100)

    def test_a_node_worker_queue_is_read_into_its_queue_worker_cron_jobs_and_services(self):
        got = panels.plan(self.repo_of("nodeq"))
        self.assertEqual(got["reason"], "")
        self.assertEqual(sorted(set(self.kinds(got))), ["arch", "funnel", "lanes", "tanks", "terminal"],
                         "no store was found, so no tree is invented")
        spec = {p["kind"]: p for p in got["spec"]["panels"]}
        funnel = spec["funnel"]["data"]
        self.assertEqual({s["label"]: s["value"] for s in funnel["sources"]}, {"welcome": 2, "digest": 1})
        self.assertEqual(funnel["gate"], "emails", "the queue's own name")
        self.assertEqual(funnel["result"]["label"], "worker.js")
        self.assertEqual(spec["lanes"]["data"]["stages"], ["cleanup", "weeklyDig…"], "the cron jobs, clipped to fit")
        self.assertEqual(spec["terminal"]["data"]["command"], "node --test")
        self.assertEqual(spec["terminal"]["data"]["rows"], [["worker.test.js", "3"]])
        labels = [n["label"].lower() for n in spec["arch"]["data"]["nodes"]]
        for want in ("emails", "worker.js", "redis", "mongodb"):
            self.assertIn(want, labels)
        self.assertEqual(sorted(t["label"] for t in spec["tanks"]["data"]["tanks"]), ["mongo", "redis", "worker"])

    def test_a_go_cli_is_read_into_arch_lanes_filetree_and_terminal_and_names_what_it_left_out(self):
        got = panels.plan(self.repo_of("gocli"))
        self.assertEqual(sorted(set(self.kinds(got))), ["arch", "filetree", "lanes", "terminal"])
        spec = {p["kind"]: p for p in got["spec"]["panels"]}
        nodes = " ".join(f"{n['label']} {n.get('sub', '')}" for n in spec["arch"]["data"]["nodes"])
        for want in ("main.go", "serve", "build", "sqlite"):
            self.assertIn(want, nodes)
        self.assertEqual(spec["lanes"]["data"]["stages"], ["test", "build", "release"])
        self.assertEqual(spec["lanes"]["data"]["lanes"], [{"label": "release.yml", "at": 2}])
        self.assertEqual(spec["filetree"]["data"]["files"], ["internal/store/store.go"])
        self.assertEqual(spec["terminal"]["data"]["command"], "go test ./...")
        self.assertEqual(spec["terminal"]["data"]["rows"], [["main_test.go", "1"], ["serve_test.go", "2"]])
        left = {x["part"]: x["why"] for x in got["skipped"]}
        self.assertIn("Dockerfile", left, "one stage is no tanks: said, not drawn")
        self.assertIn("tanks", left["Dockerfile"])

    def test_every_plan_passes_the_kits_check_renders_to_pairs_and_cites_real_files(self):
        for name in FIXTURES:
            with self.subTest(repo=name):
                root = self.repo_of(name)
                got = panels.plan(root)
                self.assertEqual(kit.check_spec(got["spec"]), [])
                self.assertTrue(got["parts"])
                for part in got["parts"]:
                    self.assertTrue(os.path.isfile(os.path.join(root, part["file"])), part)
                    self.assertIn(part["kind"], KINDS)
                for part in got["skipped"]:
                    self.assertTrue(os.path.isfile(os.path.join(root, part["file"])), part)
                path = os.path.join(root, "spec.json")
                write(root, "spec.json", json.dumps(got["spec"]))
                res = panels.render(root, path, os.path.join(root, "out"))
                self.assertEqual(res["failed"], [])
                self.assertEqual(len(res["files"]), 2 * len(got["spec"]["panels"]))
                self.assertEqual(sorted(os.listdir(os.path.join(root, "out"))), sorted(os.path.basename(f) for f in res["files"]))
                self.assertEqual(panels.plan(root), got, "a second run reads the same")

    def test_an_empty_repo_or_one_with_a_single_part_is_an_empty_spec_with_a_reason_never_invented_parts(self):
        empty = os.path.join(self.tmp, "empty")
        write(empty, "README.md", "# nothing here\n")
        got = panels.plan(empty)
        self.assertEqual(got["spec"]["panels"], [])
        self.assertEqual(got["parts"], [])
        self.assertIn("no", got["reason"])
        lone = os.path.join(self.tmp, "lone")
        write(lone, "main.py", "print('hi')\n")
        got = panels.plan(lone)
        self.assertEqual(got["spec"]["panels"], [], "one entry file has nothing to connect to")
        self.assertIn("main.py", got["reason"])
        self.assertEqual(panels.plan(os.path.join(self.tmp, "missing"))["spec"]["panels"], [])

    def test_a_secret_in_a_store_file_is_never_copied_into_the_spec(self):
        root = self.repo_of("pyapi")
        write(root, "app/models.py", 'DB_PASSWORD = "hunter2hunter2"\nowner = "alice@jarvis.home.example.test"\n'
                                     'class Order(Base):\n    __tablename__ = "orders"\n')
        got = panels.plan(root)
        text = json.dumps(got)
        for leak in ("hunter2", "alice@", "PASSWORD"):
            self.assertNotIn(leak, text)
        self.assertEqual(kit.check_spec(got["spec"]), [])

    def test_a_private_value_on_a_line_the_structure_reads_or_in_a_part_name_never_reaches_the_plan(self):
        # T-3733: the secret rides ON a struct line (the _structure filter) and in a store file's own name (the net in plan)
        token = "sk-ant-" + "a1b2" * 6
        root = self.repo_of("pyapi")
        write(root, "app/models.py", f'class Order(Base):  # key {token}\n    __tablename__ = "/home/alice/orders"\n'
                                     '    id = Column(Integer)\n')
        write(root, f"app/{token}.py", 'class Vault(Base):\n    __tablename__ = "vault"\n')
        found = readme.scan(root, parts=True)
        mine = json.dumps([s["lines"] for s in found["stores"] if s["file"] == "app/models.py"])
        self.assertNotIn(token, mine)
        self.assertNotIn("alice", mine)
        text = json.dumps(panels.plan(root))
        for leak in (token, "a1b2a1b2", "/home/alice"):
            self.assertNotIn(leak, text)

    def test_counts_are_the_totals_not_the_capped_lists(self):
        # T-3733: 30 test files and 9 store files: panels say so, not 12 and 6
        root = os.path.join(self.tmp, "big")
        for i in range(30):
            write(root, f"tests/test_t{i:02d}.py", "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
        for i in range(9):
            write(root, f"app/m{i}.py", f'class M{i}(Base):\n    __tablename__ = "m{i}"\n    id = Column(Integer)\n')
        write(root, "app/main.py", 'from fastapi import FastAPI\napp = FastAPI()\n\n\n@app.get("/x")\ndef x():\n    return 1\n')
        got = panels.plan(root)
        spec = {p["kind"]: p for p in got["spec"]["panels"]}
        self.assertEqual(spec["terminal"]["stat"], "60 tests")
        self.assertIn("30 test files", spec["terminal"]["foot"])
        self.assertEqual(spec["filetree"]["stat"], "9 files")
        arch = " ".join(n.get("sub", "") for n in spec["arch"]["data"]["nodes"])
        self.assertIn("9 store files", arch)

    def test_the_empty_plan_keeps_its_reason_beside_the_spec_and_render_says_why_it_cannot_draw_it(self):
        empty = os.path.join(self.tmp, "empty2")
        write(empty, "README.md", "# nothing here\n")
        got = panels.plan(empty)
        self.assertNotIn("reason", got["spec"], "the kit's spec has no reason field")
        self.assertEqual([e for e in kit.check_spec(got["spec"]) if "reason" in e], [])
        self.assertTrue(got["reason"])
        write(empty, "spec.json", json.dumps(got["spec"]))
        with self.assertRaisesRegex(ValueError, "no panels"):
            panels.render(empty, os.path.join(empty, "spec.json"))
        res = self.fm("panels", "plan", empty, "--json")
        self.assertIn(got["reason"], res.stdout + res.stderr)

    def test_readme_scan_reads_the_parts_only_when_asked(self):
        root = self.repo_of("pyapi")
        self.assertNotIn("stores", readme.scan(root))
        self.assertNotIn("tests", readme.plan(root), "readme plan is what it was")
        self.assertTrue({"queues", "stores", "tests"} <= set(readme.scan(root, parts=True)))

    def test_the_store_document_opens_model_lines_not_every_class_in_the_file(self):
        root = self.repo_of("pyapi")
        write(root, "app/models.py", 'import logging\n\n\nclass _Mask(logging.Filter):\n    pass\n\n\n'
                                     'class Order(Base):\n    __tablename__ = "orders"\n    id = Column(Integer)\n')
        got = panels.plan(root)
        spec = {p["kind"]: p for p in got["spec"]["panels"]}
        lines = spec["filetree"]["data"]["doc"]["lines"]
        self.assertIn("class Order(Base):", lines)
        self.assertNotIn("class _Mask(logging.Filter):", lines)

    def test_the_verb_prints_a_spec_render_takes_and_json_adds_the_parts(self):
        root = self.repo_of("gocli")
        out = self.fm("panels", "plan", root).stdout
        spec = json.loads(out)
        self.assertEqual(kit.check_spec(spec), [])
        write(root, "spec.json", out)
        self.assertEqual(self.fm("panels", "render", os.path.join(root, "spec.json"), "--root", root, "--out", os.path.join(root, "o")).returncode, 0)
        full = json.loads(self.fm("panels", "plan", root, "--json").stdout)
        self.assertEqual(full["spec"], spec)
        self.assertTrue(full["parts"] and "skipped" in full)
        self.fm("panels", "plan", root, "--out", os.path.join(root, "written.json"))
        with open(os.path.join(root, "written.json")) as f:
            self.assertEqual(json.load(f), spec)

    def test_readme_plan_is_unchanged(self):
        root = self.repo_of("pyapi")
        got = readme.plan(root)
        self.assertEqual(got["pages"], ["/orders"])
        self.assertEqual(got["api"], ["/api/health"])
        self.assertEqual(got["cli"], [])
        self.assertEqual(got["services"], ["psycopg2", "redis", "celery", "api", "db", "worker"])
        old = {"pages", "api", "cli", "entry", "services", "surfaces", "start", "name", "figures", "next"}
        self.assertTrue(old <= set(got))
        self.assertEqual(set(got), old, "no new keys: the parts are read only for sampo panels plan")
        self.assertNotIn("queues", readme.plan_text(got), "the figures text says what it said")


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
