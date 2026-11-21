"""T-3734: sampo panels render SPEC --html: every panel of a spec on one self-contained page, set moving. Packets run
along their flows, cluster clouds drift, the globe turns and dashed lines march, at 30 frames a second at most; under
prefers-reduced-motion (or in a hidden tab) nothing moves and the page is the static drawing. With --feed URL the page
polls a JSON feed, {"panels": [{"stat": "1.2k/s"}, ...]} in spec order, and writes each number in as text.

The page is the kit's own SVG (dark and light, the colour scheme picks) plus one inline script and one style, both
pinned by hash in a Content-Security-Policy: it loads nothing, and reaches only the feed's origin. The motion is read
from marks the kit leaves in the SVG: data-p and data-t on a packet (its curve and place), data-drift on a cluster,
data-globe and data-mer on the globe, stroke-dasharray on a dashed line.

sampo panels render SPEC --term draws in the terminal instead: sampo.panelterm. dispatch() routes both flags."""
import base64
import hashlib
import html
import json
import os
import re

from sampo import core as c

PAGE = "panels.html"
FPS = 30
_ABS = re.compile(r"(https?)://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:\d{1,5})?(/[A-Za-z0-9._~!$&()*+,;=:@%/?-]*)?")
_REL = re.compile(r"(?!//)[A-Za-z0-9._~!$&()*+,;=@%/?-]+")  # no scheme: no colon at all

SCRIPT = r"""(() => {
  "use strict";
  const FEED = %(feed)s;
  const FPS = %(fps)d, STEP = 1000 / FPS, NS = "http://www.w3.org/2000/svg";
  const root = document.documentElement, still = matchMedia("(prefers-reduced-motion: reduce)");
  const light = matchMedia("(prefers-color-scheme: light)"), say = document.querySelector("[data-status]");
  const base = say.textContent, nums = s => s.trim().split(/\s+/).map(Number), f1 = v => v.toFixed(1), cache = {};
  const said = { moving: "moving, " + FPS + " fps at most", still: "still: reduced motion", paused: "paused" };
  let id = 0, last = 0, t0 = null, shown = 0, frames = 0, fed = FEED ? " · feed: waiting" : "", wait = 5000;
  root.dataset.frames = "0";
  const status = () => { say.textContent = base + " · " + said[root.dataset.motion] + fed; };
  function scene() {  // what moves in the scheme on show, found once
    const s = light.matches ? ".light" : ".dark";
    if (!cache[s]) {
      const q = sel => [...document.querySelectorAll(s + " " + sel)];
      cache[s] = { packets: q("[data-p]").map(e => [e, nums(e.dataset.p), +e.dataset.t, +e.getAttribute("width")]),
                   drift: q("[data-drift]"), globes: q("[data-globe]"), mers: q("[data-mer]"), dashes: q("[stroke-dasharray]") };
    }
    return cache[s];
  }
  const bez = (p, t) => {
    const u = 1 - t, a = u * u * u, b = 3 * u * u * t, c = 3 * u * t * t, d = t * t * t;
    return [a * p[0] + b * p[2] + c * p[4] + d * p[6], a * p[1] + b * p[3] + c * p[5] + d * p[7]];
  };
  function globe(g, ph) {  // the kit's Fibonacci sphere, every dot made once and turned
    if (!g.dots) {
      const [cx, cy, R, n] = nums(g.dataset.globe), cs = g.dataset.c.split(" ");
      g.geo = [cx, cy, R]; g.dots = [];
      g.replaceChildren();
      for (let i = 0; i < Math.min(n, 1000); i++) {
        const e = document.createElementNS(NS, "circle"), y = 1 - 2 * (i + .5) / n, h = Math.sin(i * 12.9898) * 43758.5453;
        e.setAttribute("r", (1.3 + 1.2 * (h - Math.floor(h))).toFixed(2));
        e.setAttribute("fill", cs[i %% cs.length]);
        g.append(e);
        g.dots.push([e, y, Math.sqrt(1 - y * y), i * 2.39996]);
      }
    }
    const [cx, cy, R] = g.geo;
    for (const [e, y, s, th] of g.dots) {
      const z = s * Math.sin(th + ph);
      e.setAttribute("opacity", z > 0 ? (.45 + .5 * z).toFixed(2) : "0");
      if (z > 0) { e.setAttribute("cx", f1(cx + R * s * Math.cos(th + ph))); e.setAttribute("cy", f1(cy - R * y)); }
    }
  }
  function draw(t) {
    const s = scene();
    for (const [e, p, at, w] of s.packets) {
      const [x, y] = bez(p, (at + t * .12) %% 1);
      e.setAttribute("x", f1(x - w / 2)); e.setAttribute("y", f1(y - w / 2));
    }
    for (const g of s.drift) {
      const i = +g.dataset.drift;
      g.setAttribute("transform", `translate(${f1(3 * Math.sin(t * .7 + i * 1.9))} ${f1(2.2 * Math.cos(t * .55 + i * 1.3))})`);
    }
    for (const g of s.globes) globe(g, t * .3);
    for (const e of s.mers) e.setAttribute("rx", f1(+e.getAttribute("ry") * Math.abs(Math.cos(+e.dataset.mer * Math.PI / 6 + t * .3))));
    for (const e of s.dashes) e.setAttribute("stroke-dashoffset", f1(-t * 12));
  }
  function tick(now) {  // a frame every STEP: a 60 or 120 Hz display skips the frames between
    id = requestAnimationFrame(tick);
    if (now < last + STEP - 1) return;
    last = now - last > 2 * STEP ? now : last + STEP;
    if (t0 === null) t0 = now - shown;
    shown = now - t0;
    draw(shown / 1000);
    root.dataset.frames = String(++frames);
  }
  function play() {
    const go = !still.matches && document.visibilityState !== "hidden";
    if (go && !id) { t0 = null; last = 0; id = requestAnimationFrame(tick); }
    if (!go && id) { cancelAnimationFrame(id); id = 0; }
    root.dataset.motion = go ? "moving" : still.matches ? "still" : "paused";
    status();
  }
  still.addEventListener("change", play);
  document.addEventListener("visibilitychange", play);
  play();
  function apply(j) {  // text only, cut to 40: a feed never writes markup
    const ps = j && Array.isArray(j.panels) ? j.panels : [];
    document.querySelectorAll("figure").forEach((f, i) => {
      const v = ps[i] && ps[i].stat;
      if (typeof v !== "string" && typeof v !== "number") return;
      const s = String(v).replace(/\s+/g, " ").trim().slice(0, 40);
      for (const e of f.querySelectorAll("[data-stat]")) e.textContent = s;
      f.querySelector("[data-live]").textContent = s;
    });
  }
  function poll() {  // every 5 s; each failure doubles the wait, to a minute
    fetch(FEED, { cache: "no-store", credentials: "omit", redirect: "error" })
      .then(r => r.ok ? r.text() : Promise.reject(new Error("HTTP " + r.status)))
      .then(b => {
        if (b.length > 1e6) throw new Error("over 1 MB");
        apply(JSON.parse(b)); wait = 5000; fed = " · live " + new Date().toLocaleTimeString();
      })
      .catch(e => { wait = Math.min(wait * 2, 60000); fed = " · feed: " + String(e && e.message || e).slice(0, 60) + ", again in " + wait / 1000 + " s"; })
      .finally(() => { status(); setTimeout(poll, wait); });
  }
  if (FEED) poll();
})();"""

