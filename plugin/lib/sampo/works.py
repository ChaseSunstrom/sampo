"""Design works (T-3106): sampo works check FILE... renders each SVG headless, in light and dark, and checks it by its
type's measurable gates, so a batch of thousands keeps only the good ones. Models never gate: every verdict is a
number against a bar.

Every SVG is untrusted. It is parsed without entities, stripped of scripts, handlers, foreign content and every
reference outside itself (a stripped thing fails `safe`), then shown only as an <img> (SVG's secure animated mode:
no script, no fetches) on a page whose CSP allows nothing but data: images.

The gates: xml, safe, size, shapes, palette; grid and set (icons); loop (a declared animation); then from the renders
blank, clip (not scenes), contrast (text 4.5:1, an icon's ink 3:1, in both themes), motion (frames that change) and
unique (a perceptual thumb within BAR cells of an earlier work's makes this one the duplicate)."""
import collections
import copy
import math
import os
import pathlib
import re
import stat
import sys
import time
import xml.etree.ElementTree as ET

SVG_NS, XLINK_NS, XML_NS = "http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink", \
    "http://www.w3.org/XML/1998/namespace"
FOREIGN = {"http://www.w3.org/1999/xhtml", "http://www.w3.org/1998/Math/MathML"}
SMIL = {"animate", "animateTransform", "animateMotion", "animateColor", "set"}
SHAPES = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "use", "image"}
PAINTS = ("fill", "stroke", "stop-color", "flood-color", "lighting-color", "color")
NOT_COLOURS = {"none", "currentcolor", "transparent", "inherit", "initial", "unset", "context-fill", "context-stroke"}
# bytes, shapes (low, high), palette, render width (px of the longer side)
TYPES = {"icon": (3000, (1, 24), 3, 48), "spot": (40000, (2, 200), 12, 320),
         "card": (120000, (2, 1500), 24, 640), "scene": (200000, (20, 3000), 64, 640),
         "figure": (200000, (2, 3000), 24, 1280)}  # T-3247: a README table or chart, rendered near its shown size
THEMES = {"light": ("#ffffff", "#1b1f24"), "dark": ("#111318", "#e6e8eb")}  # page background, ink (currentColor)
MAX_READ, MAX_ELEMENTS, MAX_DEPTH, MAX_USES = 1 << 20, 20000, 200, 5000  # MAX_USES: copies, all expanded
MAX_RULES = 64  # T-3198: stylesheet rules naming a url(#id), each matched against every element
HANGS = 3  # works in a row the browser hangs or dies on before the rest of the batch is left untried
# the browser's whole environment (no keys or tokens), and no names to resolve and no proxy to reach: no network
ENV_KEEP = ("PATH", "HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "FONTCONFIG_FILE",
            "FONTCONFIG_PATH", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_RUNTIME_DIR",
            "LD_LIBRARY_PATH")
NET_OFF = ("--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=127.0.0.1:9", "--disable-background-networking")
PAD = 0.25  # a render's margin, of each side: what a work paints there is clipped where it is used
FRAMES, FRAME_S = 4, 0.15
BLANK_PCT, TEXT_BAR, INK_BAR, SHARE = 0.5, 4.5, 3.0, 0.10
MIN_TEXT, NEAR = 12, 0.03  # T-3226: pixels of one text colour on one background before its contrast is read (a glyph
# or two); a colour within NEAR per channel of a commoner one is that colour's hinting variant
NOISE = 2  # T-3247: a pixel the text changed by at most this many levels per channel is raster noise between the two
# renders (a shape's edge where the tiles rasterised apart), not ink: it read 1.01:1 on a clear chart in a busy batch
GRID, LEVEL = 24, 96  # thumb: a 24×24 grid of grey means stretched to 0-255; a cell off by over LEVEL differs
# cells, measured on the 48 px clock: a hair wider, recoloured or thinner 0; its hand turned 16, a knob added 12, a
# plus 224. A 544-bit dHash couldn't split them at any grid (hair wider 22 bits, hand turned 16): T-3106.
BAR = 4
XML_BAR = "bar an SVG with a viewBox"
STATIC_CSS = "*{animation:none!important;transition:none!important}"
CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
CSS_URL = re.compile(r"url\(\s*['\"]?\s*([^'\")\s]*)", re.I)
URL_REF = re.compile(r"url\(\s*['\"]?\s*#([^'\")\s]+)", re.I)  # T-3198: what a reference copies, for _uses
CSS_RULE, CSS_WORD = re.compile(r"([^{}]*)\{([^{}]*)\}"), re.compile(r"([.#]?)(-?[_a-zA-Z][\w-]*)")
NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
CSS_PAINT = re.compile(r"(?<![\w-])(" + "|".join(PAINTS) + r")\s*:\s*([^;}!]+)", re.I)


def _local(name):
    return name.rsplit("}", 1)[-1], (name[1:].split("}", 1)[0] if name.startswith("{") else None)


def _say(text, cap=80):
    from sampo import shot
    text = shot.safe(text)
    return text if len(text) <= cap else text[:cap - 1] + "…"


