"""T-3699: the Board in Sampo Code: the live page's nine panels (plugin/web/live/board.js) as terminal pictures in its
chrome (corner brackets on a hairline, a numbered chip, a label, a caption and its stat), on a grid of 3, 2 or 1
columns, the most relevant first, scrolled when they don't all fit; over them the Board's framed header.

Each picture is painted once per change of what it shows and kept; only the comet on a running board moves. The
project's live snapshot (sampo.live) is read on a thread every Feed.REFRESH seconds, never on the drawing path."""
import math
import os
import random
import re
import threading
import time

from sampo import smithtui as T

HOME = re.compile(r"/(?:home|Users)/[^/\s\"']+")
TASK = re.compile(r"\bT-\d+\b")


def s(x, n=60):
    """x as one line of at most n cells, a home path cut to ~ (the Board's str)."""
    return T.fit(HOME.sub("~", " ".join(str("" if x is None else x).split())), n)


def _num(x, d=0.0):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else d


def _int(x):
    return max(0, int(round(_num(x))))


def _d(x):
    return x if isinstance(x, dict) else {}


def _l(x):
    return x if isinstance(x, list) else []


def _cols(w, n):
    return max(1, min(n, 3 if w >= 180 else 2 if w >= 110 else 1))


META = {"lanes": ("LANES", "builders and the files they hold", "cyan"),
        "train": ("MERGE TRAIN", "finished lanes into one build", "yellow"),
        "cpu": ("CPU", "the machine by kind of work", "green"),
        "loop": ("THE LOOP", "your words into a brief", "pink"),
        "library": ("LIBRARY", "cards and moons kept", "violet"),
        "models": ("MODELS", "who calls which model", "orange"),
        "sampo": ("SAMPO", "commands and the queue", "cyan"),
        "state": ("STATE", "the files Sampo keeps", "yellow"),
        "tests": ("TESTS", "every module, cached or run", "green")}
KEYS = list(META)
LANE_COL = {"building": "cyan", "waiting": "yellow", "parked": "violet", "merged": "green"}
KINDS = ("tests", "claude", "models", "hooks", "other")
KIND_COL = dict(zip(KINDS, ("yellow", "pink", "cyan", "violet", "dim")))
TEST_STATES = ("cached", "running", "pass", "fail", "idle")
GROUPS = ("claude", "jeff", "ollama")
GROUP_COL = {"claude": "orange", "jeff": "violet", "ollama": "cyan"}
LIB = (("cards", "violet"), ("ui", "cyan"), ("moons", "pink"), ("calling", "yellow"))
STAGE = {"planning": "planning", "running": "executing", "fixing": "verifying", "closed": "closed"}
TRACK = {"planning": 0, "ready": 0, "executing": 1, "verifying": 2, "auditing": 2, "closing": 3, "closed": 4}


# ---------------------------------------------------------------- the model: what each picture shows

def _task(st):
    t = TASK.search(str(st.task or ""))
    return t.group(0) if t else s(st.task, 12) if st.task else ""


def m_lanes(st, snap, b, m):
    src = []
    if st.request or st.files:  # Sampo Code's own run is a lane: the files it changed
        paths = list(dict.fromkeys([p for v in st.files.values() for p in _l(v)] + list(st.stat)))
        hot = max(st.stat, key=lambda p: sum(_num(v) for v in _l(st.stat[p])), default=None)
        src.append({"task": _task(st) or "this run", "title": _d(st.plan).get("title") or st.request,
                    "state": "building" if st.status in T.LIVE else "merged" if st.status == "closed" else "parked",
                    "steps": {"done": sum(x.get("state") == "done" for x in st.steps), "total": len(st.steps)},
                    "files": [{"path": p, "hot": p == hot} for p in paths]})
    if isinstance(b.get("lanes"), list):
        src += b["lanes"]
    else:
        src += [{"task": _d(x).get("task"), "title": _d(x).get("title"), "steps": _d(x).get("steps"),
                 "state": "waiting" if _d(x).get("waiting") else "building"} for x in _l(snap.get("lanes"))]
    clusters = []
    for x in src[:16]:
        x = _d(x)
        files, steps, nodes = _l(x.get("files")), _d(x.get("steps")), []
        for f in files[:24]:
            p = s(_d(f).get("path"), 160)
            i = p.rfind("/")
            nodes.append({"p": s(p[i + 1:], 32), "d": s(p[:i + 1], 48), "hot": bool(_d(f).get("hot"))})
        clusters.append({"task": s(x.get("task"), 12), "title": s(x.get("title"), 80),
                         "state": x.get("state") if x.get("state") in LANE_COL else "building",
                         "steps": {"done": _int(steps.get("done")), "total": _int(steps.get("total"))},
                         "nodes": nodes, "more": _int(x.get("more")) + max(0, len(files) - 24)})
    return {"clusters": clusters, "more": max(0, len(src) - len(clusters))}


def m_train(st, snap, b, m):
    t, rank = _d(b.get("train")), {"waiting": 0, "building": 1}
    lanes = m.get("lanes") or {"clusters": []}
    waiting = sorted((c for c in lanes["clusters"] if c["state"] in rank), key=lambda c: rank[c["state"]])[:6]
    return {"known": bool(b.get("train")),
            "waiting": [{"task": c["task"], "title": c["title"], "state": c["state"],
                         "files": len(c["nodes"]) + c["more"]} for c in waiting],
            "landed": [{"task": s(_d(x).get("task"), 12), "title": s(_d(x).get("title"), 64),
                        "at": s(_d(x).get("at"), 32)} for x in _l(t.get("landed"))[-8:]],
            "landed24": _int(t.get("landed_24h")), "trains24": _int(t.get("trains_24h")),
            "left": _int(t.get("left_out")), "running": bool(t.get("running")),
            "mpl": None if t.get("minutes_per_lane") is None else _int(t.get("minutes_per_lane"))}


def m_cpu(st, snap, b, m):
    c = _d(b.get("cpu"))
    now, r2 = _d(c.get("now")), lambda x: round(max(0.0, _num(x)), 2)
    rows = [_l(r) + [0] * 6 for r in _l(c.get("minutes"))[-30:]]
    # a cpu block with no reading yet (now null, no minutes) is unknown: its panel waits, never shows a made-up 0.0
    return {"known": bool(now or rows), "goal": _num(c.get("goal_pct"), 10) or 10,
            "cores": max(1, _int(c.get("cores"))), "minutes": [[_int(r[0])] + [r2(v) for v in r[1:6]] for r in rows],
            "now": dict({"busy": r2(now.get("busy"))}, **{k: r2(now.get(k)) for k in KINDS})}


