"""T-3845: `sampo scorecard [--json]`: the direction's measures (research/direction.md section 8, the user's standing
direction of 2026-11-08, T-3839), each a number with a bar and a pass or fail mark, read model-free from what Sampo
already records. The seven:

  user asks not landed      the user's own open asks (selfcheck.open_asks): how many and how old the oldest is
  landed not shipped        commits since the last sampo ship (selfcheck.unshipped_read): the oldest one's age
  lanes waiting             finished lanes that only wait on a merge (lanes.awaiting_merge)
  agent CPU share           the sessions' share of the machine now (cpu.series, the samples the self-check keeps)
  tokens per closed task    the last TOKENS_DAYS days' input-equivalent spend (cost.task_costs) over the tasks closed
  steers per day            the avoidable steers the meter read in the last STEERS_DAYS days (steermeter.rate) a day
  Kalevala towers climbing  the share of opened towers past their first generation (rune.towers)

The bars are the named constants below, set from the direction's table; a measure that cannot be read (no ship ref, no
samples, nothing read, no tower) is "n/a" and never a miss. The hourly tick (night.tick) calls tick(): once GAP_S has
passed since the last counted tick it reads the measures, and a measure past its bar on DRIFT_TICKS counted ticks in a
row becomes one captured task naming it, its value and its bar. A recovery, or an unreadable tick, starts its count
over; no second task while the first is open; a task closed while the measure still fails is asked again only every
REFILE_TICKS ticks. The counts live in the project's state (scorecard.json), written atomically by the tick, which
holds the project's tick lock."""
import json
import os
import time

from sampo import core as c
from sampo import selfcheck

# The bars. Each is what the direction asks for, not what the machine does today: a bar the numbers sit past is the point.
ASKS_OLDEST_H = 24  # no ask of the user's open longer than a day
SHIP_OLDEST_H = selfcheck.SHIP_H  # landed work reaches GitHub within the self-check's own bar
LANES_WAITING_MAX = selfcheck.WAIT_MIN - 1  # under the count at which the self-check says a pile waits
# agent CPU share: the project's goal (cpu.goal: 10% of the machine unless `sampo cpu goal` set another)
TOKENS_TTL_S = 86400  # the tick reads the spend at most this old
TOKENS_DAYS, TOKENS_PER_TASK_MAX = 7, 6_000_000  # input-equivalent tokens (cost.weighted) per task closed in the window
STEERS_DAYS, STEERS_PER_DAY_MAX = 7, 3.0  # avoidable steers a day: the user should not need to say it
TOWERS_CLIMBING_MIN = 50.0  # percent of the opened towers past their first generation
GAP_S = 1800  # ticks closer together than this are one tick (the Stop hook's sweep runs the tick as well as the timer)
DRIFT_TICKS, REFILE_TICKS = 2, 24  # misses in a row that make a task; a closed one is asked again after this many more
LABELS = {"asks": "user asks not landed", "shipped": "landed not shipped", "lanes": "lanes waiting",
          "cpu": "agent CPU share", "tokens": "tokens per closed task", "steers": "steers per day",
          "towers": "Kalevala towers climbing"}


def _num(v):
    return f"{v:,.0f}" if isinstance(v, (int, float)) and abs(v) >= 1000 else f"{v:g}"


def asks(p, briefs, now):
    held, rows = selfcheck.open_asks(p, briefs, max_days=None)
    if held:
        rows = [(made, x) for made, x in selfcheck.concern_rows(held, max_days=None)]
    if not rows:
        return {"value": 0, "count": 0, "detail": "none open"}
    made, first = rows[0]
    return {"value": round(made * 24, 1), "count": len(rows),
            "detail": f"{len(rows)} ask(s), the oldest {selfcheck._age(made * 86400)} ({first['id'] if held else first.id})"}


def shipped(p, briefs, now):
    got = selfcheck.unshipped_read(p, now)
    if got is None:
        return {"value": None, "detail": "no ship yet, or the ship ref is off this history"}
    n, oldest = got
    return {"value": round(oldest / 3600, 1), "count": n,
            "detail": f"{n} commit(s), the oldest {selfcheck._age(oldest)}" if n else "everything landed is shipped"}