def _parse(data):
    """(root, viewBox, None) or (None, None, why). Entities are never expanded: a DOCTYPE with any is refused."""
    if len(data) > MAX_READ:
        return None, None, f"over {MAX_READ} bytes: not parsed"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "not UTF-8"
    body = re.sub(rb"<!DOCTYPE[^\[>]{0,512}>", b"", data, count=1)  # an export's plain DOCTYPE (bounded: T-3106)
    if re.search(rb"<!(DOCTYPE|ENTITY)", body, re.I):
        return None, None, "a DOCTYPE declaring entities (never expanded)"
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        return None, None, f"not XML: {_say(e)}"
    if root.tag != f"{{{SVG_NS}}}svg":
        return None, None, f"the root is {_say(root.tag)}, not an SVG svg element"
    try:
        vb = [float(x) for x in re.split(r"[\s,]+", (root.get("viewBox") or "").strip())]
    except ValueError:
        vb = []
    if len(vb) != 4 or not all(map(math.isfinite, vb)) or vb[2] < 1e-6 or vb[3] < 1e-6:  # 1e-307 overflows the scale
        return None, None, f"no usable viewBox ({_say(root.get('viewBox') or 'none')!r})"
    if not 1 / 8 <= vb[2] / vb[3] <= 8:
        return None, None, f"a {vb[2]:g}×{vb[3]:g} viewBox: past 8:1"
    count, stack = 0, [(root, 0)]
    while stack:
        el, depth = stack.pop()
        count += 1
        if count > MAX_ELEMENTS or depth > MAX_DEPTH:
            return None, None, f"over {MAX_ELEMENTS} elements or {MAX_DEPTH} deep"
        stack += [(c, depth + 1) for c in el]
    rules = _sheet(root)
    if len(rules) > MAX_RULES:
        return None, None, f"over {MAX_RULES} stylesheet rules naming a url(#…)"
    uses = _uses(root, rules)
    if uses is None or uses > MAX_USES:
        return None, None, "a reference (use, href or url(#…)) that reaches itself" if uses is None else \
            f"over {MAX_USES} copies through use, href or url(#…)"
    return root, vb, None


def _decls(text):
    """[(property, value)] of a CSS declaration block."""
    return [(k.strip().lower(), v) for k, _, v in (d.partition(":") for d in text.split(";")) if v]


def _sheet(root):
    """[(selector parts, declarations)] of the stylesheet rules that name a url(#id). A part is None when it could match
    any element (*, an attribute or pseudo-class alone), else its (prefix, name) words. T-3198."""
    rules = []
    for st in (e for e in root.iter() if _local(e.tag)[0] == "style"):
        text = re.sub(r"/\*.*?\*/", " ", "".join(st.itertext()), flags=re.S)
        for sel, body in CSS_RULE.findall(text):
            if URL_REF.search(body):
                parts = [re.sub(r"::?[\w-]+(\([^)]*\))?", " ", s) for s in sel.split(",")]
                rules.append(([None if "[" in s or "*" in s or not CSS_WORD.search(s) else set(CSS_WORD.findall(s))
                               for s in parts], _decls(body)))
    return rules


def _refs(el, rules):
    """[(id, weight)] el itself names: an href (not a link's), and each url(#id) in its attributes, its style and the
    stylesheet rules its words could match; a marker-mid weighs one per number in its geometry (over its vertices)."""
    local = _local(el.tag)[0]
    decls = [(_local(k)[0].lower(), v) for k, v in el.attrib.items()]
    if local in ("linearGradient", "radialGradient", "stop"):  # draw nothing, so only a gradient's href reaches out
        decls = [(k, v) for k, v in decls if k == "href"]
    else:
        mine = {("", local), ("#", el.get("id")), *((".", x) for x in (el.get("class") or "").split())}
        decls += _decls(el.get("style") or "") + [d for parts, ds in rules
                                                  if any(p is None or p & mine for p in parts) for d in ds]
    mid = 1 + len(NUMBER.findall(" ".join(v for k, v in decls if k in ("d", "points"))))
    out = []
    for k, v in decls:
        if k == "href":
            out += [(v.strip()[1:], 1)] if local != "a" and v.strip().startswith("#") else []
        elif k != "style":
            out += [(i, mid if k in ("marker-mid", "marker") else 1) for i in URL_REF.findall(v)]
    return out


def _uses(root, rules=()):
    """How many copies the browser makes expanding every reference: a <use>'s or another href, and each url(#id) in an
    attribute, a style or a stylesheet rule (patterns, markers, masks, clips, filters; T-3198). Nested ones multiply,
    capped past MAX_USES; None when one reaches itself; a gradient costs no copy. ponytail: counts what sits inside
    <defs> as drawn and a rule as matching every element its words could: over, never under."""
    ids = {}
    for el in root.iter():
        if el.get("id"):
            ids.setdefault(el.get("id"), el)
    own, refs, count, open_, stack = {}, {}, {}, set(), [root]

    def targets(el):
        if el not in refs:
            refs[el] = [(ids.get(i), w) for e in el.iter() for i, w in (own[e] if e in own else own.setdefault(e, _refs(e, rules)))]
        return refs[el]

    def cost(t):
        return 0 if t is not None and _local(t.tag)[0] in ("linearGradient", "radialGradient") else 1

    while stack:
        el = stack[-1]
        if el in count:
            stack.pop()
            continue
        open_.add(el)
        todo = [t for t, _ in targets(el) if t is not None and t not in count]
        if any(t in open_ for t in todo):
            return None
        if todo:
            stack += todo
            continue
        open_.discard(el)
        count[el] = min(MAX_USES + 1, sum(w * (cost(t) + (count[t] if t is not None else 0)) for t, w in targets(el)))
        stack.pop()
    return count[root]


def _bad_css(text):
    """Why a stylesheet or a value could reach outside the work, or None."""
    if "\\" in text:
        return "a CSS escape"
    low = text.lower()
    if "@import" in low:
        return "@import"
    if re.search(r"image(-set)?\s*\(", low):
        return "image()"
    out = next((m.group(1) for m in CSS_URL.finditer(text) if not m.group(1).startswith("#")), None)
    return None if out is None else f"url {_say(out) or '(empty)'}"


