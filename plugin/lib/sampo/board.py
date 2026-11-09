"""T-3659: the Board's facts, the live page's nine panels. collect(p) is the snapshot's "board" field: one part per
mechanism (lanes and their leased files, the merge train, the machine's CPU by kind, the task in focus, the card and
moon library, the sampo commands run here, the depends_on graph, the state folder, the test modules). Each part is
cheap (a slow one is cached), soft (one that raises reads None, the rest stand) and plain: every string it returns
passes redact, the home-folder and private-term redaction, and a width cap, so the page shows no secret and no
machine layout. board.js maps the field (with the rest of the snapshot) to the panels."""
import hashlib
import json
import os
import re
import threading
import time

from sampo import core as c
from sampo import cpu
from sampo import live
from sampo.watch import COMMANDS_MAX, commands as part_commands  # T-4008: the pane's poll reads it too

FILES_MAX, MINUTES_MAX, LANDED_MAX, TREE_MAX = 24, 30, 8, 12
KINDS = ("tests", "claude", "models", "hooks", "other")
HOT_S = 600  # a leased file written in the last ten minutes is hot
TICK_S = 10  # the CPU ring reads /proc at most this often
DAY_S = 86400
PARKED = {"conflict", "refused"}  # lane_stop kinds that park a lane until someone acts
GROWS = {"cards_grow": "cards", "moons_grow": "moons"}
MODELS = re.compile(r"ollama|llama-server|llama_server|\bvllm\b|\bjeff\b", re.I)  # local model servers and their runners
SLOW = {"train": 30, "library": 30, "state": 10, "deps": 5, "tests": 10, "commands": 3}  # seconds a part is kept


def _terms():
    return live._cached(("private_terms",), live.SLOW_S, c.private_terms)


def _t(s, n):
    """s as one plain, redacted line of at most n characters."""
    return c.fit(c.redact_local(c.redact(c.plain(" ".join(str(s or "").split()))), _terms()), n)


def _procs():
    """[(pid, command line)] as the CPU ring last saw them (at most TICK_S old): its scan reads each process's command
    line once in its life, so the Board adds no /proc read of its own."""
    with _RING_LOCK:
        return [(int(k[0]), f["cmd"]) for k, f in _RING["facts"].items()]


