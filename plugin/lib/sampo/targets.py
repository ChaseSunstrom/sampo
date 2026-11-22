"""T-5709: outcome targets. plugin/data/targets.json is the table (a number is a data edit); each row names a reader.

A reader makes ONE bounded read of ONE file and returns the actual (or None: unmeasured, never ok):
  usage_run_out   state/burn.json           the burn forecast's hours before the reset (cost.burn_findings)
  cpu             state/cpu.json            the samples of the last WINDOW_S, as % of the machine's cores
  the rest        <project>/targets.json    the values refresh() derived from the ledger, the cost cache and the bench
                                            results (and the notes on them)
  HEAVY (T-5973)  <project>/targets-heavy.json  push_lag, lead_context, compactions, disk_growth, steers and
                                            tokens_per_change (from cost.per_lane): what refresh_heavy() read
refresh() does the scans. It runs from the self-check (at most once per CACHE_S, never inside a reader), so the Stop
hook's readers stay small whatever the ledger holds. One scan per project at a time. A target that fails its reading is
"unmeasured", never ok. state/burn.json and state/cpu.json are the machine's; the rest are the project's.
Every derived value is read with the time it was derived: older than LIGHT_STALE_S it is "unmeasured" and the row's
note names the source that went stale (SOURCES), never a stale ok.
The HEAVY readers walk transcripts, git and the disk: far past the Stop hook's 5 s. refresh() and a hand-run `sampo targets`
start `sampo targets --refresh-heavy` detached (under nice, its exit line in state/logs/targets-heavy-<project>.log; skipped
under FOREMAN_NO_BACKGROUND, where `sampo targets` runs the due ones itself, as `--refresh-heavy` always does), each at its
own pace (its period), one run at a time per project, each reader under its own READER_BUDGET_S deadline. The three that read
transcripts (lead_context, compactions, tokens_per_change) share ONE walk of them (_pass). A value nobody refreshed for
STALE_X periods is "unmeasured" again.
"""
import calendar
import contextlib
import fcntl
import glob
import json
import math
import os
import re
import shutil
import signal
import statistics
import sys
import threading
import time

from sampo import core as c
from sampo import cost, cpu

CACHE_S = 300  # refresh() redoes the derived values at most this often
WINDOW_S = 14 * 86400  # the derived values look back this far
DAY_S = 86400
BURN_FRESH_S = DAY_S  # one freshness rule for the run-out forecast: the target reads it and the capture files on it
PREFIX = "Outcome target regressed: "
BENCH_FILES = 40  # the newest bench results the ratio reads (a refresh every CACHE_S must stay small)
SCAN_LOCK = "targets.lock"  # the project's: one derive at a time; a refresh that finds it held returns the file as it is
FILED_FILE = "targets-filed.json"  # the project's: when each target last filed a capture (a record, not a brief's title)
FILED_LOCK = "targets-filed.lock"
FILE_GAP_S = DAY_S  # a target files at most once in this long, whatever became of its capture (folded, held, refused)
HEAVY_FILE = "targets-heavy.json"  # the project's: what refresh_heavy() read, each value with the time it was read
HEAVY_LOCK = "targets-heavy.lock"  # one heavy run at a time per project; a run that finds it held does nothing
HEAVY_SPAWN = "targets-heavy.spawned"  # when refresh() last started the detached run (a number, the clock it was given)
HEAVY_SPAWN_S = 600  # refresh() starts the detached run at most this often, whether or not it did its work
HEAVY_BUDGET_S = 180  # one run starts no new reader after this long
READER_BUDGET_S = 90  # one heavy reader's own deadline: a reader still going then is cut and its key is unmeasured
STALE_X = 4  # a heavy value older than this many of its periods is "unmeasured", not a stale ok
LIGHT_STALE_S = 6 * 3600  # a derived value (targets.json) older than this is "unmeasured": refresh() redoes them every CACHE_S
SOURCES = {"accept_to_merge": "the ledger scan", "rework_rounds": "the ledger scan",
           "tokens_per_change": "the cost cache", "sampo_vs_bare": "the bench results"}  # what each derived value is read from
