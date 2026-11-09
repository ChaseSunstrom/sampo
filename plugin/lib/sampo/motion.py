"""T-1624: the Motion Score. One file (motion.toml at the project root, or a .json with the same schema) names each
motion the UI uses, so timing and the reduced-motion path are decided once and checked, not scattered through CSS:

    [[motion]]                           # or a [motion.menu-enter] table, whose key is the name
    name = "menu-enter"                  # letters, digits, - and _
    duration = "--dur-enter"             # ms (220), "220ms"/"0.2s", or a token in DESIGN.md's Tokens table
    easing = "ease-enter"                # a CSS keyword, cubic-bezier(…), steps(…), linear(…), motion.md's
                                         # ease-enter/-exit/-move, or a token
    properties = ["opacity", "transform"]
    reduced = "fade"                     # under prefers-reduced-motion, one of:
                                         #   none: the element sits in its rest state; the motion never plays
                                         #   fade: opacity only, over the same duration, in place of any travel
                                         #   instant: the change happens at once (0 ms) to its end state

check(path) lists its problems; fm motion check [PATH] prints them (exit 1 on any). compile_score(path, to) (T-1625,
fm motion compile [PATH] --to css|waapi|three|lab [--out FILE]) emits a clean score as CSS custom properties and
@keyframes, a WAAPI options module, three.js tween parameters or (T-1626) a lab page to play, scrub and edit each motion: every target carries the score's own durations and
easings, and each motion's reduced form keeps the three meanings above. verify(path, samples) (T-1627, fm motion verify
[PATH] [--samples FILE]) holds values sampled from the page under fm smoke's frozen profile to the score's duration and
curve (the three target's own ease(p), run in node), and report() names each drift; with no browser it says so and
checks only the score."""
import html
import json
import os
import re
import textwrap
import tomllib

from sampo import design as sdesign

KEYS = ("name", "duration", "easing", "properties", "reduced")
REDUCED = ("none", "fade", "instant")
MAX_MS = 2000
KEYWORDS = {"linear", "ease", "ease-in", "ease-out", "ease-in-out", "step-start", "step-end"}
CURVES = {"ease-enter": "cubic-bezier(0.05, 0.7, 0.1, 1)", "ease-exit": "cubic-bezier(0.3, 0, 0.8, 0.15)",
          "ease-move": "cubic-bezier(0.45, 0, 0.2, 1)"}  # motion.md's defaults; DESIGN.md's tokens of the name win
NAME = re.compile(r"[A-Za-z][\w-]{0,63}", re.ASCII)  # T-1625 review: ASCII, as the messages say
PROP = re.compile(r"(?:--|-[a-z]+-)?[A-Za-z][\w-]{0,63}", re.ASCII)
NUM = r"-?(?:\d{1,9}(?:\.\d{0,9})?|\.\d{1,9})"  # bounded: no input makes these regexes backtrack for long
TIME = re.compile(r"(" + NUM + r")\s*(ms|s)")
_N = r"\s*(" + NUM + r")\s*"
BEZIER = re.compile(r"cubic-bezier\(" + ",".join([_N] * 4) + r"\)")
STEPS = re.compile(r"steps\(\s*(\d{1,4})\s*(?:,\s*(jump-start|jump-end|jump-none|jump-both|start|end)\s*)?\)")
_STOP = NUM + r"(?:\s+" + NUM + r"%){0,2}"
LINEAR = re.compile(r"linear\(\s*(" + _STOP + r"(?:\s*,\s*" + _STOP + r"){1,63})\s*\)")
SEP = re.compile(r":?-+:?")
NO_DESIGN = "no DESIGN.md (fm design extract writes its Tokens table)"


def _show(v):
    r = repr(v)
    return r if len(r) <= 60 else r[:59] + "…"


def design_path(score):
    """DESIGN.md next to the score, else at its repo's root or in docs/ there, else None."""
    from sampo import core
    here = os.path.dirname(os.path.abspath(score))
    root = core.git_root(here) or here
    return next((p for p in (os.path.join(d, "DESIGN.md") for d in (here, root, os.path.join(root, "docs")))
                 if os.path.isfile(p)), None)


def _cells(ln):
    cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", ln.strip())[1:]]
    if cells and not cells[-1]:
        cells.pop()
    return cells


def tokens_of(text):
    """({name without leading dashes: value}, [line of each table with no Light or Value column]) from a DESIGN.md's
    ## Tokens section, or (None, []) when it has none. Every table there counts: a row followed by a separator row is a
    header, and its Token and Light (fm design extract's) or Value column hold for the rows under it."""
    doc = text.replace("\r\n", "\n").split("\n")
    heads, _ = sdesign.headings(doc)
    start = next((i for i, h in heads if re.fullmatch(r"##\s+tokens\s*#*", h, re.I)), None)
    if start is None:
        return None, []
    end = next((i for i, _ in heads if i > start), len(doc))
    rows = [(i, _cells(doc[i]) if doc[i].strip().startswith("|") else None) for i in range(start + 1, end)]
    out, blind, cols = {}, [], None
    for k, (i, cells) in enumerate(rows):
        if not cells:  # a line that isn't a row ends the table
            cols = None
            continue
        if all(SEP.fullmatch(c) for c in cells):
            continue
        nxt = rows[k + 1][1] if k + 1 < len(rows) else None
        if nxt and all(SEP.fullmatch(c) for c in nxt):  # a header
            low = [c.strip("`* ").lower() for c in cells]
            value = next((low.index(w) for w in ("light", "value") if w in low), None)
            cols = None if value is None else (low.index("token") if "token" in low else 0, value)
            if value is None:
                blind.append(i + 1)
            continue
        if cols and len(cells) > max(cols):
            name, value = cells[cols[0]].strip("`").strip(), cells[cols[1]]
            quoted = re.search(r"`([^`]*)`", value)
            if name:
                out.setdefault(name.lstrip("-"), (quoted.group(1) if quoted else value).strip())
    return out, blind


