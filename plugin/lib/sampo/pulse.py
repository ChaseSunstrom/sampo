"""T-5591: `sampo pulse` is one screen: the machine's load and free disk, the builder lanes, the last day's train, and the
long verbs' jobs (lane land, lane train, lane verify, lane precheck, the full check, task finish: they register in the
jobs registry while they run, T-5591) with the last line of each one's log. `sampo pulse --wait N` returns as soon as a
job ends or the board changes (its digest), and at N seconds otherwise; N is capped at MAX_WAIT_S. It only reads: the
registry, the board's parts and the logs. A wait reads the registry every POLL_S and the board's parts (lanes, train)
every BOARD_S, so the board is not walked on each poll; a job's end is seen within POLL_S, a lane's change within BOARD_S.
Nothing is written."""
import hashlib
import json
import os
import re
import stat
import time

from sampo import core as c
from sampo import jobsreg

MAX_WAIT_S = 170  # a wait never outlasts a tool call's limit (T-3536): the caller asks again
POLL_S = 2.0  # the registry is read this often during a wait
BOARD_S = 10.0  # the board's lanes and train are read this often during a wait (each read walks every lane's files)
JOBS_SHOWN = 12  # the rows of the jobs list; the rest are counted, and `sampo jobs` lists them all
TAIL_N = 5  # the last lines of an ended job's log, in a wait's answer
TAIL_B = 8192  # a log's tail is read, never the whole file
LINE_W = 160
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _tail(path, n):
    """The last n non-blank lines of a log: escape codes and controls out, cut to LINE_W; [] when it is no regular file
    this uid owns (no symlink at the end, no waiting on a FIFO), or it can't be read."""
    if not path or jobsreg.is_secret(str(path)):  # a credential's last lines are never shown (T-5591 review)
        return []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, TypeError, ValueError):
        return []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return []
        os.lseek(fd, max(0, st.st_size - TAIL_B), os.SEEK_SET)
        raw = os.read(fd, TAIL_B)
    except OSError:
        return []
    finally:
        os.close(fd)
    parts = _CTRL.sub(" ", _ANSI.sub("", raw.decode("utf-8", "replace")).replace("\r", "\n").replace("\t", " ")).split("\n")
    if st.st_size > TAIL_B:
        parts = parts[1:]  # the first line of a read from the middle is cut
    lines = [" ".join(x.split()) for x in parts]
    return [c.fit(c.redact(x), LINE_W) for x in lines if x][-n:]


def _log_of(r):
    """A running job's log: its own (registered with one), else the file its process writes to."""
    if r.get("log"):
        return r["log"]
    if r.get("state") == "running" and isinstance(r.get("pid"), int):
        from sampo import jobprog
        return jobprog.log_path(r["pid"])
    return None


def board_parts(p):
    """The board's parts that walk the lanes' files: the lanes and the train (a dict). A wait reads them every BOARD_S."""
    from sampo import board
    return {"lanes": board.part_lanes(p), "train": board.part_train(p)}


def pulse(p, now=None, parts=None):
    """The board, read once: load, disk, the lanes' states, the train's day, and the jobs (manual registrations first,
    then the journal's long jobs that no registration already shows). A dict; the same board gives the same digest.
    parts: the board_parts read earlier (a wait reuses it between its BOARD_S reads), else read now."""
    from sampo import lanes
    now = time.time() if now is None else now
    parts = board_parts(p) if parts is None else parts
    try:
        load = list(os.getloadavg())
    except (OSError, AttributeError):
        load = None
    try:
        free = lanes.disk_usage().free
    except OSError:
        free = None
    rs = jobsreg.rows(p, now)
    running_pids = {r["pid"] for r in rs if r["source"] == "manual" and r["state"] == "running" and r["pid"]}
    rs = [r for r in rs if r["source"] == "manual" or r["pid"] not in running_pids]
    return {"now": now, "load": load, "cores": os.cpu_count(), "disk_free": free,
            "lanes": parts["lanes"], "train": parts["train"], "jobs": rs}