PUSH_MAX = 5000  # git commits push_lag reads (the newest of the trunk's, the oldest of cherry's); more is "at least"
STEER_FRESH_S = DAY_S  # the steer meter's newest row must be this recent, else its record has stopped and there is no rate
STEER_MIN_SPAN_S = DAY_S  # a rate over less than a day is no rate: the span is at least this
PUSH_PERIOD_S, SLOW_PERIOD_S, DISK_PERIOD_S = 3600, 6 * 3600, DAY_S
LEAD_WINDOW_S = 3 * DAY_S  # the lead's turns it takes the median over
LEAD_MIN_TURNS = 5  # fewer lead turns than this is no median
COMPACT_WINDOW_S = 7 * DAY_S
STEER_WINDOW_S = 7 * DAY_S
PER_LANE_DAYS = 14  # the same 14 days the light values look back
GIT_S = 20  # one git call's timeout
DISK_BUDGET_S = 30  # the footprint's own clock (tidy.footprint)
DISK_WINDOW_S = 7 * DAY_S  # footprint samples kept, and the span growth is read over
DISK_MIN_SPAN_S = 6 * 3600  # two samples nearer than this are no growth rate
DISK_KEEP = 60
TABLE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "targets.json")
_RUN_OUT = re.compile(r"run out (\d+(?:\.\d+)?) h before")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _epoch(ts):
    try:
        return float(calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return None


def _read(path):
    """One file, read whole: a dict, or None when it is missing or not one."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _fresh(d, now):
    return d is not None and _num(d.get("ts")) and 0 <= now - d["ts"] < CACHE_S


def _age_h(seconds):
    return int(seconds // 3600)


def _light(p, key, now):
    """(actual or None, note or None) of one derived key from the project's targets.json: its values, only while the file's
    own time is recent (LIGHT_STALE_S, and not ahead of the clock by more than CACHE_S). An old or time-less file reads
    unmeasured, and the note names the source the value came from (SOURCES). No file: (None, None), as before it was derived."""
    d = _read(os.path.join(p.dir, "targets.json"))
    if d is None:
        return None, None
    src = SOURCES.get(key, "the derived scan")
    at = d.get("ts")
    if not _finite(at):
        return None, f"{src} (targets.json) has no time on it: not trusted"
    if now - at > LIGHT_STALE_S:
        return None, f"{src} (targets.json) was last read {_age_h(now - at)} h ago: too old to trust"
    if at - now > CACHE_S:
        return None, f"{src} (targets.json) is stamped ahead of the clock: not trusted"
    v = _dict(d.get("values"))
    n = _dict(v.get("notes")).get(key)
    return (float(v[key]) if _finite(v.get(key)) else None), (n if isinstance(n, str) else None)


def load(path=TABLE):
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str) and r.get("reader")]


# ----- readers: one file each -----

def run_out_forecast(now):
    """The burn forecast's run-out rows (state/burn.json, cost.burn_findings) as (text, hours): the one with the most hours
    (hours None when its text has none). (None, 0.0) when the forecast has no run-out row; None when it is missing or
    older than BURN_FRESH_S. The target and the capture both read it here."""
    d = _read(os.path.join(c.state_dir(), "burn.json"))
    if d is None or not _num(d.get("ts")) or now - d["ts"] > BURN_FRESH_S:
        return None
    rows = []
    for row in d.get("found") or []:
        if not (isinstance(row, list) and row and isinstance(row[0], str) and "run out" in row[0]):
            continue
        m = _RUN_OUT.search(row[0])
        text = f"{row[0]} → {row[1]}" if len(row) > 1 else row[0]
        rows.append((text, float(m.group(1)) if m else None))
    if not rows:
        return (None, 0.0)
    known = [r for r in rows if r[1] is not None]
    return max(known, key=lambda r: r[1]) if known else rows[0]


def usage_run_out(p, now):
    r = run_out_forecast(now)
    return None if r is None or r[1] is None else r[1]


def cpu_pct(p, now):
    d = _read(os.path.join(c.state_dir(), "cpu.json"))
    samples = d.get("samples") if d and isinstance(d.get("samples"), dict) else {}
    total, seen = 0.0, False
    for rows in samples.values():
        rows = sorted(r for r in rows or [] if isinstance(r, list) and len(r) == 2 and _num(r[0]) and _num(r[1])
                      and r[0] >= now - cpu.WINDOW_S)
        if len(rows) < 2 or rows[-1][0] <= rows[0][0] or rows[-1][1] < rows[0][1]:
            continue
        total += (rows[-1][1] - rows[0][1]) / 1e6 / (rows[-1][0] - rows[0][0])  # the counter is in microseconds
        seen = True
    return total / cpu.ncores() * 100 if seen else None


def _derived(key):
    return lambda p, now: _light(p, key, now)[0]


# ----- the heavy tier: read by refresh_heavy (detached), never by a reader -----

_DAY = re.compile(r'"timestamp":\s*"(\d{4}-\d\d-\d\d)T')
_COMPACTED = re.compile(r'"isCompactSummary":\s*true')


def _finite(v):
    return _num(v) and math.isfinite(v)


def _dict(v):
    return v if isinstance(v, dict) else {}


class _Deadline(BaseException):
    """A heavy reader ran past READER_BUDGET_S. A BaseException: a reader's own `except Exception` must not swallow it."""


class _Run:
    """One refresh_heavy run: what its readers share (`memo`: the transcript walk, a failure of it too, so the next reader
    does not walk again to fail the same way) and the deadline of the reader now running (`until`, a monotonic time)."""

    def __init__(self):
        self.memo, self.until = {}, None

    def check(self):
        """Between two steps of a reader: raises _Deadline when its time is up (the alarm covers a step that blocks)."""
        if self.until is not None and time.monotonic() > self.until:
            raise _Deadline


@contextlib.contextmanager
def _deadline(run, budget):
    """`budget` seconds for what runs inside, as `run.until` and, where there is one to take, a SIGALRM (main thread, no alarm
    or handler of anyone else's set) that interrupts a read or a git call blocked past it; a subprocess.run it lands in kills
    its child. Both end with the block."""
    run.until = time.monotonic() + budget
    armed = False
    try:
        if threading.current_thread() is threading.main_thread() and hasattr(signal, "setitimer") \
                and signal.getsignal(signal.SIGALRM) in (signal.SIG_DFL, None) and signal.getitimer(signal.ITIMER_REAL)[0] == 0:
            def fire(*_):
                raise _Deadline
            signal.signal(signal.SIGALRM, fire)
            armed = True
            signal.setitimer(signal.ITIMER_REAL, max(budget, 0.001))
    except (ValueError, OSError):
        pass
    try:
        yield
    finally:
        run.until = None
        if armed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:  # the alarm landing here must not leave its handler set for the next reader
                signal.signal(signal.SIGALRM, signal.SIG_DFL)


def push_lag(p, now, h, run):
    """Hours since the oldest commit on the main checkout's trunk that origin/main lacks (git cherry, oldest first: a commit
    whose patch is there already, rebased or amended, is not lacking; a merge commit has no patch, so it counts while it is not
    on origin/main). The time is the trunk's own: a lane's commit counts from the merge that put it there, so the first-parent
    log is read. Bounded by PUSH_MAX: of more trunk commits than that, the OLDEST PUSH_MAX are read (cherry lists oldest
    first too; the lag is the oldest lacking one) and the count says "N+"; when none of those lacks, past the cap, it is
    unmeasured, not 0. 0 when the trunk is all pushed; None with no origin/main (no remote, never fetched)."""
    root = c.main_worktree(p.root) or p.root
    ref = "refs/remotes/origin/main"
    if c._git(root, "rev-parse", "--verify", "-q", ref, timeout=GIT_S, proc=True).returncode != 0:
        return None, "no origin/main to compare the trunk with (no origin remote, or it was never fetched)"
    count = c._git(root, "rev-list", "--count", "--first-parent", f"{ref}..HEAD", timeout=GIT_S, proc=True)
    cherry = c._git(root, "cherry", ref, "HEAD", timeout=GIT_S, proc=True)
    if count.returncode != 0 or cherry.returncode != 0 or not count.stdout.strip().isdigit():
        return None, "git could not compare the trunk with origin/main"
    skip = max(0, int(count.stdout) - PUSH_MAX)  # the log is newest first: skip the newest, keep the oldest PUSH_MAX
    log = c._git(root, "log", "--first-parent", f"--skip={skip}", f"--max-count={PUSH_MAX}", "--format=%H%x09%ct%x09%P",
                 f"{ref}..HEAD", timeout=GIT_S, proc=True)
    if log.returncode != 0:
        return None, "git could not compare the trunk with origin/main"
    lacking = set()
    for x in cherry.stdout.splitlines():  # oldest first: the cap keeps the oldest
        if x.startswith("+ "):
            lacking.add(x[2:].strip())
            if len(lacking) >= PUSH_MAX:
                break
    times = []
    for line in log.stdout.splitlines():
        sha, _, rest = line.partition("\t")
        stamp, _, parents = rest.partition("\t")
        if stamp.isdigit() and (len(parents.split()) > 1 or sha in lacking):
            times.append(int(stamp))
    more = "+" if skip else ""
    if not times:
        return (None, f"more than {PUSH_MAX} commits ahead of origin/main, and the oldest {PUSH_MAX} of them are all there by "
                      "patch: not read further") if skip else (0.0, None)
    return (max(0.0, (now - min(times)) / 3600),
            f"{len(times)}{more} commit{'s' * (len(times) != 1)} not on origin/main yet")


def _walk(p, now, run):
    """ONE walk of the project's transcripts (its folder and its lane worktrees', no subagent's), each file opened once and
    each line read once: the lead's replies since the per-lane window (cost.lead_reply), the compaction summaries (once each,
    by uuid across files: a resumed session's copy is the same summary) and the session-days of the last COMPACT_WINDOW_S.
    A file that can't be read is skipped. -> {replies: [(ts, usage, model)], days: {(file, day)}, found: {uuid or (file, line)}}"""
    base = min(now, time.time())
    reach = (PER_LANE_DAYS + cost.PER_LANE_LOOKBACK_DAYS) * DAY_S
    wide = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(base - reach))
    first_day = time.strftime("%Y-%m-%d", time.gmtime(now - COMPACT_WINDOW_S))
    replies, days, found = {}, set(), set()
    for folder in cost.project_folders(p):
        for d, dirs, names in os.walk(folder):
            dirs[:] = [x for x in dirs if x != "subagents"]
            for n in names:
                run.check()
                path = os.path.join(d, n)
                if not n.endswith(".jsonl") or cost.mtime(path) < base - reach:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        for k, line in enumerate(f):
                            if k % 2000 == 0:
                                run.check()
                            i = line.rfind('"timestamp"')  # the entry's own stamp is its last
                            m = _DAY.match(line, i) if i >= 0 and not line.startswith(cost.BLOB) else None
                            if not m or m[1] < wide[:10]:
                                continue
                            recent = m[1] >= first_day
                            if recent:
                                days.add((path, m[1]))
                            usage, summary = '"usage"' in line, recent and _COMPACTED.search(line)
                            if not (usage or summary):
                                continue
                            try:
                                e = json.loads(line)
                            except ValueError:
                                continue
                            if not isinstance(e, dict):
                                continue
                            if summary and e.get("isCompactSummary") is True:
                                found.add(e["uuid"] if isinstance(e.get("uuid"), str) else (path, k))
                            if usage and (got := cost.lead_reply(path, e, wide)):
                                replies[got[0]] = got[1]
                except OSError:
                    continue
    return {"replies": list(replies.values()), "days": days, "found": found}