def _curve(text):
    """(curve, None) for a CSS easing (any case, spaces trimmed), (None, why) for a malformed bezier or steps(),
    None for anything else."""
    s = text.strip().lower()
    if s in KEYWORDS:
        return s, None
    m = BEZIER.fullmatch(s)
    if m:
        x1, y1, x2, y2 = m.groups()
        if not (0 <= float(x1) <= 1 and 0 <= float(x2) <= 1):
            return None, f"easing {_show(text)}: x1 and x2 must lie in 0–1"
        return f"cubic-bezier({x1}, {y1}, {x2}, {y2})", None
    m = STEPS.fullmatch(s)
    if m:
        n, pos = int(m.group(1)), m.group(2)
        if n < 1 or (pos == "jump-none" and n < 2):
            return None, f"easing {_show(text)}: steps() needs at least 1 step (2 with jump-none)"
        return f"steps({n}{', ' + pos if pos else ''})", None
    m = LINEAR.fullmatch(s)
    if m:
        return "linear(" + ", ".join(" ".join(p.split()) for p in m.group(1).split(",")) + ")", None
    return None


def _duration(v, look):
    """(ms, problem)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None, f"duration {_show(v)} must be a number of ms or a token name"
    if not isinstance(v, str):
        if 0 < v < 10:
            return None, f"duration {v:g}: bare numbers are ms; write '{v:g}s' (or '{v:g}ms' if you mean it)"
    else:
        s = shown = v.strip()
        if re.fullmatch(NUM, s):
            return None, f"duration {_show(v)} has no unit: write {s} (ms) or '{s}ms'"
        if not TIME.fullmatch(s):
            s, bad = look(shown, "duration")
            if bad:
                return None, bad
            if not TIME.fullmatch(s):
                return None, f"duration token {_show(shown)} is {_show(s)} in DESIGN.md, not a time"
        m = TIME.fullmatch(s)
        v = float(m.group(1)) * (1 if m.group(2) == "ms" else 1000)
        if v < 0:
            return None, f"duration {_show(shown)} is negative"
    v = round(v, 3)
    if not 0 <= v <= MAX_MS:  # nan and inf fail here too
        return None, f"duration {v:g} ms is outside 0–{MAX_MS}" if isinstance(v, float) else \
            f"duration {_show(v)} ms is outside 0–{MAX_MS}"
    return (int(v) if v == int(v) else v), None


def _easing(v, tokens, look):
    """(curve, problem)."""
    if not isinstance(v, str):
        return None, f"easing {_show(v)} must be a string"
    got = _curve(v)
    if got:
        return got
    s = v.strip()
    if s.lstrip("-") in tokens or s.startswith("--"):
        t, bad = look(s, "easing")
        if bad:
            return None, bad
        return _curve(t) or (None, f"easing token {_show(s)} is {_show(t)} in DESIGN.md, not an easing")
    if s.lower() in CURVES:
        return CURVES[s.lower()], None
    return None, f"unknown easing {_show(v)} (a CSS keyword, cubic-bezier(…), steps(…), linear(…), " \
                 "ease-enter/-exit/-move or a token)"


def _properties(v):
    if not isinstance(v, list) or not v or not all(isinstance(p, str) for p in v):
        return None, [f"properties must be a non-empty list of CSS property names, not {_show(v)}"]
    bad = [f"property {_show(p)} is not a CSS property name" for p in v if p != "all" and not PROP.fullmatch(p)]
    return v, bad + (["properties: list them, not all"] if "all" in v else [])


def parse(path):
    """The score's data: TOML, or JSON for a .json file. ValueError when it can't be read or parsed."""
    text, why = sdesign._read(path, strict=True)
    if text is None:
        raise ValueError(f"{path}: {why}")
    try:
        data = json.loads(text) if path.lower().endswith(".json") else tomllib.loads(text)
    except (ValueError, RecursionError) as e:  # both decoders raise ValueErrors naming the line
        raise ValueError(f"{path}: {e or 'nested too deep'}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the score is a table holding [[motion]] entries, not {type(data).__name__}")
    return data


def _lookup(design):
    """(tokens, look): DESIGN.md's tokens, and look(name, kind) -> (value, problem) for a token a motion names."""
    tokens, why, note = {}, NO_DESIGN, None
    if design:
        text, err = sdesign._read(design, strict=True)
        found, blind = tokens_of(text) if text is not None else ({}, [])
        tokens = found or {}
        note = blind and f"DESIGN.md's Tokens table at line {blind[0]} has no Light or Value column"
        why = f"DESIGN.md: {err}" if text is None else None if tokens else \
            "DESIGN.md has no ## Tokens table (fm design extract writes one)" if found is None else \
            note or "DESIGN.md's Tokens table lists no tokens"

    def look(name, kind):
        t = tokens.get(name.lstrip("-"))
        if t is None:
            return None, f"{kind} {_show(name)}: {why}" if why else \
                f"DESIGN.md has no token {_show(name)}" + (f" ({note})" if note else "")
        if t.endswith("…"):
            return None, f"{kind} token {_show(name)}: its DESIGN.md value {_show(t)} was truncated by fm design " \
                         "extract; rerun it, or write the value in the score"
        return t, None
    return tokens, look


