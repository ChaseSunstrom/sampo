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
    """A packet at t on the curve pts; data-p and data-t let an animated page (sampo panels --html, T-3734) run it on."""
    x, y = bez(*pts, t)
    on = " ".join(N(v) for q in pts for v in q)
    return rect(x - s / 2, y - s / 2, s, s, T.a(i), rx=1.5).replace("<rect ", f'<rect data-p="{on}" data-t="{t:g}" ', 1)


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

KINDS = {}  # name -> {draw, schema, sample, brief, size: (w, h), refs}


def kind(name, brief, schema, sample, size=(DEF_W, DEF_H), refs=None):
    """refs(data, where) -> [errors]: what the schema can't say, that a name used names a part the data declares."""
    def reg(fn):
        KINDS[name] = {"draw": fn, "brief": brief, "schema": schema, "sample": sample, "size": size, "refs": refs}
        return fn
    return reg


def _named(names, v, where, what):
    """[] when v is one of names, else the error naming the field, the choices and the nearest."""
    if v in names:
        return []
    near = difflib.get_close_matches(str(v), [str(x) for x in names], 1)
    return [f"{where} {str(v)[:30]!r} isn't one of the {what} {list(names)[:12]}" + (f": did you mean {near[0]!r}?" if near else "")]


# ---------------------------------------------------------------- the 29 kinds

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
        out += [f'<g data-drift="{i}">', circ(cx, cy, sp * 1.05, T.a(i), op=.07)]  # a group an animated page drifts
        for _ in range(int(c.get("n", 20))):
            ang, rad = next(r) * 2 * math.pi, sp * math.sqrt(next(r))
            x, y = min(max(cx + rad * math.cos(ang), 6), W - 6), min(max(cy + rad * math.sin(ang), 6), H - 6)
            out.append(circ(x, y, 1.8 + next(r) * 1.4, T.a(i), op=.9))
        out.append("</g>")
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
    for k in range(1, 6):  # data-mer, data-globe: what an animated page turns (T-3734)
        out.append(f'<ellipse data-mer="{k}" cx="{N(cx)}" cy="{N(cy)}" rx="{N(R * abs(math.cos(k * math.pi / 6)))}" ry="{N(R)}" '
                   f'fill="none" stroke="{T.a(0)}" opacity=".35"/>')
    n = int(d.get("dots", 160))
    r = rng("globe")
    out.append(f'<g data-globe="{N(cx)} {N(cy)} {N(R)} {n}" data-c="{" ".join(T.a(k) for k in range(1, 5))}">')
    for i in range(n):
        y = 1 - 2 * (i + .5) / n
        s, th = math.sqrt(1 - y * y), i * 2.39996
        x, z = s * math.cos(th), s * math.sin(th)
        if z > 0:
            out.append(circ(cx + R * x, cy - R * y, 1.3 + 1.2 * next(r), T.a(1 + i % 4), op=round(.45 + .5 * z, 2)))
    out.append("</g>")
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


# ---------------------------------------------------------------- the moving kinds (T-3736)
# Each draws a still frame that reads alone (a renderer without SMIL shows the whole picture), then SMIL loops over it:
# every animation repeats indefinitely and starts in phase with where its element is drawn.

def _num(v):
    """A count as drawn: 412, 1.2k, 3.5: the one form a reader and a test both recompute."""
    if v >= 1000:
        return f"{v / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(int(v)) if float(v).is_integer() else N(v)


def _s(v):
    return format(round(v, 2), "g") + "s"


def _anim(attr, values, dur, begin=0.0, times=None, calc=None):
    return (f'<animate attributeName="{attr}" values="{values}" dur="{_s(dur)}"' + (f' begin="{_s(begin)}"' if begin else "")
            + (f' keyTimes="{times}"' if times else "") + (f' calcMode="{calc}"' if calc else "") + ' repeatCount="indefinite"/>')


def _ride(d, dur, begin=0.0, points=None, times=None):
    """An animateMotion along d, offsets from where its element is drawn."""
    return (f'<animateMotion path="{d}" dur="{_s(dur)}"' + (f' begin="{_s(begin)}"' if begin else "")
            + (f' keyPoints="{points}" keyTimes="{times}" calcMode="linear"' if points else "") + ' repeatCount="indefinite"/>')


def _spin(cx, cy, dur, rev=False):
    return (f'<animateTransform attributeName="transform" type="rotate" from="0 {N(cx)} {N(cy)}" '
            f'to="{-360 if rev else 360} {N(cx)} {N(cy)}" dur="{_s(dur)}" repeatCount="indefinite"/>')


def _with(el, *kids):
    """A self-closed svg element holding kids: an animation inside the shape it moves."""
    return el[:-2] + ">" + "".join(kids) + f"</{el[1:el.index(' ')]}>"


def _keys(pairs):
    """(keyTimes, values) from [(t, v)]: times clamped to 0..1 and kept in order, a repeat time keeps its last value."""
    out = []
    for t, v in pairs:
        t = min(max(t, out[-1][0] if out else 0), 1)
        if out and t == out[-1][0]:
            out[-1] = (t, v)
        else:
            out.append((t, v))
    out[0] = (0, out[0][1])
    if out[-1][0] < 1:
        out.append((1, out[-1][1]))
    return ";".join(format(round(t, 4), "g") for t, _ in out), ";".join(str(v) for _, v in out)


def _qpoly(a, c, b, n=18):
    return [((1 - t) ** 2 * a[0] + 2 * (1 - t) * t * c[0] + t * t * b[0], (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * c[1] + t * t * b[1])
            for t in (i / n for i in range(n + 1))]


def _at(poly, f):
    """The point at fraction f (0..1) of poly's length."""
    segs = [math.dist(p, q) for p, q in zip(poly, poly[1:])]
    goal = sum(segs) * min(max(f, 0), 1)
    for (p, q), s in zip(zip(poly, poly[1:]), segs):
        if goal <= s and s > 0:
            return p[0] + (q[0] - p[0]) * goal / s, p[1] + (q[1] - p[1]) * goal / s
        goal -= s
    return poly[-1]


def _rider(poly, f, dur, el, fade=False):
    """el(x, y) drawn at fraction f of poly and riding it, looping, in phase with where it is drawn; fade: it is lost
    past halfway (a dropped packet)."""
    x, y = _at(poly, f)
    d = "M" + "L".join(f"{N(px - x)} {N(py - y)}" for px, py in poly)
    kids = [_ride(d, dur, -f * dur)] + ([_anim("opacity", "1;1;0;0", dur, -f * dur, "0;.5;.6;1")] if fade else [])
    return _with(el(x, y), *kids)


def _blink(el, dur, phase=0.0, lo=.15):
    return _with(el, _anim("opacity", f"1;{lo};1", dur, -(phase % 1) * dur))


def _edge(c, w, h, toward, gap=3):
    """Where the ray from c toward a point leaves a w x h box centred on c, gap px further out."""
    dx, dy = toward[0] - c[0], toward[1] - c[1]
    ln = math.hypot(dx, dy) or 1
    s = min((w / 2) / abs(dx) if dx else 1e9, (h / 2) / abs(dy) if dy else 1e9)
    return c[0] + dx * s + dx / ln * gap, c[1] + dy * s + dy / ln * gap


def _head(tip, frm, c, size=7):
    """A filled arrowhead at tip, pointing away from frm."""
    dx, dy = tip[0] - frm[0], tip[1] - frm[1]
    ln = math.hypot(dx, dy) or 1
    ux, uy = dx / ln, dy / ln
    bx, by = tip[0] - ux * size, tip[1] - uy * size
    return path(f"M{N(tip[0])} {N(tip[1])}L{N(bx - uy * size * .55)} {N(by + ux * size * .55)}"
                f"L{N(bx + uy * size * .55)} {N(by - ux * size * .55)}Z", None, fill=c)


def _force(n, edges, seed, iters=160):
    """n points in the unit square for nodes joined by edges (index pairs): a deterministic spring layout, spread to fill."""
    r = rng(seed)
    pos = [[.5 + .4 * math.cos(2 * math.pi * i / n) + (next(r) - .5) * .1,
            .5 + .4 * math.sin(2 * math.pi * i / n) + (next(r) - .5) * .1] for i in range(n)]
    k = math.sqrt(1 / n) * .9
    for it in range(iters):
        temp = .08 * (1 - it / iters) + .002
        disp = [[(.5 - p[0]) * .2, (.5 - p[1]) * .2] for p in pos]
        for i in range(n):
            for j in range(i + 1, n):
                dx, dy = pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]
                f = k * k / max(dx * dx + dy * dy, 1e-4)
                disp[i][0] += dx * f
                disp[i][1] += dy * f
                disp[j][0] -= dx * f
                disp[j][1] -= dy * f
        for a, b in edges:
            dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
            f = math.hypot(dx, dy) / k
            disp[a][0] -= dx * f
            disp[a][1] -= dy * f
            disp[b][0] += dx * f
            disp[b][1] += dy * f
        for i in range(n):
            m = math.hypot(*disp[i]) or 1
            step = min(m, temp)
            pos[i] = [min(max(pos[i][0] + disp[i][0] / m * step, 0), 1), min(max(pos[i][1] + disp[i][1] / m * step, 0), 1)]
    xs, ys = [p[0] for p in pos], [p[1] for p in pos]
    sx, sy = max(max(xs) - min(xs), 1e-6), max(max(ys) - min(ys), 1e-6)
    return [((p[0] - min(xs)) / sx, (p[1] - min(ys)) / sy) for p in pos]


def _heat(h):
    return 2 if h >= 60 else 1 if h >= 30 else 0


def _constellation_refs(d, where):
    names, errs = [m["name"] for m in d["modules"]], []
    for i, pair in enumerate(d.get("imports", [])):
        for j, v in enumerate(pair):
            errs += _named(names, v, f"{where}.imports[{i}][{j}]", "modules")
    return errs


@kind("constellation", "a codebase as a night sky: modules are stars sized by weight and coloured by heat, imports are "
      "the lines between them with traffic riding them",
      OBJ({"modules": ARR(OBJ({"name": STR(18), "heat": NUM(0, 100), "size": NUM(1, 100)}, ["name"]), 2, 16),
           "imports": ARR(ARR(STR(18), 2, 2), 0, 32)}, ["modules"]),
      {"modules": [{"name": "cli", "heat": 72, "size": 60}, {"name": "core", "heat": 85, "size": 90},
                   {"name": "store", "heat": 40, "size": 55}, {"name": "tasks", "heat": 66, "size": 70},
                   {"name": "lanes", "heat": 52, "size": 50}, {"name": "hooks", "heat": 18, "size": 30},
                   {"name": "check", "heat": 35, "size": 45}, {"name": "board", "heat": 24, "size": 40},
                   {"name": "git", "heat": 12, "size": 35}, {"name": "util", "heat": 8, "size": 25}],
       "imports": [["cli", "core"], ["cli", "tasks"], ["cli", "lanes"], ["cli", "check"], ["tasks", "core"],
                   ["tasks", "store"], ["lanes", "git"], ["lanes", "tasks"], ["hooks", "core"], ["check", "core"],
                   ["board", "store"], ["board", "tasks"], ["core", "store"], ["core", "util"], ["store", "util"],
                   ["git", "util"]]},
      refs=_constellation_refs)
def _constellation(d, W, H, T):
    mods, out = d["modules"], []
    idx = {m["name"]: i for i, m in enumerate(mods)}
    edges = [(idx[a], idx[b]) for a, b in d.get("imports", []) if a in idx and b in idx and a != b]
    mx = min(60, W * .12)
    P = [(mx + (W - 2 * mx) * x, 24 + (H - 54) * y) for x, y in _force(len(mods), edges, "|".join(idx))]
    glow = T.ink if T.scheme == "dark" else T.panel
    r = rng(f"sky{len(mods)}{int(W)}")
    for k in range(min(int(W * H / 2400), 140)):  # the field: faint stars, one in six twinkling
        x, y, s, ph = 2 + next(r) * (W - 4), 2 + next(r) * (H - 4), next(r), next(r)
        dot = circ(x, y, .6 + s * .9, T.muted, op=round(.25 + s * .4, 2))
        out.append(_blink(dot, 2 + s * 4, ph, .1) if k % 6 == 0 else dot)
    for j, (a, b) in enumerate(edges):  # the figure's lines, an import riding each from the importer to the imported
        c = T.a(_heat(mods[a].get("heat", 0)))
        out.append(line(*P[a], *P[b], c, 1, .4))
        out.append(_rider([P[a], P[b]], (j * .37) % 1, 2.4 + (j % 4) * .5, lambda x, y, c=c: circ(x, y, 1.8, c)))
    for i, m in enumerate(mods):
        (x, y), h = P[i], m.get("heat", 0)
        c, rad = T.a(_heat(h)), 3 + m.get("size", 30) / 100 * 5
        if h >= 60:  # a hot star breathes
            out.append(_with(circ(x, y, rad * 2.2, c, op=.2), _anim("r", f"{N(rad * 1.5)};{N(rad * 2.8)};{N(rad * 1.5)}", 2.4 + i % 3 * .4),
                             _anim("opacity", ".3;.05;.3", 2.4 + i % 3 * .4)))
        else:
            out.append(circ(x, y, rad * 1.8, c, op=.1))
        out += [path(f"M{N(x - rad * 2.1)} {N(y)}H{N(x + rad * 2.1)}M{N(x)} {N(y - rad * 2.1)}V{N(y + rad * 2.1)}", c, 1, op=.55),
                circ(x, y, rad, c), circ(x, y, rad * .38, glow, op=.85)]
        ly = y + rad + 14 if y < H - 26 else y - rad - 8
        out.append(txt(x, ly, m["name"], T.ink, SZ, "middle", 700, maxw=max(W / 5, 60)))
        if "heat" in m:
            out.append(txt(x + rad + 5, y - rad - 1, _num(h), T.txt(_heat(h)), SZ))
    return out


def _sequence_refs(d, where):
    errs = []
    for i, m in enumerate(d["messages"]):
        for k in ("from", "to"):
            errs += _named(d["actors"], m[k], f"{where}.messages[{i}].{k}", "actors")
    return errs


@kind("sequence", "swimlanes: actors on lifelines, each message a timed arrow between them, replayed in order over a "
      "timeline of when each one ran",
      OBJ({"actors": ARR(STR(12), 2, 6),
           "messages": ARR(OBJ({"from": STR(12), "to": STR(12), "label": STR(20), "at": NUM(0, 1e6), "took": NUM(0, 1e6),
                                "reply": {"type": "boolean"}}, ["from", "to", "label", "at"]), 1, 14)}, ["actors", "messages"]),
      {"actors": ["browser", "api", "auth", "db", "queue"],
       "messages": [{"from": "browser", "to": "api", "label": "POST /checkout", "at": 0, "took": 12},
                    {"from": "api", "to": "auth", "label": "verify token", "at": 14, "took": 38},
                    {"from": "auth", "to": "api", "label": "200 ok", "at": 54, "took": 4, "reply": True},
                    {"from": "api", "to": "db", "label": "INSERT order", "at": 62, "took": 85},
                    {"from": "db", "to": "api", "label": "order 8841", "at": 150, "took": 3, "reply": True},
                    {"from": "api", "to": "queue", "label": "enqueue email", "at": 158, "took": 20},
                    {"from": "api", "to": "browser", "label": "201 Created", "at": 182, "took": 9, "reply": True}]},
      size=(640, 420), refs=_sequence_refs)
