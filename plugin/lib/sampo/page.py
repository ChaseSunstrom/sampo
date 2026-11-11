"""fm page (T-0786): what's on now (active task and step, queue, inbox and blocked counts, last closes) and, with
--catalogue NAME, a research catalogue's clusters with a Build/Veto pick each, as one self-contained HTML file for a
claude.ai Artifact (private, phone-reachable; picks live in its db collection "picks"). Only ids, titles, tiers, marks
and counts go in; every string is redacted and the write is refused while any still holds an absolute path. fm page
never publishes: it prints the Artifact line for the main thread. T-1649: fm page looks writes the looks gallery
instead, one living specimen per look card (looks.html)."""
import html
import json
import os
import re
import time

from sampo import core as c

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "page.html")
LOOKS_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "looks.html")
# T-1649: a look card's type class → faces already on the device (the gallery loads no web font); longest name first
FACES = {
    "condensed grotesk": '"Barlow Condensed", "Roboto Condensed", "Avenir Next Condensed", "Arial Narrow", '
                         '"Nimbus Sans Narrow", "Liberation Sans Narrow", sans-serif',
    "transitional serif": 'Charter, "Bitstream Charter", "Sitka Text", Cambria, "Source Serif 4", "Noto Serif", '
                          'Georgia, serif',
    "old-style serif": '"Iowan Old Style", "Palatino Linotype", Palatino, "URW Palladio L", P052, "EB Garamond", '
                       'Garamond, serif',
    "geometric sans": 'Futura, "Century Gothic", Avenir, "URW Gothic", Jost, Montserrat, Corbel, sans-serif',
    "humanist sans": 'Seravek, "Gill Sans Nova", "Source Sans 3", "Fira Sans", Ubuntu, Calibri, "Noto Sans", '
                     'sans-serif',
    "neo-grotesk": 'Inter, "Adwaita Sans", Roboto, "Helvetica Neue", "Arial Nova", "Nimbus Sans", Arial, sans-serif',
    "grotesk": 'Archivo, "Schibsted Grotesk", "Libre Franklin", "Franklin Gothic Medium", "Helvetica Neue", Arial, '
               'sans-serif',
    "slab": 'Rockwell, "Rockwell Nova", "Roboto Slab", "Zilla Slab", "Sitka Small", "DejaVu Serif", serif',
    "mono": 'ui-monospace, "SF Mono", "Cascadia Code", "Source Code Pro", Menlo, Consolas, "Adwaita Mono", monospace',
}
FACE = re.compile(r"(?i)(?<![\w-])(" + "|".join(map(re.escape, FACES)) + r")(?![\w-])")
ROLE = re.compile(r"(?m)^\|\s*`(--[a-z][a-z-]*)`\s*\|\s*(#[0-9A-Fa-f]{6})\s*\|")
PLATE = re.compile(r"<!--look (look-[a-z0-9-]+)-->\n?(.*?)<!--/look-->\n?", re.S)
ABSOLUTE = re.compile(r"(?<![\w.-])(?:/home/|/Users/|/root/)")  # a bare ~/ names no user (review of T-0786)
CLUSTER = ("id", "capability", "mark", "why", "grounding", "value", "effort", "risk")
IDEA = ("n", "title", "type", "value", "effort", "lens")


class Leak(Exception):
    pass


def _private():
    """Review of T-0786: the user's home in any spelling a path takes (/var/home/x, -home-x- in an encoded folder
    name), not only the three prefixes. T-0821: plus the bare username as a /, \\ or - delimited word, which clean()
    replaces with 'user' rather than refusing (a repo named after the user isn't a path)."""
    import getpass
    home, user = os.path.expanduser("~").rstrip("/"), getpass.getuser()
    parts = [re.escape(home), re.escape(home.replace("/", "-")) + "(?![^/\\\\-])"] if home not in ("", "/") else []
    word = rf"(?:^|(?<=[/\\-])){re.escape(user)}(?=[/\\-]|$)" if user and len(user) >= 2 else None
    return (re.compile("|".join(parts), re.I) if parts else None), (re.compile(word, re.I) if word else None)


