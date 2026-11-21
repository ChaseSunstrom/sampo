"""sampo measure (T-5005, a slice of T-4307): a named number read straight from the project's raw ledger events, held to a
bound. Each measure is a function (events, since) -> [seconds], one value per accepted lane in the window; the registry
MEASURES maps a name to it, so a new number is one function and one line there. The two here read the land_train and
ship_done events the land writes (train.py) and need no other slice:

  accept-to-live    a land_train's `at` minus the epoch each of its lanes was accepted: the merge's own record
  accept-to-pushed  the end of the first ship_done with exit 0 that started at or after the train's `at`, minus the accept

The verb prints the median (minutes), the count and a verdict; it exits 1 when the median misses --max or --min."""
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


MEASURES = {"accept-to-live": accept_to_live, "accept-to-pushed": accept_to_pushed}


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
        evs = c.ledger_tail(p, n, ["land_train", "ship_done"])
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
