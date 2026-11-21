"""Design works part 2 (T-3110, of T-3104): `sampo works grow --grid works --target N`. A grid of work type x subject x
style x technique is read thinnest first; for each cell a writer drafts N SVGs (best of N), each draft is rendered and held
to the part 1 gates (works.py), a critic who sees the render names what is wrong, the writer revises it (up to --rounds
rounds), and the drafts that pass every gate are ranked pairwise, each pair judged in both orders so a judge's lean to
the first or the second cancels. The best is kept in research/genesis/works/works.jsonl with its writer, rounds, gate
numbers and rank; a cell whose drafts all fail keeps nothing (tried.jsonl notes it, so the next run takes the thinner
cells first). Models advise, proof decides: the critic and the judge never gate, a revision replaces its draft only
when it fails no more gates, and a kept work passed every per-work gate, `unique` against the whole store (each row
keeps its thumb, and a render starts from them); the icon `set` gate compares a set, not a work, so it is not run here.
The store is append-only JSONL, one `sampo works grow` at
a time (a flock); a rerun counts the store, so it resumes. The writer, renderer, critic and judge are an injectable
`io`, so the tests ask no model and start no browser; the real ones are tool-less children (ideas.run_child, budget
checked: a budget refusal stops the run) and the part 1 headless browser. The child can't open a PNG, so the critic and
the judge get an ASCII sketch of each render beside the SVG source."""
import collections
import contextlib
import fcntl
import itertools
import json
import os
import random
import re
import tempfile
import types

from sampo import core as c
from sampo import rune, works

FEATURE, FALLBACK, TIMEOUT = "works-grow", 0.15, 400
MAX_N, MAX_ROUNDS, FAILS = 8, 5, 3  # drafts a cell, revise rounds, failed calls in a row before the run stops
BUSY_EXIT = 75  # EX_TEMPFAIL, as `moons grow`: another grow holds the store
STORE, TRIED = "works.jsonl", "tried.jsonl"
MAX_SVG, MAX_DEFECTS, DEFECT_CAP = 1 << 19, 5, 300

SUBJECTS = ("a clock", "a compass", "a lighthouse", "a moon through its phases", "a circuit trace",
            "a tree of branching tasks", "a gauge with a needle", "a telescope", "a tide chart", "a key and a lock",
            "a ledger of rows", "a sailing boat", "a constellation")
STYLE_TEXT = {"crafted": "hand-crafted: every shape placed with intent, nothing left at a default",
              "detailed": "rich in small, deliberate detail that still reads at its size",
              "lit": "lit: a clear light source, with shading and highlights that give it depth",
              "animated": "continuously animated: a loop of SMIL or CSS animation that never stops, every motion "
                          "purposeful"}
STYLES = tuple(STYLE_TEXT)
TECHNIQUES = ("fine line work", "layered flat shapes", "gradients and light")

WRITE_SYSTEM = (
    "You are an illustrator who draws one SVG at a time. Reply with the complete SVG in a ```svg fence and nothing "
    "else. The SVG has a viewBox, no script, no event handler, no foreignObject and no reference to anything outside "
    "itself; text, if any, is set in a generic font. A measured bar holds it, on a light page (ink #1b1f24) and a dark "
    "one (ink #e6e8eb): it must paint something, stay inside its viewBox (a scene may bleed), keep enough contrast on "
    "both pages, stay inside its type's size, shape and colour limits, and, when animated, loop for ever. The taste: "
    "crafted, detailed and considered, never cartoonish: no mascots, no thick outlines round blobs, no clip-art "
    "faces, no stock emoji look; restraint, real proportion, one clear idea.")
CRITIC_SYSTEM = (
    "You critique a rendered SVG for an art director. You see its source, the measured gates and an ASCII sketch of "
    "each render (denser characters are more ink). Name at most five concrete defects, one per line, each starting "
    "with '- ' and saying where and what (a shape, a colour, a coordinate), covering the gates that failed first, then "
    "composition, legibility at its size and anything cartoonish. Do not rewrite the SVG. If nothing is wrong, reply "
    "NONE.")
JUDGE_SYSTEM = (
    "You compare two SVG drawings of the same brief for an art director: which is the more crafted, detailed, "
    "legible and considered, and the less cartoonish. You see both sources and an ASCII sketch of each. Reply with one "
    "letter, A or B, and nothing else.")