def _sequence(d, W, H, T):
    acts, out = d["actors"], []
    msgs = [m for m in d["messages"] if m["from"] in acts and m["to"] in acts]
    n, m = len(acts), max(len(msgs), 1)
    cw = W / n
    X = {a: cw * (j + .5) for j, a in enumerate(acts)}
    col = {a: T.a(j) for j, a in enumerate(acts)}
    total = max([x["at"] + x.get("took", 0) for x in msgs] + [1])
    top, bot = 32, H - 34
    rh = (bot - top) / m
    C = m * 1.1  # one replay of every message, in order
    for a in acts:
        bw = min(cw - 8, tw(a) + 22)
        out += [line(X[a], 22, X[a], bot + 2, T.line, 1, dash="3 4"),
                rect(X[a] - bw / 2, 0, bw, 22, T.panel, col[a], 4, 1.25), txt(X[a], 15, a, T.ink, SZ, "middle", 700, maxw=bw - 8)]
    band = rect(0, top, W, rh, T.a(0), rx=2, op=.07)
    out.append(_with(band, _anim("y", ";".join(N(top + rh * i) for i in range(m)), C, calc="discrete")))
    for i, x in enumerate(msgs):  # the callee's activation bar, from the call until it replies (or one row, if it never does)
        if not x.get("reply") and x["from"] != x["to"]:
            j = next((j for j in range(i + 1, len(msgs)) if msgs[j].get("reply") and msgs[j]["from"] == x["to"]
                      and msgs[j]["to"] == x["from"]), None)
            span = rh * (j - i) if j is not None else rh * .8
            out.append(rect(X[x["to"]] - 4, top + rh * (i + .5) - 2, 8, span + 4, T.panel, col[x["to"]], 2))
    for i, x in enumerate(msgs):
        y, a, b, c = top + rh * (i + .5), X[x["from"]], X[x["to"]], col[x["from"]]
        if a == b:  # to itself: a loop out to the right and back
            pts = [(a + 4, y - 4), (a + 26, y - 4), (a + 26, y + 6), (a + 6, y + 6)]
            lx, anchor, lw = a + 30, "start", cw / 2 - 34
        else:
            s = 1 if b > a else -1
            pts = [(a + s * 4, y), (b - s * 5, y)]
            lx, anchor, lw = (a + b) / 2, "middle", abs(b - a) - 16
        dd = "M" + "L".join(f"{N(px)} {N(py)}" for px, py in pts)
        out.append('<path class="msg"' + path(dd, c, 1.5, op=.85 if x.get("reply") else None,
                                               dash="5 4" if x.get("reply") else None)[5:])
        out.append(_head(pts[-1], pts[-2], c))
        out.append(txt(lx, y - 5, x["label"], T.ink, SZ, anchor, maxw=max(lw, 30)))
        if "took" in x and rh >= 28:
            out.append(txt(lx, y + 13, f"{_num(x['took'])}ms", T.muted, SZ, anchor))
        lo, hi = i / m, (i + 1) / m  # the token rides its arrow in its own slot of the replay
        kt, kp = _keys([(0, 0), (lo, 0), (hi - .02, 1), (1, 1)])
        ko, vo = _keys([(0, 0), (lo, 0), (lo + .01, 1), (hi - .02, 1), (hi, 0), (1, 0)])
        x0, y0 = pts[0]
        rel = "M" + "L".join(f"{N(px - x0)} {N(py - y0)}" for px, py in pts)
        out.append(_with(circ(x0, y0, 3.5, c, op=0), _ride(rel, C, 0, kp, kt), _anim("opacity", vo, C, times=ko)))
    ty = H - 20  # the timeline: when each message ran, a cursor keeping time with the replay
    out.append(rect(0, ty, W, 6, T.panel, T.line, 3))
    for x in msgs:
        x0 = W * x["at"] / total
        out.append(rect(min(x0, W - 2), ty, max(2, min(W * x.get("took", 0) / total, W - x0)), 6, col[x["from"]], rx=2, op=.85))
    kt, kv = _keys([(i / m, N(min(W * x["at"] / total, W - 2))) for i, x in enumerate(msgs)] + [(1, N(W - 2))])
    out += [_with(rect(0, ty - 4, 2, 14, T.ink, rx=1), _anim("x", kv, C, times=kt)),
            txt(0, H - 1, "0ms", T.muted, SZ), txt(W, H - 1, f"{_num(total)}ms", T.ink, SZ, "end", 700)]
    return out


def _statemachine_refs(d, where):
    s = d["states"]
    errs = _named(s, d["current"], f"{where}.current", "states")
    for i, f in enumerate(d.get("final", [])):
        errs += _named(s, f, f"{where}.final[{i}]", "states")
    for i, t in enumerate(d["transitions"]):
        for k in ("from", "to"):
            errs += _named(s, t[k], f"{where}.transitions[{i}].{k}", "states")
    return errs


_SM_HUES = (0, 5, 4, 1, 2)  # a state's hue; green is kept for the one that is lit


@kind("statemachine", "a state machine: states on a ring, transitions as arrows weighted and counted by how often "
      "they fire, the current state lit",
      OBJ({"states": ARR(STR(14), 2, 8), "current": STR(14), "final": ARR(STR(14), 0, 4),
           "transitions": ARR(OBJ({"from": STR(14), "to": STR(14), "on": STR(14, 0), "count": NUM(0, 1e9)}, ["from", "to"]), 1, 20)},
          ["states", "current", "transitions"]),
      {"states": ["inbox", "ready", "doing", "review", "done", "blocked"], "current": "doing", "final": ["done"],
       "transitions": [{"from": "inbox", "to": "ready", "on": "triage", "count": 412},
                       {"from": "ready", "to": "doing", "on": "focus", "count": 380},
                       {"from": "doing", "to": "review", "on": "submit", "count": 341},
                       {"from": "review", "to": "done", "on": "accept", "count": 296},
                       {"from": "review", "to": "doing", "on": "rework", "count": 45},
                       {"from": "doing", "to": "blocked", "on": "block", "count": 38},
                       {"from": "blocked", "to": "doing", "on": "unblock", "count": 31}]},
      refs=_statemachine_refs)
