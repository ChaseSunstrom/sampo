#!/usr/bin/env python3
"""Sampo's README figures (T-3247): every pipe table in README.md and four measured charts, as light and dark SVGs.

  python3 demo/readme_figures.py             draw every figure; a new table gets its <picture> and a text copy
  python3 demo/readme_figures.py --check     exit 1 naming each figure that is stale or missing
  python3 demo/readme_figures.py --refresh [CHART ...]   measure the charts again (git history, the hook bench, the
                                             model bench); name one to keep the rest
  sampo works check --type figure demo/figures/*.svg   the gates, at the size a reader sees them

A table is drawn from README.md's own rows, which stay under the picture in a collapsed <details> (links, search and
screen readers keep working). The charts are drawn from demo/figures/data.json, which only --refresh writes, so a
--check anywhere redraws the same bytes. Colours are the live page's modern tokens (plugin/web/live/live.css).
"""
import argparse
import collections
import datetime
import html
import json
import math
import os
import re
import subprocess
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
SUMMARY = "<details><summary>As text</summary>"
W, CHART_W, CHART_H = 880, 440, 280
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
# the live page's modern look, both schemes (live.css [data-theme="modern"])
THEMES = {
    "dark": {"page": "#0B0E14", "plate": "#131822", "band": "#10141C", "ink": "#E8ECF4", "ash": "#B4BDCE",
             "muted": "#8893A7", "accent": "#7AA2FF", "line": "#FFFFFF", "blue": "#5B8CFF", "green": "#34D399",
             "red": "#F87171", "grey": "#4A5468"},
    "light": {"page": "#F6F7F9", "plate": "#FFFFFF", "band": "#F6F7F9", "ink": "#0F1722", "ash": "#3A4556",
              "muted": "#5B6679", "accent": "#2F62E0", "line": "#0F1722", "blue": "#2F62E0", "green": "#0E8A5F",
              "red": "#C62828", "grey": "#A9B1C0"},
}


# ---- text: inline Markdown to styled runs, widths, wrapping
INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)")


def inline(cell):
    """[(text, style)]: style is '' (plain), 'b', 'code' or 'link'; the Markdown marks themselves are dropped."""
    runs, at = [], 0
    for m in INLINE.finditer(cell):
        if m.start() > at:
            runs.append((html.unescape(cell[at:m.start()]), ""))
        if m.group(1) is not None:
            runs.append((html.unescape(m.group(1).replace("`", "")), "b"))
        elif m.group(2) is not None:
            runs.append((m.group(2), "code"))
        else:
            label = m.group(3)
            runs.append((label.strip("`"), "code" if label.startswith("`") else "link"))
        at = m.end()
    if at < len(cell):
        runs.append((html.unescape(cell[at:]), ""))
    return [r for r in runs if r[0]]


NARROW, WIDE, EM = set("iljtf.,:;'|!()[]{} ·"), set("mwMW@%&#$"), set("—…")


def width(line, size):
    """An estimate of a line's width in px (ponytail: per-glyph classes, not real font metrics; wraps err wide)."""
    total = 0.0
    for text, style in line:
        if style == "code":
            total += 0.61 * len(text)
            continue
        em = sum(0.3 if c in NARROW else 1.0 if c in EM or unicodedata.east_asian_width(c) in "WF" else 0.86 if c in WIDE
                 else 0.69 if c.isupper() else 0.57 for c in text)
        total += em * (1.06 if style == "b" else 1.0)
    return total * size


def wrap(runs, maxw, size):
    """Lines of runs, each no wider than maxw by width(); a word wider than a line is cut by characters."""
    words = []
    for text, style in runs:
        words += [(w, style) for w in re.findall(r"\S+\s*|\s+", text)]
    lines, line = [], []
    for word, style in words:
        trial = line + [(word.rstrip(), style)]
        if line and width(trial, size) > maxw:
            lines.append(line)
            line = []
            if not word.strip():
                continue
        while width([(word.rstrip(), style)], size) > maxw and len(word) > 1:  # one long word: cut it
            cut = len(word) - 1
            while cut > 1 and width([(word[:cut], style)], size) > maxw:
                cut -= 1
            lines.append([(word[:cut], style)])
            word = word[cut:]
        line.append((word, style))
    if line:
        lines.append(line)
    return [_merge(trim(ln)) for ln in lines if trim(ln)]


