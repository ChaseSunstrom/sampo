"""T-3734: sampo panels render SPEC --term [--width N]: the panels drawn in this terminal. Each panel's own svg (the
kit's, so the terminal shows what the files show) is rasterized onto a pixel canvas two pixels to a cell: fills become
true-colour half blocks (▀, the top pixel the ink, the bottom the paper), lines and dots thinner than a pixel become
braille (2x4 dots a cell), and every word is set as real text, never drawn. 79 columns hold one panel as it reads at
80; a wider terminal sets panels side by side. Colour follows the terminal (smithterm.colour_depth): 24-bit, 256, 8, or
none (NO_COLOR), where the shapes still draw as ▀ ▄ █ and braille.

The rasterizer reads the svg subset the kit writes (svg, g, rect, circle, ellipse, path M L H V C Q Z, line, polyline,
polygon, text, tspan; translate; hex colours) and fails loudly, naming it, on anything else."""
import math
import re
import shutil
import unicodedata
import xml.etree.ElementTree as ET
from functools import lru_cache

MIN_W, MAX_W, PANE = 20, 400, 79
MAX_ROWS = 2000
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_PATH_TOK = re.compile(rf"[A-Za-z]|{_NUM}")
_TRANSLATE = re.compile(rf"\s*translate\(\s*({_NUM})(?:[\s,]+({_NUM}))?\s*\)\s*$")
_HEX = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
SKIP = {"title", "desc", "metadata"}
SHAPES = {"rect", "circle", "ellipse", "path", "line", "polyline", "polygon"}
REFUSE = {"style", "clip-path", "mask", "filter", "marker-start", "marker-mid", "marker-end", "href"}
BITS = {(0, 0): 1, (0, 1): 2, (0, 2): 4, (1, 0): 8, (1, 1): 0x10, (1, 2): 0x20, (0, 3): 0x40, (1, 3): 0x80}
RESET = "\x1b[0m"


def _clamp(w):
    return max(MIN_W, min(MAX_W, int(w)))


def render(root, spec_path, style=None, width=None):
    """{width, depth, source, text}: the spec's panels drawn for this terminal (its width unless one is given)."""
    from sampo import panels, smithterm
    spec = panels.load(spec_path)
    th, source = panels.themes(root, style or spec.get("style") or "board")
    w = _clamp(width or shutil.get_terminal_size((80, 24)).columns)
    depth = smithterm.colour_depth()
    return {"width": w, "depth": depth, "source": source, "text": draw(spec, th["dark"], w, depth)}