def _statemachine(d, W, H, T):
    st, out = d["states"], []
    n = len(st)
    pw = {s: min(tw(s) + 26, 132) for s in st}
    cx, cy = W / 2, H / 2 + 6
    rx, ry = max(40, W / 2 - max(pw.values()) / 2 - 8), max(30, H / 2 - 30)
    P = {s: (cx + rx * math.cos(math.pi + 2 * math.pi * i / n), cy + ry * math.sin(math.pi + 2 * math.pi * i / n))
         for i, s in enumerate(st)}
    fin = set(d.get("final", []))
    hues = {s: 3 if s in fin else _SM_HUES[i % len(_SM_HUES)] for i, s in enumerate(st)}  # where work ends well is green
    hue = {s: T.a(hues[s]) for s in st}
    tr = [t for t in d["transitions"] if t["from"] in P and t["to"] in P]
    mc = max([t.get("count", 0) for t in tr] + [1])
    labels = []
    for i, t in enumerate(tr):
        c, A, B = hue[t["from"]], P[t["from"]], P[t["to"]]
        if t["from"] == t["to"]:  # a self-loop stands out from the ring
            ln = math.hypot(A[0] - cx, A[1] - cy) or 1
            ox, oy = (A[0] - cx) / ln, (A[1] - cy) / ln
            a0 = _edge(A, pw[t["from"]], 22, (A[0] + ox - oy * .5, A[1] + oy + ox * .5), 1)
            a1 = _edge(A, pw[t["from"]], 22, (A[0] + ox + oy * .5, A[1] + oy - ox * .5), 1)
            c1 = (a0[0] + ox * 34 - oy * 16, a0[1] + oy * 34 + ox * 16)
            c2 = (a1[0] + ox * 34 + oy * 16, a1[1] + oy * 34 - ox * 16)
            poly = [bez(a0, c1, c2, a1, k / 18) for k in range(19)]
            dd = f"M{N(a0[0])} {N(a0[1])}C{N(c1[0])} {N(c1[1])} {N(c2[0])} {N(c2[1])} {N(a1[0])} {N(a1[1])}"
        else:
            dx, dy = B[0] - A[0], B[1] - A[1]
            ln = math.hypot(dx, dy) or 1
            off = min(ln * .2, 34)  # a pair of opposite transitions bow apart; a long one across the ring stays near straight
            C = ((A[0] + B[0]) / 2 - dy / ln * off, (A[1] + B[1]) / 2 + dx / ln * off)
            a0, a1 = _edge(A, pw[t["from"]], 22, C), _edge(B, pw[t["to"]], 22, C, 4)
            poly = _qpoly(a0, C, a1)
            dd = f"M{N(a0[0])} {N(a0[1])}Q{N(C[0])} {N(C[1])} {N(a1[0])} {N(a1[1])}"
        k = t.get("count", 0) / mc
        out += [path(dd, c, 1 + 3 * k, op=.75), _head(poly[-1], poly[-3], c, 8)]
        out.append(_rider(poly, (i * .29) % 1, 1.4 + 2.6 * (1 - k), lambda x, y, c=c: circ(x, y, 2.8, c)))
        labels.append((poly[len(poly) // 2], t, hues[t["from"]]))
    placed = []
    for (mx, my), t, hi in labels:  # the event and how often it fired, on a chip over the arrow's middle
        on, cnt = t.get("on", ""), _num(t["count"]) if "count" in t else ""
        if not (on or cnt):
            continue
        w1, w2 = tw(on) if on else 0, tw(cnt)
        wid = w1 + w2 + (6 if on and cnt else 0) + 10
        mx = min(max(mx, wid / 2 + 1), W - wid / 2 - 1)
        x0 = mx - wid / 2
        for _ in range(len(placed)):  # a chip that lands on one already placed steps clear of it
            hit = next((b for b in placed if x0 < b[0] + b[2] + 2 and b[0] < x0 + wid + 2 and abs(my - b[1]) < 18), None)
            if not hit:
                break
            my = hit[1] + (18 if my >= hit[1] else -18)
        placed.append((x0, my, wid))
        out.append(rect(x0, my - 9, wid, 16, T.bg, T.line, 3, op=.92))
        if on:
            out.append(txt(x0 + 5, my + 3, on, T.ink, SZ))
        if cnt:
            out.append(txt(x0 + wid - 5, my + 3, cnt, T.txt(hi), SZ, "end", 700))
    cur = d["current"]
    for s in st:
        (x, y), w, c = P[s], pw[s], hue[s]
        if s in fin:  # a final state: a double border
            out.append(rect(x - w / 2 - 3, y - 14, w + 6, 28, "none", c, 14, 1.25))
        if s == cur:
            g = T.a(3)
            halo = rect(x - w / 2 - 4, y - 15, w + 8, 30, "none", g, 15, 2)
            out += [_with(halo, _anim("opacity", ".9;0;.9", 1.8), _anim("stroke-width", "1;8;1", 1.8)),
                    rect(x - w / 2, y - 11, w, 22, g, rx=11), txt(x, y + 4, s, T.on(g), SZ, "middle", 700, maxw=w - 10),
                    txt(x, y - 19, "NOW", T.txt(3), SZ, "middle", 700, .9)]
        else:
            out += [rect(x - w / 2, y - 11, w, 22, T.panel, c, 11, 1.5), txt(x, y + 4, s, T.ink, SZ, "middle", maxw=w - 10)]
    return out


@kind("conveyor", "an assembly line: queued jobs ride a moving belt into machines that work them, a progress bar on "
      "each, finished crates dropping into a done bin",
      OBJ({"source": STR(14, 0), "queue": ARR(STR(12), 0, 12),
           "workers": ARR(OBJ({"label": STR(12), "job": STR(12, 0), "progress": NUM(0, 100)}, ["label"]), 1, 5),
           "done": NUM(0, 1e9)}, ["queue", "workers"]),
      {"source": "inbox", "queue": [f"T-{n}" for n in range(101, 108)],
       "workers": [{"label": "lane 1", "job": "T-098", "progress": 72}, {"label": "lane 2", "job": "T-099", "progress": 35},
                   {"label": "lane 3", "job": "T-100", "progress": 8}], "done": 41},
      size=(640, 320))
def _conveyor(d, W, H, T):
    q, wk, out = d.get("queue", []), d["workers"], []
    by = H * .64  # the belt's top surface
    bin_w = 66 if W >= 520 else 54
    qw = max(84, W * .26)
    end = W - bin_w + 6
    out.append(line(0, by + 34, end, by + 34, T.line, 1))  # the floor
    hb_ = by - 50  # the hopper the work comes from, a job sliding down its chute onto the belt
    hx, ht = qw / 2, max(4, hb_ - 76)
    if hb_ - ht >= 34:
        hw = min(qw - 10, 96)
        out += [path(f"M{N(hx - hw / 2)} {N(ht)}H{N(hx + hw / 2)}L{N(hx + 13)} {N(hb_ - 10)}V{N(hb_)}H{N(hx - 13)}V{N(hb_ - 10)}Z",
                     T.a(1), 1.5, T.panel)]
        if d.get("source"):
            out.append(txt(hx, ht + 17, d["source"], T.ink, SZ, "middle", 700, maxw=hw - 14))
        drip = rect(hx - 6, hb_ - 12, 12, 10, T.bg, T.a(1), 2, 1.25)
        out.append(_with(drip, _ride(f"M0 0V{N(by - 10 - hb_)}", 1.8), _anim("opacity", "0;1;1;0", 1.8, times="0;.15;.85;1")))
    elif d.get("source"):
        out.append(txt(0, by + 50, d["source"], T.muted, SZ, maxw=qw))
    for x in range(14, int(end) - 4, 40):  # rollers under the belt, turning
        hub = circ(x, by + 20, 5.5, T.panel, T.line, 1.5)
        spokes = path(f"M{N(x - 5)} {N(by + 20)}H{N(x + 5)}M{N(x)} {N(by + 15)}V{N(by + 25)}", T.muted, 1.2)
        out += [line(x, by + 26, x, by + 34, T.line, 1.5), hub, f"<g>{spokes}{_spin(x, by + 20, 1.6)}</g>"]
    out += [rect(0, by, end, 12, T.panel, T.line, 6, 1.5),
            _with(path(f"M4 {N(by + 6)}H{N(end - 4)}", T.muted, 2, dash="6 6"), _anim("stroke-dashoffset", "12;0", .8))]
    out += [txt(0, by - 32, "QUEUED", T.muted, SZ, weight=700, track=.9),
            txt(tw("QUEUED", SZ, .9) + 6, by - 32, str(len(q)), T.txt(1), SZ, weight=700)]
    xr = qw
    for j, lab in enumerate(q):  # the head of the queue nearest the machines
        cwid = tw(lab) + 10
        if xr - cwid < 0:
            break
        crate = [rect(xr - cwid, by - 20, cwid, 20, T.bg, T.a(1), 2, 1.25), txt(xr - cwid / 2, by - 6, lab, T.ink, SZ, "middle")]
        out += [_with(crate[0], _anim("y", f"{N(by - 20)};{N(by - 22)};{N(by - 20)}", .9, -j * .13)), crate[1]] if j == 0 else crate
        xr -= cwid + 4
    m0, m1 = qw + 14, W - bin_w - 8
    slot = (m1 - m0) / len(wk)
    hb = by - 30  # the machines' housing bottom
    for i, w in enumerate(wk):
        x, mw, c = m0 + slot * i + 4, slot - 8, T.a((0, 5, 4, 3, 2)[i % 5])
        p = w.get("progress", 0)
        out += [line(x + 6, hb, x + 6, by, T.line, 2), line(x + mw - 6, hb, x + mw - 6, by, T.line, 2),
                rect(x, 2, mw, hb - 2, T.panel, c, 5, 1.5), txt(x + 8, 19, w["label"], T.ink, SZ, weight=700, maxw=mw - 24),
                _blink(circ(x + mw - 10, 14, 3.5, c), .7 + i * .25, i * .3)]
        if w.get("job"):
            out.append(txt(x + 8, 34, w["job"], T.muted, SZ, maxw=mw - 16))
        bw = mw - 16
        out += [rect(x + 8, 42, bw, 6, T.bg, T.line, 3)]
        if p > 0:
            out.append(rect(x + 8, 42, bw * p / 100, 6, c, rx=3))
        if bw * p / 100 > 14:  # a glint running along what is done
            out.append(_with(rect(x + 8, 42, 8, 6, T.ink, rx=3, op=.35), _anim("x", f"{N(x + 8)};{N(x + 8 + bw * p / 100 - 8)}", 1.6, -i * .4)))
        out.append(txt(x + mw - 8, 64, f"{_num(p)}%", T.txt((0, 5, 4, 3, 2)[i % 5]), SZ, "end", 700))
        if hb - 2 >= 84:  # a gear turning in the housing
            gx, gy = x + 18, hb - 16
            gear = circ(gx, gy, 7, "none", T.muted, 4)
            out += [f"<g>{gear[:-2]} stroke-dasharray=\"3 2.5\"/>{_spin(gx, gy, 3 + i)}</g>", circ(gx, gy, 2.5, T.muted)]
        press = rect(x + mw / 2 - 4, hb, 8, 6, T.muted, rx=1)  # the press over the crate it works
        out.append(_with(press, _anim("height", "4;10;4", 1.1 + i * .2, -i * .3)))
        if w.get("job"):
            cwid = min(mw - 8, tw(w["job"]) + 10)
            out += [rect(x + mw / 2 - cwid / 2, by - 20, cwid, 20, T.bg, c, 2, 1.25),
                    txt(x + mw / 2, by - 6, w["job"], T.ink, SZ, "middle", maxw=cwid - 4)]
    bx0, bx1, bt, bb = end + 2, W - 2, by + 6, H - 4  # the done bin, a crate tipping into it
    out += [path(f"M{N(bx0)} {N(bt)}L{N(bx0 + 6)} {N(bb)}H{N(bx1 - 6)}L{N(bx1)} {N(bt)}", T.a(3), 1.75, T.panel),
            txt((bx0 + bx1) / 2, by - 22, "DONE", T.muted, SZ, "middle", 700, .9),
            txt((bx0 + bx1) / 2, (bt + bb) / 2 + 5, _num(d.get("done", 0)), T.txt(3), SZ, "middle", 700)]
    drop = rect(end - 22, by - 12, 12, 12, T.bg, T.a(3), 2, 1.25)
    fall = f"M0 0H18Q{N(26)} 0 {N(30)} {N(min(26, bb - by - 14))}"
    out.append(_with(drop, _ride(fall, 2.2), _anim("opacity", "1;1;0", 2.2, times="0;.8;1")))
    return out


_HOSTS = ("router", "switch", "server", "db", "client", "cloud")


def _network_refs(d, where):
    ids, errs = [h["id"] for h in d["hosts"]], []
    for i, ln in enumerate(d["links"]):
        for k in ("from", "to"):
            errs += _named(ids, ln[k], f"{where}.links[{i}].{k}", "host ids")
    return errs


def _device(kd, x, y, c, T, i):
    """A host's glyph, about 32 px across, centred on (x, y), with its lights blinking."""
    P_, ph = T.panel, i * .37
    if kd == "router":
        arrows = "".join(f"M{N(x - 6 * sx)} {N(y - 6 * sy)}L{N(x + 6 * sx)} {N(y + 6 * sy)}"
                         f"M{N(x + 6 * sx - 4 * sx)} {N(y + 6 * sy)}H{N(x + 6 * sx)}V{N(y + 6 * sy - 4 * sy)}"
                         f"M{N(x - 6 * sx + 4 * sx)} {N(y - 6 * sy)}H{N(x - 6 * sx)}V{N(y - 6 * sy + 4 * sy)}"
                         for sx, sy in ((1, 1), (-1, 1)))
        return [circ(x, y, 14, P_, c, 1.75), path(arrows, c, 1.5), _blink(circ(x + 11, y - 11, 2.4, c), .9, ph)]
    if kd == "switch":
        return [rect(x - 18, y - 9, 36, 18, P_, c, 3, 1.75),
                path(f"M{N(x - 10)} {N(y - 3)}H{N(x + 9)}M{N(x + 6)} {N(y - 6)}L{N(x + 9)} {N(y - 3)}L{N(x + 6)} {N(y)}"
                     f"M{N(x + 10)} {N(y + 3)}H{N(x - 9)}M{N(x - 6)} {N(y)}L{N(x - 9)} {N(y + 3)}L{N(x - 6)} {N(y + 6)}", c, 1.25)] + \
            [_blink(circ(x - 13 + 6.5 * k, y + 13, 1.8, c), .5 + k * .23, ph + k * .3) for k in range(5)]
    if kd == "server":
        out = [rect(x - 11, y - 16, 22, 32, P_, c, 2, 1.75)]
        for k in range(3):
            yy = y - 10 + k * 10
            out += [line(x - 7, yy, x + 2, yy, c, 1.25), _blink(circ(x + 6, yy, 1.8, c), .6 + k * .3, ph + k * .27)]
        return out
    if kd == "db":
        body = (f"M{N(x - 13)} {N(y - 10)}V{N(y + 10)}A13 4.5 0 0 0 {N(x + 13)} {N(y + 10)}V{N(y - 10)}"
                f"A13 4.5 0 0 0 {N(x - 13)} {N(y - 10)}Z")
        return [path(body, c, 1.75, P_), path(f"M{N(x - 13)} {N(y - 10)}A13 4.5 0 0 1 {N(x + 13)} {N(y - 10)}", c, 1.25),
                path(f"M{N(x - 13)} {N(y)}A13 4.5 0 0 0 {N(x + 13)} {N(y)}", c, 1, op=.6),
                _blink(circ(x + 8, y + 5, 1.8, c), 1.3, ph)]
    if kd == "client":
        screen = rect(x - 12, y - 10, 24, 13, c, rx=1, op=.25)
        return [rect(x - 15, y - 13, 30, 19, P_, c, 2, 1.75), _with(screen, _anim("opacity", ".15;.4;.15", 2.6, -ph)),
                path(f"M{N(x)} {N(y + 6)}V{N(y + 11)}M{N(x - 7)} {N(y + 12)}H{N(x + 7)}", c, 1.75),
                _blink(rect(x - 8, y - 6, 2, 6, c, rx=0), 1, ph)]
    cloud = (f"M{N(x - 15)} {N(y + 8)}H{N(x + 15)}A6.5 6.5 0 0 0 {N(x + 11)} {N(y - 3)}A9 9 0 0 0 {N(x - 4)} {N(y - 8)}"
             f"A9 9 0 0 0 {N(x - 15)} {N(y + 8)}Z")
    return [path(cloud, c, 1.75, P_), _with(path(cloud, c, 1, dash="3 4", op=.6), _anim("stroke-dashoffset", "14;0", 2.4)),
            _blink(circ(x, y + 2, 2, c), 1.6, ph)]


@kind("network", "a network topology: devices drawn as what they are (cloud, router, switch, servers, databases, "
      "clients), each link marked with its latency and packets crossing it, a lossy one dashed and dropping some",
      OBJ({"hosts": ARR(OBJ({"id": STR(14), "kind": {"type": "string", "enum": list(_HOSTS)}, "label": STR(16, 0)},
                            ["id", "kind"]), 2, 12),
           "links": ARR(OBJ({"from": STR(14), "to": STR(14), "ms": NUM(0, 1e5), "loss": NUM(0, 100)}, ["from", "to"]), 1, 20)},
          ["hosts", "links"]),
      {"hosts": [{"id": "internet", "kind": "cloud", "label": "internet"}, {"id": "edge-rtr", "kind": "router", "label": "edge router"},
                 {"id": "core-sw", "kind": "switch", "label": "core switch"}, {"id": "web-1", "kind": "server", "label": "web-1"},
                 {"id": "web-2", "kind": "server", "label": "web-2"}, {"id": "pg-main", "kind": "db", "label": "postgres"},
                 {"id": "office", "kind": "client", "label": "office"}],
       "links": [{"from": "internet", "to": "edge-rtr", "ms": 34}, {"from": "edge-rtr", "to": "core-sw", "ms": 2},
                 {"from": "core-sw", "to": "web-1", "ms": 1}, {"from": "core-sw", "to": "web-2", "ms": 1},
                 {"from": "web-1", "to": "pg-main", "ms": 3}, {"from": "web-2", "to": "pg-main", "ms": 4, "loss": 2},
                 {"from": "office", "to": "edge-rtr", "ms": 120, "loss": 3}]},
      refs=_network_refs)
def _network(d, W, H, T):
    hosts, out = d["hosts"], []
    ids = [h["id"] for h in hosts]
    links = [ln for ln in d["links"] if ln["from"] in ids and ln["to"] in ids and ln["from"] != ln["to"]]
    adj = {i: [] for i in ids}
    for ln in links:
        adj[ln["from"]].append(ln["to"])
        adj[ln["to"]].append(ln["from"])
    root = next((h["id"] for h in hosts if h["kind"] == "cloud"), ids[0])
    tier, todo = {root: 0}, [root]
    while todo:  # tiers by hops from the outside world
        x = todo.pop(0)
        for y in adj[x]:
            if y not in tier:
                tier[y] = tier[x] + 1
                todo.append(y)
    far = max(tier.values()) + 1
    for i in ids:
        tier.setdefault(i, far)
    nt = max(tier.values()) + 1
    cols = [[i for i in ids if tier[i] == t] for t in range(nt)]
    P = {}
    for t, col in enumerate(cols):
        for j, i in enumerate(col):
            P[i] = (W * (t + .5) / nt, 18 + (H - 50) * (j + .5) / len(col))
    hue = {k: T.a(j) for j, k in enumerate(("cloud", "router", "switch", "server", "db", "client"))}
    for j, ln in enumerate(links):
        ms = ln.get("ms", 0)
        c = T.a(3 if ms < 20 else 1 if ms < 80 else 2)
        A, B, lossy = P[ln["from"]], P[ln["to"]], bool(ln.get("loss"))
        out.append(line(*A, *B, c, 2, .7, dash="6 4" if lossy else None))
        dur = 1.2 + min(ms, 200) / 200 * 2.4
        out += [_rider([A, B], (j * .31) % 1, dur, lambda x, y, c=c: circ(x, y, 2.8, c), lossy),
                _rider([B, A], (j * .31 + .5) % 1, dur * 1.15, lambda x, y, c=c: circ(x, y, 2.2, c, op=.8), lossy)]
    taken = []  # boxes a chip mustn't cover: every device and its name
    for h in hosts:
        x, y = P[h["id"]]
        lw = min(tw(h.get("label") or h["id"]), W / nt - 6)
        taken += [(x - 20, y - 20, 40, 40), (x - lw / 2, y + 20, lw, 16)]

    def spot(a, b, wid):
        """The chip's centre: the link's middle, or the nearest point along it clear of every device and chip."""
        for f, lift in ((f, lift) for lift in (0, 12, -12, 22, -22) for f in (.5, .42, .58, .34, .66, .26, .74)):
            x, y = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f + lift
            x = min(max(x, wid / 2 + 1), W - wid / 2 - 1)
            if not any(x - wid / 2 < bx + bw and bx < x + wid / 2 and y - 9 < by + bh and by < y + 9 for bx, by, bw, bh in taken):
                break
        else:
            x, y = min(max((a[0] + b[0]) / 2, wid / 2 + 1), W - wid / 2 - 1), (a[1] + b[1]) / 2
        taken.append((x - wid / 2, y - 9, wid, 18))
        return x, y
    for ln in links:  # latency chips on top of every line
        ms = ln.get("ms", 0)
        A, B = P[ln["from"]], P[ln["to"]]
        hue_i = 3 if ms < 20 else 1 if ms < 80 else 2
        if not ln.get("loss"):
            if "ms" in ln:
                s = f"{_num(ms)}ms"
                out += pill(*spot(A, B, tw(s) + 14), s, T, hue_i, W)
            continue
        parts = ([(f"{_num(ms)}ms", T.ink, 400, 0)] if "ms" in ln else []) + \
            [(f"{_num(ln['loss'])}%", T.txt(2), 700, 0)] + ([("LOSS", T.muted, 700, .6)] if W / nt >= 120 else [])
        # one chip: the latency and what it drops (a cramped panel keeps the pink % and the dashed line, not the word)
        wid = sum(tw(s, SZ, k) for s, _, _, k in parts) + 6 * (len(parts) - 1) + 14
        mx, my = spot(A, B, wid)
        x = mx - wid / 2
        out.append(rect(x, my - 9, wid, 18, T.bg, T.a(2), 4))
        x += 7
        for s, c, wt, k in parts:
            out.append(txt(x, my + 4, s, c, SZ, weight=wt, track=k))
            x += tw(s, SZ, k) + 6
    for i, h in enumerate(hosts):
        x, y = P[h["id"]]
        out.append(circ(x, y, 22, hue[h["kind"]], op=.08))
        out += _device(h["kind"], x, y, hue[h["kind"]], T, i)
        out.append(txt(x, y + 32, h.get("label") or h["id"], T.ink, SZ, "middle", 700, maxw=W / nt - 6))
    return out


def _chord_refs(d, where):
    errs = []
    for i, f in enumerate(d["flows"]):
        for k in ("from", "to"):
            errs += _named(d["groups"], f[k], f"{where}.flows[{i}].{k}", "groups")
    return errs


@kind("chord", "a chord diagram: groups as arcs round a ring sized by their traffic, ribbons between them as wide as "
      "each flow, particles riding the ribbons",
      OBJ({"groups": ARR(STR(14), 2, 8),
           "flows": ARR(OBJ({"from": STR(14), "to": STR(14), "value": NUM(0, 1e9)}, ["from", "to", "value"]), 1, 28)},
          ["groups", "flows"]),
      {"groups": ["api", "worker", "db", "cache", "queue", "search"],
       "flows": [{"from": "api", "to": "db", "value": 420}, {"from": "api", "to": "cache", "value": 860},
                 {"from": "api", "to": "queue", "value": 240}, {"from": "worker", "to": "db", "value": 310},
                 {"from": "queue", "to": "worker", "value": 240}, {"from": "worker", "to": "search", "value": 150},
                 {"from": "api", "to": "search", "value": 190}, {"from": "cache", "to": "db", "value": 60},
                 {"from": "worker", "to": "cache", "value": 90}]},
      size=(560, 460), refs=_chord_refs)
def _chord(d, W, H, T):
    gs, out = d["groups"], []
    fl = [f for f in d["flows"] if f["from"] in gs and f["to"] in gs]
    tot = {g: 0 for g in gs}
    for f in fl:
        tot[f["from"]] += f["value"]
        tot[f["to"]] += f["value"]
    lab = min(112, max(tw(g) + tw(_num(tot[g])) for g in gs) + 40)
    cx, cy = W / 2, H / 2
    R = max(30, min(W / 2 - lab, H / 2 - 38))
    S = sum(tot.values()) or 1
    wt = {g: max(tot[g], S * .03) for g in gs}
    gap = .06
    span = 2 * math.pi - gap * len(gs)
    a, arc, cur = -math.pi / 2, {}, {}
    for g in gs:
        arc[g] = (a, a + span * wt[g] / sum(wt.values()))
        cur[g] = a
        a = arc[g][1] + gap

    def p(a, r):
        return cx + r * math.cos(a), cy + r * math.sin(a)

    def P(a, r):
        x, y = p(a, r)
        return f"{N(x)} {N(y)}"
    for g in gs:  # each flow takes its share of both ends' arcs
        pass
    ends = []
    for f in fl:
        res = []
        for g in (f["from"], f["to"]):
            w = (arc[g][1] - arc[g][0]) * f["value"] / wt[g]
            res.append((cur[g], cur[g] + w))
            cur[g] += w
        ends.append(res)
    hue = {g: T.a(i) for i, g in enumerate(gs)}
    for i, (f, ((s0, s1), (t0, t1))) in enumerate(zip(fl, ends)):
        c = hue[f["from"]]
        dd = (f"M{P(s0, R)}A{N(R)} {N(R)} 0 {int(s1 - s0 > math.pi)} 1 {P(s1, R)}Q{N(cx)} {N(cy)} {P(t0, R)}"
              f"A{N(R)} {N(R)} 0 {int(t1 - t0 > math.pi)} 1 {P(t1, R)}Q{N(cx)} {N(cy)} {P(s0, R)}Z")
        out.append(path(dd, c, .75, fill=c, op=.3))
        poly = _qpoly(p((s0 + s1) / 2, R - 2), (cx, cy), p((t0 + t1) / 2, R - 2))
        out.append(_rider(poly, (i * .23) % 1, 2.4 + (i % 3) * .5, lambda x, y, c=c: circ(x, y, 2.6, c)))
    ring = f'<circle cx="{N(cx)}" cy="{N(cy)}" r="{N(R + 17)}" fill="none" stroke="{T.muted}" stroke-dasharray="2 7" opacity=".7"/>'
    out.append(f"<g>{ring}{_spin(cx, cy, 40)}</g>")
    for i, g in enumerate(gs):
        a0, a1 = arc[g]
        large = int(a1 - a0 > math.pi)
        band = (f"M{P(a0, R + 2)}A{N(R + 2)} {N(R + 2)} 0 {large} 1 {P(a1, R + 2)}L{P(a1, R + 12)}"
                f"A{N(R + 12)} {N(R + 12)} 0 {large} 0 {P(a0, R + 12)}Z")
        out.append(_with(path(band, hue[g], 1, hue[g]), _anim("opacity", "1;.7;1", 3 + i * .4, -i * .5)))
        am = (a0 + a1) / 2
        x, y = p(am, R + 26)
        y += 4 + math.sin(am) * 5
        name, num, col = fit(g, lab - 30), _num(tot[g]), T.txt(i)
        if math.cos(am) > .25:
            out += [txt(x, y, name, T.ink, SZ, weight=700), txt(x + tw(name) + 6, y, num, col, SZ, weight=700)]
        elif math.cos(am) < -.25:
            out += [txt(x, y, num, col, SZ, "end", 700), txt(x - tw(num) - 6, y, name, T.ink, SZ, "end", 700)]
        else:
            out += [txt(x - 3, y, name, T.ink, SZ, "end", 700), txt(x + 3, y, num, col, SZ, weight=700)]
    return out


@kind("layers", "a protocol stack: layers as slabs, a request carried down through each one and the response back "
      "up, the packet growing a header at every layer and shedding them on the way up",
      OBJ({"layers": ARR(OBJ({"label": STR(14), "sub": STR(22, 0), "ms": NUM(0, 1e5)}, ["label"]), 2, 7),
           "down": STR(14, 0), "up": STR(14, 0)}, ["layers"]),
      {"layers": [{"label": "HTTP", "sub": "GET /orders", "ms": 12}, {"label": "TLS", "sub": "AES-256-GCM", "ms": 3},
                  {"label": "TCP", "sub": "port 443", "ms": 1}, {"label": "IP", "sub": "route + TTL", "ms": 0.4},
                  {"label": "Ethernet", "sub": "MTU 1500", "ms": 0.1}],
       "down": "request", "up": "response"},
      size=(560, 420))
def _layers(d, W, H, T):
    ls, out = d["layers"], []
    n = len(ls)
    rl, rr = 12, W - 12  # the rails: down on the left, up on the right
    x0, x1 = 34, W - 34
    dx, dy = min(24, (x1 - x0) * .06), 9
    top, bot = 24, H - 6
    step = (bot - top) / n
    th = max(14, min(32, step - dy - 6))
    if d.get("down"):
        out.append(txt(0, 12, d["down"], T.muted, SZ, maxw=W / 2 - 8))
    if d.get("up"):
        out.append(txt(W, 12, d["up"], T.muted, SZ, "end", maxw=W / 2 - 8))
    for x, up, c in ((rl, False, T.a(0)), (rr, True, T.a(3))):
        a, b = (bot - 4, top + 4) if up else (top + 4, bot - 4)
        out += [_with(line(x, a, x, b, c, 2, dash="4 4"), _anim("stroke-dashoffset", "8;0", .6)), _head((x, b), (x, a), c, 8)]
    loop = [(rl, top), (rl, bot - 2), (rr, bot - 2), (rr, top), (rl, top)]
    seg = [math.dist(p, q) for p, q in zip(loop, loop[1:])]
    L, C = sum(seg), 3.6 + n * .5
    fd, fu = [], []
    for i, ly in enumerate(ls):
        y = top + step * i
        mid = y + dy + th / 2
        fd.append((mid - top) / L)
        fu.append((seg[0] + seg[1] + (bot - 2 - mid)) / L)
        c = T.a(i)
        fx1 = x1 - dx
        out += [line(rl, mid, x0, mid, T.a(0), 1, .5, "2 3"), line(x1 - dx / 2, mid, rr, mid, T.a(3), 1, .5, "2 3"),
                circ(rl, mid, 3, T.bg, T.a(0), 1.25), circ(rr, mid, 3, T.bg, T.a(3), 1.25),
                path(f"M{N(x0)} {N(y + dy)}L{N(x0 + dx)} {N(y)}H{N(x1)}L{N(fx1)} {N(y + dy)}Z", c, 1, c, .3),path(f"M{N(x0)} {N(y + dy)}L{N(x0 + dx)} {N(y)}H{N(x1)}L{N(fx1)} {N(y + dy)}Z", c, 1, c, .3),
                path(f"M{N(fx1)} {N(y + dy)}L{N(x1)} {N(y)}V{N(y + th)}L{N(fx1)} {N(y + dy + th)}Z", c, 1, c, .5),
                rect(x0, y + dy, fx1 - x0, th, T.panel, c, 2, 1.25)]
        kt, kv = _keys([(0, 0), (fd[i] - .02, 0), (fd[i], .5), (fd[i] + .08, 0), (fu[i] - .02, 0), (fu[i], .5), (fu[i] + .08, 0), (1, 0)])
        out.append(_with(rect(x0, y + dy, fx1 - x0, th, c, rx=2, op=0), _anim("opacity", kv, C, times=kt)))
        ty = y + dy + th / 2 + 4
        out.append(txt(x0 + 10, ty, ly["label"], T.ink, SZ, weight=700, maxw=(fx1 - x0) * .4))
        right = fx1 - 8
        if "ms" in ly:
            ms = f"{_num(ly['ms'])}ms"
            out.append(txt(right, ty, ms, T.txt(i), SZ, "end", 700))
            right -= tw(ms) + 10
        if ly.get("sub"):
            sx = x0 + 18 + min(tw(ly["label"]), (fx1 - x0) * .4)
            if right - sx > 30:
                out.append(txt(sx, ty, ly["sub"], T.muted, SZ, maxw=right - sx))
    widths = [(0, 8)] + [(fd[i], 8 + 5 * (i + 1)) for i in range(n)] + [(fu[i], 8 + 5 * i) for i in reversed(range(n))]
    kt, kv = _keys(widths)
    pk = rect(rl - 4, top - 5, 8, 10, T.a(1), T.bg, 2, 1)
    d_ = "M" + "L".join(f"{N(x - rl)} {N(y - top)}" for x, y in loop)
    back = (L - seg[-1]) / L  # the leg back across the top is the next request being made: the packet is out of sight
    ko, vo = _keys([(0, 1), (back, 1), (back + .01, 0), (.995, 0), (1, 1)])
    out.append(_with(pk, _ride(d_, C), _anim("width", kv, C, times=kt, calc="discrete"), _anim("opacity", vo, C, times=ko)))
    return out


_CHIP = {"ok": 3, "busy": 0, "warn": 1, "fail": 2}


@kind("circuit", "a circuit board: each stage a chip in its state (lit, blinking busy, warning, failed, idle), traces "
      "carrying current from the input pad through the chips to the output, dark after a failure",
      OBJ({"input": STR(12, 0), "output": STR(12, 0),
           "stages": ARR(OBJ({"label": STR(12), "sub": STR(16, 0),
                              "state": {"type": "string", "enum": ["ok", "busy", "warn", "fail", "idle"]}}, ["label", "state"]), 2, 8)},
          ["stages"]),
      {"input": "commit", "output": "release",
       "stages": [{"label": "lint", "sub": "ruff", "state": "ok"}, {"label": "test", "sub": "812 cases", "state": "ok"},
                  {"label": "build", "sub": "wheel", "state": "ok"}, {"label": "scan", "sub": "2 advisories", "state": "warn"},
                  {"label": "sign", "sub": "sigstore", "state": "busy"}, {"label": "ship", "sub": "pypi", "state": "idle"}]},
      size=(680, 360))
def _circuit(d, W, H, T):
    st, out = d["stages"], []
    n = len(st)
    cols = min(n, max(2, int(W // 190)))
    rows = -(-n // cols)
    pl = 56
    gx, gy = (W - 2 * pl) / cols, (H - 8) / rows
    cw, ch = min(132, gx - 34), max(24, min(62, gy - 30))
    out.append(rect(0, 0, W, H, T.bg, T.line, 8, 1.25))
    grid = "".join(f"M8 {N(y)}H{N(W - 8)}" for y in range(10, int(H) - 4, 14))
    out.append(f'<path d="{grid}" stroke="{T.line}" stroke-width="1.6" stroke-dasharray=".1 14" stroke-linecap="round" fill="none"/>')
    out += [circ(x, y, 3.5, T.panel, T.line, 1.25) for x, y in ((8, 8), (W - 8, 8), (8, H - 8), (W - 8, H - 8))]
    box = []
    for i in range(n):
        r, c = divmod(i, cols)
        c = cols - 1 - c if r % 2 else c
        box.append((pl + gx * (c + .5), 4 + gy * (r + .5), r))
    fail = next((i for i, s in enumerate(st) if s["state"] == "fail"), n)
    gold = T.a(1)

    def trace(pts, k):
        dd = "M" + "L".join(f"{N(x)} {N(y)}" for x, y in pts)
        live = k <= fail
        out.append(path(dd, gold, 3, op=.35 if live else .12))
        if live:
            out.append(_with(path(dd, gold, 1.75, dash="3 9"), _anim("stroke-dashoffset", "12;0", .9)))
        out.extend(circ(x, y, 2.6, T.bg, gold, 1.25) for x, y in pts[1:-1])
    x0, y0, _ = box[0]
    pad_in = (0, y0)
    trace([(12, y0), (x0 - cw / 2, y0)], 0)
    for i in range(n - 1):
        (ax, ay, ar), (bx, by, br) = box[i], box[i + 1]
        if ar == br:
            s = 1 if bx > ax else -1
            trace([(ax + s * cw / 2, ay), (bx - s * cw / 2, by)], i + 1)
        else:  # up the end of a row and back along the next
            s = -1 if ar % 2 else 1
            tx = ax + s * (cw / 2 + 14)
            trace([(ax + s * cw / 2, ay), (tx, ay), (tx, by), (bx + s * cw / 2, by)], i + 1)
    lx, ly, lr = box[-1]
    s = -1 if lr % 2 else 1
    pad_out = (W - 12 if s > 0 else 0, ly)
    trace([(lx + s * cw / 2, ly), (pad_out[0] + (0 if s > 0 else 12), ly)], n)
    for (px, py), lab, anchor, tx in ((pad_in, d.get("input"), "start", 0), (pad_out, d.get("output"), "end" if s > 0 else "start",
                                                                                   W if s > 0 else 0)):
        out.append(rect(px, py - 9, 12, 18, gold, rx=2))
        if lab:
            out.append(txt(tx, py - 15, lab, T.muted, SZ, anchor, maxw=pl - 4))
    out.append(_blink(circ(6 + 3, y0 + 18, 2.5, T.a(3)), 1.2))  # the power light by the input
    out.append(_with(circ(6, y0, 3, gold, op=.6), _anim("r", "2;7;2", 1.6), _anim("opacity", ".7;0;.7", 1.6)))
    for i, s_ in enumerate(st):
        x, y, _ = box[i]
        x, y = x - cw / 2, y - ch / 2
        state = s_["state"]
        col = T.a(_CHIP[state]) if state in _CHIP else T.muted
        pins = max(2, int((cw - 18) // 11))
        out += [rect(x + 10 + k * (cw - 20) / (pins - 1) - 2, yy, 4, 5, T.muted, rx=.5) for k in range(pins) for yy in (y - 5, y + ch)]
        out += [rect(x, y, cw, ch, T.panel, col if state != "idle" else T.line, 3, 1.5),
                path(f"M{N(x)} {N(y + ch / 2 - 5)}A5 5 0 0 1 {N(x)} {N(y + ch / 2 + 5)}", T.line, 1.25),
                txt(x, y - 8, f"U{i + 1}", T.muted, SZ, weight=700)]
        led = circ(x + cw - 9, y + 9, 3.5, col, op=.45 if state == "idle" else None)
        speed = {"busy": .6, "warn": 1.4, "fail": .45, "ok": 3}.get(state)
        out.append(_blink(led, speed, i * .21, .7 if state == "ok" else .1) if speed else led)
        roomy = ch >= 46
        out.append(txt(x + cw / 2, y + (ch / 2 + 2 if roomy and s_.get("sub") else ch / 2 + 4) + (0 if roomy else 0), s_["label"],
                       T.ink, SZ, "middle", 700, maxw=cw - (24 if not roomy else 14)))
        if roomy:
            if s_.get("sub"):
                out.append(txt(x + cw / 2, y + ch / 2 + 17, s_["sub"], T.muted, SZ, "middle", maxw=cw - 14))
            out.append(txt(x + cw - 16, y + 13, state.upper(), T.txt(_CHIP[state]) if state in _CHIP else T.muted, SZ, "end", 700))
    return out


# ---------------------------------------------------------------- time, scale and state (T-3737)
# Eight more moving kinds: a git metro map, a trace waterfall, a treemap, log rain, a health radar, an hourglass, a
# decision tree and a circle pack. The same rules: a still frame that reads alone, SMIL that loops over it.

def _tag(el, c, **data):
    """el with a class (and data-* attributes): a hook a test or a reader's stylesheet can find."""
    i = el.index(" ")
    return el[:i] + f' class="{c}"' + "".join(f' data-{k}="{esc(v)}"' for k, v in data.items()) + el[i:]


def _join(el, cap="round"):
    """A path drawn with round joins and caps: a metro line's bends, a falling streak's ends."""
    return el[:-2] + f' stroke-linejoin="round" stroke-linecap="{cap}"/>'


def _fo(el, v):
    """A shape with its fill alone faded, its stroke kept whole."""
    return el[:-2] + f' fill-opacity="{v}"/>'


def _ms(v):
    """A duration as drawn: 4.5ms, 182ms, 1,200ms."""
    return (f"{round(v):,}" if v >= 100 else N(v)) + "ms"


def _tick(v):
    return f"{round(v, 6):,.10g}"


def _ticks(lo, hi, n):
    """Round values from lo to hi, about n of them."""
    raw = (hi - lo) / max(n, 1)
    if raw <= 0:
        return [lo]
    mag = 10 ** math.floor(math.log10(raw))
    s = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    k, out = math.ceil(lo / s - 1e-9), []
    while k * s <= hi + 1e-9 and len(out) < 40:
        out.append(k * s)
        k += 1
    return out


def _poly(pts):
    return "M" + "L".join(f"{N(x)} {N(y)}" for x, y in pts)


def _ring(x, y, r):
    """A circle as a path: one a dash can run round and a group can turn."""
    return f"M{N(x - r)} {N(y)}a{N(r)} {N(r)} 0 1 0 {N(2 * r)} 0a{N(r)} {N(r)} 0 1 0 {N(-2 * r)} 0"


def _tbox(x, y, s, pad=1.5):
    """The box (x0, y0, x1, y1) a start-anchored SZ text at baseline y covers, padded."""
    return (x - pad, y - SZ * .78 - pad, x + tw(s) + pad, y + SZ * .24 + pad)


def _seg_box(p, q, b):
    """Whether the segment p..q enters the box b (Liang-Barsky clipping)."""
    (x0, y0), dx, dy = p, q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pv, qv in ((-dx, x0 - b[0]), (dx, b[2] - x0), (-dy, y0 - b[1]), (dy, b[3] - y0)):
        if pv == 0:
            if qv < 0:
                return False
            continue
        t = qv / pv
        t0, t1 = (max(t0, t), t1) if pv < 0 else (t0, min(t1, t))
        if t0 > t1:
            return False
    return True


def _clear(b, segs=(), discs=(), boxes=(), rings=()):
    """Whether box b keeps off every segment (p, q, half-width), disc (x, y, r), box, and ring outline (x, y, r)."""
    x0, y0, x1, y1 = b
    near = lambda x, y: math.hypot(x - min(max(x, x0), x1), y - min(max(y, y0), y1))  # noqa: E731
    far = lambda x, y: max(math.hypot(x - u, y - v) for u in (x0, x1) for v in (y0, y1))  # noqa: E731
    return (not any(_seg_box(p, q, (x0 - w, y0 - w, x1 + w, y1 + w)) for p, q, w in segs)
            and not any(near(x, y) < r for x, y, r in discs)
            and not any(a[0] < x1 and x0 < a[2] and a[1] < y1 and y0 < a[3] for a in boxes)
            and not any(near(x, y) < r < far(x, y) for x, y, r in rings))


def _halo(el, bg):
    """A text with a halo of the ground under it: a line passing behind it stops short of the letters."""
    return el.replace("<text ", f'<text stroke="{bg}" stroke-width="3.5" stroke-linejoin="round" paint-order="stroke" ', 1)


def _squarify(sizes, x, y, w, h):
    """Rects (x, y, w, h) filling the box, each one's area in proportion to its size and kept near square (Bruls, Huizing
    and van Wijk's squarified layout), in the order the sizes were given."""
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i], i))
    scale = w * h / (sum(sizes) or 1)
    rest, out = [(i, max(sizes[i], 0) * scale) for i in order], [(x, y, 0, 0)] * len(sizes)

    def worst(row, side):
        s = sum(a for _, a in row)
        if s <= 0 or side <= 0:
            return 1e18
        return max(max(side * side * a / (s * s), s * s / (side * side * a)) if a > 0 else 1e18 for _, a in row)
    while rest:
        side, row = min(w, h), [rest.pop(0)]
        while rest and worst(row + [rest[0]], side) <= worst(row, side):
            row.append(rest.pop(0))
        s = sum(a for _, a in row)
        if w >= h:  # a column down the left of what is left
            cw, yy = (s / h if h > 0 else 0), y
            for i, a in row:
                hh = a / cw if cw > 0 else 0
                out[i] = (x, yy, cw, hh)
                yy += hh
            x, w = x + cw, max(w - cw, 0)
        else:  # a row along the top
            rh, xx = (s / w if w > 0 else 0), x
            for i, a in row:
                ww = a / rh if rh > 0 else 0
                out[i] = (xx, y, ww, rh)
                xx += ww
            y, h = y + rh, max(h - rh, 0)
    return out


def _tangent(a, b, r):
    """Centres of a circle of radius r touching circles a and b (x, y, r): none, or the two."""
    (x0, y0, r0), (x1, y1, r1) = a, b
    R0, R1 = r0 + r, r1 + r
    dx, dy = x1 - x0, y1 - y0
    dd = math.hypot(dx, dy)
    if dd == 0 or dd > R0 + R1 or dd < abs(R0 - R1):
        return []
    el = (R0 * R0 - R1 * R1 + dd * dd) / (2 * dd)
    hh = math.sqrt(max(R0 * R0 - el * el, 0))
    mx, my = x0 + dx * el / dd, y0 + dy * el / dd
    return [(mx - dy * hh / dd, my + dx * hh / dd), (mx + dy * hh / dd, my - dx * hh / dd)]


def _pack(radii, aspect=1.0):
    """Centres for circles of these radii packed round the origin, none overlapping: largest first, each set where it
    touches one or two already placed nearest the middle (aspect > 1: the middle of a box that much wider than tall, so
    the pack spreads to fill it). Deterministic, in the order given."""
    order = sorted(range(len(radii)), key=lambda i: (-radii[i], i))
    placed, pos = [], {}
    for i in order:
        r = radii[i]
        if not placed:
            c = (0.0, 0.0)
        else:
            cands = [(a[0] + (a[2] + r) * math.cos(math.pi * k / 6), a[1] + (a[2] + r) * math.sin(math.pi * k / 6))
                     for a in placed for k in range(12)]
            for j, a in enumerate(placed):
                for b in placed[j + 1:]:
                    cands += _tangent(a, b, r)
            ok = [p for p in cands if all(math.hypot(p[0] - q[0], p[1] - q[1]) >= q[2] + r - 1e-7 for q in placed)]
            far = max(math.hypot(q[0], q[1]) + q[2] for q in placed)
            c = min(ok, key=lambda p: (round(math.hypot(p[0] / aspect, p[1]), 6), p)) if ok else (far + r, 0.0)
        placed.append((c[0], c[1], r))
        pos[i] = c
    return [pos[i] for i in range(len(radii))]


def _enclose(cs):
    """(x, y, R): a circle holding every circle in cs, near the smallest (Badoiu and Clarkson's walk to the farthest)."""
    x = sum(c[0] for c in cs) / len(cs)
    y = sum(c[1] for c in cs) / len(cs)
    for k in range(80):
        fx, fy, fr = max(cs, key=lambda c: math.hypot(c[0] - x, c[1] - y) + c[2])
        dd = math.hypot(fx - x, fy - y) or 1
        tx, ty = fx + (fx - x) / dd * fr, fy + (fy - y) / dd * fr  # the far edge of the farthest circle
        x, y = x + (tx - x) / (k + 2), y + (ty - y) / (k + 2)
    return x, y, max(math.hypot(c[0] - x, c[1] - y) + c[2] for c in cs)


def _metro_refs(d, where):
    names, errs = [b["name"] for b in d["branches"]], []
    for i, b in enumerate(d["branches"]):
        if names.index(b["name"]) != i:
            errs.append(f"{where}.branches[{i}].name {b['name']!r} is used twice")
        for k in ("from", "into"):
            if b.get(k):
                errs += _named([v for v in names if v != b["name"]], b[k], f"{where}.branches[{i}].{k}", "other branches")
        if b["end"] < b["start"]:
            errs.append(f"{where}.branches[{i}].end {b['end']} is before its start {b['start']}")
    return errs


def _metro_names(br, geo, lane, tags, X, trunk_y, W, ay, T):
    """Each branch's name and commit count, set by its run where no line, station, flag or other name crosses it: over
    (under, below the trunk) the run first, then the other side, slid along it; cut only when no whole name fits."""
    names = [b["name"] for b in br]
    into = [names.index(b["into"]) if b.get("into") in names and names.index(b["into"]) != i else None for i, b in enumerate(br)]
    segs = [(p, q, 3.5 if i == 0 else 3) for i, pts in enumerate(geo) for p, q in zip(pts, pts[1:])]
    segs += [((X(tg["at"]), 0), (X(tg["at"]), trunk_y), 1.5) for tg in tags]
    discs = [(*pts[0], 7) for pts in geo[1:]] + [(*pts[-1], 7) for i, pts in enumerate(geo) if i and into[i] is not None]
    discs += [(X(tg["at"]), trunk_y, 8) for tg in tags]
    boxes = [_tbox(pts[-1][0] + 9, pts[-1][1] + 4, "HEAD") for i, pts in enumerate(geo) if not i or into[i] is None]
    boxes += [(pts[-1][0] - 3, pts[-1][1] - 9, pts[-1][0] + 3, pts[-1][1] + 9) for i, pts in enumerate(geo) if not i or into[i] is None]
    boxes += [(X(tg["at"]) - tw(tg["label"]) / 2 - 8, 0, X(tg["at"]) + tw(tg["label"]) / 2 + 8, 17) for tg in tags]
    out = []
    for i, (b, pts) in enumerate(zip(br, geo)):
        (ax, y), (bx, _) = pts[0:2] if i == 0 else pts[1:3]
        nxt = min((X(o["start"]) for j, o in enumerate(br) if j != i and lane[j] == lane[i] and o["start"] > b["end"]),
                  default=W)
        lim, cnt = min(nxt - 14, bx + 40, W - 1), _num(b["commits"]) if "commits" in b else ""
        full = b["name"]
        tries = ([(full, cnt)] if cnt else []) + [(full, "")] + [(full[:k - 1] + "…", "") for k in range(len(full) - 1, 3, -1)]
        sides = [y - 9, y + 18] if lane[i] >= 0 else [y + 18, y - 9]
        spot = None
        for name, c in tries:
            s = name + (" " + c if c else "")
            wd = tw(s)
            for ly in sides:
                if ly < 9 or ly > ay - 6:
                    continue
                lo, hi = max(1.0, ax - wd / 2), min(lim, W - 1) - wd
                # from the run's start rightward first, then leftward back over its climb
                xs = [ax + 4 + k * 3 for k in range(int(max(hi - ax - 4, -3) // 3) + 1)]
                xs += [ax + 4 - k * 3 for k in range(1, int(max(ax + 4 - lo, 0) // 3) + 1)]
                spot = next(((x, ly) for x in xs if lo <= x <= hi and _clear(_tbox(x, ly, s), segs, discs, boxes)), None)
                if spot:
                    break
            if spot:
                break
        if not spot:
            continue  # nowhere clear, even cut: no name beats a name a line runs through
        x, ly = spot
        out.append(_tag(txt(x, ly, name, T.txt(i), SZ, weight=700), "bname", branch=b["name"]))
        if c:
            out.append(txt(x + tw(name) + SZ * GLYPH, ly, c, T.muted, SZ))
        boxes.append(_tbox(x, ly, s))
    return out


@kind("metro", "a git history as a metro map: each branch a coloured line off the trunk with its commits as stations, "
      "forks and merges as interchanges, tags as flags, trains running every line and the open heads pulsing",
      OBJ({"branches": ARR(OBJ({"name": STR(18), "from": STR(18, 0), "into": STR(18, 0), "start": NUM(0, 1e9),
                                "end": NUM(0, 1e9), "commits": NUM(0, 1e6)}, ["name", "start", "end"]), 1, 8),
           "tags": ARR(OBJ({"label": STR(12), "at": NUM(0, 1e9)}, ["label", "at"]), 0, 6)}, ["branches"]),
      {"branches": [{"name": "main", "start": 0, "end": 14, "commits": 14},
                    {"name": "feat/cart", "start": 1, "end": 6, "into": "main", "commits": 5},
                    {"name": "spike/ws", "from": "feat/cart", "start": 2, "end": 4.5, "into": "feat/cart", "commits": 2},
                    {"name": "fix/tax-round", "start": 4, "end": 7, "into": "main", "commits": 2},
                    {"name": "feat/search", "start": 6, "end": 12.5, "commits": 6},
                    {"name": "chore/deps", "start": 9, "end": 11, "into": "main", "commits": 1}],
       "tags": [{"label": "v2.3.0", "at": 8}, {"label": "v2.4.0", "at": 11.5}]},
      size=(680, 380), refs=_metro_refs)
def _metro(d, W, H, T):
    br, tags, out = d["branches"], d.get("tags", []), []
    names = [b["name"] for b in br]

    def ref(v, i):
        return names.index(v) if v in names and names.index(v) != i else None
    times = [t for b in br for t in (b["start"], b["end"])] + [t["at"] for t in tags]
    t0, t1 = min(times), max(times)
    t1 = t1 if t1 > t0 else t0 + 1
    pad = (t1 - t0) * .06
    lane, occ = {0: 0}, {0: [(-1e18, 1e18)]}  # the trunk runs lane 0; a branch takes the nearest lane free while it lives
    for i in sorted(range(1, len(br)), key=lambda i: (br[i]["start"], i)):
        b, p = br[i], lane.get(ref(br[i].get("from"), i) or 0, 0)
        s = 1 if p >= 0 else -1
        cands = ([s * (abs(p) + k) for k in range(1, 9)] + [-s * k for k in range(1, 9)] if p else
                 [v for k in range(1, 9) for v in (k, -k)])
        lane[i] = next((k for k in cands if all(b["end"] + pad < a or b["start"] - pad > e for a, e in occ.get(k, []))),
                       cands[-1])
        occ.setdefault(lane[i], []).append((b["start"], b["end"]))
    lo, hi = min(lane.values()), max(lane.values())
    top, bottom = (38 if tags else 22), H - 30 - (14 if lo < 0 else 0)
    gap = min(48, max(bottom - top, 0) / max(hi - lo, 1))
    off = top + (bottom - top - gap * (hi - lo)) / 2

    def Y(k):
        return off + (hi - k) * gap
    x0, x1 = 10, W - 44

    def X(t):
        return x0 + (t - t0) / (t1 - t0) * (x1 - x0)
    ay = H - 16  # the time axis, its grid up behind the lines
    ticks = _ticks(t0, t1, max(2, int((x1 - x0) / 70)))
    out.append(path("".join(f"M{N(X(t))} {N(top - 10)}V{N(ay)}" for t in ticks), T.line, 1, op=.6, dash="2 4"))
    out.append(line(x0, ay, x1, ay, T.line, 1.25))
    for t in ticks:
        out += [line(X(t), ay, X(t), ay + 4, T.muted), txt(X(t), H - 3, _tick(t), T.muted, SZ, "middle")]
    geo = []
    for i, b in enumerate(br):  # each line: off its parent at 45 degrees, along its lane, and back down into its target
        y, xs, xe = Y(lane[i]), X(b["start"]), X(b["end"])
        if i == 0:
            geo.append([(xs, y), (xe, y)])
            continue
        py, into, run = Y(lane[ref(b.get("from"), i) or 0]), ref(b.get("into"), i), xe - xs
        b1 = min(abs(y - py), run / (2 if into is not None else 1.5))
        pts = [(xs, py), (xs + b1, y)]
        if into is not None:
            iy = Y(lane[into])
            pts += [(xe - min(abs(y - iy), run / 2), y), (xe, iy)]
        else:
            pts.append((xe, y))
        geo.append(pts)
    trunk_y = Y(0)
    for tg in tags:
        x = X(tg["at"])
        w = min(tw(tg["label"]) + 14, W - 2)
        cx = min(max(x, w / 2 + 1), W - w / 2 - 1)
        out += [line(x, 16, x, trunk_y - 8, T.a(1), 1, .75, "3 3"), rect(cx - w / 2, 0, w, 16, T.bg, T.a(1), 3),
                txt(cx, 12, tg["label"], T.txt(1), SZ, "middle", 700, maxw=w - 8)]
    for i, pts in enumerate(geo):  # the casings first, so a line crossing another reads as passing over it
        out.append(_join(path(_poly(pts), T.panel, 11 if i == 0 else 10)))
    for i, pts in enumerate(geo):
        out.append(_tag(_join(path(_poly(pts), T.a(i), 6 if i == 0 else 5)), "track", branch=names[i]))
    for i, (b, pts) in enumerate(zip(br, geo)):  # a station tick per commit, as room allows
        (ax, y), (bx, _) = pts[0:2] if i == 0 else pts[1:3]
        k = min(int(b.get("commits", 0)), int((bx - ax) / 11))
        if k > 0:
            out.append(path("".join(f"M{N(ax + (bx - ax) * (j + 1) / (k + 1))} {N(y + 3)}v4" for j in range(k)), T.a(i), 2.5))
    for i, (b, pts) in enumerate(zip(br, geo)):
        if not i:
            continue
        spots = [pts[0]] + ([pts[-1]] if ref(b.get("into"), i) is not None else [])
        for k, (x, y) in enumerate(spots):
            out.append(_tag(circ(x, y, 5.5, T.panel, T.ink, 2), "xchg"))
            if k:  # a merge lands: a ring spreads from the interchange
                beg = -(i * .7) % 2.8
                out.append(_with(circ(x, y, 6, "none", T.a(i), 1.5, .5), _anim("r", "6;15", 2.8, beg),
                                 _anim("opacity", ".8;0", 2.8, beg)))
    for tg in tags:
        x = X(tg["at"])
        out.append(path(f"M{N(x)} {N(trunk_y - 7)}L{N(x + 5)} {N(trunk_y)}L{N(x)} {N(trunk_y + 7)}L{N(x - 5)} {N(trunk_y)}Z",
                        T.bg, 1.5, fill=T.a(1)))
    out += _metro_names(br, geo, lane, tags, X, trunk_y, W, ay, T)
    for i, pts in enumerate(geo):  # trains: one a line, two on the trunk, each in its own phase
        L = sum(math.dist(p, q) for p, q in zip(pts, pts[1:]))
        if L < 20:
            continue
        for j in range(2 if i == 0 else 1):
            out.append(_rider(pts, (i * .37 + j * .5) % 1, max(3.0, L / 45), lambda x, y: rect(x - 4.5, y - 2, 9, 4, T.ink, rx=2)))
    for i, (b, pts) in enumerate(zip(br, geo)):  # an open branch ends at its head: a buffer bar and a pulse
        if i and ref(b.get("into"), i) is not None:
            continue
        x, y = pts[-1]
        out += [_join(line(x, y - 7, x, y + 7, T.a(i), 3)),
                _with(circ(x, y, 4, "none", T.a(i), 1.5, .6), _anim("r", "4;11", 1.8, -i * .4), _anim("opacity", ".9;0", 1.8, -i * .4)),
                txt(x + 9, y + 4, "HEAD", T.muted, SZ, weight=700)]
    return out


def _flame_refs(d, where):
    sp, errs = d["spans"], []
    names = [s["name"] for s in sp]
    par = {s["name"]: s.get("parent") for s in sp}
    for i, s in enumerate(sp):
        if names.index(s["name"]) != i:
            errs.append(f"{where}.spans[{i}].name {s['name']!r} is used twice")
        if s.get("parent"):
            e = _named([v for v in names if v != s["name"]], s["parent"], f"{where}.spans[{i}].parent", "other spans")
            seen, p = {s["name"]}, s["parent"]
            while not e and p and p not in seen:
                seen.add(p)
                p = par.get(p)
            errs += e or ([f"{where}.spans[{i}].parent {s['parent']!r} makes a loop: a span can't be its own ancestor"] if p else [])
    return errs


@kind("flame", "a trace waterfall: each span a bar on a shared clock, nested under its parent and coloured by service, "
      "a flame strip of the whole call on top, a playhead replaying it and a failed span marked",
      OBJ({"spans": ARR(OBJ({"name": STR(28), "parent": STR(28, 0), "svc": STR(12, 0), "at": NUM(0, 1e9), "took": NUM(0, 1e9),
                             "error": {"type": "boolean"}}, ["name", "at", "took"]), 1, 14)}, ["spans"]),
      {"spans": [{"name": "POST /checkout", "svc": "gateway", "at": 0, "took": 182},
                 {"name": "auth.verify", "parent": "POST /checkout", "svc": "auth", "at": 3, "took": 14},
                 {"name": "redis GET session", "parent": "auth.verify", "svc": "cache", "at": 5, "took": 4.5},
                 {"name": "cart.load", "parent": "POST /checkout", "svc": "api", "at": 19, "took": 31},
                 {"name": "SELECT cart_items", "parent": "cart.load", "svc": "db", "at": 22, "took": 24},
                 {"name": "payment.charge", "parent": "POST /checkout", "svc": "api", "at": 52, "took": 104},
                 {"name": "stripe charge", "parent": "payment.charge", "svc": "stripe", "at": 55, "took": 38, "error": True},
                 {"name": "stripe charge retry", "parent": "payment.charge", "svc": "stripe", "at": 98, "took": 54},
                 {"name": "INSERT orders", "parent": "POST /checkout", "svc": "db", "at": 158, "took": 12},
                 {"name": "enqueue email", "parent": "POST /checkout", "svc": "queue", "at": 172, "took": 6}]},
      size=(680, 400), refs=_flame_refs)
def _flame(d, W, H, T):
    sp, out = d["spans"], []
    names = {s["name"] for s in sp}
    kids, roots = {}, []
    for s in sp:
        p = s.get("parent")
        (kids.setdefault(p, []) if p in names and p != s["name"] else roots).append(s)
    rows, seen = [], set()

    def walk(s, dep, up):
        if s["name"] in seen:
            return
        seen.add(s["name"])
        rows.append((s, dep, up))
        me = len(rows) - 1
        for k in sorted(kids.get(s["name"], []), key=lambda k: k["at"]):
            walk(k, dep + 1, me)
    for s in sorted(roots, key=lambda s: s["at"]) + sp:  # then any a loop kept from the roots (the refs refuse those)
        walk(s, 0, None)
    t0 = min(s["at"] for s in sp)
    span = (max(s["at"] + s["took"] for s in sp) - t0) or 1
    svcs = list(dict.fromkeys(s.get("svc", "") for s in sp))
    n, deep = len(rows), max(r[1] for r in rows) + 1
    legend = 18 if any(svcs) else 0
    nw = min(W * .34, 230)
    tx0, tx1 = nw + 12, W - 52
    avail = H - 20 - legend
    strip = deep * 5 + 8
    rowh = min(24, (avail - strip) / n)
    if rowh < 15:  # no room for the flame strip: the rows come first
        strip, rowh = 0, min(24, avail / n)
    y0 = 20 + strip
    yb = y0 + rowh * n

    def TX(t):
        return tx0 + (t - t0) / span * (tx1 - tx0)
    ticks = _ticks(0, span, max(2, int((tx1 - tx0) / 80)))
    for i in range(0, n, 2):
        out.append(rect(0, y0 + i * rowh, W, rowh, T.line, rx=2, op=.22))
    out.append(path("".join(f"M{N(TX(t0 + t))} 15V{N(yb)}" for t in ticks), T.line, 1, op=.7, dash="2 4"))
    out += [txt(TX(t0 + t), 11, _ms(t), T.muted, SZ, "middle") for t in ticks]
    if strip:  # the flame strip: every span at its depth, the whole call at a glance
        for s, dep, _ in rows:
            out.append(rect(TX(s["at"]), 18 + dep * 5, max(TX(s["at"] + s["took"]) - TX(s["at"]), 1), 4,
                            T.a(svcs.index(s.get("svc", ""))), rx=1, op=.85))
    bh, D = max(6, min(13, rowh - 6)), 6.0
    for i, (s, dep, up) in enumerate(rows):
        mid = y0 + i * rowh + rowh / 2
        c, err = T.a(svcs.index(s.get("svc", ""))), bool(s.get("error"))
        dx = 4 + dep * 12
        if up is not None:  # the tree's elbow from the parent's row
            out.append(path(f"M{N(dx - 9)} {N(y0 + up * rowh + rowh / 2 + 4)}V{N(mid)}H{N(dx - 2)}", T.line, 1.25))
        out += [rect(dx, mid - 3, 6, 6, c, rx=1),
                txt(dx + 10, mid + 4, s["name"], T.txt(2) if err else T.ink, SZ, weight=700 if dep == 0 else 400, maxw=nw - dx - 12)]
        x, w = TX(s["at"]), max(TX(s["at"] + s["took"]) - TX(s["at"]), 1.5)
        by = mid - bh / 2
        out.append(_tag(_fo(rect(x, by, w, bh, c, T.a(2) if err else c, 2, 1.5 if err else 1), .3),
                        "span err" if err else "span", span=s["name"]))
        fa, fb = .8 * (s["at"] - t0) / span, .8 * (s["at"] + s["took"] - t0) / span  # filled as the playhead passes
        kt, vals = _keys([(0, 0), (fa, 0), (fb, N(w)), (.97, N(w)), (1, 0)])
        out.append(_with(rect(x, by, w, bh, c, rx=2), _anim("width", vals, D, times=kt)))
        lab, lx = _ms(s["took"]), x + w + (17 if err else 5)
        if err:
            ex = x + w + 8
            out.append(_blink(path(f"M{N(ex - 3)} {N(mid - 3)}L{N(ex + 3)} {N(mid + 3)}M{N(ex + 3)} {N(mid - 3)}L{N(ex - 3)} {N(mid + 3)}",
                                   T.a(2), 1.75), 1.2, i * .13))
        if lx + tw(lab) <= W:
            out.append(txt(lx, mid + 4, lab, T.txt(2) if err else T.muted, SZ))
        elif w >= tw(lab) + 8:
            out.append(txt(x + w - 4, mid + 4, lab, T.on(c), SZ, "end"))
    head = path(f"M{N(tx0 - 4)} 13L{N(tx0 + 4)} 13L{N(tx0)} 18Z", None, fill=T.ink)
    out.append(f'<g>{line(tx0, 15, tx0, yb, T.ink, 1.25, .8)}{head}<animateTransform attributeName="transform" type="translate" '
               f'values="0 0;{N(tx1 - tx0)} 0;{N(tx1 - tx0)} 0" keyTimes="0;.8;1" dur="{_s(D)}" repeatCount="indefinite"/></g>')
    if legend:
        x = 0
        for k, sv in enumerate(svcs):
            if not sv:
                continue
            if x + 14 + tw(sv) > W:
                break
            out += [rect(x, H - 13, 9, 9, T.a(k), rx=2), txt(x + 14, H - 4, sv, T.muted, SZ)]
            x += 14 + tw(sv) + 14
    return out


@kind("treemap", "a treemap: each file a tile its area by size, grouped by folder, coloured by how often it changes, the "
      "changing ones flashing and the hottest breathing",
      OBJ({"unit": STR(12, 0), "groups": ARR(OBJ({"name": STR(16), "files": ARR(OBJ({
          "name": STR(24), "size": NUM(.001, 1e12), "churn": NUM(0, 100)}, ["name", "size"]), 1, 24)}, ["name", "files"]), 1, 8)},
          ["groups"]),
      {"unit": "lines", "groups": [
          {"name": "api/", "files": [{"name": "routes.py", "size": 1840, "churn": 72}, {"name": "checkout.py", "size": 1210, "churn": 88},
                                     {"name": "auth.py", "size": 640, "churn": 35}, {"name": "admin.py", "size": 520, "churn": 12},
                                     {"name": "webhooks.py", "size": 380, "churn": 64}]},
          {"name": "tests/", "files": [{"name": "test_checkout.py", "size": 1480, "churn": 81},
                                       {"name": "test_api.py", "size": 1120, "churn": 29}, {"name": "test_cart.py", "size": 920, "churn": 44}]},
          {"name": "models/", "files": [{"name": "order.py", "size": 980, "churn": 41}, {"name": "cart.py", "size": 760, "churn": 58},
                                        {"name": "user.py", "size": 610, "churn": 22}, {"name": "product.py", "size": 450, "churn": 9}]},
          {"name": "web/", "files": [{"name": "app.tsx", "size": 860, "churn": 33}, {"name": "Cart.tsx", "size": 540, "churn": 66},
                                     {"name": "theme.css", "size": 300, "churn": 5}]},
          {"name": "jobs/", "files": [{"name": "sync.py", "size": 690, "churn": 47}, {"name": "email.py", "size": 420, "churn": 18},
                                      {"name": "reports.py", "size": 310, "churn": 6}]}]},
      size=(680, 420))
def _treemap(d, W, H, T):
    gs, out = d["groups"], []
    legend = 20 if H >= 150 else 0
    tot = [sum(f["size"] for f in g["files"]) for g in gs]
    hues, k = (5, 1, 2), 0
    for gi, (g, (x, y, w, h)) in enumerate(zip(gs, _squarify(tot, 0, 0, W, H - legend))):
        out.append(rect(x + .5, y + .5, max(w - 1, 0), max(h - 1, 0), T.bg, T.line, 3))
        head = 16 if h >= 46 and w >= 44 else 0
        if head:
            t = _num(tot[gi])
            room = tw(t) + 8 if w - 12 >= tw(t) + 8 + 4 * SZ * GLYPH else 0
            out.append(txt(x + 6, y + 12, g["name"], T.ink, SZ, weight=700, maxw=w - 12 - room))
            if room:
                out.append(txt(x + w - 6, y + 12, t, T.muted, SZ, "end"))
        tiles = _squarify([f["size"] for f in g["files"]], x + 2, y + 2 + head, max(w - 4, 0), max(h - 4 - head, 0))
        for f, (fx, fy, fw, fh) in zip(g["files"], tiles):
            ch = f.get("churn", 0)
            c = T.a(hues[_heat(ch)])
            out.append(_tag(_fo(rect(fx, fy, fw, fh, c, T.panel, 0, 1.5), round(.1 + ch / 100 * .3, 2)), "tile", file=f["name"]))
            if fw > 8 and fh > 6:  # the heat edge
                out.append(rect(fx + 1, fy + 1, 2.5, fh - 2, c, rx=1))
            if ch >= 30 and fw > 6 and fh > 6:  # it changes: its outline flashes, faster the more it churns
                out.append(_blink(rect(fx + 1.5, fy + 1.5, fw - 3, fh - 3, "none", c, 1, 1.25), round(4.6 - ch / 30, 2), k * .37, 0))
            if ch >= 60:  # the hottest breathe
                out.append(_tag(_with(_fo(rect(fx, fy, fw, fh, c, rx=0), 0), _anim("fill-opacity", "0;.32;0", 1.8, -(k * .29 % 1) * 1.8)), "hot"))
            if fw >= 36 and fh >= 18:
                out.append(txt(fx + 7, fy + 13, f["name"], T.ink, SZ, maxw=fw - 10))
                if fh >= 32:
                    out.append(txt(fx + 7, fy + 27, _num(f["size"]), T.muted, SZ, maxw=fw - 10))
            k += 1
    if legend:
        y, x = H - 5, 0
        bits = [("AREA", T.muted, None)] + ([(d["unit"], T.ink, None)] if d.get("unit") else []) + [("", None, None), ("HEAT", T.muted, None)]
        bits += [(lab, T.muted, hue) for lab, hue in (("COOL", 5), ("WARM", 1), ("HOT", 2))]
        for s, col, hue in bits:
            if not s:
                x += 10
                continue
            need = tw(s) + (14 if hue is not None else 0)
            if x + need > W:
                break
            if hue is not None:
                out.append(_fo(rect(x, y - 8, 10, 8, T.a(hue), T.a(hue), 1.5), .4))
                x += 14
            out.append(txt(x, y, s, col, SZ))
            x += tw(s) + 8
    return out


_LV = ("debug", "info", "warn", "error")


def _lvc(T, lv, text=False):
    if lv == "debug":
        return T.muted
    i = {"info": 0, "warn": 1, "error": 2}[lv]
    return T.txt(i) if text else T.a(i)


def _rain_refs(d, where):
    names, errs = [s["name"] for s in d["sources"]], []
    for i, r in enumerate(d.get("recent", [])):
        if r.get("source"):
            errs += _named(names, r["source"], f"{where}.recent[{i}].source", "sources")
    return errs


@kind("rain", "log rain: each source a column its events fall down as streaks coloured by level, errors splashing where "
      "they land, a level bar and counts under each and the latest lines on a ticker",
      OBJ({"window": STR(16, 0), "sources": ARR(OBJ({"name": STR(12), **{lv: NUM(0, 1e12) for lv in _LV}}, ["name"]), 1, 8),
           "recent": ARR(OBJ({"level": {"type": "string", "enum": list(_LV)}, "source": STR(12, 0), "msg": STR(48)},
                             ["level", "msg"]), 0, 6)}, ["sources"]),
      {"window": "last 15 min",
       "sources": [{"name": "api", "debug": 1840, "info": 9210, "warn": 312, "error": 41},
                   {"name": "worker", "debug": 620, "info": 3120, "warn": 88, "error": 12},
                   {"name": "db", "info": 1480, "warn": 205, "error": 3},
                   {"name": "auth", "debug": 210, "info": 2240, "warn": 19, "error": 27},
                   {"name": "cron", "debug": 95, "info": 410, "warn": 6, "error": 0}],
       "recent": [{"level": "error", "source": "api", "msg": "POST /checkout 502 upstream timeout"},
                  {"level": "warn", "source": "db", "msg": "slow query 1.8s orders_by_user"},
                  {"level": "error", "source": "auth", "msg": "token refresh failed: clock skew"},
                  {"level": "info", "source": "worker", "msg": "email batch 412 sent"},
                  {"level": "warn", "source": "api", "msg": "rate limit 80% for key ci-bot"}]},
      size=(680, 420), refs=_rain_refs)
def _rain(d, W, H, T):
    src, rec, out = d["sources"], d.get("recent", []), []
    n, head, foot = len(src), 18, 36
    rows = min(len(rec), max(0, int((H - head - foot - 96) // 17)))
    gy = H - (rows * 17 + 8 if rows else 0) - foot  # the ground the rain lands on
    zt, cw = head + 8, W / n
    lx = W  # the level key, right to left along the top
    for lv in reversed(_LV):
        lab = lv.upper()
        lx -= tw(lab)
        out += [txt(lx, 11, lab, _lvc(T, lv, True), SZ, weight=700), _join(line(lx - 6, 3, lx - 6, 12, _lvc(T, lv), 2))]
        lx -= 18
    if d.get("window") and lx > 40:
        out.append(txt(0, 11, d["window"], T.muted, SZ, maxw=lx - 6))
    out.append(line(0, gy, W, gy, T.line))
    r, zh = rng(f"rain{n}{int(W)}{int(H)}"), gy - zt
    cap = max(2, int(zh / 14))
    for i, s in enumerate(src):
        cx = cw * (i + .5)
        out += [line(cx, zt, cx, gy, T.line, 1, .5, "1 5"), rect(cx - cw * .36, head, cw * .72, 5, T.line, rx=2.5)]
        for li, lv in enumerate(_LV):
            v = s.get(lv, 0)
            if not v:
                continue
            c, ln, sw = _lvc(T, lv), 5 + li * 2.5, 1.25 + li * .35
            dur = max(zh / (70 + li * 30), .8)  # the heavier the level, the faster it falls
            for _ in range(min(cap, 1 + round(2.2 * math.log10(1 + v)))):
                x, f = cx + (next(r) - .5) * cw * .66, next(r)
                out.append(_rider([(x, zt), (x, gy - ln)], f, dur,
                                  lambda px, py, c=c, ln=ln, sw=sw, lv=lv: _tag(_join(line(px, py, px, py + ln, c, sw)), "drop " + lv)))
                if lv == "error":  # it splashes as it lands: in step with its drop
                    # unseen at rest: the still frame shows no ring where no drop is landing
                    out.append(_tag(_with(circ(x, gy, 1, "none", c, 1.25, 0), _anim("r", "1;8;8", dur, -f * dur, "0;.2;1"),
                                          _anim("opacity", ".9;0;0", dur, -f * dur, "0;.2;1")), "splash"))
        tot, bw = sum(s.get(lv, 0) for lv in _LV), cw * .78
        acc = cx - bw / 2
        out.append(rect(acc, gy + 4, bw, 6, T.line, rx=2))
        for lv in _LV:
            if tot and s.get(lv):
                w = bw * s[lv] / tot
                out.append(rect(acc, gy + 4, w, 6, _lvc(T, lv), rx=0))
                acc += w
        out.append(txt(cx, gy + 23, s["name"], T.ink, SZ, "middle", 700, maxw=cw - 8))
        e, wn = _num(s.get("error", 0)), _num(s.get("warn", 0))
        if cw >= tw(e) + tw(wn) + 24:
            out += [txt(cx - 5, gy + 36, wn, _lvc(T, "warn", True), SZ, "end"), txt(cx + 5, gy + 36, e, _lvc(T, "error", True), SZ)]
        else:
            out.append(txt(cx, gy + 36, e, _lvc(T, "error", True), SZ, "middle"))
    if rows:  # the ticker: the latest lines, an error's chip blinking
        ty, chw = gy + foot + 6, tw("ERROR") + 10
        out.append(line(0, ty - 2, W, ty - 2, T.line))
        for k, m in enumerate(rec[:rows]):
            y, lv = ty + k * 17, m["level"]
            chip = rect(0, y + 2, chw, 14, T.bg, _lvc(T, lv), 3)
            out += [_blink(chip, 1.1, k * .3, .2) if lv == "error" else chip, txt(chw / 2, y + 13, lv.upper(), _lvc(T, lv, True), SZ, "middle", 700)]
            x = chw + 8
            if m.get("source"):
                sname = fit(m["source"], 90)
                out.append(txt(x, y + 13, sname, T.muted, SZ))
                x += tw(sname) + 8
            if W - x >= 20:
                out.append(txt(x, y + 13, m["msg"], T.ink, SZ, maxw=W - x))
    return out


_ST = {"ok": 3, "warn": 1, "fail": 2}


@kind("radar", "health checks on a radar: each a blip set out by its latency on log rings in its group's sector, lit as "
      "the beam sweeps past, a failure pinging, the readings listed beside the scope",
      OBJ({"checks": ARR(OBJ({"name": STR(16), "ms": NUM(0, 1e7), "state": {"type": "string", "enum": list(_ST)},
                              "group": STR(10, 0)}, ["name", "ms", "state"]), 1, 12)}, ["checks"]),
      {"checks": [{"name": "cdn", "group": "edge", "ms": 18, "state": "ok"},
                  {"name": "dns", "group": "edge", "ms": 4, "state": "ok"},
                  {"name": "load balancer", "group": "edge", "ms": 9, "state": "ok"},
                  {"name": "api /health", "group": "api", "ms": 42, "state": "ok"},
                  {"name": "auth", "group": "api", "ms": 120, "state": "ok"},
                  {"name": "search", "group": "api", "ms": 610, "state": "warn"},
                  {"name": "postgres", "group": "data", "ms": 7, "state": "ok"},
                  {"name": "redis", "group": "data", "ms": 2, "state": "ok"},
                  {"name": "queue lag", "group": "data", "ms": 1900, "state": "warn"},
                  {"name": "stripe", "group": "ext", "ms": 380, "state": "ok"},
                  {"name": "s3", "group": "ext", "ms": 95, "state": "ok"},
                  {"name": "mailer", "group": "ext", "ms": 12000, "state": "fail"}]},
      size=(680, 400))
def _radar(d, W, H, T):
    ch, out = d["checks"], []
    n = len(ch)
    groups = list(dict.fromkeys(c.get("group", "") for c in ch))
    order = sorted(range(n), key=lambda i: (groups.index(ch[i].get("group", "")), i))  # grouped, as given within
    slot = {i: k for k, i in enumerate(order)}
    listw = min(W * .44, 260)
    R = max(30, min(H / 2 - 4, (W - listw - 24) / 2))
    ox = max(0, (W - (2 * R + 24 + listw)) / 2)
    cx, cy, lx = ox + R, H / 2, ox + 2 * R + 24
    lw = min(listw, W - lx)
    vals = [max(c["ms"], .1) for c in ch]
    lo = math.floor(math.log10(min(vals)))
    hi = max(math.log10(max(vals)), lo + 1)
    r0, gap = R * .16, 26  # a wedge clockwise of twelve o'clock is kept clear for the rings' scale
    a0 = -90 + gap

    def rad(ms):  # out from the middle by log latency: a slow check sits near the rim
        return r0 + (R - 10 - r0) * (math.log10(max(ms, .1)) - lo) / (hi - lo)
    out.append(_tag(circ(cx, cy, R, T.bg, T.line, 1.25), "scope"))
    out.append(path(f"M{N(cx - R)} {N(cy)}H{N(cx + R)}M{N(cx)} {N(cy - R)}V{N(cy + R)}", T.line, 1, op=.7))
    decades = [(10 ** k, rad(10 ** k)) for k in range(lo, math.floor(hi) + 1)]
    for _, rr in decades:
        out.append(circ(cx, cy, rr, "none", T.line, 1))
    if len(groups) > 1:  # the sectors' edges
        starts = [min(slot[i] for i in range(n) if ch[i].get("group", "") == g) for g in groups]
        out.append(path("".join(f"M{N(cx + r0 * math.cos(a))} {N(cy + r0 * math.sin(a))}L{N(cx + R * math.cos(a))} {N(cy + R * math.sin(a))}"
                                for a in (math.radians(a0 + (360 - gap) * s / n) for s in starts)), T.muted, 1, op=.55, dash="3 4"))
    ticks = "".join(f"M{N(cx + (R - (7 if k % 3 == 0 else 3.5)) * math.cos(math.radians(k * 10)))} "
                    f"{N(cy + (R - (7 if k % 3 == 0 else 3.5)) * math.sin(math.radians(k * 10)))}"
                    f"L{N(cx + R * math.cos(math.radians(k * 10)))} {N(cy + R * math.sin(math.radians(k * 10)))}" for k in range(36))
    out.append(path(ticks, T.muted, 1, op=.8))
    out.append("<g>" + path(_ring(cx, cy, r0 * .7), T.muted, 1, op=.7, dash="2 3") + _spin(cx, cy, 9, True) + "</g>")
    # each ring's scale as a contour label: set on its ring just right of the twelve o'clock line, on a plate of the
    # scope's ground that breaks the ring under it, slid up or down to cross as few other rings as it can; never over
    # the axis, the rim, a blip, a blip's number or another label
    spots = [(cx + rad(c["ms"]) * math.cos(math.radians(a0 + (360 - gap) * (slot[i] + .5) / n)),
              cy + rad(c["ms"]) * math.sin(math.radians(a0 + (360 - gap) * (slot[i] + .5) / n))) for i, c in enumerate(ch)]
    circles = [(cx, cy, r) for r in [rr for _, rr in decades] + [R - 7.5, r0 * .7]]
    axis = [((cx, cy - R), (cx, cy + R), 1.5), ((cx - R, cy), (cx + R, cy), 1)]
    blips = [(x, y, 6) for x, y in spots]
    taken = [_tbox(x + 6, y - 6, str(slot[i] + 1)) for i, (x, y) in enumerate(spots)] if R >= 70 else []
    scale = []
    for v, rr in decades:
        s, base, best = _ms(v), cy - rr + SZ * .27, None  # base: the baseline that centres the text on the ring's top
        for dy in sorted(range(-6, 17), key=abs):  # further in than out: the rim crowds the outermost ring
            b = _tbox(cx + 7, base + dy, s)
            if b[1] < 0 or not _clear(b, axis, blips, taken, [(cx, cy, R)]):
                continue
            k = sum(not _clear(b, rings=[c]) for c in circles if c[2] != rr)
            if best is None or k < best[0]:
                best = (k, base + dy, b)
            if not k:
                break
        if best:
            _, ly, b = best
            taken.append(b)
            scale += [rect(b[0], b[1], b[2] - b[0], b[3] - b[1], T.bg, rx=2),
                      _tag(_halo(txt(cx + 7, ly, s, T.muted, SZ), T.bg), "ring")]
    dur, bc = 4.0, T.a(3)
    wedges = []
    for k in range(12):  # the beam: a fading wake behind its leading edge, turning
        a1, a2 = math.radians(-90 - k * 3.5), math.radians(-90 - (k + 1) * 3.5)
        wedges.append(path(f"M{N(cx)} {N(cy)}L{N(cx + R * math.cos(a1))} {N(cy + R * math.sin(a1))}"
                           f"A{N(R)} {N(R)} 0 0 0 {N(cx + R * math.cos(a2))} {N(cy + R * math.sin(a2))}Z", None, fill=bc,
                           op=round(.3 * (1 - k / 12), 3)))
    out.append("<g>" + "".join(wedges) + line(cx, cy, cx, cy - R, bc, 1.5) + _spin(cx, cy, dur) + "</g>")
    out += scale  # over the beam: it sweeps under the scale, never through it
    tally = {s: 0 for s in _ST}
    for i, c in enumerate(ch):
        tally[c["state"]] += 1
        a = a0 + (360 - gap) * (slot[i] + .5) / n
        rr = rad(c["ms"])
        x, y = cx + rr * math.cos(math.radians(a)), cy + rr * math.sin(math.radians(a))
        col = T.a(_ST[c["state"]])
        beg = -(1 - (a + 90) % 360 / 360) * dur  # lit as the beam crosses it
        out.append(_with(circ(x, y, 9, col, op=.12), _anim("opacity", ".45;0;0", dur, beg, "0;.35;1")))
        if c["state"] == "warn":
            out.append(circ(x, y, 7, "none", col, 1))
        if c["state"] == "fail":
            out.append(_tag(_with(circ(x, y, 5, "none", col, 1.5, .8), _anim("r", "5;17", 1.6), _anim("opacity", ".9;0", 1.6)), "ping"))
        out.append(_tag(_with(circ(x, y, 4, col), _anim("opacity", "1;.35;.35", dur, beg, "0;.6;1")), "blip"))
        if R >= 70:
            out.append(txt(x + 6, y - 6, str(slot[i] + 1), T.muted, SZ))
    out.append(circ(cx, cy, 3, bc))
    x = lx  # the list: tallies, then each check by its number
    for st in _ST:
        num, lab = str(tally[st]), st.upper()
        if x + tw(num) + 4 + tw(lab) > lx + lw:
            break
        out += [txt(x, 11, num, T.txt(_ST[st]), SZ, weight=700), txt(x + tw(num) + 4, 11, lab, T.muted, SZ)]
        x += tw(num) + 4 + tw(lab) + 12
    heads = len(groups) > 1 and all(groups)
    rh = (H - 22) / (n + (len(groups) if heads else 0))
    if rh < 13:
        heads, rh = False, (H - 22) / n
    rh, y = min(rh, 20), 22
    for g in groups:
        if heads:
            out.append(txt(lx, y + rh / 2 + 4, g, T.muted, SZ, maxw=lw))
            y += rh
        for i in (i for i in order if ch[i].get("group", "") == g):
            c, my = ch[i], y + rh / 2
            st, ms = c["state"], _ms(c["ms"])
            dot = circ(lx + 3, my, 3, T.a(_ST[st]))
            out += [_blink(dot, .9, i * .1) if st == "fail" else dot, txt(lx + 12, my + 4, str(slot[i] + 1), T.muted, SZ),
                    txt(lx + lw, my + 4, ms, T.muted if st == "ok" else T.txt(_ST[st]), SZ, "end")]
            room = lw - 30 - tw(ms) - 8
            if room >= 2 * SZ * GLYPH:
                out.append(txt(lx + 30, my + 4, c["name"], T.txt(2) if st == "fail" else T.ink, SZ, maxw=room))
            y += rh
    return out


def _bulb(u):
    """An hourglass bulb's width (0..1) at u, from the neck (0) to the cap (1): it swells fast and rounds off at the shoulder."""
    return math.sin(math.pi / 2 * u ** .75) ** 1.3 * (1 - .22 * max(0, (u - .8) / .2) ** 2)


@kind("hourglass", "budgets as hourglasses: what is left as sand up top, what is used piled below in strata by part, the "
      "stream falling between, with the percent left and the numbers beside",
      OBJ({"glasses": ARR(OBJ({"label": STR(16), "used": NUM(0, 1e12), "total": NUM(.001, 1e12), "unit": STR(10, 0),
                               "parts": ARR(OBJ({"label": STR(12), "value": NUM(0, 1e12)}, ["label", "value"]), 0, 6)},
                              ["label", "used", "total"]), 1, 3)}, ["glasses"]),
      {"glasses": [{"label": "context", "used": 142000, "total": 200000, "unit": "tokens",
                    "parts": [{"label": "system", "value": 18000}, {"label": "files", "value": 64000},
                              {"label": "chat", "value": 41000}, {"label": "tools", "value": 19000}]},
                   {"label": "weekly quota", "used": 31, "total": 40, "unit": "hours"},
                   {"label": "API budget", "used": 412, "total": 500, "unit": "USD"}]},
      size=(640, 400))
def _hourglass(d, W, H, T):
    gl, out = d["glasses"], []
    n = len(gl)
    cw = W / n
    side = cw >= 250  # wide: the readings beside the glass, the parts listed; narrow: three lines under it
    sand, strata = T.a(1), (0, 2, 3, 4, 5)
    for gi, g in enumerate(gl):
        x0 = gi * cw
        if side:
            gh = H - 6
            gw = min(gh * .6, cw * .42)
            xc, yt = x0 + gw / 2 + 4, 3
        else:
            gh = H - 50
            gw = min(gh * .6, cw - 20)
            xc, yt = x0 + cw / 2, 2
        ct = max(5, gh * .04)
        yb, ym = yt + gh, yt + gh / 2
        bh, mx, neck = gh / 2 - ct, gw / 2 - 9, max(1.6, gw * .035)

        def hw(u, mx=mx, neck=neck):
            return neck + (mx - neck) * _bulb(u)
        us = [k / 60 for k in range(61)]
        cum = [0.0]
        for a, b in zip(us, us[1:]):
            cum.append(cum[-1] + (hw(a) + hw(b)) * (b - a) * bh)

        def u_at(area, cum=cum):
            area = min(max(area, 0), cum[-1])
            for k in range(60):
                if cum[k + 1] >= area:
                    return (k + (area - cum[k]) / ((cum[k + 1] - cum[k]) or 1)) / 60
            return 1.0

        def side_pts(ua, ub, sgn, down, hw=hw):  # the glass wall from ua to ub, in the top bulb (down=False) or the bottom
            return [(xc + sgn * hw(u), ym + (1 if down else -1) * u * bh) for u in (ua + (ub - ua) * k / 16 for k in range(17))]
        frac = min(g["used"] / g["total"], 1)
        pct = round(100 - frac * 100)
        cap = cum[-1] * .86
        top_a, bot_a = (1 - frac) * cap, frac * cap
        wall = side_pts(1, 0, -1, False) + side_pts(0, 1, -1, True) + side_pts(1, 0, 1, True) + side_pts(0, 1, 1, False)
        out.append(path(_poly(wall) + "Z", None, fill=T.bg, op=.6))
        if top_a > cum[-1] * .002:  # what is left: the sand up top, dipping where it drains
            us_ = u_at(top_a)
            ys_ = ym - us_ * bh
            dip = min(5, us_ * bh * .35)
            pts = side_pts(us_, 0, -1, False) + side_pts(0, us_, 1, False) + [(xc, ys_ + dip)]
            out.append(_tag(path(_poly(pts) + "Z", None, fill=sand, op=.9), "sand-top", glass=gi))
        ub = u_at(cum[-1] - bot_a)
        peak = ym + ub * bh - min(7, (1 - ub) * bh * .4)
        if bot_a > cum[-1] * .002:  # what is used: piled below, a mound where it lands, the parts laid down as strata
            pts = side_pts(ub, 1, -1, True) + side_pts(1, ub, 1, True) + [(xc, peak)]
            out.append(_tag(path(_poly(pts) + "Z", None, fill=sand, op=.9), "sand-bottom", glass=gi))
            parts, acc = g.get("parts", []), 0.0
            whole = max(g["used"], sum(p["value"] for p in parts)) or 1
            for k, p in enumerate(parts):
                pa = bot_a * p["value"] / whole
                if pa <= 0:
                    continue
                hi_, lo_ = u_at(cum[-1] - acc), u_at(cum[-1] - acc - pa)
                acc += pa
                out.append(path(_poly(side_pts(lo_, hi_, -1, True) + side_pts(hi_, lo_, 1, True)) + "Z", None,
                                fill=T.a(strata[k % 5]), op=.85))
        else:
            peak = yb - ct
        if top_a > cum[-1] * .002:  # the stream: grains falling from the neck onto the pile
            out.append(_with(_join(line(xc, ym, xc, peak, sand, 1.5, dash="2 3")), _anim("stroke-dashoffset", "10;0", .5)))
            for j in range(3):
                out.append(_rider([(xc, ym + 2), (xc, peak)], j / 3, .75, lambda x, y: circ(x, y, 1.3, sand)))
            out.append(_with(circ(xc, peak, 2, "none", sand, 1, .5), _anim("r", "1;6", .75), _anim("opacity", ".7;0", .75)))
        out.append(path(_poly(wall) + "Z", T.ink, 1.5, op=.6))
        out.append(path(_poly([(xc - hw(u) * .72, ym - u * bh) for u in (.3 + .5 * k / 10 for k in range(11))]), T.ink, 2, op=.18))
        out += [rect(xc - gw / 2, yt, gw, ct, T.line, T.muted, 2), rect(xc - gw / 2, yb - ct, gw, ct, T.line, T.muted, 2),
                rect(xc - gw / 2 + 2, yt + ct, 3, gh - 2 * ct, T.line, rx=1), rect(xc + gw / 2 - 5, yt + ct, 3, gh - 2 * ct, T.line, rx=1)]
        hue = 2 if pct <= 10 else 1 if pct <= 30 else 3
        ptxt = f"{pct}%"
        nums = [(_num(g["used"]), T.ink), ("OF", T.muted), (_num(g["total"]), T.ink)] + ([(g["unit"], T.muted)] if g.get("unit") else [])
        width = lambda bits: sum(tw(s) for s, _ in bits) + 5 * (len(bits) - 1)  # noqa: E731
        if side:
            rx = x0 + gw + 20
            rw = x0 + cw - rx - 4
            out.append(txt(rx, 16, g["label"], T.ink, SZ, weight=700, maxw=rw))
            pel = txt(rx, 34, ptxt, T.txt(hue), SZ, weight=700)
            out += [pel.replace("</text>", _anim("opacity", "1;.3;1", 1) + "</text>") if pct <= 0 else pel, txt(rx + tw(ptxt) + 6, 34, "LEFT", T.muted, SZ)]
            while len(nums) > 3 and width(nums) > rw:
                nums.pop()
            x = rx
            if width(nums) <= rw:
                for s, col in nums:
                    out.append(txt(x, 50, s, col, SZ))
                    x += tw(s) + 5
            for k, p in enumerate(g.get("parts", [])):
                y = 74 + k * 17
                if y > H - 2:
                    break
                val = _num(p["value"])
                out += [rect(rx, y - 8, 9, 9, T.a(strata[k % 5]), rx=2), txt(rx + rw, y, val, T.muted, SZ, "end")]
                if rw - 14 - tw(val) - 8 >= 2 * SZ * GLYPH:
                    out.append(txt(rx + 14, y, p["label"], T.ink, SZ, maxw=rw - 14 - tw(val) - 8))
        else:
            out.append(txt(xc, yb + 14, g["label"], T.ink, SZ, "middle", 700, maxw=cw - 6))
            pw = tw(ptxt) + 6 + tw("LEFT")
            pel = txt(xc - pw / 2, yb + 28, ptxt, T.txt(hue), SZ, weight=700)
            out += [pel.replace("</text>", _anim("opacity", "1;.3;1", 1) + "</text>") if pct <= 0 else pel, txt(xc - pw / 2 + tw(ptxt) + 6, yb + 28, "LEFT", T.muted, SZ)]
            while len(nums) > 3 and width(nums) > cw - 6:
                nums.pop()
            if width(nums) <= cw - 6:
                x = xc - width(nums) / 2
                for s, col in nums:
                    out.append(txt(x, yb + 42, s, col, SZ))
                    x += tw(s) + 5
    return out


def _dtree_refs(d, where):
    nodes, errs = d["nodes"], []
    ids = [x["id"] for x in nodes]
    par = {x["id"]: x.get("parent") for x in nodes}
    for i, x in enumerate(nodes):
        if ids.index(x["id"]) != i:
            errs.append(f"{where}.nodes[{i}].id {x['id']!r} is used twice")
        if x.get("parent"):
            e = _named([v for v in ids if v != x["id"]], x["parent"], f"{where}.nodes[{i}].parent", "node ids")
            seen, p = {x["id"]}, x["parent"]
            while not e and p and p not in seen:
                seen.add(p)
                p = par.get(p)
            errs += e or ([f"{where}.nodes[{i}].parent {x['parent']!r} makes a loop: a node can't be its own ancestor"] if p else [])
    roots = [x["id"] for x in nodes if not x.get("parent")]
    if len(roots) != 1:
        errs.append(f"{where}.nodes has {len(roots)} roots {roots[:4]}: a tree has one root, the node with no parent")
    return errs


@kind("dtree", "a decision tree: questions as boxes left to right, each branch as wide as the cases that took it, outcomes "
      "with their confidence at the leaves and cases flowing down the paths in proportion",
      OBJ({"nodes": ARR(OBJ({"id": STR(12), "parent": STR(12, 0), "label": STR(28), "edge": STR(14, 0), "conf": NUM(0, 100),
                             "n": NUM(0, 1e12)}, ["id", "label"]), 2, 15)}, ["nodes"]),
      {"nodes": [{"id": "tests", "label": "tests pass?", "n": 541},
                 {"id": "block", "parent": "tests", "edge": "no", "label": "block merge", "conf": 99, "n": 120},
                 {"id": "size", "parent": "tests", "edge": "yes", "label": "diff > 400 lines?", "n": 421},
                 {"id": "auth", "parent": "size", "edge": "yes", "label": "touches auth/?", "n": 89},
                 {"id": "review", "parent": "auth", "edge": "yes", "label": "human review", "conf": 92, "n": 38},
                 {"id": "canary", "parent": "auth", "edge": "no", "label": "canary first", "conf": 74, "n": 51},
                 {"id": "flaky", "parent": "size", "edge": "no", "label": "flaky history?", "n": 332},
                 {"id": "rerun", "parent": "flaky", "edge": "yes", "label": "rerun twice", "conf": 81, "n": 22},
                 {"id": "merge", "parent": "flaky", "edge": "no", "label": "auto-merge", "conf": 96, "n": 310}]},
      size=(680, 400), refs=_dtree_refs)
def _dtree(d, W, H, T):
    nodes, out = d["nodes"], []
    ids = {x["id"]: x for x in nodes}
    kids = {}
    for x in nodes:
        if x.get("parent") in ids and x["parent"] != x["id"]:
            kids.setdefault(x["parent"], []).append(x)
    dep, seq = {}, []

    def walk(x, k):
        if x["id"] in dep:
            return
        dep[x["id"]] = k
        seq.append(x)
        for c in kids.get(x["id"], []):
            walk(c, k + 1)
    for x in nodes:  # from the root (and any other the refs refuse); a loop with no way in is left out
        if not x.get("parent") or x["parent"] not in ids:
            walk(x, 0)
    if not seq:
        walk(nodes[0], 0)
    leaves = [x for x in seq if not any(c["id"] in dep and dep[c["id"]] > dep[x["id"]] for c in kids.get(x["id"], []))]
    deepest, nl = max(dep.values()), len(leaves)
    lh = H / nl
    Y = {x["id"]: lh * (j + .5) for j, x in enumerate(leaves)}
    for x in reversed(seq):
        if x["id"] not in Y:
            ys_ = [Y[c["id"]] for c in kids.get(x["id"], []) if c["id"] in Y]
            Y[x["id"]] = (min(ys_) + max(ys_)) / 2 if ys_ else H / 2
    isleaf = {x["id"] for x in leaves}
    # the outcomes as wide as their words need, the questions given the rest: room for the answer on each edge
    leafw = min(190, W * .34, max(110, W * .2, max(tw(x["label"]) for x in leaves) + 34))
    step = (W - leafw - 6) / max(deepest, 1)
    ask = max((tw(x["label"]) + 22 for x in seq if x["id"] not in isleaf), default=60)
    bw, bh, ph = min(160, ask, max(step * .64, step - 48)), 22, min(34, lh - 6)

    def X(x):
        return W - leafw if x["id"] in isleaf else 2 + dep[x["id"]] * step
    pal = (3, 0, 1, 4, 2, 5)
    outcomes = list(dict.fromkeys(x["label"] for x in leaves))
    hue = {x["id"]: pal[outcomes.index(x["label"]) % 6] for x in leaves}
    top_n = max((x.get("n", 0) for x in nodes), default=0) or 1
    polys = {}
    for x in seq:  # each edge as wide as the cases that took it
        p = ids.get(x.get("parent"))
        if p is None or p["id"] not in dep or x is p:
            continue
        dd, pts = curve((X(p) + bw, Y[p["id"]]), (X(x), Y[x["id"]]), .5)
        polys[x["id"]] = [bez(*pts, t / 16) for t in range(17)]
        out.append(path(dd, T.muted, round(1.5 + 6 * math.sqrt(x.get("n", 0) / top_n), 1), op=.35))
    leaf_n = max((x.get("n", 0) for x in leaves), default=0) or 1
    for li, x in enumerate(leaves):  # cases flowing root to outcome, as many riders as its share; they pass under the boxes
        chain, v = [], x
        while v["id"] in polys:
            chain.append(v)
            v = ids[v["parent"]]
        poly = [(X(v) + 2, Y[v["id"]])] + [pt for c in reversed(chain) for pt in polys[c["id"]]] + [(X(x) + 12, Y[x["id"]])]
        L = sum(math.dist(a, b) for a, b in zip(poly, poly[1:]))
        k = 1 + round(3 * x.get("n", 0) / leaf_n)
        c = T.a(hue[x["id"]])
        for j in range(k):
            out.append(_rider(poly, (j / k + li * .17) % 1, round(min(max(L / 75, 2.5), 9), 2), lambda px, py, c=c: circ(px, py, 2.6, c)))
    for x in seq:  # the questions
        if x["id"] in isleaf:
            continue
        bx, by = X(x), Y[x["id"]] - bh / 2
        out += [rect(bx, by, bw, bh, T.panel, T.a(4), 4, 1.5), txt(bx + 8, by + 15, x["label"], T.ink, SZ, weight=700, maxw=bw - 14)]
        if dep[x["id"]] == 0 and "n" in x and by - 5 >= 9:
            out.append(txt(bx, by - 5, _num(x["n"]), T.muted, SZ))
    for x in leaves:  # the outcomes, each with its confidence
        lx, ly, c = W - leafw, Y[x["id"]], T.a(hue[x["id"]])
        conf = x.get("conf")
        ct = f"{_num(conf)}%" if conf is not None else ""
        out += [_tag(rect(lx, ly - ph / 2, leafw, ph, T.panel, c, 5, 1.5), "leaf"), rect(lx, ly - ph / 2, 4, ph, c, rx=2)]
        if ph >= 28:
            out.append(txt(lx + 12, ly - 2, x["label"], T.ink, SZ, weight=700, maxw=leafw - 18))
            if ct:
                barw = leafw - 30 - tw(ct)
                out += [rect(lx + 12, ly + 6, barw, 4, T.line, rx=2),
                        _with(rect(lx + 12, ly + 6, barw * conf / 100, 4, c, rx=2), _anim("opacity", "1;.55;1", 2.4, -(hue[x["id"]] * .4))),
                        txt(lx + leafw - 8, ly + 11, ct, T.txt(hue[x["id"]]), SZ, "end", 700)]
        else:
            out.append(txt(lx + 12, ly + 4, x["label"], T.ink, SZ, weight=700, maxw=leafw - 22 - (tw(ct) + 6 if ct else 0)))
            if ct:
                out += [txt(lx + leafw - 8, ly + 4, ct, T.txt(hue[x["id"]]), SZ, "end", 700),
                        rect(lx + 4, ly + ph / 2 - 3, (leafw - 8) * conf / 100, 2, c, rx=1)]
    for x in seq:  # each answer at the end of its edge, the cases that took it under
        p = ids.get(x.get("parent"))
        if x["id"] not in polys:
            continue
        room = X(x) - (X(p) + bw) - 10
        if x.get("edge") and room >= 2 * SZ * GLYPH:
            out.append(txt(X(x) - 5, Y[x["id"]] - 6, x["edge"], T.ink, SZ, "end", 700, maxw=room))
        if "n" in x and lh >= 30 and room >= tw(_num(x["n"])):
            out.append(txt(X(x) - 5, Y[x["id"]] + 14, _num(x["n"]), T.muted, SZ, "end"))
    return out


@kind("bloom", "a circle pack: each group a circle of circles, every item's area by its size, the whole breathing in a "
      "slow wave out from each group's middle",
      OBJ({"unit": STR(12, 0), "groups": ARR(OBJ({"name": STR(16), "items": ARR(OBJ({"name": STR(16), "size": NUM(.001, 1e12)},
                                                                                     ["name", "size"]), 1, 16)},
                                                 ["name", "items"]), 1, 7)}, ["groups"]),
      {"unit": "kB", "groups": [
          {"name": "app", "items": [{"name": "checkout", "size": 84}, {"name": "cart", "size": 61}, {"name": "search", "size": 47},
                                    {"name": "account", "size": 33}, {"name": "admin", "size": 22}]},
          {"name": "ui kit", "items": [{"name": "components", "size": 120}, {"name": "charts", "size": 92},
                                       {"name": "icons", "size": 38}]},
          {"name": "vendor", "items": [{"name": "react-dom", "size": 131}, {"name": "lodash", "size": 71},
                                       {"name": "zod", "size": 57}, {"name": "date-fns", "size": 39}]},
          {"name": "data", "items": [{"name": "graphql", "size": 64}, {"name": "sync", "size": 26}, {"name": "cache", "size": 18}]}]},
      size=(640, 420))
def _bloom(d, W, H, T):
    gs, out = d["groups"], []
    legend, top = (16 if H >= 170 else 0), 12
    ah = H - legend - top
    packs = []
    for g in gs:  # each group packed on its own, then the groups packed as circles
        radii = [math.sqrt(it["size"]) for it in g["items"]]
        cs = [(x, y, r) for (x, y), r in zip(_pack(radii), radii)]
        ex, ey, er = _enclose(cs)
        packs.append((cs, ex, ey, er + max(radii) * .12))
    gpad = max(p[3] for p in packs) * .05
    gc = _pack([p[3] + gpad for p in packs], max(1.0, min(W / max(ah, 1), 3.0)))
    x0 = min(x - p[3] for (x, _), p in zip(gc, packs))
    x1 = max(x + p[3] for (x, _), p in zip(gc, packs))
    y0 = min(y - p[3] for (_, y), p in zip(gc, packs))
    y1 = max(y + p[3] for (_, y), p in zip(gc, packs))
    s = min((W - 4) / ((x1 - x0) or 1), ah / ((y1 - y0) or 1))
    ox, oy = (W - (x1 - x0) * s) / 2 - x0 * s, top + (ah - (y1 - y0) * s) / 2 - y0 * s
    pills = []
    for gi, (g, (cs, ex, ey, er), (gx, gy)) in enumerate(zip(gs, packs, gc)):
        c = T.a(gi)
        X, Y, R = ox + gx * s, oy + gy * s, er * s
        out.append(_tag(_with(_fo(circ(X, Y, R, c, c, 1.25), .06), _anim("stroke-opacity", "1;.35;1", round(4 + gi * .5, 2))), "grp", g=g["name"]))
        if R > 14:
            out.append("<g>" + path(_ring(X, Y, R - 4), c, 1, op=.4, dash="2 6") + _spin(X, Y, 40 + gi * 7, gi % 2 == 1) + "</g>")
        md = max(math.hypot(ix - ex, iy - ey) for ix, iy, _ in cs) or 1
        for (ix, iy, ir), it in zip(cs, g["items"]):
            x, y, r = X + (ix - ex) * s, Y + (iy - ey) * s, ir * s
            r -= min(1.5, r * .2)
            ph = math.hypot(ix - ex, iy - ey) / md
            out.append(_tag(_with(_fo(circ(x, y, r, c, c, 1.25), .2), _anim("r", f"{N(r)};{N(r * .88)};{N(r)}", 3.6, -(1 - ph) * 2.2)),
                            "item", g=g["name"]))
            if r > 8:
                out.append(circ(x, y, r * .6, "none", c, .75, .3))
            room = 2 * r * .86 - 4
            if r >= 12 and (room >= 4 * SZ * GLYPH or tw(it["name"]) <= room):  # a name, or three letters of it: never a stub
                out.append(txt(x, y + (0 if r >= 26 else 4), it["name"], T.ink, SZ, "middle", maxw=room))
                if r >= 26:
                    out.append(txt(x, y + 13, _num(it["size"]), T.muted, SZ, "middle"))
        pills += pill(X, Y - R, g["name"], T, gi, W)
    out += pills
    if legend:
        total = sum(it["size"] for g in gs for it in g["items"])
        bits = [("AREA", T.muted)] + ([(d["unit"], T.ink)] if d.get("unit") else []) + [("TOTAL", T.muted), (_num(total), T.ink)]
        x = 0
        for k, (s_, col) in enumerate(bits):
            if x + tw(s_) > W:
                break
            out.append(txt(x, H - 4, s_, col, SZ))
            x += tw(s_) + (14 if s_ == (d.get("unit") or "AREA") else 6)
    return out


# ---------------------------------------------------------------- panel chrome, spec, render

PANEL = OBJ({"kind": {"type": "string", "enum": list(KINDS)}, "title": STR(40), "stat": STR(24, 0), "foot": STR(70, 0),
             "n": NUM(1, 99), "accent": NUM(0, 5), "w": NUM(MIN_W, MAX_W), "h": NUM(MIN_H, MAX_H), "data": {"type": "object"}},
            ["kind", "title", "data"])
SPEC = OBJ({"name": STR(40), "title": STR(80), "style": {"type": "string", "enum": list(STYLES_NAMES)},
            "panels": ARR(PANEL, 1, 40)}, ["panels"])


def check_spec(spec):
    """[errors]: each names its field path (panels[2].data.clusters[0].n) and what would be valid."""
    errs = []
    validate(spec, SPEC, "spec", errs)
    if errs:
        return errs
    for i, p in enumerate(spec["panels"]):
        errs += _check(p["kind"], p["data"], f"panels[{i}].data")
    return errs


def _check(name, data, where):
    """The schema's errors, then (only on data the schema passed) the kind's references."""
    errs = []
    validate(data, KINDS[name]["schema"], where, errs)
    if not errs and KINDS[name].get("refs"):
        errs += KINDS[name]["refs"](data, where)
    return errs


def check_data(name, data):
    if name not in KINDS:
        near = difflib.get_close_matches(name, KINDS, 1)
        return [f"kind {name!r} isn't one of {list(KINDS)}" + (f": did you mean {near[0]!r}?" if near else "")]
    return _check(name, data, "data")


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
            *([txt(x + W - PAD, y + 21, stat, T.txt(acc), SZ, "end", 700).replace("<text ", '<text data-stat="" ', 1)]
              if stat else []),  # data-stat: where an animated page's feed writes the live number (T-3734)
            line(x, y + HEAD - 4, x + W, y + HEAD - 4, T.line), path(brk, T.a(acc), 1.5)]


def sheet_svg(panels, T, cols=3, gap=18):
    """Every panel in one svg, in a grid: the contact sheet. The panels in a column share its widest one's width, so the
    column's edges line up."""
    sizes = [size_of(p) for p in panels]
    cols = max(1, min(cols, len(sizes)))
    cw = [max(w for w, _ in sizes[c::cols]) for c in range(cols)]
    rows = [sizes[i:i + cols] for i in range(0, len(sizes), cols)]
    rh = [max(h for _, h in r) for r in rows]
    W, H = sum(cw) + (cols + 1) * gap, sum(rh) + (len(rows) + 1) * gap
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {N(W)} {N(H)}" width="{N(W)}" height="{N(H)}" '
           f'font-family="{FONT}"><title>panel kit contact sheet</title>', rect(0, 0, W, H, T.bg, rx=0)]
    for i, p in enumerate(panels):
        r, c = divmod(i, cols)
        x, y = gap + sum(cw[:c]) + c * gap, gap + sum(rh[:r]) + r * gap
        out += [f'<g transform="translate({N(x)} {N(y)})">', panel_svg(dict(p, w=cw[c]), T, i + 1, f"p{i}", nested=True), "</g>"]
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
