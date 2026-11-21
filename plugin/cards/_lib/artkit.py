"""T-4995 (a slice of T-3311): what the art cards share. An art card is a panel-* card (card.json, gen.py, check.py,
tests.json) whose slots are at most 8 typed values and whose gen.py draws one picture as STEM-dark.svg and
STEM-light.svg, coloured only from the design tokens (panelkit's styles: bg, panel, line, ink, muted, six accents).

gen.py: art_main(draw) checks the call's slots against the card's own card.json and writes the pair; draw(slots, T)
returns (body, defs, the keyframe names used, desc). Everything is drawn inside a clip to the 640x400 frame, so nothing paints past the viewBox.
Motion is CSS from the `motion` slot (a duration from an enum, or none); works judges the still frame only.

check.py: art_check() runs the source gates the card jail can judge itself, then renders both files through
sampo.works (the headless browser of `sampo works check`). Unrendered is never green: no browser on the box, or the
callcards jail (no socket, no read of Sampo's library), exits 1 with `environment: missing a render: not rendered`,
which callcards reports as status environment, not as the card's failure."""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

from panelkit import N, esc, rng, theme, validate

W, H = 640, 400
SIZE_CAP, SHAPES = 120_000, (2, 1500)  # works' card type: bytes, shapes
SVG_NS = "http://www.w3.org/2000/svg"
SHAPE_TAGS = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "use", "image"}
SKIP_GATES = {"motion", "unique"}  # T-3311 non-goals: no animation check beyond a still frame; a pair is one picture
KEYFRAMES = {
    "spin": "@keyframes spin{to{transform:rotate(360deg)}}",
    "spinr": "@keyframes spinr{to{transform:rotate(-360deg)}}",
    "drift": "@keyframes drift{to{transform:translateX(-640px)}}",  # a strip drawn twice, 640 apart, loops seamlessly
    "sweep": "@keyframes sweep{from{transform:translateX(-280px)}to{transform:translateX(640px)}}",
    "pulse": "@keyframes pulse{50%{opacity:.2}}",  # stepped per half: steps(N) applies between each keyframe pair
}


def fail(msg):
    sys.stderr.write(str(msg).rstrip() + "\n")
    sys.exit(1)


# ---------------------------------------------------------------- drawing helpers

def seq(slots, *keys):
    """A deterministic number stream for these slots: the same slots draw the same picture, byte for byte."""
    return rng(":".join(str(slots[k]) for k in keys)).__next__


def smooth(pts, close=None):
    """A Catmull-Rom curve through pts as an SVG path; close=(y) runs it down to y at both ends and shuts it."""
    if len(pts) < 2:
        return ""
    d = [f"M{N(pts[0][0])} {N(pts[0][1])}"]
    for i in range(len(pts) - 1):
        p0, p1, p2 = pts[max(i - 1, 0)], pts[i], pts[i + 1]
        p3 = pts[min(i + 2, len(pts) - 1)]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{N(c1[0])} {N(c1[1])} {N(c2[0])} {N(c2[1])} {N(p2[0])} {N(p2[1])}")
    if close is not None:
        d.append(f"L{N(pts[-1][0])} {N(close)}L{N(pts[0][0])} {N(close)}Z")
    return "".join(d)


def poly(pts, close=None):
    """Straight segments through pts (a ridge's jagged line); close as smooth's."""
    d = "M" + "L".join(f"{N(x)} {N(y)}" for x, y in pts)
    return d + (f"L{N(pts[-1][0])} {N(close)}L{N(pts[0][0])} {N(close)}Z" if close is not None else "")


def attrs(**kw):
    """name="value" pairs, None left out, trailing underscores and inner underscores as SVG's dashes."""
    return "".join(f' {k.rstrip("_").replace("_", "-")}="{v}"' for k, v in kw.items() if v is not None)


def el(tag, extra="", **kw):
    """An empty element; extra is raw attribute text appended as is (anim()'s class and style)."""
    return f"<{tag}{attrs(**kw)}{extra}/>"


def anim(cls, motion, factor=1.0, origin=None, delay=0.0):
    """class and style attributes that run keyframes `cls` at motion x factor, or nothing for a still picture."""
    if motion == "none":
        return ""
    style = []
    if origin:
        style.append(f"transform-origin:{N(origin[0])}px {N(origin[1])}px")
    if factor != 1.0:
        style.append(f"animation-duration:{N(seconds(motion) * factor)}s")
    if delay:
        style.append(f"animation-delay:-{N(delay)}s")
    return f' class="{cls}"' + (f' style="{";".join(style)}"' if style else "")