def m_loop(st, snap, b, m):
    if st.request:  # Sampo Code's own run: the request, its brief, its checks
        closed = str(st.closed or "")
        sha = re.search(r"committed ([0-9a-f]{7,40})", closed) or re.search(r"\b([0-9a-f]{7,40})\b", closed)
        return {"id": _task(st) or "this run", "words": s(st.request, 240),
                "title": s(_d(st.plan).get("title") or st.request, 120),
                "criteria": [{"text": s(c.get("text"), 90), "ok": c.get("state") == "pass",
                              "bad": c.get("state") == "fail"} for c in st.criteria[:6]],
                "steps": [{"text": s(x.get("text"), 70), "done": x.get("state") == "done" or st.status == "closed",
                           "current": x.get("state") == "now"} for x in st.steps[:9]],
                "choice": None, "stage": STAGE.get(st.status, st.status), "sha": sha.group(1)[:7] if sha else ""}
    focus = _d(snap.get("focus"))
    if isinstance(b.get("loop"), dict):
        x, ch = b["loop"], b["loop"].get("choice")
        return {"id": s(x.get("id"), 12), "words": s(x.get("words"), 240), "title": s(x.get("title"), 120),
                "criteria": [{"text": s(_d(c).get("text"), 90), "ok": bool(_d(c).get("ok")), "bad": False}
                             for c in _l(x.get("criteria"))[:6]],
                "steps": [{"text": s(_d(c).get("text"), 70), "done": bool(_d(c).get("done")),
                           "current": bool(_d(c).get("current"))} for c in _l(x.get("steps"))[:9]],
                "choice": min(100, _int(ch)) if _num(ch, None) is not None else None,
                "stage": s(x.get("stage") or focus.get("stage"), 16), "sha": ""}
    if focus.get("id"):
        done, total = ([int(p) if p.strip().isdigit() else 0 for p in str(focus.get("step") or "").split("/")]
                       + [0, 0])[:2]
        return {"id": s(focus["id"], 12), "words": s(focus.get("title"), 240), "title": s(focus.get("title"), 120),
                "criteria": [], "steps": [{"text": f"step {i + 1}", "done": i < done - 1, "current": i == done - 1}
                                          for i in range(min(9, total))],
                "choice": None, "stage": s(focus.get("stage"), 16), "sha": ""}
    return None


def m_library(st, snap, b, m):
    stats, lib = _d(snap.get("stats")), _d(b.get("library"))
    raw = {"cards": _d(stats.get("cards")).get("general"), "ui": _d(stats.get("cards")).get("ui"),
           "moons": _d(stats.get("moons")).get("candidates"), "calling": lib.get("calling")}
    counts = {k: _int(raw[k]) for k, _ in LIB}
    hourly = [_int(x) for x in _l(lib.get("hourly"))[-24:]]
    return {"known": bool(lib or stats), "counts": counts, "total": sum(counts.values()),
            "hourly": [0] * (24 - len(hourly)) + hourly}


def _agent(x):
    """Who called: its first two words, a namespaced one by its last name (subagent:sampo:fm-builder: fm-builder)."""
    words = re.split(r"\s--?", str("" if x is None else x))[0].split(" ")
    return s(re.sub(r"^\S*:", "", " ".join(words[:2])), 20)  # not a list: a caller's name is its first two words


def _group(family, name):
    f, n = str(family or "").lower(), str(name or "").lower()
    if f == "claude" or re.match(r"(claude|opus|sonnet|haiku)", n):
        return "claude"
    return "jeff" if f == "jeff" or n.startswith("jeff") else "ollama"


def m_models(st, snap, b, m):
    models = [_d(x) for x in _l(snap.get("models"))]
    by, links = {str(x.get("name")): x for x in models}, {}

    def add(a, to):
        if a and to:
            links[(a, to)] = links.get((a, to), 0) + 1
    for f in _l(snap.get("flows"))[:600]:
        add(_agent(_d(f).get("from")), s(_d(f).get("to"), 32))
    for j in _l(snap.get("jobs"))[:60]:
        if _d(j).get("model"):
            add(_agent(j.get("name")), s(j["model"], 32))
    if st.model or st.worker:  # Sampo Code's own run calls its model
        add("sampo code", s(st.model or st.worker, 32))
    ranked = sorted(links.items(), key=lambda kv: (-kv[1], kv[0]))
    agents = list(dict.fromkeys(a for (a, _), _ in ranked))[:8]
    ranked = [x for x in ranked if x[0][0] in agents]
    ms = sorted(({"name": n, "group": _group(_d(by.get(n)).get("family"), n), "calls": _int(_d(by.get(n)).get("calls_5m")),
                  "loaded": _d(by.get(n)).get("loaded") is not False}
                 for n in list(dict.fromkeys(to for (_, to), _ in ranked))[:10]),
                key=lambda x: (GROUPS.index(x["group"]), -x["calls"]))
    at, g = {x["name"]: i for i, x in enumerate(ms)}, _d(snap.get("gpu"))
    return {"agents": agents, "models": ms,
            "links": [{"a": agents.index(a), "m": at[to], "n": n} for (a, to), n in ranked if to in at][:24],
            "gpu": {"name": s(g.get("name"), 40), "used": _int(g.get("used_mb")), "total": _int(g.get("total_mb")),
                    "util": min(100, _int(g.get("util_pct")))} if g else None,
            "cpm": round(sum(_int(x.get("calls_5m")) for x in models) / 5),
            "loaded": sum(bool(x.get("loaded")) for x in models)}


def m_sampo(st, snap, b, m):
    cmds = [{"cmd": s(_d(x).get("cmd"), 96), "ok": _d(x).get("ok") is not False} for x in _l(b.get("commands"))[-12:]]
    cmds = cmds or [{"cmd": s(_d(v).get("cmd") or _d(v).get("label"), 96), "ok": not _d(v).get("exit")}
                    for v in st.verify[-12:]]
    q = _l(snap.get("queue"))
    queue = [{"id": s(_d(x).get("id"), 12), "title": s(_d(x).get("title"), 64), "tier": s(_d(x).get("tier"), 2),
              "type": s(_d(x).get("type"), 10)} for x in q[:8]]
    if not queue and st.task:  # no snapshot yet: the run's own task
        p = _d(st.plan)
        queue = [{"id": _task(st), "title": s(p.get("title") or st.request, 64), "tier": s(p.get("tier"), 2),
                  "type": s(p.get("type"), 10)}]
    deps = [[s(x, 12) for x in _l(d)[:4]] for d in _l(b.get("deps"))[:40]]
    return {"commands": cmds, "queue": queue, "queued": len(q) or len(queue), "deps": [d for d in deps if len(d) > 1]}


def m_state(st, snap, b, m):
    x = _d(b.get("state"))
    doc = x.get("doc")
    return {"known": bool(x),
            "tree": [{"name": s(_d(e).get("name"), 32), "dir": bool(_d(e).get("dir")), "n": _int(_d(e).get("n")),
                      "kb": _int(_d(e).get("kb"))} for e in _l(x.get("tree"))[:12]],
            "newest": s(x.get("newest"), 32),
            "doc": {k: s(doc.get(k), n) for k, n in (("kind", 12), ("title", 200), ("text", 240), ("at", 32))}
            if isinstance(doc, dict) else None}


def m_tests(st, snap, b, m):
    t = _d(b.get("tests"))
    mods, count, cells = _l(t.get("modules")), dict.fromkeys(TEST_STATES, 0), []
    if mods:
        for x in mods[:5000]:
            state = _d(x).get("state") if _d(x).get("state") in TEST_STATES else "idle"
            count[state] += 1
            if len(cells) < 400:
                cells.append({"n": s(re.sub(r"^test_", "", str(_d(x).get("name") or "")), 28), "s": state})
        return {"known": True, "count": count, "cells": cells, "more": max(0, len(mods) - 400),
                "done": _int(t.get("done")), "total": _int(t.get("total")), "running": _int(t.get("running")),
                "source": s(t.get("source"), 24)}
    for c in st.criteria[:400]:  # no snapshot yet: the run's own checks
        cells.append({"n": s(c.get("text"), 28), "s": {"pass": "pass", "fail": "fail"}.get(c.get("state"), "idle")})
        count[cells[-1]["s"]] += 1
    return {"known": bool(cells), "count": count, "cells": cells, "more": 0, "done": count["pass"],
            "total": len(cells), "running": 0, "source": "this run's checks" if cells else ""}


BUILD = {k: globals()[f"m_{k}"] for k in KEYS}


