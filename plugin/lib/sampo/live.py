"""T-2998: sampo watch --web: everything Sampo is doing, as a read-only page on 127.0.0.1 (T-2997).

The server is settingsserve's: the one-time token swapped for an HttpOnly SameSite=Strict cookie, the Host check against
DNS rebinding, a CSP with nonces (its Handler, subclassed, not copied). Its one write is POST /api/act (T-3026, below); it never stops on idle:
it runs until Ctrl-C or SIGTERM. Its URL is written to the project's state (live.url, mode 0600); a spent token is
replaced at once, so `sampo watch --web --url` always prints a URL that still works.

GET /            the page: plugin/web/live/index.html (its __NONCE__ becomes the CSP nonce), or a placeholder
GET /static/NAME a file of plugin/web/live/ (live.css, live.js)
GET /api/state   one snapshot (the contract: research live-schema)
GET /api/stream  Server-Sent Events: a `state` event with the snapshot every 2 s
POST /api/act    {action, project?, session?, text?, confirmed?}: one of ACTIONS as a sampo child process (or a bus note); needs
                 Content-Type application/json, the server's own Origin and an X-Sampo-Nonce minted for a page load

Each collector is cheap (no model call, nothing beyond 127.0.0.1: Ollama's /api/ps, the Jeff server's /v1/models,
nvidia-smi, /proc) and fails soft: its field is empty, never a 500. The whole snapshot is cached CACHE_S seconds, so any
number of pages cost one build. Model calls come from what the code already logs: the ledger's cards_grow, moons_grow,
moons_funnel, run_session, genesis_confirm and genesis_review events (each names its models) and state/spend.jsonl."""
import calendar
import hmac
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from sampo import core as c
from sampo import settingsserve

WEB = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "web", "live"))
CACHE_S = 1.5  # the snapshot is shared; the stream asks every 2 s
SLOW_S = 30  # the stats read whole card and moon files
STREAM_S = 2
ALL_S = 10  # T-3730: the All view refreshes each other project's slice this often
STREAMS_MAX = 8  # open streams at once: a thread each
FLOW_S, CALL_S = 60, 300
NET_TIMEOUT = 1.0
TAIL_B = 1 << 20  # jeff-train's events: the last megabyte holds the steps and, usually, the latest start
OLLAMA, JEFF = "http://127.0.0.1:11434/api/ps", "http://127.0.0.1:8766/v1/models"
TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".png": "image/png",
         ".json": "application/json", ".woff2": "font/woff2", ".html": "text/html"}
JOBS = ((r"(?:moons|genesis moonshot) funnel", "funnel"), (r"(?:playbooks small|moons|genesis moonshot) grow", "grow"),
        (r"(?:exam|louhi) run|(?:genesis|rune) exam", "exam"), (r"(?:exam|louhi) models", "bench"),
        (r"\bcheck\b", "check"))  # T-3044: each loop by its verb and its old name (louhi, rune, moons)
FLOW_OF = {"cards_grow": ("card grow", "write"), "moons_grow": ("moon grow", "write"),
           "moons_funnel": ("moon funnel", "judge"), "run_session": ("session run", "act"),
           "genesis_confirm": ("genesis", "review"), "genesis_review": ("genesis", "review")}
CALL_KINDS = ((r"grow|card|write", "write"), (r"funnel|judge|moon", "judge"), (r"embed", "embed"), (r"train", "train"),
              (r"plan", "plan"), (r"review|audit", "review"))  # T-3014: the calling verb's spark colour; else act
_CACHE, _LOCK = {}, threading.Lock()
_STALE = {}  # T-4071: key -> the one thread refreshing it


def _cached(key, ttl, fn):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _LOCK:
        _CACHE[key] = (time.monotonic(), val)
    return val


def _stale(key, ttl, fn, empty):
    """T-4071: the last value at once (`empty` before the first), refreshed by one background thread once older than
    ttl: a reader never waits on a slow fn, and readers arriving together never each run it."""
    with _LOCK:
        hit = _CACHE.get(key)
        if (not hit or time.monotonic() - hit[0] >= ttl) and key not in _STALE:
            def run():
                try:
                    val = fn()
                except Exception:  # an optional collector: keep what the page had, retry after ttl
                    val = hit[1] if hit else empty
                with _LOCK:
                    _CACHE[key] = (time.monotonic(), val)
                    _STALE.pop(key, None)
            _STALE[key] = threading.Thread(target=run, daemon=True, name="live-stale")
            _STALE[key].start()
    return hit[1] if hit else empty


def _get_json(url):
    """The JSON at a loopback URL, None when it is down or isn't JSON."""
    try:
        with urllib.request.urlopen(url, timeout=NET_TIMEOUT) as r:  # nosec: a constant 127.0.0.1 URL
            return json.loads(r.read(1 << 20))
    except (OSError, ValueError, RecursionError):
        return None


def _smi():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _procs():
    """[(pid, command line)] of every process /proc lists (empty where it isn't there)."""
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if n.isdigit() and int(n) != os.getpid():
            try:
                with open(f"/proc/{n}/cmdline", "rb") as f:
                    out.append((int(n), f.read(4096).replace(b"\0", b" ").decode("utf-8", "replace").strip()))
            except OSError:
                continue
    return out


