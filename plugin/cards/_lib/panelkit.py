"""T-3662: the panel kit. A typed spec of panels drawn as static SVG, each a picture of a mechanism (a funnel through a
gate, an orbit, tanks filling) in the Board's chrome: a numbered chip, an UPPERCASE title, corner brackets, a footer
stat. Stdlib only, no sampo imports, so a confined calling card (gen.py) and `sampo panels` run the same code.

A kind is one draw function plus its input schema and sample data, registered with @kind: a new kind is one function.
Every draw function takes (data, W, H, T): the content box in px (never under MIN_W x MIN_H after the chrome) and the
theme, and lays out to whatever size it is given. Drawing is deterministic: the same spec gives the same bytes."""
import difflib
import html
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
import zlib

FONT = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
GLYPH = 0.62  # a monospace glyph's advance, of its size; wide errs safe, a label is cut with an ellipsis instead
MIN_W, MIN_H, MAX_W, MAX_H, DEF_W, DEF_H = 420, 260, 2400, 1600, 640, 400
PAD, HEAD, FOOT = 14, 34, 24
SZ = 11  # the one text size: the smallest a reader needs
STYLES_NAMES = ("board", "blueprint", "paper")

# ---------------------------------------------------------------- colour and styles

STYLES = {  # bg, panel, line, ink, muted, six accents (a hue each; the chip and bracket colours cycle through them)
    "board": {"dark": ("#0A0D13", "#0F141D", "#253043", "#E6EAF2", "#8D97AB",
                       ("#22D3EE", "#F5C542", "#FF4FA0", "#3DDC84", "#9D8CFF", "#4FA3FF")),
              "light": ("#F3F5F9", "#FFFFFF", "#D3DAE6", "#0E1420", "#566176",
                        ("#0A7189", "#8A6100", "#C2186B", "#127544", "#5B47D1", "#1D63C4"))},
    "blueprint": {"dark": ("#0A2342", "#0D2D52", "#2C5C8F", "#DCEBFA", "#8FB3D9",
                           ("#6FD3FF", "#FFE066", "#FF9F68", "#8DE4AF", "#C3B1E1", "#9BE7FF")),
                  "light": ("#EAF3FB", "#F8FBFE", "#A9C4DE", "#0A2342", "#3E5F82",
                            ("#0B5C8A", "#8A6100", "#B4440E", "#127544", "#5B47D1", "#0E7490"))},
    "paper": {"dark": ("#1E1A16", "#27221C", "#4A4136", "#F1E8DA", "#B5A790",
                       ("#E8806F", "#7FB0E0", "#A9C96B", "#E3B45B", "#B48BD6", "#6FC7C0")),
              "light": ("#FBF7EF", "#FFFFFF", "#DDD2BF", "#2B2118", "#6B5D4A",
                        ("#B33A2A", "#2F5D8A", "#4F6E1F", "#8A5A00", "#6B3F8C", "#1F6F6A"))},
}


def _rgb(h):
    h = h.lstrip("#")
    h = "".join(ch * 2 for ch in h) if len(h) == 3 else h
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lum(h):
    f = lambda v: v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4  # noqa: E731
    r, g, b = (f(v / 255) for v in _rgb(h))
    return .2126 * r + .7152 * g + .0722 * b


def contrast(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + .05) / (lo + .05)


def mix(a, b, t):
    return "#%02X%02X%02X" % tuple(round(p + (q - p) * t) for p, q in zip(_rgb(a), _rgb(b)))


def readable(fg, bgs, toward, need):
    """fg moved toward `toward` until it reads at `need`:1 on every one of bgs."""
    for i in range(21):
        col = mix(fg, toward, i / 20)
        if all(contrast(col, b) >= need for b in bgs):
            return col
    return toward


class Th:
    """A theme: bg panel line ink muted and accents, every stroke 3:1 and every text colour 4.6:1 on its ground."""

    def __init__(self, bg, panel, line, ink, muted, accents, name="board", scheme="dark"):
        self.name, self.scheme = name, scheme
        self.bg, self.panel, self.line, self.ink = bg, panel, line, ink
        self.muted = readable(muted, (bg, panel), ink, 4.6)
        self.accents = [readable(a, (bg, panel), ink, 3.0) for a in accents] or [mix(ink, bg, .3)]

    def a(self, i):
        return self.accents[i % len(self.accents)]

    def txt(self, i):
        """Accent i as text: moved toward the ink until it reads at 4.6:1 on the bg and the panel."""
        return readable(self.a(i), (self.bg, self.panel), self.ink, 4.6)

    def on(self, fill):
        """The colour for text over a fill: bg or ink, whichever reads better."""
        return max((self.bg, self.ink), key=lambda x: contrast(x, fill))


def theme(style="board", scheme="dark", tokens=None):
    """A Th for a style and scheme; tokens (a project's bg/panel/line/ink/muted/accents) replace the style's."""
    if tokens:
        return Th(tokens["bg"], tokens["panel"], tokens["line"], tokens["ink"], tokens["muted"], tokens["accents"],
                  "project", scheme)
    return Th(*STYLES[style][scheme][:5], STYLES[style][scheme][5], style, scheme)


# ---------------------------------------------------------------- svg primitives

def N(v):
    return format(round(v, 1), "g")


def esc(s):
    return html.escape(str(s), quote=True)


def tw(s, size=SZ, track=0.0):
    return len(s) * (size * GLYPH + track)


