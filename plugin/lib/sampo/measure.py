"""sampo measure (T-5005, a slice of T-4307): a named number read straight from the project's raw ledger events, held to a
bound. Each measure is a function (events, since) -> [seconds], one value per accepted lane in the window; the registry
MEASURES maps a name to it, so a new number is one function and one line there. The two here read the land_train and
ship_done events the land writes (train.py) and need no other slice:

  accept-to-live    a land_train's `at` minus the epoch each of its lanes was accepted: the merge's own record
  accept-to-pushed  the end of the first ship_done with exit 0 that started at or after the train's `at`, minus the accept

The verb prints the median (minutes), the count and a verdict; it exits 1 when the median misses --max or --min.

review-cost (T-5843) is the second kind, named in DAILY: its reader takes the review_cost events (one per `sampo audit run`,
T-5842) and returns per-UTC-day rows (lanes, lenses, tokens, dollars and the estimated saving against the five-lens
default), printed as rows with a total, exit 0, and no --max or --min (a bound on a sum is refused)."""
import calendar
import math
import os
import statistics
import time

from sampo import core as c
from sampo import train

TAIL = 50000  # ledger lines first read; the read grows (x4) until it passes the window's start or the file's start
MAXTAIL = 2000000  # the most lines a read will grow to; a window older than that is reported, not guessed
DAY = 86400.0


def _trains(evs, since):
    """[(at, [accept epochs])] for the land_train events at or after `since`, newest last as the ledger holds them."""
    got = []
    for e in evs:
        d = e.get("data") if isinstance(e, dict) and e.get("event") == "land_train" else None
        x = train.train_accepts(d)
        if x and x[0] >= since:
            got.append(x)
    return got


def accept_to_live(evs, since):
    return [max(0.0, at - a) for at, accepted in _trains(evs, since) for a in accepted]


def accept_to_pushed(evs, since):
    return train.pushed_seconds(evs, _trains(evs, since))


def _num(v):
    """A ledger field as a non-negative finite number; junk (text, a bool, a negative, NaN) is 0."""
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    return v if ok else 0


def _lens_count(data):
    """How many lenses a review_cost run ran: len of its lens list (T-5842), or a bare count; None when the event has
    neither (written before it carried one), so the day says unknown rather than 0."""
    v = data.get("lenses")
    if isinstance(v, list):
        return len(v)
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def review_cost(evs, since):
    """Per-UTC-day rows from the review_cost events at or after `since`, oldest day first: the lanes reviewed, runs, lenses
    run (None when any run that day lacks a count), tokens (input + output + cache read + cache creation), dollars, and the
    estimated saving against the five-lens default: the lenses not run (each run's shortfall under DEFAULT_LENSES) times
    the day's mean tokens (and dollars) per lens. A day with no lens run has no mean, and so no saving: None, not 0."""
    full = len(c.DEFAULT_LENSES)
    days = {}
    for e in evs:
        d = e.get("data") if isinstance(e, dict) and e.get("event") == "review_cost" else None
        at = _epoch(e.get("ts")) if isinstance(d, dict) else None
        if at is None or at < since:
            continue
        r = days.setdefault(time.strftime("%Y-%m-%d", time.gmtime(at)),
                            {"tasks": set(), "runs": 0, "lenses": 0, "unknown": False, "missed": 0, "tokens": 0, "usd": 0.0})
        n = _lens_count(d)
        r["runs"] += 1
        if e.get("task"):
            r["tasks"].add(e["task"])
        if n is None:
            r["unknown"] = True
        else:
            r["lenses"] += n
            r["missed"] += max(0, full - n)
        r["tokens"] += sum(_num(d.get(k)) for k in ("input", "output", "cache_read", "cache_creation"))
        r["usd"] += _num(d.get("usd"))
    rows = []
    for day in sorted(days):
        r = days[day]
        lenses = None if r["unknown"] else r["lenses"]
        priced = lenses is not None and lenses > 0
        rows.append({"day": day, "lanes": len(r["tasks"]), "tasks": sorted(r["tasks"]), "runs": r["runs"], "lenses": lenses,
                     "tokens": round(r["tokens"]),
                     "usd": round(r["usd"], 4),
                     "saving_tokens": round(r["missed"] * r["tokens"] / lenses) if priced else None,
                     "saving_usd": round(r["missed"] * r["usd"] / lenses, 4) if priced else None})
    return rows


MEASURES = {"accept-to-live": accept_to_live, "accept-to-pushed": accept_to_pushed, "review-cost": review_cost}
DAILY = {"review-cost"}  # the measures that return per-day rows, not seconds per lane: no median, no bound
KINDS = {"review-cost": ["review_cost"]}  # the ledger kinds a measure reads; the default is the land's two


def _line_count(p):
    n = 0
    try:
        with open(os.path.join(p.dir, "ledger.jsonl"), "rb") as f:
            while chunk := f.read(1 << 20):
                n += chunk.count(b"\n")
    except OSError:
        pass
    return n