def draw(spec, theme, width, depth):
    """The panels as lines of exactly `width` cells (clamped to 20..400): one panel per 79 columns, side by side, a
    blank line between rows of panels."""
    from sampo import panels
    k, width = panels.kit(), _clamp(width)
    per = max(1, (width + 1) // PANE)
    pane = (width - (per - 1)) // per
    blocks = [raster(k.panel_svg(p, theme, i, f"t{i}-"), pane, depth, theme.panel)
              for i, p in enumerate(spec["panels"], 1)]
    out = []
    for g in range(0, len(blocks), per):
        if out:
            out.append(" " * width)
        group = blocks[g:g + per]
        for r in range(max(len(b) for b in group)):
            segs = [b[r] if r < len(b) else " " * pane for b in group] + [" " * pane] * (per - len(group))
            line = " ".join(segs) + " " * (width - per * pane - (per - 1))
            out.append(line + RESET if "\x1b" in line and not line.endswith(RESET) else line)
    return "\n".join(out)


# ---------------------------------------------------------------- reading the svg

def _tag(el):
    return el.tag.rsplit("}", 1)[-1]


def _rgb(v):
    m = _HEX.match(v.strip())
    if not m:
        raise ValueError(f"the colour {v!r} can't be drawn in a terminal: #rgb or #rrggbb only")
    h = m.group(1)
    h = "".join(ch * 2 for ch in h) if len(h) == 3 else h
    return tuple(float(int(h[i:i + 2], 16)) for i in (0, 2, 4))


def _paint(v):
    return None if v.strip() == "none" else _rgb(v)


def _num(el, name, default=0.0):
    v = el.get(name)
    if v is None:
        return default
    try:
        return float(v.strip().removesuffix("px"))
    except ValueError:
        raise ValueError(f"<{_tag(el)} {name}={v!r}>: a plain number only") from None


def _ctx(el, up):
    """The drawing state an element sees: what it sets over what it inherits (opacity multiplies, like a group's)."""
    ctx = dict(up)
    for k in el.attrib:
        if k.rsplit("}", 1)[-1] in REFUSE:
            raise ValueError(f"<{_tag(el)}> has {k.rsplit('}', 1)[-1]}=, which a terminal drawing can't honour")
    if el.get("transform") is not None:
        m = _TRANSLATE.match(el.get("transform"))
        if not m:
            raise ValueError(f"the transform {el.get('transform')!r} can't be drawn in a terminal: translate only")
        ctx["tx"] += float(m.group(1))
        ctx["ty"] += float(m.group(2) or 0)
    for name, key in (("fill", "fill"), ("stroke", "stroke")):
        if el.get(name) is not None:
            ctx[key] = _paint(el.get(name))
    for name, key in (("stroke-width", "sw"), ("font-size", "fs")):
        if el.get(name) is not None:
            ctx[key] = _num(el, name)
    for name, key in (("opacity", "op"), ("fill-opacity", "fop"), ("stroke-opacity", "sop")):
        if el.get(name) is not None:
            ctx[key] = max(0.0, min(1.0, _num(el, name))) * (ctx[key] if key == "op" else 1.0)
    if el.get("font-weight") is not None:
        w = el.get("font-weight").strip()
        ctx["bold"] = w == "bold" or w.isdigit() and int(w) >= 600
    if el.get("text-anchor") is not None:
        ctx["anchor"] = el.get("text-anchor").strip()
    if el.get("stroke-dasharray") is not None:
        v = el.get("stroke-dasharray").strip()
        try:
            dash = [abs(float(x)) for x in v.replace(",", " ").split()] if v != "none" else []
        except ValueError:
            raise ValueError(f"stroke-dasharray={v!r}: numbers only") from None
        ctx["dash"] = dash * (2 if len(dash) % 2 else 1) if sum(dash) > 0 else None
    return ctx


def _path(d, s):
    """[(points, closed)] in user units; curves flattened finely enough for the scale s."""
    toks, subs, i = _PATH_TOK.findall(d), [], 0
    x = y = sx = sy = 0.0
    cur, cmd = None, None

    def num():
        nonlocal i
        if i >= len(toks) or toks[i].isalpha():
            raise ValueError(f"the path {d[:40]!r} is missing a number")
        i += 1
        return float(toks[i - 1])

    def add(p):
        nonlocal cur
        if cur is None or cur[1]:
            cur = [[(x, y)], False]
            subs.append(cur)
        cur[0].append(p)

    def steps(*pts):
        n = sum(math.dist(a, b) for a, b in zip(pts, pts[1:])) * s
        return max(2, min(48, math.ceil(n)))

    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            if t not in "MmLlHhVvCcQqZz":
                raise ValueError(f"the path command {t} can't be drawn in a terminal: M L H V C Q Z only")
            cmd, i = t, i + 1
            if t in "Zz":
                if cur is not None:
                    cur[1] = True
                x, y = sx, sy
                continue
        elif cmd is None or cmd in "Zz":
            raise ValueError(f"the path {d[:40]!r} has a number where a command belongs")
        ox, oy = (x, y) if cmd.islower() else (0.0, 0.0)
        C = cmd.upper()
        if C == "M":
            x, y = num() + ox, num() + oy
            cur = [[(x, y)], False]
            subs.append(cur)
            sx, sy = x, y
            cmd = "l" if cmd == "m" else "L"
        elif C == "L":
            p = (num() + ox, num() + oy)
            add(p)
            x, y = p
        elif C == "H":
            p = (num() + ox, y)
            add(p)
            x, y = p
        elif C == "V":
            p = (x, num() + oy)
            add(p)
            x, y = p
        elif C == "C":
            p0, p1, p2, p3 = (x, y), (num() + ox, num() + oy), (num() + ox, num() + oy), (num() + ox, num() + oy)
            n = steps(p0, p1, p2, p3)
            for k in range(1, n + 1):
                t_, u = k / n, 1 - k / n
                add(tuple(u ** 3 * a + 3 * u * u * t_ * b + 3 * u * t_ * t_ * c + t_ ** 3 * e
                          for a, b, c, e in zip(p0, p1, p2, p3)))
            x, y = p3
        elif C == "Q":
            p0, p1, p2 = (x, y), (num() + ox, num() + oy), (num() + ox, num() + oy)
            n = steps(p0, p1, p2)
            for k in range(1, n + 1):
                t_, u = k / n, 1 - k / n
                add(tuple(u * u * a + 2 * u * t_ * b + t_ * t_ * c for a, b, c in zip(p0, p1, p2)))
            x, y = p2
    return [(pts, closed) for pts, closed in subs]


def _ring(cx, cy, rx, ry, a0=0.0, a1=2 * math.pi, n=None):
    n = n or max(12, min(256, math.ceil(2 * math.pi * max(rx, ry) * 1.5)))
    return [(cx + rx * math.cos(a0 + (a1 - a0) * k / n), cy + ry * math.sin(a0 + (a1 - a0) * k / n)) for k in range(n + 1)]


def _rrect_outline(x, y, w, h, rx, ry):
    if rx <= 0 or ry <= 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    q = math.pi / 2
    pts = []
    for cx, cy, a in ((x + w - rx, y + ry, -q), (x + w - rx, y + h - ry, 0), (x + rx, y + h - ry, q), (x + rx, y + ry, 2 * q)):
        pts += _ring(cx, cy, rx, ry, a, a + q, 6)
    return pts


def _poly_spans(polys):
    edges = [(a, b) for pts in polys for a, b in zip(pts, pts[1:] + pts[:1]) if a[1] != b[1]]

    def spans(y):
        xs = sorted(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]) for a, b in edges
                    if (a[1] <= y < b[1]) or (b[1] <= y < a[1]))
        return list(zip(xs[::2], xs[1::2]))
    return spans