def check(path):
    """{score, design, motions, problems}: each motion resolved (ms, curve; None where it can't be) and each problem
    as {motion: its name, or "motion N" when the name is bad, or None for the file; problem: text}."""
    data = parse(path)
    design = design_path(path)
    tokens, look = _lookup(design)
    problems, motions, seen = [], [], set()

    def flag(who, text):
        problems.append({"motion": who, "problem": text})

    for k in sorted(set(data) - {"motion"}):
        flag(None, f"unknown key {_show(k)} (the score holds [[motion]] entries or [motion.NAME] tables)")
    entries, keys = data.get("motion"), None
    if not entries:
        flag(None, "no motions ([[motion]] entries or [motion.NAME] tables)")
        entries = []
    elif isinstance(entries, dict):  # [motion.NAME] tables: the key is the name
        keys, entries = list(entries), list(entries.values())
    elif not isinstance(entries, list):
        flag(None, "motion must be [[motion]] entries or [motion.NAME] tables")
        entries = []
    for i, m in enumerate(entries, 1):
        if not isinstance(m, dict):
            flag(f"motion {i}", f"not a table: {_show(m)}")
            continue
        clash = None
        if keys:
            clash = m["name"] if "name" in m and m["name"] != keys[i - 1] else None
            m = {**m, "name": keys[i - 1]}
        name = m.get("name")
        who = name if isinstance(name, str) and NAME.fullmatch(name) else f"motion {i}"
        if "name" not in m:
            flag(who, "no name")
        elif who != name:
            flag(who, f"name {_show(name)} must be letters, digits, - or _, starting with a letter (at most 64)")
        elif name in seen:
            flag(who, "the name is used twice")
        if clash is not None:
            flag(who, f"name {_show(clash)} differs from its table key")
        seen.add(who)
        for k in sorted(set(m) - set(KEYS), key=str):
            flag(who, f"unknown key {_show(k)} ({', '.join(KEYS)})")
        ms, bad = _duration(m["duration"], look) if "duration" in m else (None, "no duration")
        curve, bad2 = _easing(m["easing"], tokens, look) if "easing" in m else (None, "no easing")
        props, bad3 = _properties(m["properties"]) if "properties" in m else \
            (None, ["no properties (list what it animates)"])
        reduced = m.get("reduced")
        bad4 = "no reduced form (none, fade or instant)" if "reduced" not in m else None if reduced in REDUCED \
            else f"reduced form {_show(reduced)} is not none, fade or instant"
        for b in [bad, bad2, *bad3, bad4]:
            if b:
                flag(who, b)
        motions.append({"name": who, "ms": ms, "curve": curve, "properties": props,
                        "reduced": reduced if reduced in REDUCED else None})
    return {"score": path, "design": design, "motions": motions, "problems": problems}


TARGETS = ("css", "waapi", "three", "lab")
NAMED = {"linear": "linear(0, 1)", "ease": "cubic-bezier(0.25, 0.1, 0.25, 1)", "ease-in": "cubic-bezier(0.42, 0, 1, 1)",
         "ease-out": "cubic-bezier(0, 0, 0.58, 1)", "ease-in-out": "cubic-bezier(0.42, 0, 0.58, 1)",
         "step-start": "steps(1, jump-start)", "step-end": "steps(1, jump-end)"}  # CSS Easing's own definitions
REDUCED_NOTE = ('form "none" plays nothing (it stays in its rest state), "instant" goes to the end state at once '
                '(duration 0), "fade" animates opacity only over the same duration')


def curve_data(curve):
    """A resolved curve as data for three.js, which has no CSS easings: ["bezier", x1, y1, x2, y2], ["steps", n,
    "jump-…"] or ["linear", [[x, y], …]] with every x filled in as CSS Easing 2's linear() does."""
    curve = NAMED.get(curve, curve)
    m = BEZIER.fullmatch(curve)
    if m:
        return ["bezier", *map(float, m.groups())]
    m = STEPS.fullmatch(curve)
    if m:
        pos = m.group(2) or "end"
        return ["steps", int(m.group(1)), pos if pos.startswith("jump-") else "jump-" + pos]
    stops = LINEAR.fullmatch(curve).group(1).split(",")
    pts, top = [], float("-inf")
    for i, stop in enumerate(stops):  # a missing x: 0 first, max(1, the largest so far) last, else spaced evenly
        y, *xs = stop.split()
        bare = 0.0 if i == 0 else max(1.0, top) if i == len(stops) - 1 else None
        for x in [float(x[:-1]) / 100 for x in xs] or [bare]:
            if x is not None:
                x = top = max(top, x)  # an x below an earlier one is raised to it
            pts.append([x, float(y)])
    known = [i for i, p in enumerate(pts) if p[0] is not None]
    for a, b in zip(known, known[1:]):
        for k in range(a + 1, b):
            pts[k][0] = pts[a][0] + (pts[b][0] - pts[a][0]) * (k - a) / (b - a)
    return ["linear", pts]


def _variants(m):
    """(a motion's parameters, its reduced form's), each {duration ms, easing, properties}; the reduced one names its
    form too."""
    form = m["reduced"]
    return ({"duration": m["ms"], "easing": m["curve"], "properties": m["properties"]},
            {"form": form, "duration": m["ms"] if form == "fade" else 0, "easing": m["curve"],
             "properties": ["opacity"] if form == "fade" else m["properties"] if form == "instant" else []})


def _label(name):
    """The score's file name for a header comment: quoted, ASCII, and unable to end a /* */ comment or a <script>."""
    return json.dumps(name).replace("*/", "*\\/").replace("</", "<\\/")