STYLE = """:root{color-scheme:dark light}
*{box-sizing:border-box}
body{margin:0;padding:28px clamp(12px,3vw,40px) 44px;background:%(bg)s;color:%(ink)s;font:13px/1.5 %(font)s}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 18px;margin:0 0 22px}
h1{font-size:15px;letter-spacing:.12em;text-transform:uppercase;margin:0}
header p,figcaption{margin:0;color:%(muted)s}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%%,520px),1fr));gap:22px;align-items:start}
figure{margin:0}
svg{display:block;width:100%%;height:auto}
figcaption{margin-top:8px;font-size:12px}
figcaption b{color:%(ink)s}
[data-live]{color:%(acc)s;font-weight:700}
[data-live]:not(:empty)::before{content:"· ";color:%(muted)s;font-weight:400}
.light{display:none}
@media (prefers-color-scheme: light){body{background:%(lbg)s;color:%(link)s}header p,figcaption{color:%(lmuted)s}
figcaption b{color:%(link)s}[data-live]{color:%(lacc)s}.dark{display:none}.light{display:block}}"""


def feed_url(url):
    """(the url, the connect-src it needs): an http(s) URL or a path on the page's own origin, nothing that could close
    the script or the policy it sits in. ValueError says what would do."""
    if not url:
        return "", "'none'"
    m = _ABS.fullmatch(url) if len(url) <= 2048 else None
    if m:
        return url, f"{m[1]}://{m[2]}{m[3] or ''}"
    if len(url) <= 2048 and _REL.fullmatch(url):
        return url, "'self'"
    raise ValueError(f"--feed {url[:60]!r}: an http(s):// URL or a path on the page's own site (no quotes, spaces, "
                     "angle brackets or other schemes), at most 2048 characters")