# ---------------------------------------------------------------- collectors: each takes the project, returns its field

def _briefs(p):
    return _cached(("briefs", p.dir), CACHE_S, lambda: c.load_briefs(p))


def project(p):
    meta = c.read_meta(p)
    return {"slug": p.slug, "drive": bool(meta.get("drive", True)), "autonomy": c.effective_autonomy(p, meta)}


def focus(p):
    b = c.active_brief(_briefs(p), p.lane)
    if not b:
        return None
    steps = b.steps()
    return {"id": b.id, "title": b.title, "step": f"{sum(s.done for s in steps)}/{len(steps)}",
            "stage": c.stage(b, c.effective_autonomy(p))}


def queue(p):
    open_, a = sorted((b for b in _briefs(p) if b.status in c.RUNNABLE), key=c._key)[:12], c.effective_autonomy(p)
    return [{"id": b.id, "title": b.title, "type": b.type, "tier": b.tier, "status": b.status,
             "step": f"{sum(s.done for s in b.steps())}/{len(b.steps())}", "stage": c.stage(b, a)} for b in open_]  # T-3044


def _closed_today(p):
    from sampo import outcomes
    today = c.now()[:10]
    done = [b for b in _briefs(p) if b.status == "done" and str(b.meta.get("updated") or "").startswith(today)]
    rows = sorted(({"id": b.id, "title": b.title, "at": outcomes.done_at(b)} for b in done), key=lambda r: r["at"], reverse=True)
    return [r for r in rows if r["at"].startswith(today)]


def closed_today(p):
    return _closed_today(p)[:30]


def lanes(p):
    from sampo import lanes as lanes_mod
    out, briefs = [], _briefs(p)
    try:  # T-3170: count builders the way lanes.builder_brief's cap does; a lane waiting on a merge isn't at work
        done = set(c.finished_lanes(p, briefs, quiet_min=0))
    except Exception:
        done = set()
    for b in briefs:
        if b.status in c.CLOSED or not b.meta.get("lane"):
            continue
        steps = b.steps()
        wait = b.id in done or bool(b.meta.get("builder_returned")) or lanes_mod._stop(b)[0] == "merged"
        out.append({"task": b.id, "waiting": wait, "title": b.title, "model": b.meta.get("builder_model"), "since": b.meta.get("updated"),
                    "step": f"{sum(s.done for s in steps)}/{len(steps)}", "stage": c.stage(b, c.effective_autonomy(p))})
    return out


def _ticks(pid):
    """Process pid's start time in clock ticks since boot (field 22 of /proc/PID/stat; the comm field may hold spaces
    and parens, so count from the last ')'), or None. Unlike /proc/PID's ctime it is the real start."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read(4096).rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _started(pid):
    """Epoch seconds the process started (start ticks + boot time from /proc/stat), or None."""
    t = _ticks(pid)
    try:
        with open("/proc/stat") as f:
            boot = next(int(x.split()[1]) for x in f if x.startswith("btime "))
        return boot + t / os.sysconf("SC_CLK_TCK") if t is not None else None
    except (OSError, StopIteration, ValueError, IndexError):
        return None


def _mine(pid):
    try:
        return os.stat(f"/proc/{pid}").st_uid == os.getuid()
    except OSError:
        return False


def _environ(pid):
    if not _mine(pid):
        return {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:  # the kernel lets only the same uid read it
            return dict(x.split("=", 1) for x in f.read(65536).decode("utf-8", "replace").split("\0") if "=" in x)
    except OSError:
        return {}


def _open_mine(path, cap=None):
    """A binary file object on a regular file this uid owns, or None: no symlink at the end (O_NOFOLLOW), no waiting on
    a FIFO (O_NONBLOCK), and the type and owner are read from the open fd, so nothing swaps in after the check."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, TypeError, ValueError):
        return None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or (cap and st.st_size > cap):
        os.close(fd)
        return None
    return os.fdopen(fd, "rb")


def _num(x):
    return x if type(x) in (int, float) and x == x and abs(x) < 1e15 else None


def _shape(done, total, unit, note, rate):
    """The progress dict a job carries; None when done or total isn't a sane count."""
    done, total = _num(done), _num(total)
    if done is None or not total or total <= 0 or done < 0:
        return None
    eta = round((total - done) / rate) if rate and rate > 0 and done < total else None
    return {"done": done, "total": total, "unit": str(unit or "")[:20], "note": str(note or "")[:80],
            "rate": rate, "eta_s": eta}


def _file_progress(pid, rec):
    try:
        have = _ticks(pid)
        if have is None or rec.get("start") != have:  # the pid was reused (or can't be told): not this process's file
            return None
        t0, t, d0 = float(rec["t0"]), float(rec["t"]), float(rec["d0"])
    except (KeyError, TypeError, ValueError):
        return None
    rate = (_num(rec.get("done")) or 0) - d0
    rate = rate / (t - t0) if t > t0 and rate > 0 else None
    return _shape(rec.get("done"), rec.get("total"), rec.get("unit"), rec.get("note"), rate)