# ---------------------------------------------------------------- the canvas

class Canvas:
    """Pixels (cols x 2 rows), a braille dot layer (2 cols x 4 rows: the same grid as the 2x2 supersamples) and text."""

    def __init__(self, cols, rows, bg):
        self.cols, self.rows, self.bg = cols, rows, bg
        self.px = [[bg] * cols for _ in range(rows * 2)]
        self.dots, self.text = {}, {}

    def cover(self, subs, rgb, a):
        """Blend rgb at alpha a over each pixel by the share of its four subsamples in subs; an opaque fill hides the
        braille dots under it."""
        if not subs or rgb is None or a <= 0:
            return
        count = {}
        for i, j in subs:
            count[(i >> 1, j >> 1)] = count.get((i >> 1, j >> 1), 0) + 1
        for (x, y), n in count.items():
            k = a * n / 4
            p = self.px[y][x]
            self.px[y][x] = (p[0] + (rgb[0] - p[0]) * k, p[1] + (rgb[1] - p[1]) * k, p[2] + (rgb[2] - p[2]) * k)
        if a >= .5:
            for q in subs:
                self.dots.pop(q, None)

    def fill(self, spans, y0, y1, rgb, a):
        if rgb is None or a <= 0:
            return
        subs, W = set(), self.cols * 2
        for j in range(max(0, math.floor(2 * y0)), min(self.rows * 4, math.ceil(2 * y1) + 1)):
            for xa, xb in spans((j + .5) / 2):
                for i in range(max(0, math.ceil(2 * xa - .5)), min(W, math.ceil(2 * xb - .5))):
                    subs.add((i, j))
        self.cover(subs, rgb, a)

    def _walk(self, lines, dash, step):
        """(x, y) every `step` pixels along the polylines, the dash pattern's gaps left out."""
        total = sum(dash) if dash else 0
        for pts, closed in lines:
            seq = pts + [pts[0]] if closed and len(pts) > 1 else pts
            if len(seq) == 1:
                seq = seq * 2
            run = 0.0
            for a, b in zip(seq, seq[1:]):
                L = math.dist(a, b)
                n = max(1, min(20000, math.ceil(L / step)))
                for k in range(n + 1):
                    d = run + L * k / n
                    if total:
                        m, on = d % total, True
                        for seg in dash:
                            if m < seg:
                                break
                            m -= seg
                            on = not on
                        if not on:
                            continue
                    yield a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n
                run += L

    def stroke(self, lines, width, dash, rgb, a):
        """A line a pixel or wider is painted into the pixels; a thinner one is set in braille dots."""
        if rgb is None or a <= 0 or width <= 0:
            return
        W, H = self.cols * 2, self.rows * 4
        if width < 1:
            for x, y in self._walk(lines, dash, .25):
                i, j = math.floor(2 * x), math.floor(2 * y)
                if 0 <= i < W and 0 <= j < H:
                    self.dots[(i, j)] = (rgb, a)
            return
        R, subs = width, set()  # the radius in subsamples: half the width, two subsamples a pixel
        for x, y in self._walk(lines, dash, .25):
            cx, cy = 2 * x, 2 * y
            for j in range(max(0, math.floor(cy - R)), min(H, math.ceil(cy + R) + 1)):
                for i in range(max(0, math.floor(cx - R)), min(W, math.ceil(cx + R) + 1)):
                    if (i + .5 - cx) ** 2 + (j + .5 - cy) ** 2 <= R * R:
                        subs.add((i, j))
        self.cover(subs, rgb, a)

    def dot_box(self, x, y, w, h, rgb, a):
        """A fill thinner than a pixel (a hairline bar, a pinpoint): the braille dots it covers, at least one row."""
        if rgb is None or a <= 0:
            return
        x0, x1, y0, y1 = 2 * x, 2 * (x + w), 2 * y, 2 * (y + h)
        if x1 - x0 < 1:
            x0, x1 = (x0 + x1) / 2 - .5, (x0 + x1) / 2 + .5
        if y1 - y0 < 1:
            y0, y1 = (y0 + y1) / 2 - .5, (y0 + y1) / 2 + .5
        for j in range(max(0, math.floor(y0)), min(self.rows * 4, math.ceil(y1))):
            for i in range(max(0, math.floor(x0)), min(self.cols * 2, math.ceil(x1))):
                self.dots[(i, j)] = (rgb, a)

    def put_text(self, x, y, cells, anchor):
        """Set the words in the cells at (x, y) (pixels, y the glyphs' middle). Text never overprints text: a run that
        meets earlier words or the edge stops there and ends in …."""
        row = math.floor(y / 2)
        if not cells or not 0 <= row < self.rows:
            return
        n = sum(c[4] for c in cells)
        col = round(x) if anchor not in ("middle", "end") else round(x - n / 2) if anchor == "middle" else round(x) - n
        col, done, cut = max(0, col), [], False
        for ch, rgb, bold, a, w in cells:
            if col + w > self.cols or any((row, col + k) in self.text for k in range(w)):
                cut = True
                break
            self.text[(row, col)] = (ch, rgb, bold, a)
            if w == 2:
                self.text[(row, col + 1)] = ("", rgb, bold, a)
            done.append((col, w))
            col += w
        if cut and done:
            c0, w = done[-1]
            self.text[(row, c0)] = ("…",) + self.text[(row, c0)][1:]
            if w == 2:
                self.text[(row, c0 + 1)] = (" ",) + self.text[(row, c0 + 1)][1:]

    def lines(self, depth, paper):
        out = []
        for r in range(self.rows):
            top_row, bot_row = self.px[2 * r], self.px[2 * r + 1]
            parts, last = [], None
            for col in range(self.cols):
                top, bot = top_row[col], bot_row[col]
                back = _mean((top, bot))
                t, bold = self.text.get((r, col)), False
                if t:
                    ch, fg, bold = t[0], _mix(back, t[1], t[3]), t[2]
                    if ch == "":
                        continue
                else:
                    bits, ink = 0, []
                    for (u, v), b in BITS.items():
                        d = self.dots.get((2 * col + u, 4 * r + v))
                        if d:
                            bits |= b
                            ink.append(d)
                    if bits:
                        ch = chr(0x2800 + bits)
                        fg = _mix(back, _mean([d[0] for d in ink]), .35 + .65 * sum(d[1] for d in ink) / len(ink))
                    elif depth == 0:
                        a, b = _ink(top, paper), _ink(bot, paper)
                        ch, fg = " ▄▀█"[a * 2 + b], None
                    elif _hex(top) == _hex(bot):
                        ch, fg, back = " ", None, top
                    else:
                        ch, fg, back = "▀", top, bot
                if depth:
                    fgc = _code(_hex(fg), depth, 3) if fg is not None else (last[0] if last else "")
                    key = (fgc, _code(_hex(back), depth, 4), bold)
                    if key != last:
                        parts.append(f"\x1b[0{';1' if bold else ''}{key[0]}{key[1]}m")
                        last = key
                parts.append(ch)
            out.append("".join(parts) + (RESET if depth else ""))
        return out


