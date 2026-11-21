"""T-5034: what each background job row on the live page says about its own run (a slice of T-4329).

live.jobs lists the loops a process list shows. rows() gives each one, beyond its name and pid:
  elapsed  seconds since it started (a finished job's: start to the time its end was seen)
  typical  the ledger's median for that kind of run, the rule selfcheck.slow_jobs uses (selfcheck.typical_s: the last 20
           passing runs of the last 7 days, none under 3 runs); only a check has one: the other loops record no seconds
  ratio    elapsed / typical, rounded to 2 places, None without a typical
  last     the last line of the job's own log: the regular file on its stdout (else stderr), its tail only, cleaned
A job that is gone from the list stays as an ended row for ENDED_KEEP_S (at most ENDED_MAX): the row as last seen plus
  ended    when its end was seen (an iso time; a page that looks every 2 s sees it within that)
  result   "passed" or "failed" for a check when exactly one non-train check_run lies between its start and end, else "ended": the exit code of a
           process that isn't ours to wait on is unknowable, so nothing is guessed
The page's own jobs list shows running rows only (jobs(p) default); the panel that draws ended ones asks jobs(p, ended=True).
Nothing here is written: the remembered rows live in this process, bounded by the process list and ENDED_MAX."""
import os
import re
import stat
import threading
import time

from sampo import core as c

LINE_W, TAIL_B = 120, 8192  # a line is cut to LINE_W characters; the tail read is TAIL_B bytes
ENDED_KEEP_S, ENDED_MAX = 600, 8
SEEN_GAP_S = 60  # a job gone longer than this since the last look ended when last seen, not when its absence was noticed
RUN_SLACK_S = 10  # a check logs its run just before it exits: the ledger line may land this long after the last look
TYPICAL_TTL_S = 30  # the ledger read behind a typical is shared by every page this long
EVENT_OF = {"check": "check_run"}  # live job kind -> the ledger event that records its finished runs' seconds
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LOCK = threading.Lock()
_SEEN = {}  # (project dir, pid) -> (the row as last built, its log path, the epoch of that look)
_GONE = {}  # (project dir, pid, since) -> (ended row, the epoch its end was seen): kept ENDED_KEEP_S
_TYPICAL = {}  # (project dir, event) -> (monotonic time read, seconds or None)


def reset():
    """Forget every remembered row and typical (tests)."""
    with _LOCK:
        _SEEN.clear()
        _GONE.clear()
        _TYPICAL.clear()


def typical(p, kind, now):
    """The kind's typical seconds from the ledger (selfcheck's median), None where the ledger has none. One read per
    TYPICAL_TTL_S, shared; a failing read is None."""
    event = EVENT_OF.get(kind)
    if not event:
        return None
    key = (getattr(p, "dir", None), event)  # no project (a bare find_project before init): no ledger, so no typical
    with _LOCK:
        hit = _TYPICAL.get(key)
        if hit and time.monotonic() - hit[0] < TYPICAL_TTL_S:
            return hit[1]
    from sampo import selfcheck
    try:
        val = selfcheck.typical_s(p, event, now)
    except Exception:  # an optional field: the row is still a row
        val = None
    with _LOCK:
        _TYPICAL[key] = (time.monotonic(), val)
    return val


def log_path(pid):
    """The file a job's stdout (else stderr) is redirected to, or None: only a regular absolute path that still exists
    (not a pipe, a tty, a socket, or a deleted file)."""
    for fd in (1, 2):
        try:
            path = os.readlink(f"/proc/{int(pid)}/fd/{fd}")
        except (OSError, ValueError, TypeError):
            continue
        if path.startswith("/") and not path.startswith(("/dev/", "/proc/")) and not path.endswith(" (deleted)"):
            return path
    return None