def _pass(p, now, run):
    """The run's one transcript walk (_walk), made by the first reader that asks. A walk that failed fails the same way for
    the readers after it, at once: it is not walked again for each."""
    if "pass" not in run.memo:
        try:
            run.memo["pass"] = _walk(p, now, run)
        except BaseException as e:  # noqa: BLE001 — kept for the next reader, and raised for this one
            run.memo["pass"] = e
            raise
    got = run.memo["pass"]
    if isinstance(got, BaseException):
        raise got
    return got


def lead_context(p, now, h, run):
    """The median cache-read tokens of the lead's turns (the main thread's: a subagent's are left out) over LEAD_WINDOW_S."""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - LEAD_WINDOW_S))
    reads = [u["cache_read_input_tokens"] for ts, u, _m in _pass(p, now, run)["replies"]
             if ts[:19] >= since and _finite(u.get("cache_read_input_tokens"))]
    if len(reads) < LEAD_MIN_TURNS:
        return None, f"{len(reads)} lead turns in 3 days: too few for a median"
    return float(statistics.median(reads)), f"median of {len(reads)} lead turns in 3 days"


def compactions(p, now, h, run):
    """Compaction summaries (the entries Claude Code marks isCompactSummary, once each, whichever transcript holds them) per
    session-day: a session file's days with an entry in the last COMPACT_WINDOW_S. A subagent's transcript is not a session.
    None when no session had a day."""
    g = _pass(p, now, run)
    days, found = g["days"], g["found"]
    if not days:
        return None, "no session had a message in the last 7 days"
    return len(found) / len(days), f"{len(found)} compaction summaries over {len(days)} session-days in 7 days"