def trim(line):
    if line and line[-1][0] != line[-1][0].rstrip():
        line = line[:-1] + [(line[-1][0].rstrip(), line[-1][1])]
    return [r for r in line if r[0]]


def _merge(line):
    out = []
    for text, style in line:
        if out and out[-1][1] == style:
            out[-1] = (out[-1][0] + text, style)
        else:
            out.append((text, style))
    return out


def esc(text):
    return html.escape(text, quote=True)


# ---- README tables
def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


SEP = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>|[*`]", "", text).lower()).strip("-") or "table"


def tables(text):
    """[{slug, heading, head, rows, start, end}] for each pipe table outside code fences; end is past its last line."""
    lines, out, heading, fence, seen = text.split("\n"), [], "table", False, collections.Counter()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.lstrip().startswith("```"):
            fence = not fence
        elif not fence and re.match(r"#{1,4} ", ln):
            heading = ln.lstrip("#").strip()
        elif not fence and ln.startswith("|") and i + 1 < len(lines) and SEP.match(lines[i + 1]):
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            base = slugify(heading)
            seen[base] += 1
            out.append({"slug": base if seen[base] == 1 else f"{base}-{seen[base]}", "heading": heading,
                        "head": _cells(ln), "rows": [_cells(r) for r in lines[i + 2:j]], "start": i, "end": j})
            i = j
            continue
        i += 1
    return out


def picture(slug, alt, w):
    return [f'<p align="center"><picture>',
            f'  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/{slug}-dark.svg">',
            f'  <source media="(prefers-color-scheme: light)" srcset="demo/figures/{slug}-light.svg">',
            f'  <img src="demo/figures/{slug}-dark.svg" width="{w}" alt="{esc(alt)}">',
            "</picture></p>"]


def _alt(t):
    head = [h for h in t["head"] if h] or ["the table"]
    return f"{t.get('heading', t['slug'])}: a table of {len(t['rows'])} rows ({', '.join(head)}); its text follows"


def wrapped(text):
    """README.md with every table under its picture and inside a collapsed text copy; a wrapped one is refreshed."""
    lines = text.split("\n")
    for t in reversed(tables(text)):
        s, e = t["start"], t["end"]
        if s >= 2 and lines[s - 1] == "" and lines[s - 2] == SUMMARY:  # wrapped before: redraw only its picture
            top = s - 2
            if top >= 1 and lines[top - 1] == "</picture></p>":
                first = top - 1
                while first > 0 and lines[first] and not lines[first].startswith('<p align="center"><picture>'):
                    first -= 1  # a blank line ends the walk: a hand-edited opener is left alone, never eaten past
                if lines[first].startswith('<p align="center"><picture>'):
                    lines[first:top] = picture(t["slug"], _alt(t), W)
            continue
        lines[s:e] = picture(t["slug"], _alt(t), W) + [SUMMARY, ""] + lines[s:e] + ["", "</details>"]
    return "\n".join(lines)


# ---- drawing
def _open(w, h, label, th, extra_css=""):
    return [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="{esc(label)}">', f"<title>{esc(label)}</title>",
            f"<style>text{{font-family:{SANS};fill:{th['ash']}}}.i{{fill:{th['ink']}}}.m{{fill:{th['muted']}}}"
            f".b{{font-weight:600;fill:{th['ink']}}}.c{{font-family:{MONO};fill:{th['accent']}}}"
            f".l{{fill:{th['accent']}}}{extra_css}</style>",
            f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="12" fill="{th["plate"]}" '
            f'stroke="{th["line"]}" stroke-opacity=".12"/>']


STYLE_CLASS = {"": "", "b": "b", "code": "c", "link": "l"}


def _line_svg(x, y, runs, size, base=""):
    spans = "".join(f'<tspan class="{STYLE_CLASS[s]}">{esc(t)}</tspan>' if STYLE_CLASS[s] else esc(t)
                    for t, s in runs)
    cls = f' class="{base}"' if base else ""
    return f'<text x="{x:g}" y="{y:g}" font-size="{size}"{cls} xml:space="preserve">{spans}</text>'


def table_svg(t, theme, w=W):
    th, size, lh, pad, vpad, head_h = THEMES[theme], 14, 21, 16, 13, 40
    cols = max(len(t["head"]), *(len(r) for r in t["rows"])) if t["rows"] else len(t["head"])
    rows = [(r + [""] * cols)[:cols] for r in t["rows"]]
    runs = [[inline(c) if i else [(txt, "b" if s == "" else s) for txt, s in inline(c)] for i, c in enumerate(r)]
            for r in rows]
    nat = [max([width(rr[c], size) for rr in runs] + [40]) + 2 * pad for c in range(cols)]
    low = [max([max((width([(wd, s)], size) for t_, s in rr[c] for wd in t_.split()), default=0) for rr in runs]
               + [40]) + 2 * pad for c in range(cols)]
    low = [min(x, w * 0.45) for x in low]
    if sum(nat) <= w:
        widths = [x * w / sum(nat) for x in nat]
    else:
        spare, give = w - sum(low), sum(nat) - sum(low)
        widths = [lo + (n - lo) * spare / give for lo, n in zip(low, nat)]
    cells = [[wrap(rr[c], widths[c] - 2 * pad, size) for c in range(cols)] for rr in runs]
    heights = [max(len(c) for c in row) * lh + 2 * vpad for row in cells]
    h = head_h + sum(heights)
    out = _open(w, h, _alt(t), th)
    out.append(f'<path d="M0.5 {head_h}V12.5A12 12 0 0 1 12.5 0.5H{w - 12.5}A12 12 0 0 1 {w - 0.5} 12.5V{head_h}Z" '
               f'fill="{th["band"]}"/>')
    out.append(f'<line x1="0.5" y1="{head_h}" x2="{w - 0.5}" y2="{head_h}" stroke="{th["line"]}" '
               f'stroke-opacity=".12"/>')
    x = 0.0
    for c, name in enumerate(t["head"][:cols]):
        label = "".join(r[0] for r in inline(name)).upper()
        out.append(f'<text x="{x + pad:g}" y="25" font-size="11.5" class="m" font-weight="600" '
                   f'letter-spacing=".07em">{esc(label)}</text>')
        x += widths[c]
    y = head_h
    for r, row in enumerate(cells):
        if r:
            out.append(f'<line x1="{pad}" y1="{y:g}" x2="{w - pad}" y2="{y:g}" stroke="{th["line"]}" '
                       f'stroke-opacity=".07"/>')
        x = 0.0
        for c, lines in enumerate(row):
            for k, ln in enumerate(lines):
                out.append(_line_svg(round(x + pad, 1), y + vpad + 15 + k * lh, ln, size))
            x += widths[c]
        y += heights[r]
    return "\n".join(out + ["</svg>"]) + "\n"


# ---- charts
def ticks(top, n=4):
    """0 .. a round number at or above top (charts.js's ticks)."""
    if top <= 0:
        return [0, 1]
    raw = top / n
    p = 10 ** math.floor(math.log10(raw))
    f = raw / p
    st = (1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10) * p
    out = []
    for i in range(12):
        v = round(i * st, 10)
        out.append(v)
        if v >= top:
            break
    return out


def fnum(v):
    return f"{v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:.1f}"


def _day(iso):
    d = datetime.date.fromisoformat(iso[:10])
    return f"{d.strftime('%b')} {d.day}"


def _chart_top(w, title, sub, big, big_label, th):
    """(elements, dy): the title, the subtitle wrapped clear of the headline figure, and how far it pushed the plot."""
    room = w - 40 - max(width([(big, "b")], 22), width([(big_label, "")], 11.5)) - 18
    lines = wrap([(sub, "")], room, 11.5)
    out = [f'<text x="20" y="30" font-size="15" class="b">{esc(title)}</text>',
           f'<text x="{w - 20}" y="33" font-size="22" class="i" font-weight="650" text-anchor="end">{esc(big)}</text>',
           f'<text x="{w - 20}" y="49" font-size="11.5" class="m" text-anchor="end">{esc(big_label)}</text>']
    out += [_line_svg(20, 49 + 15 * k, ln, 11.5, "m") for k, ln in enumerate(lines)]
    return out, 15 * (len(lines) - 1)


def _y_axis(out, x0, x1, y0, y1, ts, th, fmt=fnum):
    top = ts[-1] or 1
    for v in ts:
        y = y1 - (y1 - y0) * v / top
        out.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" stroke="{th["line"]}" '
                   f'stroke-opacity="{".16" if v == 0 else ".07"}"/>')
        out.append(f'<text x="{x0 - 8}" y="{y + 4:.1f}" font-size="11" class="m" text-anchor="end">{fmt(v)}</text>')


