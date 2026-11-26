"""T-6859: the throughput watchdog. Every GAP_S (the hourly tick and the Stop hook's sweep both call tick()) it reads one
outcome and one stage census, both model-free, from what Sampo already records:

  outcome   builder-done to on trunk. A lane whose builder returned (its first builder_mark that returned) and landed (its
            last lane_landed) counts its hours from the return to the landing. A returned lane not landed yet counts its
            hours so far (a censored hour: now less the return), so a stall moves the median and p90 too. The median and p90
            hours, the lanes landed in the window, and the returned lanes not landed (returned_open).
  census    where the open work sits now: parked (lanes.parked), held (recover.held, open tasks only), waiting on a land
            (lanes.awaiting_merge), waiting on a ship (selfcheck.unshipped_read), idle (lanes.builders_cap less the working
            slots) and queued (planned tasks with no builder).

A regressed outcome is the median or p90 past its bar, or no lane landed while MIN_N returned (the per-hour bar). The
verdict names the item stage most items sit in (parked, held, land), or idle when tasks are queued and no item stage holds
anything, and names ship beside it. The advice is checked: the stage's fix is given once, and after WAIT_S the ledger
decides. An action of that stage's kinds that ran and left the stage's count where it was raises the stage's alarm and
is not repeated as advice; a count that moved clears the advice and any alarm. An open alarm is in every tick's note until
it clears. State: watchdog.json in the project folder, written atomically. The note the lead reads is state["note"]."""
import json
import math
import os
import statistics
import time

from sampo import core as c

GAP_S = 1800  # ticks closer together than this are one tick
WINDOW_DAYS = 1  # the outcome's window: the landings counted, and the lanes landed an hour
LOOKBACK_DAYS = 7  # the ledger is read this far back, so a lane returned days before it landed is timed
MEDIAN_BAR_H, P90_BAR_H = 24, 72  # builder-done to on trunk, hours: past either, the outcome regressed
MIN_N = 3  # lanes timed (landed, or returned and not landed) before the outcome is judged
WAIT_S = 2 * GAP_S  # after advice, the watchdog waits this long before the ledger decides
STAGES = ("parked", "held", "land", "ship", "idle")
ITEM_STAGES = ("parked", "held", "land")  # the stages whose items are open work; idle and ship are judged apart
FIXES = {
    "parked": "unpark or drop the parked lanes: sampo lane list gives each one's stop reason",
    "held": "clear the hold: sampo next names the held task and what holds it",
    "land": "land the finished lanes: sampo next names the oldest waiting one to merge",
    "ship": "ship the landed commits: sampo ship moves main's ship ref forward",
    "idle": "start work on the idle builder slots: sampo next names the task to start",
}
# the ledger kinds that record the action each stage's advice names: one of them after the advice means it ran
ACTS = {"parked": ("lane_builder", "lane_new"), "held": ("task_set", "lane_builder"), "land": ("lane_merge", "land_train"),
        "ship": ("ship_test",), "idle": ("lane_builder", "lane_new")}
OUTCOME_KINDS = ("builder_mark", "lane_landed")


def _at(ts):
    d = c.parse_ts(ts)
    return d.timestamp() if d else None


def _ids(e):
    return [x.strip() for x in str(e.get("task") or "").split(",") if x.strip()]


def _p90(xs):
    xs = sorted(xs)
    return xs[max(0, math.ceil(0.9 * len(xs)) - 1)]


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _read(p, kinds, since):
    """The ledger lines of these kinds at or after the epoch second `since`."""
    return c.ledger_events(p, kinds, since=c.iso(since))