def _css(motions, label):
    root, reduced, frames = [], [], []
    for m in motions:
        n, full, red = m["name"], *_variants(m)
        for v, into, ind in ((full, root, "  "), (red, reduced, "    ")):
            ms, curve = f"{json.dumps(v['duration'])}ms", v["easing"]
            frame = f"reduced-motion-{n}" if v.get("form") == "fade" else f"motion-{n}"
            tr, an = ("all 0s", "none") if v.get("form") == "none" else \
                (", ".join(f"{p} {ms} {curve}" for p in v["properties"]), f"{frame} {ms} {curve} backwards")
            # all 0s, not none: none is only valid alone, so it would void a comma list holding other motions
            into += [f"{ind}--motion-{n}-duration: {ms};", f"{ind}--motion-{n}-easing: {curve};",
                     f"{ind}--motion-{n}-transition: {tr};", f"{ind}--motion-{n}-animation: {an};"]
        starts = " ".join(f"{p}: var(--motion-{n}-from-{p});" for p in full["properties"])
        frames.append(f"@keyframes motion-{n} {{\n  from {{ {starts} }}\n}}")
        if red["form"] == "fade":
            frames.append(f"@keyframes reduced-motion-{n} {{\n  from {{ opacity: var(--motion-{n}-from-opacity, 0); }}"
                          "\n}")
    plain = "\n".join(ln[2:] for ln in reduced)
    return "\n".join([
        f"/* fm motion compile --to css (T-1625): {len(motions)} motion(s) from {label}.",
        "   Each motion NAME sets --motion-NAME-duration, -easing, -transition (for transition:) and -animation (for",
        "   animation:, which starts from var(--motion-NAME-from-PROPERTY), set where it plays, and ends at the",
        "   element's own style). Under prefers-reduced-motion, unless <html data-motion=\"full\">, or with",
        "   <html data-motion=\"reduce\">, each motion takes its reduced form: none plays nothing (the element sits in",
        "   its rest state; transition all 0s, animation none), instant jumps to the end state (0ms), fade animates",
        "   opacity only over the same duration.",
        "*/", ":root {", *root, "}", *frames, "@media (prefers-reduced-motion: reduce) {",
        '  :root:not([data-motion="full"]) {', *reduced, "  }", "}", ':root[data-motion="reduce"] {', plain, "}", ""])


THREE_EASE = """\
const bezier = (x1, y1, x2, y2) => {
  const at = (a, b, t) => 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3;
  return (p) => {
    if (p <= 0 || p >= 1) return p <= 0 ? 0 : 1;
    let lo = 0, hi = 1;  // x(t) rises with t when x1 and x2 lie in 0-1, as fm motion check makes sure
    for (let i = 0; i < 40; i++) { const t = (lo + hi) / 2; if (at(x1, x2, t) < p) lo = t; else hi = t; }
    return at(y1, y2, (lo + hi) / 2);
  };
};
const steps = (n, pos) => {
  const jumps = pos === "jump-none" ? n - 1 : pos === "jump-both" ? n + 1 : n;
  const lead = pos === "jump-start" || pos === "jump-both" ? 1 : 0;
  return (p) => Math.min(Math.max(Math.floor(p * n) + lead, 0), jumps) / jumps;
};
const linear = (pts) => (p) => {
  let i = 1;
  while (i < pts.length - 1 && pts[i][0] <= p) i++;
  const [[x0, y0], [x1, y1]] = [pts[i - 1], pts[i]];
  return x1 === x0 ? y1 : y0 + ((y1 - y0) * (p - x0)) / (x1 - x0);
};
const ease = ([kind, ...a]) => (kind === "steps" ? steps(...a) : kind === "linear" ? linear(a[0]) : bezier(...a));
for (const m of Object.values(motion)) for (const v of [m, m.reduced]) v.ease = ease(v.curve);
"""
PICK = """\
// as the CSS: reduced under the system's prefers-reduced-motion unless <html data-motion="full">, and always with
// data-motion="reduce" (the motion playbook's app setting)
export const reduced = () => {
  const set = typeof document === "object" ? document.documentElement.dataset.motion : undefined;
  const system = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  return set === "reduce" || (set !== "full" && system);
};
export const pick = (name) => (reduced() ? motion[name].reduced : motion[name]);
"""
PLAY = """\
// play(el, NAME, frames): el.animate() in whichever form applies, keeping only the properties that form lists (a fade
// needs opacity in the frames); null for "none", which plays nothing: play(...)?.finished
export const play = (el, name, frames) => {
  const m = pick(name);
  if (m.form === "none") return null;
  const camel = (p) => (p === "float" ? "cssFloat" : p.startsWith("--") ? p : p.replace(/-([a-z])/g, (_, c) => c.toUpperCase()));
  const keep = new Set([...m.properties.map(camel), "offset", "easing", "composite"]);
  const only = (f) => Object.fromEntries(Object.entries(f).filter(([k]) => keep.has(k)));
  return el.animate(Array.isArray(frames) ? frames.map(only) : only(frames), { duration: m.duration, easing: m.easing });
};
"""


def _module(motions, label, to):
    data = {}
    for m in motions:
        full, red = _variants(m)
        if to == "three":
            full["curve"] = red["curve"] = curve_data(m["curve"])
        data[m["name"]] = {**full, "reduced": red}
    head = (f"fm motion compile --to {to} (T-1625): {len(motions)} motion(s) from {label}. " + (
        "motion[NAME] is el.animate()'s options: duration in ms, easing as CSS writes it, and properties, what its "
        "keyframes may animate." if to == "waapi" else
        "three.js has no tweens or easings of its own, so motion[NAME] holds tween parameters: duration in ms (the "
        "same numbers as CSS and WAAPI; / 1000 for an AnimationClip's seconds), easing as CSS writes it, curve as data, "
        "and ease(p), that curve as a function from progress 0-1 to 0-1 (tween.js: .easing(m.ease)); steps() and "
        "linear() map exactly; a fade is material.opacity with transparent: true.")
        + f" motion[NAME].reduced is its reduced form: {REDUCED_NOTE}. pick(NAME) gives whichever applies now"
        + (", play(el, NAME, frames) runs it." if to == "waapi" else "."))
    rows = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in data.items())  # one motion a line: diffable
    body = [f"export const motion = {{\n{rows}\n}};"] + ([THREE_EASE] if to == "three" else [])
    return "\n".join(["// " + ln for ln in textwrap.wrap(head, 114)] + body + [PICK + (PLAY if to == "waapi" else "")])


