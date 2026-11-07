#!/usr/bin/env python3
"""Build the Pages site (T-1220): python3 site/build.py [OUT]  (default: _site beside site/)

Copies site/ (the landing page, style.css, assets) and the demo files, then renders one page per capability from
site/pages.py with charts drawn from site/data/stats.json (made by site/stats.py on a machine with Sampo's state).
Charts are inline SVG that follow the page's light and dark tokens; every mark has a hover title and every chart a
data table. Stdlib only, so the Pages workflow runs it with the runner's Python. T-1411: the pages load nothing from
another host (fonts are the reader's own, named first in style.css with system fallbacks); the build fails on any
page, stylesheet, script or picture that would."""
import html
import html.parser
import json
import math
import os
import re
import shlex
import hashlib
import shutil
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
def _v(rel):  # T-2060, T-2063: browsers keep a file by its URL, so the URL carries its content's hash
    with open(os.path.join(HERE, rel), "rb") as f:
        return rel + "?v=" + hashlib.sha256(f.read()).hexdigest()[:8]


# T-2753: assets/sampo.svg, favicon.ico and share.png are links into design/sampo/logo, so the Runestone files have one home
ICON, FAVICON, CSS, JS = _v("assets/sampo.svg"), _v("assets/favicon.ico"), _v("style.css"), _v("site.js")
FORGE_CSS, FORGE_JS = _v("forge.css"), _v("forge.js")  # T-2851: the landing's illustrated forge, loaded after style.css and before site.js
SECTIONS = _v("sections.css")  # T-2852: the section pages' forge, loaded after style.css
SOURCE_ONLY = {"build.py", "stats.py", "pages.py", "bench_safety.py", "__pycache__"}
TOKENS = os.path.join(ROOT, "design", "sampo", "tokens.json")
TOKENS_START = "/* tokens: python3 site/build.py --tokens writes this block from design/sampo/tokens.json; edit the json */"
TOKENS_END = "/* end tokens */"
NIGHT = ".install, .window, .term, .ex-cmd"  # terminals are iron in either theme


def tokens_css():
    """T-2753: design/sampo/tokens.json as custom properties, the one source of the site's colours, type and motion."""
    with open(TOKENS, encoding="utf-8") as f:
        t = json.load(f)

    def own(group):
        return [(k, v["$value"]) for k, v in group.items() if not k.startswith("$")]

    def rows(decls, per=4):
        return "\n".join("  " + " ".join(decls[i:i + per]) for i in range(0, len(decls), per))

    def stack(names):
        return ", ".join(n if re.fullmatch(r"[A-Za-z][\w-]*", n) else json.dumps(n) for n in names)

    def theme(name, pad=""):
        return "\n".join(pad + r for r in rows([f"--{k}: {v};" for k, v in own(t[name]["color"]) + own(t[name]["chart"]) + own(t[name].get("art", {}))] +
                                               [f"color-scheme: {name};"]).splitlines())

    ratio = t["font"]["scale"]["ratio"]["$value"]
    light = "\n".join([
        rows([f"--font-{k}: {stack(t['font'][k]['$value'])};" for k in ("display", "text", "mono")], 1),
        rows([f"--step-{n}: {round(ratio ** n, 3)}rem;" for n in range(1, 7)], 3),
        rows([f"--dur-{k}: {v};" for k, v in own(t["motion"]["duration"])]),
        rows([f"--ease-{k}: cubic-bezier({', '.join(str(x) for x in v)});" for k, v in own(t["motion"]["easing"])] +
             [f"--autoplay-limit: {t['motion']['autoplay-limit']['$value']};"], 2)])
    return (f"{TOKENS_START}\n:root {{\n{theme('light')}\n{light}\n}}\n"
            f"@media (prefers-color-scheme: dark) {{\n  :root:not([data-theme=\"light\"]) {{\n{theme('dark', '  ')}\n  }}\n}}\n"
            f":root[data-theme=\"dark\"], {NIGHT} {{\n{theme('dark')}\n}}\n{TOKENS_END}")


def _token_span(css):
    start, end = css.find(TOKENS_START), css.find(TOKENS_END)
    if start < 0 or end < start:
        raise SystemExit("build.py: site/style.css lost its tokens block; run python3 site/build.py --tokens")
    return start, end + len(TOKENS_END)


def check_tokens(css):
    """The build refuses a style.css whose tokens block isn't what tokens.json makes, so the two never drift."""
    start, end = _token_span(css)
    if css[start:end] != tokens_css():
        raise SystemExit("build.py: site/style.css's tokens block is stale; run python3 site/build.py --tokens")


def write_tokens(path):
    with open(path, encoding="utf-8") as f:
        css = f.read()
    start, end = _token_span(css)
    with open(path, "w", encoding="utf-8") as f:
        f.write(css[:start] + tokens_css() + css[end:])
TYPES = ["FEATURE", "FIX", "CLEAN", "PERFORMANCE", "SECURITY", "RESEARCH"]
esc = html.escape


def fmt(n):
    return f"{n:,.0f}" if (isinstance(n, (int, float)) and abs(n) >= 100) or isinstance(n, int) else f"{n:g}"


def safe_out(out):
    """Site review: the output folder is wiped first, so never the repo, its site/ source, a folder holding either,
    the home folder or the root."""
    out = os.path.realpath(out)
    keep = [os.path.realpath(x) for x in (ROOT, HERE, os.path.expanduser("~"))]
    return out != os.path.sep and not any(out == k or k.startswith(out.rstrip(os.sep) + os.sep) for k in keep)


OWN_HOSTS = {"chasesunstrom.github.io"}  # the site itself
# T-1785: XML namespace URIs in script or CSS text are names, never fetched; in a src or href they still count
NAMESPACES = {"http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink", "http://www.w3.org/1999/xhtml"}
ABSOLUTE = re.compile(r"""(?i)\bhttps?:[/\\]{2}[^\s'"`)<>]+""")  # any http(s) URL in a stylesheet or script
CSS_SHORT = re.compile(r"""(?i)(?:url\(\s*|@import\s+)['"]?([/\\]{2}[^\s'")]+)""")  # url(//host/x), @import "//host"


def _outside(url):
    """The URL if it names a host other than the site's own (as a browser reads it: tabs and newlines dropped, \\ as /)."""
    try:
        u = urllib.parse.urlsplit(re.sub(r"[\t\n\r]", "", url).strip().replace("\\", "/"))
        return url if u.netloc and (u.hostname or "") not in OWN_HOSTS else None
    except ValueError:
        return url


def _text_urls(text):
    text = re.sub(r"(?s)/\*.*?\*/", "", text)
    return [u for u in ABSOLUTE.findall(text) if u not in NAMESPACES] + CSS_SHORT.findall(text)