def steers(p, now, h, run):
    """The avoidable steers the steer meter read, per day: the avoidable ones of the last STEER_WINDOW_S over the days its rows
    actually cover (from its oldest row to now, at least STEER_MIN_SPAN_S, at most STEER_WINDOW_S: a meter that started
    yesterday is not read as a week of quiet). Machine text and a compaction summary are left out of its rows. None when it
    read no message, or its newest is older than STEER_FRESH_S (its record has stopped: a rate over a gap is no rate)."""
    from sampo import steermeter
    rs = steermeter.read_rows(steermeter.rows(p))
    stamps = [t for t in (_epoch(r["at"]) for r in rs) if t is not None]
    if not stamps:
        return None, "the steer meter has read no message yet"
    if now - max(stamps) > STEER_FRESH_S:
        return None, f"the steer meter's newest message is {_age_h(now - max(stamps))} h old: its record has stopped"
    since = c.iso(now - STEER_WINDOW_S)
    got = [r for r in rs if r["at"] >= since]
    span = min(STEER_WINDOW_S, max(now - min(stamps), STEER_MIN_SPAN_S))
    n = sum(1 for r in got if r["avoidable"])
    return n * DAY_S / span, f"{n} avoidable steers of {len(got)} messages read in {round(span / DAY_S, 1):g} days"