def lanes(p, briefs, now):
    from sampo import lanes as slanes
    waiting = slanes.awaiting_merge(p, list(briefs))
    return {"value": len(waiting), "detail": f"{len(waiting)} finished lane(s) wait on a merge"
            + (f": {', '.join(waiting[:3])}{' …' if waiting[3:] else ''}" if waiting else "")}


def cpu(p, briefs, now):
    from sampo import cpu as scpu
    g = scpu.goal(p)
    got = scpu.series(g, now=now)
    if not got:
        return {"value": None, "bar": g, "detail": "no CPU samples in the last 30 minutes"}
    return {"value": got["now"], "bar": g, "detail": f"{got['now']:g}% of the machine's {scpu.ncores()} cores now"}


def _spent(p, now, fresh):
    """The window's spend: cost.task_costs rescans days of transcripts, so the hourly tick reads a copy up to
    TOKENS_TTL_S old (tokens.json beside scorecard.json); the verb reads fresh and refreshes it."""
    from sampo import cost
    path = os.path.join(p.dir, "tokens.json")
    if not fresh:
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st.get("at"), (int, float)) and isinstance(st.get("spent"), (int, float)) \
                    and 0 <= now - st["at"] < TOKENS_TTL_S:
                return st["spent"]
        except (OSError, ValueError, AttributeError):
            pass
    spent = sum(cost.task_costs(p, TOKENS_DAYS).values())
    try:
        c.write_atomic(path, json.dumps({"at": now, "spent": spent}))
    except OSError:
        pass
    return spent


def tokens(p, briefs, now, fresh=False):
    since = c.iso(now - TOKENS_DAYS * 86400)[:19]  # the digest's own count of done tasks, archived ones too (cost._week)
    closed = sum(1 for b in c.load_briefs(p, include_archive=True)
                 if b.status == "done" and str(b.meta.get("updated", ""))[:19] >= since)
    if not closed:
        return {"value": None, "detail": f"no task closed in the last {TOKENS_DAYS} days"}
    spent = _spent(p, now, fresh)
    return {"value": round(spent / closed), "count": closed,
            "detail": f"{_num(spent)} tokens over {closed} task(s) closed in {TOKENS_DAYS} days"}


def steers(p, briefs, now):
    from sampo import steermeter
    since = c.iso(now - STEERS_DAYS * 86400)
    avoidable, read = steermeter.rate(p, since)
    if not read:
        return {"value": None, "detail": f"no message read in the last {STEERS_DAYS} days"}
    return {"value": round(avoidable / STEERS_DAYS, 1),
            "detail": f"{avoidable} avoidable of {read} read in {STEERS_DAYS} days"}


def towers(p, briefs, now):
    from sampo import rune
    rows = [t for t in rune.towers(p) if t["name"] != "root"]
    if not rows:
        return {"value": None, "detail": "no tower opened yet"}
    up = sum(1 for t in rows if (t.get("generation") or 0) >= 1)
    return {"value": round(100.0 * up / len(rows), 1), "count": up,
            "detail": f"{up} of {len(rows)} towers past their first generation"}


# name, read, bar, op, unit: a bar of None is the read's own (the project's CPU goal)
MEASURES = (("asks", asks, ASKS_OLDEST_H, "<=", "h"), ("shipped", shipped, SHIP_OLDEST_H, "<=", "h"),
            ("lanes", lanes, LANES_WAITING_MAX, "<=", ""), ("cpu", cpu, None, "<=", "%"),
            ("tokens", tokens, TOKENS_PER_TASK_MAX, "<=", "tokens"), ("steers", steers, STEERS_PER_DAY_MAX, "<=", "/day"),
            ("towers", towers, TOWERS_CLIMBING_MIN, ">=", "%"))