def digest(snap):
    """What changed on the board: the jobs' states and results, the lanes' states and steps, the train's counts. Load,
    disk and the log tails are left out: they move on every poll, and a wait would never sleep."""
    t = snap["train"]
    payload = {"jobs": [[r["id"], r["state"], r["end"], r["result"]] for r in snap["jobs"]],
               "lanes": [[x["task"], x["state"], x["steps"]["done"], x["steps"]["total"]] for x in snap["lanes"]],
               "train": [t.get(k) for k in ("landed_24h", "trains_24h", "minutes_per_lane", "left_out", "running")]}
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def wait(p, secs, sleep=time.sleep, clock=time.time, poll=POLL_S):
    """pulse, then again every `poll` seconds until the digest changes or `secs` (capped at MAX_WAIT_S; none below 0)
    have passed. The lanes and the train are re-read every BOARD_S, not on each poll; the digest uses the last read.
    Returns the board as it is then, with "returned" (changed | capped), "waited" in seconds and "ended": the ids that
    were running at the start and are not now, each with its name and result (a name that is gone is None: the id
    stands for it) and the last TAIL_N lines of its log."""
    try:
        secs = min(max(0.0, float(secs)), float(MAX_WAIT_S))
    except (TypeError, ValueError):
        secs = 0.0
    start = clock()
    parts, read = board_parts(p), clock()
    first = pulse(p, parts=parts)
    base, started = digest(first), {r["id"]: r for r in first["jobs"] if r["state"] == "running"}
    snap, returned = first, "capped"
    while secs - (clock() - start) > 0:
        sleep(min(poll, secs - (clock() - start)))
        if clock() - read >= BOARD_S:
            parts, read = board_parts(p), clock()
        snap = pulse(p, parts=parts)
        if digest(snap) != base:
            returned = "changed"
            break
    now_running, rows = {r["id"] for r in snap["jobs"] if r["state"] == "running"}, {r["id"]: r for r in snap["jobs"]}
    ended = []
    for jid, was in started.items():  # the id diff: a row that vanished (pruned, removed) still shows as ended
        if jid not in now_running:
            r = dict(rows.get(jid) or {"id": jid, "name": None, "state": "gone", "result": None})
            r["tail"] = _tail(_log_of(was), TAIL_N)
            ended.append(r)
    return {**snap, "returned": returned, "waited": round(clock() - start, 1), "ended": ended}


def text(snap):
    """One screen, under 40 lines however many jobs run: the header, the lanes, the train, the jobs (JOBS_SHOWN rows,
    each running one with its log's last line), and a count of the rest."""
    lines = []
    if "returned" in snap:
        if snap["ended"]:
            why = "a job ended: " + c.joinmore([f"{r['name'] or r['id']} ({r['result'] or 'gone'})" for r in snap["ended"]], 3)
        else:
            why = "the board changed" if snap["returned"] == "changed" else "no change"
        lines.append(f"Returned after {snap['waited']:g} s: {why}")
    load = snap.get("load")
    head = (f"load {' '.join(f'{x:.2f}' for x in load)} on {snap.get('cores') or '?'} cores" if load else "load n/a")
    free = snap.get("disk_free")
    lines.append(f"sampo pulse · {head} · " + (f"{free / 1e9:.1f} GB free" if free is not None else "disk n/a"))
    by = {}
    for x in snap["lanes"]:
        by[x["state"]] = by.get(x["state"], 0) + 1
    lines.append(f"lanes: {by.get('building', 0)} building · {by.get('waiting', 0)} waiting · "
                 f"{by.get('parked', 0)} parked · {by.get('merged', 0)} merged in 24h")
    t = snap["train"]
    per = f"{t['minutes_per_lane']} min a lane" if t.get("minutes_per_lane") is not None else "no lane timed yet"
    lines.append(f"train: {t['landed_24h']} landed in 24h by {t['trains_24h']} trains · {per} · "
                 f"{t['left_out']} left out by a red train · running: {'yes' if t['running'] else 'no'}")
    lines.append("")
    rs = snap["jobs"]
    now = snap.get("now")
    run = sum(1 for r in rs if r["state"] == "running")
    lines.append(f"jobs: {run} running, {len(rs) - run} ended or gone in the last day" if rs else
                 "jobs: none running (sampo jobs lists them)")
    for r in rs[:JOBS_SHOWN]:
        lines.append(jobsreg.text([r], now).splitlines()[0])
        if r["state"] == "running":
            tail = _tail(_log_of(r), 1)
            if tail:
                lines.append(f"      last: {tail[0]}")
    if len(rs) > JOBS_SHOWN:
        lines.append(f"... {len(rs) - JOBS_SHOWN} more (sampo jobs lists them)")
    return "\n".join(lines)


def cmd_pulse(args):
    from sampo import cli
    p = cli.resolve(args)
    secs = getattr(args, "wait", None)
    snap = pulse(p) if secs is None else wait(p, secs)
    return cli.out(args, snap, text(snap))