def disk_growth(p, now, h, run):
    """MB per day Sampo's own paths grew (tidy --report's total over every project), from the samples this reader keeps
    in `h` (one per run, DISK_WINDOW_S of them): the newest against the oldest. A footprint the clock cut short is a lower
    bound, so it is not a sample; one sample, or two nearer than DISK_MIN_SPAN_S, is no rate."""
    from sampo import tidy
    rep = tidy.footprint([q for q, _ in c.all_projects()], DISK_BUDGET_S)
    if rep.get("unsized") or not _finite(rep.get("total_bytes")):
        return None, "Sampo's footprint was not fully sized in time, so it is not a sample"
    kept = [s for s in h.get("disk") or [] if isinstance(s, list) and len(s) == 2 and _finite(s[0]) and _finite(s[1])
            and 0 <= now - s[0] < DISK_WINDOW_S]
    h["disk"] = (kept + [[now, rep["total_bytes"]]])[-DISK_KEEP:]
    (t0, b0), (t1, b1) = h["disk"][0], h["disk"][-1]
    if t1 - t0 < DISK_MIN_SPAN_S:
        return None, "one footprint sample so far: growth needs two, hours apart"
    return (b1 - b0) / 1e6 / ((t1 - t0) / DAY_S), f"{tidy.human(b1)} now, from {tidy.human(b0)} {int((t1 - t0) // 3600)} h ago"


def tokens_per_change(p, now, h, run):
    """Input-equivalent tokens per landed change, from the per-lane cost reader (cost.per_lane, T-5950): each lane's lead,
    builder, reviewer and fixer shares, averaged over the lanes that have tokens. A landed change with none is left out.
    The lead's replies are the run's one transcript walk's (_pass), not a second read of them."""
    r = cost.per_lane(p, PER_LANE_DAYS, lead_replies=_pass(p, now, run)["replies"])
    landed = r.get("landed") or 0
    totals = [t for t in (sum(cost.equiv(s) for s in roles.values() if isinstance(s, dict))
                          for roles in _dict(r.get("lanes")).values() if isinstance(roles, dict)) if t > 0]
    if not landed:
        return None, f"no change landed in the last {PER_LANE_DAYS} days"
    if not totals:
        return None, f"{landed} changes landed in {PER_LANE_DAYS} days, none with transcript tokens"
    left = landed - len(totals)
    return round(sum(totals) / len(totals)), (f"{left} of {landed} landed changes had no tokens and are left out"
                                              if left > 0 else None)


# key -> (reader(p, now, state, run) -> (actual or None, note or None), its period in seconds)
HEAVY = {"push_lag": (push_lag, PUSH_PERIOD_S), "steers": (steers, SLOW_PERIOD_S),
         "compactions": (compactions, SLOW_PERIOD_S), "lead_context": (lead_context, SLOW_PERIOD_S),
         "disk_growth": (disk_growth, DISK_PERIOD_S), "tokens_per_change": (tokens_per_change, SLOW_PERIOD_S)}


def _heavy_state(p, key, now):
    """(actual or None, note or None) of a heavy key as refresh_heavy left it: one small read. A value never read, or not
    refreshed for STALE_X periods, is None with that said."""
    h = _read(os.path.join(p.dir, HEAVY_FILE)) or {}
    at = _dict(h.get("at")).get(key)
    note = _dict(h.get("notes")).get(key)
    note = note if isinstance(note, str) else None
    if not _finite(at):
        return None, "not read yet"
    period = HEAVY[key][1] if key in HEAVY else SLOW_PERIOD_S
    if now - at > STALE_X * period:
        return None, f"not refreshed for {int((now - at) // 3600)} h"
    v = _dict(h.get("values")).get(key)
    return (float(v) if _finite(v) else None), note


def _heavy_reader(key):
    return lambda p, now: _heavy_state(p, key, now)[0]


def _tokens_reader(p, now):
    """The per-lane reading when there is one, else the cost cache's (the light value)."""
    v = _heavy_state(p, "tokens_per_change", now)[0]
    return v if v is not None else _light(p, "tokens_per_change", now)[0]


READERS = {"usage_run_out": usage_run_out, "cpu": cpu_pct,
           **{k: _derived(k) for k in ("accept_to_merge", "rework_rounds", "sampo_vs_bare")},
           **{k: _heavy_reader(k) for k in HEAVY}, "tokens_per_change": _tokens_reader}


# ----- refresh: the scans, once per CACHE_S -----