def _score(k, st, m):
    """0: what needs eyes now (the run, a failing module), 1: what is going on, 2: the rest."""
    d = m.get(k)
    if k == "loop":
        return 0 if st.request else 1 if d else 2
    if k == "tests":
        return 0 if d and d["count"]["fail"] else 1 if d and (d["count"]["running"] or d["running"]) else 2
    if k == "train":
        return 1 if d and d["running"] else 2
    if k == "lanes":
        return 1 if d and any(c["state"] == "building" for c in d["clusters"]) else 2
    return 2


def _order(st, m, keys):
    return sorted(keys, key=lambda k: (_score(k, st, m), keys.index(k)))


def model(st, app=None):
    """Each picture's data from the run (st) and the project's snapshot, kept until either changes."""
    feed = getattr(app, "board_feed", None)
    key = (id(feed), getattr(feed, "rev", 0), id(st), st.rev, st.status)
    hit = getattr(app, "board_model", None)
    if hit and hit[0] == key:
        return hit[1]
    snap = _d(getattr(feed, "snap", None))
    b, m = _d(snap.get("board")), {}
    for k in KEYS:
        try:
            m[k] = BUILD[k](st, snap, b, m)
        except Exception:  # one odd field leaves its picture empty, never the screen
            m[k] = None
    m["sig"] = {k: repr(m[k]) for k in KEYS}
    if app is not None:
        app.board_model = (key, m)
    return m


class Feed:
    """The project's live snapshot, read on a thread: never on the drawing path (a cold read is seconds of CPU)."""
    REFRESH = 30.0  # seconds between reads while a run is live
    IDLE = 300.0  # and while nothing is: an idle screen costs a read each five minutes, not each 30 s
    SLOW = 600.0  # the stats (every card and moon file read) at most this often: they cost the most

    def __init__(self, root=None, snap=None):
        self.root, self.snap, self.rev, self.at, self.busy = root, snap, 1 if snap else 0, -math.inf, False
        self.slow_at = -math.inf

    @classmethod
    def fixed(cls, snap):
        return cls(None, snap)

    def want(self, now, live=True):
        if self.busy or not self.root or now - self.at < (self.REFRESH if live else self.IDLE):
            return
        self.at, self.busy = now, True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            from sampo import core, live
            p = core.find_project(self.root, create=False)
            if p is None:
                return
            old, now = _d(self.snap), time.monotonic()
            slow, snap = now - self.slow_at >= self.SLOW or "stats" not in old, {}
            for name, fn in live.COLLECTORS.items():
                if name == "stats" and not slow:
                    snap[name] = old["stats"]
                    continue
                try:
                    snap[name] = fn(p)
                except Exception:  # as live.snapshot: a collector that raises leaves its field empty
                    snap[name] = live.EMPTY.get(name, [])
            self.slow_at = now if slow else self.slow_at
            if snap != self.snap:  # the same content again is no news: no new rev, so no redraw
                self.snap, self.rev = snap, self.rev + 1
        except Exception:
            pass
        finally:
            self.busy = False


def fresh(app):
    """True once per new snapshot (the loop redraws); asks for the next read while the Board is on screen."""
    feed = getattr(app, "board_feed", None)
    if feed is None:
        return False
    if getattr(app, "board", False) and getattr(app, "mode", "main") == "main":
        st = getattr(app, "state", None)
        feed.want(time.monotonic(), getattr(app, "running", lambda: False)() or getattr(st, "status", "") in T.LIVE)
    if feed.rev != getattr(app, "board_rev", 0):
        app.board_rev = feed.rev
        return True
    return False


# ---------------------------------------------------------------- the grid and the chrome