def compute(p, now=None, fresh=False):
    """[{name, label, value, bar, op, unit, ok, detail[, count]}], one per measure in the direction's order. ok is True or
    False against the bar, None when the measure cannot be read (value None): a read that raises is n/a, with its error."""
    now = time.time() if now is None else now
    briefs = c.load_briefs(p)
    out = []
    for name, read, bar, op, unit in MEASURES:
        try:
            got = read(p, briefs, now, fresh=True) if fresh and name == "tokens" else read(p, briefs, now)
        except Exception as e:  # noqa: BLE001: one unreadable measure never costs the others
            got = {"value": None, "detail": f"not read: {c.fit(c.plain(str(e)), 120)}"}
        bar = got.pop("bar", bar)
        v = got.get("value")
        ok = None if v is None else (v <= bar if op == "<=" else v >= bar)
        out.append({"name": name, "label": LABELS[name], "value": v, "bar": bar, "op": op, "unit": unit, "ok": ok, **got})
    return out


def line(r):
    mark = {True: "✓", False: "✗", None: "·"}[r["ok"]]
    bar = f"bar {r['op']} {_num(r['bar'])}{(' ' + r['unit']) if r['unit'] else ''}"
    return f"{mark} {r['label']}: {r['detail']} ({bar}" + (")" if r["ok"] is not None else "; n/a)")


def cmd_scorecard(args):
    from sampo import cli
    p = cli.resolve(args)
    rows = compute(p, fresh=True)
    failing = [r["name"] for r in rows if r["ok"] is False]
    head = (f"scorecard: {len(failing)} of {len(rows)} measures past their bar" if failing
            else "scorecard: every measure that can be read is within its bar")
    cli.out(args, {"measures": rows, "failing": failing}, "\n".join([head] + [line(r) for r in rows]))


# ---------------------------------------------------------------- the tick's drift pass

def _path(p):
    return os.path.join(p.dir, "scorecard.json")


def _load(p):
    try:
        with open(_path(p), encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and isinstance(st.get("misses"), dict):
            return {"at": st.get("at") if isinstance(st.get("at"), (int, float)) else None,
                    "misses": {k: v for k, v in st["misses"].items() if isinstance(k, str) and isinstance(v, int)}}
    except (OSError, ValueError):
        pass
    return {"at": None, "misses": {}}


def _inside_gap(st, now):
    return st["at"] is not None and 0 <= now - st["at"] < GAP_S


def due(p, now=None):
    """Whether a tick at `now` counts: GAP_S since the last one counted (a clock that went back counts again)."""
    now = time.time() if now is None else now
    return not _inside_gap(_load(p), now)


def _text(r, n):
    bar = f"{r['op']} {_num(r['bar'])}{(' ' + r['unit']) if r['unit'] else ''}"
    val = f"{_num(r['value'])}{(' ' + r['unit']) if r['unit'] else ''}"
    return (f"Scorecard: {r['label']} is past its bar\n\n"
            f"{r['label']} reads {val} against its bar of {bar}, on {n} ticks in a row (research/direction.md section 8, "
            f"the direction's measures). {r.get('detail') or ''}\n\n"
            f"Find what holds it there and move it: `sampo scorecard` prints every measure, its bar and its mark.")


def drift(p, rows, now=None):
    """One tick's rows into the counts: [(task id, measure)] for each task newly captured. A measure past its bar adds
    one to its count (ok False); within the bar, unread or absent clears it. At DRIFT_TICKS it is captured once, by title
    (eco.capture_once: nothing while an open task has it), and again only every REFILE_TICKS ticks after."""
    from sampo import eco
    now = time.time() if now is None else now
    st = _load(p)
    if _inside_gap(st, now):
        return []
    by = {r["name"]: r for r in rows}
    misses, made = {}, []
    for name, r in by.items():
        if r.get("ok") is not False:
            continue
        n = misses[name] = st["misses"].get(name, 0) + 1
        if n < DRIFT_TICKS or (n - DRIFT_TICKS) % REFILE_TICKS:
            continue
        b, new = eco.capture_once(p, _text(r, n), "FIX", "self", trigger="scorecard")
        if new:
            made.append((b.id, name))
    try:
        c.write_atomic(_path(p), json.dumps({"at": now, "misses": misses}))
    except OSError:
        pass
    return made


def tick(p, now=None):
    """The hourly tick's pass: [] when this tick is inside the gap of the last one; else compute, then drift."""
    now = time.time() if now is None else now
    if not due(p, now):
        return []
    return drift(p, compute(p, now), now)