def _train_progress(pid):
    """jeff-train's own events (JEFF_EVENTS in its environment): the last step of total_steps, loss, and a rate over the
    steps still in the tail. Only a regular file this uid owns is read, through one fd.
    Events (one JSON object a line): {"kind": "training_started", "total_steps": N, ...} once per start, and a resumed
    run appends another; {"kind": "training_step", "step": i, "loss": x, "process_elapsed_seconds": s, ...} each step."""
    path = _environ(pid).get("JEFF_EVENTS")
    if not path:
        return None
    try:
        if not os.path.isabs(path):  # relative to the trainer's own directory
            path = os.path.join(os.readlink(f"/proc/{pid}/cwd"), path)
    except OSError:
        return None
    f = _open_mine(path)
    if not f:
        return None
    with f:
        try:
            size = f.seek(0, 2)
            f.seek(max(0, size - TAIL_B))
            rows = []
            for line in f.read(TAIL_B).splitlines()[1 if size > TAIL_B else 0:]:  # the cut first line is partial
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
            if not any(isinstance(e, dict) and e.get("kind") == "training_started" for e in rows):
                f.seek(0)  # the start fell out of the tail: its line is the file's first
                try:
                    rows.insert(0, json.loads(f.readline(1 << 20)))
                except ValueError:
                    pass
        except OSError:
            return None
    started = [e for e in rows if isinstance(e, dict) and e.get("kind") == "training_started"]
    total = started[-1].get("total_steps") if started else None  # the latest start: a resume appends its own
    steps = [e for e in rows if isinstance(e, dict) and e.get("kind") == "training_step"][-40:]
    if not steps:
        return None
    a, b = steps[0], steps[-1]
    try:
        dt, ds = b["process_elapsed_seconds"] - a["process_elapsed_seconds"], b["step"] - a["step"]
        rate = ds / dt if dt > 0 and ds > 0 else None
    except (KeyError, TypeError):
        rate = None
    loss = _num(b.get("loss"))
    return _shape(b.get("step"), total, "steps", f"loss {loss:.3g}" if loss is not None else "", rate)


def _progress_files(alive):
    """{pid: parsed progress file} for live pids; a file whose pid is gone is removed."""
    d, out = os.path.join(c.state_dir(), "progress"), {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        pid = int(n[:-5]) if n.endswith(".json") and n[:-5].isascii() and n[:-5].isdecimal() else None
        if pid is None:
            continue
        if pid not in alive:
            try:
                os.unlink(os.path.join(d, n))
            except OSError:
                pass
            continue
        f = _open_mine(os.path.join(d, n), 4096)  # not a symlink, FIFO or big file
        if f:
            with f:
                try:
                    rec = json.loads(f.read(4096))
                except (OSError, ValueError):
                    continue
            out[pid] = rec if isinstance(rec, dict) else None
    return out


PY_VALUE_OPTS = ("-X", "-W", "--check-hash-based-pycs")  # T-3199: python3 options whose next word is their value


def _program_at(words):
    """The index of the program in a python command (python3 [options] PROGRAM ...): an option's value is skipped, -m
    MODULE names the module, -c CODE runs no program (None)."""
    i = 1
    while i < len(words):
        w = words[i]
        if w == "-c":
            return None
        if w == "-m":
            return i + 1 if i + 1 < len(words) else None
        if not w.startswith("-"):
            return i
        i += 2 if w in PY_VALUE_OPTS else 1
    return None


def jobs(p):
    out = []
    procs = _procs()
    files = _progress_files({pid for pid, _ in procs})
    for pid, cmd in procs:
        words = cmd.split()
        if not words or os.path.basename(words[0]) not in ("sampo", "fm") and not os.path.basename(words[0]).startswith("python"):
            continue
        # the program: sampo itself, or python3 [-u] PROGRAM ..., never a word in its args (T-3044 review: "fix jeff-train")
        at = 0 if os.path.basename(words[0]) in ("sampo", "fm") else _program_at(words)
        prog = os.path.basename(words[at]) if at is not None else ""
        at = at if prog in ("sampo", "fm") else None
        rest = " ".join(words[at + 1:]) if at is not None else cmd
        rest = re.sub(r"(?:^|\s)(?:-p|--project)(?:\s+|=)\S+", "", rest).strip()  # -p SLUG anywhere: the verb names the job
        kind = "train" if prog == "jeff-train" else next((k for rx, k in JOBS if at is not None and re.match(rx, rest)), None)
        if not kind:
            continue
        m = re.search(r"--(?:profile|model)[ =](\S+)", cmd)
        since = _started(pid)
        since = c.iso(since) if since else None
        out.append({"name": c.fit(rest, 60), "kind": kind, "pid": pid, "since": since, "model": m and m.group(1),
                    "progress": _train_progress(pid) if kind == "train" else
                    _file_progress(pid, files[pid]) if files.get(pid) else None, "line": c.fit(cmd, 100)})
    return out


def _calls(p):
    """[(at, from, to, kind, where)] (where: local/remote from the call log, else None) of the last CALL_S seconds' model calls the code already logged."""
    since = c.iso(time.time() - CALL_S)
    out = []
    for e in c.ledger_tail(p, 300):
        spec, at, d = FLOW_OF.get(e.get("event")), str(e.get("ts") or ""), e.get("data") or {}
        if not spec or at < since:
            continue
        names = d.get("models") if isinstance(d.get("models"), dict) else {d.get("model"): 1} if d.get("model") else {}
        out += [(at, spec[0], str(m), spec[1], None) for m in names if m]
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "spend.jsonl"), 200):
        f, at = str(e.get("feature") or ""), str(e.get("ts") or "")
        if f and at >= since:
            out.append((at, f, "claude", "review" if re.search("review|audit", f) else "plan" if "plan" in f else "act", None))
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "calls.jsonl"), 400):  # T-3014: every models.complete call
        f, at, to = str(e.get("from") or ""), str(e.get("ts") or ""), str(e.get("to") or "")
        if to and at >= since:
            out.append((at, f, to, str(e.get("kind") or next((k for pat, k in CALL_KINDS if re.search(pat, f)), "act")),
                    e.get("where") if e.get("where") in ("local", "remote") else None))
    return sorted(out, reverse=True)