def compile_score(path, to):
    """(the score at path compiled for to, one of TARGETS, [] ) or (None, check's problems) when it isn't clean.
    ValueError when it can't be read."""
    if to not in TARGETS:
        raise ValueError(f"--to {_show(to)}: one of {', '.join(TARGETS)}")
    got = check(path)
    if got["problems"]:
        return None, got["problems"]
    label = _label(os.path.basename(path))
    if to == "lab":
        return _lab(got["motions"], label), []
    return (_css(got["motions"], label) if to == "css" else _module(got["motions"], label, to)), []


LAB = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Motion lab - {{TITLE}}</title>
<style>
:root { color-scheme: light dark; --fg: #1d1d1f; --dim: #6b6b6b; --bg: #fafaf9; --line: #d4d4d0; --accent: #3366cc; }
@media (prefers-color-scheme: dark) { :root { --fg: #ececec; --dim: #9a9a9a; --bg: #161616; --line: #3a3a3a; --accent: #7aa2ff; } }
body { margin: 0; padding: 16px; font: 14px/1.45 system-ui, sans-serif; color: var(--fg); background: var(--bg); }
h1 { font-size: 18px; margin: 0 0 4px; } p { margin: 0 0 12px; color: var(--dim); max-width: 70ch; }
.m { display: grid; grid-template-columns: 112px 1fr; gap: 16px; padding: 14px 0; border-top: 1px solid var(--line); }
.head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; } .dim { color: var(--dim); }
input[type=number] { width: 6em; } input[type=range] { width: 100%; margin: 0; }
.track { position: relative; height: 28px; margin-top: 8px; border-bottom: 1px solid var(--line); }
.dot { position: absolute; left: 0; top: 4px; width: 20px; height: 20px; border-radius: 50%; background: var(--accent); }
svg { width: 112px; height: 205px; border: 1px solid var(--line); touch-action: none; overflow: visible; }
.curve { stroke: var(--accent); stroke-width: 0.03; } .arm { stroke: var(--dim); stroke-width: 0.015; }
.handle { fill: var(--bg); stroke: var(--fg); stroke-width: 0.02; pointer-events: none; }
.hit { fill: transparent; cursor: grab; } .hit:focus { outline: none; } .hit:focus + .handle { stroke: var(--accent); stroke-width: 0.04; }  /* a 28 px target over the dot; an outline scales with the viewBox */
code { display: block; margin-top: 8px; white-space: pre; user-select: all; font: 12px/1.4 ui-monospace, monospace; }
</style>
<h1>Motion lab</h1>
<p>From {{TITLE}}. Play a motion, or drag the slider under it to scrub. Drag a curve's handles (or focus one and use the
arrow keys) and change the duration; the lines under each motion are ready to paste back into the score.
<label><input type="checkbox" id="red"> reduced motion</label></p>
<div id="list"></div>
<script>
const MOTIONS = {{DATA}};
const NS = "http://www.w3.org/2000/svg", red = document.getElementById("red"), redraws = [];
red.checked = matchMedia("(prefers-reduced-motion: reduce)").matches;
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const svgEl = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function curveBox(m, changed) {
  const svg = svgEl("svg", { viewBox: "-0.1 -0.6 1.2 2.2", role: "img", "aria-label": m.name + " easing: " + m.curve });
  const b = m.bezier;
  if (!b) { const t = svgEl("text", { x: 0, y: 0.55, "font-size": 0.12, fill: "currentColor" }); t.textContent = "no handles"; svg.append(t); return svg; }
  const path = svgEl("path", { fill: "none", class: "curve" }), arms = [svgEl("line", { x1: 0, y1: 1, class: "arm" }), svgEl("line", { x1: 1, y1: 0, class: "arm" })];
  const hs = [0, 1].map(k => svgEl("circle", { r: 0.15, class: "hit", tabindex: 0, role: "slider", "aria-label": m.name + " handle " + (k + 1) }));
  const dots = [0, 1].map(() => svgEl("circle", { r: 0.06, class: "handle" }));
  const draw = () => {
    path.setAttribute("d", `M0 1C${b[0]} ${1 - b[1]} ${b[2]} ${1 - b[3]} 1 0`);
    hs.forEach((c, k) => {
      for (const [e, x, y] of [[arms[k], "x2", "y2"], [c, "cx", "cy"], [dots[k], "cx", "cy"]]) { e.setAttribute(x, b[2 * k]); e.setAttribute(y, 1 - b[2 * k + 1]); }
      c.setAttribute("aria-valuetext", `x ${b[2 * k]}, y ${b[2 * k + 1]}`);
    });
  };
  const move = (k, x, y) => {
    b[2 * k] = +clamp(x, 0, 1).toFixed(3); b[2 * k + 1] = +clamp(y, -0.5, 1.5).toFixed(3);
    m.curve = `cubic-bezier(${b.join(", ")})`; draw(); changed();
  };
  hs.forEach((c, k) => {
    c.addEventListener("pointerdown", e => {
      c.setPointerCapture(e.pointerId);
      c.onpointermove = ev => { const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(svg.getScreenCTM().inverse()); move(k, p.x, 1 - p.y); };
      c.onpointerup = () => { c.onpointermove = null; };
    });
    c.addEventListener("keydown", e => {
      const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[e.key];
      if (d) { e.preventDefault(); move(k, b[2 * k] + d[0] * 0.02, b[2 * k + 1] + d[1] * 0.02); }
    });
  });
  svg.append(path, ...arms, hs[0], dots[0], hs[1], dots[1]); draw();
  return svg;
}

for (const m of MOTIONS) {
  const row = el("section", "m"), side = el("div"), head = el("div", "head"), track = el("div", "track"), dot = el("div", "dot");
  const play = el("button", null, "Play"), dur = el("input"), scrub = el("input"), line = el("code");
  Object.assign(dur, { type: "number", min: 0, max: 2000, step: 10, value: m.ms });
  Object.assign(scrub, { type: "range", min: 0, max: 1000, value: 0 });
  dur.setAttribute("aria-label", m.name + " duration in ms"); scrub.setAttribute("aria-label", "scrub " + m.name);
  head.append(el("b", null, m.name), play, dur, el("span", "dim", "ms, reduced: " + m.reduced));
  track.append(dot); side.append(head, track, scrub, line);
  let anim = null;
  const redraw = () => {
    if (anim) anim.cancel();
    const form = red.checked ? m.reduced : null, w = track.clientWidth - dot.offsetWidth;
    const frames = form === "fade" ? [{ opacity: 0 }, { opacity: 1 }] : [{ transform: "translateX(0)" }, { transform: `translateX(${w}px)` }];
    anim = form === "none" ? null : dot.animate(frames, { duration: form === "instant" ? 0 : m.ms, easing: m.curve, fill: "both" });
    if (anim) { anim.pause(); anim.currentTime = scrub.value / 1000 * m.ms; }
    play.disabled = scrub.disabled = !anim; play.title = anim ? "" : "reduced: none plays nothing";
    line.textContent = `duration = ${m.ms}\neasing = "${m.curve}"`;
  };
  row.append(curveBox(m, redraw), side);
  document.getElementById("list").append(row);
  const follow = () => { if (anim && anim.playState === "running") { scrub.value = m.ms ? anim.currentTime / m.ms * 1000 : 1000; requestAnimationFrame(follow); } };
  play.onclick = () => { scrub.value = 0; redraw(); if (anim) { anim.play(); follow(); } };
  scrub.oninput = () => { if (anim) { anim.pause(); anim.currentTime = scrub.value / 1000 * m.ms; } };
  dur.oninput = () => { m.ms = clamp(Math.round(+dur.value || 0), 0, 2000); redraw(); };
  scrub.addEventListener("pointerdown", () => anim && anim.pause());
  redraws.push(redraw);
}
red.onchange = () => redraws.forEach(f => f());
addEventListener("resize", () => redraws.forEach(f => f()));
redraws.forEach(f => f());
</script>
</html>
"""


def _lab(motions, label):
    """T-1626: one self-contained page that plays each motion, scrubs it, and edits its duration and bezier handles
    into score lines to paste back. Names, curves and forms are check()'s validated ones; </ is escaped anyway."""
    data = [{"name": m["name"], "ms": m["ms"], "curve": m["curve"], "reduced": m["reduced"],
             "bezier": (lambda c: c[1:] if c[0] == "bezier" else None)(curve_data(m["curve"]))} for m in motions]
    page = LAB.replace("{{DATA}}", json.dumps(data).replace("</", "<\\/"), 1)  # first: the label can't land in it
    return page.replace("{{TITLE}}", html.escape(label.strip('"')))


TOL = 0.02  # a sample may sit this share of its property's travel (from to to) off the score's curve
MAX_SAMPLES, MAX_AT = 500, 64  # entries a verify reads, points each: the page or the file is outside text
EASE_RUN = "const motion = {};\n" + THREE_EASE + """
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () =>
  console.log(JSON.stringify(JSON.parse(s).map(([c, ps]) => ps.map(ease(c))))));
"""


def _eases(jobs):
    """[[ease(p) for p in ps] for curve, ps in jobs], by the three target's own ease() run in node (one solver, T-1625):
    None when there is no node to run it."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        return None
    try:
        p = subprocess.run([node, "-e", EASE_RUN], input=json.dumps([[curve_data(c), ps] for c, ps in jobs]),
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise ValueError("node took over 30 s to run ease(p)") from None
    if p.returncode:
        raise ValueError(f"node could not run ease(p): {p.stderr.strip()[-200:]}")
    return json.loads(p.stdout)


def _nums(v):
    """A sampled value as a list of numbers (a number alone, or a transform's matrix), else None."""
    v = [v] if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    ok = isinstance(v, list) and len(v) <= 16 and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and abs(x) < 1e12 for x in v)
    return [float(x) for x in v] if ok else None


def _round(v):
    out = [round(x, 4) for x in v]
    return f"{out[0]:g}" if len(out) == 1 else "[" + ", ".join(f"{x:g}" for x in out) + "]"


# properties whose computed values move in a straight line from one end to the other, so from + (to - from) * ease(p)
# predicts them: opacity, lengths, translate and scale. Colours (premultiplied, maybe another space), rotations (a
# matrix's sines), filters and shadows don't: verify notes those instead of flagging good CSS.
LINEAR_PROPS = re.compile(r"opacity|fill-opacity|stroke-opacity|translate|scale|transform|(?:min-|max-)?(?:width|height)|"
                          r"top|right|bottom|left|inset(?:-[a-z-]+)?|(?:margin|padding)(?:-[a-z-]+)?|"
                          r"(?:row-|column-)?gap|font-size|letter-spacing|word-spacing|flex-basis|"
                          r"border(?:-[a-z]+)*-radius|border(?:-[a-z]+)?-width|outline-(?:width|offset)|"
                          r"stroke-(?:width|dashoffset)")


def _linear(prop, frm, to):
    """Whether prop moves linearly from frm to to: a transform only as a 2D matrix with no rotation or skew (b = c = 0
    at both ends), where scale and translate interpolate linearly."""
    if not LINEAR_PROPS.fullmatch(prop):
        return False
    return prop != "transform" or len(frm) == 6 and all(abs(v[k]) < 1e-9 for v in (frm, to) for k in (1, 2))


def verify(path, samples, why=None, tol=TOL):
    """{score, problems, drifts, checked, skipped}: each sample (an entry {motion, property, where, from, to, at:
    [[ms, value], ...]}, as the frozen crawl returns them) asserted against the score's own duration and curve: a value
    at t ms is expected at from + (to - from) * ease(t / duration) within tol of the travel. samples None: only the
    score is checked, and skipped says why (why)."""
    got = check(path)
    out = {"score": path, "problems": list(got["problems"]), "drifts": [], "checked": 0, "notes": [],
           "skipped": None if samples is not None else (why or "no samples")}
    if samples is None or got["problems"]:
        return out
    flag = lambda who, text: out["problems"].append({"motion": who, "problem": text})  # noqa: E731
    if not isinstance(samples, list):
        flag(None, "samples must be a list of {motion, property, from, to, at} entries")
        return out
    if len(samples) > MAX_SAMPLES:
        flag(None, f"{len(samples)} samples: only the first {MAX_SAMPLES} were read")
    score = {m["name"]: m for m in got["motions"]}
    rows = []
    for i, s in enumerate(samples[:MAX_SAMPLES], 1):
        m = score.get(s.get("motion")) if isinstance(s, dict) else None
        at = s.get("at") if isinstance(s, dict) else None
        prop = s.get("property") if isinstance(s, dict) else None
        if not isinstance(s, dict) or not isinstance(s.get("motion"), str) or not isinstance(prop, str) \
                or not PROP.fullmatch(prop) or not isinstance(at, list) \
                or not all(isinstance(a, list) and len(a) == 2 for a in at):
            flag(None, f"sample {i}: not {{motion, property, from, to, at: [[ms, value], …]}}: {_show(s)}")
            continue
        if m is None:
            flag(None, f"sample {i}: motion {_show(s['motion'])} is not in the score")
            continue
        who = m["name"]  # the score's own name and a checked property name: safe to print
        if prop not in m["properties"]:
            flag(who, f"sample {i} animates {prop}, which the score doesn't list")
            continue
        frm, to = _nums(s.get("from")), _nums(s.get("to"))
        pts = [(a[0], _nums(a[1])) for a in at[:MAX_AT]]
        if frm is None or to is None or any(not isinstance(t, (int, float)) or isinstance(t, bool)
                                            or not 0 <= t <= MAX_MS * 10 or v is None for t, v in pts):
            flag(who, f"sample {i}: {prop}'s values aren't numbers (a number or a list of them at ms 0–{MAX_MS * 10})")
            continue
        why = "its values differ in length" if not frm or any(len(v) != len(frm) for v in [to] + [v for _, v in pts]) \
            else None if _linear(prop, frm, to) else "it doesn't move in a straight line between its ends"
        if why:
            if not any(n.startswith(f"{who} {prop}: ") for n in out["notes"]):  # one note per motion and property
                out["notes"].append(f"{who} {prop}: not comparable ({why}), not checked")
            continue
        rows.append((m, prop, str(s.get("where") or "")[:80], frm, to, pts))
    eased = _eases([(m["curve"], [min(t / m["ms"], 1) if m["ms"] else 1 for t, _ in pts])
                    for m, _, _, _, _, pts in rows]) if rows else []
    if eased is None:
        out["skipped"] = "no node to run the score's ease(p)"
        return out
    for (m, prop, where, frm, to, pts), es in zip(rows, eased):
        for (t, v), e in zip(pts, es):
            out["checked"] += 1
            want = [a + (b - a) * e for a, b in zip(frm, to)]
            room = [max(tol * abs(b - a), 0.001) for a, b in zip(frm, to)]
            if any(abs(x - w) > r for x, w, r in zip(v, want, room)):
                out["drifts"].append({"motion": m["name"], "property": prop, "where": where, "ms": t,
                                      "expected": want, "sampled": v, "tolerance": room})
    return out


def report(got):
    """verify's result as text: each problem, each drift naming motion, property, time, expected, sampled and the
    tolerance, then one summary line."""
    from sampo import smoke
    label = os.path.basename(got["score"])
    lines = _report(got["problems"], label)
    for d in got["drifts"]:
        room = d["tolerance"]
        lines.append(f"{d['motion']} {d['property']} at {d['ms']:g} ms" + (f" ({smoke._clean(d['where'])})"
                                                                          if d["where"] else "")
                     + f": expected {_round(d['expected'])}, sampled {_round(d['sampled'])} (tolerance ±"
                     + (f"{room[0]:g}" if len(set(room)) == 1 else _round(room)) + ")")
    lines += [f"  · {smoke._clean(x)}" for x in got.get("notes") or []]
    n, k = len(got["problems"]), len(got["drifts"])
    if got["skipped"]:
        lines.append(f"motion verify: {got['skipped']}; checked the score only: "
                     + (f"{n} problem(s)" if n else "clean") + f" ({got['score']})")
    elif n or k:
        lines.append(f"motion verify: {k} drift(s), {n} problem(s) in {got['checked']} sample(s) ({got['score']})")
    elif not got["checked"]:
        lines.append(f"motion verify: nothing checked (no motion-NAME animation played on the page, or none could be "
                     f"compared); the score is clean ({got['score']})")
    else:
        lines.append(f"motion verify: clean, {got['checked']} sample(s) against the score ({got['score']})")
    return "\n".join(lines)


def _sampler():
    """smoke_web.cjs's sampleMotion, as source: the one sampler fm smoke's crawl and fm motion verify both run."""
    from sampo import smoke
    with open(smoke.SCRIPT, encoding="utf-8") as f:
        m = re.search(r"^function sampleMotion\(where\) \{\n.*?^\}\n", f.read(), re.M | re.S)
    return m.group(0) if m else None


def sample(url, root):
    """(samples, None, notes) from the page at url, or (None, why, []) when it can't be sampled. T-1716: one page in
    fm shot's browser (fmshot.find_browser: no Playwright needed, nothing installed) with fm smoke's frozen clock and
    paused animations, then smoke_web.cjs's sampleMotion run in the page, seeking each paused motion-NAME animation
    through one iteration. ponytail: one page at one width; the views a crawl walked aren't visited."""
    from sampo import shot
    path, bad = shot.find_browser("fm motion verify")
    if not path:
        return None, bad, []
    src = _sampler()
    if not src:
        return None, "smoke_web.cjs has no sampleMotion to run", []
    try:
        b = shot.Browser(path, shot.SIZE)
    except (OSError, RuntimeError, EOFError, ValueError) as e:  # TimeoutError is an OSError
        return None, f"no browser: {shot.safe(path)} did not start: {e}", []
    try:
        b.call("Page.addScriptToEvaluateOnNewDocument", b.sid, source=shot.FREEZE)
        notes = shot.load(b, url)
        got = b.call("Runtime.evaluate", b.sid, expression=f"({src})({json.dumps('page')})", returnByValue=True)
    except (OSError, RuntimeError, EOFError, ValueError) as e:
        return None, f"the page wasn't sampled: {e}", []
    finally:
        b.close()
    if got.get("exceptionDetails"):
        return None, f"the page wasn't sampled: {str(got['exceptionDetails'].get('text') or 'the sampler threw')[:200]}", []
    v = (got.get("result") or {}).get("value")
    v = v if isinstance(v, dict) else {}
    return [s for s in v.get("samples") or [] if isinstance(s, dict)], None, notes + [str(n)[:300] for n in
                                                                                    v.get("notes") or []]


def _cmd_verify(args, path, samples):
    """fm motion verify: --samples FILE, else a frozen crawl of fm smoke's web URL; exit 1 on a drift or problem."""
    from sampo import cli
    from sampo import core
    why, notes = None, []
    if samples:
        text, err = sdesign._read(samples, strict=True)
        if text is None:
            raise ValueError(f"{samples}: {err}")
        try:
            got = json.loads(text)
        except (ValueError, RecursionError) as e:
            raise ValueError(f"{samples}: {e or 'nested too deep'}") from None
    else:
        try:
            p = cli.resolve(args, create=False)
        except cli.UsageError:
            if getattr(args, "project", None):
                raise
            p = None
        url = (core.read_meta(p).get("smoke") or {}).get("web") if p else None
        got, why, notes = sample(url, p.root) if isinstance(url, str) else \
            (None, "no fm smoke web URL to sample (fm smoke set web <url>)", [])
    res = verify(path, got, why)
    res["notes"] = notes + res["notes"]  # the sampler's own (a cap reached, an animation it couldn't read) first
    cli.out(args, res, report(res))
    return 1 if res["problems"] or res["drifts"] else 0


def _report(problems, label):
    return [f"{p['motion'] or label}: {p['problem']}" for p in problems]


def cmd_motion(args):
    import sys
    from sampo import cli
    from sampo import core
    act, path = args.action, args.path
    samples = getattr(args, "samples", None)
    for flag, v, verb in (("--to", args.to, "compile"), ("--out", args.out, "compile"), ("--samples", samples, "verify")):
        if act != verb and v is not None:
            print(f"sampo: motion {act}: {flag} is for {verb} (fm motion {verb} [PATH] {flag} …)", file=sys.stderr)
            return 2
    if not path:
        root = core.git_root(os.getcwd()) or os.getcwd()
        path = os.path.join(root, "motion.toml")
        if not os.path.lexists(path):
            print(f"sampo: motion {act}: no motion.toml at {root} (fm motion {act} PATH names another)", file=sys.stderr)
            return 2
    label = os.path.basename(path)
    try:
        if act == "verify":
            return _cmd_verify(args, path, samples)
        if act == "compile":
            to = args.to or "css"
            text, problems = compile_score(path, to)
            out = os.path.abspath(args.out) if args.out else None  # write_atomic makes dirname(path): "" if bare
            if problems:  # stdout stays for the compiled text; --json puts {problems} there, as check does
                if getattr(args, "json", False):
                    cli.out(args, {"problems": problems}, None)
                else:
                    print("\n".join(_report(problems, label) + [f"motion compile: {len(problems)} problem(s), "
                                                                f"nothing compiled ({path})"]), file=sys.stderr)
                return 1
            if out and os.path.exists(out) and os.path.samefile(out, path):
                raise ValueError(f"--out {args.out} is the score it compiles")
            if out:
                core.write_atomic(out, text)  # via a temp file: a symlink at out is replaced, never written through
                cli.out(args, {"out": args.out, "to": to, "bytes": len(text.encode())}, f"wrote {args.out}")
            else:
                cli.out(args, {"to": to, "text": text}, text.rstrip("\n"))
            return 0
        got = check(path)
    except (OSError, ValueError, RecursionError) as e:
        print(f"sampo: motion {act}: {e or 'nested too deep'}", file=sys.stderr)
        return 2
    n, m = len(got["problems"]), len(got["motions"])
    text = "\n".join(_report(got["problems"], label)
                     + [f"motion check: {n} problem(s) in {m} motion(s) ({path})" if n else
                        f"motion check: clean, {m} motion(s) ({path})"])
    cli.out(args, got, text)
    return 1 if n else 0