def _epoch(ts):
    try:
        return calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def read(p, name, window_days, now=None):
    """(seconds, note): the measure over the last `window_days` days. The ledger is read from its end in growing windows of
    raw lines until the oldest event read is older than the window or the file is exhausted; note is '' or says how far
    back a read stopped at MAXTAIL reached (then the answer is not a verdict)."""
    since = (time.time() if now is None else now) - window_days * DAY
    total = _line_count(p)
    n, note = min(TAIL, MAXTAIL, total) or 1, ""
    while True:
        evs = c.ledger_tail(p, n, KINDS.get(name, ["land_train", "ship_done"]))
        if n >= total:
            break
        oldest = min((x for x in (_epoch(e.get("ts")) for e in evs) if x is not None), default=None)
        if oldest is not None and oldest < since:
            break
        if n >= MAXTAIL:
            note = f"ledger read covers only back to {evs[0].get('ts') if evs else 'the last ' + str(n) + ' lines'}"
            break
        n = min(total, MAXTAIL, n * 4)
    return MEASURES[name](evs, since), note


def values(p, name, window_days, now=None):
    """The measure's seconds over the last `window_days` days of the project's ledger."""
    return read(p, name, window_days, now)[0]


def verdict(median_min, maximum, minimum):
    """'ok' or what the median missed; None bounds are not held."""
    if maximum is not None and median_min > maximum:
        return f"misses --max {maximum:g}"
    if minimum is not None and median_min < minimum:
        return f"misses --min {minimum:g}"
    return "ok"


def _saving(r):
    if r["saving_tokens"] is None:
        return "no saving (no lens ran to price one by)" if r["lenses"] == 0 else "no saving (lens count unknown)"
    return f"saving ~{r['saving_tokens']:,} tokens (${r['saving_usd']:.2f})"


def _cmd_daily(args, p):
    """The per-day measures' output: one row per UTC day that had a review in the window, and a total over them."""
    from sampo import cli
    rows, note = read(p, args.name, args.window)
    tasks = {t for r in rows for t in r["tasks"]}
    ok = [r for r in rows if r["saving_tokens"] is not None]
    total = {"days": len(rows), "lanes": len(tasks), "runs": sum(r["runs"] for r in rows),
             "tokens": sum(r["tokens"] for r in rows), "usd": round(sum(r["usd"] for r in rows), 4),
             "saving_tokens": sum(r["saving_tokens"] for r in ok) if ok else None,
             "saving_usd": round(sum(r["saving_usd"] for r in ok), 4) if ok else None}
    data = {"name": args.name, "window_days": args.window, "days": rows, "total": total, "note": note or None}
    if not rows:
        cli.out(args, data, note or "no reviews in window")
        return 0
    lines = [f"{args.name}: {len(rows)} day(s) with reviews in {args.window:g} day(s)"]
    for r in rows:
        n = "lenses unknown" if r["lenses"] is None else f"{r['lenses']} lens(es)"
        lines.append(f"{r['day']}  {r['lanes']} lane(s)  {n}  {r['tokens']:,} tokens  ${r['usd']:.2f}  {_saving(r)}")
    lines.append(f"total  {total['lanes']} lane(s)  {total['tokens']:,} tokens  ${total['usd']:.2f}"
                 + (f"  saving ~{total['saving_tokens']:,} tokens (${total['saving_usd']:.2f})" if ok else "  no saving"))
    if note:
        lines.append(note)
    cli.out(args, data, "\n".join(lines))
    return 0


def cmd_measure(args):
    from sampo import cli
    if args.name not in MEASURES:
        raise cli.UsageError(f"measure: {'unknown name ' + repr(args.name) if args.name else 'name one'}; "
                             f"registered: {', '.join(sorted(MEASURES))}")
    for flag, v in (("--max", args.max), ("--min", args.min)):
        if v is not None and (not math.isfinite(v) or v < 0):
            raise cli.UsageError(f"measure: {flag} is a number of minutes, 0 or more")
    if not math.isfinite(args.window) or args.window <= 0:
        raise cli.UsageError("measure: --window is a number of days above 0")
    if args.name in DAILY:
        if args.max is not None or args.min is not None:
            raise cli.UsageError(f"measure: {args.name} is a per-day sum, not a median: it takes no --max or --min")
        return _cmd_daily(args, cli.resolve(args))
    p = cli.resolve(args)
    secs, note = read(p, args.name, args.window)
    bound = args.max is not None or args.min is not None
    base = {"name": args.name, "window_days": args.window, "max": args.max, "min": args.min, "count": len(secs)}
    if note:  # the read stopped short of the window: whatever it found is not a verdict
        med = statistics.median(secs) / 60 if secs else None
        cli.out(args, {**base, "median_min": None if med is None else round(med, 2), "verdict": note, "note": note}, note)
        return 1 if bound else 0
    if not secs:
        cli.out(args, {**base, "median_min": None, "verdict": "no landings in window"}, "no landings in window")
        return 1 if bound else 0
    med = statistics.median(secs) / 60
    ver = verdict(med, args.max, args.min)
    cli.out(args, {**base, "median_min": round(med, 2), "verdict": ver},
            f"{args.name}: median {med:.1f} min over {len(secs)} lane(s) in {args.window:g} day(s): {ver}")
    return 0 if ver == "ok" else 1
