"""T-3286: sampo flakes forecasts which tests will likely flake before a run, and tells a red run's known flakes from a
new break. It reads only the flake ledger (flakes.json, written by core.note_flakes): a test's fail count and recency,
its trigger (timing words in its failure text mean load, else chance), weighted up when the box is loaded. A forecast
is counted, never learned: the ledger and the test verdicts decide."""
import datetime
import json
import os
import re

from sampo import core as c

LOAD_WORDS = re.compile(r"time ?outs?|timed out|deadline|timing|too slow|time limit|took \d", re.I)
HOT_WEIGHT = 2.0  # ponytail: a flat doubling of load-tagged flakes once the box is hot; a curve over load if the data asks
KNOWN_FAILS = 2  # a red run's test is a known flake with this many fails on record...
KNOWN_DAYS = 30  # ...and a failure within this many days
STAMP = "%Y-%m-%dT%H:%M:%SZ"  # core.now()'s format: a ledger `last` compares as text


def history(directory):
    """flakes.json's valid entries {test: entry}; {} when it is missing, empty or damaged (never raises)."""
    try:
        with open(os.path.join(directory, "flakes.json"), encoding="utf-8") as f:
            flaky = json.load(f).get("flaky")
    except (OSError, ValueError, AttributeError, RecursionError):
        return {}
    return {str(k): v for k, v in (flaky.items() if isinstance(flaky, dict) else ()) if isinstance(v, dict)}


def counts(entry):
    """(fails, recoveries) as core.note_flakes keeps them: fails counts every failure, passes the recoveries. None when
    either isn't a whole number or no fail is on record. Shared with doctor's flaky-tests check."""
    f, p = entry.get("fails"), entry.get("passes")
    if type(f) is int and type(p) is int and f > 0 and p >= 0:
        return f, p
    return None


def trigger(entry):
    """'load' when the failure text under the test's name says a timeout, a deadline or timing (it flakes when the box
    is busy), else 'chance'. The first line of `why` is the line naming the test, so only the lines after it count."""
    lines = str(entry.get("why") or "").split("\n", 1)
    return "load" if len(lines) == 2 and LOAD_WORDS.search(lines[1]) else "chance"


def ranked(flaky, per_cpu):
    """The entries with a fail count, worst first: fails, times the load weight (per_cpu is the load per CPU, None when
    unreadable), then the most recent failure, then name. No fail rate: recoveries are passes, so a rate is always 0.5 to
    1 and would tie a one-time flake with a chronic one. An entry without whole counts is known but not ranked."""
    rows, hot = [], per_cpu is not None and per_cpu >= c.LOAD_BAR
    for name, e in flaky.items():
        k = counts(e)
        if k:
            t = trigger(e)
            rows.append({"name": name, "fails": k[0], "recoveries": k[1], "last": str(e.get("last") or ""),
                         "trigger": t, "cmd": str(e.get("cmd") or "?"),
                         "score": k[0] * (HOT_WEIGHT if hot and t == "load" else 1)})
    rows.sort(key=lambda r: r["name"])  # stable sorts, last key first: score, then recency, then name
    rows.sort(key=lambda r: r["last"], reverse=True)
    rows.sort(key=lambda r: -r["score"])
    return rows


def rerun(entry, per_cpu):
    """How to tell a known flake from a break: a load flake reruns on a quiet box (said only when the box is loaded), a
    chance flake reruns once."""
    cmd = c.plain(str(entry.get("cmd") or "the gate"))[:120]
    if trigger(entry) == "load":
        if per_cpu is not None and per_cpu >= c.LOAD_BAR:
            return f"timing flake: rerun on a quiet box (load {per_cpu:.2f} per cpu now): `{cmd}`"
        return f"timing flake: rerun: `{cmd}`"
    return f"chance flake: rerun once: `{cmd}`"


def unknown_why(entry, cutoff):
    """Why a test in the ledger is not a known flake yet ('' when it is): KNOWN_FAILS fails on record and a failure
    stamped on or after `cutoff`."""
    k = counts(entry)
    if k is None:
        return "no fail counts on record"
    if k[0] < KNOWN_FAILS:
        return f"{k[0]} fail on record, not a known flake yet"
    if str(entry.get("last") or "") < cutoff:
        return f"no failure in the last {KNOWN_DAYS} days on record"
    return ""


def classify(flaky, names, per_cpu):
    """(known, new) for a red run's failing test names. A known flake has KNOWN_FAILS fails or more and a recent failure
    (rerun hint with its trigger); anything else is new, with why it isn't known: possibly a real break."""
    cutoff = (c.clock() - datetime.timedelta(days=KNOWN_DAYS)).strftime(STAMP)
    known, new = [], []
    for n in names:
        e = flaky.get(n)
        why = "no flake history" if e is None else unknown_why(e, cutoff)
        if why:
            new.append({"name": n, "why": why})
        else:
            known.append({"name": n, "fails": counts(e)[0], "trigger": trigger(e), "rerun": rerun(e, per_cpu)})
    return known, new


def cmd_flakes(args):
    from sampo import cli
    p = cli.resolve(args, create=False)
    flaky = history(p.dir)
    load = c.host_load()
    per_cpu = round(load[0] / load[1], 2) if load and load[1] else None
    rows = ranked(flaky, per_cpu)
    known, new = classify(flaky, args.red or [], per_cpu)
    hot = per_cpu is not None and per_cpu >= c.LOAD_BAR
    d = {"history": bool(rows), "load_per_cpu": per_cpu, "hot": hot, "forecast": rows,
         "red": {"known": known, "new": new} if args.red else None}
    top = max(args.top, 0)
    if not rows:  # missing, empty, damaged, or only entries without whole counts: nothing to rank
        lines = ["no flake history: " + (f"{len(flaky)} known without fail counts, nothing to rank" if flaky else
                                         "flakes.json is missing, empty or damaged, so nothing to forecast")]
    else:
        lines = [f"Forecast (load {per_cpu:.2f} per cpu{', hot: load flakes weigh up' if hot else ''}):"
                 if per_cpu is not None else "Forecast (load unknown):"]
        lines += [f"  {c.fit(c.plain(r['name']), 70)}  {r['fails']} fails, {r['recoveries']} recoveries, "
                  f"last {r['last'][:10] or '?'}  {r['trigger']}" for r in rows[:top]]
        if len(rows) > top:
            lines.append(f"  … {len(rows) - top} more")
    if args.red:
        lines += [f"known: {c.fit(c.plain(k['name']), 70)}: {k['rerun']}" for k in known]
        lines += [f"new: {c.fit(c.plain(n['name']), 70)}: {n['why']}; possibly a real break" for n in new]
        lines.append(f"Red: {len(new)} new, possibly a real break; {len(known)} known flake(s)" if new
                     else f"Red: all {len(known)} known flake(s): rerun them before blaming the change")
    return cli.out(args, d, "\n".join(lines))