def _bad_element(el):
    """None to keep, "" to drop quietly (an editor's metadata), else why it is dropped."""
    local, ns = _local(el.tag)
    if ns in FOREIGN:
        return f"{local} element (HTML or MathML)"
    if ns != SVG_NS:
        return ""
    if local in ("script", "foreignObject"):
        return f"{local} element"
    target = el.get("attributeName", "").strip()
    if local in SMIL and (target.rsplit(":", 1)[-1] == "href" or target.lower().startswith("on")):
        return f"{local} of {_say(target)}"
    if local == "style":
        why = _bad_css("".join(el.itertext()))
        return f"style element: {why}" if why else None
    return None


def _clean_attrs(el, offences):
    for name, value in list(el.attrib.items()):
        local, ns = _local(name)
        why = None
        if ns not in (None, XLINK_NS, XML_NS):
            del el.attrib[name]  # inkscape:label and the like
            continue
        if local.lower().startswith("on"):
            why = f"{local} handler"
        elif local == "href" and not value.strip().startswith("#"):
            why = f"href {_say(value) or '(empty)'}"
        else:
            bad = _bad_css(value)  # presentation attributes are CSS values: url(), escapes
            why = f"{local}: {bad}" if bad else None
        if why:
            offences.append(why)
            del el.attrib[name]


def _sanitise(root, raw):
    """Strip what could run or reach out, in place: the offences found."""
    offences = ["an xml-stylesheet instruction"] if re.search(rb"<\?xml-stylesheet", raw, re.I) else []
    stack = [root]
    while stack:
        el = stack.pop()
        _clean_attrs(el, offences)
        for child in list(el):
            why = _bad_element(child)
            if why is None:
                stack.append(child)
                continue
            el.remove(child)
            if why:
                offences.append(why)
    return offences


def _colours(value, out):
    from sampo import design
    value = re.sub(r"url\([^)]{0,512}\)", " ", value.strip(), flags=re.I)  # bounded: url(#url(#… (T-3106)
    found = [design.hex_of(c) for c in (design._rgb(m.group()) for m in design.COLOUR.finditer(value)) if c]
    if not found and re.fullmatch(r"[a-zA-Z]+", value) and value.lower() not in NOT_COLOURS:
        found = [value.lower()]  # a named colour design's table lacks
    out.update(found)


def _facts(root):
    """What the gates read from the clean tree."""
    shapes, text, palette, strokes, smil, css = 0, False, set(), set(), [], []
    for el in root.iter():
        local = _local(el.tag)[0]
        shapes += local in SHAPES
        text = text or (local == "text" and bool("".join(el.itertext()).strip()))
        if local in SMIL and local != "set":
            smil.append("indefinite" in (el.get("repeatCount", ""), el.get("repeatDur", "")))
        if local == "style":
            css.append("".join(el.itertext()))
        if el.get("style"):
            css.append(el.get("style"))
        for p in PAINTS:
            if el.get(p):
                _colours(el.get(p), palette)
        if el.get("stroke-width"):
            strokes.add(el.get("stroke-width"))
    sheet = re.sub(r"/\*.*?(?:\*/|$)", " ", "\n".join(css), flags=re.S)  # a comment declares nothing
    for m in CSS_PAINT.finditer(sheet):
        _colours(m.group(2), palette)
    strokes |= set(re.findall(r"stroke-width\s*:\s*([^;}!]+)", sheet))
    norm = set()
    for s in strokes:
        try:
            norm.add(format(float(s.strip().removesuffix("px")), "g"))
        except ValueError:
            norm.add(_say(s.strip(), 20))  # the work's own text, shown in grid's and set's got
    css_anim = bool(re.search(r"@keyframes|(?<![\w-])animation(-name)?\s*:", sheet, re.I))
    infinite = re.search(r"(?<![\w-])animation(?:-iteration-count)?\s*:[^;}]{0,500}?(?<![\w-])infinite(?![\w-])",
                         sheet, re.I)
    loops = sum(smil) + bool(css_anim and infinite)  # ponytail: one CSS verdict for the sheet
    return {"shapes": shapes, "text": text, "palette": palette, "strokes": sorted(norm),
            "animated": len(smil) + css_anim, "loops": loops}


def _text(root):
    """The clean tree as SVG source with plain names (ElementTree's default_namespace refuses plain attributes)."""
    root = copy.deepcopy(root)
    for el in root.iter():
        el.tag = _local(el.tag)[0]  # only SVG elements are left
        for name in [n for n in el.attrib if n.startswith(f"{{{XLINK_NS}}}")]:
            el.set("xlink:" + _local(name)[0], el.attrib.pop(name))
    root.set("xmlns", SVG_NS)
    root.set("xmlns:xlink", XLINK_NS)
    return ET.tostring(root, encoding="unicode")


def inspect(data, kind=None):
    """{type, svg (the clean text, None when unparsed), gates, …}: every gate the source alone decides."""
    out = {"type": kind, "svg": None, "gates": []}

    def gate(name, ok, got, bar):
        out["gates"].append({"gate": name, "ok": ok, "got": got, "bar": bar})

    root, vb, why = _parse(data)
    if root is None:
        gate("xml", False, why, XML_BAR)
        return out
    w, h = vb[2], vb[3]
    kind = out["type"] = kind or ("icon" if w == h and w <= 64 else "card")
    cap, (lo, hi), colours, _ = TYPES[kind]
    gate("xml", True, f"{w:g}×{h:g} viewBox", XML_BAR)
    offences = _sanitise(root, data)
    gate("safe", not offences, _say("removed " + "; ".join(offences), 300) if offences else
         "no script, handler or outside reference", "bar nothing that runs or reaches out")
    gate("size", len(data) <= cap, f"{len(data)} bytes", f"bar ≤ {cap} bytes")
    f = _facts(root)
    gate("shapes", lo <= f["shapes"] <= hi, str(f["shapes"]), f"bar {lo}–{hi}")
    gate("palette", len(f["palette"]) <= colours, f"{len(f['palette'])} colours", f"bar ≤ {colours} colours")
    if kind == "icon":
        square = w == h and all(float(x).is_integer() for x in vb)
        say = f"{w:g}×{h:g} grid" if square else f"{w:g}×{h:g}, not a square whole-number grid"
        gate("grid", square and len(f["strokes"]) <= 1,
             _say(say + (f", stroke {' and '.join(f['strokes'])}" if f["strokes"] else ", no stroke"), 200),
             "bar a square whole-number viewBox, one stroke width")
    if f["animated"]:
        gate("loop", f["loops"] == f["animated"], f"{f['loops']} of {f['animated']} animations loop",
             "bar every animation indefinite or infinite")
    out.update(svg=_text(root), viewbox=vb, text=f["text"],
               strokes=f["strokes"], animated=bool(f["animated"]))
    return out