def flows(p):
    since = c.iso(time.time() - FLOW_S)
    return [{"from": a, "to": b, "kind": k, "at": at} for at, a, b, k, _ in _calls(p) if at >= since]


def _family(name):
    n = name.lower()
    return ("claude" if re.match(r"(claude|haiku|sonnet|opus)", n) else "jeff" if "jeff" in n else
            "embed" if "embed" in n or "bge" in n else "ollama")


def models(p):
    out, seen = [], {}
    for r in (_cached("ps", 2, lambda: _get_json(OLLAMA)) or {}).get("models") or []:
        name = str(r.get("name") or r.get("model") or "")
        vram = int(r.get("size_vram") or 0) // 1048576
        if name:
            seen[name] = {"name": name, "family": _family(name), "where": "gpu" if vram else "cpu", "loaded": True,
                          "vram_mb": vram}
    for r in (_cached("jeff", 3, lambda: _get_json(JEFF)) or {}).get("data") or []:
        if isinstance(r, dict) and r.get("id"):
            seen.setdefault(str(r["id"]), {"name": str(r["id"]), "family": "jeff", "where": "remote", "loaded": True,
                                           "vram_mb": None})
    calls = _calls(p)
    local = {to for _, _, to, _, w in calls if w == "local"}  # any local row wins over a ledger row seen first
    for _, _, to, _, _ in calls:
        seen.setdefault(to, {"name": to, "family": _family(to),
                             "where": "cloud" if _family(to) == "claude" else "gpu" if to in local else "remote",
                             "loaded": _family(to) == "claude", "vram_mb": None})
    for name, m in seen.items():
        hits = [at for at, _, to, _, _ in calls if to == name]
        out.append(dict(m, calls_5m=len(hits), last_call=hits[0] if hits else None))
    return out


def gpu(p):
    for line in _smi().splitlines():
        name, *nums = [x.strip() for x in line.split(",")]
        if len(nums) == 3 and all(x.isdigit() for x in nums):
            return {"name": name, "used_mb": int(nums[0]), "total_mb": int(nums[1]), "util_pct": int(nums[2])}
    return None


def events(p):
    """The ledger's and state/events.jsonl's newest, plain text, redacted (hook timing and tool rows are noise)."""
    rows = []
    for e in c.ledger_tail(p, 80):
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        rows.append((str(e.get("ts") or ""), str(e.get("event") or ""), f"{e.get('task') or ''} {_words(d)}".strip()))
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 200):
        if e.get("kind") not in ("hook_ms", "tool"):
            rows.append((str(e.get("ts") or ""), str(e.get("kind") or ""), _words(e)))
    return [{"at": at, "kind": k, "text": c.fit(c.redact(t), 160)} for at, k, t in sorted(rows, reverse=True)[:60]]


_QUIET = {"ts", "kind", "session_id", "task", "project", "h", "prev", "hash", "file", "tool", "step"}


def _words(d):
    """T-3014: a payload as the ticker's words, never JSON: the tool, the file's name and the step, then counts, then
    its prose fields: `Edit test_traces.py`, `step 3 · python3 plugin/bin/sampo shot x`."""
    head = [str(d[k]) for k in ("tool",) if d.get(k)] + ([os.path.basename(str(d["file"]))] if d.get("file") else [])
    head += [f"step {d['step']}"] if d.get("step") is not None else []
    head += [f"{k} {v}" for k, v in d.items() if k not in _QUIET and isinstance(v, (int, float)) and not isinstance(v, bool)]
    prose = [v for k, v in d.items() if k not in _QUIET and isinstance(v, str) and v]
    return " · ".join(x for x in (" ".join(head), *prose) if x)