def _sha(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def _build(spec, th, feed=None, source=""):
    """(the page text, [(name, svg)] for the gates)."""
    from sampo import panels
    k = panels.kit()
    url, connect = feed_url(feed)
    figs, svgs = [], []
    for i, p in enumerate(spec["panels"], 1):
        pair = []
        for s in ("dark", "light"):
            svg = k.panel_svg(p, th[s], i, f"{s[0]}{i}-")
            svgs.append((f"{i:02d}-{p['kind']}-{s}.svg", svg))
            pair.append(f'<div class="{s}">\n{svg.rstrip()}\n</div>')
        figs.append(f'<figure id="panel-{i}" data-kind="{p["kind"]}">\n' + "\n".join(pair)
                    + f'\n<figcaption><b>{i:02d} {html.escape(p["title"])}</b> {html.escape(k.KINDS[p["kind"]]["brief"])} '
                      '<span data-live></span></figcaption>\n</figure>')
    d, li = th["dark"], th["light"]
    style = STYLE % {"bg": d.bg, "ink": d.ink, "muted": d.muted, "acc": d.txt(0), "font": k.FONT, "lbg": li.bg,
                     "link": li.ink, "lmuted": li.muted, "lacc": li.txt(0)}
    script = SCRIPT % {"fps": FPS, "feed": json.dumps(url).replace("<", "\\u003c")}
    policy = (f"default-src 'none'; style-src {_sha(style)}; script-src {_sha(script)}; connect-src {connect}; "
              "base-uri 'none'; form-action 'none'")
    title = spec.get("title") or spec.get("name") or "Panels"
    n = len(spec["panels"])
    said = f"{n} panel{'s' if n != 1 else ''}" + (f" · drawn in {source}" if source else "")
    text = "\n".join([
        "<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(policy, quote=False)}">',  # the policy holds no double quote: a feed URL can't
        f"<title>{html.escape(title)}</title>", f"<style>{style}</style>", "</head>", "<body>",
        f"<header><h1>{html.escape(title)}</h1><p data-status>{html.escape(said)}</p></header>", "<main>", *figs, "</main>",
        f"<script>{script}</script>", "</body>", "</html>"]) + "\n"
    return text, svgs


def page(spec, themes, feed=None, source=""):
    """The page's text: deterministic for a spec, themes ({dark, light}) and feed."""
    return _build(spec, themes, feed, source)[0]


def write(root, spec_path, out=None, style=None, feed=None):
    """{file, panels, source, feed, failed}: the page written to out (FILE.html, or DIR/panels.html; default
    docs/assets/panels/panels.html) only once every panel passed the gates and the spec held nothing private."""
    from sampo import panels, readme
    k, spec = panels.kit(), panels.load(spec_path)
    hits = readme.private_hits(json.dumps(spec, ensure_ascii=False) + "\n" + (feed or ""))
    if hits:
        raise ValueError(f"the spec or feed holds private text: {readme._said(hits)}")
    th, source = panels.themes(root, style or spec.get("style") or "board")
    text, svgs = _build(spec, th, feed, source)
    failed = panels._check(svgs) + [f"{a}: {b}" for a, t in svgs for b in k.check_svg(t)]
    out = os.path.abspath(out or os.path.join(root, panels.OUT))
    path = out if out.lower().endswith((".html", ".htm")) else os.path.join(out, PAGE)
    if not failed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c.write_atomic(path, text)
    return {"file": path, "panels": len(spec["panels"]), "source": source, "feed": feed or None, "failed": failed}


def dispatch(args, root):
    """True when --html or --term did the render; False leaves it to the SVG files. ValueError for a flag mix."""
    want_html, want_term = bool(getattr(args, "html", False)), bool(getattr(args, "term", False))
    feed, width = getattr(args, "feed", None), getattr(args, "width", None)
    if want_html and want_term:
        raise ValueError("--html writes a page and --term draws in this terminal: one at a time")
    if feed is not None and not want_html:
        raise ValueError("--feed names what the --html page polls for live numbers: add --html")
    if width is not None and not want_term:
        raise ValueError("--width sets --term's columns (a page fits its window): add --term or drop it")
    if not (want_html or want_term):
        return False
    from sampo import cli
    spec_path = os.path.abspath(args.target)
    if want_term:
        if args.out:
            raise ValueError("--term writes nothing: --out is for the files and the page")
        from sampo import panelterm
        res = panelterm.render(root, spec_path, args.style, width)
        if getattr(args, "json", False):
            cli.out(args, res, None)
        else:
            print(res["text"])
        return True
    res = write(root, spec_path, args.out, args.style, feed)
    rel = os.path.relpath(res["file"], root).replace(os.sep, "/")
    rel = res["file"] if rel.startswith("..") else rel
    lines = [f"sampo panels render --html: {res['panels']} panels on one page, drawn in {res['source']}, moving at "
             f"{FPS} fps at most (still under reduced motion):", f"  {rel}"]
    if res["feed"]:
        lines.append(f"  live numbers from {res['feed']}, polled every 5 s")
    lines += [f"  ✗ {x}" for x in res["failed"]]
    if res["failed"]:
        lines.append("  nothing written: the gates above must pass first")
    cli.out(args, res, "\n".join(lines))
    if res["failed"]:
        raise SystemExit(1)
    return True