def set_gate(results):
    """An icon set is one viewBox and one stroke: the odd one out of three or more fails `set` (a tie decides nothing)."""
    icons = [r for r in results if r.get("type") == "icon" and r.get("viewbox")]
    if len(icons) < 3:
        return

    def major(values):
        top = collections.Counter(values).most_common(2)
        return top[0][0] if top and (len(top) == 1 or top[0][1] > top[1][1]) else None

    size = major((r["viewbox"][2], r["viewbox"][3]) for r in icons)
    stroke = major(tuple(r["strokes"]) for r in icons if r["strokes"])
    for r in icons:
        mine, wrong = (r["viewbox"][2], r["viewbox"][3]), []
        if size and mine != size:
            wrong.append(f"{mine[0]:g}×{mine[1]:g} where the set is {size[0]:g}×{size[1]:g}")
        if stroke and r["strokes"] and tuple(r["strokes"]) != stroke:
            wrong.append(f"stroke {' and '.join(r['strokes'])} where the set has {' and '.join(stroke)}")
        r["gates"].append({"gate": "set", "ok": not wrong, "got": _say("; ".join(wrong), 200) or "like the set",
                           "bar": f"bar the set's majority of {len(icons)} icons"})


def thumb(w, h, rgba):
    """A perceptual fingerprint: GRID×GRID grey means, stretched so the darkest pixel is 0 and the lightest 255 (a
    recolour moves nothing; a stroke moved or added moves whole cells, a hair's shift only a little)."""
    grey = [(299 * rgba[i] + 587 * rgba[i + 1] + 114 * rgba[i + 2]) / 1000 for i in range(0, w * h * 4, 4)]
    span = lambda n: [(i * n // GRID, max((i + 1) * n // GRID, i * n // GRID + 1)) for i in range(GRID)]  # noqa: E731
    xs, ys = span(w), span(h)
    rows = [[sum(grey[y * w + a:y * w + b]) for a, b in xs] for y in range(h)]
    cells = [sum(rows[y][c] for y in range(a, b)) / ((b - a) * (xs[c][1] - xs[c][0])) for a, b in ys
             for c in range(GRID)]
    lo, hi = min(grey), max(grey)
    return bytes(round(255 * (v - lo) / ((hi - lo) or 1)) for v in cells)


def distance(a, b):
    """How many cells of two thumbs differ by more than LEVEL."""
    return sum(abs(x - y) > LEVEL for x, y in zip(a, b))


def duplicates(items, prior=()):
    """{name: (the earlier work it repeats, cells)} for each (name, thumb) within BAR of one kept before it."""
    kept, dup = list(prior), {}
    for name, h in items:  # ponytail: O(n²) over the kept; a BK-tree or LSH bands when a store holds thousands
        twin = min(kept, key=lambda k: distance(h, k[1]), default=None)
        if twin is not None and distance(h, twin[1]) <= BAR:
            dup[name] = (twin[0], distance(h, twin[1]))
        else:
            kept.append((name, h))
    return dup


def _layout(work):
    """(vw, vh, px, py): the work's size in px (its longer side the type's width) and the margin around it."""
    _, _, w, h = work["viewbox"]
    s = TYPES[work["type"]][3] / max(w, h)
    vw, vh = max(1, round(w * s)), max(1, round(h * s))
    return vw, vh, round(vw * PAD), round(vh * PAD)


def _tile(work, still, hide_text, ink):
    """One render as an image: the work inside a margin, so what it paints past its viewBox shows."""
    vw, vh, px, py = _layout(work)
    root = ET.fromstring(work["svg"])
    drop = (SMIL if still else set()) | ({"text"} if hide_text else set())  # gone, not hidden: no rule brings it back
    for parent in list(root.iter()):
        for child in [c for c in parent if _local(c.tag)[0] in drop]:
            parent.remove(child)
    root.attrib.update(x=str(px), y=str(py), width=str(vw), height=str(vh), overflow="visible")
    css = ":root>svg{overflow:visible!important}" + (STATIC_CSS if still else "")
    tw, th = vw + 2 * px, vh + 2 * py
    return (f'<svg xmlns="{SVG_NS}" width="{tw}" height="{th}" viewBox="0 0 {tw} {th}" color="{ink}">'
            f'{_text(root)}<style>{css}</style></svg>')  # the harness's rules last: a tie goes to them


def _page(bg, tiles, th):
    import base64
    imgs = "".join(f'<img style="top:{i * th}px" src="data:image/svg+xml;base64,'
                   f'{base64.b64encode(t.encode()).decode()}">' for i, t in enumerate(tiles))
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" '
            f'content="{CSP}"><style>html,body{{margin:0;padding:0;overflow:hidden;background:{bg}}}'
            f'img{{position:absolute;left:0;display:block}}</style></head><body>{imgs}</body></html>')


def _load(b, url):
    from sampo import shot
    b.events.clear()
    nav = b.call("Page.navigate", b.sid, url=url)
    if nav.get("errorText"):
        raise RuntimeError(f"did not load: {nav['errorText']}")
    if not b.wait("Page.loadEventFired", shot.LOAD_S):
        raise TimeoutError(f"no load event in {shot.LOAD_S} s")


def _capture(b, top, tw, th):
    import base64
    clip = {"x": 0, "y": top, "width": tw, "height": th, "scale": 1}
    return base64.b64decode(b.call("Page.captureScreenshot", b.sid, format="png", clip=clip)["data"])


def _word(hexcolour):
    return int.from_bytes(bytes(int(hexcolour[i:i + 2], 16) for i in (1, 3, 5)) + b"\xff", sys.byteorder)


def _rgb(word):
    return tuple(v / 255 for v in word.to_bytes(4, sys.byteorder)[:3])


def _shade(x, bg, y, tol=0.06):
    """Whether colour x lies between bg and y: an antialiasing shade of y's text over bg, not a text colour of its own."""
    i = max(range(3), key=lambda k: abs(y[k] - bg[k]))
    if abs(y[i] - bg[i]) < 1e-9:
        return False
    t = (x[i] - bg[i]) / (y[i] - bg[i])
    return -tol < t < 1 - tol / 2 and all(abs(bg[k] + t * (y[k] - bg[k]) - x[k]) <= tol for k in range(3))


def _text_ratio(words, without):
    """Text's contrast: each pixel the text changed, as (with text, without), grouped by the background it covered.
    In each group a text colour with at least MIN_TEXT pixels counts unless it lies between the background and another
    of the group's colours (antialiasing); the lowest ratio of all (T-3226: a small badge or one faint line under SHARE
    of the text went unread). The commonest pair when no colour reaches MIN_TEXT; None when the text changed none."""
    from sampo import design
    pairs = collections.Counter((a, b) for a, b in zip(words, memoryview(without.rgba).cast("I")) if a != b
                                and max(abs(x - y) for x, y in zip(a.to_bytes(4, "little"), b.to_bytes(4, "little")))
                                > NOISE)
    groups = collections.defaultdict(list)
    for (a, b), n in pairs.items():
        groups[b].append((n, a))
    ratios = []
    for b, cols in groups.items():  # ponytail: the group's 64 commonest colours, compared pairwise
        bg = _rgb(b)
        top = [_rgb(a) for n, a in sorted(cols, reverse=True)[:64] if n >= MIN_TEXT]
        ratios += [design.contrast(x, bg) for i, x in enumerate(top)  # a hinting variant of a commoner one is that one
                   if not any(y is not x and _shade(x, bg, y) for y in top)
                   and not any(max(abs(x[k] - y[k]) for k in range(3)) <= NEAR for y in top[:i])]
    if not ratios and pairs:
        (a, b), _ = pairs.most_common(1)[0]
        ratios = [design.contrast(_rgb(a), _rgb(b))]
    return min(ratios, default=None)


def _measure(work, shots, bg):
    """One theme's numbers from its renders: painted share, pixels past the viewBox, the contrast, the centre."""
    from sampo import design
    vw, vh, px, py = _layout(work)
    p = shots["P"]
    tw, words = p.width, memoryview(p.rgba).cast("I").tolist()
    centre, outside = [], 0
    for y in range(p.height):
        row = words[y * tw:(y + 1) * tw]
        if py <= y < py + vh:
            centre += row[px:px + vw]
        if py - 1 <= y < py + vh + 1:
            row = row[:max(0, px - 1)] + row[px + vw + 1:]
        outside += len(row) - row.count(bg)
    ink = collections.Counter(v for v in centre if v != bg)
    share = 100 * sum(ink.values()) / len(centre)
    if "N" in shots:
        ratio = _text_ratio(words, shots["N"])
        ratio = 1.0 if ratio is None else ratio
    elif work["type"] == "icon":
        ratio = design.contrast(_rgb(ink.most_common(1)[0][0]), _rgb(bg)) if ink else None
    else:
        ratio = None
    rgba = b"".join(p.rgba[(y * tw + px) * 4:(y * tw + px + vw) * 4] for y in range(py, py + vh))
    return {"share": share, "outside": outside, "ratio": ratio, "centre": rgba}


def _render(b, work, n):
    """{theme: measures} plus the light page's distinct animation frames."""
    from sampo import img as simg
    vw, vh, px, py = _layout(work)
    tw, th = vw + 2 * px, vh + 2 * py
    got = {}
    for theme, (bg, ink) in THEMES.items():
        tiles = {"P": _tile(work, True, False, ink)}
        if work["text"]:
            tiles["N"] = _tile(work, True, True, ink)
        if work["animated"] and theme == "light":
            tiles["A"] = _tile(work, False, False, ink)
        page = os.path.join(b.profile.name, f"work-{n}-{theme}.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(_page(bg, list(tiles.values()), th))
        b.emulate((tw, th * len(tiles)), 1, theme)
        _load(b, pathlib.Path(page).as_uri())
        shots = {}
        for i, key in enumerate(tiles):
            if key == "A":
                frames = []
                for k in range(FRAMES):
                    if k:
                        time.sleep(FRAME_S)
                    frames.append(_capture(b, i * th, tw, th))
                got["distinct"] = len(set(frames))
            else:
                shots[key] = simg.decode(_capture(b, i * th, tw, th))
        got[theme] = _measure(work, shots, _word(bg))
    return got


def _judge(work, m, kept):
    """The rendered gates from a work's measures; a unique one that failed nothing joins `kept`."""
    def gate(name, ok, got, bar):
        work["gates"].append({"gate": name, "ok": ok, "got": got, "bar": bar})

    light, dark = m["light"], m["dark"]
    share = min(light["share"], dark["share"])
    gate("blank", share >= BLANK_PCT, f"{share:.1f}% painted", f"bar ≥ {BLANK_PCT}% painted")
    if work["type"] != "scene":
        out = max(light["outside"], dark["outside"])
        gate("clip", out == 0, f"{out} px outside", "bar 0 px outside the viewBox")
    bar = TEXT_BAR if work["text"] else INK_BAR
    ratios = [x["ratio"] for x in (light, dark)]
    if work["text"] or work["type"] == "icon":
        if None in ratios:
            gate("contrast", False, "no ink", f"bar {bar:g}:1")
        else:
            gate("contrast", min(ratios) >= bar, f"{min(ratios):.2f}:1", f"bar {bar:g}:1")
    else:
        gate("contrast", None, "no text", f"bar {TEXT_BAR:g}:1 for text")
    if work["animated"]:
        gate("motion", m["distinct"] >= 2, f"{m['distinct']} of {FRAMES} frames differ", "bar 2+ distinct")
    bar = f"bar > {BAR} of {GRID * GRID} cells off by > {LEVEL}/255 from every earlier work that passed"
    if share < BLANK_PCT:
        gate("unique", None, "blank: nothing to compare", bar)
        return
    if work["type"] == "figure":  # T-3247: figures share a plate and differ by their text, which a thumb can't see
        gate("unique", None, "a figure: told apart by its text", bar)
        return
    h = thumb(*_layout(work)[:2], light["centre"])
    work["thumb"] = h.hex()
    dup = duplicates([(work["path"], h)], kept)
    if dup:
        twin, d = dup[work["path"]]
        gate("unique", False, f"{d} cells from {_say(twin, 200)}", bar)
        return
    near = min((distance(h, k) for _, k in kept), default=None)
    gate("unique", True, "the first" if near is None else f"nearest {near} cells", bar)
    if not any(g["ok"] is False for g in work["gates"]):
        kept.append((work["path"], h))  # a work that failed is no original: its twin may still pass


def _unrendered(work, why=None):
    for name in ("blank", "clip", "contrast", "motion", "unique"):
        if (name != "clip" or work["type"] != "scene") and (name != "motion" or work["animated"]):
            work["gates"].append({"gate": name, "ok": None, "got": "not rendered", "bar": "bar a render"})
    work["error"] = why


def _save(out, work, m, taken):
    from sampo import img as simg
    stem = os.path.splitext(os.path.basename(work["path"]))[0] or "work"
    name, i = stem, 1
    while name in taken:
        i += 1
        name = f"{stem}-{i}"
    taken.add(name)
    vw, vh = _layout(work)[:2]
    for theme in THEMES:
        simg.save(os.path.join(out, f"{name}-{theme}.png"), vw, vh, m[theme]["centre"])


def _browser(path):
    """A browser holding none of our secrets that can reach nothing: every work it shows is untrusted."""
    from sampo import shot
    return shot.Browser(path, (640, 480), env={k: os.environ[k] for k in ENV_KEEP if k in os.environ}, args=NET_OFF)


FAILS = (OSError, RuntimeError, EOFError, ValueError, ArithmeticError)  # TimeoutError is an OSError


def _each(path, todo, do, fail):
    """do(browser, item, n) for each item, in one browser that holds no secrets. A FAILS error costs its item only:
    fail(item, why, error), error set when the browser hung or died, None when it never started or the item went
    untried. After a hang a fresh browser takes the next item, unless HANGS in a row leave the rest untried."""
    if not todo:
        return
    try:
        b = _browser(path)
    except FAILS as e:
        for x in todo:
            fail(x, f"the browser ({path}) did not start: {e}", None)
        return
    dead, hung = None, 0
    try:
        for n, x in enumerate(todo):
            if dead:
                fail(x, dead, None)
                continue
            try:
                do(b, x, n)
            except FAILS as e:
                hang = isinstance(e, (EOFError, TimeoutError))
                fail(x, _say(str(e), 300), e if hang else None)
                if not hang:
                    continue
                b.close()
                hung += 1
                if hung >= HANGS:
                    dead = f"not tried: the browser hung or died on {HANGS} in a row"
                    continue
                try:
                    b = _browser(path)
                except FAILS as e2:
                    dead = f"not tried: the browser did not restart: {_say(str(e2), 200)}"
                continue
            hung = 0
    finally:
        b.close()


def _hung(e):
    return {"gate": "render", "ok": False, "got": _say(f"{type(e).__name__}: {e}", 200),
            "bar": "bar a render within the browser's time"}


def _render_all(path, works, out, prior=()):
    """Render and judge each work; `prior` is [(name, thumb)] of works kept earlier (a store's), which `unique` compares
    each against too."""
    kept, taken = list(prior), set()

    def do(b, w, n):
        before = len(w["gates"])
        try:
            m = _render(b, w, n)
            _judge(w, m, kept)
        except FAILS:
            del w["gates"][before:]  # a judgement cut short counts for nothing
            raise
        w["rendered"] = True
        if out:
            try:
                _save(out, w, m, taken)
            except OSError as e:  # a full disk loses this work's pictures, not the batch's verdicts
                w["error"] = f"its renders were not saved: {_say(str(e), 200)}"

    def fail(w, why, hang):
        _unrendered(w, why)
        if hang:  # hung or killed the browser: this work fails
            w["gates"].append(_hung(hang))

    _each(path, [w for w in works if w["svg"]], do, fail)


# T-3102: a UI card's example, an HTML fragment shown at phone width
HTML_W, HTML_H, HTML_KB = 390, 800, 12
HTML_TAGS = frozenset("""div span p a button input label select option optgroup textarea form fieldset legend table
thead tbody tfoot tr th td caption colgroup col ul ol li dl dt dd nav header footer main section article aside h1 h2 h3 h4
h5 h6 strong em b i u s small code pre kbd mark abbr time hr br img figure figcaption blockquote sup sub del ins details
summary progress meter output style svg g path circle rect line polyline polygon ellipse defs lineargradient
radialgradient stop title text tspan""".split())
HTML_VOID = frozenset("input br hr img col".split())
HTML_GONE = frozenset("""script iframe object embed template noscript frame frameset applet base link meta head noembed
noframes xmp plaintext math foreignobject use image animate set animatemotion animatetransform portal""".split())
HTML_QUIET = frozenset(("html", "body"))
HTML_URLS = frozenset("""href src action formaction srcset xlink:href poster background ping data srcdoc codebase cite
longdesc manifest lowsrc dynsrc usemap""".split())
HTML_ATTRS = frozenset("""class id style title role type value name placeholder checked disabled selected readonly
required for alt width height colspan rowspan scope open min max step low high optimum lang dir tabindex hidden datetime
rows cols maxlength multiple label size autocomplete inputmode viewbox d x y x1 y1 x2 y2 cx cy r rx ry points fill stroke
stroke-width stroke-linecap stroke-linejoin stroke-dasharray stroke-dashoffset transform offset stop-color stop-opacity
opacity fill-opacity fill-rule clip-rule preserveaspectratio xmlns gradientunits focusable font-size font-weight
text-anchor dominant-baseline vector-effect""".split()) | HTML_URLS
HTML_FREE = frozenset("title value placeholder alt label".split())  # free text: not CSS


def sanitise_html(text):
    """(clean HTML, offences) of an untrusted fragment: re-serialised from Python's parse with only known elements and
    attributes, text escaped, no script or handler and no URL but "#..." (and data:image/ for an img). Anything
    stripped that could run or reach out is an offence; a work with any fails `safe`."""
    import html
    import html.parser

    out, offences, skip = [], [], []

    class P(html.parser.HTMLParser):
        def tag(self, name, attrs, close):
            if skip:
                if name == skip[0]:
                    skip.append(name)
                return
            if name in HTML_GONE:
                offences.append(f"{name} element")
                if not close and name not in HTML_VOID:
                    skip.append(name)
                return
            if name not in HTML_TAGS:
                if name not in HTML_QUIET:
                    offences.append(f"{_say(name, 40)} element (unknown)")
                return
            kept = []
            for k, v in attrs:
                v = v or ""
                if k.startswith("on"):
                    offences.append(f"{_say(k, 40)} handler")
                elif k in HTML_URLS:
                    ok = v.strip().startswith("#") and k in ("href", "xlink:href") or \
                        k == "src" and name == "img" and v.strip().lower().startswith("data:image/")
                    if ok:
                        kept.append((k, v))
                    else:
                        offences.append(f"{k} {_say(v, 60) or '(empty)'}")
                elif k in HTML_ATTRS or k.startswith(("aria-", "data-")):
                    bad = None if k in HTML_FREE or k.startswith("aria-") else _bad_css(v)
                    if bad:
                        offences.append(f"{k}: {bad}")
                    else:
                        kept.append((k, v))
            out.append(f"<{name}" + "".join(f' {k}="{html.escape(v)}"' for k, v in kept)
                       + ("/>" if close and name not in HTML_VOID else ">"))
            if name == "style":
                self.style = []

        def handle_starttag(self, name, attrs):
            self.tag(name, attrs, False)

        def handle_startendtag(self, name, attrs):
            self.tag(name, attrs, True)

        def handle_endtag(self, name):
            if skip:
                if name == skip[-1]:
                    skip.pop()
                return
            if name == "style" and getattr(self, "style", None) is not None:
                css, self.style = "".join(self.style), None
                why = "a < in it" if "<" in css else _bad_css(css)
                if why:  # the open tag goes too: an empty element is harmless, its rules are what reach out
                    offences.append(f"style element: {why}")
                    out.append("</style>")
                    return
                out.append(css)
            if name in HTML_TAGS and name not in HTML_VOID:
                out.append(f"</{name}>")

        def handle_data(self, data):
            if skip:
                return
            if getattr(self, "style", None) is not None:
                self.style.append(data)
            else:
                out.append(html.escape(data, quote=False))

    p = P(convert_charrefs=True)
    p.style = None
    p.feed(text)
    p.close()
    if getattr(p, "style", None) is not None:  # a style never closed
        p.handle_endtag("style")
    return "".join(out), offences


def html_doc(css, body, theme):
    """One page around an HTML fragment: nothing may load or run (the CSP), its theme on the root."""
    return (f'<!doctype html><html lang="en" data-theme="{theme}"><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{css}</style></head>'
            f'<body>{body}</body></html>')


HIDE_TEXT = ("*,*::before,*::after,::placeholder{-webkit-text-fill-color:transparent!important;"
             "text-shadow:none!important;text-decoration-color:transparent!important}")
SIZE_JS = ("(() => { const d = document.documentElement; return [d.scrollWidth, "
           "Math.ceil(document.body.getBoundingClientRect().bottom), Math.max(0, ...Array.from("
           "document.body.querySelectorAll('*'), e => Math.ceil(e.getBoundingClientRect().width)))]; })()")


def _style(b, css):
    expr = f"document.head.appendChild(Object.assign(document.createElement('style'), {{textContent: {css!r}}})) && 1"
    b.call("Runtime.evaluate", b.sid, expression=expr, returnByValue=True)


def _html_shots(b, doc, n, theme):
    """One theme of a page: (with text, without, scroll width, height, its widest box's width)."""
    from sampo import img as simg
    page = os.path.join(b.profile.name, f"html-{n}-{theme}.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(doc)
    b.emulate((HTML_W, 1), 1, theme)
    _load(b, pathlib.Path(page).as_uri())
    _style(b, STATIC_CSS + "*{caret-color:transparent!important}")
    got = b.call("Runtime.evaluate", b.sid, expression=SIZE_JS, returnByValue=True)["result"].get("value") or [0, 0, 0]
    sw, h, box = int(got[0]), max(1, int(got[1])), int(got[2])
    b.emulate((HTML_W, min(h, HTML_H)), 1, theme)
    shot = simg.decode(_capture(b, 0, HTML_W, min(h, HTML_H)))
    _style(b, HIDE_TEXT)
    return shot, simg.decode(_capture(b, 0, HTML_W, min(h, HTML_H))), sw, h, box


def _html_gates(shots):
    gates = []

    def gate(name, ok, got, bar):
        gates.append({"gate": name, "ok": ok, "got": got, "bar": bar})

    shares, ratios = [], []
    for theme, (with_, without, *_) in shots.items():
        words, bg = memoryview(with_.rgba).cast("I").tolist(), _word(THEMES[theme][0])
        shares.append(100 * (len(words) - words.count(bg)) / max(1, len(words)))
        ratios.append(_text_ratio(words, without))
    share = min(shares)
    wide, h, box = (max(s[i] for s in shots.values()) for i in (2, 3, 4))
    gate("blank", share >= BLANK_PCT, f"{share:.1f}% painted", f"bar ≥ {BLANK_PCT}% painted")
    gate("overflow", wide <= HTML_W, f"{wide} px wide, its widest box {box} px", f"bar ≤ {HTML_W} px")
    gate("height", h <= HTML_H, f"{h} px tall", f"bar ≤ {HTML_H} px")
    if None in ratios:
        gate("contrast", False, "no text", f"bar {TEXT_BAR:g}:1 in both themes")
    else:
        gate("contrast", min(ratios) >= TEXT_BAR, f"{min(ratios):.2f}:1", f"bar {TEXT_BAR:g}:1 in both themes")
    return gates, h


def check_html(path, docs):
    """Render each page ({theme: document}, untrusted HTML) HTML_W px wide, light and dark, in a browser that holds no
    secrets and reaches nothing: [{"gates", "h"}] in order. Gates: blank, overflow, height, contrast; a page that
    couldn't render has one failed render gate and an "error"."""
    res = [{"gates": [], "h": 0} for _ in docs]

    def do(b, i, n):
        res[i]["gates"], res[i]["h"] = _html_gates({t: _html_shots(b, docs[i][t], n, t) for t in THEMES})

    def fail(i, why, hang):
        res[i].update(gates=[_hung(hang) if hang else {"gate": "render", "ok": False, "got": _say(why, 200),
                                                       "bar": "bar a render"}], error=why)

    _each(path, list(range(len(docs))), do, fail)
    return res


def _read(path):
    """The file's first MAX_READ+1 bytes; a FIFO or device is refused, not waited on."""
    with open(path, "rb", opener=lambda p, fl: os.open(p, fl | os.O_NONBLOCK)) as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError("not a regular file")
        return f.read(MAX_READ + 1)


def cmd_works(args):
    from sampo import cli, shot, worksgrow
    if getattr(args, "action", "check") == "grow":
        if args.files:
            raise cli.UsageError("works grow takes no FILE: it writes its own (see --grid, --target)")
        code = worksgrow.cmd_grow(args, cli.resolve(args))
        if code:
            sys.exit(code)
        return
    if not args.files:
        raise cli.UsageError("works check needs FILE...")
    out = None
    if args.out:
        out = os.path.abspath(args.out)
        if os.path.islink(out):
            raise cli.UsageError(f"--out {shot.safe(out)} is a link: name a real folder")
        if os.path.exists(out) and not os.path.isdir(out):
            raise cli.UsageError(f"--out {shot.safe(out)} isn't a folder")
    works = []
    for path in args.files:
        try:
            w = inspect(_read(path), args.type)
        except Exception as e:  # one file's crash fails that file's xml gate, never the batch
            got = f"unreadable: {e.strerror or e}" if isinstance(e, OSError) else \
                f"not checked: {type(e).__name__}: {e}"
            w = {"type": args.type, "svg": None, "gates": [{"gate": "xml", "ok": False, "got": _say(got, 200),
                                                            "bar": XML_BAR}]}
        w["path"] = path
        works.append(w)
    set_gate(works)
    browser, why = shot.find_browser("sampo works")
    if browser:
        if out:
            os.makedirs(out, exist_ok=True)
        with shot.killable():
            _render_all(browser, works, out)
    else:
        for w in works:
            if w["svg"]:
                _unrendered(w)
    rows, passed = [], 0
    for w in works:
        w["passed"] = bool(w.get("rendered")) and not any(g["ok"] is False for g in w["gates"])
        passed += w["passed"]
        bad = [g["gate"] for g in w["gates"] if g["ok"] is False]
        head = "✓ passed" if w["passed"] else f"✗ failed: {', '.join(bad)}" if bad else "- not rendered"
        rows.append(f"{shot.safe(w['path'])} ({w['type'] or '?'}): {head}")
        if w.get("error"):
            rows.append(f"  {_say(w['error'], 300)}")
        rows += [f"  {'✓' if g['ok'] else '-' if g['ok'] is None else '✗'} {g['gate']}: {g['got']} ({g['bar']})"
                 for g in w["gates"]]
    if not browser:
        rows.insert(0, f"sampo works: not rendered: {why}.")
    rows.append(f"{passed} of {len(works)} works passed")
    data = {"works": [{k: w.get(k) for k in ("path", "type", "gates", "passed")} for w in works],
            "passed": passed, "total": len(works), "browser": browser and why, "skipped": None if browser else why}
    cli.out(args, data, "\n".join(rows))
    if passed < len(works):
        sys.exit(1)