def last_line(path):
    """The last non-blank line of a log, escape codes and control characters out, cut to LINE_W; None when there is none
    or the path is no regular file this uid owns (no symlink at the end, no waiting on a FIFO). Reads the tail only,
    through one fd; a progress bar's carriage returns count as line ends."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, TypeError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None
        os.lseek(fd, max(0, st.st_size - TAIL_B), os.SEEK_SET)
        raw = os.read(fd, TAIL_B)
    except OSError:
        return None
    finally:
        os.close(fd)
    text = _CTRL.sub(" ", _ANSI.sub("", raw.decode("utf-8", "replace")).replace("\r", "\n").replace("\t", " "))
    for line in reversed(text.split("\n")):
        line = " ".join(line.split())
        if line:
            return c.fit(c.redact(line), LINE_W)  # a log can hold a token: the page is local, still not a place for it
    return None


def _epoch(row):
    t = c.parse_ts(row.get("since"))
    return t.timestamp() if t else None


def _shape(p, row, now, last, end=None):
    """The row with elapsed, typical, ratio and last (end: the epoch of its end seen, else it is running at `now`)."""
    start = _epoch(row)
    elapsed = max(0, round((end if end is not None else now) - start)) if start is not None else None
    typ = typical(p, row.get("kind"), now)
    return dict(row, elapsed=elapsed, typical=round(typ) if typ else None,
                ratio=round(elapsed / typ, 2) if typ and elapsed is not None else None, last=last)


def _result(p, row, end=None):
    """"passed" or "failed" from the one check_run (not a train's) between the job's start and its end, else "ended"."""
    start = _epoch(row)
    slug = re.search(r"(?:^|\s)(?:-p|--project)(?:\s+|=)(\S+)", str(row.get("line") or ""))
    if row.get("kind") != "check" or start is None or end is None or (slug and slug.group(1) != getattr(p, "slug", None)):
        return "ended"  # another project's check is not judged by this ledger
    try:
        runs = c.ledger_events(p, ["check_run"], since=c.iso(start))
    except Exception:
        return "ended"
    upto = c.iso(end + RUN_SLACK_S)
    runs = [r for r in runs if "train" not in (r.get("data") or {}) and str(r.get("ts") or "") <= upto]
    if len(runs) != 1:  # none, or several (a lane's or another session's among them): which was this job's is unknown
        return "ended"
    d = runs[0].get("data") or {}
    return "failed" if d.get("exit") or any((r or {}).get("exit") for r in d.get("results") or [] if isinstance(r, dict)) else "passed"


def _end(p, row, log, at, now):
    out = _shape(p, row, now, last_line(log) or row.get("last"), end=at)
    out.update(ended=c.iso(at), result=_result(p, row, at))
    return out


def rows(p, live_rows, now, ended=False):
    """live_rows (live.jobs' running rows, each with name, kind, pid and since) with elapsed, typical, ratio and last
    added; with ended=True the recently ended rows of this project follow them. A row's pid gone from the list, or the
    pid held by a job that started later, makes the old one an ended row, fixed at that look."""
    me = getattr(p, "dir", None)
    now_pids = {r["pid"]: r for r in live_rows if isinstance(r.get("pid"), int)}
    out = []
    with _LOCK:
        gone = [(k, v) for k, v in _SEEN.items() if k[0] == me and (k[1] not in now_pids or now_pids[k[1]].get("since") != v[0].get("since"))]
        for k, _ in gone:
            _SEEN.pop(k, None)
    for k, (row, log, seen) in gone:
        at = seen if now - seen > SEEN_GAP_S else now
        done = _end(p, row, log, at, now)
        with _LOCK:
            _GONE[k + (row.get("since"),)] = (done, at)  # a reused pid's two jobs are two rows
    for r in live_rows:
        log = log_path(r.get("pid")) if isinstance(r.get("pid"), int) else None
        row = _shape(p, r, now, last_line(log))
        out.append(row)
        if isinstance(r.get("pid"), int):
            with _LOCK:
                _SEEN[(me, r["pid"])] = (row, log, now)
    with _LOCK:
        for k, (_, at) in list(_GONE.items()):
            if now - at > ENDED_KEEP_S:
                del _GONE[k]
        mine = sorted(((k, v) for k, v in _GONE.items() if k[0] == me), key=lambda kv: -kv[1][1])
        for k, _ in mine[ENDED_MAX:]:
            del _GONE[k]
        kept = [v[0] for _, v in mine[:ENDED_MAX]]
    return out + kept if ended else out