SVG_START, SVG_END = re.compile(r"<svg\b", re.I), re.compile(r"</svg\s*>", re.I)
BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(\S.*)$")
PICK = re.compile(r"^[\s*\"'(\[]*([AB])(?![A-Za-z0-9])")
RAMP = " .:-=+*#%@"


class Busy(Exception):
    pass


class NoBrowser(Exception):
    pass


class Stop(Exception):
    """The run must stop; args[0] says why: budget, failed, browser."""


# ---------------------------------------------------------------- the grid and the store

FIGURE_SUBJECTS = ("a bar chart of monthly releases", "a table of benchmark results", "a line chart of latency over a week",
                   "a table of supported platforms", "a donut chart of test coverage by module",
                   "a table of command flags")


def _fits(t, s, y):
    """Whether a type takes a subject and a style: a figure is a chart or a table (still, plain), anything else draws an
    object; an icon is a still symbol, and a scene of phases is no 24 px icon."""
    if t == "figure":
        return s in FIGURE_SUBJECTS and y in ("crafted", "detailed")
    if s in FIGURE_SUBJECTS:
        return False
    return not (t == "icon" and (y == "animated" or "phases" in s))


def cells():
    """Every cell, type innermost so the first cells of a thin store span all the types."""
    return [{"type": t, "subject": s, "style": y, "technique": q}
            for s, y, q, t in itertools.product(SUBJECTS + FIGURE_SUBJECTS, STYLES, TECHNIQUES, works.TYPES)
            if _fits(t, s, y)]


def key(cell):
    return tuple(str(cell.get(k) or "") for k in ("type", "subject", "style", "technique"))


def _dir(p):
    return os.path.join(rune.root(p), "works")