def seconds(motion):
    return float(motion.rstrip("s"))


def css(motion, used):
    """The stylesheet for the keyframes used, each looping forever at the slot's duration; reduced motion stops all."""
    if motion == "none" or not used:
        return ""
    # everything changes in fine steps, never on every frame: under load Chromium re-rasters a whole SVG-as-image for
    # each frame anything in it moves, and starves the screenshot past works' 30 s (measured: drift 6 ribbons 55 s and
    # a timeout linear, 8.6 s in steps(320), steps(160) halves it again; a mountain day with an eased halo pulse timed out); a 4 px or half-degree
    # step still reads as a glide
    timing = {"pulse": "steps(16)", "spin": "steps(720)", "spinr": "steps(720)"}
    rules = [f".{k}{{animation:{k} {motion} {timing.get(k, 'steps(160)')} infinite}}" for k in sorted(used)]
    return "".join(rules + [KEYFRAMES[k] for k in sorted(used)]) + \
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"


def glow(gid, colour, cx, cy, r, op):
    """A radial glow: colour at op in the middle, gone at the rim (a local url(#id): it reaches nothing)."""
    return (f'<radialGradient id="{gid}" cx="{N(cx)}" cy="{N(cy)}" r="{N(r)}" gradientUnits="userSpaceOnUse">'
            f'<stop offset="0" stop-color="{colour}" stop-opacity="{op}"/>'
            f'<stop offset="1" stop-color="{colour}" stop-opacity="0"/></radialGradient>')


def ramp(gid, stops, x2=0, y2=1, frame=False):
    """A linear gradient, top to bottom by default: stops [(offset, colour[, opacity])]. frame: (0,0)-(x2,y2) are
    picture coordinates, so every shape that uses it shares one ramp across the frame."""
    units = ' gradientUnits="userSpaceOnUse"' if frame else ""
    return (f'<linearGradient id="{gid}"{units} x1="0" y1="0" x2="{N(x2)}" y2="{N(y2)}">'
            + "".join(f'<stop offset="{N(st[0])}" stop-color="{st[1]}"'
                      + (f' stop-opacity="{N(st[2])}"' if len(st) > 2 else "") + "/>" for st in stops)
            + "</linearGradient>")


def document(slots, T, body, defs, style, desc):
    """The whole picture: role=img with a title and desc, the token ground, the art clipped to the frame."""
    ident = "art"
    return (f'<svg xmlns="{SVG_NS}" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
            f'aria-labelledby="{ident}-t {ident}-d"><title id="{ident}-t">{esc(slots["title"])}</title>'
            f'<desc id="{ident}-d">{esc(desc)}</desc>' + (f"<style>{style}</style>" if style else "")
            + f'<defs>{defs}<clipPath id="{ident}-frame"><rect width="{W}" height="{H}"/></clipPath></defs>'
            f'<rect width="{W}" height="{H}" fill="{T.bg}"/><g clip-path="url(#{ident}-frame)">{body}</g></svg>\n')


# ---------------------------------------------------------------- gen.py

def _card(here):
    with open(os.path.join(here, "card.json"), encoding="utf-8") as f:
        return json.load(f)


def art_main(draw):
    """gen.py: the slots checked against card.json (each error names its slot), defaults filled, then the pair."""
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    schema = _card(here)["inputs"]
    inp = json.load(sys.stdin).get("inputs")
    errs = []
    validate(inp, schema, "inputs", errs)
    if not errs and (os.path.isabs(inp["out"]) or ".." in inp["out"].replace("\\", "/").split("/")):
        errs.append(f"inputs.out {inp['out'][:60]!r} must be a new path inside the project")  # callcards' newpath
    if errs:
        fail("; ".join(errs[:4]))
    slots = {k: s["default"] for k, s in schema["properties"].items() if "default" in s}
    slots.update(inp)
    os.makedirs(os.path.dirname(slots["out"]) or ".", exist_ok=True)
    for scheme in ("dark", "light"):
        T = theme(slots["style"], scheme)
        body, defs, used, desc = draw(slots, T)
        text = document(slots, T, body, defs, css(slots["motion"], used), desc)
        with open(f"{slots['out']}-{scheme}.svg", "w", encoding="utf-8") as f:
            f.write(text)


# ---------------------------------------------------------------- check.py