def _mean(cs):
    return tuple(sum(c[k] for c in cs) / len(cs) for k in range(3))


def _mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def _hex(c):
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(v))) for v in c)


def _luma(c):
    return (.2126 * c[0] + .7152 * c[1] + .0722 * c[2]) / 255


def _ink(c, paper):
    return int(abs(_luma(c) - _luma(paper)) > .12)


@lru_cache(maxsize=4096)
def _code(h, depth, ground):
    from sampo import smithterm
    return smithterm._code(h, depth, ground)


# ---------------------------------------------------------------- svg → canvas

def raster(svg_text, cols, depth, bg):
    """The svg drawn `cols` cells wide: a list of lines, each exactly cols cells. ValueError names what it can't draw."""
    if "<!DOCTYPE" in svg_text or "<!ENTITY" in svg_text:
        raise ValueError("a DOCTYPE or ENTITY can't be drawn in a terminal: the kit's svg declares none")
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as e:
        raise ValueError(f"not well-formed svg: {e}") from None
    if _tag(root) != "svg":
        raise ValueError(f"<{_tag(root)}> is not an svg")
    vb = (root.get("viewBox") or "").replace(",", " ").split()
    try:
        mx, my, W, H = map(float, vb) if len(vb) == 4 else (0.0, 0.0, _num(root, "width"), _num(root, "height"))
    except ValueError:
        raise ValueError(f"viewBox={root.get('viewBox')!r}: four numbers") from None
    if not (W > 0 and H > 0 and math.isfinite(W) and math.isfinite(H)):
        raise ValueError("the svg has no size: a viewBox or a width and height")
    cols = max(1, min(MAX_W, int(cols)))
    s = cols / W
    rows = max(1, math.ceil(H * s / 2 - 1e-9))
    if rows > MAX_ROWS:
        raise ValueError(f"the svg is {rows} rows tall at {cols} columns: {MAX_ROWS} at most")
    paper = _rgb(bg)
    cv = Canvas(cols, rows, paper)
    ctx = {"tx": -mx, "ty": -my, "fill": (0.0, 0.0, 0.0), "stroke": None, "sw": 1.0, "fs": 16.0, "op": 1.0, "fop": 1.0,
           "sop": 1.0, "bold": False, "anchor": "start", "dash": None}
    _children(root, _ctx(root, ctx) | {"tx": -mx, "ty": -my}, cv, s)
    return cv.lines(depth, paper)


