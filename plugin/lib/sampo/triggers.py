"""fm triggers (T-2276): which triggers earn their keep. Each capture event names its trigger (capture_once's trigger=,
else the event's source); a captured brief's final status says whether it was worth filing. A trigger whose recent
closed captures were mostly dropped files at low priority (demote, never silence). Stdlib only; fm triggers reads the
ledger and the briefs and writes nothing. T-2355: one table routes every trigger (inbox, queue, lane, muted) with a tier
cap, a daily budget and a burst window; the user sets rows (fm triggers set, the user's to run), the digest moves
routes on outcomes Sampo didn't author (settle_routes), each move an fm decide row."""
import datetime
import json
import os

from sampo import core as c

MIN_CLOSED = 5        # closed captures before a precision counts
WINDOW = 20           # the last this-many closed captures decide
LOW_PRECISION = 0.3
LOOKBACK_DAYS = 180   # ponytail: capture_once reads this much ledger; widen if rare triggers need older history
URGENT = {"baseline", "ci", "ci-red", "security"}  # a red build is never "noise": never demoted
ROUTES = ("inbox", "queue", "lane", "muted")
DEFAULT = {"route": "inbox", "tier_cap": "M", "budget": None, "burst": 0}  # T-2355: the defaults change nothing
PROMOTE, PROMOTE_MIN, MUTE, MUTE_MIN = 0.7, 10, 0.1, 10  # queue: 0.7 of 10+ closed S/M held; muted: under 0.1 of 10+
TIERS = c.TIERS  # T-4220: every tier name; the cap compares by rank


def _since(days):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _captures(p, days, only=None):
    """[(trigger, task id)] oldest first. The user's own captures aren't triggers."""
    out = []
    for e in c.ledger_events(p, kinds=["capture"], since=_since(days)):
        d = e.get("data") or {}
        t = str(d.get("trigger") or d.get("source") or "")
        if t and t != "user" and e.get("task") and (only is None or t == only):
            out.append((t, e["task"]))
    return out


def _status(p):
    return {b.id: b.status for b in c.load_briefs(p, include_archive=True)}


def stats(p, days=LOOKBACK_DAYS, status=None):
    """{trigger: {fired, done, dropped, open, precision}}: precision is done/(done+dropped), None with none closed."""
    status = _status(p) if status is None else status
    rows = {}
    for t, tid in _captures(p, days):
        r = rows.setdefault(t, {"trigger": t, "fired": 0, "done": 0, "dropped": 0, "open": 0})
        r["fired"] += 1
        r[status.get(tid) if status.get(tid) in c.CLOSED else "open"] += 1
    for r in rows.values():
        closed = r["done"] + r["dropped"]
        r["precision"] = round(r["done"] / closed, 3) if closed else None
    return rows


def noisy(p, trigger, caps=None, status=None):
    """(precision, closed) over the trigger's last WINDOW closed captures when that's a demotion, else None. caps
    (all of _captures) and status (_status) may be passed in when a caller asks about many triggers."""
    if trigger in URGENT:
        return None
    caps = _captures(p, LOOKBACK_DAYS, only=trigger) if caps is None else [x for x in caps if x[0] == trigger]
    if len(caps) < MIN_CLOSED:  # too few firings to have enough closes: skip loading every brief
        return None
    status = _status(p) if status is None else status
    closed = [status[tid] for _, tid in caps if status.get(tid) in c.CLOSED][-WINDOW:]
    if len(closed) < MIN_CLOSED:
        return None
    prec = closed.count("done") / len(closed)
    return (prec, len(closed)) if prec < LOW_PRECISION else None