def source_problems(data):
    """[problems] the jail can see in one file by its source alone: well formed, a viewBox, labelled, not too big or
    bare, and safe (nothing that runs, loads or reaches out). works re-checks all of it, and the palette, rendered."""
    if len(data) > SIZE_CAP:
        return [f"size: {len(data)} bytes, over {SIZE_CAP}"]
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return [f"xml: not well-formed ({e})"]
    if root.tag != f"{{{SVG_NS}}}svg" or len((root.get("viewBox") or "").split()) != 4:
        return ["xml: no svg root with a viewBox"]
    bad, tags = [], [e.tag.rsplit("}", 1)[-1] for e in root.iter()]
    for e in root.iter():
        tag = e.tag.rsplit("}", 1)[-1]
        if tag in ("script", "foreignObject", "image", "iframe"):
            bad.append(f"not safe: a {tag} element")
        if tag == "style" and re.search(r"@import|url\(\s*['\"]?\s*(?!#)|\\\\|image(-set)?\s*\(", e.text or "", re.I):
            bad.append("not safe: a style that reaches out")
        for k, v in e.attrib.items():
            name = k.rsplit("}", 1)[-1]
            if name.lower().startswith("on"):
                bad.append(f"not safe: {tag} has a {name} handler")
            elif name == "href" and not v.strip().startswith("#"):
                bad.append(f"not safe: {tag} links outside itself")
            elif re.search(r"url\(\s*['\"]?\s*(?!#)", v, re.I):
                bad.append(f"not safe: {tag}.{name} reaches out")
    if root.get("role") != "img" or "title" not in tags or "desc" not in tags:
        bad.append("labels: no role=img, title and desc")
    shapes = sum(t in SHAPE_TAGS for t in tags)
    if not SHAPES[0] <= shapes <= SHAPES[1]:
        bad.append(f"shapes: {shapes}, outside {SHAPES[0]}-{SHAPES[1]}")
    return bad[:5]


def _works():
    """(works, shot, None) from Sampo's own library beside this one, or (None, None, why): the card jail reads only
    the card, this folder and the scratch copy."""
    lib = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
    if lib not in sys.path:
        sys.path.append(lib)
    try:
        from sampo import shot, works
    except Exception as e:  # a jail's refusal surfaces as any of several errors; each one means no render here
        return None, None, f"sampo.works can't load here ({type(e).__name__}): the card jail reads only the card"
    return works, shot, None


def unrendered(paths, why):
    fail("environment: missing a render: these cards cannot deliver output inside the card jail yet (the pair is "
         f"discarded, nothing is applied); not rendered, {' and '.join(paths)}: {why}")


def art_check():
    """check.py: both files exist and pass the source gates, then render through works and pass every gate it judges
    on the still frame (motion and unique aside: T-3311's non-goals). Prints one line per file when green."""
    inp = json.load(sys.stdin)
    stem = inp["inputs"]["out"]
    paths = [f"{stem}-{s}.svg" for s in ("dark", "light")]
    datas = []
    for p in paths:
        if not os.path.isfile(p):
            fail(f"{p} wasn't written")
        with open(p, "rb") as f:
            datas.append(f.read())
    for p, data in zip(paths, datas):
        bad = source_problems(data)
        if bad:
            fail(f"{p}: " + "; ".join(bad))
    works, shot, why = _works()
    if works is None:
        unrendered(paths, why)
    ws = []
    for p, data in zip(paths, datas):
        w = works.inspect(data, "card")
        w["path"] = p
        bad = [f"{g['gate']}: {g['got']} ({g['bar']})" for g in w["gates"] if g["ok"] is False]
        if bad:
            fail(f"{p}: " + "; ".join(bad))
        ws.append(w)
    browser, why = shot.find_browser("art check")
    if not browser:
        unrendered(paths, why)
    try:
        with shot.killable():
            works._render_all(browser, ws, None)
    except Exception as e:  # the browser, not the work: _render_all already charges a work that hangs it
        unrendered(paths, f"the render broke ({type(e).__name__}: {str(e)[:200]})")
    lines = []
    for w in ws:
        bad = [f"{g['gate']}: {g['got']} ({g['bar']})" for g in w["gates"]
               if g["ok"] is False and g["gate"] not in SKIP_GATES]
        if bad:
            fail(f"{w['path']}: " + "; ".join(bad))
        if not w.get("rendered"):
            unrendered([w["path"]], w.get("error") or "the browser rendered nothing")
        seen = ", ".join(f"{g['gate']} {g['got']}" for g in w["gates"] if g["ok"] is not None
                         and g["gate"] not in SKIP_GATES)
        lines.append(f"{w['path']}: rendered, passed ({seen})")
    print("\n".join(lines))