class _Loads(html.parser.HTMLParser):
    """Every URL a page or SVG makes the browser fetch: src, srcset, poster, data, href off anything but a link to
    follow (<a>, <area>), CSS in <style> and style="", and any http(s) URL in a <script>."""

    def __init__(self):
        super().__init__()
        self.urls, self.inside = [], None

    def handle_starttag(self, tag, attrs):
        self.handle_startendtag(tag, attrs)
        self.inside = tag if tag in ("style", "script") else None

    def handle_startendtag(self, tag, attrs):
        for k, v in attrs:
            if v is None:
                continue
            if k == "srcset":
                self.urls += [c.split()[0] for c in v.split(",") if c.split()]
            elif k == "ping":  # <a ping> posts to each URL on a click
                self.urls += v.split()
            elif k in ("src", "poster", "data", "background") or k in ("href", "xlink:href") and tag not in ("a", "area"):
                self.urls.append(v)
            elif k == "style":
                self.urls += _text_urls(v)

    def handle_endtag(self, tag):
        self.inside = None

    def handle_data(self, data):
        if self.inside:
            self.urls += _text_urls(data)


def outside_loads(text, ext):
    """T-1411: the URLs on another host that this .html, .svg, .css or .js file makes the browser fetch."""
    if ext in (".html", ".svg"):
        p = _Loads()
        p.feed(text)
        p.close()
        urls = p.urls
    else:
        urls = _text_urls(text)
    return [u for u in urls if _outside(u)]


def outside_hosts(out):
    """Each built file's outside loads, as "path: url" lines."""
    found = []
    for d, dirs, files in os.walk(out):
        dirs.sort()
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext in (".html", ".svg", ".css", ".js"):
                with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                    rel = os.path.relpath(os.path.join(d, f), out)
                    found += [f"{rel}: {u}" for u in outside_loads(fh.read(), ext)]
    return found


def table(head, rows):
    th = "".join(f"<th>{esc(str(h))}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{esc(fmt(c) if isinstance(c, (int, float)) else str(c))}</td>" for c in r)
                 + "</tr>" for r in rows)
    return f'<details class="data"><summary>Show the numbers</summary><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></details>'


def legend(keys):
    return '<ul class="legend">' + "".join(f'<li><i class="sw s{i + 1}"></i>{esc(k.title() if k.isupper() else k)}</li>'
                                           for i, k in enumerate(keys)) + "</ul>"


def figure(title, note, svg, data):
    return (f'<figure class="chart forged"><figcaption><b>{esc(title)}</b>{f"<span>{esc(note)}</span>" if note else ""}'
            f'</figcaption>{svg}{data}</figure>')


def hbars(title, note, items, unit="", series=1, budget=None):
    """Horizontal bars, sorted by the caller; value at each tip. budget: {label: value} draws a tick per row."""
    items = list(items)
    top = max([v for _, v in items] + list((budget or {}).values()) + [1])
    row, label_w, w = 30, 170, 640
    plot = w - label_w - 70
    h = row * len(items) + 8
    out = [f'<svg class="viz grow-x" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">']  # T-1615
    for i, (k, v) in enumerate(items):
        y = 4 + i * row
        bw = max(2, plot * v / top)
        out.append(f'<text class="lab" x="{label_w - 10}" y="{y + 17}" text-anchor="end">{esc(str(k))}</text>')
        out.append(f'<rect class="m s{series}" x="{label_w}" y="{y + 5}" width="{bw:.1f}" height="18" rx="4">'
                   f'<title>{esc(str(k))}: {fmt(v)}{unit}</title></rect>')
        bx = label_w + plot * budget[k] / top if budget and k in budget else 0
        tip = max(bw, bx - label_w + 4) if bx and bx - label_w - bw < 60 else bw  # past a budget tick it would cross
        out.append(f'<text class="val" x="{label_w + tip + 8:.1f}" y="{y + 18}">{fmt(v)}{esc(unit)}</text>')
        if bx:
            out.append(f'<line class="budget" x1="{bx:.1f}" x2="{bx:.1f}" y1="{y + 1}" y2="{y + 27}">'
                       f'<title>budget {fmt(budget[k])}{unit}</title></line>')
    out.append("</svg>")
    head = ["", "value"] + (["budget"] if budget else [])
    rows = [[k, v] + ([budget.get(k, "")] if budget else []) for k, v in items]
    return figure(title, note, "".join(out), table(head, rows))


def pairs(title, note, rows, names=("With Sampo", "Claude Code"), unit=""):
    """Two horizontal bars per row (series 1 and 2), each with its value at the tip: rows are (label, a, b)."""
    rows = list(rows)
    top = max([max(a, b) for _, a, b in rows] + [1e-9])
    band, label_w, w = 72, 0, 640  # T-1239: each label above its bars, full width, so a long task name stays inside
    plot = w - label_w - 80
    h = band * len(rows) + 6
    out = [f'<svg class="viz pairs grow-x" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">']
    for i, (k, a, b) in enumerate(rows):
        y = 4 + i * band
        out.append(f'<text class="lab" x="0" y="{y + 14}">{esc(str(k))}</text>')
        for j, (v, name) in enumerate(((a, names[0]), (b, names[1]))):
            yy = y + 20 + j * 22
            bw = max(2, plot * v / top)
            out.append(f'<rect class="m s{j + 1}" x="{label_w}" y="{yy}" width="{bw:.1f}" height="18" rx="4">'
                       f'<title>{esc(str(k))} · {esc(name)}: {fmt(v)}{unit}</title></rect>'
                       f'<text class="val" x="{label_w + bw + 8:.1f}" y="{yy + 13}">{fmt(v)}{esc(unit)}</text>')
    out.append("</svg>")
    return figure(title, note, legend(list(names)) + "".join(out),
                  table(["", names[0], names[1]], [[k, a, b] for k, a, b in rows]))


def verdicts(rows, names=("Claude Code alone", "With Sampo")):
    """A table of commands and what each setup does with them; the stamp carries the outcome, never colour alone."""
    stamp = {"deny": '<span class="stamp stop">Stopped</span>', "saved": '<span class="stamp sig">Saved first</span>',
             "allow": '<span class="stamp warn">Runs</span>', "run": '<span class="stamp warn">Runs</span>'}
    body = "".join(f"<tr><td><code>{esc(r['cmd'])}</code><br><small>{esc(r.get('what', ''))}</small></td>"
                   f"<td>{stamp['run']}</td><td>{stamp.get(r['decision'], esc(r['decision']))}</td></tr>" for r in rows)
    return (f'<div class="verdicts"><table><thead><tr><th>Command</th><th>{esc(names[0])}</th><th>{esc(names[1])}</th>'
            f'</tr></thead><tbody>{body}</tbody></table></div>')


