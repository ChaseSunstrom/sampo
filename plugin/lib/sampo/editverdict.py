"""T-3277 (Moon C-001115): a ruling on each edit at the edit. The ambient run (T-0705) already replays the affected tests
after an edit; this rules each edited file keep (a red went green, or a test linked to the file passed), harmful (a pass
went red) or neutral (nothing links the file, or a red stayed red), one jsonl ledger line per file. The tests give the
ruling for free: no model reads a diff. Files an edit touched wait in ambient/pending until a run takes them. A lane
shares its project's state folder, so each queued line and each ledger row carries the root it was edited in, and a run
rules only its own root's. A taken batch stays as pending.<pid>.<start> until its ledger lines are written (`done`); a
run that raised releases it, and a run that died (or whose pid a new process reuses) leaves it for the next `take` to adopt. The blame is the batch's: one replay
covers every file edited since the last, so each row says how many files it was ruled with and whether a test links it."""
import json
import os

from sampo import core as c

KEEP, NEUTRAL, HARMFUL = "keep", "neutral", "harmful"
LEDGER_MAX = 500  # ledger lines kept (the oldest go first)
BATCH_MAX = 50  # files one run rules; the rest wait for the next run
PENDING_MAX = 64 * 1024  # bytes: a queue no run empties (strained host, ambient run never reached) stops growing
LIST_MAX = 20  # files `sampo check ambient edits` lists


def _dir(p):
    d = os.path.join(p.dir, "ambient")
    os.makedirs(d, exist_ok=True)
    return d


def _links(file, linked):
    """True when a test is linked to the file: `linked` is {file: [tests]}, or one list of tests linking every file."""
    if isinstance(linked, dict):
        linked = linked.get(file)
    return isinstance(linked, (list, tuple, set)) and bool(linked)


def verdict(prev_exit, exit, edited_files, linked_tests):
    """keep / neutral / harmful for an edit of edited_files, ruled on the affected tests' exit before (prev_exit, None
    when there was no earlier run: it counts as a pass, as the flip note does) and after it. harmful: a pass turned red;
    keep: a red turned green, or a linked test passed; neutral: no run, a red that stayed red, or no test links any edited
    file. Pure: it never raises on a malformed linked_tests (read as nothing linked)."""
    if exit is None:
        return NEUTRAL
    if exit and not prev_exit:
        return HARMFUL
    if exit:
        return NEUTRAL
    if prev_exit:
        return KEEP
    return KEEP if any(_links(f, linked_tests) for f in edited_files or []) else NEUTRAL


def _line(root, rel):
    return json.dumps({"r": root, "f": rel}) + "\n"


def _append(path, text):
    """One O_APPEND write, so concurrent hooks never interleave short lines."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def _entries(path):
    """[(root, file)] of a queue file; a line that doesn't parse is dropped. [] when it is not there."""
    try:
        with open(path, errors="replace") as h:
            lines = h.read().splitlines()
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("r"), str) and isinstance(r.get("f"), str) and r["f"]:
            out.append((r["r"], r["f"]))
    return out


def queue(p, path):
    """After an edit: the file waits for the next run in its root to rule it. Paths outside the project are dropped
    (after normpath, so `a/../../x` is outside); no \\n or \\r in a name; one line per edit; bounded."""
    if not isinstance(path, str):
        return
    root = p.root.rstrip("/") + "/"
    path = os.path.normpath(path)
    if not path.startswith(root):
        return
    rel = path[len(root):]
    if not rel or "\n" in rel or "\r" in rel or rel == ".git" or rel.startswith(".git/"):
        return
    f = os.path.join(_dir(p), "pending")
    try:
        if os.path.getsize(f) > PENDING_MAX:
            return
    except OSError:
        pass
    _append(f, _line(p.root, rel))


_PROC = "/proc"  # T-4993: where the process stat files are; a host without it (macOS) asks ps


def _start(pid):
    """The process's start time: clock ticks since boot (/proc/PID/stat field 22); where there is no /proc (macOS,
    T-4993: every start read "" there, so a reused pid held its batch for good) its start in epoch seconds from ps
    (this process's own through core._self_ticks, read once); "" when neither can say."""
    if pid == os.getpid():
        got = c._self_ticks()
        return str(got) if got else ""
    try:
        with open(os.path.join(_PROC, str(pid), "stat")) as f:
            return f.read().rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        pass
    if os.path.isdir(_PROC):  # a /proc without the pid: it is gone
        return ""
    got = c.ps_start(pid)
    return str(got) if got else ""