def _derive(p, now):
    """The values from the ledger (landings, changes asked, each landed task's first accept), the cost cache and the bench
    results. No brief is read: a scan costs the ledger's window, not the archive."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - WINDOW_S))
    landed, first = {}, {}  # task -> newest landing (epoch); task -> first accept (epoch)
    review = {}  # T-5972: task -> its audit_prepped and changes_asked rows, counted by outcomes.review_rounds
    for e in c.ledger_events(p, ["lane_landed", "changes_asked", "accepted", "audit_prepped"], since=since):
        t = _epoch(e.get("ts"))
        if t is None:
            continue
        kind = e.get("event")
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        if kind == "lane_landed":
            for tid in data.get("tasks") or [e.get("task")]:
                if isinstance(tid, str) and tid:
                    landed[tid] = max(t, landed.get(tid, 0.0))
        elif kind in ("changes_asked", "audit_prepped") and isinstance(e.get("task"), str) and e.get("task"):
            review.setdefault(e["task"], []).append(e)
        elif kind == "accepted" and isinstance(e.get("task"), str) and e.get("task"):
            at = float(data["at"]) if _num(data.get("at")) else t  # the first stamp: a re-accept never moves it
            first[e["task"]] = min(at, first.get(e["task"], at))
    values, notes = {}, {}
    if landed:
        mins = [(landed[t] - first[t]) / 60 for t in landed if t in first and landed[t] >= first[t]]
        if mins:
            values["accept_to_merge"] = round(statistics.median(mins), 2)
        # T-5972: the rounds each landed lane took (outcomes.review_rounds, the reader a reviewer's stop also uses); a lane
        # with no prep and no ask was never reviewed and is left out of the divisor, not read as zero rounds
        from sampo import outcomes
        taken = [n for n in (outcomes.review_rounds(review.get(t, ())) for t in landed) if n]
        if taken:
            values["rework_rounds"] = round(sum(taken) / len(taken), 2)
        costs, made = cost.cache_as_is(p)
        if made is None:
            notes["tokens_per_change"] = "no cost cache yet, so no tokens are counted"
        else:
            priced = [t for t in landed if costs.get(t, 0) > 0]  # a change with no cost is left out, not counted as zero
            if priced:
                values["tokens_per_change"] = round(sum(costs[t] for t in priced) / len(priced))
            if len(priced) < len(landed):
                notes["tokens_per_change"] = (f"{len(landed) - len(priced)} of {len(landed)} landed changes had no cost "
                                              "and are left out")
    if notes:
        values["notes"] = notes
    ratio = _bench_ratio(p)
    if ratio is not None:
        values["sampo_vs_bare"] = round(ratio, 3)
    return values


def _per_pass(cases):
    """The cost of a case set per passing case: every case's cost counts (a failed attempt is paid for). None: no pass."""
    total = sum(x["cost_usd"] for x in cases if isinstance(x, dict) and _num(x.get("cost_usd")))
    passed = sum(1 for x in cases if isinstance(x, dict) and x.get("pass") is True)
    return total / passed if passed else None


def _bench_ratio(p):
    """Sampo's cost per landed case over plain Claude Code's (bare), from the newest duel pair in bench/results (the real
    shape: label duel-<set>-foreman-<stamp> is Sampo's arm; duel-<set>-bare-<stamp> with top-level "bare": true is the
    bare arm). Other results (evolve, court, a "bare": "snippet" arm) are not a pair. None until a pair has a pass in both."""
    pairs = {}
    for path in sorted(glob.glob(os.path.join(p.dir, "bench", "results", "*.json")), key=cost.mtime)[-BENCH_FILES:]:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            mt = os.path.getmtime(path)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not isinstance(d.get("cases"), list):
            continue
        label = str(d.get("label") or "")
        if not label.startswith("duel-"):
            continue
        if d.get("bare") is True and "-bare-" in label:
            arm, key = "bare", label.replace("-bare-", "-", 1)
        elif not d.get("bare") and "-foreman-" in label:
            arm, key = "sampo", label.replace("-foreman-", "-", 1)
        else:
            continue
        pair = pairs.setdefault(key, {})
        if arm not in pair or mt > pair[arm][0]:
            pair[arm] = (mt, d["cases"])
    scored = []
    for pair in pairs.values():
        if len(pair) != 2:
            continue
        sampo, bare = _per_pass(pair["sampo"][1]), _per_pass(pair["bare"][1])
        if sampo is not None and bare:
            scored.append((max(mt for mt, _ in pair.values()), sampo / bare))
    return max(scored)[1] if scored else None


def _take_lock(p, name):
    """The project's lock file `name`, held (close it when done), or None when another holds it. Never waits."""
    try:
        f = open(os.path.join(p.dir, name), "a")
    except OSError:
        return None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _take_scan_lock(p):
    return _take_lock(p, SCAN_LOCK)