def spend_today():
    """T-3092: dollars spent in each local hour of today so far (state/spend.jsonl), for the page's spend chart."""
    t0 = time.localtime()
    start = time.mktime((t0.tm_year, t0.tm_mon, t0.tm_mday, 0, 0, 0, 0, 0, -1))
    hrs = [0.0] * (t0.tm_hour + 1)
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "spend.jsonl"), 5000):
        try:
            at, usd = calendar.timegm(time.strptime(str(e["ts"]), "%Y-%m-%dT%H:%M:%SZ")), float(e.get("usd") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        h = int((at - start) // 3600)
        if 0 <= h < len(hrs) and math.isfinite(usd):
            hrs[h] += usd
    return [round(x, 2) for x in hrs]


def stats(p, wait=True):
    """The card and moon totals read every card and moon (12.8 s here): wait=False (the live page) serves the last ones
    and refreshes them in the background (T-4071)."""
    def slow():
        from sampo import moonscale, smallcards
        cov, st = smallcards.coverage(), moonscale.status(p)
        return {"cards": {"general": cov["total"], "ui": cov["ui"].get("total", 0), "ui_cells": cov["ui"].get("filled", 0),
                          "grid_ui": cov["ui"].get("grid", 0)},
                "moons": {"candidates": st["total"], "stages": st["stages"], "pace": moonscale.pace(p)},
                "spend": spend_today(), "bench": []}  # ponytail: the bench table is a model read away; the page shows none until a cheap one exists
    from sampo import louhi
    key = ("stats", p.dir)
    return dict(_cached(key, SLOW_S, slow) if wait else _stale(key, SLOW_S, slow, {}), closed_today=len(_closed_today(p)),
                gap=_cached(("gap", p.dir), 30, lambda: louhi.gap_series(p)))  # T-3930: the models panel's trend


def board(p):
    """T-3659: the Board's own facts (sampo.board imports this module, so it loads here)."""
    from sampo import board as b
    return b.collect(p)


COLLECTORS = {"project": project, "focus": focus, "queue": queue, "closed_today": closed_today, "lanes": lanes, "jobs": jobs,
              "models": models, "gpu": gpu, "flows": flows, "events": events,
              "stats": lambda p: stats(p, wait=False), "board": board}
EMPTY = {"project": {}, "focus": None, "gpu": None, "stats": {}, "board": {}}  # every other field's empty is []


def snapshot(p):
    """The contract's snapshot; a collector that raises leaves its field empty. Cached CACHE_S seconds per project."""
    def build():
        s = {"at": c.now()}
        for name, fn in COLLECTORS.items():
            try:
                s[name] = fn(p)
            except Exception:  # a collector is cheap and optional; the rest of the page stands
                s[name] = EMPTY.get(name, [])
        return s
    return _cached(("snap", p.dir), CACHE_S, build)


def _slice(p):
    """T-3730: what the All view draws of one project (queue, lanes, train, loop), kept ALL_S seconds."""
    def build():
        from sampo import board as bd
        out = {"queue": queue(p)}
        for name in ("lanes", "train", "loop"):
            try:
                out[name] = getattr(bd, "part_" + name)(p)
            except Exception:
                out[name] = [] if name == "lanes" else None
        return out
    return _cached(("slice", p.dir), ALL_S, build)


def _tag(rows, slug):
    return [{**r, "project": slug} for r in rows or []]


def snapshot_all(default):
    """T-3730: every open project at once. The machine-wide fields are the server's own project's snapshot (built once);
    each other project adds its slice, cached ALL_S seconds. Items carry "project"; a sensitive project is its name
    only, with no items."""
    base = snapshot(default)
    s = dict(base)
    queue_, lanes_, rows, trains, loops = [], [], [], [], []
    for p, meta in c.all_projects():
        name = os.path.basename(p.root.rstrip("/")) or p.slug
        if meta.get("sensitive"):
            rows.append({"slug": p.slug, "name": name, "sensitive": True})
            continue
        rows.append({"slug": p.slug, "name": name, "sensitive": False})
        if p.slug == default.slug:
            b = base.get("board") or {}
            sl = {"queue": base.get("queue"), "lanes": b.get("lanes"), "train": b.get("train"), "loop": b.get("loop")}
        else:
            sl = _slice(p)
        queue_ += _tag(sl["queue"], p.slug)
        lanes_ += _tag(sl["lanes"], p.slug)
        if sl["train"]:
            trains.append({**sl["train"], "project": p.slug})
        if sl["loop"]:
            loops.append({**sl["loop"], "project": p.slug})
    s["project"] = {"slug": "all", "name": "All projects"}
    s["all"] = True
    s["projects"] = rows
    s["queue"] = queue_
    s["lanes"] = [{**r, "project": default.slug} for r in base.get("lanes") or []]
    s["board"] = {**(base.get("board") or {}), "lanes": lanes_, "trains": trains, "loops": loops}
    return s


def machine():
    """T-3023: every registered project and every live session here; a sensitive project is its name and nothing else."""
    def build():
        from sampo import bus
        projects, hidden = [], set()
        for p, meta in c.all_projects():
            name = os.path.basename(p.root.rstrip("/")) or p.slug
            if meta.get("sensitive"):
                hidden.add(p.slug)
                projects.append({"slug": p.slug, "name": name, "sensitive": True})
                continue
            try:
                briefs = c.load_briefs(p)
                b = c.active_brief(briefs, p.lane)
                row = {"slug": p.slug, "name": name, "sensitive": False, "drive": bool(meta.get("drive", True)),
                       "task": b.id if b else None, "stage": c.stage(b, c.effective_autonomy(p, meta)) if b else None,
                       "queue": sum(x.status in c.RUNNABLE for x in briefs)}
            except Exception:  # one unreadable project doesn't hide the rest
                row = {"slug": p.slug, "name": name, "sensitive": False}
            projects.append(row)
        sessions = []
        try:
            live_ = bus.live()
            rows = {r["session"]: r for r in bus.rows(live_)}
        except Exception:
            live_, rows = [], {}
        for s in live_:
            r = rows.get(s["session_id"], {})
            quiet = s.get("project") in hidden
            sessions.append({"id": str(s["session_id"])[:8], "project": s.get("project"),
                             "task": None if quiet else r.get("task"), "model": None if quiet else s.get("model"),
                             "context_pct": s.get("context_pct"), "seen": s.get("ts")})
        return {"at": c.now(), "projects": projects, "sessions": sessions}
    return _cached("machine", CACHE_S, build)


def resolve(default, slug):
    """T-3023: the project ?project= names (None: unknown, or sensitive and not the server's own); no slug: the newest
    live session's project, else the server's own."""
    from sampo import bus
    asked = bool(slug)
    if not asked:
        try:
            slug = next((s["project"] for s in bus.live() if s.get("project")), None)
        except Exception:
            slug = None
    if slug == default.slug:
        return default
    for p, meta in c.all_projects():
        if p.slug == slug:
            if meta.get("sensitive"):
                break
            return p
    return None if asked else default  # a busiest session's project that is gone or hidden: the server's own


_LOOP = {"127.0.0.1": ("0100007F", "0000000000000000FFFF00000100007F"), "::1": ("00000000000000000000000001000000",)}


def peer_uid(caddr, cport, sport, root="/proc", saddr="127.0.0.1"):
    """T-3023: the uid owning the client-side socket of a loopback connection, from root/net/tcp and tcp6 (IPv4-mapped
    rows too). T-3027: the row's local address must be the client's own (caddr:cport) and its remote end the server
    (saddr:sport): matching ports alone let another uid connect from 127.0.0.2 with the user's port number and inherit
    the user's uid. Exactly one such row, else None (the cookie path answers)."""
    mine, server = _LOOP.get(caddr), _LOOP.get(saddr)
    if not mine or not server:
        return None
    hits = []
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(root, "net", name), encoding="ascii", errors="replace") as f:
                lines = f.read(4 << 20).splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            try:
                (la, lp), (ra, rp) = f[1].split(":"), f[2].split(":")
                if la.upper() in mine and int(lp, 16) == cport and ra.upper() in server and int(rp, 16) == sport:
                    hits.append(int(f[7]))
            except (ValueError, IndexError):
                continue
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- the server

PLACEHOLDER = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sampo live</title></head><body>'
               '<h1>Sampo live</h1><p>The page files (plugin/web/live) are not here; /api/state has the data.</p></body></html>')