def chart_columns(c, th, w=CHART_W, h=CHART_H):
    pts = c["points"]
    total = sum(v for _, v in pts)
    out = _open(w, h, f"{c['title']}: {total} in {len(pts)} days, measured {c['measured'][:10]}", th)
    top, dy = _chart_top(w, c["title"], f"{c['sub']} · measured {c['measured'][:10]}", fnum(total),
                         f"in {len(pts)} days", th)
    out += top
    x0, x1, y0, y1 = 48, w - 20, 74 + dy, h - 36
    ts = ticks(max(v for _, v in pts) or 1)
    _y_axis(out, x0, x1, y0, y1, ts, th)
    step = (x1 - x0) / len(pts)
    for i, (day, v) in enumerate(pts):
        bh = (y1 - y0) * v / ts[-1]
        if bh > 0:
            out.append(f'<rect x="{x0 + i * step + step * 0.16:.1f}" y="{y1 - bh:.1f}" width="{step * 0.68:.1f}" '
                       f'height="{bh:.1f}" rx="2" fill="{th["blue"]}"/>')
    for i in (0, len(pts) // 2, len(pts) - 1):
        out.append(f'<text x="{x0 + (i + 0.5) * step:.1f}" y="{y1 + 18}" font-size="11" class="m" '
                   f'text-anchor="middle">{_day(pts[i][0])}</text>')
    return "\n".join(out + ["</svg>"]) + "\n"


def chart_area(c, th, w=CHART_W, h=CHART_H):
    pts = c["points"]
    last = pts[-1][1]
    out = _open(w, h, f"{c['title']}: {last} on {pts[-1][0]}, measured {c['measured'][:10]}", th)
    top, dy = _chart_top(w, c["title"], f"{c['sub']} · measured {c['measured'][:10]}", fnum(last), c["unit"], th)
    out += top
    x0, x1, y0, y1 = 52, w - 24, 74 + dy, h - 36
    ts = ticks(max(v for _, v in pts))
    _y_axis(out, x0, x1, y0, y1, ts, th)
    d0 = datetime.date.fromisoformat(pts[0][0])
    span = max(1, (datetime.date.fromisoformat(pts[-1][0]) - d0).days)
    xy = [(x0 + (x1 - x0) * (datetime.date.fromisoformat(d) - d0).days / span, y1 - (y1 - y0) * v / ts[-1])
          for d, v in pts]
    line = "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in xy)
    out.append(f'<path d="{line}L{xy[-1][0]:.1f} {y1}L{xy[0][0]:.1f} {y1}Z" fill="{th["green"]}" fill-opacity=".14"/>')
    out.append(f'<path d="{line}" fill="none" stroke="{th["green"]}" stroke-width="2.5" stroke-linejoin="round"/>')
    out.append(f'<circle cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="4" fill="{th["green"]}"/>')
    for i in (0, len(pts) // 2, len(pts) - 1):
        out.append(f'<text x="{xy[i][0]:.1f}" y="{y1 + 18}" font-size="11" class="m" '
                   f'text-anchor="{"start" if i == 0 else "end" if i == len(pts) - 1 else "middle"}">'
                   f'{_day(pts[i][0])}</text>')
    return "\n".join(out + ["</svg>"]) + "\n"


def chart_bars(c, th, w=CHART_W):
    """Horizontal bars: points are [label, value, mark]; mark is a budget tick (hooks) or None; pct scales 0..1."""
    pts, pct = c["points"], c.get("pct")
    head, dy = _chart_top(w, c["title"], f"{c['sub']} · measured {c['measured'][:10]}", c["big"], c["big_label"],
                          th)
    row, top = 24, 74 + dy
    h = top + row * len(pts) + 30
    out = _open(w, h, f"{c['title']}, measured {c['measured'][:10]}: " +
                "; ".join(f"{p[0]} {p[1]:.0%}" if pct else f"{p[0]} {p[1]:g}" for p in pts), th) + head
    x0, x1 = 176, w - 64
    ts = [0, 0.25, 0.5, 0.75, 1] if pct else ticks(max(max(p[1], p[2] or 0) for p in pts))
    span = ts[-1] or 1
    for v in ts:
        x = x0 + (x1 - x0) * v / span
        out.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{top + row * len(pts)}" '
                   f'stroke="{th["line"]}" stroke-opacity="{".16" if v == 0 else ".07"}"/>')
        out.append(f'<text x="{x:.1f}" y="{top + row * len(pts) + 16}" font-size="11" class="m" '
                   f'text-anchor="middle">{f"{v:.0%}" if pct else fnum(v)}</text>')
    for i, (label, v, mark, *rest) in enumerate(pts):
        y = top + i * row
        hot = bool(rest and rest[0])
        over = mark is not None and v > mark
        fill = th["red"] if over else th["blue"] if hot or not pct else th["grey"]
        out.append(f'<text x="{x0 - 10}" y="{y + 15}" font-size="11.5" class="{"b" if hot else ""}" '
                   f'text-anchor="end">{esc(label)}</text>')
        bw = max(2.0, (x1 - x0) * min(v, span) / span)
        out.append(f'<rect x="{x0}" y="{y + 5}" width="{bw:.1f}" height="{row - 10}" rx="3" fill="{fill}"/>')
        end = x0 + bw
        if mark is not None:
            mx = x0 + (x1 - x0) * min(mark, span) / span
            out.append(f'<line x1="{mx:.1f}" y1="{y + 2}" x2="{mx:.1f}" y2="{y + row - 2}" stroke="{th["ink"]}" '
                       f'stroke-width="2" stroke-opacity=".55"/>')
            end = max(end, mx + 1)  # the figure clears the budget tick
        out.append(f'<text x="{end + 6:.1f}" y="{y + 15}" font-size="11" class="i">'
                   f'{f"{v:.0%}" if pct else fnum(v)}</text>')
    return "\n".join(out + ["</svg>"]) + "\n"


DRAW = {"columns": chart_columns, "area": chart_area, "bars": chart_bars}


# ---- measuring (--refresh only)
def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout


def measure_closed(root, today, days=30):
    first = {}
    since = (today - datetime.timedelta(days=days - 1)).isoformat()
    for line in _git(root, "log", "--reverse", "--format=%ad\t%s", "--date=short").splitlines():  # all history:
        # a task first committed before the window and touched in it wasn't closed in it
        day, _, subject = line.partition("\t")
        for tid in re.findall(r"\bT-\d{3,5}\b", subject):
            first.setdefault(tid, day)
    count = collections.Counter(d for d in first.values() if d >= since)
    pts = [[(today - datetime.timedelta(days=days - 1 - i)).isoformat(), 0] for i in range(days)]
    for p in pts:
        p[1] = count.get(p[0], 0)
    return {"kind": "columns", "title": "Tasks closed per day", "sub": "each task id's first commit, git log",
            "points": pts}


def measure_tests(root, today, every=4):
    start = datetime.date.fromisoformat(_git(root, "log", "--reverse", "--format=%ad", "--date=short")
                                        .splitlines()[0])
    pts, d = [], start + datetime.timedelta(days=every)
    while True:
        d = min(d, today)
        rev = _git(root, "rev-list", "-1", f"--before={d.isoformat()} 23:59:59", "HEAD").strip()
        if rev:
            got = subprocess.run(["git", "-C", root, "grep", "-c", "-E", r"^[[:space:]]*def test_", rev, "--", "plugin/tests"],
                                 capture_output=True, text=True).stdout
            pts.append([d.isoformat(), sum(int(x.rpartition(":")[2]) for x in got.splitlines() if x)])
        if d >= today:
            break
        d += datetime.timedelta(days=every)
    return {"kind": "area", "title": "Tests in the suite", "sub": "def test_ in plugin/tests, git grep", "unit": "tests",
            "points": pts}


def measure_hooks(root, runs=15):
    load = os.getloadavg()[0]
    got = json.loads(subprocess.run([sys.executable, os.path.join(root, "plugin", "tests", "bench_hooks.py"),
                                     "--json", "--runs", str(runs)], capture_output=True, text=True).stdout)
    got = got.get("bench", got)
    pts = [[k, round(v["p95"], 1), v["budget"]] for k, v in got.items()]
    ok = sum(p[1] <= p[2] for p in pts)
    return {"kind": "bars", "title": "Hook time per call, p95",
            "sub": f"ms, bench_hooks.py × {runs}, load {load:.0f} on {os.cpu_count()} CPUs", "points": pts,
            "big": f"{ok} of {len(pts)}", "big_label": "under budget (the tick)"}


NAMES = {"majority": "always the commonest", "bm25": "BM25 nearest briefs", "jeff-sampo-decide": "Sampo's own (Jeff)"}


def measure_models(root):
    agg, when = {}, ""
    sys.path.insert(0, os.path.join(root, "plugin", "lib"))
    from sampo import core  # T-3064: the bench rows live in the project's state, not the repo
    with open(core.bench_path(core.find_project(root)), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("arm") != "loo" or r.get("use") != "intake-type" or r.get("accuracy") is None:
                continue
            key, n = r["backend"], r.get("n") or 1
            run = agg.setdefault(key, {})
            s = run.setdefault(r["run"], [0.0, 0, r["at"]])
            s[0] += r["accuracy"] * n
            s[1] += n
            when = max(when, r["at"])
    pts = []
    for backend, runs in agg.items():
        s, n, _ = max(runs.values(), key=lambda x: x[2])  # each backend's latest run
        if n >= 500:
            pts.append([NAMES.get(backend, backend), round(s / n, 3), None, backend == "jeff-sampo-decide"])
    pts.sort(key=lambda p: -p[1])
    best = pts[0]
    return {"kind": "bars", "pct": True, "title": "Picking a task's type", "measured": when,
            "sub": "held-out projects, research/models/bench.jsonl", "points": pts,
            "big": f"{best[1]:.0%}", "big_label": best[0]}


def refresh(root, data=None, names=()):
    """data with the named charts (all when none are named) measured again; the rest kept as they were."""
    today = datetime.date.today()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    how = {"closed": lambda: measure_closed(root, today), "tests": lambda: measure_tests(root, today),
           "hooks": lambda: measure_hooks(root), "models": lambda: measure_models(root)}
    charts = dict((data or {}).get("charts", {}))
    for name in names or how:
        c = charts[name] = how[name]()
        c.setdefault("measured", now)
        c["source"] = c["sub"]
    return {"charts": {k: charts[k] for k in how if k in charts}}


# ---- the run
def figures(root, text, data):
    """{relative path: bytes} for every figure README.md and data.json call for."""
    out = {}
    for t in tables(text):
        for theme in THEMES:
            out[f"demo/figures/{t['slug']}-{theme}.svg"] = table_svg(t, theme)
    for name, c in (data or {}).get("charts", {}).items():
        for theme in THEMES:
            out[f"demo/figures/chart-{name}-{theme}.svg"] = DRAW[c["kind"]](c, THEMES[theme])
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", default=os.path.dirname(HERE))
    p.add_argument("--check", action="store_true", help="exit 1 naming each stale or missing figure")
    p.add_argument("--refresh", nargs="*", choices=["closed", "tests", "hooks", "models"], default=None,
                   metavar="CHART", help="measure the charts' data again: all, or the charts named (closed, tests, "
                   "hooks, models); measure hooks on a quiet machine")
    a = p.parse_args(argv)
    readme, data_path = os.path.join(a.root, "README.md"), os.path.join(a.root, "demo", "figures", "data.json")
    with open(readme, encoding="utf-8") as f:
        text = f.read()
    data = None
    if os.path.exists(data_path):
        with open(data_path, encoding="utf-8") as f:
            data = json.load(f)
    if a.refresh is not None:
        data = refresh(a.root, data, a.refresh)
    new = wrapped(text)
    want = figures(a.root, new, data)
    stale = ["README.md (a table without its picture)"] if new != text else []
    if data is None:
        stale.append("demo/figures/data.json (missing: run with --refresh)")
    figs = os.path.join(a.root, "demo", "figures")
    orphans = sorted(f"demo/figures/{n}" for n in (os.listdir(figs) if os.path.isdir(figs) else [])
                     if n.endswith(".svg") and f"demo/figures/{n}" not in want)
    stale += [f"{o} (no table or chart draws it)" for o in orphans]
    stale += [f"README.md names {r}, which nothing draws" for r in
              sorted(set(re.findall(r'(demo/figures/[\w.-]+\.svg)', new)) - set(want))]
    for rel, body in sorted(want.items()):
        try:
            with open(os.path.join(a.root, rel), encoding="utf-8") as f:
                same = f.read() == body
        except OSError:
            same = False
        if not same:
            stale.append(rel)
    if a.check:
        print("\n".join(f"stale: {s}" for s in stale) or f"all {len(want)} figures are current")
        return 1 if stale else 0
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    if a.refresh is not None:
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
    for rel in stale:
        if rel in want:
            with open(os.path.join(a.root, rel), "w", encoding="utf-8") as f:
                f.write(want[rel])
    for o in orphans:  # only this script's own output folder: a figure nothing draws any more
        os.remove(os.path.join(a.root, o))
    if new != text:
        with open(readme, "w", encoding="utf-8") as f:
            f.write(new)
    print(f"{len(stale)} written, {len(want)} figures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