def _children(el, ctx, cv, s):
    for ch in el:
        tag = _tag(ch)
        if tag in SKIP:
            continue
        if tag == "g":
            _children(ch, _ctx(ch, ctx), cv, s)
        elif tag in SHAPES:
            _shape(tag, ch, _ctx(ch, ctx), cv, s)
        elif tag == "text":
            _text(ch, _ctx(ch, ctx), cv, s)
        else:
            raise ValueError(f"<{tag}> can't be drawn in a terminal")


def _points(el):
    v = (el.get("points") or "").replace(",", " ").split()
    try:
        nums = [float(x) for x in v]
    except ValueError:
        raise ValueError(f"<{_tag(el)} points=…>: numbers only") from None
    return list(zip(nums[::2], nums[1::2]))


def _shape(tag, el, ctx, cv, s):
    tx, ty = ctx["tx"], ctx["ty"]
    P = lambda p: ((p[0] + tx) * s, (p[1] + ty) * s)  # noqa: E731
    fill, stroke = ctx["fill"], ctx["stroke"]
    fa, sa, sw = ctx["op"] * ctx["fop"], ctx["op"] * ctx["sop"], ctx["sw"] * s
    dash = [d * s for d in ctx["dash"]] if ctx["dash"] else None
    if tag == "rect":
        x, y, w, h = (_num(el, k) for k in ("x", "y", "width", "height"))
        if w <= 0 or h <= 0:
            return
        rx, ry = _num(el, "rx", None), _num(el, "ry", None)
        rx = ry if rx is None else rx
        ry = rx if ry is None else ry
        rx, ry = min(max(rx or 0, 0), w / 2) * s, min(max(ry or 0, 0), h / 2) * s
        (x, y), w, h = P((x, y)), w * s, h * s
        if w < 1 or h < 1:
            cv.dot_box(x, y, w, h, fill or stroke, fa if fill else sa)
            return

        def spans(yy):
            if yy < y or yy >= y + h:
                return []
            e = max(y + ry - yy, yy - (y + h - ry), 0)
            inset = rx * (1 - math.sqrt(max(0.0, 1 - (e / ry) ** 2))) if e and ry else 0
            return [(x + inset, x + w - inset)]
        cv.fill(spans, y, y + h, fill, fa)
        if stroke:
            cv.stroke([(_rrect_outline(x, y, w, h, rx, ry), True)], sw, dash, stroke, sa)
        return
    if tag in ("circle", "ellipse"):
        cx, cy = P((_num(el, "cx"), _num(el, "cy")))
        if tag == "circle":
            rx = ry = _num(el, "r") * s
        else:
            rx, ry = _num(el, "rx") * s, _num(el, "ry") * s
        if rx <= 0 or ry <= 0:
            return
        if max(rx, ry) < .75:
            cv.dot_box(cx - rx, cy - ry, 2 * rx, 2 * ry, fill or stroke, fa if fill else sa)
            return
        cv.fill(lambda yy: [] if abs(yy - cy) >= ry else
                [(cx - rx * math.sqrt(1 - ((yy - cy) / ry) ** 2), cx + rx * math.sqrt(1 - ((yy - cy) / ry) ** 2))],
                cy - ry, cy + ry, fill, fa)
        if stroke:
            cv.stroke([(_ring(cx, cy, rx, ry), True)], sw, dash, stroke, sa)
        return
    if tag == "path":
        subs = [([P(p) for p in pts], closed) for pts, closed in _path(el.get("d") or "", s)]
    elif tag == "line":
        subs = [([P((_num(el, "x1"), _num(el, "y1"))), P((_num(el, "x2"), _num(el, "y2")))], False)]
    else:
        subs = [([P(p) for p in _points(el)], tag == "polygon")]
    subs = [(pts, closed) for pts, closed in subs if pts]
    polys = [pts for pts, _ in subs if len(pts) > 2]
    if fill and polys and tag != "line":
        ys = [p[1] for pts in polys for p in pts]
        cv.fill(_poly_spans(polys), min(ys), max(ys), fill, fa)
    if stroke:
        cv.stroke(subs, sw, dash, stroke, sa)