def clean(obj, where=""):
    """obj with every string redacted; Leak names the first field that still holds an absolute path."""
    if isinstance(obj, dict):
        return {k: clean(v, f"{where}.{k}" if where else k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean(v, f"{where}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, str):
        obj = c.redact(obj)
        home, user = _private()
        if ABSOLUTE.search(obj) or re.search(r"(?i)\b[a-z]:\\users\\", obj) or home and home.search(obj):
            raise Leak(where)
        obj = user.sub("user", obj) if user else obj
    return obj


def now_data(p):
    briefs = c.load_briefs(p)
    sd = c.state_dict(p, briefs)
    a = sd["active"]
    closed = sorted((b for b in c.lane_view(briefs, p.lane) if b.status == "done"),
                    key=lambda b: str(b.meta.get("updated") or ""), reverse=True)[:5]
    return {"active": a and {"id": a["id"], "title": a["title"], "type": a["type"], "tier": a["tier"],
                             "step": a["step"] and {"n": a["step"]["n"], "of": a["step"]["of"]}},
            "queue": [{k: q[k] for k in ("id", "type", "tier", "title")} for q in sd["queue"]],
            "inbox": len(sd["inbox"]), "blocked": len(sd["blocked"]),
            "closed": [{"id": b.id, "type": b.type, "title": b.title} for b in closed]}


def load_catalogue(p, name):
    from sampo import cli
    if name.endswith(".json") and os.path.lexists(name):  # a catalogue file anywhere (the brainstorm's scratch copy)
        if os.path.islink(name) or not os.path.isfile(name):
            raise cli.UsageError(f"{name} must be a plain file, not a link")
        with open(name, encoding="utf-8") as f:
            return _clusters(cli, name, json.load(f))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", name):
        raise cli.UsageError(f"catalogue name must be a plain file name, got {name!r}")
    folder = os.path.join(p.dir, "research")
    for path in (os.path.join(folder, name + ".json"), os.path.join(folder, name, "catalogue.json")):
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
            break
    else:
        raise cli.UsageError(f"no catalogue {name!r}: save it as research/{name}.json or research/{name}/catalogue.json")
    return _clusters(cli, name, rows)


def _clusters(cli, name, rows):
    if not isinstance(rows, list):
        raise cli.UsageError(f"catalogue {name!r} must be a list of clusters")
    rows = [r for r in rows if isinstance(r, dict) and r.get("id") is not None]  # T-0821: no id, no pick: skipped
    bad = [r["id"] for r in rows if not re.fullmatch(r"[\w-]{1,100}", str(r["id"]))]
    if bad:  # the page keeps picks at db doc picks/<id>: a slash would reach elsewhere in the store
        raise cli.UsageError(f"catalogue {name!r}: cluster id {bad[0]!r} must be letters, digits, _ or -")
    ideas = lambda r: [i for i in r.get("ideas") if isinstance(i, dict)] if isinstance(r.get("ideas"), list) else []
    return [dict({k: r.get(k) for k in CLUSTER}, ideas=[{k: i.get(k) for k in IDEA} for i in ideas(r)]) for r in rows]


def embed(data):
    """JSON for a <script> block: no '<' survives, so nothing in it can close the tag or open a comment."""
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def save_picks(p, args, catalogue):
    """T-0962: the Build/Veto picks read back from the Artifact ({cluster id: add|veto}) into state picks.json, every
    id checked against the catalogue the page showed."""
    import sys
    from sampo import cli
    if not args.catalogue:
        raise cli.UsageError("--picks needs --catalogue NAME: the cluster ids it checks against")
    try:
        if args.picks == "-":
            picks = json.load(sys.stdin)
        else:
            with open(args.picks, encoding="utf-8") as f:
                picks = json.load(f)
    except (OSError, ValueError) as e:
        raise cli.UsageError(f"--picks: can't read {args.picks}: {e}")
    if not isinstance(picks, dict):
        raise cli.UsageError('--picks must be a JSON object {"cluster-id": "add" | "veto"}')
    known = {str(r["id"]) for r in catalogue}
    unknown = sorted(k for k in picks if k not in known)
    if unknown:
        raise cli.UsageError(f"--picks: unknown cluster id(s) {', '.join(map(repr, unknown))}; "
                               f"{args.catalogue} has {', '.join(sorted(known)) or 'none'}")
    bad = sorted(k for k, v in picks.items() if v not in ("add", "veto"))
    if bad:
        raise cli.UsageError(f"--picks: each choice must be add or veto, not {picks[bad[0]]!r} ({bad[0]})")
    name = os.path.basename(args.catalogue).removesuffix(".json")
    path = os.path.join(p.dir, "picks.json")
    c.write_atomic(path, json.dumps({"catalogue": name, "at": c.now(), "picks": picks}, indent=1) + "\n")
    by = {ch: sorted(k for k, v in picks.items() if v == ch) for ch in ("add", "veto")}
    return cli.out(args, {"path": path, "catalogue": name, **by},
                     f"Picks for {name}: {len(by['add'])} add ({', '.join(by['add']) or '-'}), "
                     f"{len(by['veto'])} veto ({', '.join(by['veto']) or '-'}); saved to {path}")


def look_cards(folder=None):
    """T-1649: [{name, title, line, roles {--role: #HEX}, faces [type class…], motion, texture}] for each look card,
    the set fmtaste.looks finds (the one rule for what a look card is)."""
    from sampo import taste
    folder = folder or taste.LOOKS
    out = []
    for name, look in taste.looks(folder).items():
        part = lambda h: taste.section(look["body"], h)
        faces = list(dict.fromkeys(m.group(1).lower() for m in FACE.finditer(part("Type"))))
        out.append({"name": re.sub(r"[^a-z0-9-]", "-", name.lower()), "title": look["title"], "line": look["line"],
                    "roles": dict(ROLE.findall(part("Palette"))), "faces": faces or ["neo-grotesk"],
                    "motion": part("Motion"), "texture": part("Texture")})
    return out


def _prose(text):
    """A card's first sentence as HTML: escaped, `code` spans kept."""
    first = re.split(r"(?<=\.)\s", " ".join(text.split()), 1)[0]
    return re.sub(r"`([^`<]+)`", r"<code>\1</code>", html.escape(first))


def looks_html(cards):
    """T-1649: the gallery, one section per card: its own plate from looks.html's <!--look NAME--> block (a plain
    palette-and-type plate when it has none), wearing the card's palette roles and type pair."""
    with open(LOOKS_TEMPLATE, encoding="utf-8") as f:
        page = f.read()
    plates = dict(PLATE.findall(page))
    styles, index, sections = [], [], []
    for i, x in enumerate(cards, 1):
        n, esc, faces = x["name"], html.escape(x["title"]), x["faces"] + x["faces"][:1]
        styles.append(f"#{n} {{ " + "; ".join([f"{k}: {v}" for k, v in x["roles"].items()]
                                              + [f"--f1: {FACES[faces[0]]}", f"--f2: {FACES[faces[1]]}"]) + " }")
        dots = f'--s: {x["roles"].get("--surface", "Canvas")}; --a: {x["roles"].get("--accent", "CanvasText")}'
        index.append(f'<li><a href="#{n}"><span class="dots" style="{dots}" aria-hidden="true"></span>{esc}</a></li>')
        type_ = " + ".join(f'<span class="face" style="font-family: var(--f{j})">{html.escape(f)}</span>'
                           for j, f in enumerate([faces[0].capitalize()] + x["faces"][1:2], 1))
        swatches = "".join(f'<li><span class="chip" style="background: {v}"></span><code>{k[2:]}</code> '
                           f'<code>{v}</code></li>' for k, v in x["roles"].items())
        plate = (f'<div class="plate" data-plate="own">{plates[n]}</div>' if n in plates else
                 f'<div class="plate" data-plate="fallback"><p class="plain-a">Aa</p>'
                 f'<p class="plain-b">{html.escape(x["line"])}</p></div>')
        sections.append(
            f'<section class="specimen" id="{n}" aria-labelledby="{n}-h">\n<div class="caption">'
            f'<p class="no">{i:02d}</p><h2 id="{n}-h">{esc}</h2><p class="line">{html.escape(x["line"])}</p>'
            f'<dl><dt>Type</dt><dd>{type_}</dd>'
            f'<dt>Motion</dt><dd>{_prose(x["motion"])}</dd><dt>Texture</dt><dd>{_prose(x["texture"])}</dd></dl>'
            f'<button type="button" class="replay" data-replay>Replay motion</button></div>\n'
            f'{plate}\n<ul class="swatches" aria-label="{esc} palette">{swatches}</ul>\n</section>')
    fill = {"STYLES": "\n".join(styles), "INDEX": "\n".join(index), "SPECIMENS": "\n".join(sections),
            "COUNT": str(len(cards))}
    return re.sub(r"__([A-Z]+)__", lambda m: fill.get(m.group(1), m.group(0)), PLATE.sub("", page))


CITY_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "city.html")
CITY_MAX = 4000  # ponytail: the biggest 4000 files stand; a bigger repo's smallest files are left out, and the page says so
READ_CAP = 2 << 20  # lines are counted in a file's first 2 MB; past that, estimated from its own bytes per line
READ_BUDGET = 256 << 20  # past 256 MB read in all, each file's head only (the page says how many were estimated)


def city_files(root):
    """T-1653: ([{path, lines, churn}], {tracked, history, estimated}) for the tracked text files, from fm map's
    sources (git ls-files, and each file's commits in the last 180 days as fm map's hot list counts them; --relative,
    so a project in a repo's subfolder gets its own), each with its line count. Only the 2 × CITY_MAX biggest by bytes
    are opened; each gives its first 8 KB (a NUL there: a binary, left out) and, while READ_BUDGET lasts, up to
    READ_CAP; what isn't read is estimated from the bytes per line that was. history is False when git log failed,
    so no window reads as "no commits"."""
    import collections
    from sampo import map as smap
    log = smap._git(root, "log", "--relative", "--since=180.days", "--name-only", "--pretty=format:", proc=True)
    churn = collections.Counter(f for f in log.stdout.splitlines() if f) if log.returncode == 0 else {}
    sized = []
    for f in (f for f in smap._git(root, "ls-files", "-z").split("\0") if f):
        path = os.path.join(root, f)
        try:
            if not os.path.islink(path) and os.path.isfile(path):
                sized.append((os.path.getsize(path), f))
        except OSError:
            continue
    out, budget, estimated = [], READ_BUDGET, 0
    for size, f in sorted(sized, key=lambda s: (-s[0], s[1]))[:2 * CITY_MAX]:
        path = os.path.join(root, f)
        try:
            with open(path, "rb") as fh:
                head = fh.read(8192)
                if b"\0" in head:
                    continue
                lines, done, last = head.count(b"\n"), len(head), head[-1:]
                left = min(READ_CAP, max(0, budget)) - done
                while left > 0 and (chunk := fh.read(min(1 << 20, left))):
                    lines, done, left, last = lines + chunk.count(b"\n"), done + len(chunk), left - len(chunk), chunk[-1:]
                budget -= done
                whole = not fh.read(1)
        except OSError:
            continue
        if whole:
            lines += last not in (b"", b"\n")  # a last line with no newline
        else:
            lines, estimated = lines + round((size - done) * max(lines, 1) / done), estimated + 1
        out.append({"path": f, "lines": lines, "churn": churn.get(f, 0)})
    return out, {"tracked": len(sized), "history": log.returncode == 0, "estimated": estimated}


def _districts(paths, share=0.35, small=2):
    """{path: district}: one per top folder ('.' for the root's files). As fm map's layout opens a folder that holds
    most of the repo, a district holding over `share` of the files opens into its subfolders, each of fewer than
    `small` files staying with it, until none does."""
    import collections
    of = {f: f.split("/", 1)[0] if "/" in f else "." for f in paths}
    tried = set()
    while True:
        big = next((d for d, n in collections.Counter(of.values()).most_common()
                    if d != "." and d not in tried and n > share * len(paths)), None)
        if big is None:
            return of
        tried.add(big)
        k = big.count("/") + 1
        sub = {f: "/".join(f.split("/")[:k + 1]) for f, d in of.items() if d == big and f.count("/") > k}
        sizes = collections.Counter(sub.values())
        of.update({f: s for f, s in sub.items() if sizes[s] >= small})


def city_layout(files):
    """T-1653: the city's plan from [{path, lines, churn}]: districts as plates packed in shelves with roads between,
    each file a square building on its district's grid, the biggest at the centre. Buildings come biggest first as
    [x, z, width, height, heat 0-1, rise delay 0-1, district index]; height and width by lines (square root), heat by
    commits (log). Districts {name, x, z, w, d, files, lines}, most lines first."""
    import collections
    import math
    import zlib
    files = sorted(files, key=lambda f: (-f["lines"], f["path"]))
    of = _districts([f["path"] for f in files])
    groups = collections.defaultdict(list)
    for f in files:
        groups[of[f["path"]]].append(f)
    names = sorted(groups, key=lambda d: (-sum(f["lines"] for f in groups[d]), d))
    top, hot = max([f["lines"] for f in files] + [1]), max([f["churn"] for f in files] + [0])
    grid = {d: (math.ceil(math.sqrt(len(groups[d]))), math.ceil(len(groups[d]) / math.ceil(math.sqrt(len(groups[d])))))
            for d in names}
    road, rim = 1.4, 0.2
    shelf = max(max(c for c, _ in grid.values()) + 2 * rim,
                1.15 * math.sqrt(sum((c + 2 * rim + road) * (r + 2 * rim + road) for c, r in grid.values())))
    size = lambda d: (grid[d][0] + 2 * rim, grid[d][1] + 2 * rim)
    shelves, x = [[]], 0.0
    for d in sorted(names, key=lambda d: (-grid[d][1], -grid[d][0], d)):  # shelf packing: deepest first
        if shelves[-1] and x + size(d)[0] > shelf:
            shelves.append([])
            x = 0.0
        shelves[-1].append(d)
        x += size(d)[0] + road

    def centre_out(seq):  # the first in the middle, the rest alternating out: downtown at the centre
        return seq[::2][::-1] + seq[1::2]
    plates, z = {}, 0.0
    for row in centre_out(shelves):
        row, deep = centre_out(row), max(size(d)[1] for d in row)
        x = -(sum(size(d)[0] for d in row) + road * (len(row) - 1)) / 2
        for d in row:
            plates[d] = [x + size(d)[0] / 2, z + deep / 2, *size(d)]
            x += size(d)[0] + road
        z += deep + road
    cx = (min(p[0] - p[2] / 2 for p in plates.values()) + max(p[0] + p[2] / 2 for p in plates.values())) / 2
    cz = (min(p[1] - p[3] / 2 for p in plates.values()) + max(p[1] + p[3] / 2 for p in plates.values())) / 2
    for p in plates.values():
        p[0], p[1] = p[0] - cx, p[1] - cz
    span = max([max(abs(p[0]) + p[2] / 2, abs(p[1]) + p[3] / 2) for p in plates.values()] + [1]) * 2
    hmax = min(max(span * 0.24, 3.0), 22.0)
    reach = span / math.sqrt(2) or 1
    rank = {f["path"]: n for n, f in enumerate(files)}
    buildings = []
    for i, d in enumerate(names):
        (cols, rows), (px, pz, _, _) = grid[d], plates[d]
        cells = sorted(((r, c) for r in range(rows) for c in range(cols)),
                       key=lambda rc: ((rc[0] - (rows - 1) / 2) ** 2 + (rc[1] - (cols - 1) / 2) ** 2, rc))
        for f, (r, c) in zip(groups[d], cells):
            size = math.sqrt(f["lines"] / top)
            bx, bz = px - cols / 2 + c + 0.5, pz - rows / 2 + r + 0.5
            jitter = zlib.crc32(f["path"].encode()) % 1000 / 1000
            buildings.append([rank[f["path"]], round(bx, 2), round(bz, 2), round(0.5 + 0.3 * size, 2), round(max(hmax * size, 0.12), 2),
                              round(math.log1p(f["churn"]) / math.log1p(hot), 3) if hot else 0.0,
                              round(min(1.0, 0.8 * math.hypot(bx, bz) / reach + 0.2 * jitter), 3), i])
    return {"b": [b[1:] for b in sorted(buildings)], "hmax": round(hmax, 2), "span": round(span, 2),
            "d": [{"name": d, "x": round(plates[d][0], 2), "z": round(plates[d][1], 2), "w": round(plates[d][2], 2),
                   "d": round(plates[d][3], 2), "files": len(groups[d]), "lines": sum(f["lines"] for f in groups[d])}
                  for d in names]}


def _poster(plan):
    """The first paint: the same plan drawn isometric in SVG, plates then buildings back to front, each building its
    two lit sides (one path, a hard-stop gradient per heat step: the front edge sits mid-box on a square footprint)
    and its roof; the six biggest districts named."""
    import math
    cos, sin = math.cos(math.pi / 6), math.sin(math.pi / 6)
    iso = lambda x, z, y=0.0: ((x - z) * cos, (x + z) * sin - y)
    pts = [iso(p["x"] + sx * p["w"] / 2, p["z"] + sz * p["d"] / 2) for p in plan["d"] for sx in (-1, 1) for sz in (-1, 1)]
    pts += [iso(b[0], b[1], b[3]) for b in plan["b"]]
    x0, y0 = min(p[0] for p in pts) - 2, min(p[1] for p in pts) - 2
    w, h = max(p[0] for p in pts) + 2 - x0, max(p[1] for p in pts) + 2 - y0
    path = lambda corners: "M" + "L".join(f"{x - x0:.1f} {y - y0:.1f}" for x, y in corners) + "Z"
    out = []
    for p in plan["d"]:
        hx, hz = p["w"] / 2, p["d"] / 2
        out.append(f'<path class="plate" d="{path([iso(p["x"] + a, p["z"] + b) for a, b in ((-hx, -hz), (hx, -hz), (hx, hz), (-hx, hz))])}"/>')
    for b in sorted(plan["b"], key=lambda b: (b[0] + b[1], b[0])):
        x, z, s, y = b[0], b[1], b[2] / 2, b[3]
        step = min(4, int(b[4] * 5))
        sides = [iso(x - s, z + s), iso(x + s, z + s), iso(x + s, z - s), iso(x + s, z - s, y), iso(x + s, z + s, y),
                 iso(x - s, z + s, y)]
        roof = [iso(x - s, z - s, y), iso(x + s, z - s, y), iso(x + s, z + s, y), iso(x - s, z + s, y)]
        out.append(f'<path class="b" fill="url(#h{step})" d="{path(sides)}"/><path class="b roof r{step}" d="{path(roof)}"/>')
    for p in plan["d"][:6]:
        tx, ty = iso(p["x"] - p["w"] / 2, p["z"] + p["d"] / 2)
        out.append(f'<text class="tag" x="{tx - x0:.1f}" y="{ty - y0 + 1.6:.1f}">{html.escape(p["name"])}</text>')
    stops = "".join(f'<linearGradient id="h{i}"><stop offset=".5" style="stop-color:var(--side-l{i})"/>'
                    f'<stop offset=".5" style="stop-color:var(--side-r{i})"/></linearGradient>' for i in range(5))
    return (f'<svg class="poster" viewBox="0 0 {w:.1f} {h:.1f}" preserveAspectRatio="xMidYMid meet" aria-hidden="true">'
            f"<defs>{stops}</defs>" + "".join(out) + "</svg>")


def _area(d):
    where = "the root" if d["name"] == "." else d["name"]
    return f"{where} ({d['files']:,} file{'s' * (d['files'] != 1)}, {d['lines']:,} lines)"


def city_html(files, name, when=None, tracked=0, history=True, estimated=0):
    """T-1653: the city page, one file: the SVG poster, a canvas named by what the city shows (the biggest districts,
    the most changed files), its text twin, and the plan as JSON for the WebGL2 scene. Raw WebGL2 inlined, as the
    template and the site's hero are: a box per instance needs no three.js, and nothing loads from another host.
    `tracked`: how many files the repo has, when more than `files` (the twin says how many are left out); `history`
    False: git log failed, so the label says so rather than "every window is dark"; `estimated`: files whose lines
    were estimated from their size. Leak, naming the path, when one is still absolute after redaction."""
    left = max(0, max(tracked, len(files)) - min(len(files), CITY_MAX))
    kept = []
    for f in sorted(files, key=lambda f: -int(f["lines"]))[:CITY_MAX]:
        try:
            kept.append({"path": clean(str(f["path"])), "lines": int(f["lines"]), "churn": int(f["churn"])})
        except Leak:
            raise Leak(f"the file path {f['path']}") from None
    files, name = kept, clean(str(name), "name")
    plan = city_layout(files)
    lines = sum(f["lines"] for f in files)
    hottest = [f for f in sorted(files, key=lambda f: (-f["churn"], f["path"])) if f["churn"]][:3]
    label = (f"{name} as a city: {len(files):,} files and {lines:,} lines in {len(plan['d'])} districts, one per "
             f"folder, each file a building as tall as its lines. Biggest: "
             + "; ".join(_area(d) for d in plan["d"][:3]) + ". "  # not a list: the three biggest districts, a ranking
             + ("Commit history unavailable (git log failed): the windows show nothing." if not history else
                "Lit windows mark commits in the last 180 days, most in "
                + ", ".join(f"{f['path']} ({f['churn']})" for f in hottest) + "." if hottest
                else "No file changed in the last 180 days: every window is dark."))
    notes = ([f"{left:,} other tracked file{'s' * (left != 1)} (binaries, or past the {CITY_MAX:,} biggest) "
              f"{'are' if left != 1 else 'is'} not drawn."] if left else []) + (
             [f"{estimated:,} file{'s' * (estimated != 1)}' lines are estimated from their size (each file is read "
              f"up to {READ_CAP >> 20} MB, {READ_BUDGET >> 20} MB in all)."] if estimated else [])
    twin = (f"<p>{html.escape(' '.join([label] + notes))}</p>"
            + "<ol class=\"areas\">" + "".join(f"<li>{html.escape(_area(d))}</li>" for d in plan["d"][:12]) + "</ol>")
    fill = {"TITLE": html.escape(name), "EYEBROW": html.escape(f"fm page city · {when or time.strftime('%Y-%m-%d')}"),
            "LEDE": html.escape(f"{len(files):,} files · {lines:,} lines · {len(plan['d'])} districts"),
            "LABEL": html.escape(label), "TWIN": twin, "POSTER": _poster(plan), "DATA": embed(plan)}
    with open(CITY_TEMPLATE, encoding="utf-8") as f:
        return re.sub(r"__([A-Z]+)__", lambda m: fill.get(m.group(1), m.group(0)), f.read())


def city_page(p, args):
    from sampo import cli
    if args.catalogue or args.picks or args.url:
        raise cli.UsageError("fm page city takes only --out: the city shows the repository, not a catalogue")
    if not c.git_root(p.root):
        raise cli.UsageError("fm page city needs a git repository: its buildings are the tracked files")
    files, info = city_files(p.root)
    if not files:
        raise cli.UsageError("fm page city: no tracked text file to build")
    try:
        page = city_html(files, os.path.basename(p.root.rstrip("/")) or p.slug, **info)
    except Leak as e:
        raise cli.UsageError(f"refused: {e} still holds an absolute or home path after redaction; fm page city "
                               "writes no page that names one")
    path = _out(args, os.path.join(p.dir, "city.html"), "/* fm page city (T-1653):")
    c.write_atomic(path, page)
    return cli.out(args, {"path": path, "files": min(len(files), CITY_MAX)},
                     f"Wrote {path}: {min(len(files), CITY_MAX):,} files as buildings; one file that loads nothing "
                     "from another host (fm shot it to see it)")


GRAPH_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "graph.html")
GRAPH_MAX = 1500  # ponytail: the newest 1500 tasks stand; older ones are left out, and the page says so
GRAPH_EDGES = 6000  # and the first 6000 dependencies by waiter id are drawn (the text twin lists every one)
GRAPH_NAMED = 12  # tasks named on the poster: the active work first, then the blocked
GRAPH_STATUSES = ["active", "verifying", "blocked", "planned", "captured", "deferred", "done", "dropped"]
TIER_HEIGHT = dict(zip(c.TIERS, (0.5, 0.9) + (1.4,) * (len(c.TIERS) - 2)))  # T-4220: a big task draws as tall as an L


def graph_tasks(p):
    """T-1654: [{id, title, status, tier, deps}] from fm state, through graph_keep. deps: the brief's own
    depends_on, not what fm relate inferred (a guess draws no edge)."""
    out = []
    for b in c.load_briefs(p):
        deps = b.meta.get("depends_on") or []
        out.append({"id": b.id, "title": b.title, "status": b.status, "tier": b.tier,
                    "deps": [str(d) for d in (deps if isinstance(deps, list) else [deps])]})
    return graph_keep(out)


def graph_keep(rows):
    """Every open task and every task a dependency links, each dep kept only when its task is loaded (an archived
    one draws no edge, so it keeps no closed task): a closed task with no link would stand alone."""
    ids = {t["id"] for t in rows}
    rows = [dict(t, deps=[d for d in t["deps"] if d in ids]) for t in rows]
    linked = {d for t in rows for d in t["deps"]}
    return [t for t in rows if t["status"] in c.OPEN or t["deps"] or t["id"] in linked]


def _components(deps, kids):
    """Strongly connected components (Kosaraju, iterative: a 1500-long chain needs no recursion), as a component
    index per node."""
    n, order, seen = len(deps), [], [False] * len(deps)
    for s in range(n):
        if seen[s]:
            continue
        seen[s], stack = True, [(s, iter(deps[s]))]
        while stack:
            v, it = stack[-1]
            w = next((w for w in it if not seen[w]), None)
            if w is None:
                stack.pop()
                order.append(v)
            else:
                seen[w] = True
                stack.append((w, iter(deps[w])))
    comp, k = [-1] * n, 0
    for s in reversed(order):
        if comp[s] >= 0:
            continue
        comp[s], todo = k, [s]
        while todo:
            for w in kids[todo.pop()]:
                if comp[w] < 0:
                    comp[w] = k
                    todo.append(w)
        k += 1
    return comp


def graph_layout(tasks):
    """T-1654: the graph's plan from [{id, status, tier, deps}], deterministic: each task in the layer after the
    longest chain it waits on (Kahn's order, no recursion; each cycle, found as a strongly connected set, is broken
    at its lowest id once nothing outside it holds it back, its members counted),
    ordered in its layer by where what it waits on stands (one barycentre sweep), a big layer wrapped into a square
    block. Nodes [x, z, height (by tier), status index into GRAPH_STATUSES, layer, rise delay 0-1]; edges [waited
    on, waiter] as node indices; layers {x, w} for the ground under each; chain: the longest chain's node indices."""
    import collections
    import math
    n = len(tasks)
    index = {t["id"]: i for i, t in enumerate(tasks)}
    deps = [[index[d] for d in dict.fromkeys(t["deps"]) if d in index and index[d] != i] for i, t in enumerate(tasks)]
    kids = [[] for _ in tasks]
    for i, ds in enumerate(deps):
        for d in ds:
            kids[d].append(i)
    left, layer, pred, done = [len(ds) for ds in deps], [0] * n, [None] * n, [False] * n
    ready = collections.deque(i for i in range(n) if not left[i])

    def drain():
        while ready:
            i = ready.popleft()
            done[i] = True
            for k in kids[i]:
                if done[k]:
                    continue
                if layer[i] + 1 > layer[k]:
                    layer[k], pred[k] = layer[i] + 1, i
                left[k] -= 1
                if not left[k]:
                    ready.append(k)
    drain()
    cyclic = 0
    if not all(done):
        comp = _components(deps, kids)
        size = collections.Counter(comp)
        cyclic = sum(size[comp[i]] > 1 for i in range(n))
        while not all(done):  # ponytail: one pass per cycle (O(cycles × tasks)); cycles in a task graph are rare
            # a cycle nothing else unplaced holds back, broken at its lowest index: that task stands after what of it
            # is already placed, and what waits on the cycle stands after it
            blocked = {comp[i] for i in range(n) if not done[i] for d in deps[i] if not done[d] and comp[d] != comp[i]}
            i = min(i for i in range(n) if not done[i] and comp[i] not in blocked)
            left[i] = 0
            ready.append(i)
            drain()
    layers = collections.defaultdict(list)
    for i in range(n):
        layers[layer[i]].append(i)
    pos, x, gap, places, grounds = {}, 0.0, 1.6, [None] * n, []
    for L in range(max(layers) + 1 if layers else 0):
        row = layers[L]
        if not row:
            continue
        bary = {i: sum(pos[d] for d in deps[i] if d in pos) / max(1, sum(d in pos for d in deps[i])) for i in row}
        row.sort(key=lambda i: (not any(d in pos for d in deps[i]), bary[i], i))
        per = max(12, math.ceil(math.sqrt(len(row))))
        cols = math.ceil(len(row) / per)
        rows = math.ceil(len(row) / cols)
        for j, i in enumerate(row):
            col, r = divmod(j, rows)
            pos[i] = (r - (rows - 1) / 2) / max(1, rows)
            places[i] = (x + col, (r - (rows - 1) / 2) * 1.2)
        grounds.append([x - 0.6, (cols - 1) + 1.2, rows * 1.2 + 0.2])
        x += (cols - 1) + gap + 1
    mid = (grounds[0][0] + grounds[-1][0] + grounds[-1][1]) / 2 if grounds else 0
    top = max(layer) if n else 0
    nodes = [[round(places[i][0] - mid, 2), round(places[i][1], 2), TIER_HEIGHT.get(str(t["tier"]), 0.5),
              GRAPH_STATUSES.index(t["status"]) if t["status"] in GRAPH_STATUSES else GRAPH_STATUSES.index("captured"),
              layer[i], round(layer[i] / top, 3) if top else 0.0] for i, t in enumerate(tasks)]
    end = max(range(n), key=lambda i: (layer[i], -i)) if n else None
    chain = []
    while end is not None:
        chain.append(end)
        end = pred[end]
    return {"n": nodes, "e": [[d, i] for i, ds in enumerate(deps) for d in ds],
            "layers": [{"x": round(g[0] - mid + g[1] / 2, 2), "w": round(g[1], 2), "d": round(g[2], 2)} for g in grounds],
            "chain": chain[::-1], "cyclic": cyclic, "hmax": max(TIER_HEIGHT.values())}


def _graph_newest(tasks):
    """The newest GRAPH_MAX tasks by id, oldest first, and how many older ones are left out."""
    tasks = sorted(tasks, key=lambda t: (c.id_num(t["id"]), str(t["id"])))
    left = max(0, len(tasks) - GRAPH_MAX)
    return tasks[left:], left


def graph_scene(plan):
    """T-1655: graph_layout's plan as a scene.json dict (fmscene): a box per task as tall as its tier, coloured by its
    status from graph.html's --graph-* tokens (so the page and the terminal can't drift), a ground slab per layer, the
    camera above and to the south looking at the middle, framed for fmscene.cells' 8:5 picture."""
    import math
    from sampo import scene as sscene
    with open(GRAPH_TEMPLATE, encoding="utf-8") as f:
        tokens = dict(re.findall(r"--graph-([a-z]+): (#[0-9a-f]{6});", f.read()))
    meshes = [{"name": f"g{i}", "material": "floor", "geometry": {"type": "box", "size": [g["w"], 0.05, g["d"]]},
               "position": [g["x"], -0.025, 0]} for i, g in enumerate(plan["layers"])]
    meshes += [{"name": f"t{i}", "material": GRAPH_STATUSES[nd[3]], "geometry": {"type": "box", "size": [0.8, nd[2], 0.8]},
                "position": [nd[0], nd[2] / 2, nd[1]]} for i, nd in enumerate(plan["n"])]
    wide = max((abs(nd[0]) for nd in plan["n"]), default=0) + 1
    deep = max((abs(nd[1]) for nd in plan["n"]), default=0) + 1
    tan = math.tan(math.radians(35 / 2))
    dist = 1.1 * max(wide / (tan * 1.6), (deep * 0.6 + plan["hmax"]) / tan) + deep
    far = max(100, round(dist * 3))
    return sscene.validate({
        "background": tokens["bg"], "meshes": meshes,
        "materials": {s: {"color": tokens[s]} for s in GRAPH_STATUSES + ["floor"]},
        "camera": {"fov": 35, "target": [0, 0.4, 0], "near": max(0.1, far / 5000), "far": far,
                   "position": [0, round(dist * math.sin(math.radians(35)), 2), round(dist * math.cos(math.radians(35)), 2)]}})


def graph_cells(tasks, cols=64):
    """T-1655: the task graph in the terminal: graph_scene drawn by fmscene.cells as 24-bit half blocks, then a key
    naming each status's count in its colour (status isn't colour alone), and how many older tasks aren't drawn.
    ValueError for cols outside 2-400, before any drawing."""
    import collections
    from sampo import scene as sscene
    if not 2 <= cols <= 400:
        raise ValueError(f"--cols {cols}: 2 to 400")
    tasks, left = _graph_newest(tasks)
    plan = graph_layout(tasks)
    scene = graph_scene(plan)
    counts = collections.Counter(GRAPH_STATUSES[nd[3]] for nd in plan["n"])
    key = []
    for s in GRAPH_STATUSES:
        if counts[s]:
            rgb = ";".join(str(int(scene["materials"][s]["color"][i:i + 2], 16)) for i in (1, 3, 5))
            key.append(f"\x1b[38;2;{rgb}m■\x1b[0m {counts[s]} {s}")
    if left:
        key.append(f"({left:,} older not drawn)")
    return sscene.cells(scene, cols) + ["  ".join(key)]


def _graph_poster(plan, named):
    """The first paint: the same plan drawn isometric in SVG, each layer's ground, the edges as arcs, then each task
    back to front as its two sides and its roof, coloured by status, a blocked roof hatched and a done one ringed
    (status isn't colour alone); then the named tasks' ids ({node index: id}) over their roofs."""
    import math
    cos, sin = math.cos(math.pi / 6), math.sin(math.pi / 6)
    iso = lambda x, z, y=0.0: ((x - z) * cos, (x + z) * sin - y)
    pts = [iso(g["x"] + sx * g["w"] / 2, sz * g["d"] / 2) for g in plan["layers"] for sx in (-1, 1) for sz in (-1, 1)]
    pts += [iso(nd[0], nd[1], nd[2] + 1) for nd in plan["n"]] + [(0.0, 0.0)]
    x0, y0 = min(p[0] for p in pts) - 2, min(p[1] for p in pts) - 2
    w, h = max(p[0] for p in pts) + 2 - x0, max(p[1] for p in pts) + 2 - y0
    pt = lambda p: f"{p[0] - x0:.1f} {p[1] - y0:.1f}"
    path = lambda corners: "M" + "L".join(map(pt, corners)) + "Z"
    out = [f'<path class="ground" d="{path([iso(g["x"] + a * g["w"] / 2, b * g["d"] / 2) for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])}"/>'
           for g in plan["layers"]]
    for a, b in plan["e"]:
        (ax, az, ah), (bx, bz, bh) = plan["n"][a][:3], plan["n"][b][:3]
        lift = max(ah, bh) + 0.4 + 0.15 * math.hypot(bx - ax, bz - az)
        out.append(f'<path class="e" d="M{pt(iso(ax, az, ah))}Q{pt(iso((ax + bx) / 2, (az + bz) / 2, lift))} '
                   f'{pt(iso(bx, bz, bh))}"/>')
    for nd in sorted(plan["n"], key=lambda nd: (nd[0] + nd[1], nd[0])):
        x, z, y, s = nd[0], nd[1], nd[2], 0.3
        st = GRAPH_STATUSES[nd[3]]
        sides = [iso(x - s, z + s), iso(x + s, z + s), iso(x + s, z - s), iso(x + s, z - s, y), iso(x + s, z + s, y),
                 iso(x - s, z + s, y)]
        square = lambda s: [iso(x - s, z - s, y), iso(x + s, z - s, y), iso(x + s, z + s, y), iso(x - s, z + s, y)]
        out.append(f'<path class="n side st-{st}" d="{path(sides)}"/><path class="n roof st-{st}" d="{path(square(s))}"/>')
        if st == "blocked":
            out.append(f'<path class="mark hatch" d="{path(square(s))}"/>')
        elif st == "done":
            out.append(f'<path class="mark ring" d="{path(square(s * 0.55))}"/>')
    placed = []  # T-1735: label boxes so far (.5px mono: ~0.3 a character wide, 0.6 tall), in `named`'s priority order
    for i, tid in named.items():
        tx, ty = iso(plan["n"][i][0], plan["n"][i][1], plan["n"][i][2])
        x, y, half = tx - x0, ty - y0 - 0.45, 0.15 * len(tid)
        if any(abs(x - px) < half + ph and abs(y - py) < 0.6 for px, py, ph in placed):
            continue  # ponytail: a colliding label is dropped, not moved; the text twin names every task
        placed.append((x, y, half))
        out.append(f'<text class="tag" x="{x:.1f}" y="{y:.1f}">{html.escape(tid)}</text>')
    hatch = ('<defs><pattern id="hatch" width=".2" height=".2" patternUnits="userSpaceOnUse" '
             'patternTransform="rotate(45)"><rect width=".08" height=".2"/></pattern></defs>')
    return (f'<svg class="poster" viewBox="0 0 {w:.1f} {h:.1f}" preserveAspectRatio="xMidYMid meet" aria-hidden="true">'
            + hatch + "".join(out) + "</svg>")


def graph_html(tasks, name, when=None):
    """T-1654: the task graph page, one file, on the city's contract: the SVG poster, a canvas named by what the
    graph shows (counts by status, the active work, the longest chain, any cycle), its text twin holding every task
    and what it waits on, and the plan as JSON for the WebGL2 scene. Leak, naming the task, when a string is still
    absolute after redaction."""
    import collections
    tasks, left = _graph_newest(tasks)
    kept = []
    for t in tasks:
        try:
            kept.append({"id": clean(str(t["id"])), "title": clean(str(t.get("title") or "")),
                         "status": str(t.get("status") or ""),
                         "tier": t.get("tier") if t.get("tier") in TIER_HEIGHT else "",  # any tier name (T-4220)
                         "deps": [clean(str(d)) for d in t.get("deps") or []]})
        except Leak:
            raise Leak(f"task {t['id']}") from None
    tasks, name = kept, clean(str(name), "name")
    plan = graph_layout(tasks)
    status = [GRAPH_STATUSES[nd[3]] for nd in plan["n"]]
    counts = collections.Counter(status)
    tag = lambda i: f"{tasks[i]['id']} {tasks[i]['title']}"
    active = [i for i, s in enumerate(status) if s in ("active", "verifying")]
    chain = plan["chain"]
    shown = [tasks[i]["id"] for i in chain] if len(chain) <= 6 else (
        [tasks[i]["id"] for i in chain[:3]] + ["…"] + [tasks[i]["id"] for i in chain[-2:]])
    layers = len(plan["layers"])
    label = (f"{name}'s task graph: {len(tasks):,} task{'s' * (len(tasks) != 1)} and {len(plan['e']):,} "
             f"dependenc{'ies' if len(plan['e']) != 1 else 'y'} in {layers} layer{'s' * (layers != 1)}, each task a "
             "block as tall as its tier and coloured by status, standing after what it waits on: "
             + ", ".join(f"{counts[s]:,} {s}" for s in GRAPH_STATUSES if counts[s]) + ". "
             + (("Active now: " + "; ".join(tag(i) for i in active[:3]) + (f" and {len(active) - 3} more" if
                 len(active) > 3 else "") + "; ") if active else "No task is active; ")
             + (f"longest chain: {len(chain)} tasks, {' → '.join(shown)}." if len(chain) > 1 else
                "no task waits on another."))
    cut = max(0, len(plan["e"]) - GRAPH_EDGES)
    notes = ([f"{plan['cyclic']:,} tasks wait on each other in a cycle; each cycle is broken at its lowest id."]
             if plan["cyclic"] else []) + (
             [f"{left:,} older task{'s are' if left != 1 else ' is'} not drawn (the newest {GRAPH_MAX:,} are)."]
             if left else []) + (
             [f"{cut:,} more dependenc{'ies are' if cut != 1 else 'y is'} not drawn (the first {GRAPH_EDGES:,} by "
              "waiter are; the list below holds them all)."] if cut else [])
    ids = [t["id"] for t in tasks]
    waits = [[] for _ in tasks]
    for a, b in plan["e"]:
        waits[b].append(ids[a])
    order = sorted(range(len(tasks)), key=lambda i: (plan["n"][i][4], i))
    twin = (f"<p>{html.escape(' '.join([label] + notes))}</p><ol class=\"tasks\">"
            + "".join(f"<li>{html.escape(tag(i))} ({status[i]}{html.escape(', ' + tasks[i]['tier']) if tasks[i]['tier'] else ''})"
                      + (html.escape(f" — waits on {', '.join(waits[i])}") if waits[i] else "") + "</li>"
                      for i in order) + "</ol>")
    drawn = dict(plan, e=plan["e"][:GRAPH_EDGES])
    named = {i: ids[i] for i in (active + [i for i, s in enumerate(status) if s == "blocked"])[:GRAPH_NAMED]}
    data = dict(drawn, s=GRAPH_STATUSES, t=[{"id": t["id"], "title": t["title"]} for t in tasks], a=active[:6])
    fill = {"TITLE": html.escape(name), "EYEBROW": html.escape(f"fm page graph · {when or time.strftime('%Y-%m-%d')}"),
            "LEDE": html.escape(f"{len(tasks):,} tasks · {len(plan['e']):,} dependencies · {layers} layers"),
            "LABEL": html.escape(label), "TWIN": twin, "POSTER": _graph_poster(drawn, named),
            "DATA": embed(data)}
    with open(GRAPH_TEMPLATE, encoding="utf-8") as f:
        return re.sub(r"__([A-Z]+)__", lambda m: fill.get(m.group(1), m.group(0)), f.read())


def graph_page(p, args):
    from sampo import cli
    if args.catalogue or args.picks or args.url:
        raise cli.UsageError("fm page graph takes only --out: the graph shows fm's tasks, not a catalogue")
    tasks = graph_tasks(p)
    if not tasks:
        raise cli.UsageError("fm page graph: no open or linked task to draw")
    if args.cells:
        if args.out:
            raise cli.UsageError("fm page graph --cells prints to the terminal; it takes no --out")
        try:
            lines = graph_cells(tasks, args.cols)
        except ValueError as e:
            raise cli.UsageError(f"fm page graph {e}") from None
        return cli.out(args, {"cols": args.cols, "tasks": min(len(tasks), GRAPH_MAX), "lines": lines},
                         "\n".join(lines)) or 0
    try:
        page = graph_html(tasks, os.path.basename(p.root.rstrip("/")) or p.slug)
    except Leak as e:
        raise cli.UsageError(f"refused: {e} still holds an absolute or home path after redaction; fm page graph "
                               "writes no page that names one")
    path = _out(args, os.path.join(p.dir, "graph.html"), "/* fm page graph (T-1654):")
    c.write_atomic(path, page)
    drawn = min(len(tasks), GRAPH_MAX)
    return cli.out(args, {"path": path, "tasks": drawn},
                     f"Wrote {path}: {drawn:,} tasks in layers by what they wait on; one file that loads nothing from "
                     "another host (run it again to refresh it from fm state)")


def _out(args, default, mark):
    """Where the page goes. Review of T-1649: an --out that already exists is replaced only when it holds mark, the
    template's own opening comment, so the user's own page is never overwritten (the default path is Sampo's)."""
    from sampo import cli
    path = os.path.abspath(args.out) if args.out else default
    if not path.endswith(".html") or os.path.islink(path):  # review: --out never overwrites another kind of file
        raise cli.UsageError(f"--out must be a .html file that isn't a link: {path}")
    if args.out and os.path.lexists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                ours = mark in f.read(4096)
        except OSError:
            ours = False
        if not ours:
            raise cli.UsageError(f"--out {path} already exists and isn't one fm page wrote; pick a new file or "
                                   "move that one")
    return path


CARDS_MARK = "/* fm page looks --cards (T-1650):"
CARDS_CSS = CARDS_MARK + """ the look cards as trading cards, 63 x 88 mm, nine to an A4 page */
@page { size: A4; margin: 10mm; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: Canvas; color: CanvasText; font: 14px/1.4 system-ui, sans-serif; }
h1 { font-size: 18px; margin: 0 0 12px; }
.sheet { display: grid; grid-template-columns: repeat(auto-fill, 63mm); gap: 4mm; justify-content: center; }
.card { width: 63mm; height: 88mm; padding: 4mm; border-radius: 3mm; overflow: hidden; display: flex;
  flex-direction: column; gap: 2mm; background: var(--bg); color: var(--fg); border: 0.6mm solid var(--ac);
  break-inside: avoid; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
.card h2 { margin: 0; font: 600 13pt/1.1 var(--f1); }
.card .aa { margin: 0; font: 34pt/1 var(--f1); color: var(--ac); }
.card .line { margin: 0; font: 8pt/1.3 var(--f2); flex: 1; overflow: hidden; }
.card .sw { display: flex; height: 6mm; border-radius: 1mm; overflow: hidden; border: 0.3mm solid var(--fg); }
.card .sw i { flex: 1; }
.card dl { margin: 0; font: 7pt/1.3 var(--f2); display: grid; grid-template-columns: auto 1fr; gap: 0 2mm; }
.card dt { font-weight: 600; }
.card dd { margin: 0; }
.card .no { margin: 0; font: 7pt var(--f2); text-align: right; opacity: 0.8; }
@media print { body { padding: 0; } h1 { display: none; } .sheet { justify-content: start; } }
"""


def cards_html(cards):
    """T-1650: the look cards as trading cards on a print sheet: each card in its own palette roles and type pair (its
    title, an Aa, its line, a palette strip, type and motion), 63 x 88 mm, nine to an A4 page. One file, no other host."""
    styles, items = [], []
    for i, x in enumerate(cards, 1):
        n, faces, roles = x["name"], x["faces"] + x["faces"][:1], x["roles"]
        fg = roles.get("--text", "#111111")
        styles.append(f"#c-{n} {{ --bg: {roles.get('--surface', '#FFFFFF')}; --fg: {fg}; "
                      f"--ac: {roles.get('--accent', fg)}; --f1: {FACES[faces[0]]}; --f2: {FACES[faces[1]]} }}")
        strip = "".join(f'<i style="background: {v}" title="{html.escape(k[2:])} {v}"></i>' for k, v in roles.items())
        items.append(f'<article class="card" id="c-{n}" aria-labelledby="c-{n}-h"><h2 id="c-{n}-h">'
                     f'{html.escape(x["title"])}</h2><p class="aa" aria-hidden="true">Aa</p>'
                     f'<p class="line">{html.escape(x["line"])}</p><div class="sw" aria-hidden="true">{strip}</div>'
                     f'<dl><dt>Type</dt><dd>{html.escape(" + ".join(x["faces"][:2]))}</dd>'
                     f'<dt>Motion</dt><dd>{_prose(x["motion"])}</dd></dl><p class="no">{i:02d} / {len(cards):02d}</p>'
                     f'</article>')
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>Look cards</title>\n"
            f"<style>\n{CARDS_CSS}{chr(10).join(styles)}\n</style></head>\n<body><h1>Look cards: {len(cards)}</h1>\n"
            f"<main class=\"sheet\">\n{chr(10).join(items)}\n</main></body></html>\n")