def noisiest(p):
    """The trigger with the most dropped captures and precision under LOW_PRECISION, or None: for fm state. Cached in
    triggers.json keyed on the ledger's size, so a polled fm state doesn't load every brief (T-2276: +90 ms CPU
    here). ponytail: any new ledger line invalidates it; key on the capture/close counts if that bites."""
    path = os.path.join(p.dir, "triggers.json")
    try:
        size = os.path.getsize(os.path.join(p.dir, "ledger.jsonl"))
    except OSError:
        size = -1
    try:
        with open(path) as f:
            hit = json.load(f)
        if hit.get("size") == size:
            return hit.get("noisiest")
    except (OSError, ValueError, AttributeError):
        pass
    status, caps = _status(p), _captures(p, LOOKBACK_DAYS)
    bad = [r for r in stats(p, LOOKBACK_DAYS, status).values() if noisy(p, r["trigger"], caps, status)]
    n = max(bad, key=lambda r: r["dropped"], default=None)
    try:
        c.write_atomic(path, json.dumps({"size": size, "noisiest": n}))
    except OSError:
        pass
    return n


def cmd_set(p, args, cli):
    """T-2355: the user's row for a trigger (the guard keeps fm triggers set the user's to run)."""
    if not args.name:
        raise cli.UsageError("fm triggers set NAME [--route R] [--budget N] [--burst S] [--tier-cap T]")
    with c.lock(p.dir):
        rows = _load(p, "trigger-table.json")
        cur = _override(rows.get(args.name))
        for k, v in (("route", args.route), ("budget", args.budget), ("burst", args.burst), ("tier_cap", args.tier_cap)):
            if v is not None:
                cur[k] = v
        rows[args.name] = dict(_override(cur), set_at=c.now())
        c.write_atomic(os.path.join(p.dir, "trigger-table.json"), json.dumps(rows, sort_keys=True))
    r = row(p, args.name)
    cli.out(args, r, f"{args.name}: route {r['route']}, tier cap {r['tier_cap']}, budget {r['budget'] or 'none'}, "
                       f"burst {r['burst'] or 0} s")


def cmd_triggers(args):
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "action", None) == "set":
        return cmd_set(p, args, cli)
    args.days = min(max(args.days, 1), 3650)
    status, caps = _status(p), _captures(p, LOOKBACK_DAYS)
    rows = sorted(stats(p, args.days, status).values(), key=lambda r: (-r["fired"], r["trigger"]))
    from sampo import trials as strials  # T-2351: self-fixes whose key stayed gone and whose task survived (fm outcomes)
    held = strials.held_fixes(p)
    lines = [f"{'trigger':<22}{'fired':>6}{'done':>6}{'dropped':>8}{'open':>6}{'precision':>10}{'fixes held':>12}"
             f"  route"]
    for r in rows:
        r["fixes_held"] = held.get(r["trigger"], 0)
        r.update({k: v for k, v in row(p, r["trigger"]).items() if k in ("route", "tier_cap", "budget", "burst", "by")})
        prec = "-" if r["precision"] is None else f"{r['precision']:.2f}"
        flag = "  (files at low priority)" if noisy(p, r["trigger"], caps, status) else ""
        lines.append(f"{r['trigger']:<22}{r['fired']:>6}{r['done']:>6}{r['dropped']:>8}{r['open']:>6}{prec:>10}"
                     f"{r['fixes_held']:>12}  {r['route']}{flag}")
    from sampo import lanes  # T-2364: the lanes recovering and the self-fix trials ride along (the MCP tool reads them)
    briefs = c.load_briefs(p)
    recoveries = [{"task": t, "cause": v[0], "recovery": lanes.recovery_text(v[0], t, v[1])}
                  for t, v in sorted(lanes.stop_causes(p, briefs).items()) if v[0] != "no_report"]
    trials = [{"task": t.get("task"), "what": c.plain(str(t.get("what") or ""))[:120], "verdict": t.get("verdict")}
              for t in strials.load(p) if isinstance(t, dict) and t.get("kind") == "self-fix"][-20:]
    sr = self_running(p, briefs)
    cli.out(args, {"days": args.days, "triggers": rows, "recoveries": recoveries, "trials": trials,
                     "self_running": sr},
              ("\n".join(lines) if rows else f"No trigger has filed a task in the last {args.days} days.")
              + (f"\n{sr['line']}" if sr["line"] else ""))