ACT_MAX, ACT_TIMEOUT, NOTE_MAX = 4096, 30, 2000
SLOW_ACT_TIMEOUT = 150  # sampo resume re-runs the last green check (up to 120 s)


def act_token(server):
    """T-3026: stateless, so no number of page loads evicts it; derived from the server's secret, put in the page's HTML
    only (a cross-origin page can't read it)."""
    return hmac.new(server.session.encode(), b"sampo-live-act", "sha256").hexdigest()
ACTIONS = {  # T-3026: action -> (needs a project, the sampo argv); nothing else runs, and no shell
    "drive_on": (True, ["drive", "on"]), "drive_off": (True, ["drive", "off"]), "checkpoint": (True, ["checkpoint"]),
    "resume": (True, ["resume"]), "pause_now": (False, ["pause", "on", "--now"]), "pause_off": (False, ["pause", "off"]),
    "note": (False, None)}


def _run(argv, p):
    """The exit code of `sampo ARGV` as a child process (no shell, no stdin, a timeout; 124 on it, 127 if it can't start)."""
    try:  # its own process group, so a timeout kills what the verb started too
        child = subprocess.Popen([sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "sampo"), *argv], cwd=p.root,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
    except OSError:
        return 127
    try:
        return child.wait(timeout=SLOW_ACT_TIMEOUT if argv[0] == "resume" else ACT_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
        child.wait()
        return 124


def act(default, body):
    """T-3026: (status, JSON) for a checked POST body {action, project?, session?, text?, confirmed?}. 400 for anything not
    in ACTIONS, an unknown or hidden (sensitive) project or session, a bad note or an unconfirmed pause now; each act that
    ran is a `live_act` ledger row of action, project and exit (a note: its length, never the text)."""
    if not isinstance(body, dict) or not isinstance(body.get("action"), str) or body["action"] not in ACTIONS:
        return 400, {"ok": False, "error": "unknown action"}
    name = body["action"]
    needs, argv = ACTIONS[name]
    if name == "pause_now" and body.get("confirmed") is not True:
        return 400, {"ok": False, "error": "pause now needs confirmed: true"}
    p, data = default, {"action": name}
    if needs:
        slug = body.get("project")
        p = resolve(default, slug) if isinstance(slug, str) and slug else None
        if p is None:
            return 400, {"ok": False, "error": "unknown project"}
    if name == "note":
        from sampo import bus
        text, sid = body.get("text"), body.get("session")
        if not isinstance(text, str) or not text.strip() or len(text) > NOTE_MAX or not isinstance(sid, str) or not sid:
            return 400, {"ok": False, "error": "a note needs a session and 1-2000 characters"}
        try:
            hit = [s for s in bus.live() if str(s.get("session_id", "")).startswith(sid)]
        except Exception:
            hit = []
        hidden = {q.slug for q, meta in c.all_projects() if meta.get("sensitive")}
        if len(hit) != 1 or not hit[0].get("project") or hit[0]["project"] in hidden:
            return 400, {"ok": False, "error": "unknown session"}
        try:
            bus.send(hit[0]["session_id"], text, kind="note", sender="live")
            code = 0
        except Exception:
            code = 1
        data.update(len=len(text), session=str(hit[0]["session_id"])[:8])
    else:
        code = _run(argv + (["-p", p.slug] if needs else []), p)
        data.update(project=p.slug if needs else None)
    data["exit"] = code
    c.log_event(p, "live_act", data=data)
    return 200, {"ok": code == 0, "exit": code, "action": name}


class Handler(settingsserve.Handler):
    CMD, PATHS = "sampo watch --web --url", ()  # the 403 names the command that prints a fresh link

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False), ctype="application/json; charset=utf-8")

    def _page(self):
        nonce = secrets.token_urlsafe(16)
        try:
            with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
                body = f.read().replace("__NONCE__", nonce)
        except OSError:
            body = PLACEHOLDER
        try:  # T-3055: the setting live_theme is the look a browser gets until its viewer picks one
            from sampo import smithtui
            theme = smithtui.load_config()[0].get("live_theme")
        except Exception:
            theme = None
        if theme == "forge":
            body = body.replace('data-theme="modern"', 'data-theme="forge"', 1)
        meta = f'<meta name="sampo-act" content="{act_token(self.server)}">'  # T-3026: the page sends it as X-Sampo-Nonce
        body = body.replace("</head>", meta + "</head>", 1) if "</head>" in body else meta + body
        self._send(200, body, nonce, csp=(f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'self' "
                                          f"'nonce-{nonce}'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
                                          "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"))

    def _static(self, name):
        ext = os.path.splitext(name)[1]
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", name) or ext not in TYPES:
                raise OSError
            with open(os.path.join(WEB, name), "rb") as f:
                data = f.read(4 << 20)
            if ext in (".css", ".js", ".svg", ".json"):
                data = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return self._send(404, "not found")
        self._send(200, data, ctype=TYPES[ext] + ("; charset=utf-8" if ext in (".css", ".js", ".svg", ".json") else ""))

    def _stream(self, p, snap=None):
        snap = snap or snapshot
        srv = self.server
        with srv.once:
            if srv.streams >= STREAMS_MAX:
                return self._send(503, "too many streams")
            srv.streams += 1
        try:
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"), ("Connection", "close"),
                         ("Content-Security-Policy", "default-src 'none'"), ("X-Content-Type-Options", "nosniff")):
                self.send_header(k, v)
            self.end_headers()
            while not srv.stop.is_set():
                self.wfile.write(b"event: state\ndata: " + json.dumps(snap(p), ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
                srv.stop.wait(STREAM_S)
        except (OSError, ValueError):
            pass  # the page went away
        finally:
            with srv.once:
                srv.streams -= 1
            self.close_connection = True

    def _allowed(self):
        """T-3023: a loopback peer whose socket is the server's own uid needs no link (Host is still checked); another uid,
        or none found, takes the token and cookie path. T-3063: a request a proxy has marked (Forwarded, X-Forwarded-*) came
        from beyond this machine however its socket reads, so it is refused outright."""
        if any(h.lower() == "forwarded" or h.lower().startswith("x-forwarded-") for h in self.headers.keys()):
            self._send(403, "refused: forwarded request")
            return None
        if (self.headers.get("Host") or "").lower() in self._mine():
            me = self.server.server_address[1]
            if peer_uid(self.client_address[0], self.client_address[1], me, saddr=self.server.server_address[0]) == os.getuid():
                return urllib.parse.urlsplit(self.path).path
        return super()._allowed()

    def _bad(self, status, why):
        self._send(status, json.dumps({"ok": False, "error": why}), ctype="application/json; charset=utf-8")

    def do_POST(self):
        """T-3026: POST /api/act. The browser's own uid passes _allowed, so these checks, in this order, are the whole CSRF
        defence: JSON only (a cross-site form can't send it without a preflight, which is never answered), Origin the
        server's own, the act token only this page's HTML carries. Nothing runs before all pass.

        Limit (T-3063, measured by test_each_act_refuses_an_attacker_and_runs_for_the_user): a forwarder the user runs
        (ssh -L, a VS Code port forward, socat) makes its remote clients look like the user's uid, so they pass _allowed.
        A forward onto another port or a proxy's header fails the Host / Forwarded checks; a same-port forward that adds
        nothing is a second copy of the user's own browser, and can load the page and its token: that one stays open."""
        path = self._allowed()
        if path is None:
            return
        if path != "/api/act":
            return self._send(404, "not found")
        if self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":  # a browser says cross-site when it is
            return self._bad(403, "refused: cross-site")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._bad(403, "refused: JSON only")
        if (self.headers.get("Origin") or "").lower() not in [f"http://{h}" for h in self._mine()]:
            return self._bad(403, "refused: not posted from this page")
        if not hmac.compare_digest((self.headers.get("X-Sampo-Nonce") or "").encode("utf-8", "replace"),
                                   act_token(self.server).encode()):
            return self._bad(403, "refused: reload the page")
        size = self.headers.get("Content-Length") or ""
        if self.headers.get("Transfer-Encoding") or not (size.isascii() and size.isdigit()):
            return self._bad(411, "a Content-Length is required")
        if int(size) > ACT_MAX:
            return self._bad(413, f"over {ACT_MAX} bytes")
        try:
            body = json.loads(self.rfile.read(int(size)).decode("utf-8"))
        except (ValueError, RecursionError):
            return self._bad(400, "not JSON")
        status, out = act(self.server.project, body)
        self._send(status, json.dumps(out, ensure_ascii=False), ctype="application/json; charset=utf-8")

    def do_GET(self):
        path = self._allowed()
        if path is None:
            return
        if path == "/api/act":
            return self._bad(405, "POST only")
        if path == "/":
            return self._page()
        if path in ("/api/state", "/api/stream", "/api/machine"):
            if path == "/api/machine":
                return self._json(machine())
            slug = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("project", [""])[0]
            if slug == "all":  # T-3730
                return self._json(snapshot_all(self.server.project)) if path == "/api/state" else self._stream(
                    self.server.project, snapshot_all)
            p = resolve(self.server.project, slug)
            if p is None:
                return self._send(404, "unknown project")
            return self._json(snapshot(p)) if path == "/api/state" else self._stream(p)
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        self._send(404, "not found")


def _session(p):
    """T-3021: the cookie's secret outlives a restart (a deploy, the systemd unit): an open tab keeps working. Kept 0600
    in the project's state; the URL's token still works once per start."""
    path = os.path.join(p.dir, "live.session")
    try:
        with open(path, encoding="utf-8") as f:
            s = f.read().strip()
        if len(s) >= 40:
            os.chmod(path, 0o600)
            return s
    except OSError:
        pass
    s = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(s)
    os.chmod(path, 0o600)
    return s


def make(p, port=0):
    """A server bound to 127.0.0.1 (port 0: a random one), not yet serving: (server, the URL's one-time token)."""
    server = settingsserve.Server(("127.0.0.1", port), Handler)
    server.token, server.session = secrets.token_urlsafe(32), _session(p)
    server.once, server.project, server.last = threading.Lock(), p, time.monotonic()
    server.stop, server.streams = threading.Event(), 0
    return server, server.token


def url_path(p):
    return os.path.join(p.dir, "live.url")


def write_url(p, url):
    c.write_atomic(url_path(p), url + "\n")
    os.chmod(url_path(p), 0o600)  # the URL holds a token that opens the page


def _url(server):
    return f"http://127.0.0.1:{server.server_address[1]}/?t={server.token}"


RELOAD_S = 30  # T-3852: how often the server looks for new code of its own


def _code_id():
    """The committed plugin tree's id: changes when a merge changes Sampo's code ('' outside git)."""
    return (c._git(c.PLUGIN_ROOT, "rev-parse", "HEAD:./", timeout=10, fail="") or "").strip()


def _again(argv, port):
    """argv for the re-exec, pinned to the port it serves, so an open tab reconnects (its cookie outlives it, T-3021)."""
    return argv if any(a == "--port" or a.startswith("--port=") for a in argv) else [*argv, "--port", str(port)]


def serve(server, stop=None):
    """Answer requests until `stop` is set; a spent token is replaced and live.url rewritten. No idle timeout.
    T-3852: returns with server.reload set once the committed plugin code changed, for cmd_live to re-exec."""
    stop = server.stop = stop or server.stop
    server.timeout, server.reload = 0.5, False
    code, looked = _code_id(), time.monotonic()
    try:
        while not stop.is_set():
            if time.monotonic() - looked >= RELOAD_S:
                looked = time.monotonic()
                if code and _code_id() not in ("", code):
                    server.reload = True
                    break
            server.handle_request()
            with server.once:
                if server.token is None:
                    server.token = secrets.token_urlsafe(32)
                    if server.project is not None:
                        write_url(server.project, _url(server))
    finally:
        stop.set()  # open streams end
        server.server_close()


def cmd_live(args):
    from sampo import cli
    p = cli.resolve(args, create=False)
    if args.url:
        try:
            with open(url_path(p), encoding="utf-8") as f:
                print(f.read().strip())
            return 0
        except OSError:
            raise cli.UsageError("sampo watch --web isn't running here (sampo watch --web starts it)") from None
    server, _ = make(p, args.port or 0)
    write_url(p, _url(server))
    print(f"Sampo live: {_url(server)}\n(read-only, 127.0.0.1 only; the link works once, `sampo watch --web --url` prints a fresh one; "
          "Ctrl-C stops it)", flush=True)
    stop = threading.Event()
    old = signal.signal(signal.SIGTERM, lambda *a: stop.set())
    try:
        serve(server, stop)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, old)
        try:
            os.remove(url_path(p))
        except OSError:
            pass
    if server.reload and not os.environ.get("FOREMAN_NO_BACKGROUND"):  # T-3852: same pid (systemd's unit), new code
        print("new code: restarting", flush=True)
        os.execv(sys.executable, [sys.executable, *_again(sys.argv, server.server_address[1])])
    print("stopped", flush=True)
    return 0