def outcome_of(evs, now, days=WINDOW_DAYS, closed=()):
    """{n, landed, returned_open, median_h, p90_h, per_hour, window_h} over ledger dicts. A lane landed in the window is
    timed from its first builder return to its landing (when it has a return before the landing). A lane returned but not
    landed is timed to now, unless its task is in `closed`. n counts the timed lanes; median_h and p90_h are None with none.
    per_hour is the lanes landed in the window an hour."""
    marks = {}
    for e in evs:
        ev, t = e.get("event"), _at(e.get("ts"))
        if t is None or ev not in OUTCOME_KINDS:
            continue
        for tid in _ids(e):
            m = marks.setdefault(tid, {})
            if ev == "builder_mark" and isinstance(e.get("data"), dict) and e["data"].get("returned"):
                m.setdefault("returned", t)
            elif ev == "lane_landed":
                m["landed"] = t
    since, hours, landed, stuck = now - days * 86400, [], 0, 0
    for tid, m in marks.items():
        if "landed" in m:
            if since <= m["landed"] <= now:
                landed += 1
                if "returned" in m and m["landed"] >= m["returned"]:
                    hours.append((m["landed"] - m["returned"]) / 3600)
        elif "returned" in m and tid not in closed and m["returned"] <= now:
            stuck += 1
            hours.append((now - m["returned"]) / 3600)
    window_h = days * 24
    return {"n": len(hours), "landed": landed, "returned_open": stuck,
            "median_h": round(statistics.median(hours), 2) if hours else None,
            "p90_h": round(_p90(hours), 2) if hours else None, "per_hour": round(landed / window_h, 3),
            "window_h": window_h}


def verdict(out, census):
    """None while the outcome is within its bars or too few lanes are timed; else {stage, count, fix, ship}. A regression is
    the median or p90 past its bar, or no lane landed while MIN_N lanes returned. The stage is the item stage most items sit
    in (ITEM_STAGES order breaks a tie); idle is named only when tasks are queued and no item stage holds anything. ship is
    its own finding beside the stage, never the stage. Nothing to name: stage None and the fix says so."""
    if out["n"] < MIN_N:
        return None
    stalled = out["landed"] == 0 and out["returned_open"] >= MIN_N
    late = out["median_h"] > MEDIAN_BAR_H or out["p90_h"] > P90_BAR_H
    if not (stalled or late):
        return None
    ship = census.get("ship", 0)
    top = max(ITEM_STAGES, key=lambda s: census.get(s, 0))
    if census.get(top):
        stage, count = top, census[top]
    elif census.get("idle") and census.get("queued"):
        stage, count = "idle", census["idle"]
    else:
        stage, count = None, 0
    fix = FIXES[stage] if stage else (None if ship else "no stage holds items: the hours are spent in the builders' own work")
    return {"stage": stage, "count": count, "fix": fix, "ship": ship}


def effect(adv, count, acted, now):
    """'moved' when the count fell below the advice's count; else 'waiting' inside WAIT_S; after it, 'alarm' when the
    action ran and the count did not move, and 'not_run' when no action of the stage's kinds ran."""
    if count < adv["count"]:
        return "moved"
    if now - adv["at"] < WAIT_S:
        return "waiting"
    return "alarm" if acted else "not_run"


def check_advice(st, census, acted, now):
    """Judge each advice and alarm in state st, changing it in place. 'moved' clears the advice; 'alarm' moves it to the
    stage's alarm; 'waiting' and 'not_run' keep it, so the fix is given once and advise() never gives it again. An alarm
    clears when its stage's count falls below the count it was raised at."""
    for stage, adv in list(st["advice"].items()):
        how = effect(adv, census.get(stage, 0), bool(acted.get(stage)), now)
        if how == "moved":
            del st["advice"][stage]
        elif how == "alarm":
            del st["advice"][stage]
            st["alarm"][stage] = {"count": census.get(stage, 0), "at": now}
    for stage, al in list(st["alarm"].items()):
        if census.get(stage, 0) < al["count"]:
            del st["alarm"][stage]


def advise(st, stage, count, now):
    """The stage's fix, once: None while advice for the stage waits or its alarm is open (the alarm is what the lead reads)."""
    if stage is None or stage in st["alarm"] or stage in st["advice"]:
        return None
    st["advice"][stage] = {"count": count, "at": now}
    return FIXES[stage]


def _path(p):
    return os.path.join(p.dir, "watchdog.json")


def _entries(d):
    """The advice or alarm entries of a state dict as {stage: {count, at}}: a corrupt entry is dropped, so the state heals."""
    if not isinstance(d, dict):
        return {}
    return {s: {"count": int(v["count"]), "at": float(v["at"])} for s, v in d.items()
            if s in STAGES and isinstance(v, dict) and _num(v.get("count")) and _num(v.get("at"))}