def _load(p, name):
    try:
        with open(os.path.join(p.dir, name), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _override(v):
    """A user row (trigger-table.json), every field coerced; a bad value is dropped, never an error."""
    out = {}
    if not isinstance(v, dict):
        return out
    if v.get("route") in ROUTES:
        out["route"] = v["route"]
    if v.get("tier_cap") in TIERS:
        out["tier_cap"] = v["tier_cap"]
    for k in ("budget", "burst"):
        x = v.get(k)
        if isinstance(x, int) and not isinstance(x, bool) and x >= 0:
            out[k] = x or (None if k == "budget" else 0)
    return out


def row(p, trigger):
    """T-2355: the trigger's row: the defaults, Sampo's settled route, then the user's row; an urgent trigger is never
    muted and has no budget or burst. Two small JSON reads, so capture_once can afford it."""
    r = dict(DEFAULT, trigger=trigger, by="default")
    auto = _load(p, "trigger-routes.json").get(trigger)
    if auto in ROUTES:
        r.update(route=auto, by="foreman")
    user = _override(_load(p, "trigger-table.json").get(trigger))
    r.update(user)
    if "route" in user:
        r["by"] = "user"
    if trigger in URGENT:
        r.update(budget=None, burst=0, route="inbox" if r["route"] == "muted" else r["route"])
    return r


def table(p, status=None):
    """{trigger: row} for every trigger that filed a capture or has a row."""
    names = set(stats(p, LOOKBACK_DAYS, status)) | set(_load(p, "trigger-table.json")) | set(_load(p, "trigger-routes.json"))
    return {t: row(p, t) for t in sorted(names)}


def over_budget(p, trigger, r):
    """Why a firing joins the trigger's open capture instead of filing another: past its daily budget (local day) or
    within its burst window of the last filing; None when it files (always for an urgent trigger)."""
    if trigger in URGENT or not (r.get("budget") or r.get("burst")):
        return None
    now = datetime.datetime.now().astimezone()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(datetime.timezone.utc)
    since = (day if not r.get("burst") else min(day, now.astimezone(datetime.timezone.utc)
                                                  - datetime.timedelta(seconds=r["burst"])))
    ts = [str(e.get("ts") or "") for e in c.ledger_events(p, ["capture"], since=since.strftime("%Y-%m-%dT%H:%M:%SZ"))
          if str((e.get("data") or {}).get("trigger") or "") == trigger]
    if r.get("budget") and sum(t >= day.strftime("%Y-%m-%dT%H:%M:%SZ") for t in ts) >= r["budget"]:
        return f"past its daily budget of {r['budget']}"
    edge = (now.astimezone(datetime.timezone.utc) - datetime.timedelta(seconds=r.get("burst") or 0)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    if r.get("burst") and ts and max(ts) >= edge:
        return f"within its {r['burst']} s burst window"
    return None


def lane_only(p, trigger, tier, type_):
    """A queue- or lane-routed trigger's capture starts only in a lane (a branch, never main), within its tier cap; L and
    SECURITY captures stay in the inbox whatever the route (T-4220: so do the big tiers)."""
    r = row(p, trigger)
    return (r["route"] in ("queue", "lane") and c.tier_rank(tier) < c.tier_rank("L") and tier in TIERS[:TIERS.index(r["tier_cap"]) + 1]
            and type_ != "SECURITY")


def decide_route(p, trigger, closed, held, sm=None):
    """(route, why) from the trigger's last closed captures (statuses) and how many of its closed S/M ones held in fm
    outcomes: muted (under MUTE of MUTE_MIN+), inbox-low (T-2276's demotion), queue (PROMOTE of PROMOTE_MIN+ closed
    S/M held), else inbox. Checked in that order; an urgent trigger is never muted or demoted."""
    n, done = len(closed), closed.count("done")
    sm = n if sm is None else sm
    if trigger not in URGENT and n >= MUTE_MIN and done / n < MUTE:
        return "muted", f"{done}/{n} of its last closed captures were done (under {MUTE})"
    if trigger not in URGENT and n >= MIN_CLOSED and done / n < LOW_PRECISION:
        return "inbox-low", f"{done}/{n} of its last closed captures were done (under {LOW_PRECISION})"
    if sm >= PROMOTE_MIN and held / sm >= PROMOTE:
        return "queue", f"{held}/{sm} of its last closed S/M captures held in fm outcomes (survived, no rework)"
    return "inbox", f"{done}/{n} done, {held}/{sm} held"


def settle_routes(p):
    """T-2355: move each trigger's route on its outcomes (the digest calls it), skipping a user-set route; each move is
    an fm decide row and lands in trigger-routes.json. {trigger: new route} for the moves made."""
    from sampo import cli
    from sampo import outcomes
    status, caps = _status(p), _captures(p, LOOKBACK_DAYS)
    tiers = {b.id: b.tier for b in c.load_briefs(p, include_archive=True)}
    outs, user = outcomes.outcomes(p), _load(p, "trigger-table.json")
    cur, moved = _load(p, "trigger-routes.json"), {}
    for t in sorted({x for x, _ in caps}):
        if "route" in _override(user.get(t)):
            continue
        ids = [i for x, i in caps if x == t]
        closed = [status[i] for i in ids if status.get(i) in c.CLOSED][-WINDOW:]
        sm = [i for i in ids if status.get(i) in c.CLOSED and tiers.get(i) in ("S", "M")][-WINDOW:]
        held = sum((outs.get(i) or {}).get("fate") == "held" and (outs.get(i) or {}).get("survival") == "survived"
                   for i in sm)
        route, why = decide_route(p, t, closed, held, len(sm))
        route = "inbox" if route == "inbox-low" else route  # the low priority stays noisy()'s, live at capture time
        if cur.get(t, "inbox") != route:
            moved[t] = route
            cli._write_decision(p, f"Trigger {t} routes to {route}", why=why)
            cur[t] = route
    if moved:
        c.write_atomic(os.path.join(p.dir, "trigger-routes.json"),
                       json.dumps({k: v for k, v in cur.items() if v != "inbox"}, sort_keys=True))
    return moved


def self_running(p, briefs=None):
    """T-2364: what runs here by itself: triggers fired today (each with its route), installs waiting for the user's
    yes, builder lanes recovering, self-fix trials not yet graded, the prediction book's next-request hit rate beside
    its baseline (T-2406); with the one line fm state, the band and fm code print ("" when nothing ran or waits)."""
    from sampo import foresight
    from sampo import grants
    from sampo import lanes
    from sampo import trials as strials
    briefs = c.load_briefs(p) if briefs is None else briefs
    day = datetime.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    fired = {}
    for e in c.ledger_events(p, ["capture"], since=day.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")):
        t = (e.get("data") or {}).get("trigger")
        if isinstance(t, str) and t:
            fired[t] = fired.get(t, 0) + 1
    installs = len(grants.queued(p, briefs))
    recovering = sum(1 for v in lanes.stop_causes(p, briefs).values() if v[0] != "no_report")
    due = sum(1 for t in strials.load(p) if isinstance(t, dict) and t.get("kind") == "self-fix" and not t.get("verdict"))
    pk = next((k for k in foresight.prediction_kinds(p) if k["kind"] == "next-request"), None)
    s = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"  # noqa: E731
    parts = []
    if fired:
        parts.append(f"{s(sum(fired.values()), 'trigger')} fired today ("
                     + ", ".join(f"{t} → {row(p, t)['route']}" for t in sorted(fired)) + ")")
    if installs:
        parts.append(f"{s(installs, 'install')} waiting")
    if recovering:
        parts.append(f"{s(recovering, 'lane')} recovering")
    if due:
        parts.append(f"{s(due, 'self-fix trial')} due")
    if pk and pk["n"] and pk["hit"] is not None and pk["base_hit"] is not None:
        from sampo import foresight  # T-3069: a lead is a paired sign test, not a gap of a few rows
        parts.append(f"next-request predictions {pk['hit']:.0%} vs baseline {pk['base_hit']:.0%}"
                     f" ({foresight.lead_text(pk['lead'])})")
    return {"fired": fired, "installs": installs, "recovering": recovering, "trials_due": due, "predictions": pk,
            "line": ("Self-running: " + " · ".join(parts)) if parts else ""}