def fit(s, maxw, size=SZ, track=0.0):
    s = " ".join(str(s).split())
    n = max(1, int(maxw // (size * GLYPH + track)))
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def txt(x, y, s, fill, size=SZ, anchor="start", weight=400, track=0.0, maxw=None):
    s = fit(s, maxw, size, track) if maxw else " ".join(str(s).split())
    return (f'<text x="{N(x)}" y="{N(y)}" font-size="{size}"' + (f' font-weight="{weight}"' if weight != 400 else "")
            + (f' text-anchor="{anchor}"' if anchor != "start" else "")
            + (f' letter-spacing="{track}"' if track else "") + f' fill="{fill}">{esc(s)}</text>')


def rect(x, y, w, h, fill="none", stroke=None, rx=3, sw=1, op=None):
    return (f'<rect x="{N(x)}" y="{N(y)}" width="{N(max(w, 0))}" height="{N(max(h, 0))}" rx="{N(rx)}" fill="{fill}"'
            + (f' stroke="{stroke}"' + (f' stroke-width="{N(sw)}"' if sw != 1 else "") if stroke else "")
            + (f' opacity="{op}"' if op is not None else "") + "/>")


def path(d, stroke, sw=1.5, fill="none", op=None, dash=None):
    return (f'<path d="{d}" fill="{fill}"' + (f' stroke="{stroke}" stroke-width="{N(sw)}"' if stroke else "")
            + (f' stroke-dasharray="{dash}"' if dash else "") + (f' opacity="{op}"' if op is not None else "") + "/>")


def line(x0, y0, x1, y1, stroke, sw=1, op=None, dash=None):
    return path(f"M{N(x0)} {N(y0)}L{N(x1)} {N(y1)}", stroke, sw, op=op, dash=dash)


def circ(x, y, r, fill="none", stroke=None, sw=1, op=None):
    return (f'<circle cx="{N(x)}" cy="{N(y)}" r="{N(max(r, 0))}" fill="{fill}"'
            + (f' stroke="{stroke}"' + (f' stroke-width="{N(sw)}"' if sw != 1 else "") if stroke else "")
            + (f' opacity="{op}"' if op is not None else "") + "/>")


def bez(p0, p1, p2, p3, t):
    u = 1 - t
    return tuple(u ** 3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t ** 3 * d for a, b, c, d in zip(p0, p1, p2, p3))


def curve(a, b, k=.5):
    """(svg path d, the four points) of a horizontal S from a to b."""
    dx = max(10, (b[0] - a[0]) * k)
    pts = (a, (a[0] + dx, a[1]), (b[0] - dx, b[1]), b)
    return f"M{N(a[0])} {N(a[1])}C{N(pts[1][0])} {N(pts[1][1])} {N(pts[2][0])} {N(pts[2][1])} {N(b[0])} {N(b[1])}", pts


def packet(pts, t, T, i, s=7):
    x, y = bez(*pts, t)
    return rect(x - s / 2, y - s / 2, s, s, T.a(i), rx=1.5)


def pill(x, y, s, T, i, W=None, size=SZ, fill=None):
    """A label pill centred on (x, y) with an accent outline; kept inside 0..W."""
    w = tw(s, size) + 14
    if W:
        x = min(max(x, w / 2 + 1), W - w / 2 - 1)
    return [rect(x - w / 2, y - 9, w, 18, fill or T.bg, T.a(i), 4), txt(x, y + 4, s, T.ink, size, "middle")]


def rng(seed):
    s = zlib.crc32(str(seed).encode()) or 1
    while True:
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        yield s / 4294967296


# ---------------------------------------------------------------- schema: a JSON-schema subset, errors name the field

def STR(mx=40, mn=1):
    return {"type": "string", "minLength": mn, "maxLength": mx}


def NUM(lo=0, hi=100):
    return {"type": "number", "minimum": lo, "maximum": hi}


def ARR(items, lo=1, hi=12):
    return {"type": "array", "items": items, "minItems": lo, "maxItems": hi}


def OBJ(props, req=()):
    return {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}


_T = {"string": str, "number": (int, float), "integer": int, "array": list, "object": dict, "boolean": bool}


def validate(v, sc, where, errs):
    """Append what is wrong with v against schema sc, each naming its field path and what would be valid."""
    t = sc.get("type")
    if t and (not isinstance(v, _T[t]) or (t in ("number", "integer") and isinstance(v, bool))):
        errs.append(f"{where} must be {'an' if t[0] in 'aeiou' else 'a'} {t}, not {type(v).__name__} {json.dumps(v)[:30]}")
        return
    if isinstance(v, str):
        if len(v) > sc.get("maxLength", 10 ** 6) or len(v) < sc.get("minLength", 0):
            errs.append(f"{where} is {len(v)} characters; {sc.get('minLength', 0)} to {sc.get('maxLength')}")
        if "enum" in sc and v not in sc["enum"]:
            near = difflib.get_close_matches(v, sc["enum"], 1)
            errs.append(f"{where} {v[:30]!r} isn't one of {sc['enum']}" + (f": did you mean {near[0]!r}?" if near else ""))
    elif isinstance(v, (int, float)):
        if not sc.get("minimum", -1e12) <= v <= sc.get("maximum", 1e12):
            errs.append(f"{where} is {v}; {sc.get('minimum')} to {sc.get('maximum')}")
    elif isinstance(v, list):
        if not sc.get("minItems", 0) <= len(v) <= sc.get("maxItems", 100):
            errs.append(f"{where} has {len(v)} items; {sc.get('minItems', 0)} to {sc.get('maxItems', 100)}")
            return
        for i, x in enumerate(v):
            validate(x, sc.get("items", {}), f"{where}[{i}]", errs)
    elif isinstance(v, dict):
        props = sc.get("properties")
        if props is None:  # a bare object: the kind's own schema checks its keys
            return
        for k in sc.get("required", []):
            if k not in v:
                errs.append(f"{where}.{k} is required")
        for k, x in v.items():
            if k not in props:
                near = difflib.get_close_matches(k, props, 1)
                errs.append(f"{where}.{k} isn't a field here" + (f": did you mean {near[0]!r}?" if near else
                                                                  f"; the fields are {sorted(props)}"))
            else:
                validate(x, props[k], f"{where}.{k}", errs)


# ---------------------------------------------------------------- the registry

KINDS = {}  # name -> {draw, schema, sample, brief, size: (w, h)}


def kind(name, brief, schema, sample, size=(DEF_W, DEF_H)):
    def reg(fn):
        KINDS[name] = {"draw": fn, "brief": brief, "schema": schema, "sample": sample, "size": size}
        return fn
    return reg


# ---------------------------------------------------------------- the 13 kinds

def _spread(i, k):
    return 2 * math.pi * i / max(k, 1) - math.pi / 2 + .35


@kind("cluster", "a graph of clusters: dot clouds with a label pill each, faint links between them",
      OBJ({"clusters": ARR(OBJ({"label": STR(16), "n": NUM(1, 60)}, ["label"]), 2, 8),
           "links": ARR(ARR(NUM(0, 7), 2, 2), 0, 12)}, ["clusters"]),
      {"clusters": [{"label": "orders", "n": 26}, {"label": "billing", "n": 18}, {"label": "search", "n": 22},
                    {"label": "auth", "n": 14}, {"label": "notify", "n": 16}],
       "links": [[0, 1], [0, 2], [3, 0], [2, 4]]})
def _cluster(d, W, H, T):
    cl, k, out = d["clusters"], len(d["clusters"]), []
    sp = min(W, H) * (.17 if k < 4 else .11)
    cen = []
    for i in range(k):
        s = 0 if k == 1 else (1 if i % 2 == 0 else .62)
        x = W / 2 + (W / 2 - sp - 14) * .85 * s * math.cos(_spread(i, k))
        y = H / 2 + (H / 2 - sp - 20) * .85 * s * math.sin(_spread(i, k))
        cen.append((x, y))
    for a, b in d.get("links", []):
        if a < k and b < k and a != b:
            out.append(line(*cen[a], *cen[b], T.line, 1.2, op=.9))
    for i, c in enumerate(cl):
        r, (cx, cy) = rng(f"{i}{c['label']}"), cen[i]
        out.append(circ(cx, cy, sp * 1.05, T.a(i), op=.07))
        for _ in range(int(c.get("n", 20))):
            ang, rad = next(r) * 2 * math.pi, sp * math.sqrt(next(r))
            x, y = min(max(cx + rad * math.cos(ang), 6), W - 6), min(max(cy + rad * math.sin(ang), 6), H - 6)
            out.append(circ(x, y, 1.8 + next(r) * 1.4, T.a(i), op=.9))
    for i, c in enumerate(cl):
        out += pill(cen[i][0], max(cen[i][1] - sp * .95, 12), fit(c["label"], 120), T, i, W)
    return out


@kind("funnel", "sources flow in ribbons through a gate into one output",
      OBJ({"sources": ARR(OBJ({"label": STR(18), "value": NUM(1, 1e9)}, ["label", "value"]), 2, 8),
           "gate": STR(14), "result": OBJ({"label": STR(14), "sub": STR(22, 0)}, ["label"])}, ["sources", "result"]),
      {"sources": [{"label": "Orders db", "value": 4200}, {"label": "Webhooks", "value": 2600},
                   {"label": "CSV import", "value": 1500}, {"label": "Mobile app", "value": 3100}],
       "gate": "validate", "result": {"label": "events", "sub": "one schema"}})
def _funnel(d, W, H, T):
    src, k, out = d["sources"], len(d["sources"]), []
    bw, rh = min(W * .27, 150), H / k
    bh, hi = min(rh - 8, 38), max(s["value"] for s in src)
    fx, mouth = W * .60, (W * .60, H / 2)
    for i, s in enumerate(src):
        y = rh * i + rh / 2
        d_, pts = curve((bw, y), mouth)
        out.append(path(d_, T.a(i), 1.5 + 7 * s["value"] / hi, op=.45))
        out += [packet(pts, t, T, i) for t in (.3, .62)]
    for i, s in enumerate(src):
        y = rh * i + rh / 2
        out += [rect(0, y - bh / 2, bw, bh, T.panel, T.a(i), 4),
                txt(8, y - 1, s["label"], T.ink, SZ, maxw=bw - 14),
                rect(8, y + bh / 2 - 9, (bw - 16) * s["value"] / hi, 3, T.a(i), rx=1)]
    fh = min(H * .8, H - 12)
    out += [path(f"M{N(fx)} {N(H / 2 - fh / 2)}L{N(W * .70)} {N(H / 2 - 7)}V{N(H / 2 + 7)}L{N(fx)} {N(H / 2 + fh / 2)}Z",
                 T.a(1), 1.5, mix(T.panel, T.a(1), .2)),
            *pill((fx + W * .70) / 2, H / 2 + (fh / 4 + 3.5) * .75, d.get("gate", "gate"), T, 1, W,
                  fill=mix(T.panel, T.a(1), .2))]
    ow, oh = W - W * .76, min(H * .42, 90)
    r = d["result"]
    out += [line(W * .70, H / 2, W * .76, H / 2, T.a(0), 2), rect(W * .76, H / 2 - oh / 2, ow, oh, T.panel, T.a(0), 6, 1.5),
            txt(W * .76 + ow / 2, H / 2 - 2, r["label"], T.ink, 13, "middle", 700, maxw=ow - 8)]
    if r.get("sub"):
        out.append(txt(W * .76 + ow / 2, H / 2 + 14, r["sub"], T.muted, SZ, "middle", maxw=ow - 8))
    return out


@kind("transform", "a page becomes a clean output: a wireframe, chevrons, a document card",
      OBJ({"from": STR(24), "to": STR(24), "lines": ARR(STR(30), 1, 9)}, ["from", "to", "lines"]),
      {"from": "checkout.html", "to": "order.json",
       "lines": ["{", '  "id": 8841,', '  "items": 3,', '  "total": 59.2,', '  "paid": true', "}"]})
def _transform(d, W, H, T):
    pw, out = W * .40, []
    ph = H - 22
    a = T.a(1)
    out += [rect(0, 0, pw, ph, T.panel, a, 4), rect(6, 6, pw - 12, 12, a, rx=2, op=.3)]
    sbw = pw * .2
    for sx in (6, pw - 6 - sbw):
        out.append(rect(sx, 26, sbw, ph - 58, a, a, 2, 1, .12))
        out += [line(sx, 26 + j * 16, sx + sbw, 26 + j * 16 + 14, a, 1, .5) for j in range((int(ph) - 58) // 16)]
    cx0, rows = 6 + sbw + 8, max(2, int((ph - 58) // 14))
    for j in range(rows):
        wj = (pw - 2 * (sbw + 14)) * (.5 + .5 * ((j * 7) % 5) / 4)
        out.append(rect(cx0, 26 + j * 14, wj, 7, T.muted, rx=2, op=.55))
    out += [rect(6, ph - 26, pw - 12, 18, T.line, rx=2, op=.8), txt(pw / 2, H - 6, d["from"], T.muted, SZ, "middle", maxw=pw)]
    mx = W * .465
    for j in range(3):
        out.append(path(f"M{N(mx + j * 11)} {N(H / 2 - 10)}L{N(mx + j * 11 + 8)} {N(H / 2)}L{N(mx + j * 11)} {N(H / 2 + 10)}",
                        T.a(0), 2, op=.4 + .3 * j))
    cx, cw = W * .55, W * .45
    out += [rect(cx, 0, cw, ph, T.panel, T.a(0), 4, 1.5), txt(cx + 12, 22, d["to"], T.txt(0), SZ, weight=700, maxw=cw - 24),
            line(cx + 12, 30, cx + cw - 12, 30, T.line)]
    lh = min(17, (ph - 50) / max(len(d["lines"]), 1))
    for j, s in enumerate(d["lines"]):
        out.append(txt(cx + 12, 48 + j * lh, s, T.ink, SZ, maxw=cw - 24))
    out.append(txt(cx + cw / 2, H - 6, "output", T.muted, SZ, "middle"))
    return out


@kind("globe", "a dotted globe over a histogram skyline",
      OBJ({"bars": ARR(NUM(0, 1e9), 4, 48), "dots": NUM(20, 400)}, ["bars"]),
      {"bars": [3, 5, 4, 8, 6, 9, 7, 12, 10, 8, 14, 11, 9, 13, 16, 12, 10, 15, 18, 14, 11, 9, 12, 8, 6, 7, 5, 4],
       "dots": 180}, (520, 420))
def _globe(d, W, H, T):
    bars, out = d["bars"], []
    hh = H * .2
    R = max(20, min(W * .45, (H - hh - 16) / 2))
    cx, cy = W / 2, R + 4
    out.append(circ(cx, cy, R, T.panel, T.a(0), 1.5))
    for lat in (-60, -30, 0, 30, 60):
        s, y = math.cos(math.radians(lat)), math.sin(math.radians(lat))
        out.append(f'<ellipse cx="{N(cx)}" cy="{N(cy - R * y * .85)}" rx="{N(R * s)}" ry="{N(R * s * .22)}" fill="none" '
                   f'stroke="{T.a(0)}" opacity=".35"/>')
    for k in range(1, 6):
        out.append(f'<ellipse cx="{N(cx)}" cy="{N(cy)}" rx="{N(R * abs(math.cos(k * math.pi / 6)))}" ry="{N(R)}" fill="none" '
                   f'stroke="{T.a(0)}" opacity=".35"/>')
    n = int(d.get("dots", 160))
    r = rng("globe")
    for i in range(n):
        y = 1 - 2 * (i + .5) / n
        s, th = math.sqrt(1 - y * y), i * 2.39996
        x, z = s * math.cos(th), s * math.sin(th)
        if z > 0:
            out.append(circ(cx + R * x, cy - R * y, 1.3 + 1.2 * next(r), T.a(1 + i % 4), op=round(.45 + .5 * z, 2)))
    hi, bw = max(max(bars), 1), (W - 8) / len(bars)
    for i, v in enumerate(bars):
        bh = max(2, (hh - 4) * v / hi)
        out.append(rect(4 + i * bw + 1, H - 3 - bh,max(bw - 2, 1), bh, T.a(0), rx=0, op=.8))
    return out


@kind("lanes", "pipeline lanes: rails with stations, a cart on each lane at its stage",
      OBJ({"stages": ARR(STR(10), 2, 7), "lanes": ARR(OBJ({"label": STR(16), "at": NUM(0, 6)}, ["label", "at"]), 1, 6)},
          ["stages", "lanes"]),
      {"stages": ["queue", "build", "test", "deploy", "verify"],
       "lanes": [{"label": "shop-api", "at": 3}, {"label": "notes-app", "at": 1}, {"label": "billing", "at": 4},
                 {"label": "search", "at": 2}]})
def _lanes(d, W, H, T):
    st, ln, out = d["stages"], d["lanes"], []
    L, n = min(110, W * .22), len(st)
    xs = [L + (W - L - 24) * k / (n - 1) for k in range(n)]
    for k, s in enumerate(st):
        out.append(txt(xs[k], 12, s, T.muted, SZ, "middle", maxw=(W - L) / n))
    rh = (H - 30) / len(ln)
    for j, lane in enumerate(ln):
        y, at = 30 + rh * j + rh / 2, min(int(lane["at"]), n - 1)
        out += [txt(0, y + 4, lane["label"], T.ink, SZ, maxw=L - 8), line(L, y - 5, xs[-1], y - 5, T.line),
                line(L, y + 5, xs[-1], y + 5, T.line), line(L, y, xs[at], y, T.a(j), 2.5, op=.7)]
        for k in range(n):
            out.append(circ(xs[k], y, 5, T.a(j) if k <= at else T.bg, T.a(j) if k > at else None, 1.5))
        out += [rect(xs[at] - 14, y - 9, 28, 18, T.a(j), rx=4), txt(xs[at], y + 4, ">>", T.on(T.a(j)), SZ, "middle", 700)]
    return out


@kind("filetree", "a file tree with one file opened as a document card, joined by a connector",
      OBJ({"files": ARR(STR(40), 1, 16), "focus": STR(40, 0), "doc": OBJ({"title": STR(24), "lines": ARR(STR(34, 0), 1, 9)},
                                                                         ["title", "lines"])}, ["files", "doc"]),
      {"files": ["src/app.py", "src/routes/orders.py", "src/routes/cart.py", "src/db/models.py", "tests/test_orders.py",
                 "README.md", "pyproject.toml"], "focus": "src/routes/orders.py",
       "doc": {"title": "orders.py", "lines": ["@router.get('/orders')", "def list_orders():", "    return db.all()",
                                               "", "@router.post('/orders')", "def add_order(body):",
                                               "    return db.add(body)"]}}, (640, 340))
def _filetree(d, W, H, T):
    rows, seen, out = [], set(), []
    for p in sorted(d["files"], key=lambda p: (p.count("/") == 0, p)):
        parts = p.strip("/").split("/")
        for k in range(len(parts) - 1):
            pre = "/".join(parts[:k + 1])
            if pre not in seen:
                seen.add(pre)
                rows.append((k, parts[k] + "/", None))
        rows.append((len(parts) - 1, parts[-1], p))
    rh = min(20, H / max(len(rows), 1))
    rh = max(rh, 14)
    rows = rows[:max(1, int(H // rh))]
    tw_ = W * .42
    cx, cw = W * .54, W * .46
    ph = min(H, 24 + 17 * (len(d["doc"]["lines"]) + 1))
    foc = None
    for i, (dep, name, full) in enumerate(rows):
        y = i * rh
        hit = full is not None and full == d.get("focus")
        if hit:
            foc = (tw_, y + rh / 2)
            out.append(rect(0, y + 1, tw_, rh - 2, T.a(2), T.a(2), 3, 1, .22))
        out.append(txt(8 + dep * 14, y + rh / 2 + 4, name, T.muted if full is None else T.ink, SZ,
                       weight=700 if hit else 400, maxw=tw_ - 12 - dep * 14))
    if foc:
        d_, pts = curve(foc, (cx, 24), .5)
        out += [path(d_, T.a(2), 1.5, op=.8), packet(pts, .5, T, 2)]
    out += [rect(cx, 0, cw, ph, T.panel, T.a(2), 4, 1.5), txt(cx + 10, 18, d["doc"]["title"], T.txt(2), SZ, weight=700,
                                                              maxw=cw - 20), line(cx + 10, 26, cx + cw - 10, 26, T.line)]
    for j, s in enumerate(d["doc"]["lines"]):
        out.append(txt(cx + 10, 44 + j * 17, s, T.ink, SZ, maxw=cw - 20))
    return out


_GOOD, _WARN, _BAD = ("ok", "pass", "up", "done", "green"), ("warn", "wait", "slow", "pending", "idle"), \
                     ("fail", "down", "error", "err", "red", "bad")


@kind("terminal", "a terminal running a command above a status table with state pills",
      OBJ({"command": STR(40), "columns": ARR(STR(12), 2, 4), "rows": ARR(ARR(STR(18), 2, 4), 1, 8)},
          ["command", "columns", "rows"]),
      {"command": "shop check --all", "columns": ["service", "version", "status"],
       "rows": [["api", "2.4.1", "ok"], ["worker", "2.4.0", "ok"], ["search", "1.9.2", "warn"], ["mailer", "0.8.0", "fail"]]}, (640, 270))
def _terminal(d, W, H, T):
    cols, rows = d["columns"], d["rows"]
    H = min(H, 54 + 24 * (len(rows) + 1) + 6)  # the window hugs its table instead of leaving a void below
    out = [rect(0, 0, W, H, T.bg, T.line, 6), line(0, 22, W, 22, T.line)]
    out += [circ(12 + 14 * i, 11, 3.5, T.a(c)) for i, c in enumerate((2, 1, 3))]
    cmd = fit(d["command"], W - 40)
    out += [txt(10, 40, "$", T.txt(3), SZ, weight=700), txt(24, 40, cmd, T.ink, SZ),
            rect(24 + tw(cmd) + 3, 31, 7, 12, T.ink, rx=0, op=.8)]
    ty, k = 54, len(cols)
    rh = max(12, min(24, (H - ty - 6) / (len(rows) + 1)))
    rows = rows[:max(1, int((H - ty - 6) // rh) - 1)]
    cw = (W - 20) / k
    out.append(rect(10, ty, W - 20, rh, T.panel, T.line, 2))
    for j, c in enumerate(cols):
        out.append(txt(16 + j * cw, ty + rh / 2 + 4, c.upper(), T.muted, SZ, weight=700, maxw=cw - 8))
    for i, r in enumerate(rows):
        y = ty + rh * (i + 1)
        out.append(line(10, y, W - 10, y, T.line, 1, .6))
        for j, cell in enumerate(r[:k]):
            word = cell.lower()
            c = 3 if word in _GOOD else 1 if word in _WARN else 2 if word in _BAD else None
            if c is not None and j == k - 1:
                out += [rect(16 + j * cw - 2, y + rh / 2 - 8 + 0.5, tw(fit(cell, cw - 8)) + 12, 16, T.a(c), T.a(c), 8, 1, .25),
                        txt(16 + j * cw + 4, y + rh / 2 + 4.5, cell, T.ink, SZ, maxw=cw - 14)]
            else:
                out.append(txt(16 + j * cw, y + rh / 2 + 4.5, cell, T.ink, SZ, maxw=cw - 8))
    return out


@kind("tanks", "tanks filled to a level, joined by a manifold of pipes and valves",
      OBJ({"tanks": ARR(OBJ({"label": STR(12), "value": NUM(0, 100)}, ["label", "value"]), 2, 6)}, ["tanks"]),
      {"tanks": [{"label": "queue", "value": 72}, {"label": "cache", "value": 41}, {"label": "disk", "value": 88},
                 {"label": "memory", "value": 57}]})
def _tanks(d, W, H, T):
    tk, out = d["tanks"], []
    n = len(tk)
    slot = W / n
    w = min(84, slot * .62)
    y0, y1 = 22, H - 34
    cxs = [slot * (i + .5) for i in range(n)]
    out.append(line(cxs[0], y1 + 10, cxs[-1], y1 + 10, T.line, 3))
    for i, t in enumerate(tk):
        x, lvl = cxs[i] - w / 2, y1 - (y1 - y0) * t["value"] / 100
        out += [rect(x, y0, w, y1 - y0, T.panel, T.line, 8)]
        if t["value"] > 0:
            out += [rect(x + 1, lvl, w - 2, y1 - lvl - 1, T.a(i), rx=6, op=.4),
                    line(x + 1, lvl, x + w - 1, lvl, T.a(i), 2)]
        out += [line(x - 5, y0 + (y1 - y0) * k / 4, x, y0 + (y1 - y0) * k / 4, T.muted) for k in range(5)]
        out += [line(cxs[i], y1, cxs[i], y1 + 10, T.line, 3), circ(cxs[i], y1 + 10, 4, T.bg, T.a(i), 1.5),
                txt(cxs[i], y0 - 7, f"{t['value']:g}%", T.txt(i), SZ, "middle", 700),
                txt(cxs[i], H - 3, t["label"], T.ink, SZ, "middle", maxw=slot - 4)]
    return out


@kind("orbit", "a hub with its nodes on two orbits, joined by spokes",
      OBJ({"center": STR(16), "nodes": ARR(OBJ({"label": STR(12)}, ["label"]), 2, 12)}, ["center", "nodes"]),
      {"center": "shop-api", "nodes": [{"label": n} for n in ("Postgres", "Redis", "Stripe", "S3", "Sentry", "Mailgun", "CDN")]})
def _orbit(d, W, H, T):
    nodes, out = d["nodes"], []
    cx, cy = W / 2, H / 2
    k_in = max(1, len(nodes) // 3)
    rx2, ry2 = max(40, W / 2 - 52), max(30, H / 2 - 18)
    rings = [(rx2 * .5, ry2 * .5), (rx2, ry2)]
    for rx, ry in rings:
        out.append(f'<ellipse cx="{N(cx)}" cy="{N(cy)}" rx="{N(rx)}" ry="{N(ry)}" fill="none" stroke="{T.line}" '
                   f'stroke-dasharray="3 5"/>')
    groups = [nodes[:k_in], nodes[k_in:]]
    for g, (rx, ry) in zip(groups, rings):
        for i, nd in enumerate(g):
            a = 2 * math.pi * i / max(len(g), 1) - math.pi / 2 + (.5 if g is groups[1] else 0)
            x, y = cx + rx * math.cos(a), cy + ry * math.sin(a)
            n_ = nodes.index(nd)
            out += [line(cx, cy, x, y, T.a(n_), 1, .35), circ(x, y, 4, T.a(n_))]
            out += pill(x, y + (-14 if math.sin(a) <= 0 else 14), nd["label"], T, n_, W)
    hr = min(max(20, tw(d["center"]) / 2 + 8), rings[0][1] * .8, rings[0][0] * .8)
    out += [circ(cx, cy, hr + 8, T.a(0), op=.12), circ(cx, cy, hr, T.panel, T.a(0), 2)]
    if tw(d["center"]) > hr * 2 - 8:  # a long name sits whole on a bar across the hub
        bw_ = min(tw(d["center"]) + 16, W - 8)
        out.append(rect(cx - bw_ / 2, cy - 10, bw_, 20, T.panel, T.a(0), 4, 2))
    out.append(txt(cx, cy + 4, d["center"], T.ink, SZ, "middle", 700, maxw=W - 16))
    return out


@kind("coremap", "a chip die: a core, module blocks and the traces that wire them, pins along the edge",
      OBJ({"core": STR(14), "modules": ARR(OBJ({"label": STR(12)}, ["label"]), 2, 8)}, ["core", "modules"]),
      {"core": "scheduler", "modules": [{"label": n} for n in ("parser", "planner", "cache", "runner", "store", "logger")]})
def _coremap(d, W, H, T):
    mods, out = d["modules"], []
    out.append(rect(4, 8, W - 8, H - 16, T.panel, T.line, 8, 1.5))
    for i in range(min(int((W - 40) // 16), 24)):
        x = 22 + i * 16
        out += [line(x, 2, x, 8, T.muted), line(x, H - 8, x, H - 2, T.muted)]
    cw, ch = min(W * .22, 120), min(H * .4, 80)
    cx, cy = W / 2, H / 2
    out += [rect(cx - cw / 2, cy - ch / 2, cw, ch, T.a(0), T.a(0), 6, 2, .22),
            txt(cx, cy + 4, d["core"], T.ink, SZ, "middle", 700, maxw=cw - 8)]
    sides = [mods[0::2], mods[1::2]]
    mw = min(W * .22, 110)
    for s, g in enumerate(sides):
        rh = (H - 36) / max(len(g), 1)
        bh = min(34, rh - 8)
        for i, m in enumerate(g):
            idx = mods.index(m) + 1
            y = 18 + rh * i + rh / 2
            x = 22 if s == 0 else W - 22 - mw
            ex = x + mw if s == 0 else x
            core_x = cx - cw / 2 if s == 0 else cx + cw / 2
            ty = cy + (i - (len(g) - 1) / 2) * min(14, ch / max(len(g), 1))
            mid = (ex + core_x) / 2 + (i - len(g) / 2) * 6
            out += [path(f"M{N(ex)} {N(y)}H{N(mid)}V{N(ty)}H{N(core_x)}", T.a(idx), 1.5, op=.7),
                    rect(x, y - bh / 2, mw, bh, T.bg, T.a(idx), 4, 1.25), txt(x + mw / 2, y + 4, m["label"], T.ink, SZ,
                                                                             "middle", maxw=mw - 8),
                    circ(mid, (y + ty) / 2, 2.5, T.a(idx))]
    return out


@kind("river", "a timeline river: three streams carrying buoys, labelled by events in order",
      OBJ({"events": ARR(OBJ({"label": STR(14), "lane": NUM(0, 2)}, ["label"]), 2, 8)}, ["events"]),
      {"events": [{"label": "v1.0", "lane": 1}, {"label": "auth rewrite", "lane": 0}, {"label": "v1.2", "lane": 1},
                  {"label": "search", "lane": 2}, {"label": "outage", "lane": 0}, {"label": "v2.0", "lane": 1}]})
def _river(d, W, H, T):
    ev, out = d["events"], []
    base = (H * .3, H * .5, H * .7)
    amp = H * .06

    def yat(lane, x):
        return base[lane] + amp * math.sin(x / W * 2 * math.pi * 1.3 + lane * 1.7)
    for lane in range(3):
        pts = [(x, yat(lane, x)) for x in range(0, int(W) + 1, 10)]
        dd = "M" + "L".join(f"{N(x)} {N(y)}" for x, y in pts)
        out += [path(dd, T.a(lane), 14, op=.12), path(dd, T.a(lane), 2, op=.8), path(dd, T.a(lane), 1, op=.9, dash="2 12")]
    n = len(ev)
    for i, e in enumerate(ev):
        lane = int(e.get("lane", 1))
        x = W * (i + .5) / n
        y = yat(lane, x)
        up = lane == 0 or (lane == 1 and i % 2 == 0)
        ly = y - 28 if up else y + 28
        out += [line(x, y, x, ly + (7 if up else -11), T.a(lane), 1, .6), circ(x, y, 5, T.bg, T.a(lane), 2),
                circ(x, y, 2, T.a(lane))]
        out += pill(x, ly, fit(e["label"], max(W * 2 / n - 20, 30)), T, lane, W)
    return out


@kind("gate", "one stream hits a gate and splits: allowed items continue, refused ones are stopped",
      OBJ({"source": STR(14), "gate": STR(14), "allowed": ARR(STR(16), 1, 5), "refused": ARR(STR(16), 1, 5)},
          ["gate", "allowed", "refused"]),
      {"source": "requests", "gate": "policy", "allowed": ["GET /orders", "POST /cart", "GET /items"],
       "refused": ["DELETE /users", "GET /admin"]})
def _gate(d, W, H, T):
    out, gx = [], W * .40
    sw = W * .2
    out += [rect(0, H / 2 - 14, sw, 28, T.panel, T.a(0), 4), txt(sw / 2, H / 2 + 4, d.get("source", "in"), T.ink, SZ, "middle",
                                                                  maxw=sw - 8)]
    d_, pts = curve((sw, H / 2), (gx, H / 2), .5)
    out += [path(d_, T.a(0), 6, op=.4), packet(pts, .5, T, 0)]
    out += [rect(gx - 5, H * .12, 10, H * .76, T.panel, T.a(1), 3, 1.5), path(
        f"M{N(gx)} {N(H / 2 - 12)}L{N(gx + 10)} {N(H / 2)}L{N(gx)} {N(H / 2 + 12)}L{N(gx - 10)} {N(H / 2)}Z", T.a(1), 1.5, T.a(1),
        .3)] + pill(gx, H * .12 - 2, d["gate"], T, 1, W)
    colx = W * .62
    for items, up, ci in ((d["allowed"], True, 3), (d["refused"], False, 2)):
        top, k = (4, len(items)) if up else (H / 2 + 6, len(items))
        rh = (H / 2 - 10) / k
        bh = min(24, rh - 4)
        for j, s in enumerate(items):
            y = top + rh * j + rh / 2
            dd, p2 = curve((gx + 5, H / 2 - (4 if up else -4)), (colx, y), .5)
            out.append(path(dd, T.a(ci), 2 if up else 1.5, op=.7 if up else .45, dash=None if up else "4 4"))
            out += [rect(colx, y - bh / 2, W - colx, bh, T.panel, T.a(ci), 4), txt(colx + 8, y + 4, s, T.ink, SZ,
                                                                                   maxw=W - colx - 28)]
            mx = W - 14
            out.append(path(f"M{N(mx - 4)} {N(y)}L{N(mx - 1)} {N(y + 3)}L{N(mx + 4)} {N(y - 3)}", T.a(ci), 1.5) if up else
                       path(f"M{N(mx - 3)} {N(y - 3)}L{N(mx + 3)} {N(y + 3)}M{N(mx + 3)} {N(y - 3)}L{N(mx - 3)} {N(y + 3)}",
                            T.a(ci), 1.5))
    return out


@kind("arch", "architecture boxes in ranks with packets flowing along the edges between them",
      OBJ({"nodes": ARR(OBJ({"id": STR(16), "label": STR(16), "sub": STR(22, 0)}, ["id"]), 2, 10),
           "edges": ARR(OBJ({"from": STR(16), "to": STR(16), "label": STR(12, 0)}, ["from", "to"]), 1, 16)},
          ["nodes", "edges"]),
      {"nodes": [{"id": "browser", "label": "Browser", "sub": "React app"}, {"id": "api", "label": "API", "sub": "FastAPI"},
                 {"id": "worker", "label": "Worker", "sub": "Celery"}, {"id": "db", "label": "Postgres"},
                 {"id": "cache", "label": "Redis"}],
       "edges": [{"from": "browser", "to": "api", "label": "HTTPS"}, {"from": "api", "to": "db", "label": "SQL"},
                 {"from": "api", "to": "cache"}, {"from": "api", "to": "worker", "label": "jobs"},
                 {"from": "worker", "to": "db"}]})
def _arch(d, W, H, T):
    nodes, edges, out = d["nodes"], d.get("edges", []), []
    ids = [n["id"] for n in nodes]
    rank = {i: 0 for i in ids}
    for _ in range(len(ids)):  # longest path, capped so a loop can't climb forever
        for e in edges:
            if e["from"] in rank and e["to"] in rank and rank[e["from"]] + 1 < len(ids) and rank[e["to"]] < rank[e["from"]] + 1 \
                    and rank[e["from"]] < rank[e["to"]] + len(ids):
                if e["from"] != e["to"] and rank[e["to"]] <= rank[e["from"]] and rank[e["from"]] < len(ids) - 1 \
                        and not _reaches(edges, e["to"], e["from"]):
                    rank[e["to"]] = rank[e["from"]] + 1
    cols = max(rank.values()) + 1
    gap = 64 if W >= 560 else 30  # room between ranks for an edge label, only when wide enough to keep boxes readable
    bw = min(130, (W - gap * (cols - 1)) / cols)
    labels = W >= 560
    box = {}
    for c in range(cols):
        col = [n for n in nodes if rank[n["id"]] == c]
        rh = H / len(col)
        bh = min(48, rh - 10)
        for j, n in enumerate(col):
            x = 0 if cols == 1 else (W - bw) * c / (cols - 1)
            box[n["id"]] = (x, rh * j + rh / 2 - bh / 2, bw, bh, ids.index(n["id"]))
    for i, e in enumerate(edges):
        if e["from"] not in box or e["to"] not in box or e["from"] == e["to"]:
            continue
        ax, ay, aw, ah, ai = box[e["from"]]
        bx, by, bw_, bh_, _ = box[e["to"]]
        if bx > ax + aw - 1:
            dd, pts = curve((ax + aw, ay + ah / 2), (bx, by + bh_ / 2), .5)
        else:  # across or back: down below both and up
            low = max(ay + ah, by + bh_) + 12
            pts = ((ax + aw / 2, ay + ah), (ax + aw / 2, low), (bx + bw_ / 2, low), (bx + bw_ / 2, by + bh_))
            dd = f"M{N(pts[0][0])} {N(pts[0][1])}C" + " ".join(f"{N(p[0])} {N(p[1])}" for p in pts[1:])
        out += [path(dd, T.a(ai), 1.5, op=.6), packet(pts, .35, T, ai), packet(pts, .72, T, ai)]
        if e.get("label") and labels:
            mx, my = bez(*pts, .5)
            out += pill(mx, my, e["label"], T, ai, W, size=SZ)
    for n in nodes:
        x, y, w, h, i = box[n["id"]]
        nm, tight = n.get("label") or n["id"], w < tw(n.get("label") or n["id"], SZ) + 34
        out.append(rect(x, y, w, h, T.panel, T.a(i), 4, 1.25))
        if tight:  # no room for the dot and the sub line: the name alone, whole as far as the box allows
            out.append(txt(x + w / 2, y + h / 2 + 4, nm, T.ink, SZ, "middle", 700, maxw=w - 8))
            continue
        out += [circ(x + 12, y + 14, 3.5, T.a(i)), txt(x + 22, y + 18, nm, T.ink, SZ, weight=700, maxw=w - 28)]
        if n.get("sub"):
            out.append(txt(x + 22, y + 33, n["sub"], T.muted, SZ, maxw=w - 28))
    return out


def _reaches(edges, a, b):
    seen, todo = set(), [a]
    while todo:
        x = todo.pop()
        if x == b:
            return True
        if x not in seen:
            seen.add(x)
            todo += [e["to"] for e in edges if e["from"] == x]
    return False


# ---------------------------------------------------------------- panel chrome, spec, render

PANEL = OBJ({"kind": {"type": "string", "enum": list(KINDS)}, "title": STR(40), "stat": STR(24, 0), "foot": STR(70, 0),
             "n": NUM(1, 99), "accent": NUM(0, 5), "w": NUM(MIN_W, MAX_W), "h": NUM(MIN_H, MAX_H), "data": {"type": "object"}},
            ["kind", "title", "data"])
SPEC = OBJ({"name": STR(40), "title": STR(80), "style": {"type": "string", "enum": list(STYLES_NAMES)},
            "panels": ARR(PANEL, 1, 24)}, ["panels"])


def check_spec(spec):
    """[errors]: each names its field path (panels[2].data.clusters[0].n) and what would be valid."""
    errs = []
    validate(spec, SPEC, "spec", errs)
    if errs:
        return errs
    for i, p in enumerate(spec["panels"]):
        validate(p["data"], KINDS[p["kind"]]["schema"], f"panels[{i}].data", errs)
    return errs


def check_data(name, data):
    errs = []
    if name not in KINDS:
        near = difflib.get_close_matches(name, KINDS, 1)
        return [f"kind {name!r} isn't one of {list(KINDS)}" + (f": did you mean {near[0]!r}?" if near else "")]
    validate(data, KINDS[name]["schema"], "data", errs)
    return errs


def _words(v, out):
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, dict):
        [_words(x, out) for x in v.values()]
    elif isinstance(v, list):
        [_words(x, out) for x in v]
    return out


def size_of(p):
    dw, dh = KINDS[p["kind"]]["size"]
    h = min(max(p.get("h", dh), MIN_H), MAX_H)
    d = p.get("data") or {}
    if p["kind"] == "terminal":  # a window hugs its table: taller than its rows is a void
        h = min(h, HEAD + 60 + 24 * (len(d.get("rows", [])) + 1) + 24 + FOOT)
    elif p["kind"] == "filetree":
        dirs = {"/".join(f.strip("/").split("/")[:k]) for f in d.get("files", []) for k in range(1, f.strip("/").count("/") + 1)}
        doc = 24 + 17 * (len((d.get("doc") or {}).get("lines", [])) + 1)
        h = min(h, HEAD + max(20 * (len(d.get("files", [])) + len(dirs)), doc) + 24 + FOOT)
    return min(max(p.get("w", dw), MIN_W), MAX_W), max(h, MIN_H)


def panel_svg(p, T, n, ident="", nested=False):
    """One panel as an svg text: p = {kind, title, data, stat, foot, n, accent, w, h}."""
    W, H = size_of(p)
    foot = p.get("foot", "")
    body = KINDS[p["kind"]]["draw"](p["data"], W - 2 * PAD, H - HEAD - (FOOT if foot else 12), T)
    desc = f"{p['kind']}: " + "; ".join(_words(p["data"], [])[:10])
    label = f'role="img" aria-labelledby="{ident}t {ident}d" font-family="{FONT}"'
    head = (f'<g {label}>' if nested else f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {N(W)} {N(H)}" '
                                           f'width="{N(W)}" height="{N(H)}" {label}>')
    parts = [head, f'<title id="{ident}t">{esc(p["title"])}</title><desc id="{ident}d">{esc(desc)}</desc>',
             rect(0, 0, W, H, T.bg, rx=8),
             *chrome(T, 0, 0, W, H, f"{p.get('n', n):02d}", p["title"], p.get("stat", ""), p.get("accent", n - 1), out=3 if nested else -1),
             f'<g transform="translate({PAD} {HEAD})">', *body, "</g>",
             *([txt(PAD, H - 8, foot, T.muted, SZ, maxw=W - 2 * PAD)] if foot else []), "</g>" if nested else "</svg>"]
    return "\n".join(parts) + "\n"


def chrome(T, x, y, W, H, chip, title, stat="", acc=0, sub="", out=3):
    """The Board frame of a panel at (x, y): the card, a numbered chip, the UPPERCASE title (and a quiet sub after it), a
    stat on the right, the header rule and the corner brackets. One place, so readme.diagram's groups look the same."""
    cwid = tw(chip) + 10
    full = W - 2 * PAD - cwid - 14
    if stat and tw(title.upper(), SZ, .9) > full - tw(stat) - 16:
        stat = ""  # the title is whole or the stat is dropped, never a cut title beside a stat
    room = full - (tw(stat) + 8 if stat else 0)
    t = fit(title.upper(), room, SZ, .9)
    k = 7
    o = out  # how far the brackets stand off the card; a standalone svg passes -1 so nothing is painted past its viewBox
    brk = (f"M{N(x - o)} {N(y + k)}V{N(y - o)}H{N(x + k)}M{N(x + W - k)} {N(y - o)}H{N(x + W + o)}V{N(y + k)}"
           f"M{N(x + W + o)} {N(y + H - k)}V{N(y + H + o)}H{N(x + W - k)}M{N(x + k)} {N(y + H + o)}H{N(x - o)}V{N(y + H - k)}")
    subtxt = ""
    if sub and tw(t, SZ, .9) + 24 < room:
        subtxt = (f'<tspan font-weight="400" letter-spacing="0" fill="{T.muted}"> · '
                  f'{esc(fit(sub, room - tw(t, SZ, .9) - 24))}</tspan>')
    on = T.on(T.a(acc))  # the chip's number reads at 4.6:1: its fill moves away from the text colour until it does
    fill = readable(T.a(acc), (on,), T.ink if on == T.bg else T.bg, 4.6)
    return [rect(x + .5, y + .5, W - 1, H - 1, T.panel, T.line, 6),
            rect(x + PAD, y + 9, cwid, 16, fill, rx=2), txt(x + PAD + 5, y + 21, chip, on, SZ, weight=700),
            f'<text x="{N(x + PAD + cwid + 10)}" y="{N(y + 21)}" font-size="{SZ}" font-weight="700" letter-spacing=".9" '
            f'fill="{T.ink}">{esc(t)}{subtxt}</text>',
            *([txt(x + W - PAD, y + 21, stat, T.txt(acc), SZ, "end", 700)] if stat else []),
            line(x, y + HEAD - 4, x + W, y + HEAD - 4, T.line), path(brk, T.a(acc), 1.5)]


def sheet_svg(panels, T, cols=3, gap=18):
    """Every panel in one svg, in a grid: the contact sheet."""
    sizes = [size_of(p) for p in panels]
    cw = max(w for w, _ in sizes)
    rows = [sizes[i:i + cols] for i in range(0, len(sizes), cols)]
    rh = [max(h for _, h in r) for r in rows]
    W, H = cols * cw + (cols + 1) * gap, sum(rh) + (len(rows) + 1) * gap
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {N(W)} {N(H)}" width="{N(W)}" height="{N(H)}" '
           f'font-family="{FONT}"><title>panel kit contact sheet</title>', rect(0, 0, W, H, T.bg, rx=0)]
    for i, p in enumerate(panels):
        r, c = divmod(i, cols)
        x, y = gap + c * (cw + gap), gap + sum(rh[:r]) + r * gap
        out += [f'<g transform="translate({N(x)} {N(y)})">', panel_svg(p, T, i + 1, f"p{i}", nested=True), "</g>"]
    return "\n".join(out + ["</svg>"]) + "\n"


def _stat(sample):
    n = sum(len(v) for v in sample.values() if isinstance(v, list))
    return f"{n} items"


def sample_spec(style="board", names=None):
    names = names or list(KINDS)
    return {"name": "kit-sample", "title": "Panel kit sample", "style": style,
            "panels": [{"kind": k, "title": k, "stat": _stat(KINDS[k]["sample"]), "foot": "sample data: shop-api",
                        "data": json.loads(json.dumps(KINDS[k]["sample"]))} for k in names]}


def check_svg(text):
    """[problems] in one panel svg by structure alone (what a confined checker can see): well formed, labelled, drawn,
    and nothing that runs, loads or reaches out."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return [f"not well-formed XML: {e}"]
    bad, tags = [], [el.tag.split("}")[-1] for el in root.iter()]
    for el in root.iter():
        for k, v in el.attrib.items():
            if k.split("}")[-1] in ("href", "src") or k.lower().startswith("on") or "url(" in v or "http" in v.lower() and k != "xmlns":
                bad.append(f"{el.tag.split('}')[-1]} has {k}")
    if root.get("role") != "img" or "title" not in tags or "desc" not in tags:
        bad.append("no role=img, title and desc")
    if tags.count("script") or tags.count("foreignObject") or tags.count("image") or tags.count("style"):
        bad.append("a script, foreignObject, image or style element")
    if len(tags) < 12:
        bad.append(f"only {len(tags)} elements drawn")
    return bad[:5]


# ---------------------------------------------------------------- calling cards

def card_json(name):
    k = KINDS[name]
    w, h = k["size"]
    data = dict(k["schema"])
    data["description"] = f"the {name} panel's data: " + ", ".join(k["schema"]["properties"])
    return {"name": f"panel-{name}", "kind": "py", "deterministic": True,
            "purpose": f"Draw a {name} panel as dark and light SVG in the Board look (or blueprint, paper): {k['brief']}.",
            "examples": [f"draw a {name} panel", f"a {name} diagram of my project", k["brief"]],
            "inputs": {"type": "object", "required": ["out", "title", "data"], "additionalProperties": False, "properties": {
                "out": {"type": "string", "format": "newpath", "description": "the file stem: STEM-dark.svg and STEM-light.svg are written"},
                "title": {"type": "string", "minLength": 1, "maxLength": 40},
                "style": {"enum": list(STYLES_NAMES), "default": "board"},
                "stat": {"type": "string", "maxLength": 24}, "foot": {"type": "string", "maxLength": 70},
                "w": {"type": "integer", "minimum": MIN_W, "maximum": MAX_W, "default": w},
                "h": {"type": "integer", "minimum": MIN_H, "maximum": MAX_H, "default": h},
                "data": data}},
            "creates": ["$out-dark.svg", "$out-light.svg"]}


def card_main(name):
    """gen.py: one panel's two svgs from the call's inputs; a bad input exits 1 with the field named."""
    inp = json.load(sys.stdin)["inputs"]
    errs = check_data(name, inp.get("data"))
    if errs:
        sys.stderr.write("; ".join(errs[:4]) + "\n")
        sys.exit(1)
    p = {k: inp[k] for k in ("title", "stat", "foot", "w", "h") if k in inp}
    p.update(kind=name, data=inp["data"])
    os.makedirs(os.path.dirname(inp["out"]) or ".", exist_ok=True)
    for scheme in ("dark", "light"):
        with open(f"{inp['out']}-{scheme}.svg", "w", encoding="utf-8") as f:
            f.write(panel_svg(p, theme(inp.get("style", "board"), scheme), 1))


def card_check():
    """check.py: both svgs exist and pass check_svg."""
    inp = json.load(sys.stdin)
    for scheme in ("dark", "light"):
        path_ = f"{inp['inputs']['out']}-{scheme}.svg"
        if not os.path.isfile(path_):
            sys.stderr.write(f"{path_} wasn't written\n")
            sys.exit(1)
        with open(path_, encoding="utf-8") as f:
            bad = check_svg(f.read())
        if bad:
            sys.stderr.write(f"{path_}: " + "; ".join(bad) + "\n")
            sys.exit(1)