def heavy_due(p, now):
    """The heavy keys whose last reading is older than their period, or that were never read."""
    at = _dict((_read(os.path.join(p.dir, HEAVY_FILE)) or {}).get("at"))
    return [k for k, (_fn, period) in HEAVY.items() if not (_finite(at.get(k)) and 0 <= now - at[k] < period)]


def refresh_heavy(p, now=None, keys=None, force=False):
    """Reads the heavy keys that are due (`keys`: only those; force: due or not) into the project's targets-heavy.json,
    each stamped with its own time. One run at a time per project: one that finds another running does nothing and returns
    None. A reader that raises leaves its key unmeasured (the exception's type, never its words, in the note) and the next
    still runs; a reader still going after READER_BUDGET_S of its own is cut (_deadline) and its key is unmeasured with
    that said; no new reader starts after HEAVY_BUDGET_S. The readers of one run share what they read (_Run). The file is
    written after each reader."""
    now = time.time() if now is None else now
    lock = _take_lock(p, HEAVY_LOCK)
    if lock is None:
        return None
    try:
        path = os.path.join(p.dir, HEAVY_FILE)
        h = _read(path) or {}
        for k in ("values", "at", "notes"):
            h[k] = dict(_dict(h.get(k)))
        due = set(heavy_due(p, now))
        t0 = time.monotonic()
        run = _Run()
        for key in [k for k in HEAVY if (keys is None or k in keys) and (force or k in due)]:
            if time.monotonic() - t0 > HEAVY_BUDGET_S:
                break
            try:
                with _deadline(run, READER_BUDGET_S):
                    value, note = HEAVY[key][0](p, now, h, run)
            except _Deadline:
                value, note = None, f"did not finish in {READER_BUDGET_S} s"
            except Exception as e:  # noqa: BLE001 — one reader's trouble costs the others nothing; its words may hold a path
                value, note = None, f"could not be read ({type(e).__name__})"
            h["values"][key], h["at"][key] = (float(value) if _finite(value) else None), now
            if isinstance(note, str) and note:
                h["notes"][key] = note
            else:
                h["notes"].pop(key, None)
            try:
                c.write_atomic(path, json.dumps(h))
            except OSError:
                pass
        return h
    finally:
        lock.close()


def _spawn_heavy(p, now):
    """Starts `sampo targets --refresh-heavy` detached when a heavy key is due, at most every HEAVY_SPAWN_S (the stamp holds
    the clock it was started by; no stamp written, no child). Never under FOREMAN_NO_BACKGROUND (tests call refresh_heavy)."""
    if os.environ.get("FOREMAN_NO_BACKGROUND") or not heavy_due(p, now):
        return
    stamp = os.path.join(p.dir, HEAVY_SPAWN)
    try:
        with open(stamp, encoding="utf-8") as f:
            last = float(f.read().strip())
        if 0 <= now - last < HEAVY_SPAWN_S:
            return
    except (OSError, ValueError):
        pass
    try:
        c.write_atomic(stamp, repr(float(now)))
    except OSError:
        return
    import subprocess
    nice = shutil.which("nice")  # the child is a background read of the transcripts: it yields the CPU to the user's work
    argv = ([nice, "-n", "10"] if nice else []) + [os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "targets", "--refresh-heavy"]
    log = None
    try:  # its exit line (cmd_targets) lands here, in the project's log, not in a terminal nobody reads
        logs = os.path.join(c.state_dir(), "logs")
        os.makedirs(logs, exist_ok=True)
        log = open(os.path.join(logs, f"targets-heavy-{os.path.basename(p.dir)}.log"), "w", encoding="utf-8")
    except OSError:
        log = None
    try:
        subprocess.Popen(argv, cwd=p.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=log if log is not None else subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass
    finally:
        if log is not None:
            log.close()


def refresh(p, now=None, force=False):
    """The project's targets.json: the derived values, stamped with the time the scan began and kept CACHE_S (force redoes
    them). One scan at a time per project: a caller that finds one running gets the file as it is, at once, and never
    scans twice."""
    now = time.time() if now is None else now
    path = os.path.join(p.dir, "targets.json")
    old = _read(path)
    if not force and _fresh(old, now):
        return old
    lock = _take_scan_lock(p)
    if lock is None:
        return old if old is not None else {"ts": now, "values": {}}
    try:
        old = _read(path)  # another scan may have finished between the first read and the lock
        if not force and _fresh(old, now):
            return old
        out = {"ts": now, "values": _derive(p, now)}
        try:
            c.write_atomic(path, json.dumps(out))
        except OSError:
            pass
        _spawn_heavy(p, now)
        return out
    finally:
        lock.close()


# ----- the verdict -----

def evaluate(p, now=None):
    """Every target's row: name, label, actual (None when unmeasured), target, unit, status (ok|regressed|unmeasured),
    provisional (a starting value: it tells, it neither files nor leads) and note (what the derivation left out)."""
    now = time.time() if now is None else now
    out = []
    for r in load():
        fn = READERS.get(r["reader"])
        try:
            actual = fn(p, now) if fn else None
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError, AttributeError):
            actual = None
        key = r["reader"]
        note = _light(p, key, now)[1] if key in SOURCES else None  # a derived value: where it came from, when it is old
        if key in HEAVY:
            hv, hn = _heavy_state(p, key, now)
            note = note if key == "tokens_per_change" and hv is None and note else hn
        target = r.get("target")
        worse = _num(target) and actual is not None and (
            actual > target if r.get("higher_is_worse", True) else actual < target)
        status = "unmeasured" if actual is None else ("regressed" if worse else "ok")
        out.append({"name": r["name"], "label": r.get("label", r["name"]),
                    "actual": None if actual is None else round(actual, 2), "target": target,
                    "unit": r.get("unit", ""), "status": status, "why": r.get("why", ""),
                    "provisional": bool(r.get("provisional")), "note": note})
    return out