def looks_page(p, args):
    from sampo import cli
    if args.catalogue or args.picks or args.url:
        raise cli.UsageError("fm page looks takes only --out: the gallery shows the look cards, not a catalogue")
    cards = look_cards()
    if getattr(args, "cards", False):  # T-1650
        path = _out(args, os.path.join(p.dir, "look-cards.html"), CARDS_MARK)
        c.write_atomic(path, cards_html(cards))
        return cli.out(args, {"path": path, "looks": len(cards)},
                         f"Wrote {path}: {len(cards)} looks as trading cards, nine to an A4 page; print it as it is")
    path = _out(args, os.path.join(p.dir, "looks.html"), "/* fm page looks (T-1649):")
    c.write_atomic(path, looks_html(cards))
    return cli.out(args, {"path": path, "looks": len(cards)},
                     f"Wrote {path}: {len(cards)} looks, one living specimen each; it loads nothing from another host")


def cmd_page(args):
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "cells", False) and args.what != "graph":
        raise cli.UsageError("--cells draws only fm page graph in the terminal")
    if getattr(args, "what", None) == "looks":
        return looks_page(p, args)
    if getattr(args, "what", None) == "city":
        return city_page(p, args)
    if getattr(args, "what", None) == "graph":
        return graph_page(p, args)
    meta = c.read_meta(p)
    if args.url:
        if not re.fullmatch(r"https://claude\.ai/(?:code/)?artifact/[\w-]{6,80}", args.url):
            raise cli.UsageError("--url must be the https://claude.ai/... link the Artifact tool returned")
        meta = c.update_meta(p, page_url=args.url)
    catalogue = load_catalogue(p, args.catalogue) if args.catalogue else []
    if getattr(args, "picks", None):
        return save_picks(p, args, catalogue)
    data = {"eyebrow": f"Sampo · {time.strftime('%Y-%m-%d')} · {p.slug}" + (f" · {os.path.basename(args.catalogue).removesuffix('.json')}" if args.catalogue else ""),
            "title": f"{os.path.basename(p.root.rstrip('/')).replace('-', ' ').title()} Board",  # a name, stable
            "lede": "What Sampo is working on, what waits, and what just closed.",
            "now": now_data(p), "catalogue": catalogue}
    try:
        data = clean(data)
    except Leak as e:
        raise cli.UsageError(f"refused: {e} still holds an absolute path after redaction; fix that text "
                               "(repo-relative paths are fine) and run fm page again")
    with open(TEMPLATE, encoding="utf-8") as f:
        page = f.read()
    page = page.replace("__TITLE__", html.escape(data["title"])).replace("__DATA__", embed(data))
    path = _out(args, os.path.join(p.dir, "page.html"), "/* fm page (T-0786):")
    c.write_atomic(path, page)
    url = meta.get("page_url")
    publish = ("Publish: refused here, this repo is marked sensitive (the Artifact tool is denied); fm sensitive off "
               "if the user wants it shared" if meta.get("sensitive") else  # T-0821: the outward gate's rule
               f"Publish (main thread): Artifact file_path {path} capabilities {{\"db\": {{}}, \"user\": {{}}}}"
               + (f" url {url}" if url else "") + "\n"
               "Picks: read with ArtifactData list collection picks (one doc per cluster id: {choice: add|veto|\"\", "
               "at}); after a first publish, record its link with fm page --url URL")
    cli.out(args, {"path": path, "url": url, "queue": len(data["now"]["queue"]), "clusters": len(catalogue),
                     "sensitive": bool(meta.get("sensitive"))},
              f"Wrote {path} ({len(data['now']['queue'])} queued, {len(catalogue)} clusters)\n" + publish)
