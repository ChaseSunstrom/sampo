"""T-5031 (slice of T-4329): a background job Claude starts by hand registers itself in the running-jobs journal
(recover's state/<project>/running/, T-3928) with a why, so the Board's jobs panel (T-4108) and `sampo jobs` can show it.
`sampo jobs add NAME --why WHY [--pid N] [--log FILE]` writes running/manual-<id>.json (kind "manual"), `sampo jobs end
ID [RESULT]` stamps its end and result, `sampo jobs` lists the manual entries beside this boot's own long jobs. A manual
entry is a record, never a command: it holds no argv a restart could run, recover.entries never returns it, and a
reboot leaves it listed as gone until it ages out a day after it ended. Data layer only: the Board draws it elsewhere."""
import contextlib
import json
import os
import re
import time
import uuid

from sampo import core as c
from sampo import recover

KIND = "manual"
KEEP_S = 24 * 3600  # an ended or gone entry stays listed this long, then the next add/end removes it
MAX_LIVE = 100  # running manual entries per project: a loop that never ends its jobs is refused, not unbounded
_NAME_W, _WHY_W, _RESULT_W = 80, 300, 300
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _clean(text, width):
    """One line, controls gone, cut to width: what a caller typed is stored as data, never as a layout."""
    return c.fit(" ".join(_CTRL.sub(" ", str(text)).split()), width)


def _path(p, jid):
    return os.path.join(recover._dir(p), f"{KIND}-{jid}.json")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError, ValueError):
        return True  # another user's process: it exists
    return True


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            e = json.load(f)
    except (OSError, ValueError):
        return None
    return e if isinstance(e, dict) and e.get("kind") == KIND and isinstance(e.get("id"), str) else None


def _files(p):
    try:
        names = sorted(os.listdir(recover._dir(p)))
    except OSError:
        return []
    return [os.path.join(recover._dir(p), n) for n in names if n.startswith(f"{KIND}-") and n.endswith(".json")]


def state(e, now_boot=None):
    """running | ended | gone (its pid is dead, or the machine rebooted since it was registered, without an end)."""
    if e.get("end"):
        return "ended"
    now_boot = recover.boot() if now_boot is None else now_boot
    if now_boot and e.get("boot") and e["boot"] != now_boot:
        return "gone"
    pid = e.get("pid")
    return "gone" if isinstance(pid, int) and not _alive(pid) else "running"


def _prune(p, now):
    for f in _files(p):
        e = _read(f)
        if e is None:
            continue
        if state(e) == "running":
            continue
        if not e.get("end") and not e.get("gone_at"):
            # first seen gone: the day's grace runs from now, not from when the job started
            with contextlib.suppress(OSError):
                c.write_atomic(f, json.dumps({**e, "gone_at": now}))
            continue
        if now - max(float(e.get("end") or 0), float(e.get("gone_at") or 0)) > KEEP_S:
            with contextlib.suppress(OSError):
                os.remove(f)


def add(p, name, why, pid=None, log=None):
    """Register a hand-started job; its entry. A name and a why are required (the why is the point: the Board's row
    says what the job is for). pid: the job's process, when it has one (without it, the job runs until `end`)."""
    name, why = _clean(name or "", _NAME_W), _clean(why or "", _WHY_W)
    if not name or not why:
        raise ValueError("a job needs a name and a why (--why 'what it is for')")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid < 1):
        raise ValueError(f"pid must be a positive process id, not {pid!r}")
    now = time.time()
    with c.lock(recover._dir(p)):
        _prune(p, now)
        if sum(1 for f in _files(p) if (e := _read(f)) and state(e) == "running") >= MAX_LIVE:
            raise ValueError(f"{MAX_LIVE} manual jobs are still running: end the finished ones (sampo jobs end ID)")
        jid = uuid.uuid4().hex[:6]
        while os.path.exists(_path(p, jid)):
            jid = uuid.uuid4().hex[:6]
        e = {"kind": KIND, "id": jid, "name": name, "why": why, "argv": [], "cwd": os.getcwd(), "pid": pid,
             "log": os.path.abspath(str(log)) if log else None, "boot": recover.boot(), "project": p.slug,
             "start": now, "end": None, "result": None}
        c.write_atomic(_path(p, jid), json.dumps(e))
    return e