def columns(title, note, rows, keys, label_key="day", short=lambda s: s[5:]):
    """Columns over time, stacked when there is more than one key; a 2px surface gap between segments."""
    w, h, left, bottom, top_pad = 640, 240, 44, 26, 12
    totals = [sum(r.get(k, 0) for k in keys) for r in rows]
    top = max(totals + [1])
    step = 10 ** max(0, len(str(int(top))) - 1)
    top_tick = -(-top // step) * step
    plot_w, plot_h = w - left - 8, h - bottom - top_pad
    slot = plot_w / max(1, len(rows))
    bw = min(46, slot * 0.62)
    out = [f'<svg class="viz grow-y" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">']  # T-1615
    for t in range(0, int(top_tick) + 1, int(max(1, top_tick // 4))):
        y = top_pad + plot_h - plot_h * t / top_tick
        out.append(f'<line class="grid" x1="{left}" x2="{w - 8}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="tick" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{fmt(t)}</text>')
    for i, r in enumerate(rows):
        x = left + i * slot + (slot - bw) / 2
        y = top_pad + plot_h
        for j, k in enumerate(keys):
            v = r.get(k, 0)
            if not v:
                continue
            hh = plot_h * v / top_tick
            y -= hh
            gap = 2 if j else 0
            out.append(f'<rect class="m s{j + 1}" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(1, hh - gap):.1f}"'
                       f' rx="{3 if j == len(keys) - 1 or len(keys) == 1 else 1}">'
                       f'<title>{esc(r[label_key])} · {esc(k.title() if k.isupper() else k)}: {fmt(v)}</title></rect>')
        if len(rows) <= 16 or i % 2 == 0:
            out.append(f'<text class="tick" x="{x + bw / 2:.1f}" y="{h - 8}" text-anchor="middle">{esc(short(r[label_key]))}</text>')
    out.append(f'<line class="axis" x1="{left}" x2="{w - 8}" y1="{top_pad + plot_h}" y2="{top_pad + plot_h}"/></svg>')
    data = table([label_key] + keys + (["total"] if len(keys) > 1 else []),
                 [[r[label_key]] + [r.get(k, 0) for k in keys] + ([t] if len(keys) > 1 else []) for r, t in zip(rows, totals)])
    return figure(title, note, (legend(keys) if len(keys) > 1 else "") + "".join(out), data)


def line(title, note, rows, key, label_key):
    """One series over time, 2px line, the last value labelled at its end."""
    if not rows:  # site review: no release commits yet raised IndexError
        return figure(title, note, "<p>No data yet.</p>", "")
    w, h, left, bottom, top_pad = 640, 220, 50, 26, 16
    vals = [r[key] for r in rows]
    lo, hi = 0, max(vals + [1])
    step = 10 ** max(0, len(str(int(hi))) - 1)
    hi = -(-hi // step) * step
    plot_w, plot_h = w - left - 70, h - bottom - top_pad
    xs = [left + (plot_w * i / max(1, len(rows) - 1)) for i in range(len(rows))]
    ys = [top_pad + plot_h - plot_h * (v - lo) / (hi - lo) for v in vals]
    out = [f'<svg class="viz draw" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">']  # T-1615
    for t in range(0, int(hi) + 1, int(max(1, hi // 4))):
        y = top_pad + plot_h - plot_h * t / hi
        out.append(f'<line class="grid" x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="tick" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{fmt(t)}</text>')
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    out.append(f'<polyline class="ln s1" points="{pts}" fill="none" pathLength="1"/>')  # T-1615: drawn as a fraction
    for x, y, r in zip(xs, ys, rows):
        out.append(f'<circle class="dot s1" cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{esc(r[label_key])}: {fmt(r[key])}</title></circle>')
    out.append(f'<text class="val" x="{xs[-1] + 10:.1f}" y="{ys[-1] + 4:.1f}">{fmt(vals[-1])}</text>')
    for i in sorted({0, len(rows) // 2, len(rows) - 1}):  # two rows: each label once
        out.append(f'<text class="tick" x="{xs[i]:.1f}" y="{h - 8}" text-anchor="middle">{esc(rows[i][label_key])}</text>')
    out.append(f'<line class="axis" x1="{left}" x2="{left + plot_w}" y1="{top_pad + plot_h}" y2="{top_pad + plot_h}"/></svg>')
    return figure(title, note, "".join(out), table([label_key, key], [[r[label_key], r[key]] for r in rows]))


def tiles(pairs, cls=""):
    return f'<div class="tiles{" " + cls if cls else ""}">' + "".join(f'<div class="tile"><span>{esc(label)}</span>'
                                           f'<strong>{fmt(v) if isinstance(v, (int, float)) else esc(v)}</strong></div>'
                                           for label, v in pairs) + "</div>"


PROOF_MARK = "<!-- proof: build.py fills the dials (T-1609) -->"
SHADER_MARK = "<!-- shaders: build.py fills it with four shader cards running live (T-2062) -->"
LANDING_SHADERS = ("shader-aurora-bands", "shader-soft-caustics", "shader-grain-gradient", "shader-halftone")
MOONS_MARK = "<!-- moons: build.py fills the heading and the list from genesis.json's moonshots (T-2851) -->"
STATIONS = [("tasks_done", "tasks done"), ("evidence", "evidence runs"), ("proofs", "proofs"), ("commits", "commits")]


def moons_html(rows):
    """T-2851: the Moons section's heading, intro and list from genesis.json's moonshots: the wild 4s and 5s, the wildest
    first then by id, every count from the rows (the chart beside it is a drawing and says its own)."""
    n = len(rows)
    top = sorted((r for r in rows if (r.get("wild") or 0) >= 4), key=lambda r: (-r["wild"], r["id"]))  # T-2958: unrated
    kills = [r["id"] for r in rows if r["stage"] == "kill"]
    built = sum(r["stage"] == "built" for r in rows)
    ideas = sum(r["stage"] == "idea" for r in rows)
    kill = (f" {', '.join(kills[:-1]) + ' and ' if len(kills) > 1 else ''}{kills[-1]} "
            f"{'stand' if len(kills) > 1 else 'stands'} at {'their' if len(kills) > 1 else 'its'} kill test, a crescent."
            if kills else "")
    made = f" {built} {'is' if built == 1 else 'are'} built." if built else " None is built, so no moon is full."
    items = "".join(f'<li{" class=" + chr(34) + "kt" + chr(34) if r["stage"] == "kill" else ""}><b>{esc(r["id"])}</b>'
                    f'<span>{esc(r["title"])}<i>wild {r["wild"]} of 5 · {"kill test" if r["stage"] == "kill" else esc(r["stage"])}</i></span></li>'
                    for r in top)
    return (f'<p class="eyebrow">Moons and the Underworld</p>\n    <h2 id="moons-h">{n} moonshots over the forge, <em>the wildest first</em></h2>\n'
            f'    <p class="intro">Moons is the moonshot catalogue: ideas too large for a task, each with what it needs first. It holds {n}; '
            f'these are the {len(top)} rated 4 or 5 of 5 for wildness, wildest first. {ideas} of the {n} are still ideas, a thin sliver '
            f'on the chart;{kill}{made} <a class="more" href="genesis/index.html#moonshots">All of them in Rune&#x27;s sky</a></p>\n'
            f'    <ol class="moons">{items}</ol>')


def reading(n):
    """T-2759: a dial's needle, 0 to 1, a decade apart from 1 to 100,000, so a count of 7 and one of 7,000 both move."""
    return round(min(math.log10(max(n, 1)) / 5, 1), 3)


def proof(totals, asof=""):
    """T-1609, T-2040: the hero's proof ledger, the four totals from Sampo's own record as plain figures on hairlines
    (the diamond poster and its WebGL scene read as decoration, not data). Inline, so it is first paint. T-2759: each
    figure sits under a dial whose needle (--v) reads the same total; style.css draws the gear and the arc."""
    return dials([(label, totals.get(k) or 0) for k, label in STATIONS],
                 f'From Sampo\'s own record{", to " + esc(asof) if asof else ""}')


def dials(items, note):
    """T-2767: a row of dials, (label, count) each: the figure as text (site.js counts up to it), the needle its
    reading. NOTE is markup."""
    cells = "".join(f'<div style="--v:{reading(n):g}"><dt>{esc(label)}</dt><dd>{esc(fmt(n))}</dd></div>'
                    for label, n in items)
    return f'<div class="proof"><p>{note}</p><dl>{cells}</dl></div>'


# T-2903: the main nav, once, for the landing, the tower and every section page (label, path from the site root, shown: 0 from 1180 px, 2 from 940 px, 1 always)
NAV = (("How it works", "index.html#how", 0), ("Smith", "index.html#smith", 0), ("Rune", "genesis/index.html", 0),
       ("Ward", "index.html#ward", 0), ("Moons", "index.html#moons", 0), ("Record", "index.html#record", 0),
       ("Features", "features/index.html", 2), ("Cards", "cards.html", 1), ("Benchmarks", "features/benchmarks.html", 0),
       ("Install", "index.html#install", 1), ("GitHub", "https://github.com/ChaseSunstrom/sampo", 1))


def nav_links(up, here="", skip=(), hide=True, landing=False, menu=False):
    """NAV as links. up: the way to the site root; landing: its own anchors drop the file name; here: this page's path,
    marked current; skip: labels left out; hide: entries not always shown hide on a narrow screen; menu: after the
    links, a <details> listing every entry for the screens where some are hidden."""
    out = []
    for label, path, always in NAV:
        if label in skip:
            continue
        href = path if "//" in path else path[len("index.html"):] if landing and path.startswith("index.html#") else up + path
        cls = "" if always == 1 or not hide else ' class="mid-sm"' if always else ' class="hide-sm"'
        cur = ' aria-current="page"' if path == here else ""
        out.append(f'<a{cls} href="{href}"{cur}>{esc(label)}</a>')
    if menu:
        out.append('<details class="menu"><summary aria-label="All sections" title="All sections">&#9776;</summary><div class="menu-list">'
                   + nav_links(up, here, skip, False, landing) + "</div></details>")
    return "\n      ".join(out)


def layout(title, desc, body, pages, here, depth=1, script=""):
    """A section page. pages=None: one wide column with no side list of feature pages (site/cards.html); script: its own JS."""
    up = "../" * depth
    current = ' aria-current="page"'
    nav = "".join(f'<a href="{up}features/{p["slug"]}.html"{current if p["slug"] == here else ""}>{esc(p["nav"])}</a>'
                  for p in pages or ())
    shell = (f'<div class="wrap feature-shell">\n  <nav class="side" aria-label="Features">{nav}</nav>\n  '
             f'<main class="feature">{body}</main>\n</div>') if pages else f'<main class="wrap feature wide">{body}</main>'
    script = f'\n<script src="{up}{script}" defer></script>' if script else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>try {{ var t = localStorage.getItem("fm-theme"); if (t) document.documentElement.setAttribute("data-theme", t); }} catch (e) {{}}</script>
<title>{esc(title)} · Sampo</title>
<meta name="description" content="{esc(desc)}">
<meta property="og:title" content="{esc(title)} · Sampo">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:image" content="https://chasesunstrom.github.io/sampo/assets/share.png">
<link rel="icon" href="{up}{ICON}" type="image/svg+xml">
<link rel="icon" href="{up}{FAVICON}" sizes="32x32">
<link rel="stylesheet" href="{up}{CSS}">
<link rel="stylesheet" href="{up}{SECTIONS}">
</head>
<body>
<header class="top">
  <div class="wrap">
    <a class="brand" href="{up}index.html"><img src="{up}{ICON}" alt="" width="28" height="28">Sampo</a>
    <nav class="nav" aria-label="Main">
      {nav_links(up, here if here == "cards.html" else f"features/{here}.html", menu=True)}
      <button class="theme" type="button" aria-label="Switch between light and dark" title="Light or dark">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/></svg>
      </button>
    </nav>
  </div>
</header>
{shell}
<footer>
  <div class="wrap">
    <a class="brand" href="{up}index.html"><img src="{up}{ICON}" alt="" width="22" height="22">Sampo</a>
    <nav aria-label="Links">
      <a href="https://github.com/ChaseSunstrom/sampo">Source</a>
      <a href="https://github.com/ChaseSunstrom/sampo/blob/main/README.md">Read me</a>
      <a href="https://github.com/ChaseSunstrom/sampo/blob/main/CHANGELOG.md">Changelog</a>
    </nav>
    <p class="fine">A community project for coding agents. Not made by or affiliated with Anthropic or any agent's maker. Charts are drawn from Sampo's own record of building itself.</p>
  </div>
</footer>
<script src="{up}{JS}"></script>{script}
</body>
</html>
"""


# T-1320: real fm code runs, one per kind of request, kept as their event streams (fm code --json) in demo/examples/
EXAMPLES = [("fix", "Fix a bug"), ("feature", "Add a feature"), ("rename", "Rename across files"),
            ("docs", "Write docs"), ("question", "Ask about the code"), ("new-project", "Start from an empty folder"),
            ("javascript", "Another language")]


def load_example(path):
    """One run's events → what its card shows: the request, the plan, each check before and after, the files, the
    worker turns, tokens, time and how it ended."""
    with open(path, encoding="utf-8") as f:
        ev = [json.loads(line) for line in f if line.startswith("{")]
    first = lambda kind: next((e for e in ev if e.get("event") == kind), {})
    checks = {}
    for e in ev:
        if e.get("event") == "verify":
            checks.setdefault(e.get("criterion"), {})["before" if e.get("label") == "before" else "after"] = e.get("exit")
    stat = {}
    for e in ev:
        if e.get("event") == "changed":
            stat.update(e.get("stat") or {f: stat.get(f, [0, 0]) for f in e.get("files") or []})
    tokens = next((e.get("total_in", 0) + e.get("total_out", 0) for e in reversed(ev) if e.get("event") == "tokens"), 0)
    ts = [e["ts"] for e in ev if isinstance(e.get("ts"), (int, float))]
    end = next((e for e in reversed(ev) if e.get("event") in ("closed", "blocked", "error", "answer")), {})
    return {"request": first("start").get("request", ""), "worker": first("start").get("worker"),
            "plan": first("plan").get("plan") or {}, "checks": checks, "stat": stat, "tokens": tokens,
            "turns": sum(e.get("event") == "worker" for e in ev), "seconds": round(ts[-1] - ts[0]) if ts else None,
            "end": end.get("event"), "end_text": end.get("text", ""), "events": ev}


def _took(sec):
    return "" if sec is None else f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


def example_html(name, label, ex):
    pl, worker = ex["plan"], ex["worker"]
    cmd = f"sampo code {shlex.quote(ex['request'])}" + (f" --worker {shlex.quote(worker)}" if worker else "")
    rows = []
    for i, c in enumerate(pl.get("criteria") or [], 1):
        got = ex["checks"].get(i, {})
        after = got.get("after")
        chip = ("pass", "PASS") if after == 0 else ("fail", "FAIL") if after is not None else ("todo", "····")
        first = " (failed before the work)" if got.get("before") not in (0, None) else ""
        rows.append(f'<li><span class="chip {chip[0]}">{chip[1]}</span> {esc(c.get("text", ""))}'
                    f'<code>{esc(c.get("verify", ""))}</code><small>{first}</small></li>')
    files = "".join(f"<li><code>{esc(f)}</code> <span class=\"plus\">+{a}</span>"
                    + (f' <span class="minus">−{d}</span>' if d else "") + "</li>" for f, (a, d) in ex["stat"].items())
    facts = [f"{ex['turns']} worker turn{'s' if ex['turns'] != 1 else ''}" if ex["turns"] else "",
             f"{ex['tokens'] / 1000:.0f}k tokens" if ex["tokens"] else "", _took(ex["seconds"])]
    end = {"closed": "Closed", "blocked": "Blocked", "error": "Stopped", "answer": "Answer"}.get(ex["end"], "Ended")
    return (f'<article class="ex" id="ex-{name}"><header><span class="ex-kind">{esc(label)}</span>'
            f'<p class="ex-cmd"><code>{esc(cmd)}</code></p></header><div class="ex-body">'
            f'<img class="ex-shot" src="../assets/examples/{name}.svg" loading="lazy" alt="sampo code tui at the end of this '
            f'run: {esc(pl.get("title", ""))}"><div class="ex-facts">'
            + (f'<p><b>Plan</b> {esc(pl.get("type", ""))} {esc(pl.get("tier", ""))} · {esc(pl.get("title", ""))}</p>'
               if pl.get("type") else '<p><b>Plan</b> a question: answered from the files, no task, nothing changed</p>')
            + (f'<p><b>Steps</b></p><ol class="ex-steps">{"".join(f"<li>{esc(s)}</li>" for s in pl.get("steps") or [])}</ol>'
               if pl.get("steps") else "")
            + (f'<p><b>Checks</b></p><ul class="ex-checks">{"".join(rows)}</ul>' if rows else "")
            + (f'<p><b>Changed</b></p><ul class="ex-files">{files}</ul>' if files else "")
            + (f'<div class="ex-answer"><p><b>{end}</b></p>{"".join(f"<p>{esc(x)}</p>" for x in ex["end_text"].split(chr(10)) if x.strip())}</div>'
               if ex["end"] == "answer" else f'<p class="ex-end"><b>{end}</b> {esc(ex["end_text"])}</p>')
            + f'<p class="ex-meta">{esc(" · ".join(x for x in facts if x))} · replay it: '
              f'<code>sampo code tui --events demo/examples/{name}.jsonl</code> · '
              f'<a href="../stories/{name}.html">the case study</a></p></div></div></article>')


STEP_KINDS = {"before": "before", "verify": "check"}


def case_study_html(name, label, ex):
    """T-1541: one recorded run as a case study: its card, then every event in order at the time it came: the checks
    failing before the work, each step and what it changed, the checks after, and how it ended."""
    ev = ex["events"]
    t0 = next((e["ts"] for e in ev if isinstance(e.get("ts"), (int, float))), 0)
    rows = []
    for e in ev:
        kind, at = e.get("event"), e.get("ts")
        when = (_took(round(at - t0)) or "0s") if isinstance(at, (int, float)) else ""
        if kind == "verify":
            ok = e.get("exit") == 0
            rows.append((when, "before" if e.get("label") == "before" else "check",
                         f"{'passes' if ok else 'fails'}: <code>{esc(str(e.get('cmd') or ''))}</code>",
                         "pass" if ok else "fail"))
        elif kind == "step":
            rows.append((when, "step", esc(str(e.get("text") or f"step {e.get('step')}")), ""))
        elif kind == "changed":
            rows.append((when, "changed", ", ".join(f"<code>{esc(str(f))}</code>" for f in e.get("files") or []), ""))
        elif kind in ("closed", "blocked", "error", "answer"):
            rows.append((when, kind, esc(str(e.get("text") or "")), "pass" if kind == "closed" else ""))
    timeline = "".join(f'<li class="{cls}"><span class="at">{w}</span> <b>{esc(k)}</b> {txt}</li>'
                       for w, k, txt, cls in rows)
    title = ex["plan"].get("title") or ex["request"]
    return (f'<p class="kicker eyebrow">Case study · {esc(label)}</p><h1 class="engraved">{esc(title)}</h1><p class="lede">{esc(ex["request"])}'
            f'</p>{example_html(name, label, ex)}<section><h2>The run, event by event</h2>'
            f'<ol class="timeline">{timeline}</ol></section>'
            '<p class="next"><a href="../features/code.html">Back to sampo code</a></p>')


def examples_html(folder=None):
    folder = folder or os.path.join(ROOT, "demo", "examples")
    cards = [example_html(n, label, load_example(os.path.join(folder, n + ".jsonl")))
             for n, label in EXAMPLES if os.path.isfile(os.path.join(folder, n + ".jsonl"))]
    return f'<div class="examples">{"".join(cards)}</div>' if cards else ""


def render_page(p, stats, pages):
    parts = [f'<p class="kicker eyebrow">{esc(p["nav"])}</p><h1 class="engraved">{esc(p["title"])}</h1><p class="lede">{p["lede"]}</p>']
    for block in p["blocks"]:
        kind = block[0]
        if kind == "text":  # T-1246: its body may be computed from the stats
            body = block[2](stats) if callable(block[2]) else block[2]
            parts.append(f"<section><h2>{esc(block[1])}</h2>{body}</section>")
        elif kind == "term":
            parts.append(f'<div class="term forged"><pre>{block[1]}</pre></div>')
        elif kind == "chart":
            parts.append(block[1](stats))
        elif kind == "raw":
            parts.append(block[1])
        elif kind == "examples":  # T-1320
            parts.append(f"<section><h2>{esc(block[1])}</h2>{block[2]}{examples_html()}</section>")
    i = [q["slug"] for q in pages].index(p["slug"])
    nxt = pages[(i + 1) % len(pages)]
    parts.append(f'<p class="next"><a href="{nxt["slug"]}.html">Next: {esc(nxt["nav"])}</a></p>')
    return layout(p["title"], p["desc"], "".join(parts), pages, p["slug"])


def render_index(pages):
    cards = "".join(f'<a class="fcard" href="{p["slug"]}.html"><b>{esc(p["nav"])}</b><span>{esc(p["desc"])}</span></a>'
                    for p in pages)
    body = ('<p class="kicker eyebrow">Features</p><h1 class="engraved">Everything Sampo does</h1><p class="lede">Each page shows what one part '
            'of Sampo does, how it works, the commands, and charts from Sampo\'s own record of building itself.</p>'
            f'<div class="fgrid">{cards}</div>')
    return layout("Features", "Every part of Sampo, with charts from its own history.", body, pages, "index")


def roles(v):
    """T-2852: the records' words read a model by its role and the orchestrator as any agent: an endpoint's name
    ("jarvis") becomes "a large local model" and a bare "Claude" doing the work "the agent". The hosts Sampo is
    measured on and installs into (Claude Code, Claude Desktop) keep their names."""
    if isinstance(v, str):
        # a repo named jarvis ("jarvis (1,853)") and Claude's model names (Claude Opus) are facts, kept
        return re.sub(r"\bClaude\b(?! Code| Desktop| Haiku| Sonnet| Opus| Fable)", "the agent",
                      re.sub(r"(?i)[\w-]*jarvis[\w-]*(?!\s*\(\d)", "a large local model", v))
    if isinstance(v, list):
        return [roles(x) for x in v]
    if isinstance(v, dict):
        return {roles(k): roles(x) for k, x in v.items()}
    return v


# T-3101: the card store (plugin/lib/sampo/smallcards.py) as site/cards/. index.json is a small manifest: the counts per
# grid and cell (the page's facet counts, exact before a page loads) and the files. index.<n>.json are pages of CARD_PAGE
# cards in id order, each card's id, grid, cell, title, when line and commands, which the page searches (~420 B a card);
# <grid>-<n>.json hold the bodies (steps, example, check, traps) in blocks of CARD_BLOCK cards per grid, fetched when a
# card opens. Ids only grow, so a new card rewrites the manifest, the last page and its own block, nothing else.
CARD_BLOCK = 250
CARD_PAGE = 2000
CARD_GRIDS = {"general": ("kind", "stack", "stage", "surface"), "ui": ("component", "platform", "concern", "stage"),
              "variants": ("playbook", "profile")}
CARD_FILE = re.compile(r"(?:[a-z0-9]+-|index\.)\d+|kit")  # what the export writes, the only names it ever removes


def _card(r):
    """(grid, index entry, body) of one stored card; None for a row that isn't one."""
    if not isinstance(r, dict) or not re.fullmatch(r"K-\d{1,9}", str(r.get("id") or "")) or not str(r.get("title") or "").strip():
        return None
    cell = r.get("cell")
    if isinstance(cell, dict):
        grid = str(cell.get("grid") or "general")
        grid = grid if re.fullmatch(r"[a-z0-9]{1,20}", grid) else "other"  # it names files: never a path
        cell = {str(k): str(v) for k, v in cell.items() if k != "grid"}
    elif r.get("playbook"):  # T-2917: a playbook's small-model variant fills no cell; its playbook by name, never a path
        book = str(r["playbook"]).replace("\\", "/")
        book = book.rsplit("references/", 1)[-1] if "references/" in book else book.rsplit("/", 1)[-1]
        grid, cell = "variants", {"playbook": book.removesuffix(".md"), "profile": str(r.get("profile") or "")}
    else:
        grid, cell = "other", {}
    steps = [{"do": str(s.get("do") or ""), "cmd": str(s.get("cmd") or "")} for s in r.get("steps") or []
             if isinstance(s, dict)]
    entry = {"id": r["id"], "grid": grid, "cell": cell, "title": str(r["title"]), "when": str(r.get("when") or ""),
             "cmds": "\n".join(s["cmd"] for s in steps if s["cmd"])}
    traps = r.get("traps")
    body = {"steps": steps, "example": str(r.get("example") or ""), "check": str(r.get("check") or ""),
            "traps": [str(t) for t in traps] if isinstance(traps, list) else []}
    return grid, entry, body


def _lines(head, rows, tail):
    """JSON with one row per line, so a new card is a new line in git and not a rewritten one."""
    return head + ("\n" + ",\n".join(rows) + "\n" if rows else "") + tail + "\n"


def export_cards(rows, dest, block=CARD_BLOCK, page=CARD_PAGE, examples=None, kit=None, frame=None):
    """Write rows (smallcards.rows()) to dest as the index.json manifest, index.<n>.json pages and <grid>-<n>.json body
    shards, each only when its text changes; a file the last export wrote that nothing fills any more goes.
    T-3102: given the kit ({component: {"html", "h"}}), each UI card's body gets a "demo": its own kept example
    (examples, {card: {"html", "h"}}) else {"kit": its component}; kit.json holds the kit and the frame, the page an
    example sits in (smallcards.doc). (cards exported, files written or removed)."""
    sys.path.insert(0, os.path.join(HERE, "tools"))
    from writeif import write_if_changed
    seen, cards = set(), []
    for r in rows:
        got = _card(r)
        if got and got[1]["id"] not in seen:
            seen.add(got[1]["id"])
            cards.append(got)
    cards.sort(key=lambda g: int(g[1]["id"][2:]))
    order = [g for g in CARD_GRIDS if any(c[0] == g for c in cards)] + sorted({c[0] for c in cards} - set(CARD_GRIDS))
    shards, grids, files, pages = {}, {}, {}, []
    dump = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    try:  # the files the last export wrote: the only ones a stale file can be (dest may be any folder)
        with open(os.path.join(dest, "index.json"), encoding="utf-8") as f:
            last = json.load(f)
        before = set(last.get("files") or ()) | {c.get("shard") for c in last.get("cards") or ()}  # v1 named shards only
    except (OSError, ValueError, TypeError, AttributeError):
        before = set()
    for g in order:
        mine = [c for c in cards if c[0] == g]
        fields = CARD_GRIDS.get(g) or sorted({k for c in mine for k in c[1]["cell"]})
        cells = {f: {} for f in fields}
        for i, (_, entry, body) in enumerate(mine):
            entry["shard"] = f"{g}-{i // block + 1}"
            if kit and g == "ui":
                own, comp = (examples or {}).get(entry["id"]), entry["cell"].get("component")
                if own or comp in kit:
                    body["demo"] = own or {"kit": comp}
            shards.setdefault(entry["shard"], []).append(dump({entry["id"]: body})[1:-1])
            for f in fields:
                v = entry["cell"].get(f)
                if v:
                    cells[f][v] = cells[f].get(v, 0) + 1
        grids[g] = {"fields": list(fields), "count": len(mine), "cells": cells}
    for n, at in enumerate(range(0, len(cards), page), 1):
        chunk = cards[at:at + page]
        files[f"index.{n}"] = _lines('{"cards":[', [dump(c[1]) for c in chunk], "]}")
        pages.append(dump({"name": f"index.{n}", "count": len(chunk), "first": chunk[0][1]["id"], "last": chunk[-1][1]["id"],
                           "grids": {g: k for g in order if (k := sum(c[0] == g for c in chunk))}}))
    for name, bodies in shards.items():
        files[name] = _lines("{", bodies, "}")
    if kit and "ui" in grids:
        files["kit"] = dump({"frame": frame or "", "kit": kit}) + "\n"
    wrote = sum(write_if_changed(os.path.join(dest, name + ".json"), text) for name, text in files.items())
    sums = {name: hashlib.sha256(text.encode()).hexdigest()[:8] for name, text in files.items()}  # the page's ?v=
    wrote += write_if_changed(os.path.join(dest, "index.json"), _lines(
        '{"v":2,"count":%d,"page":%d,"grids":%s,"files":%s,"pages":[' % (len(cards), page, dump(grids), dump(sums)),
        pages, "]}"))
    for name in sorted(n for n in before - set(files) if isinstance(n, str) and CARD_FILE.fullmatch(n)):
        if os.path.isfile(os.path.join(dest, name + ".json")):
            os.remove(os.path.join(dest, name + ".json"))
            wrote += 1
    return len(cards), int(wrote)


def build(out):
    sys.path.insert(0, HERE)
    import pages as P
    with open(os.path.join(HERE, "data", "stats.json"), encoding="utf-8") as f:
        stats = json.load(f)
    bench_path = os.path.join(HERE, "data", "bench.json")
    if os.path.isfile(bench_path):  # T-1222: the safety benchmark (site/bench_safety.py)
        with open(bench_path, encoding="utf-8") as f:
            stats.update(json.load(f))
    sys.path.insert(0, os.path.join(ROOT, "plugin", "lib"))
    from sampo import louhi
    mdir, rows, frozen = os.path.join(ROOT, "research", "models"), [], {}  # T-3093: the model bench, tolerant of absent files
    for name in ("bench.jsonl", "frozen.json"):
        try:
            with open(os.path.join(mdir, name), encoding="utf-8") as f:
                if name == "frozen.json":
                    frozen = (json.load(f) or {}).get("ids") or {}
                else:
                    rows = [json.loads(x) for x in f if x.strip()]
        except (OSError, ValueError, AttributeError):
            pass
    stats["model_bench"] = louhi.models_view(rows, frozen)
    stats = roles(stats)
    with open(os.path.join(HERE, "style.css"), encoding="utf-8") as f:
        check_tokens(f.read())
    if os.path.isdir(out):
        shutil.rmtree(out)
    shutil.copytree(HERE, out, ignore=lambda d, names: [n for n in names if n in SOURCE_ONLY])
    shutil.copyfile(os.path.join(out, ICON.split("?")[0]), os.path.join(out, "assets", "foreman.svg"))  # old links, new mark
    with open(os.path.join(out, "index.html"), encoding="utf-8") as f:  # T-1609: the hero's poster
        page = f.read()
    for what, mark in (("proof", PROOF_MARK), ("shaders", SHADER_MARK), ("moons", MOONS_MARK)):  # review: a lost mark shipped a hero with no proof
        if mark not in page:
            raise SystemExit(f"build.py: site/index.html lost the {what} mark: {mark}")
    totals, asof = stats.get("totals") or {}, str(stats.get("generated") or "")
    sys.path.insert(0, os.path.join(ROOT, "plugin", "lib"))  # T-1283: fm code tui draws its own picture
    from sampo import smithtui
    from sampo import replay
    from sampo import scene
    looks = [c for c in scene.sketch_cards() if c["name"] in LANDING_SHADERS]
    if len(looks) != len(LANDING_SHADERS):  # a renamed card would ship a short strip
        raise SystemExit("build.py: a landing shader card is missing: " + ", ".join(LANDING_SHADERS))
    page = page.replace(SHADER_MARK, scene.sketchbook_html(looks, page=False), 1)
    cards = stats.get("cards") or {}
    counts = {"tasks": totals.get("tasks_done", 0), "tests": (stats.get("code") or {}).get("tests", 0),  # T-2040
              "cards": sum(cards.values()), "areas": len(cards), "pages": len(P.PAGES), "replay": replay.MAX}
    if not (counts["tests"] and counts["cards"]):  # review: a stats.json without them shipped "0 tests"
        raise SystemExit("build.py: site/data/stats.json has no code.tests or cards; run site/stats.py")
    page = page.replace(PROOF_MARK, proof(totals, asof), 1)
    with open(os.path.join(HERE, "genesis", "genesis.json"), encoding="utf-8") as f:
        page = page.replace(MOONS_MARK, moons_html(json.load(f)["moonshots"]["rows"]), 1)
    for old, new in (('rel="icon" href="assets/sampo.svg"', f'rel="icon" href="{ICON}"'),
                     ('rel="icon" href="assets/favicon.ico"', f'rel="icon" href="{FAVICON}"'),
                     ('rel="stylesheet" href="style.css"', f'rel="stylesheet" href="{CSS}"'),
                     ('<img src="assets/sampo.svg"', f'<img src="{ICON}"'),
                     ('rel="stylesheet" href="forge.css"', f'rel="stylesheet" href="{FORGE_CSS}"'),
                     ('<script src="forge.js">', f'<script src="{FORGE_JS}">'),
                     ('<script src="site.js">', f'<script src="{JS}">')):
        if old not in page:  # a reworded tag would ship the old, cached file
            raise SystemExit(f"build.py: site/index.html lost {old}")
        page = page.replace(old, new)
    page = (page.replace("{{nav}}", nav_links("", skip=("GitHub",), hide=False, landing=True))  # T-2903
            .replace("{{menu}}", nav_links("", hide=False, landing=True)))
    for k, v in counts.items():
        page = page.replace("{{" + k + "}}", fmt(v))
    if "{{" in page:  # a count the build doesn't know would ship as braces
        raise SystemExit("build.py: site/index.html has an unknown {{count}}: " + page[page.index("{{"):][:40])
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    gpath = os.path.join(out, "genesis", "index.html")  # T-2087: the Genesis page shares the site's stylesheet and icon
    with open(gpath, encoding="utf-8") as f:
        gpage = f.read()
    if 'href="../style.css"' not in gpage:
        raise SystemExit('build.py: site/genesis/index.html lost href="../style.css"')
    gpage = gpage.replace("{{nav}}", nav_links("../", "genesis/index.html", menu=True))  # T-2903
    gpage = (gpage.replace('href="../style.css"', f'href="../{CSS}"').replace('"../assets/sampo.svg"', f'"../{ICON}"')
             .replace('"../assets/favicon.ico"', f'"../{FAVICON}"'))
    with open(gpath, "w", encoding="utf-8") as f:
        f.write(gpage)
    os.makedirs(os.path.join(out, "demo"), exist_ok=True)
    for f in ("live.mp4", "demo.svg"):
        shutil.copy2(os.path.join(ROOT, "demo", f), os.path.join(out, "demo", f))
    with open(os.path.join(ROOT, "demo", "code-tui.jsonl"), encoding="utf-8") as f:
        smithtui.snapshot([json.loads(line) for line in f if line.strip()], os.path.join(out, "assets", "code-tui.svg"),
                           (120, 34))
    smithtui.tour_svg(os.path.join(out, "assets", "code-tui-tour.svg"), (120, 34))  # T-2061: today's TUI, every build
    os.makedirs(os.path.join(out, "assets", "examples"), exist_ok=True)  # T-1320: each example's last screen
    for name, _ in EXAMPLES:
        src = os.path.join(ROOT, "demo", "examples", name + ".jsonl")
        if os.path.isfile(src):
            smithtui.snapshot(load_example(src)["events"], os.path.join(out, "assets", "examples", name + ".svg"),
                               (120, 34))
    os.makedirs(os.path.join(out, "stories"), exist_ok=True)  # T-1541: each recorded run as a case study
    for name, label in EXAMPLES:
        src = os.path.join(ROOT, "demo", "examples", name + ".jsonl")
        if os.path.isfile(src):
            ex = load_example(src)
            with open(os.path.join(out, "stories", name + ".html"), "w", encoding="utf-8") as f:
                f.write(layout(f"Case study: {ex['plan'].get('title') or label}", ex["request"][:150],
                               case_study_html(name, label, ex), P.PAGES, None))
    os.makedirs(os.path.join(out, "gallery"), exist_ok=True)  # T-1785: each UI playbook's worked example
    for name, _, _, src in P.gallery():
        if not os.path.isfile(src):
            raise SystemExit(f"build.py: ui/{name}.md's Example: line names a file that doesn't exist: {src}")
        shutil.copy2(src, os.path.join(out, "gallery", name + ".html"))
    os.makedirs(os.path.join(out, "features"), exist_ok=True)
    for p in P.PAGES:
        with open(os.path.join(out, "features", p["slug"] + ".html"), "w", encoding="utf-8") as f:
            f.write(render_page(p, stats, P.PAGES))
    with open(os.path.join(out, "features", "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(P.PAGES))
    index = os.path.join(out, "cards", "index.json")  # T-3101: the committed export (build.py --cards); none yet → empty
    if not os.path.isfile(index):
        export_cards([], os.path.dirname(index))
    with open(index, "rb") as f:
        index_url = "cards/index.json?v=" + hashlib.sha256(f.read()).hexdigest()[:8]
    with open(os.path.join(out, "cards.html"), "w", encoding="utf-8") as f:
        f.write(layout("Cards", "Every small card Sampo has written: search them, filter by grid and cell, open one in place.",
                       P.cards_page(index_url), None, "cards.html", depth=0, script=_v("cards.js")))
    return len(P.PAGES)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "_site")
    if sys.argv[1:] == ["--tokens"]:  # T-2753: rewrite style.css's tokens block after design/sampo/tokens.json changes
        write_tokens(os.path.join(HERE, "style.css"))
        print("site/style.css: tokens block written from design/sampo/tokens.json")
        sys.exit()
    if sys.argv[1:2] == ["--cards"] and len(sys.argv) <= 3:  # T-3101: the card store → site/cards, committed for CI
        sys.path.insert(0, os.path.join(ROOT, "plugin", "lib"))
        from sampo import smallcards
        dest = sys.argv[2] if len(sys.argv) == 3 else os.path.join(HERE, "cards")
        n, wrote = export_cards(smallcards.rows(), dest, examples=smallcards.examples(), kit=smallcards.kit(),
                                frame=smallcards.doc("", "light"))
        print(f"{dest}: {n} cards, {wrote} files written")
        sys.exit()
    if target.startswith("-"):  # its options are --tokens and --cards: `--check` once became a folder that got committed
        print("usage: python3 site/build.py [OUTPUT_FOLDER] | --tokens | --cards [FOLDER] (default _site, site/cards)",
              file=sys.stderr)
        sys.exit(2)
    if not safe_out(target):
        sys.exit(f"build.py: refusing to wipe {target}: give a new or build-only folder, such as _site")
    n = build(os.path.abspath(target))
    outside = outside_hosts(target)
    if outside:
        sys.exit("build.py: the site loads nothing from another host (T-1411), but these would:\n  " + "\n  ".join(outside))
    print(f"built {target}: landing page + {n} feature pages")