def regressed(p, now=None):
    return [r for r in evaluate(p, now) if r["status"] == "regressed"]


def title(row):
    """The discovered task's title: the target, its numbers, what it should be (and that it is provisional, if it is)."""
    prov = " (provisional)" if row.get("provisional") else ""
    return f"{PREFIX}{row['name']} {_fmt(row['actual'])} {row['unit']} vs target {_fmt(row['target'])}{prov}"


def filed_recently(p, name, now):
    """Whether a capture for this target was filed in the last FILE_GAP_S: the project's record, not the briefs."""
    at = (_read(os.path.join(p.dir, FILED_FILE)) or {}).get(name)
    return _num(at) and 0 <= now - at < FILE_GAP_S


def claim_filing(p, name, now):
    """Records this target's filing now, unless it filed in the last FILE_GAP_S: the check and the record in one locked
    step, so two runs (the Stop hook, the hourly sweep) can't both file. True when it may file. Waits only for that step."""
    try:
        with open(os.path.join(p.dir, FILED_LOCK), "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            if filed_recently(p, name, now):
                return False
            path = os.path.join(p.dir, FILED_FILE)
            d = {k: v for k, v in (_read(path) or {}).items() if _num(v) and 0 <= now - v < FILE_GAP_S}
            d[name] = now
            c.write_atomic(path, json.dumps(d))
            return True
    except OSError:
        return False  # no record, no filing: the day's one capture is the rule


# ----- the verb -----

def _fmt(v):
    if v is None:
        return "-"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:g}"


def cmd_targets(args):
    """`sampo targets`: each outcome target's actual, its target and ok | regressed | unmeasured (--json: the rows). The scan
    is refreshed first; when that fails, the table is the project's last values."""
    from sampo import cli
    p = cli.resolve(args, create=False)
    if getattr(args, "refresh_heavy", False):  # the detached child's whole job: the heavy readers that are due, here
        t0, ok = time.monotonic(), True
        try:
            h = refresh_heavy(p, time.time())
        except Exception as e:  # noqa: BLE001 — the exit line says the type only: its words may hold a path
            h, ok = None, False
            line = f"targets --refresh-heavy failed ({type(e).__name__})"
        if ok:
            if h is None:
                line = "targets --refresh-heavy: another run holds the lock"
            else:
                bare = sorted(k for k in HEAVY if not _finite(h["values"].get(k)))
                line = (f"targets --refresh-heavy: {len(HEAVY) - len(bare)} of {len(HEAVY)} read in "
                        f"{time.monotonic() - t0:.1f} s" + (f"; unmeasured: {', '.join(bare)}" if bare else ""))
        print(line, file=sys.stderr)
        return 0 if ok else 1
    try:  # by hand: the last values at once. The due heavy readers run in the background (_spawn_heavy: it is a no-op while
        if os.environ.get("FOREMAN_NO_BACKGROUND"):  # none is due or one started lately), never here for a person waiting; only
            refresh_heavy(p)  # where no child may start (the tests') do they run inline
        else:
            _spawn_heavy(p, time.time())
    except Exception:  # noqa: BLE001 — the table prints from the last values
        pass
    try:
        refresh(p)
    except Exception:  # noqa: BLE001 — the table still prints, from the last values
        pass
    rows = evaluate(p)
    text = "\n".join(f"{r['label']:<34} {_fmt(r['actual']):>10} {r['unit']}  (target {_fmt(r['target'])})  {r['status']}"
                     + (" (provisional)" if r["provisional"] else "") + (f"  [{r['note']}]" if r["note"] else "")
                     for r in rows)
    cli.out(args, rows, text or "no outcome targets (plugin/data/targets.json)")