def _find(p, ident):
    ident = str(ident or "").strip()
    hits = [(f, e) for f in _files(p) if (e := _read(f)) and ident and e["id"].startswith(ident)]
    exact = [h for h in hits if h[1]["id"] == ident]
    hits = exact or hits
    if not hits:
        raise KeyError(f"no manual job {ident!r} (sampo jobs lists them)")
    if len(hits) > 1:
        raise ValueError(f"{ident!r} matches {', '.join(e['id'] for _, e in hits)}: name one")
    return hits[0]


def end(p, ident, result=""):
    """Stamp the job's end and result (an id or its unique prefix); its entry. A second end keeps the first result."""
    with c.lock(recover._dir(p)):
        f, e = _find(p, ident)
        if e.get("end"):
            return {**e, "again": True}
        now = time.time()
        e = {**e, "end": now, "result": _clean(result, _RESULT_W) or "done"}
        c.write_atomic(f, json.dumps(e))
        _prune(p, now)
    return e


def rows(p, now=None):
    """What `sampo jobs` and the Board's jobs panel read: manual entries (state running/ended/gone, name, why, pid, log,
    result, start, end) then this boot's own long jobs (kind land/train/run/night/ship, no why), running ones first."""
    now = time.time() if now is None else now
    boot = recover.boot()
    out = []
    for f in _files(p):
        e = _read(f)
        if e is not None:
            out.append({**{k: e.get(k) for k in ("id", "kind", "name", "why", "pid", "log", "cwd", "start", "end", "result")},
                        "state": state(e, boot), "source": "manual"})
    for e in recover.entries(p):
        if not boot or e.get("boot") in (boot, "", None):
            pid = e.get("pid")
            out.append({"id": os.path.basename(e["file"])[:-5], "kind": e["kind"], "name": "sampo " + " ".join(e["argv"]),
                        "why": None, "pid": pid, "log": None, "cwd": e.get("cwd"), "start": e.get("start"), "end": None,
                        "result": None, "state": "running" if not isinstance(pid, int) or _alive(pid) else "gone",
                        "source": "journal"})
    order = {"running": 0, "gone": 1, "ended": 2}
    return sorted(out, key=lambda r: (order[r["state"]], -(r.get("start") or 0)))


def _ago(t, now):
    s = max(0, int(now - (t or now)))
    return f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"


def text(rs, now=None):
    now = time.time() if now is None else now
    if not rs:
        return "No background jobs registered (sampo jobs add NAME --why 'what it is for')."
    lines = []
    for r in rs:
        tail = f" -> {r['result']}" if r["state"] == "ended" else ""
        why = f" · {r['why']}" if r["why"] else ""
        pid = f" pid {r['pid']}" if r["pid"] else ""
        age = _ago(r["end"] if r["state"] == "ended" else r["start"], now)
        lines.append(f"{r['id']}  {r['state']:<7} {age:>4}  {r['name']}{why}{pid}{tail}")
    return "\n".join(lines)


def cmd_jobs(args):
    from sampo import cli
    p = cli.resolve(args)
    action, words = getattr(args, "action", None) or "ls", list(getattr(args, "words", None) or [])
    if action == "add":
        if not words:
            raise cli.UsageError("sampo jobs add NAME --why 'what it is for' [--pid N] [--log FILE]")
        try:
            e = add(p, " ".join(words), getattr(args, "why", None), getattr(args, "pid", None), getattr(args, "log", None))
        except ValueError as err:
            raise cli.UsageError(str(err))
        return cli.out(args, e, f"Registered {e['id']}: {e['name']} · {e['why']}")
    if action == "end":
        if not words:
            raise cli.UsageError("sampo jobs end ID [RESULT]")
        try:
            e = end(p, words[0], " ".join(words[1:]))
        except (KeyError, ValueError) as err:
            raise cli.UsageError(err.args[0])
        if e.get("again"):
            return cli.out(args, e, f"{e['id']} had already ended ({e['result']}); kept.")
        return cli.out(args, e, f"Ended {e['id']}: {e['result']}")
    rs = rows(p)
    return cli.out(args, {"jobs": rs}, text(rs))