def _read(path):
    """The dict lines of a JSONL file: a torn, foreign or non-object line is skipped, a missing file is empty."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    v = json.loads(line)
                except ValueError:
                    continue
                if isinstance(v, dict):
                    yield v
    except FileNotFoundError:
        return


def rows(p):
    return list(_read(os.path.join(_dir(p), STORE)))


def tried(p):
    return list(_read(os.path.join(_dir(p), TRIED)))


def counts(p):
    """{cell key: works kept in it}."""
    return collections.Counter(key(r["cell"]) for r in rows(p) if isinstance(r.get("cell"), dict))


def pick(p, k, seed=0):
    """The k thinnest cells: fewest works first, then fewest failed tries; equals keep the grid's order (a seed other
    than 0 shuffles them first)."""
    names, have = cells(), counts(p)
    failed = collections.Counter(key(r["cell"]) for r in tried(p) if isinstance(r.get("cell"), dict))
    if seed:
        random.Random(seed).shuffle(names)
    return sorted(names, key=lambda x: (have.get(key(x), 0), failed.get(key(x), 0)))[:max(0, k)]


def _last_id(path):
    ids = [int(m.group(1)) for e in c.tail_jsonl(path, 50) if isinstance(e, dict)
           for m in [re.fullmatch(r"W-(\d+)", str(e.get("id")))] if m]  # 50: a torn last line can't restart the ids
    return max(ids, default=0)


def add(p, cell, work, writer, rounds, rank, drafts, critique=()):
    """Append one kept work as a row and return it."""
    d = _dir(p)
    with c.lock(p.dir):
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, STORE)
        row = {"id": f"W-{_last_id(path) + 1:06d}", "at": c.now(), "cell": {"grid": "works", **cell},
               "type": work.get("type"), "svg": work["svg"], "viewbox": work.get("viewbox"), "thumb": work.get("thumb"),
               "writer": writer,
               "rounds": rounds, "drafts": drafts, "rank": rank, "critique": list(critique),
               "gates": [{k: g.get(k) for k in ("gate", "ok", "got", "bar")} for g in work.get("gates", [])]}
        c.append_line(path, json.dumps(row, ensure_ascii=False))
    return row


def note_tried(p, cell, why):
    with c.lock(p.dir):
        os.makedirs(_dir(p), exist_ok=True)
        c.append_line(os.path.join(_dir(p), TRIED), json.dumps(
            {"at": c.now(), "cell": {k: cell.get(k) for k in ("type", "subject", "style", "technique")},
             "why": works._say(why, 300)}, ensure_ascii=False))


@contextlib.contextmanager
def grow_lock(p):
    """One `works grow` at a time on a store (two would spend the budget on the same cells): a non-blocking flock on
    .grow.lock with the holder's pid inside; the kernel drops it with the process."""
    os.makedirs(_dir(p), exist_ok=True)
    fd = os.open(os.path.join(_dir(p), ".grow.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = os.pread(fd, 20, 0).decode("ascii", "replace").strip()
            raise Busy(f"another works grow is running on this store (pid {pid if pid.isdigit() else '?'}); "
                       "a second one would spend the same budget twice") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        yield
    finally:
        os.close(fd)


# ---------------------------------------------------------------- reading what a model said

def extract_svg(text):
    """The first <svg ...> to the last </svg> of a reply (its prose and fence dropped); None when there is none, or it
    is cut off, or it is too long to be a drawing."""
    text = text or ""
    start, ends = SVG_START.search(text), list(SVG_END.finditer(text))
    if not start or not ends or ends[-1].end() <= start.start():
        return None
    svg = text[start.start():ends[-1].end()]
    return svg if len(svg) <= MAX_SVG else None


def parse_defects(text):
    """The critic's few concrete lines: bullets, at most MAX_DEFECTS of DEFECT_CAP characters each; NONE is no advice."""
    text = (text or "").strip()
    if text.strip(" .*_`").upper() == "NONE":
        return []
    found = [m.group(1) for line in text.splitlines() for m in [BULLET.match(line)] if m]
    if not found and text:
        found = [text.splitlines()[0]]  # a reply with no bullets: its first line is the advice
    return [d for d in (works._say(" ".join(x.split()), DEFECT_CAP) for x in found[:MAX_DEFECTS]) if d]


def parse_pick(text):
    """'A' or 'B' from a judge's reply; anything else is a tie (None)."""
    m = PICK.match(text or "")
    return m.group(1) if m else None


def sketch(path, cols=40):
    """An ASCII sketch of a PNG render: denser characters where it differs more from its background (its commonest
    colour); '' when the file can't be read. The child is tool-less, so this is how it sees."""
    from sampo import img
    try:
        im = img.load(path)
    except (OSError, ValueError):
        return ""
    w, h = im.width, im.height
    if not w or not h:
        return ""
    cols = min(cols, w)
    rws = max(1, min(cols, round(cols * h / w / 2)))
    grey = lambda i: (299 * im.rgba[i] + 587 * im.rgba[i + 1] + 114 * im.rgba[i + 2]) // 1000  # noqa: E731
    cell = []
    for r in range(rws):
        for q in range(cols):
            xs = [min(w - 1, (q * w + (k + 1) * w // 4) // cols) for k in range(3)]
            ys = [min(h - 1, (r * h + (k + 1) * h // 4) // rws) for k in range(3)]
            cell.append(sum(grey((y * w + x) * 4) for x in xs for y in ys) / 9)
    bg = collections.Counter(round(v) for v in cell).most_common(1)[0][0]
    ink = [abs(v - bg) for v in cell]
    top = max(ink) or 1
    return "\n".join("".join(RAMP[min(len(RAMP) - 1, int(ink[r * cols + q] / top * (len(RAMP) - 1) + 0.5))]
                             for q in range(cols)).rstrip() for r in range(rws))


# ---------------------------------------------------------------- prompts

def brief(cell):
    cap, (lo, hi), colours, px = works.TYPES[cell["type"]]
    rule = (f"A {cell['type']}: at most {cap} bytes, {lo} to {hi} shapes, at most {colours} colours, shown about {px} px "
            "wide.")
    if cell["type"] == "icon":
        rule += " Use a square whole-number viewBox (0 0 24 24), one stroke width and currentColor strokes."
    elif cell["type"] == "figure":
        rule += " A figure is a README table or chart: real-looking labelled data, legible text."
    return (f"Draw {cell['subject']}, {STYLE_TEXT[cell['style']]}, in {cell['technique']}.\n{rule}")


def draft_prompt(cell, i, n):
    return brief(cell) + (f"\n\nThis is draft {i + 1} of {n}: make its composition clearly its own." if n > 1 else "")


def _gate_lines(w, only_failed=False):
    return [f"- {'ok' if g['ok'] else 'not run' if g['ok'] is None else 'FAILED'} {g['gate']}: {g['got']} ({g['bar']})"
            for g in w.get("gates", []) if not only_failed or g.get("ok") is False]


def critic_prompt(cell, w):
    return (f"{brief(cell)}\n\nThe gates measured on its renders:\n" + "\n".join(_gate_lines(w)) +
            f"\n\nThe SVG source:\n{w['svg']}")


def revise_prompt(cell, w, defects):
    failed = _gate_lines(w, only_failed=True)
    return ("Revise this SVG.\n" + brief(cell) +
            ("\n\nGates it failed (fix these first; a number past its bar fails the work):\n" + "\n".join(failed)
             if failed else "") +
            ("\n\nThe critic's notes (advice only: the gates outrank them):\n" + "\n".join(f"- {d}" for d in defects)
             if defects else "") +
            f"\n\nThe current SVG:\n{w['svg']}\n\nReply with the complete revised SVG in a ```svg fence only.")


def judge_prompt(cell, a, b):
    return (f"Brief: {brief(cell)}\n\nDrawing A:\n{a['svg']}\n\nDrawing B:\n{b['svg']}\n\nWhich is better? Reply A or B.")


# ---------------------------------------------------------------- the loop

def fails(w):
    return [g for g in w.get("gates", []) if g.get("ok") is False]


def passed(w):
    return bool(w.get("svg")) and bool(w.get("rendered")) and not fails(w)


def bad(w):
    """How far a work is from passing: the gates it fails; a work with no render is worse than any."""
    return len(fails(w)) if w.get("svg") and w.get("rendered") else 99


def calls_needed(n, rounds):
    """The most model calls one cell takes: n drafts, a critique and a revision per draft per round, and each pair of
    survivors judged in both orders."""
    return n + 2 * n * rounds + n * (n - 1)


def estimate(n, rounds, cells_to_do):
    """What a run may ask, by role: {per_cell, writer, critic, judge, cells, calls, usd}; usd is rough (the budget's
    per-call fallback for the drawing model, a third of it for the critic and the judge)."""
    w, cr, j = n + n * rounds, n * rounds, n * (n - 1)
    return {"per_cell": w + cr + j, "writer": w, "critic": cr, "judge": j, "cells": cells_to_do,
            "calls": cells_to_do * (w + cr + j), "usd": round(cells_to_do * (w + (cr + j) / 3) * FALLBACK, 2)}


def stored_thumbs(p):
    """[(row id, thumb bytes)] of the kept works that have one: what a new render's `unique` gate compares with."""
    out = []
    for r in rows(p):
        try:
            out.append((str(r.get("id")), bytes.fromhex(r["thumb"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class Run:
    """One grow's calls, counted, with the rules that stop it: a budget refusal, or FAILS failed calls in a row."""

    def __init__(self, io, max_calls, prior=None):
        self.io, self.max_calls, self.calls, self.streak = io, max_calls, 0, 0
        self.prior = prior  # a callable → the store's thumbs; read afresh, a cell keeps works the next one is judged by

    def ask(self, fn, *args):
        """fn(*args) → its reply, or None when the call failed (the caller goes on without it)."""
        self.calls += 1
        try:
            reply = fn(*args)
        except (ValueError, OSError, RuntimeError) as e:
            if str(e).startswith("budget"):
                raise Stop("budget") from None
            self.streak += 1
            if self.streak >= FAILS:
                raise Stop("failed") from None
            return None
        self.streak = 0
        return reply

    def render(self, items, out):
        try:
            self.io.render(items, out, self.prior() if self.prior else [])
        except NoBrowser:
            raise Stop("browser") from None


def pictures(out, w):
    stem = os.path.splitext(os.path.basename(w["path"]))[0]
    return [p for p in (os.path.join(out, f"{stem}-{t}.png") for t in works.THEMES) if os.path.isfile(p)]


def make(cell, svg, path):
    """A work dict from a reply's SVG: inspected (cleaned, the static gates run); one that crashes the inspection fails."""
    try:
        w = works.inspect(svg.encode("utf-8"), cell["type"])
    except Exception as e:  # untrusted text: one draft's crash costs that draft only
        w = {"type": cell["type"], "svg": None, "gates": [{"gate": "xml", "ok": False, "bar": works.XML_BAR,
                                                           "got": works._say(f"not checked: {type(e).__name__}", 200)}]}
    w["path"] = path
    return w


def revise(run, cell, d, rounds, out):
    """Up to `rounds` rounds of critique and revision on one draft; the work is replaced only when the revision fails no
    more gates (the critic advises, the gates decide)."""
    for r in range(rounds):
        w = d["w"]
        defects = parse_defects(run.ask(run.io.critic, critic_prompt(cell, w), pictures(out, w)) or "NONE")
        if passed(w) and not defects:
            return
        text = run.ask(run.io.write, revise_prompt(cell, w, defects))
        if text is None:
            return
        svg = extract_svg(text)
        if svg is None:
            continue
        new = make(cell, svg, f"{os.path.splitext(w['path'])[0]}r{r + 1}.svg")
        run.render([new], out)
        if bad(new) <= bad(w):
            d["w"], d["rounds"] = new, d["rounds"] + 1
            d["notes"] += defects


def rank(run, cell, survivors, out):
    """The survivors ranked by round robin, each pair in both orders: (the best, its rank). A reply that is not A or B,
    or a call that failed, is half a point to each."""
    if len(survivors) == 1:
        return survivors[0], {"place": 1, "of": 1, "points": 0}
    pts = [0.0] * len(survivors)
    for i, j in itertools.combinations(range(len(survivors)), 2):
        for a, b in ((i, j), (j, i)):
            wa, wb = survivors[a]["w"], survivors[b]["w"]
            pics = pictures(out, wa)[:1] + pictures(out, wb)[:1]
            pick = parse_pick(run.ask(run.io.judge, judge_prompt(cell, wa, wb), pics))
            for who, win in ((a, "A"), (b, "B")):
                pts[who] += 1.0 if pick == win else 0.5 if pick is None else 0.0
    best = max(range(len(pts)), key=lambda i: (pts[i], -i))  # a tie goes to the earlier draft
    top = pts[best]
    return survivors[best], {"place": 1, "of": len(survivors), "points": int(top) if top == int(top) else top}


def attempt(p, run, cell, n, rounds):
    """(the kept row or None, why none, replies seen) for one cell. Stop is raised when the run must end; on a budget
    stop the drafts that already passed every gate are not thrown away: the first is kept, unranked."""
    ds, replies, reasons = [], [0], []
    with tempfile.TemporaryDirectory(prefix="works-grow-") as out:
        try:
            best, ranked = _work_cell(run, cell, n, rounds, out, ds, replies, reasons)
        except Stop as s:
            if s.args[0] == "budget":
                with contextlib.suppress(Stop):
                    run.render([d["w"] for d in ds if d["w"]["svg"] and not d["w"].get("rendered")], out)
                live = [d for d in ds if passed(d["w"])]
                if live:
                    add(p, cell, live[0]["w"], getattr(run.io, "writer", "?"), live[0]["rounds"],
                        {"place": 1, "of": len(live), "points": 0, "unranked": True}, n, live[0]["notes"])
            raise
    if best is None:
        return None, "; ".join(dict.fromkeys(reasons)), replies[0]
    row = add(p, cell, best["w"], getattr(run.io, "writer", "?"), best["rounds"], ranked, n, best["notes"])
    return row, "", replies[0]


def _work_cell(run, cell, n, rounds, out, ds, replies, reasons):
    """Draft, render, revise and rank one cell: (the best draft, its rank), or (None, None) when none passed."""
    for i in range(n):
        text = run.ask(run.io.write, draft_prompt(cell, i, n))
        if text is None:
            continue
        replies[0] += 1
        svg = extract_svg(text)
        if svg is None:
            reasons.append("a reply with no complete SVG")
            continue
        ds.append({"w": make(cell, svg, f"d{i + 1}.svg"), "rounds": 0, "notes": []})
    run.render([d["w"] for d in ds if d["w"]["svg"]], out)
    for d in ds:
        if d["w"]["svg"] and rounds:
            revise(run, cell, d, rounds, out)
    live = [d for d in ds if passed(d["w"])]
    if not live:
        for d in ds:
            names = [g["gate"] for g in fails(d["w"])] or ["not rendered"]
            reasons.append("failed " + ", ".join(names))
        return None, None
    return rank(run, cell, live, out)


def grow(p, target, n=3, rounds=1, max_calls=None, max_cells=None, seed=0, grid="works", writer="opus", io=None,
         critic="sonnet", judge="sonnet"):
    """Fill the thinnest cells until the store holds `target` works, the cells or the calls run out, or the budget stops
    it. {added, kept, empty, cells, calls, stopped, target}; stopped is target, cells, max-calls, budget, failed,
    browser or busy (then `error` says why). ValueError for an argument out of range, before any call."""
    if grid != "works":
        raise ValueError(f"--grid {grid!r}: works is the only grid")
    for name, v, lo, hi in (("batch", n, 1, MAX_N), ("rounds", rounds, 0, MAX_ROUNDS), ("target", target, 1, 10 ** 6),
                            ("max-calls", max_calls, 1, 10 ** 6), ("max-cells", max_cells, 1, 10 ** 6)):
        if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi):
            raise ValueError(f"--{name} {v!r}: a whole number from {lo} to {hi}")
    r = {"added": 0, "kept": 0, "empty": 0, "cells": 0, "calls": 0, "stopped": None, "target": target}
    try:
        io = io or default_io(p, writer, critic, judge)
        with grow_lock(p):
            run, have = Run(io, max_calls, lambda: stored_thumbs(p)), len(rows(p))
            start = have
            try:
                for cell in pick(p, len(cells()), seed):
                    if have >= target:
                        break
                    if max_cells is not None and r["cells"] >= max_cells:
                        r["stopped"] = "cells"
                        break
                    if max_calls is not None and run.calls + calls_needed(n, rounds) > max_calls:
                        r["stopped"] = "max-calls"
                        break
                    r["cells"] += 1
                    row, why, replies = attempt(p, run, cell, n, rounds)
                    if row:
                        r["added"] += 1
                        have += 1
                    elif replies:
                        r["empty"] += 1
                        note_tried(p, cell, why)
                else:
                    r["stopped"] = "cells"
                r["stopped"] = r["stopped"] or "target"
            except Stop as s:
                r["stopped"] = s.args[0]
            r["calls"] = run.calls
            r["kept"] = len(rows(p))
            r["added"] = r["kept"] - start  # a budget stop may keep a cell's finished draft
    except Busy as e:
        r.update(stopped="busy", error=str(e), kept=len(rows(p)))
    except NoBrowser as e:
        r.update(stopped="browser", error=works._say(str(e), 400), kept=len(rows(p)))
    return r


def default_io(p, writer, critic="sonnet", judge="sonnet"):
    """The real hooks: tool-less children for the writer, critic and judge (each its own model), the part 1 browser for
    the render."""
    from sampo import ideas, shot
    browser, why = shot.find_browser("sampo works grow")
    if not browser:
        raise NoBrowser(why)

    def ask(system, prompt, detail):
        model = {"write": writer, "critic": critic, "judge": judge}[detail]
        return ideas.run_child(FEATURE, system, prompt, model, TIMEOUT, project=p.slug, detail=detail,
                               fallback=FALLBACK)

    def render(items, out, prior=()):
        os.makedirs(out, exist_ok=True)
        with shot.killable():
            works._render_all(browser, items, out, prior)

    def seen(pics):
        shown = [(os.path.basename(x), sketch(x)) for x in pics]
        return "".join(f"\n\nASCII sketch of {name}:\n{s}" for name, s in shown if s)

    return types.SimpleNamespace(
        writer=writer, render=render,
        write=lambda prompt: ask(WRITE_SYSTEM, prompt, "write"),
        critic=lambda prompt, pics: ask(CRITIC_SYSTEM, prompt + seen(pics), "critic"),
        judge=lambda prompt, pics: ask(JUDGE_SYSTEM, prompt + seen(pics), "judge"))


def line(r):
    head = (f"works grow: added {r['added']}, {r['kept']} of {r['target']} kept; {r['cells']} cells tried, "
            f"{r['empty']} kept nothing, {r['calls']} model calls: stopped on {r['stopped']}")
    return head + (f": {r['error']}" if r.get("error") else "")


def cmd_grow(args, p, io=None):
    """`sampo works grow`: run the loop and say what it kept; the exit code: 0, 75 busy, 1 no browser or a run the model
    calls kept failing."""
    import sys
    from sampo import cli
    try:
        if all(isinstance(v, int) for v in (args.target, args.batch, args.rounds)):
            todo = max(0, min(args.target - len(rows(p)), len(cells())))
            e = estimate(args.batch, args.rounds, todo)
            if args.max_calls:
                e["calls"] = min(e["calls"], args.max_calls)
            print(f"works grow: up to {e['calls']} model calls ({e['per_cell']} a cell over up to {todo} cells: "
                  f"{e['writer']} drawing, {e['critic']} critic, {e['judge']} judge), about ${e['usd']} at most",
                  file=sys.stderr)
        r = grow(p, target=args.target, n=args.batch, rounds=args.rounds, max_calls=args.max_calls, seed=args.seed,
                 grid=args.grid, writer=args.writer, io=io,
                 critic=getattr(args, "critic", "sonnet"), judge=getattr(args, "judge", "sonnet"))
    except ValueError as e:
        raise cli.UsageError(str(e)) from None
    cli.out(args, r, line(r))
    return BUSY_EXIT if r["stopped"] == "busy" else 1 if r["stopped"] in ("browser", "failed") else 0