def _cells(runs):
    """(char, colour, bold, alpha, width) per character: whitespace collapsed, controls shown as ?, wide characters
    two cells, combining marks dropped (a terminal can't be trusted to set them in one)."""
    out = []
    for text, rgb, bold, a in runs:
        for ch in text:
            if ch.isspace():
                if out and out[-1][0] != " ":
                    out.append((" ", rgb, bold, a, 1))
                elif not out:
                    out.append((" ", rgb, bold, a, 1))
                continue
            cat = unicodedata.category(ch)
            if cat in ("Mn", "Me") or ch == "‍":
                continue
            if cat[0] == "C":
                ch = "?"
            out.append((ch, rgb, bold, a, 2 if unicodedata.east_asian_width(ch) in "WF" else 1))
    while out and out[0][0] == " ":
        out.pop(0)
    while out and out[-1][0] == " ":
        out.pop()
    return out


def _text(el, ctx, cv, s):
    runs = [(el.text or "", ctx["fill"], ctx["bold"], ctx["op"] * ctx["fop"])]
    for ch in el:
        if _tag(ch) != "tspan":
            raise ValueError(f"<{_tag(ch)}> inside <text> can't be drawn in a terminal")
        for k in ("x", "y", "dx", "dy", "rotate"):
            if ch.get(k) is not None:
                raise ValueError(f"<tspan {k}=…> can't be drawn in a terminal: tspans flow inline")
        sub = _ctx(ch, ctx)
        runs.append((ch.text or "", sub["fill"], sub["bold"], sub["op"] * sub["fop"]))
        runs.append((ch.tail or "", ctx["fill"], ctx["bold"], ctx["op"] * ctx["fop"]))
    cells = [c for c in _cells(runs) if c[1] is not None]
    x, y = (_num(el, "x") + ctx["tx"]) * s, (_num(el, "y") - .35 * ctx["fs"] + ctx["ty"]) * s
    cv.put_text(x, y, cells, ctx["anchor"])
