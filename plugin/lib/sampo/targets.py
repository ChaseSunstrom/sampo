"""T-5709: outcome targets. plugin/data/targets.json is the table (a number is a data edit); each row names a reader.

A reader makes ONE bounded read of ONE file and returns the actual (or None: unmeasured, never ok):
  usage_run_out   state/burn.json           the burn forecast's hours before the reset (cost.burn_findings)
  cpu             state/cpu.json            the samples of the last WINDOW_S, as % of the machine's cores
  the rest        <project>/targets.json    the values refresh() derived from the ledger, the cost cache and the bench
                                            results (and the notes on them)
refresh() does the scans. It runs from the self-check (at most once per CACHE_S, never inside a reader), so the Stop
hook's readers stay small whatever the ledger holds. One scan per project at a time. A target that fails its reading is
"unmeasured", never ok. state/burn.json and state/cpu.json are the machine's; the rest are the project's.
"""
import calendar
import fcntl
import glob
import json
import os
import re
import statistics
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


def _values(p):
    """The project's derived values (targets.json's values), {} when there are none."""
    v = (_read(os.path.join(p.dir, "targets.json")) or {}).get("values")
    return v if isinstance(v, dict) else {}


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
    def reader(p, now):
        v = _values(p).get(key)
        return float(v) if _num(v) else None
    return reader


READERS = {"usage_run_out": usage_run_out, "cpu": cpu_pct,
           **{k: _derived(k) for k in ("accept_to_merge", "rework_rounds", "tokens_per_change", "sampo_vs_bare")}}


# ----- refresh: the scans, once per CACHE_S -----

def _derive(p, now):
    """The values from the ledger (landings, changes asked, each landed task's first accept), the cost cache and the bench
    results. No brief is read: a scan costs the ledger's window, not the archive."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - WINDOW_S))
    landed, changes, first = {}, {}, {}  # task -> newest landing (epoch); task -> changes asked; task -> first accept (epoch)
    for e in c.ledger_events(p, ["lane_landed", "changes_asked", "accepted"], since=since):
        t = _epoch(e.get("ts"))
        if t is None:
            continue
        kind = e.get("event")
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        if kind == "lane_landed":
            for tid in data.get("tasks") or [e.get("task")]:
                if isinstance(tid, str) and tid:
                    landed[tid] = max(t, landed.get(tid, 0.0))
        elif kind == "changes_asked" and isinstance(e.get("task"), str) and e.get("task"):
            changes[e["task"]] = changes.get(e["task"], 0) + 1
        elif kind == "accepted" and isinstance(e.get("task"), str) and e.get("task"):
            at = float(data["at"]) if _num(data.get("at")) else t  # the first stamp: a re-accept never moves it
            first[e["task"]] = min(at, first.get(e["task"], at))
    values, notes = {}, {}
    if landed:
        mins = [(landed[t] - first[t]) / 60 for t in landed if t in first and landed[t] >= first[t]]
        if mins:
            values["accept_to_merge"] = round(statistics.median(mins), 2)
        values["rework_rounds"] = round(sum(changes.get(t, 0) for t in landed) / len(landed), 2)
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


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


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
    for path in sorted(glob.glob(os.path.join(p.dir, "bench", "results", "*.json")), key=_mtime)[-BENCH_FILES:]:
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


def _take_scan_lock(p):
    """The project's scan lock, held (close it when done), or None when another scan holds it. Never waits."""
    try:
        f = open(os.path.join(p.dir, SCAN_LOCK), "a")
    except OSError:
        return None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


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
        return out
    finally:
        lock.close()


# ----- the verdict -----

def evaluate(p, now=None):
    """Every target's row: name, label, actual (None when unmeasured), target, unit, status (ok|regressed|unmeasured),
    provisional (a starting value: it tells, it neither files nor leads) and note (what the derivation left out)."""
    now = time.time() if now is None else now
    values = _values(p)
    notes = values.get("notes") if isinstance(values.get("notes"), dict) else {}
    out = []
    for r in load():
        fn = READERS.get(r["reader"])
        try:
            actual = fn(p, now) if fn else None
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError, AttributeError):
            actual = None
        target = r.get("target")
        worse = _num(target) and actual is not None and (
            actual > target if r.get("higher_is_worse", True) else actual < target)
        status = "unmeasured" if actual is None else ("regressed" if worse else "ok")
        out.append({"name": r["name"], "label": r.get("label", r["name"]),
                    "actual": None if actual is None else round(actual, 2), "target": target,
                    "unit": r.get("unit", ""), "status": status, "why": r.get("why", ""),
                    "provisional": bool(r.get("provisional")), "note": notes.get(r["name"])})
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
    try:
        refresh(p)
    except Exception:  # noqa: BLE001 — the table still prints, from the last values
        pass
    rows = evaluate(p)
    text = "\n".join(f"{r['label']:<34} {_fmt(r['actual']):>10} {r['unit']}  (target {_fmt(r['target'])})  {r['status']}"
                     + (" (provisional)" if r["provisional"] else "") + (f"  [{r['note']}]" if r["note"] else "")
                     for r in rows)
    cli.out(args, rows, text or "no outcome targets (plugin/data/targets.json)")