def _alive(pid, start=""):
    """The process that took a batch is still running: the pid answers, and (when both start times are known) it is
    the same process, not a new one that reused the pid."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except OSError:
        return False
    now = _start(pid)
    return not (start and now and start != now)


def _tag(token=""):
    me = os.getpid()
    return f"{me}.{_start(me)}" + (f".{token}" if token else "")


def _mine(p):
    return os.path.join(_dir(p), f"pending.{_tag()}")


def _owner(name):
    """(pid, start) of the run that holds a pending.<pid>.<start> or adopting.<pid>.<start>.<token> file, else None."""
    parts = name.split(".")
    if parts[0] not in ("pending", "adopting") or len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1]), (parts[2] if len(parts) > 2 else "")


def _requeue(p, path):
    """A held batch goes back to the queue: renamed to a file only this run holds first, so a kill before the unlink
    leaves it for `_adopt` (a repeat at worst, which `take` dedups), never a batch two files own."""
    d = _dir(p)
    tmp = os.path.join(d, f"adopting.{_tag(os.urandom(4).hex())}")
    try:
        os.replace(path, tmp)
    except OSError:
        return  # another run took it first
    text = "".join(_line(r, f) for r, f in _entries(tmp))
    if text:
        _append(os.path.join(d, "pending"), text)
    os.unlink(tmp)


def _adopt(p):
    """A batch whose run was killed (no finally ran) goes back to the queue: a pending.* or adopting.* file whose
    owner is gone, or is another process on a reused pid."""
    d = _dir(p)
    for name in os.listdir(d):
        who = _owner(name)
        if who and who[0] != os.getpid() and not _alive(*who):
            _requeue(p, os.path.join(d, name))


def take(p):
    """This root's queued files (each once, oldest first, at most BATCH_MAX). The queue is emptied by one rename, so an
    edit landing meanwhile starts the next batch; other roots' lines and the files beyond BATCH_MAX go back to the queue.
    The batch stays in pending.<pid> until `done` (the ledger is written) or `release`."""
    release(p)
    _adopt(p)
    f = os.path.join(_dir(p), "pending")
    mine = _mine(p)
    try:
        os.replace(f, mine)
    except OSError:
        return []
    rows = _entries(mine)
    own = list(dict.fromkeys(rel for r, rel in rows if r == p.root))
    keep = own[:BATCH_MAX]
    back = "".join(_line(r, rel) for r, rel in rows if r != p.root) + "".join(_line(p.root, rel) for rel in own[BATCH_MAX:])
    hold = os.path.join(_dir(p), f"adopting.{_tag(os.urandom(4).hex())}")
    if back:
        c.write_atomic(hold, back)  # held by this run until it is queued: a kill in between is adopted, not lost
    c.write_atomic(mine, "".join(_line(p.root, rel) for rel in keep))  # mine no longer holds `back`
    if back:
        _append(f, back)
        os.unlink(hold)
    return keep


def done(p):
    """The taken batch is ruled: it is not queued again."""
    try:
        os.unlink(_mine(p))
    except OSError:
        pass


def release(p):
    """The taken batch was not ruled (the run raised, or the set was skipped or cut): it waits for the next run."""
    _requeue(p, _mine(p))


def linked_for(p, files, tests):
    """{file: the run's tests linked to it}: the map's links, and a test file edited itself. One tests_for over the
    batch picks the candidates, so each file is matched against those alone. Never raises."""
    try:
        from sampo import map as smap
        m = smap.load(p)
        cand = set(smap.tests_for(m, list(files))) & set(tests)
        sub = {"tests": {t: srcs for t, srcs in m["tests"].items() if t in cand}}
        return {f: sorted(set(smap.tests_for(sub, [f])) & cand) + ([f] if f in tests else []) for f in files}
    except Exception:
        return {}


def record(p, files, prev_exit, exit, linked, task, tests=0):
    """One ledger line per edited file with its verdict, the batch size and whether a test links it, and the root it
    was edited in; returns the rows. The ledger keeps its last LEDGER_MAX lines."""
    rows = [{"ts": c.now(), "root": p.root, "file": f, "verdict": verdict(prev_exit, exit, [f], linked), "prev": prev_exit,
             "exit": exit, "batch": len(files), "linked": _links(f, linked), "tests": tests, "task": task}
            for f in files]
    if not rows:
        return rows
    path = os.path.join(_dir(p), "edits.jsonl")
    old = _read(path)
    c.write_atomic(path, "".join(json.dumps(r) + "\n" for r in (old + rows)[-LEDGER_MAX:]))
    return rows


def _read(path):
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    rows = []
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("file"), str):
            rows.append(r)
    return rows


def last(p, n=LIST_MAX):
    """The latest ruling per file of this root, newest first, at most n."""
    seen = {}
    for r in _read(os.path.join(p.dir, "ambient", "edits.jsonl")):
        if r.get("root") != p.root:
            continue
        seen.pop(r["file"], None)
        seen[r["file"]] = r
    return list(reversed(list(seen.values())))[:n]


def render(rows):
    if not rows:
        return "No edits ruled yet (sampo check ambient on, then edit)."
    out = []
    for r in rows:
        n = r.get("batch")
        how = (f"one of {n} in the batch, " if isinstance(n, int) and n > 1 else "") \
            + ("linked" if r.get("linked") else "not linked")
        out.append(f"{c.plain(str(r.get('verdict', '?')))}, {how}  {c.plain(c.fit(r['file'], 80))}  "
                   f"({c.plain(c.fit(str(r.get('ts', '')), 25))})")
    return "\n".join(out)


def names(files, width=80):
    """The edited files, as one short list for a note."""
    return c.plain(c.fit(", ".join(files), width))