def draw(cv, st, app, cfg, grid, y0, body_h, w):
    """The pictures on the grid from row y0, body_h rows: whole rows, the scroll's; the order drawn."""
    m = model(st, app)
    keys = list(dict.fromkeys(k for k in grid if k in PAINT)) or KEYS
    order = _order(st, m, keys)
    n = len(order)
    cols = _cols(w, n)
    rows = -(-n // cols)
    vis = min(rows, rows if rows * 16 + rows - 1 <= body_h else max(1, body_h // 17))
    note_y = y0 + body_h - 1 if vis < rows else None  # a scrolled grid says so on its own row, never on a frame
    ph = (body_h - (vis - 1) - (vis < rows)) // vis
    top = max(0, min(getattr(app, "scroll", 0) or 0, rows - vis))
    cache = {}
    if app is not None:
        app.scroll = top
        look = (id(cfg), id(cfg.get("theme")), cfg.get("preset"))
        if getattr(app, "board_look", None) != look:
            app.board_cache, app.board_look = {}, look
        cache = app.board_cache
    fx = T.current()
    moving = app is not None and fx.moving and st.status in T.LIVE
    for r in range(top, top + vis):
        row = order[r * cols:(r + 1) * cols]
        pw, y = (w - (len(row) - 1)) // len(row), y0 + (r - top) * (ph + 1)  # a short last row: its panels widen
        for i, k in enumerate(row):
            x = i * (pw + 1)
            ww = w - x if i == len(row) - 1 else pw
            sig = (m["sig"][k], ww, ph, order.index(k) + 1)
            hit = cache.get(k)
            if not hit or hit[0] != sig:
                hit = cache[k] = (sig, panel(k, m[k], cfg, ww, ph, order.index(k) + 1))
            pc = hit[1]
            for rr in range(pc.h):
                if 0 <= y + rr < cv.h:
                    cv.cells[y + rr][x:x + pc.w] = pc.cells[rr]
                    cv.bg[y + rr][x:x + pc.w] = pc.bg[rr]
            lo, hi = getattr(pc, "gap", (0, 0))
            if moving and hi - lo >= 9:  # a comet runs a live board's footers, in the row above the frame
                cv.put(y + pc.h - 2, x + lo + int(fx.t * 8 + i * 7) % (hi - lo - 2), "━━━", META[k][2])
    if note_y is not None:
        more = n - min(n, (top + vis) * cols)
        segs = ([(f"↑ {top * cols} above", "dim")] if top else []) + ([("  ", "text")] if top and more else []) + \
            ([(f"↓ {more} more", "dim")] if more else []) + [("  ", "text"), (" ↑↓ ", "cap"), (" scroll", "dim")]
        cv.line(note_y, max(2, w - 2 - sum(T.width(t) for t, _ in segs)), segs, w - 2)
    return order


def _whole(pc, y, x, segs, right):
    """segs at (y, x) only when all of them fit before right: words are shown whole or not at all."""
    if x + sum(T.width(t) for t, _ in segs) > right:
        return x
    for t, style in segs:
        x = pc.put(y, x, t, style)
    return x


def _parts(parts, n):
    """As many whole parts, joined with " · ", as fit in n cells: a caption never ends cut."""
    out = ""
    for p in parts:
        nxt = f"{out} · {p}" if out else p
        if T.width(nxt) > n:
            break
        out = nxt
    return out


def _frame(pc, top, bottom, rows, cfg):
    """The panel's hairline frame in the `border` setting; rounded, the default, draws square as the web Board's."""
    w, border = pc.w, cfg.get("border")
    tl, tr, bl, br, hz, vt = T.BORDERS["square" if border not in T.BORDERS or border == "rounded" else border]
    for y, (a, b) in ((top, (tl + hz, hz + tr)), (bottom, (bl + hz, hz + br))):
        pc.put(y, 0, a, "bracket")
        pc.put(y, 2, hz * (w - 4), "line")
        pc.put(y, w - 2, b, "bracket")
    for y in range(top + 1, bottom):
        style = "bracket" if y in rows else "line"
        pc.put(y, 0, vt, style)
        pc.put(y, w - 1, vt, style)


def panel(key, d, cfg, w, h, n):
    """One picture in the Board's chrome, w×h: its number, name, label, the picture, its caption and stat."""
    name, what, ink = META[key]
    pc = T.Canvas(w, h)
    pc.tint(0, 0, h, w, T.col(cfg, "panel", "bg"))
    chip = f" {n:02d} "
    if h < 5 or w < 8:
        pc.line(0, 0, [(chip, f"chip_{ink}"), (f" {name}", "strong")], w)
        return pc
    py = 3 if h >= 12 else 2
    sub = T.Canvas(w - 4, max(1, h - 2 - py))
    try:
        label, foot, stat, pct = PAINT[key](sub, d, cfg)
    except Exception as e:  # a picture that can't draw says why, in its own frame
        sub = T.Canvas(w - 4, max(1, h - 2 - py))
        label, foot, stat, pct = "", [s(f"can't draw: {type(e).__name__}: {e}", 200)], "", None
    for r in range(min(sub.h, h - 2 - py)):
        pc.cells[py + r][2:2 + sub.w] = sub.cells[r]
        for c, bg in enumerate(sub.bg[r]):
            if bg is not None:
                pc.bg[py + r][2 + c] = bg
    _frame(pc, 0, h - 1, (1, h - 2), cfg)
    x = pc.put(1, 2, chip, f"chip_{ink}")
    x = pc.put(1, x, f" {name}", "strong", right=w - 2)
    label, right = s(label, 28).upper(), w - 2
    if label and w - 2 - T.width(label) - x >= 2:
        right = pc.put(1, w - 2 - T.width(label), label, ink) - T.width(label) - 2
    if T.width(what) + 3 <= right - x:
        pc.put(1, x, f" · {what}", "dim")
    sx, stat = w - 2, s(stat, max(0, w // 2))
    if stat:
        sx = w - 2 - T.width(stat)
        pc.put(h - 2, sx, stat, ink)
    if pct is not None and sx - 10 >= 24:
        k = round(8 * max(0.0, min(1.0, pct)))
        pc.put(h - 2, sx - 9, "━" * k, ink)
        pc.put(h - 2, sx - 9 + k, "─" * (8 - k), "line")
        sx -= 9
    end = pc.put(h - 2, 2, _parts(foot, sx - 4) if isinstance(foot, list) else T.fit(foot, sx - 4), "dim")
    pc.gap = (end + 2, sx - 2)  # the footer's free cells: a live board's comet runs there, never on the frame
    return pc


# ---------------------------------------------------------------- the pictures (each on a w×h canvas, no clock)

def _flow(items, room, lines=2):
    """(text, style) items greedily into `lines` rows of room cells, whole only; the rows and how many fit."""
    rows, used = [[]], 0
    for i, (t, style) in enumerate(items):
        need = T.width(t) + (1 if rows[-1] else 0)
        if used + need > room:
            if len(rows) == lines or T.width(t) > room:
                return rows, i
            rows.append([])
            used, need = 0, T.width(t)
        rows[-1].append((t, style))
        used += need
    return rows, len(items)


def p_lanes(pc, d, cfg):
    """A cluster per lane, as on the Board: its task a pill over a hub, its steps done an arc round it, the files it
    holds inside on the golden angle (spokes faint, the hot ones ringed), the files past the named ones a faint scatter,
    its state beneath; as many clusters as fit at 14 cells by 5 rows and a blank row each, a short last row centred."""
    W, H, cl = pc.w, pc.h, d["clusters"]
    n = len(cl)
    if not n:
        _whole(pc, H // 2, max(0, (W - 20) // 2), [("no builder lanes out", "dim")], W)
    most = max(1, (H + 1) // 6)
    cols = max(1, min(n, W // 14, max(round(math.sqrt(n * W / (2 * H))), -(-n // most)))) if n else 1
    rows = min(-(-n // cols), most) if n else 1
    shown, ch, cw = cl[:cols * rows], (H + 1) // rows, W // cols  # each row's last line a gap, but the grid's last
    dots, text = T.Dots(W, H), []
    for i, c in enumerate(shown):
        inrow = min(cols, len(shown) - i // cols * cols)
        x0, y0, ink = (W - inrow * cw) // 2 + i % cols * cw, i // cols * ch, LANE_COL[c["state"]]
        names = ch - 1 >= 7  # room for a row of the files' names
        dh = max(1, ch - 3 - names)  # the rows between the pill and the caption
        hx, hy = 2 * x0 + cw, 4 * (y0 + 1) + 2 * dh  # the hub, in dots
        ry = max(2.0, dh * 2 - 1.0)
        rx = max(3.0, min(cw - 2.0, ry * 1.6))
        rnd = random.Random(c["task"])  # each lane's own scatter, the same each frame
        spin, nodes = rnd.random() * math.tau, c["nodes"]
        at = [(hx + math.cos(spin + j * 2.39996) * rx * (.3 + .5 * math.sqrt((j + .5) / len(nodes))),
               hy + math.sin(spin + j * 2.39996) * ry * (.3 + .5 * math.sqrt((j + .5) / len(nodes)))) for j in range(len(nodes))]
        done, total = c["steps"]["done"], c["steps"]["total"]
        arc = max(12, int((rx + ry) * 2.2))  # its steps: the done share bright from twelve o'clock, the rest dashed
        for a in range(arc):
            lit = total and a < arc * done / total
            if lit or a % 2 == 0:
                th = -math.pi / 2 + a / arc * math.tau
                dots.dot(hx + math.cos(th) * rx, hy + math.sin(th) * ry, ink if lit else "line")
        for px, py in at:
            dots.line(hx, hy, px, py, "line", dash=1)
        for _ in range(min(40, c["more"])):
            a, k = rnd.random() * math.tau, .55 + rnd.random() * .3
            dots.dot(hx + math.cos(a) * rx * k, hy + math.sin(a) * ry * k, "line")
        for (px, py), nd in zip(at, nodes):
            if nd["hot"]:
                dots.ring(px, py, 1.2, "yellow")
            dots.dot(px, py, "yellow" if nd["hot"] else ink)
        dots.ring(hx, hy, 2.2, ink)
        dots.dot(hx, hy, ink)
        pill = T.fit(f" {c['task']} ", cw - 1)
        text.append((y0, x0 + (cw - T.width(pill)) // 2, [(pill, f"chip_{ink}")], x0 + cw))
        frac = f"{done}/{total}" if total else ""
        opts = (f"{c['state']} · {frac}", f"{c['state']} {frac}", c["state"], frac) if frac else (c["state"],)
        cap = next((x for x in opts if T.width(x) <= cw - 2), "")  # a blank between neighbours' captions
        text.append((y0 + ch - 2, x0 + (cw - T.width(cap)) // 2, [(cap, "dim")], x0 + cw - 1))
        if names and nodes:
            items = [(nd["p"], "yellow" if nd["hot"] else "muted") for nd in sorted(nodes, key=lambda nd: not nd["hot"])]
            got, k = _flow(items, cw - 2, 1)
            if k < len(items) + c["more"]:
                got, k = _flow(items, cw - 2 - len(f" +{len(items) + c['more']}"), 1)
                got[-1].append((f"+{len(items) + c['more'] - k}", "dim"))
            row = [seg for t, style in got[0] for seg in ((t, style), (" ", "text"))][:-1]
            text.append((y0 + ch - 3, x0 + (cw - sum(T.width(t) for t, _ in row)) // 2, row, x0 + cw - 1))
    dots.draw(pc, 0, 0)
    for y, x, segs, right in text:  # the words over the dots, whole
        _whole(pc, y, x, segs, right)
    building = sum(c["state"] == "building" for c in cl)
    waiting = sum(c["state"] == "waiting" for c in cl)
    files = sum(len(c["nodes"]) + c["more"] for c in cl)
    hot = sum(nd["hot"] for c in cl for nd in c["nodes"])
    count = len(cl) + d["more"]
    return (f"{count} lane{'s' * (count != 1)}", [f"building {building}", f"waiting {waiting}"] if cl
            else ["no builder lanes out"], f"{hot} hot of {files} files" if files else "", None)


def p_train(pc, d, cfg):
    W, H = pc.w, pc.h
    lw = max(4, min(16, W * 28 // 100))
    nx = max(lw + 2, int(W * .58))
    tx = min(W - 1, nx + 6)
    ny, hm, ht = 2 * H, max(2, 2 * H - 3), 2  # the funnel's axis, its half height at the mouth and the throat (dots)
    wait = d["waiting"][:max(1, (H + 1) // 2)]
    dots = T.Dots(W, H)
    for i, c in enumerate(wait or [None]):
        y = min(H - 1, 2 * i)
        ink = ("cyan" if c["state"] == "building" else "yellow") if c else "line"
        th = min(3, 1 + c["files"] // 2) if c else 1
        for k in range(th):  # a band per lane, as thick as its files, into the funnel's mouth
            o = k - (th - 1) / 2
            ey = max(ny - hm + 1, min(ny + hm - 1, ny + (i - (len(wait) - 1) / 2) * 3 + o))
            dots.curve((2 * lw, 4 * y + 2 + o), (lw + nx, 4 * y + 2 + o), (lw + nx, ey), (2 * nx, ey), ink)
    dots.line(2 * nx, ny - hm, 2 * tx, ny - ht, "yellow")
    dots.line(2 * nx, ny + hm, 2 * tx, ny + ht, "yellow")
    for xx in range(2 * nx + 3, 2 * tx, 3):  # the gate's lattice
        half = hm + (ht - hm) * (xx - 2 * nx) / max(1, 2 * (tx - nx))
        dots.line(xx, ny - half, xx, ny + half, "yellow", dash=1)
    rx = tx + 2
    dots.line(2 * tx, ny, 2 * rx - 1, ny, "yellow")
    dots.draw(pc, 0, 0)
    for i, c in enumerate(wait):
        pc.put(min(H - 1, 2 * i), 0, T.fit(f" {c['task']} ", lw), "chip_cyan" if c["state"] == "building" else "chip_yellow")
    if not wait:
        pc.put(0, 0, T.fit(" none waiting ", lw), "chip_todo")
    if H >= 3:
        pc.put(H - 1, max(0, tx - 4), "GATE", "yellow")
    cy = H // 2
    pc.put(cy, rx, " foreman/build " if W - rx >= 15 else " build ", "chip_yellow", right=W)
    if d["left"]:
        _whole(pc, max(0, cy - 2), rx, [("✗ ", "pink"), (f"{d['left']} left out", "pink")], W)
    for j, x in enumerate(reversed(d["landed"])):
        if cy + 2 + j >= H:
            break
        hm_ = re.search(r"(\d\d:\d\d)", x["at"])
        segs = [("✓ ", "green"), (x["task"] + " ", "strong"), ((hm_.group(1) + " ") if hm_ else "", "dim")]
        if W - rx - sum(T.width(t) for t, _ in segs) >= 10:  # a title only where a few words of it fit, never a lone "…"
            segs.append((x["title"], "muted"))
        pc.line(cy + 2 + j, rx, segs, W)
    foot = [f"{d['landed24']} landed in 24 h", f"{d['trains24']} trains", f"{d['left']} left out"] if d["known"] \
        else ["no train yet"]
    return ("train running" if d["running"] else "foreman/build", foot,
            f"{d['mpl']} min / lane" if d["mpl"] is not None else "", None)


def p_cpu(pc, d, cfg):
    W, H = pc.w, pc.h
    now, cores, goal = d["now"], d["cores"], d["goal"]
    pct = 100 * now["busy"] / cores
    lw = max(6, min(16, W // 3))
    big = T.Pix(lw, 3)
    big.text(0, 0, "--%" if not d["known"] else f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%",
             "dim" if not d["known"] else "bad" if pct > goal else "green")
    big.draw(pc, 0, 0, cfg)
    _whole(pc, 3, 0, [(f"of {cores} cores", "dim")], lw)
    for i, k in enumerate(KINDS):
        if 4 + i >= H - 1:
            break
        if not d["known"] or _whole(pc, 4 + i, 0, [("█ ", KIND_COL[k]), (f"{k} ", "muted"), (f"{now[k]:.1f}", "strong")],
                                    lw) == 0:
            _whole(pc, 4 + i, 0, [("█ ", KIND_COL[k]), (k, "muted")], lw)
    cx0 = lw + 2
    cw, ch = W - cx0, H - 1
    if cw >= 4 and ch >= 1:
        pix, mins = T.Pix(cw, ch), d["minutes"]
        top = max(max((sum(r[1:]) for r in mins), default=0), goal * cores / 100 * 1.5) or 1
        for c in range(cw if mins else 0):  # the last 30 minutes, stacked by kind of work
            r, acc = mins[min(len(mins) - 1, c * len(mins) // cw)], 0.0
            for i, k in enumerate(KINDS):
                h0 = round(acc / top * 2 * ch)
                acc += r[1 + i]
                for yy in range(h0, round(acc / top * 2 * ch)):
                    pix.set(c, 2 * ch - 1 - yy, KIND_COL[k])
        for q in (1, 2, 3):  # the chart's hairline axes, as on the Board (never a made-up curve while nothing is read)
            for c in range(1, cw, 2):
                if (c, 2 * ch - 1 - q * 2 * ch // 4) not in pix.px:
                    pix.set(c, 2 * ch - 1 - q * 2 * ch // 4, "line")
        gy = 2 * ch - 1 - round(goal * cores / 100 / top * 2 * ch)
        for c in range(0, cw, 3):  # the goal, dashed
            if (c, gy) not in pix.px:
                pix.set(c, gy, "bad")
        pix.draw(pc, 0, cx0, cfg)
        if not mins and ch >= 3:  # centred, a blank either side where it crosses an axis
            msg = " waiting for the first CPU minute "
            _whole(pc, ch // 2, cx0 + max(0, (cw - T.width(msg)) // 2), [(msg, "dim")], W)
        if cw >= 14:
            pc.put(H - 1, cx0, "−30 min", "dim")
            pc.put(H - 1, W - 3, "now", "dim")
    foot = [f"{now['busy']:.1f} of {cores} cores busy now"] if d["known"] else ["no CPU reading yet"]
    return f"goal {goal:g}%", foot, f"{pct:.1f}%" if d["known"] else "", now["busy"] / cores if d["known"] else None


def p_loop(pc, d, cfg):
    W, H = pc.w, pc.h
    lw = max(6, W * 38 // 100)
    ink = "pink" if d else "line"
    pc.put(0, 0, "YOU ASKED", ink)
    said = T.wrap(d["words"] if d else "nothing asked yet · n for a new request", lw - 1, lines=max(1, min(5, H - 2)))
    for i, ln in enumerate(said):
        pc.put(1 + i, 0, ln, "said" if d else "dim")
    dots, my = T.Dots(4, H), 2 * H
    for i in range(len(said)):  # the words, drawn into the brief
        sy = min(4 * H - 1, 4 * (1 + i) + 2)
        dots.curve((0, sy), (5, sy), (3, my), (7, my), ink)
    dots.draw(pc, 0, lw)
    pc.put(H // 2, lw + 3, ">>>", ink)
    for y in range(H):
        pc.put(y, lw + 6, "▌", ink)
    cx = lw + 7
    if not d:
        pc.put(0, cx, " brief.md ", "chip_todo")
        return "idle", ["no task in focus"], "", None
    x = pc.put(0, cx, f" {d['id']}.md ", "chip_pink")
    _whole(pc, 0, x, [(f" choice {d['choice']}%" if d["choice"] is not None else f" {d['sha']}" if d["sha"] else "",
                       "dim")], W)
    items = [(("[x] ", "green") if c["ok"] else ("[✗] ", "pink") if c["bad"] else ("[ ] ", "dim"), c["text"])
             for c in d["criteria"]]
    items += [(("● ", "green") if x["done"] else ("◉ ", "yellow") if x["current"] else ("○ ", "dim"), x["text"])
              for x in d["steps"]]
    tw = max(1, W - cx - 4)
    for k in (2, 1):  # the title on two lines while the items still fit whole, else one
        title = T.wrap(d["title"], W - cx, lines=k)
        room = max(0, H - 2 - len(title))
        shown = items if len(items) <= room else items[:max(0, room - 1)]
        wrap = lambda n: [T.wrap(text, tw, lines=n) for _, text in shown]
        lines = next((ls for n in (3, 2) for ls in [wrap(n)] if sum(map(len, ls)) <= room), None)  # whole, when room
        if lines is not None:
            break
    lines = lines or wrap(1)
    for i, ln in enumerate(title):
        pc.put(1 + i, cx, ln, "strong", right=W)
    y = 1 + len(title)
    for ((mark, style), _), ls in zip(shown, lines):
        x = pc.put(y, cx, mark, style)
        for ln in ls:
            pc.put(y, x, ln, "text", right=W)
            y += 1
    if len(shown) < len(items) and room:
        pc.put(y, cx, f"+{len(items) - len(shown)} more", "dim")
    cur = TRACK.get(d["stage"], -1)
    segs = []
    for i, name in enumerate(("plan", "build", "verify", "close")):
        segs += [(" ─ ", "line")] if i else []
        segs += [(name, "green" if i < cur else "pink" if i == cur else "dim")]
    if H >= 4:
        _whole(pc, H - 1, cx, segs, W)
    ok, n = sum(c["ok"] for c in d["criteria"]), len(d["criteria"])
    return (d["stage"] or "idle", [d["id"], f"{ok} of {n} criteria met"] if n else [d["id"]],
            f"choice {d['choice']}%" if d["choice"] is not None else d["sha"], ok / n if n else None)


def p_library(pc, d, cfg):
    W, H = pc.w, pc.h
    gw = max(2, min(2 * (H - 1), W * 40 // 100))
    dots, tot = T.Dots(gw, H), d["total"]
    cx, cy = gw, 2 * H
    R = max(1.0, min(gw, 2 * H) - 1.0)
    bounds, acc = [], 0
    for k, ink in LIB:
        acc += d["counts"][k]
        bounds.append((acc / tot if tot else 0, ink))
    dots.ring(cx, cy, R, "line")
    for i in range(160):  # a Fibonacci sphere, its front half, banded by each grid's share of what is kept
        yy = 1 - 2 * (i + .5) / 160
        r, th = math.sqrt(max(0.0, 1 - yy * yy)), i * 2.39996 + .6
        if math.sin(th) >= 0:
            ink = next((ink for b, ink in bounds if i / 160 < b), "line") if tot else "line"
            dots.dot(cx + R * r * math.cos(th), cy - R * yy, ink)
    dots.draw(pc, 0, 0)
    lx = gw + 1
    for i, (k, ink) in enumerate(LIB[:H]):
        _whole(pc, i, lx, [("● ", ink if d["counts"][k] else "line"), (f"{k} ", "muted"), (f"{d['counts'][k]:,}", "strong")], W)
    hx, top_y = (gw + 18, 0) if W - (gw + 18) >= 12 else (lx, 5)
    hw, hh = W - hx, H - 1 - top_y
    if hw >= 10 and hh >= 1:  # kept per hour, the last 24
        bw = max(1, hw // 24)
        vals = d["hourly"][-(hw // bw):]
        mx = max(vals) or 1
        for i, v in enumerate(vals):
            if v:
                T.vbar(pc, hx + i * bw, H - 2, max(1, round(v / mx * hh * 8)), "violet", w=max(1, bw - 1))
            else:
                pc.put(H - 2, hx + i * bw, "▁" * max(1, bw - 1), "line")
        pc.put(H - 1, hx, "−24 h", "dim")
        pc.put(H - 1, W - 3, "now", "dim")
    day = sum(d["hourly"])
    return (f"{tot:,} kept", [f"{day:,} kept in 24 h"] if d["known"] else ["nothing kept yet"],
            f"{d['hourly'][-1]:,} this hour" if d["known"] else "", None)


def p_models(pc, d, cfg):
    W, H = pc.w, pc.h
    ghost = not d["agents"]
    agents, ms = (["agent"], [{"name": "model", "group": "ollama"}]) if ghost else (d["agents"], d["models"])
    links = [{"a": 0, "m": 0, "n": 1}] if ghost else d["links"]
    rows = max(1, H - (1 if ghost else 0))
    na, nm = min(len(agents), rows), min(len(ms), rows)
    step = lambda n: min(rows / n, 4)  # a few rows sit together, centred, never spread to the panel's ends
    ay = lambda i: int((rows - step(na) * na) / 2 + (i + .5) * step(na))
    my = lambda j: int((rows - step(nm) * nm) / 2 + (j + .5) * step(nm))
    aw = max(6, min(18, W * 30 // 100))
    mw = min(max(8, W * 40 // 100), max((T.width(x["name"]) + 2 for x in ms[:nm]), default=8))
    dots = T.Dots(W, H)
    x0, x1 = 2 * aw, 2 * (W - mw) - 1
    total, band = sum(ln["n"] for ln in links) or 1, 4 * step(max(na, nm)) * .6  # the room a whole flow may take
    for ln in links:  # who calls which model, as thick as its share of the calls (a Sankey band)
        if ln["a"] < na and ln["m"] < nm and x1 > x0:
            y0, y1, mid = 4 * ay(ln["a"]) + 2, 4 * my(ln["m"]) + 2, (x0 + x1) / 2
            ink = "line" if ghost else GROUP_COL[ms[ln["m"]]["group"]]
            thick = max(1, min(int(band), round(band * ln["n"] / total)))
            for k in range(thick):
                o = k - thick // 2
                dots.curve((x0, y0 + o), (mid, y0 + o), (mid, y1 + o), (x1, y1 + o), ink)
    dots.draw(pc, 0, 0)
    for i in range(na):
        pc.put(ay(i), 0, f" {T.fit(agents[i], aw - 2)} ", "chip_todo", right=aw)
    for j in range(nm):
        txt = f" {T.fit(ms[j]['name'], mw - 2)} "
        pc.put(my(j), W - mw, txt, "chip_todo" if ghost else f"chip_{GROUP_COL[ms[j]['group']]}")  # where flows end
    if ghost and H >= 2:
        _whole(pc, H - 1, max(0, (W - 12) // 2), [("no calls yet", "dim")], W)
    g = d["gpu"]
    foot = [f"GPU {g['util']}%", f"VRAM {g['used'] / 1024:.1f} of {g['total'] / 1024:.1f} GB"] if g else ["no GPU reading"]
    return f"{d['cpm']} call{'' if d['cpm'] == 1 else 's'} / min", foot, f"{d['loaded']} loaded", g["util"] / 100 if g else None


def p_sampo(pc, d, cfg):
    W, H = pc.w, pc.h
    tw = max(8, W // 2)
    x = 0
    for ink in ("pink", "yellow", "green"):  # the terminal's lights
        x = pc.put(0, x, "● ", ink)
    pc.put(0, x, "sampo", "dim")
    cmds = d["commands"][-max(0, H - 2):] if H > 2 else []
    for i in range(max(0, H - 2)):
        pc.put(1 + i, 0, "▌", "line")
        if i < len(cmds):
            c = cmds[i]
            x = pc.put(1 + i, 2, "$ ", "cyan")
            x = pc.put(1 + i, x, T.fit(c["cmd"], tw - 3 - x), "text")
            pc.put(1 + i, x, " ✓" if c["ok"] else " ✗", "green" if c["ok"] else "pink")
    if H >= 2:
        pc.put(H - 1, 0, "▌", "line")
        pc.put(H - 1, 2, "$ ▌", "cyan")
    queue, qx = d["queue"], tw + 3  # a cell clear of the arcs, never touching an id
    ids = [q["id"] for q in queue]
    dots = T.Dots(2, H)
    arcs = [(ids.index(a), ids.index(b)) for dep in d["deps"] for a, b in zip(dep, dep[1:]) if a in ids and b in ids]
    for a, b in arcs or [(0, 2), (1, 3)]:  # what waits on what in the queue (ghosts: none known)
        ya, yb = 4 * (1 + a) + 2, 4 * (1 + b) + 2
        dots.curve((3, ya), (0, ya), (0, yb), (3, yb), "violet" if arcs else "line")
    dots.draw(pc, 0, tw)
    if W - qx >= 12:
        ix = 20 if W - qx - 20 >= 24 else 11  # the type's column only while the titles keep 24 cells
        for t, dx in (("ID", 0), ("T", 8)) + ((("TYPE", 11),) if ix == 20 else ()) + (("TITLE", ix),):
            _whole(pc, 0, qx + dx, [(t, "dim")], W)
        for i, q in enumerate(queue[:max(0, H - 1)]):
            pc.put(1 + i, qx, q["id"], "cyan", right=qx + 7)
            pc.put(1 + i, qx + 8, q["tier"], "strong", right=qx + 10)
            if ix == 20:
                pc.put(1 + i, qx + 11, q["type"], "dim", right=qx + 19)
            pc.put(1 + i, qx + ix, T.fit(q["title"], W - qx - ix), "muted")
    ok = sum(c["ok"] for c in d["commands"])
    return (f"{d['queued']} queued", [f"{len(d['commands'])} commands", f"{len(d['deps'])} waiting on others"],
            f"{ok}/{len(d['commands'])} ok" if d["commands"] else "", None)


def p_state(pc, d, cfg):
    W, H = pc.w, pc.h
    tree, doc = d["tree"], d["doc"]
    tw = max(8, W * 55 // 100)
    ink = "yellow" if tree else "line"
    pc.put(0, 0, "state/", ink)
    rows = tree[:max(0, H - 1)]
    last = len(rows) or min(4, H - 1)
    dots = T.Dots(2, H)
    if last:
        dots.line(1, 4, 1, 4 * last + 2, ink)  # the trunk, a branch to each entry
        for i in range(1, last + 1):
            dots.line(1, 4 * i + 2, 3, 4 * i + 2, ink)
    dots.draw(pc, 0, 0)
    nw = max(4, min(16, tw - 3 - 15))
    ax, bx = 3 + nw + 1, 3 + nw + 8
    mx = max([e["n"] or e["kb"] for e in rows] or [0]) or 1
    for i, e in enumerate(rows):
        pc.put(1 + i, 3, T.fit(e["name"] + ("/" if e["dir"] else ""), nw), "strong" if e["dir"] else "muted")
        pc.put(1 + i, ax, f"{e['n']:,}" if e["dir"] else f"{e['kb']:,} kB", "dim", right=bx - 1)
        if tw - 1 - bx >= 2:
            pc.put(1 + i, bx, T.hbar(tw - 1 - bx, round((e["n"] or e["kb"]) / mx * (tw - 1 - bx) * 8)), "yellow")
    if not rows and H >= 2:
        _whole(pc, 1, 3, [("nothing kept yet", "dim")], tw)
    for y in range(H):  # the newest document, as a card
        pc.put(y, tw + 1, "▌", "yellow" if doc else "line")
    cx = tw + 3
    if doc and W - cx >= 6:
        pc.put(0, cx, T.fit(f" latest {doc['kind']} ", W - cx), "chip_yellow")
        y = 1
        for ln in T.wrap(doc["title"], W - cx, lines=max(2, (H - 2) // 2 if doc["text"] else H - 2)):
            if y < H - 1:
                pc.put(y, cx, ln, "strong")
                y += 1
        for ln in T.wrap(doc["text"], W - cx, lines=max(1, H - 1 - y)):
            if y < H - 1:
                pc.put(y, cx, ln, "said")
                y += 1
        if H >= 3:
            pc.put(H - 1, cx, doc["at"], "dim", right=W)
    files = sum(e["n"] if e["dir"] else 1 for e in tree)
    return ("state/", [f"{len(tree)} entries", f"newest {d['newest']}"] if tree else ["nothing kept yet"],
            f"{files:,} files" if tree else "", None)


def p_tests(pc, d, cfg):
    W, H = pc.w, pc.h
    cnt = d["count"]
    colour = {"cached": T.mix(T.col(cfg, "green"), T.col(cfg, "panel", "bg"), .55) or "green", "pass": "green",
              "fail": "pink", "running": "yellow", "idle": "line"}
    style = lambda ink: T.rgb(fg=ink) if ink.startswith("#") else ink
    x = 0
    for k in TEST_STATES:  # the legend
        if cnt[k] or k in ("cached", "pass", "fail"):
            x = _whole(pc, 0, x, [("█ ", style(colour[k])), (f"{k} ", "muted"), (f"{cnt[k]:,}  ", "strong")], W)
    gh = H - 2
    if gh >= 1:  # a module a cell: what is cached, run, passing or failing
        per = (W + 1) // 2
        pix = T.Pix(W, gh)
        for i in range(per * gh):
            r, c = divmod(i, per)
            pix.set(2 * c, 2 * r, colour[d["cells"][i]["s"]] if i < len(d["cells"]) else "line")
        pix.draw(pc, 1, 0, cfg)
    fails = [c["n"] for c in d["cells"] if c["s"] == "fail"]
    if fails and H >= 2:
        rows, k = _flow([(f, "pink") for f in fails], W - 2 - len(f" +{len(fails)}"), lines=1)
        segs = [("✗ ", "pink")] + [seg for t in rows[0] for seg in (t, (" ", "text"))]
        pc.line(H - 1, 0, segs + ([(f"+{len(fails) - k}", "dim")] if k < len(fails) else []), W)
    elif H >= 2:
        pc.put(H - 1, 0, "✓ all passing" if d["total"] and d["done"] == d["total"] else "nothing run yet",
               "green" if d["total"] and d["done"] == d["total"] else "dim")
    foot = [f"{cnt['cached']:,} cached", f"{d['running'] or cnt['running']} running", f"{cnt['fail']} failing", d["source"]]
    mods = len(d["cells"]) + d["more"]
    return (f"{mods:,} modules", [p for p in foot if p], f"{d['done']}/{d['total']} passed" if d["total"] else "",
            d["done"] / d["total"] if d["total"] else None)


PAINT = {k: globals()[f"p_{k}"] for k in KEYS}  # looked up at each paint (a test spies on them)


# ---------------------------------------------------------------- the framed header

def _strip(st, fx):
    """The header's stats: the run's status chip, then small dim labels and their values."""
    chip = T.STATUS_CHIP.get(st.status, "chip_todo")
    status = f" {fx.spin()} {st.status.upper()} " if st.status in T.LIVE and fx.moving else f" {st.status.upper()} "
    paths = {p for v in st.files.values() for p in _l(v)} | set(st.stat)
    add, rm = (sum(_int((_l(v) + [0, 0])[i]) for v in st.stat.values()) for i in (0, 1))
    passed = sum(c.get("state") == "pass" for c in st.criteria)
    value = lambda v: (v, "strong") if v else ("–", "dim")
    return [[(status, chip)],
            [("TASK ", "dim"), value(_task(st))],
            [("STEP ", "dim"), value(f"{st.step}/{len(st.steps)}" if st.steps else "")],
            [("CHECKS ", "dim"), value(f"{passed}/{len(st.criteria)}" if st.criteria else "")],
            [("FILES ", "dim"), value(f"{len(paths)}" if paths else "")] + ([(f" +{add} −{rm}", "dim")] if paths and st.stat else []),
            [("TOKENS ", "dim"), value(T.human(sum(st.tokens)) if sum(st.tokens) else "")]]


def header(cv, st, app, cfg):
    """The Board's framed header, rows 0-3: the wordmark on the frame with the project and where the run runs, the
    stats on the grid's columns, the task now, and an amber callout only when something needs the user."""
    w, fx = cv.w, T.current()
    cv.tint(0, 0, 4, w, T.col(cfg, "panel", "bg"))
    _frame(cv, 0, 3, (1, 2), cfg)
    brand = " ⊖ SAMPO CODE "  # T-2876: the anvil-stone mark
    a, b, glow, ink = fx.c("accent"), fx.c("accent2"), fx.c("glow"), T.col(fx.cfg, "brand")
    live = fx.moving and st.status in T.LIVE  # a still screen's name holds still: its 10 s redraw writes nothing
    sweep = (fx.t % 7) / 1.4 * (len(brand) + 8) - 4 if live else -99  # a glint crosses the name now and then
    for i, ch in enumerate(brand):
        bg = T.mix(T.mix(a, b, i / (len(brand) - 1)), glow, 0.55 * max(0.0, 1 - abs(i - sweep) / 3))
        cv.put(0, 2 + i, ch, T.rgb(fg=ink, bg=bg, bold=True) if bg else "brand")
    x = 2 + len(brand)
    clock = T.elapsed(st, app)
    bits = [st.worker and f"worker {st.worker}" or st.model or "", st.containment or "", clock and f"⏱ {clock}"]
    for drop in ((), (2,), (2, 0), (2, 0, 1)):  # the clock goes first, then the worker: the containment stays
        right = "   ".join(v for i, v in enumerate(bits) if v and i not in drop)
        if not right or x + 2 + T.width(right) + 2 <= w - 3:
            break
    end = w - 2
    if right:
        end = cv.put(0, w - 4 - T.width(right), f" {right} ", "dim") - T.width(right) - 2
    root = getattr(app, "root", None)
    project = os.path.basename(os.path.abspath(str(root))) if root else ""  # a root like a/b/.. names b's parent
    if project:
        _whole(cv, 0, x + 1, [(" PROJECT ", "dim"), (f"{project} ", "strong")], end - 1)
    cols = _cols(w, len(set(k for k in ((cfg.get("layout") or T.DEFAULTS["layout"]).get("grid") or KEYS)
                            if k in PAINT)) or 9)
    pw, items = (w - (cols - 1)) // cols, _strip(st, fx)
    if cols == 1:  # one column: the stats packed by their own widths, details then whole stats dropped, a … says so
        wid, cut = lambda segs: sum(T.width(t) for t, _ in segs), False
        while len(items) > 1 and sum(map(wid, items)) + 2 * (len(items) - 1) > w - 4:
            long = [i for i, segs in enumerate(items) if len(segs) > 2]
            if long:
                items[long[-1]] = items[long[-1]][:2]
            else:
                items = items[:-2 if cut else -1] + [[("…", "dim")]]
                cut = True
        gap, x = (w - 4 - sum(map(wid, items))) / max(1, len(items) - 1), 2
        for i, segs in enumerate(items):
            _whole(cv, 1, int(x + i * gap), segs, w - 2)
            x += wid(segs)
        items = []
    while len(items) > 1 and pw // -(-len(items) // cols) < 16:  # a stat never shares less than 16 cells
        items.pop()
    k = max(1, -(-len(items) // cols))
    slot = pw // k
    for i, segs in enumerate(items):  # each stat whole, its detail first to go: FILES 3 +56 −3, then FILES 3
        x = (i // k) * (pw + 1) + 2 + (i % k) * slot
        for n in range(len(segs), min(2, len(segs)) - 1, -1):
            if _whole(cv, 1, x, segs[:n], min(w - 2, x + slot - 1)) != x:
                break
    need = None
    try:
        need = T.card(st, app)
    except Exception:
        need = None
    callout = s(f" ⚠ NEEDS YOU · {need[1]}: {need[2][0][1]} ", max(12, w // 2)) + " " if need else ""
    right = w - 2 - (T.width(callout) + 1 if callout else 0)
    x = cv.put(2, 2, " NOW ", "chip_cyan")
    tickets, rail = list(getattr(app, "tickets", None) or []), []  # T-1350: the requests that run after this one
    for n in range(min(len(tickets), len(T.CIRCLED)), 0, -1):  # whole tickets, at most half the strip; the rest counted
        rail = [("NEXT ", "dim")] + [seg for i, t in enumerate(tickets[:n]) for seg in (
            (f"{T.CIRCLED[i]} ", "accent"), (T.fit(T._one_line(t), 24) + "  ", "text"))] + \
            ([(f"+{len(tickets) - n} ", "dim")] if len(tickets) > n else [])
        if sum(T.width(t) for t, _ in rail) <= (right - x) // 2:
            break
    else:
        rail = []
    rw = sum(T.width(t) for t, _ in rail)
    if rail:
        cv.line(2, right - rw, rail, right)
        right -= rw + 1
    receipt = next((e.get("text") for e in reversed(st.log) if e.get("kind") == "receipt"), None) \
        if st.status == "closed" else None
    if receipt:  # T-1305: the closed run's receipt, the line the scrollback keeps: whole parts, the title only whole
        room = right - x - 12
        got = _parts(str(receipt).split(" · "), room)
        title = f" · {s(_d(st.plan).get('title') or st.request, 200)}"
        segs = [(" ▣ ", "accent"), ("receipt  ", "dim"), (got, "strong")] + \
            ([(title, "dim")] if T.width(got + title) <= room else [])
    elif st.task or st.request:
        step = st.steps[st.step - 1]["text"] if 0 < st.step <= len(st.steps) and st.status in T.LIVE else \
            st.closed if st.status == "closed" else ""
        segs = [(" ", "text")] + ([(_task(st), "strong"), (" · ", "dim")] if _task(st) else []) + \
            [(s(_d(st.plan).get("title") or st.request, 200), "strong")] + ([(f" › {s(step, 120)}", "dim")] if step else [])
    else:
        segs = [("  nothing running · ", "dim"), (" n ", "cap"), (" for a new request", "dim")]
    cv.line(2, x, segs, right)
    if callout:
        cv.put(2, w - 2 - T.width(callout), callout, "chip_yellow")


def _register():
    titles = {"cpu": "CPU"}
    for k in KEYS:
        def fn(st, w, h, app, k=k):
            m = model(st, app)
            pc = panel(k, m[k], app.cfg if app is not None else T.current().cfg, max(1, w), max(1, h or 16),
                       _order(st, m, KEYS).index(k) + 1)
            return [T._segments(row) for row in pc.cells]
        T.PANELS[k] = (titles.get(k, META[k][0].capitalize()), fn)
        T.BUILTIN_PANELS[k] = fn


_register()