def _load(p):
    try:
        with open(_path(p), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    at = st.get("at")
    return {"at": float(at) if _num(at) else None, "advice": _entries(st.get("advice")), "alarm": _entries(st.get("alarm"))}


def note(p, now):
    """The last tick's notes, joined, while that tick is fresh (inside 2 GAP_S); '' otherwise. The self-check leads with it."""
    try:
        with open(_path(p), encoding="utf-8") as f:
            st = json.load(f)
        at, notes = st.get("at"), st.get("note")
        if isinstance(at, (int, float)) and 0 <= now - at < 2 * GAP_S and isinstance(notes, list):
            return "; ".join(str(x) for x in notes if isinstance(x, str))
    except (OSError, ValueError, AttributeError):
        pass
    return ""


def census(p, now, briefs=None):
    """{parked, held, land, ship, idle, queued}: the counts of the open work, each read once."""
    from sampo import lanes, recover, selfcheck
    briefs = c.load_briefs(p) if briefs is None else briefs
    open_ = [b for b in briefs if b.status not in c.CLOSED]
    open_ids = {b.id for b in open_}
    parked = sum(1 for b in open_ if b.meta.get("lane") and lanes.parked(b))
    held = sum(1 for t in (recover.held(p) or {}) if t in open_ids)
    land = len(lanes.awaiting_merge(p, briefs))
    got = selfcheck.unshipped_read(p, now)
    working = lanes._slots(p, briefs)[0]  # (working, done, stalled)
    queued = sum(1 for b in open_ if b.status == "planned" and not b.meta.get("builder"))
    return {"parked": parked, "held": held, "land": land, "ship": got[0] if got else 0,
            "idle": max(0, lanes.builders_cap(p) - len(working)), "queued": queued}


def _acted(p, since):
    """{stage: True} for each stage in since (stage: the time its advice was given) whose action kinds ran at or after it."""
    kinds = tuple({k for s in since for k in ACTS.get(s, ())})
    if not kinds:
        return {}
    evs = _read(p, kinds, min(since.values()))
    return {s: any(e.get("event") in ACTS.get(s, ()) and (_at(e.get("ts")) or 0) >= at for e in evs)
            for s, at in since.items()}


def _alarm_note(stage, al, n, now):
    return (f"the {stage} alarm is open ({int((now - al['at']) / 3600)} h): the {stage} advice ran and {n} item(s) still sit "
            f"at {stage}; the advice is not repeated")


def tick(p, now=None):
    """The tick's pass: None inside GAP_S of the last one; else the state written (outcome, census, advice, alarm, note)."""
    now = time.time() if now is None else now
    st = _load(p)
    if st["at"] is not None and 0 <= now - st["at"] < GAP_S:
        return None
    briefs = c.load_briefs(p)
    closed = {b.id for b in briefs if b.status in c.CLOSED}
    out = outcome_of(_read(p, OUTCOME_KINDS, now - LOOKBACK_DAYS * 86400), now, closed=closed)
    cen = census(p, now, briefs)
    check_advice(st, cen, _acted(p, {s: a["at"] for s, a in st["advice"].items()}), now)
    notes, v = [], verdict(out, cen)
    if v:
        said = []
        for stage, count in ((v["stage"], v["count"]), ("ship", v["ship"])):
            if stage and count:
                fix = advise(st, stage, count, now)
                if fix is None:
                    fix = ("its alarm is open, below" if stage in st["alarm"]
                           else "advice is out; watching whether its count moves")
                said.append(f"{count} item(s) at {stage}: {fix}")
        body = "; ".join(said) or v["fix"]
        notes.append(f"throughput regressed: builder-done to on trunk median {out['median_h']} h, p90 {out['p90_h']} h "
                     f"(bars {MEDIAN_BAR_H} h, {P90_BAR_H} h); {out['landed']} landed in {out['window_h']} h, "
                     f"{out['returned_open']} returned and not landed: {body}")
    notes += [_alarm_note(s, al, cen.get(s, 0), now) for s, al in st["alarm"].items()]
    st.update({"at": now, "outcome": out, "census": cen, "note": notes})
    c.write_atomic(_path(p), json.dumps(st, indent=1, sort_keys=True))
    return st