def _ls(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def _epoch(ts):
    at = c.parse_ts(ts)
    return at.timestamp() if at else None


# ---------------------------------------------------------------- the parts: each takes the project

def part_lanes(p):
    """Each builder lane (open, or merged in the last day): its state and the files its leases hold, hot ones (written
    in the last HOT_S) first, at most FILES_MAX of them."""
    from sampo import bus
    from sampo import lanes as lanes_mod
    briefs, now = live._briefs(p), time.time()
    try:
        done = set(c.finished_lanes(p, briefs, quiet_min=0))
    except Exception:
        done = set()
    held = {}
    for v in bus._leases().values():
        if isinstance(v, dict) and v.get("project") == p.slug and v.get("file"):
            held.setdefault(v.get("task"), set()).add(str(v["file"]).partition("::")[0])
    out = []
    for b in briefs:
        lane = b.meta.get("lane")
        if not lane:
            continue
        kind, at, _ = lanes_mod._stop(b)
        if b.status in c.CLOSED and not (kind == "merged" and now - at < DAY_S):
            continue
        state = ("merged" if kind == "merged" else "parked" if kind in PARKED
                 else "waiting" if b.id in done or b.meta.get("builder_returned") else "building")
        files = []
        for path in held.get(b.id, ()):
            try:
                hot = now - os.stat(os.path.join(str(lane), path)).st_mtime < HOT_S
            except (OSError, ValueError):
                hot = False
            files.append({"path": _t(path, 160), "hot": hot})
        files.sort(key=lambda f: (not f["hot"], f["path"]))
        steps = b.steps()
        out.append({"task": b.id, "title": _t(b.title, 80), "state": state, "files": files[:FILES_MAX],
                    "more": max(0, len(files) - FILES_MAX),
                    "steps": {"done": sum(s.done for s in steps), "total": len(steps)}})
    return out


def part_train(p):
    """The last day's merges: the lanes landed (newest LANDED_MAX), how many trains carried them, the minutes from a
    builder's launch to its merge, the lanes a red train left out, and whether a train runs now."""
    titles = {b.id: b.title for b in live._briefs(p)}
    built, waits, trains, landed, left = {}, [], set(), [], 0
    for e in c.ledger_events(p, ["lane_builder", "lane_merge", "lane_train_red"], c.iso(time.time() - DAY_S)):
        t, task, kind = _epoch(e.get("ts")), e.get("task"), e.get("event")
        if t is None:
            continue
        if kind == "lane_builder":
            built[task] = t
        elif kind == "lane_train_red":
            left += 1
        elif kind == "lane_merge":
            landed.append({"task": _t(task, 12), "title": _t(titles.get(task, ""), 80), "at": e["ts"]})
            train = (e.get("data") or {}).get("train") if isinstance(e.get("data"), dict) else None
            trains.add(tuple(train) if isinstance(train, list) else (task,))
            if task in built:
                waits.append(t - built.pop(task))
    return {"landed": landed[-LANDED_MAX:], "landed_24h": len(landed), "trains_24h": len(trains),
            "minutes_per_lane": round(sum(waits) / len(waits) / 60) if waits else None, "left_out": left,
            "running": any(re.search(r"\b(?:fm|sampo) lane train\b", cmd) for _, cmd in _procs())}


def part_loop(p):
    """The task in focus, from the words asked to the brief: criteria (met or not), steps, the stated choice's
    confidence and the stage. None with nothing in focus."""
    b = c.active_brief(live._briefs(p), p.lane)
    if not b:
        return None
    return {"id": b.id, "title": _t(b.title, 120), "words": _t(b.section("Raw request") or b.title, 240),
            "criteria": [{"text": _t(a.text.partition(" :: ")[0], 90), "ok": a.checked} for a in b.acceptance()[:6]],
            "steps": [{"text": _t(s.text, 70), "done": s.done, "current": s.current} for s in b.steps()[:9]],
            "choice": c.stated_confidence(b.section("Approach (options → choice → why)")),
            "stage": c.stage(b, c.effective_autonomy(p))}


def part_library(p):
    """The calling cards there are, the cards and moons kept each hour of the last day (oldest first) and the newest
    grow runs."""
    from sampo import callcards
    now, hourly, recent = time.time(), [0] * 24, []
    for e in c.ledger_events(p, list(GROWS), c.iso(now - DAY_S)):
        t, data = _epoch(e.get("ts")), e.get("data") if isinstance(e.get("data"), dict) else {}
        kept = data.get("kept") if isinstance(data.get("kept"), int) else 0
        if t is None or e.get("event") not in GROWS:
            continue
        h = 23 - int((now - t) // 3600)
        if 0 <= h < 24:
            hourly[h] += kept
        recent.append({"grid": GROWS[e["event"]], "at": e["ts"], "kept": kept})
    return {"calling": len(callcards.cards()[0]), "hourly": hourly, "recent": recent[-6:]}


def part_deps(p):
    """[task, what it depends on...] for each open task that waits on another."""
    return [[b.id] + [str(d) for d in list(b.meta.get("depends_on") or [])[:3]]
            for b in live._briefs(p) if b.status not in c.CLOSED and b.meta.get("depends_on")][:40]


def part_state(p):
    """The state folder's top entries (the newest TREE_MAX, folders first) and the latest decision as a card."""
    from sampo import cli
    rows = []
    try:
        with os.scandir(p.dir) as it:
            for e in it:
                try:
                    st = e.stat()
                    d = e.is_dir()
                except OSError:
                    continue
                rows.append((st.st_mtime, e.name, d, len(_ls(e.path)) if d else 0, 0 if d else st.st_size // 1024))
    except OSError:
        pass
    rows.sort(reverse=True)
    newest = rows[0][1] if rows else ""
    tree = sorted(rows[:TREE_MAX], key=lambda r: (not r[2], r[1]))
    doc = None
    try:
        with open(os.path.join(p.dir, "decisions.md"), "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - 16384))
            lines = f.read().decode("utf-8", "replace").splitlines()
        last = next((l for l in reversed(lines) if re.match(r"\|\s*\d{4}-\d\d-\d\d\s*\|", l)), None)
        if last:
            row = cli._decision_row(last)
            doc = {"kind": "decision", "title": _t(row["text"], 80), "text": _t(row["why"], 240), "at": _t(row["date"], 32)}
    except OSError:
        pass
    return {"tree": [{"name": _t(n, 32), "dir": d, "at": c.iso(t), "n": k, "kb": kb} for t, n, d, k, kb in tree],
            "newest": _t(newest, 32), "doc": doc}


def part_tests(p):
    """A cell per test module: running (a live process names it), failing (the latest check's run named it), cached
    (green in the run cache) or idle; and how many of the latest check's commands passed."""
    names = sorted({f[:-3] for d in ("plugin/tests", "tests") for f in _ls(os.path.join(p.root, d))
                    if f.startswith("test_") and f.endswith(".py")})
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "sampo", "run-cache", hashlib.sha1(os.fsencode(p.root)).hexdigest()[:16] + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            classes = json.load(f).get("classes") or {}
    except (OSError, ValueError, AttributeError):
        classes = {}
    cached = {str(k).split(".")[0] for k in classes}
    last = (c.ledger_tail(p, 2000, kinds=["check_run"]) or [{}])[-1]
    results = [r for r in ((last.get("data") or {}).get("results") or []) if isinstance(r, dict)]
    failing = {str(t).split(".")[0] for r in results for t in (r.get("failing") or [])}
    running = set()
    for _, cmd in _procs():
        if "python" in cmd:
            running.update(re.findall(r"\b(test_\w+)", cmd))
    state = lambda n: "running" if n in running else "fail" if n in failing else "cached" if n in cached else "idle"
    mods = [{"name": _t(n, 40), "state": state(n)} for n in names]
    ts = str(last.get("ts") or "")
    return {"modules": mods, "done": sum(1 for r in results if r.get("exit") == 0), "total": len(results),
            "running": sum(1 for m in mods if m["state"] == "running"), "source": f"last check {ts[11:16]}" if ts else ""}


# ---------------------------------------------------------------- the machine's CPU by kind, a minute at a time

_RING, _RING_LOCK = {}, threading.Lock()


def cpu_reset():
    with _RING_LOCK:
        _RING.clear()
        _RING.update(at=None, busy=None, procs=None, facts={}, minutes=[], now=None)


cpu_reset()


def _busy(proc):
    """The machine's busy clock ticks since boot: every /proc/stat cpu field but idle and iowait (guest is in user)."""
    head = (cpu._read(os.path.join(proc, "stat")) or "").split("\n", 1)[0].split()
    try:
        v = [int(x) for x in head[1:9]]
        return sum(v) - v[3] - v[4] if head[0] == "cpu" and len(v) == 8 else None
    except (IndexError, ValueError):
        return None


def _kind(pid, table):
    k = cpu.kind(pid, table)
    if k in ("tests", "hooks"):
        return k
    me = table[pid]
    if MODELS.search(me["cmd"] or me["comm"]):
        return "models"
    return "claude" if k in ("Claude itself", "short-lived under Claude") else "other"


def cpu_tick(now=None, proc="/proc", every=TICK_S):
    """Reads the machine (at most once each `every` seconds): the cores each kind used since the last read, into this
    minute's average. A process first seen now started since the last read, so all its ticks count; a child seen then and gone
    now is already in its parent's reaped ticks, so those are taken off (cpu.sample's rule). other is the busy rest."""
    now = time.time() if now is None else now
    with _RING_LOCK:
        if _RING["at"] is not None and 0 <= now - _RING["at"] < every:
            return
        facts, busy = _RING["facts"], _busy(proc)
        scan = cpu._scan(None, proc, facts, pids=[n for n in _ls(proc) if n.isdigit()])
        prev, at, busy0 = _RING["procs"], _RING["at"], _RING["busy"]
        _RING.update(at=now, busy=busy, procs=scan)
        if prev is not None and busy is not None and busy0 is not None and now > at:
            secs, used = now - at, dict.fromkeys(KINDS, 0)
            table = {k[0]: facts[k] for k in scan}
            gone = {}
            for k in prev.keys() - scan.keys():
                gone[facts[k]["ppid"]] = gone.get(facts[k]["ppid"], 0) + sum(prev[k])
            for k, (own, reaped) in scan.items():
                own0, reaped0 = prev.get(k, (0, 0))
                used[_kind(k[0], table)] += max(0, own - own0) + max(0, reaped - reaped0 - gone.get(k[0], 0))
            got = {k: v / cpu.TICK / secs for k, v in used.items() if k != "other"}
            got["busy"] = max(0, busy - busy0) / cpu.TICK / secs
            got["other"] = max(0.0, got["busy"] - sum(got[k] for k in KINDS if k != "other"))
            _RING["now"] = {k: round(v, 2) for k, v in got.items()}
            minute, rows = int(now // 60 * 60), _RING["minutes"]
            if not rows or rows[-1][0] != minute:
                rows.append([minute, 0, [0.0] * len(KINDS)])
            rows[-1][1] += 1
            rows[-1][2] = [s + got[k] for s, k in zip(rows[-1][2], KINDS)]
            _RING["minutes"] = [r for r in rows if now - r[0] < MINUTES_MAX * 60][-MINUTES_MAX:]
        for k in [k for k in facts if k not in scan]:
            del facts[k]


def cpu_part(p=None):
    """{kinds, goal_pct, cores, minutes: [[minute epoch, cores per kind...]], now: {kind: cores, busy}}."""
    with _RING_LOCK:
        rows = [[m] + [round(s / n, 2) for s in sums] for m, n, sums in _RING["minutes"]]
        return {"kinds": list(KINDS), "goal_pct": cpu.goal(p) if p else cpu.GOAL, "cores": cpu.ncores(),
                "minutes": rows, "now": dict(_RING["now"]) if _RING["now"] else None}


def part_cpu(p):
    cpu_tick()
    return cpu_part(p)


PARTS = {"cpu": part_cpu, "lanes": part_lanes, "train": part_train, "loop": part_loop, "library": part_library,
         "commands": part_commands, "deps": part_deps, "state": part_state, "tests": part_tests}  # cpu first: its scan is _procs


def collect(p):
    """{part: its facts, or None when it raised}; the slow parts kept SLOW[part] seconds."""
    out = {}
    for name in list(PARTS):
        fn = PARTS[name]
        try:
            out[name] = live._cached(("board", name, p.dir), SLOW[name], lambda: fn(p)) if name in SLOW else fn(p)
        except Exception:  # one part down leaves the rest of the board
            out[name] = None
    return out
