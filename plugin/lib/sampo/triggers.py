"""fm triggers (T-2276): which triggers earn their keep. Each capture event names its trigger (capture_once's trigger=,
else the event's source); a captured brief's final status says whether it was worth filing. A trigger whose recent
closed captures were mostly dropped files at low priority (demote, never silence). Stdlib only; fm triggers reads the
ledger and the briefs and writes nothing. T-2355: one table routes every trigger (inbox, queue, lane, muted) with a tier
cap, a daily budget and a burst window; the user sets rows (fm triggers set, the user's to run), the digest moves
routes on outcomes Sampo didn't author (settle_routes), each move an fm decide row."""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

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
UNLIMITED = "unlimited"  # T-5897: a user's budget with no cap (row["budget"]; allowance() is None, nothing is held for it)


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


def budget_arg(text):
    """T-5897: --budget's value: a whole number (0 clears it) or the word unlimited."""
    if str(text).strip().lower() == UNLIMITED:
        return UNLIMITED
    try:
        n = int(text)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError(f"{text!r}: a whole number (0 clears it) or {UNLIMITED}")
    return n


def set_row(p, name, route=None, budget=None, burst=None, tier_cap=None):
    """The user's row for a trigger, the fields given changed under the project lock. The one writer: fm triggers set
    (the user's to run) and the user's own words in the prompt hook (T-5897: sampo.userwords) both come here."""
    with c.lock(p.dir):
        rows = _load(p, "trigger-table.json")
        cur = _override(rows.get(name))
        for k, v in (("route", route), ("budget", budget), ("burst", burst), ("tier_cap", tier_cap)):
            if v is not None:
                cur[k] = v
        rows[name] = dict(_override(cur), set_at=c.now())
        c.write_atomic(os.path.join(p.dir, "trigger-table.json"), json.dumps(rows, sort_keys=True))


def cmd_set(p, args, cli):
    """T-2355: the user's row for a trigger (the guard keeps fm triggers set the user's to run)."""
    if not args.name:
        raise cli.UsageError("fm triggers set NAME [--route R] [--budget N] [--burst S] [--tier-cap T]")
    set_row(p, args.name, route=args.route, budget=args.budget, burst=args.burst, tier_cap=args.tier_cap)
    r = row(p, args.name)
    cli.out(args, r, f"{args.name}: route {r['route']}, tier cap {r['tier_cap']}, budget {r['budget'] or 'none'}, "
                       f"burst {r['burst'] or 0} s")


def cmd_triggers(args):
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "action", None) == "set":
        return cmd_set(p, args, cli)
    if getattr(args, "held", False):
        return cmd_held(p, args, cli)
    if getattr(args, "replay", False):
        return cmd_replay(p, args, cli)
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


def cmd_held(p, args, cli):
    """fm triggers --held: what waits for a day with room, best first (a pure read)."""
    cutoff = _cutoff(_local_now())
    items = [e for _, e in sorted(enumerate(held(p)), key=lambda x: _rank(x[1], x[0], cutoff))]
    allow = {s: allowance(p, s) for s in FOLDED}
    over = sum(_overdue(e, cutoff) for e in items)
    lines = [f"{e['source']:<11}{e.get('type') or '-':<9}{_one_line(e['text'], 90)}  (held {str(e.get('held_at') or '')[:10]}"
             f"{', overdue' if _overdue(e, cutoff) else ''})" for e in items]
    cli.out(args, {"held": items, "overdue": over, "allowance": allow},
            (f"{len(items)} held" + (f", {over} overdue (held over {MAX_WAIT_DAYS} days: they file first, past the allowance)"
                                     if over else "")
             + f", filed best-first as each trigger's daily allowance has room "
             f"(allowance a day: {', '.join(f'{s} {UNLIMITED if a is None else a}' for s, a in allow.items())}):\n" + "\n".join(lines))
            if items else "Nothing is held.")


def cmd_replay(p, args, cli):
    """fm triggers --replay --days N: tasks the fold would have kept out of the last N days (a pure read)."""
    days = min(max(args.days, 1), 365)
    r = replay(p, days)
    by = "; ".join(f"{t} {v['before']} → {v['after']}" for t, v in sorted(r["by_trigger"].items()))
    cli.out(args, {"replay": r},
            f"In the last {days} days {r['before']} discovered and followup tasks were filed through captures; with the fold, "
            f"the append and the allowance: filed {r['before']} before, {r['after']} after ({r['folded']} folded, "
            f"{r['appended']} appended, {r['held']} still held; none lost)"
            + (f". Per trigger: {by}" if by else ".")
            + f" Not counted: {r['direct']} filed directly by a check, a census or a doctor (they never go through the fold).")


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
        if k == "budget" and x == UNLIMITED:
            out[k] = x
        elif isinstance(x, int) and not isinstance(x, bool) and x >= 0:
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
    cap = r.get("budget") if r.get("budget") != UNLIMITED else None  # T-5897: unlimited is no cap
    if trigger in URGENT or not (cap or r.get("burst")):
        return None
    now = datetime.datetime.now().astimezone()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(datetime.timezone.utc)
    since = (day if not r.get("burst") else min(day, now.astimezone(datetime.timezone.utc)
                                                  - datetime.timedelta(seconds=r["burst"])))
    ts = [str(e.get("ts") or "") for e in c.ledger_events(p, ["capture"], since=since.strftime("%Y-%m-%dT%H:%M:%SZ"))
          if str((e.get("data") or {}).get("trigger") or "") == trigger]
    if cap and sum(t >= day.strftime("%Y-%m-%dT%H:%M:%SZ") for t in ts) >= cap:
        return f"past its daily budget of {cap}"
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


# ---- T-4743: the fold, the append, the learned allowance and the held list. discovered and followup captures were a
# project's whole inbox (one project: 340 and 77 against ~60 requests); an item an open task already owns goes onto that
# task, and a trigger files no more a day than its own record earns (unless the user sets its budget: T-5722). The user's
# captures and the urgent triggers never
# come through here. Callers hold the project lock (cmd_capture, cmd_research); none of these takes it.
FOLDED = ("discovered", "followup")
DAILY_MAX = 40        # a trigger with a clean record may file this many a day (T-5722: was 12)
MIN_ALLOW = 2         # and one with a bad record this many: demoted, never silenced (as noisy())
TRAIL_DAYS = 14       # the closed captures of this many days are the record the allowance follows
DRAIN_LOOKS = 40      # a drain checks this many held items against the open tasks, no more
MAX_WAIT_DAYS = 7     # a held item files by then, past the allowance: the allowance puts a finding off, never loses it
HELD_FILE = "held-captures.json"  # unbounded and uncut: what a trigger found stays whole until a task holds it
FOLLOW = "Follow-ups captured"  # the brief template's own section
_AS = {"promise": "followup"}   # capture_promises' events name the source "promise"
_ASLEEP = ("deferred", "blocked")  # nobody is on these: a finding put on one would go nowhere
_PROVENANCE = ("from", "finder", "incident", "trigger", "carried_from")  # a capture event that says where it came from
_LINE = re.compile(r"^- \[(?P<mark>[^\]]*)\] (?P<body>.*?) \(f-(?P<key>[0-9a-f]{8}) (?P<src>\w+)\)"
                   r"(?: — whole text: (?P<rel>audits/[\w.-]+\.md))?\s*$")


_TEXT_WIDTH = 240  # what one line of a log or follow-up holds; the rest of a finding is a file, linked from the line


def _one_line(text, width=_TEXT_WIDTH):
    return c.fit(c.plain(c.redact(" ".join(str(text or "").split()))), width)


def exempt(type_, source, priority=None):
    """True for what the fold, the allowance and the held list never touch, judged by fields the capture recorded: a
    trigger other than discovered and followup (a user's ask, a red build, CI, the urgent triggers), a SECURITY item, and
    one recorded urgent (--urgent or FIX!:; a baseline-breaking finding carries no other mark, so a builder that finds one
    says --urgent)."""
    return _AS.get(source, source) not in FOLDED or type_ == "SECURITY" or priority == "urgent"


def named_paths(text):
    """The files an item names, in order: a token with a directory and a file ending, or a bare name with an ending
    (path:line and path::test cut to the path). Judged by shape only, through tidy's own path test."""
    from sampo import tidy
    out = []
    for tok in re.findall(r"[^\s`'\"(),;<>\[\]]+", str(text or "")):
        tok = re.sub(r":\d+(?::\d+)?$", "", tidy.path_part(tok.strip(":!?"))).rstrip(".:")
        tok = tok[2:] if tok.startswith("./") else tok
        if len(tok) < 3 or not tidy._looks_like_path(tok):
            continue
        last = tok.rsplit("/", 1)[-1]
        ended = bool(re.fullmatch(r"[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{1,5}", last))
        if ("/" in tok and not (ended or tok.count("/") >= 2)) or ("/" not in tok and not ended):
            continue  # "and/or" and "v1.2" are no file
        if tok not in out:
            out.append(tok)
    return out


def _scope_rank(path, scope):
    """None when no entry of a task's scope covers the file an item names (a bare file name matches a scope path ending
    in it), else (has a wildcard, minus its literal path segments) of the narrowest entry that does: an exact file
    ranks before a directory glob, and that before a broad one."""
    best = None
    for s in scope if isinstance(scope, list) else ():
        s = str(s).strip()
        if not s.strip("*/."):  # '**' or '.' covers everything: it owns nothing in particular
            continue
        if c.glob_match(path, s) or ("/" not in path and (s == path or s.endswith("/" + path))):
            parts = [x for x in s.split("/") if x]
            r = ("*" in s or "?" in s or "[" in s, -sum(1 for x in parts if not re.search(r"[*?\[]", x)))
            best = r if best is None or r < best else best
    return best


def _in_scope(path, scope):
    """Whether a task's scope covers the file an item names."""
    return _scope_rank(path, scope) is not None


def _local_now():
    return datetime.datetime.now().astimezone()


def _day_start(now):
    return now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


def _utc(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def allowance(p, trigger):
    """How many tasks a day this trigger may file: DAILY_MAX until TRAIL_DAYS of its captures hold MIN_CLOSED closed
    ones, then DAILY_MAX times its precision (done of done and dropped), not under MIN_ALLOW; a budget the user set is
    the allowance instead, raising it past the record or capping it under (T-5722). None for an urgent trigger, for
    one the user set to unlimited (T-5897) and for one that isn't folded."""
    trigger = _AS.get(trigger, trigger)
    if trigger in URGENT:
        return None
    budget = row(p, trigger).get("budget")
    if budget:
        return None if budget == UNLIMITED else budget  # T-5897: the user lifted the limit
    caps = [(t, i) for t, i in _captures(p, TRAIL_DAYS) if _AS.get(t, t) == trigger]
    n = DAILY_MAX
    if len(caps) >= MIN_CLOSED:
        status = _status(p)
        closed = [status[i] for _, i in caps if status.get(i) in c.CLOSED]
        if len(closed) >= MIN_CLOSED:
            n = max(MIN_ALLOW, round(DAILY_MAX * closed.count("done") / len(closed)))
    return n


def filed_today(p, trigger, now=None):
    """Tasks this trigger filed through the fold on the local day of now: the capture events naming it as trigger or
    source that carry the fold's own mark (via). A direct creator's filing (a red base, a census, a doctor finding: a
    brief of its own, deduped by its own key) is no capture the allowance should crowd out."""
    day = _day_start(now or _local_now())
    end, n = _utc(day + datetime.timedelta(days=1)), 0
    for e in c.ledger_events(p, ["capture"], since=_utc(day)):
        d = e.get("data") or {}
        t = str(d.get("trigger") or d.get("source") or "")
        n += d.get("via") == "fold" and _AS.get(t, t) == trigger and str(e.get("ts") or "") < end
    return n


def _room(p, trigger, now):
    """How many more tasks the trigger may file today; None without a limit."""
    a = allowance(p, trigger)
    return None if a is None else max(0, a - filed_today(p, trigger, now))


def held(p):
    """The items waiting for a day with room (oldest first), as written by _hold. No file: []. A file that can't be read
    is c.CorruptState naming it: a capture stops rather than write over what a trigger found (sampo fsck)."""
    try:
        with open(os.path.join(p.dir, HELD_FILE), encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        raise c.CorruptState(f"{HELD_FILE} in the project folder can't be read ({type(e).__name__}): nothing was written; "
                             f"mend it or move it aside") from None
    if not isinstance(d, list) or not all(isinstance(x, dict) and isinstance(x.get("text"), str)
                                          and x.get("source") in FOLDED for x in d):
        raise c.CorruptState(f"{HELD_FILE} in the project folder isn't a list of held items: nothing was written; "
                             f"mend it or move it aside")
    return d


def _write_held(p, items):
    path = os.path.join(p.dir, HELD_FILE)
    if items:
        c.write_atomic(path, json.dumps(items, ensure_ascii=False))
    elif os.path.exists(path):
        os.remove(path)


def _overdue(e, cutoff):
    held_at = str(e.get("held_at") or "")
    return bool(cutoff and held_at and held_at <= cutoff)


def _rank(e, i=0, cutoff=None):
    """What files first. An item held for MAX_WAIT_DAYS (held_at at or before cutoff) first, the oldest first: waiting
    is the cost the allowance may put on a finding, never losing it. Then the best: the source's value over the tier's
    effort, a FIX before the rest, then the oldest."""
    if _overdue(e, cutoff):
        return (0, str(e.get("held_at")), i)
    tier = e.get("tier") if e.get("tier") in c.TIER_EFFORT else "M"
    return (1, -c.SOURCE_VALUE.get(e.get("source"), 1) / c.TIER_EFFORT[tier], e.get("type") != "FIX",
            str(e.get("held_at") or ""), i)


def _cutoff(now):
    return _utc(now - datetime.timedelta(days=MAX_WAIT_DAYS))


def _record(p, kind, tid, source, type_):
    from sampo import cli
    c.log_event(p, "capture_folded", task=tid, data={"kind": kind, "source": source, "type": type_}, session=cli.session())


def _finding(text):
    """(key, whole, short, long): the item's own id (the first 8 hex of its redacted text's hash, whitespace folded),
    the whole redacted text, its one-line form, and whether a line can't hold it (several lines, or over _TEXT_WIDTH)."""
    whole = c.redact(str(text).strip())
    flat = " ".join(whole.split())
    return hashlib.sha1(flat.encode("utf-8")).hexdigest()[:8], whole, _one_line(text), "\n" in whole or len(flat) > _TEXT_WIDTH


def _sidecar(p, host, key, whole):
    """The file (relative to the project folder, written once) that holds a finding too long for a line."""
    rel = f"audits/{host}.finding-{key}.md"
    path = os.path.join(p.dir, rel)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c.write_atomic(path, whole + "\n")
    return rel


def _near_host(p, text, type_, scope):
    """The open, awake task whose title nearly repeats the item's (recall's near-match, the best share): one of the item's
    type first, else one of another type (T-1726: an agent's line is typed by a guess at its words, so the guess alone
    never keeps it off the task that covers it)."""
    from sampo import cli
    from sampo import recall
    q, names = recall._title_tokens(cli._title(text))
    sc, best = cli._scope_paths(scope), {}
    for b in c.load_briefs(p) if len(q) >= 2 else ():
        if b.status not in c.OPEN or b.status in _ASLEEP:
            continue
        d, theirs = recall._title_tokens(b.title)
        j = recall.near_share(q, names, sc, d, theirs, b.meta.get("scope") or [])
        kind = b.type == type_
        if j is not None and (kind not in best or j > best[kind][0]):
            best[kind] = (j, b)
    hit = best.get(True) or best.get(False)
    return hit[1] if hit else None


def _in_lane(b):
    """T-5363: the task has a lane (a builder holds it, or it merged and waits on its close): no builder reads a follow-up
    appended now, and the close gate would refuse the close until someone settled the line by hand."""
    return bool(b.meta.get("lane")) or str(b.meta.get("lane_stop") or "").startswith("merged")


def _place(p, text, type_, source, scope=(), under=None, note=None):
    """('folded', id): the item nearly repeats an open task, of its type first (recall's near-match), and goes on its log.
    ('appended', id): it names a file inside an open task's scope and goes on that task's follow-ups: the task in hand,
    else the narrowest scope, then the same type, the one being worked, the oldest. Neither goes onto a deferred or
    blocked task, and an append skips a task that has a lane (_in_lane). (None, None): it needs a task of its own. The whole text is kept: a line holds it up to _TEXT_WIDTH,
    a file in the project folder the rest, linked from the line."""
    key, whole, short, long_ = _finding(text)
    hit = _near_host(p, text, type_, scope)
    if hit is not None:
        if f"(f-{key})" not in hit.section("Log"):  # a hook that runs twice adds one line
            link = f" — whole text: {_sidecar(p, hit.id, key, whole)}" if long_ else ""
            hit.append_log(f"{note or f'folded in from a {source} capture'}: {short} (f-{key}){link}")
            c.save_brief(p, hit)
        return "folded", hit.id
    paths = named_paths(text)
    if not paths:
        return None, None
    cands = []
    for b in c.load_briefs(p):
        if b.status in c.OPEN and b.status not in _ASLEEP and not _in_lane(b):
            ranks = [r for r in (_scope_rank(x, b.meta.get("scope")) for x in paths) if r is not None]
            if ranks:
                cands.append((b, min(ranks)))
    if not cands:
        return None, None
    pick = min(cands, key=lambda x: (x[0].id != under, x[1], x[0].type != type_, x[0].status not in ("active", "verifying"),
                                     x[0].status not in c.RUNNABLE, c.id_num(x[0].id)))[0]
    if f"(f-{key} " not in pick.section(FOLLOW):
        link = f" — whole text: {_sidecar(p, pick.id, key, whole)}" if long_ else ""
        body = pick.section(FOLLOW).strip("\n")
        pick.set_section(FOLLOW, (body + "\n" if body else "") + f"- [ ] {type_}: {short} (f-{key} {_AS.get(source, source)}){link}\n")
        c.save_brief(p, pick)
    return "appended", pick.id


def _hold(p, entry):
    """'added' when the entry joins the held list, 'same' when it is already there, 'merged' when it is there and brings
    steps the held one lacks (a rerun of a report). Nothing leaves the list but by filing."""
    items = held(p)
    for x in items:
        if x["text"] == entry["text"] and x["source"] == entry["source"]:
            more = [s for s in entry.get("steps") or () if s not in (x.get("steps") or [])]
            if not more:
                return "same"
            x["steps"] = list(x.get("steps") or []) + more
            if entry.get("raw"):
                x["raw"] = entry["raw"]
            _write_held(p, items)
            return "merged"
    items.append(entry)
    _write_held(p, items)
    return "added"


def file_item(p, e):
    """A held item filed as the task it would have been; its id. Its capture event carries the fold's mark (via) unless the
    item is exempt or the entry says "direct" (a follow-up taken back as a task of its own)."""
    from sampo import cli
    type_ = e.get("type") if e.get("type") in c.TYPES else "FEATURE"
    urgent = e.get("priority") == "urgent"
    b = cli._create(p, cli._title(e["text"]), type_, e.get("tier") if e.get("tier") in c.TIERS else
                    c.guess_tier(type_, e["text"]), "captured", raw=e.get("raw") or e["text"], scope=e.get("scope") or (),
                    source=e["source"], net=False, priority="urgent" if urgent else "normal")
    steps = [s for s in e.get("steps") or () if isinstance(s, str) and s.strip()]
    if steps:  # a report's items about one file: each is a step, readable in the task
        for s in steps:
            b.add_step(s)
        c.save_brief(p, b, touch=False)
    data = {"source": e["source"], "type": type_, "from": e.get("from") or "held"}
    if not e.get("direct") and not exempt(type_, e["source"], "urgent" if urgent else None):
        data["via"] = "fold"
    c.log_event(p, "capture", task=b.id, data=data, session=cli.session())
    return b.id


def group_items(items, name):
    """Entries for a report's noticed items [(text, type)]: those naming the same file make one entry whose steps are
    the items (scope: that file, so later items about it are appended to it); the rest stay one entry each."""
    areas, order = {}, []
    for text, type_ in items:
        k = named_area(text) or len(order)  # no file named: its own entry
        if k not in areas:
            areas[k] = []
            order.append(k)
        areas[k].append((text, type_))
    out = []
    for k in order:
        got = areas[k]
        tier_text = " ".join(t for t, _ in got)
        if len(got) == 1:
            text, type_ = got[0]
            out.append({"text": text, "raw": f"{text}\n(noticed by an agent; research {name})", "type": type_,
                        "source": "discovered", "from": name, "tier": c.guess_tier(type_, text)})
            continue
        type_ = "FIX" if any(t == "FIX" for _, t in got) else got[0][1]
        out.append({"text": f"{k}: {len(got)} findings noticed in {name}", "type": type_, "source": "discovered",
                    "from": name, "steps": [t for t, _ in got], "scope": [k] if "/" in k else [],
                    "tier": c.guess_tier(type_, tier_text),
                    "raw": "\n".join(f"- {t}" for t, _ in got) + f"\n(noticed by an agent; research {name})"})
    return out


def file_or_hold(p, e, now=None):
    """The id of the task an entry files as, or None when its trigger is past today's allowance and it is held."""
    now = now or _local_now()
    if exempt(e.get("type"), e["source"], e.get("priority")):
        return file_item(p, e)
    room = _room(p, e["source"], now)
    if room is not None and room <= 0:
        if _hold(p, dict(e, held_at=c.now(), why=f"{e['source']} is past its allowance of {allowance(p, e['source'])} a day")) == "added":
            _record(p, "held", None, e["source"], e.get("type"))
        return None
    return file_item(p, e)


def fold(p, text, type_, source, scope=(), under=None, raw=None, note=None, now=None, limit=True, priority=None):
    """(kind, task id) for an item a discovered or followup trigger found: ('folded', id) onto an open task of its type
    it nearly repeats, ('appended', id) onto the follow-ups of the one whose scope holds the file it names, ('held', None)
    in the held list when the trigger is past its allowance today, else (None, None): the caller files it. A user's
    capture, an urgent trigger (a red build), a SECURITY item and one recorded urgent are always (None, None). Held items
    from earlier days are filed first."""
    source = _AS.get(source, source)
    if source not in FOLDED or exempt(type_, source, priority) or not str(text or "").strip():
        return None, None
    now = now or _local_now()
    drain_due(p, now)
    kind, tid = _place(p, text, type_, source, scope, under, note)
    if kind is None and limit:  # limit=False: the caller groups what is left and holds by the group (file_or_hold)
        room = _room(p, source, now)
        if room is not None and room <= 0:
            res = _hold(p, {"text": c.redact(str(text).strip()), "raw": c.redact(raw) if raw else raw, "type": type_,
                            "source": source, "scope": list(scope or ()), "under": under,
                            "tier": c.guess_tier(type_, str(text)), "held_at": c.now(),
                            "why": f"{source} is past its allowance of {allowance(p, source)} a day"})
            if res != "added":  # a second run of the same: it is held already, nothing to count
                return "held", None
            kind = "held"
    if kind:
        _record(p, kind, tid, source, type_)
    return kind, tid


def drain(p, now=None):
    """File the held items the day has room for, best first; the ids filed. An item held for MAX_WAIT_DAYS files first,
    oldest first, past the day's room. An item that an open task covers by now goes onto it instead and takes no room.
    The room is each trigger's allowance less what it filed on the day of now."""
    items = held(p)
    if not items:
        return []
    now = now or _local_now()
    cutoff = _cutoff(now)
    room, keep, filed, looked = {}, set(range(len(items))), [], 0
    for i in sorted(keep, key=lambda i: _rank(items[i], i, cutoff)):
        e = items[i]
        if e["source"] not in room:
            room[e["source"]] = _room(p, e["source"], now)
        if room[e["source"]] is not None and room[e["source"]] <= 0 and not _overdue(e, cutoff):
            continue  # no room left for this trigger today: it keeps waiting, unlooked at (each look reads every brief)
        looked += 1
        if looked > DRAIN_LOOKS:  # a pile of items an open task covers can't hold the lock for long
            break
        kind, tid = _place(p, e["text"], e.get("type"), e["source"], e.get("scope") or (), e.get("under"))
        if kind:
            _record(p, kind, tid, e["source"], e.get("type"))
            keep.discard(i)
            continue
        filed.append(file_item(p, e))
        keep.discard(i)
        if room[e["source"]] is not None:
            room[e["source"]] = max(0, room[e["source"]] - 1)
    if len(keep) != len(items):
        _write_held(p, [items[i] for i in sorted(keep)])
        c.regen_views(p)
    return filed


def drain_due(p, now=None):
    """drain, when an item has waited past midnight (a new day is the only thing that makes room): a file read when the
    list is empty or all of today's, so a capture or a digest pays nothing for it."""
    now = now or _local_now()
    today = _utc(_day_start(now))
    return drain(p, now) if any(str(e.get("held_at") or "") < today for e in held(p)) else []


def fold_counts(p, now=None):
    """What the fold did today: {folded, appended, held}."""
    n = dict.fromkeys(("folded", "appended", "held"), 0)
    for e in c.ledger_events(p, ["capture_folded"], since=_utc(_day_start(now or _local_now()))):
        k = (e.get("data") or {}).get("kind")
        if k in n:
            n[k] += 1
    return n


def named_area(text):
    """The first file an item names, or None: the area a report's items are filed under."""
    return next(iter(named_paths(text)), None)


def replay(p, days):
    """What the last `days` days would have filed with the fold, append and allowance: {before, after, folded, appended,
    held, direct, by_trigger}, and before == after + folded + appended + held. before: the discovered and followup
    tasks filed through captures; after: those the fold would still file (direct or from the held list later); held: what
    still waited at the end; direct: filings of a check, a census or a doctor (their own brief and key: never folded,
    never counted against the allowance). A capture event is told from a direct one by the fold's mark (via), and
    before the mark existed by having no sign of a creator of its own (from, finder, incident, trigger): a legacy
    doctor, rune or replay filing carries none, so it counts as a capture. Reads the ledger and the briefs, writes
    nothing. Approximations: a task counts as closed when its brief was last touched (updated), and a deferred or
    blocked one is asleep throughout; each item is judged by its title and raw request."""
    from sampo import recall
    since = _since(days)
    evs = []
    for e in c.ledger_events(p, ["capture"], since=_utc(datetime.datetime.now(datetime.timezone.utc)
                                                         - datetime.timedelta(days=days + TRAIL_DAYS))):
        d = e.get("data") or {}
        t = str(d.get("trigger") or d.get("source") or "")
        if e.get("task") and _AS.get(t, t) in FOLDED:
            evs.append([str(e.get("ts") or ""), e["task"], _AS.get(t, t), d])
    marked = [x[0] for x in evs if x[3].get("via") == "fold"]
    first_mark = min(marked) if marked else None
    recs = {}
    for b in c.load_briefs(p, include_archive=True):
        q, names = recall._title_tokens(b.title)
        raw = " ".join(x.lstrip("> ") for x in b.section("Raw request").splitlines())
        recs[b.id] = {"id": b.id, "created": str(b.meta.get("created") or ""), "status": b.status, "type": b.type,
                      "close": str(b.meta.get("updated") or "") if b.status in c.CLOSED else None,
                      "q": q, "names": names, "scope": b.meta.get("scope") or [], "tier": b.tier,
                      "text": f"{b.title} {raw}", "source": b.meta.get("source"), "priority": b.meta.get("priority")}
    for x in evs:  # what went through the fold: its mark, or, before the mark existed, no sign of a creator of its own
        r = recs.get(x[1])
        if r is not None and exempt(r["type"], x[2], r["priority"]):
            x[3] = "direct"
        elif x[3].get("via") == "fold" or ((first_mark is None or x[0] < first_mark)
                                           and not any(x[3].get(k) for k in _PROVENANCE)):
            x[3] = "fold"
        else:
            x[3] = "direct"
    open_ = lambda r, t: r["created"] <= t and (r["close"] is None or r["close"] > t)  # noqa: E731
    out = {"before": 0, "after": 0, "folded": 0, "appended": 0, "held": 0, "direct": 0}
    by = {}
    gone, waiting, filed_day = set(), [], {}
    day = None

    def place(r, t, trig):
        live = [x for x in recs.values() if x["id"] != r["id"] and x["id"] not in gone and x["status"] not in _ASLEEP
                and open_(x, t)]
        if len(r["q"]) >= 2 and any(x["type"] == r["type"] and recall.near_share(
                r["q"], r["names"], r["scope"], x["q"], x["names"], x["scope"]) is not None for x in live):
            return "folded"
        paths = named_paths(r["text"])
        if paths and any(_in_scope(y, x["scope"]) for x in live for y in paths):
            return "appended"
        return None

    def allow(trig, i, t):
        closed = [recs[tid]["status"] for ts, tid, tr, _ in evs[max(0, i - 400):i]
                  if tr == trig and ts >= _utc(datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(
                      tzinfo=datetime.timezone.utc) - datetime.timedelta(days=TRAIL_DAYS))
                  and tid in recs and recs[tid]["status"] in c.CLOSED and (recs[tid]["close"] or "") <= t]
        budget = row(p, trig).get("budget")
        if budget:  # T-5722: the user's budget is the allowance, as in allowance()
            return float("inf") if budget == UNLIMITED else budget
        return DAILY_MAX if len(closed) < MIN_CLOSED else max(MIN_ALLOW, round(DAILY_MAX * closed.count("done") / len(closed)))

    def settle(kind, trig):
        out[kind] += 1
        by.setdefault(trig, dict.fromkeys(("before", "after"), 0))
        if kind == "after":
            by[trig]["after"] += 1

    def roll(t, i):
        """A new day at t: the held items file, an overdue one first and past the room, the rest best first within each
        trigger's room; one that a task covers by then goes onto it."""
        cutoff = _utc(datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                      - datetime.timedelta(days=MAX_WAIT_DAYS))
        for e in sorted(waiting, key=lambda e: _rank(e, 0, cutoff)):
            kind = place(recs[e["id"]], t, e["source"])
            if kind:
                waiting.remove(e)
                settle(kind, e["source"])
                continue
            if _overdue(e, cutoff) or allow(e["source"], i, t) - filed_day.get(e["source"], 0) > 0:
                waiting.remove(e)
                gone.discard(e["id"])
                filed_day[e["source"]] = filed_day.get(e["source"], 0) + 1
                settle("after", e["source"])

    for i, (t, tid, trig, kind) in enumerate(evs):
        if t < since:
            continue
        today = datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).astimezone().date()
        if today != day:
            day, filed_day = today, {}
            roll(t, i)
        if kind == "direct":  # a check's, a census's or a doctor's own filing: no capture to fold, no allowance to eat
            out["direct"] += 1
            continue
        out["before"] += 1
        by.setdefault(trig, dict.fromkeys(("before", "after"), 0))["before"] += 1
        r = recs.get(tid)
        if r is None:  # the brief is gone: it was filed, and nothing says it would not be
            filed_day[trig] = filed_day.get(trig, 0) + 1
            settle("after", trig)
            continue
        kind = place(r, t, trig)
        if kind:
            gone.add(tid)
            settle(kind, trig)
        elif filed_day.get(trig, 0) >= allow(trig, i, t):
            gone.add(tid)
            waiting.append({"id": tid, "source": trig, "type": r["type"], "tier": r["tier"], "held_at": t})
        else:
            filed_day[trig] = filed_day.get(trig, 0) + 1
            settle("after", trig)
    if waiting:  # the drain at now: a new day since the last capture, or an item gone overdue, files what it can
        end = _utc(datetime.datetime.now(datetime.timezone.utc))
        if day != datetime.datetime.now().astimezone().date():
            day, filed_day = datetime.datetime.now().astimezone().date(), {}
        roll(end, len(evs))
    out["held"] = len(waiting)
    for e in waiting:
        by.setdefault(e["source"], dict.fromkeys(("before", "after"), 0))
    out["by_trigger"] = by
    out["days"] = days
    return out


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
    fired, folds = {}, dict.fromkeys(("folded", "appended", "held"), 0)
    for e in c.ledger_events(p, ["capture", "capture_folded"], since=_utc(day)):  # T-4743: one pass for both
        t = (e.get("data") or {}).get("trigger")
        if e.get("event") == "capture_folded":
            k = (e.get("data") or {}).get("kind")
            if k in folds:
                folds[k] += 1
        elif isinstance(t, str) and t:
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
    if any(folds.values()):  # the fold kept these out of the inbox (T-4743)
        parts.append(", ".join(f"{n} {k}" for k, n in folds.items() if n) + " today")
    try:  # everything held, not just today's: a standing list shows on a quiet day
        waiting = held(p)
        if waiting:
            over = sum(_overdue(e, _cutoff(_local_now())) for e in waiting)
            parts.append(f"{len(waiting)} held in all" + (f", {over} overdue" if over else ""))
    except c.CorruptState as e:
        parts.append(f"{HELD_FILE} can't be read ({e})")
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
    return {"fired": fired, "folds": folds, "installs": installs, "recovering": recovering, "trials_due": due, "predictions": pk,
            "line": ("Self-running: " + " · ".join(parts)) if parts else ""}


# ---- the follow-ups captured on a task: each one is settled (done, taken as a task, or dropped with a reason) before it closes
def follow_items(b):
    """The follow-up lines the fold appended to a task, as dicts {mark, body, key, src, rel}: mark is ' ' while open."""
    out = []
    for line in b.section(FOLLOW).splitlines():
        m = _LINE.match(line.strip())
        if m:
            out.append({"mark": m["mark"], "body": m["body"], "key": m["key"], "src": m["src"], "rel": m["rel"]})
    return out


def _is_open(item):
    return not item["mark"].strip()


def follow_gaps(p, b):
    """What stops a close: one message when any follow-up appended to the task is unsettled (no one reads a follow-up
    that no one has to settle). [] for a task with none."""
    if b.status in c.CLOSED:
        return []
    todo = [x for x in follow_items(b) if _is_open(x)]
    if not todo:
        return []
    named = "; ".join(f"f-{x['key']} {c.fit(c.plain(x['body']), 60)}" for x in todo[:3])  # a train's refusal line is cut: lead with them
    return [f"{len(todo)} follow-up(s) captured on {b.id} are unsettled ({named}" + (" …" if len(todo) > 3 else "")
            + f"): sampo task follow {b.id} KEY --done | --capture | --drop --why \"…\" settles each, --drain files them all, the small ones in one batch (no key lists them)"]


def safe_follow_gaps(p, b):
    """follow_gaps for the close gates; a reader that breaks must not stop a close: it fails open and logs where hooks'
    errors go."""
    try:
        return follow_gaps(p, b)
    except Exception:
        import traceback
        try:
            path = os.path.join(c.state_dir(), "logs", "hooks.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            c.append_line(path, f"{c.now()} fmtriggers.follow_gaps {c.redact(traceback.format_exc()).rstrip()}\n")
        except OSError:
            pass
        return []


def _set_mark(b, marks):
    """Rewrite the follow-up lines of these keys ({key: mark}) with a new mark (the line otherwise as it was)."""
    lines = []
    for line in b.section(FOLLOW).splitlines():
        m = _LINE.match(line.strip())
        if m and m["key"] in marks:
            line = f"- [{marks[m['key']]}]" + line.strip()[line.strip().index("]") + 1:]
        lines.append(line)
    b.set_section(FOLLOW, "\n".join(lines).strip("\n") + "\n")


def _whole(p, item):
    """The whole text of a follow-up: its file when the line was too short for it, else the line's text."""
    if item["rel"]:
        try:
            with open(os.path.join(p.dir, item["rel"]), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    return item["body"]


def _take(p, host, item):
    """File a follow-up as a task of its own (no fold, no allowance: it was filed once already); its id. The caller holds
    the lock."""
    text = _whole(p, item)
    head, _, rest = text.partition(": ")
    type_ = head if head in c.TYPES and rest else "FEATURE"
    text = rest if type_ == head and rest else text
    src = _AS.get(item["src"], item["src"])
    return file_item(p, {"text": text, "raw": f"{text}\n(a follow-up captured on {host.id})", "type": type_,
                         "source": src if src in FOLDED else "followup", "from": host.id, "direct": True,
                         "tier": c.guess_tier(type_, text)})


def refile_follow(p, host):
    """A host that is dropped or deferred will never read its follow-ups: file each open one as a captured task and mark
    it. The ids filed. Changes host in place; the caller holds the lock and saves it."""
    ids = []
    for item in follow_items(host):
        if _is_open(item):
            tid = _take(p, host, item)
            _set_mark(host, {item["key"]: f"captured {tid}"})
            ids.append(tid)
    return ids


def task_follow(p, args):
    """sampo task follow ID [KEY…] [--done | --capture | --drop --why "…" | --drain]: list the follow-ups captured on a task,
    or settle them (each named by its key: read first, never every open one at once; --drain files every open one, the
    small ones in one batch host for a haiku builder, T-5717)."""
    from sampo import cli
    flags = [n for n in ("done", "capture", "drop", "drain") if getattr(args, n, False)]
    if len(flags) > 1:
        raise cli.UsageError("one of --done, --capture, --drop, --drain at a time")
    why = " ".join(str(getattr(args, "why", None) or "").split())
    if flags == ["drop"] and not why:
        raise cli.UsageError("--drop needs --why \"…\": a dropped follow-up says why it isn't needed")
    keys = [str(k).removeprefix("f-") for k in args.keys or ()]
    with c.lock(p.dir):
        b = cli.need_brief(p, args.id)
        items = follow_items(b)
        if not flags:
            lines = [f"[{x['mark'] or ' '}] {x['body']} (f-{x['key']} {x['src']})" + (f"  whole: {x['rel']}" if x["rel"] else "")
                     for x in items if not keys or x["key"] in keys]
            return cli.out(args, {"id": b.id, "follow_ups": items},
                           (f"{b.id} follow-ups:\n" + "\n".join(lines)) if lines else f"{b.id} has no follow-ups captured.")
        unknown = [k for k in keys if k not in {x["key"] for x in items}]
        if unknown:
            raise cli.UsageError(f"no follow-up f-{unknown[0]} on {b.id}: sampo task follow {b.id} lists them")
        if flags == ["drain"]:  # T-5717: every open one (or those named), the small ones in one batch host
            got = drain_follow_ups(p, b, keys)
            n = len(got["filed"])
            return cli.out(args, dict(got, id=b.id),
                           f"{b.id}: {n} follow-up(s) filed" + (f", {len(got['members'])} of them batched in {got['batch']}: "
                                                                 f"{got['line']}" if got["batch"] else "")
                           + f"; {len([x for x in follow_items(c.find_brief(p, b.id)) if _is_open(x)])} open.")
        todo = [x for x in items if _is_open(x) and (not keys or x["key"] in keys)]
        if not keys:  # never every open one at once: each is read, then named
            raise cli.UsageError(f"name the follow-up(s) (f-xxxxxxxx): sampo task follow {b.id} lists them")
        marks = {}
        for x in todo:
            if flags == ["done"]:
                marks[x["key"]] = "x"
            elif flags == ["capture"]:
                marks[x["key"]] = f"captured {_take(p, b, x)}"
            else:
                marks[x["key"]] = ("dropped: " + why).replace("]", ")")
        if marks:
            _set_mark(b, marks)
            b.append_log(f"follow-ups settled: {', '.join(f'f-{k} {v.split(chr(58))[0]}' for k, v in marks.items())}")
            c.save_brief(p, b)
            c.regen_views(p)
    return cli.out(args, {"id": b.id, "settled": marks},
                   f"{b.id}: {len(marks)} follow-up(s) settled" + (f" ({', '.join(marks.values())})" if marks else "")
                   + f"; {len([x for x in follow_items(b) if _is_open(x)])} open.")


DRAIN_MAX = 6  # T-5717: the most follow-ups one drain host takes: one haiku builder holds six small ones
DRAIN_AC = "the tests linked to the change pass :: sampo check --affected"  # a builder brief refuses a host with no verify


def _small_follow(p, item, tid):
    """T-5717: whether a filed follow-up is small enough for the drain host: the process tier (trivial or small) from the
    paths its text names. No named path, no score: it stays alone. A text that names only docs and tests is scored as a
    one-line change to them (it is no code); one that names code is scored on its claimed tier, with no linked tests
    known, so only a claimed S passes."""
    files = named_paths(_whole(p, item))
    b = c.find_brief(p, tid)
    if not files or not b:
        return False
    prose = all(c.DOC.search(f) or c._TEST_DIR.search(f) for f in files)
    diff = "".join(f"diff --git a/{f} b/{f}\n@@ -1 +1 @@\n+x\n" for f in files) if prose else ""
    return c.process_tier(b.tier, b.type, files, diff, {})[0] in ("trivial", "small")


def _undo_batch(p, h, members):
    """A batch host that was made but not finished: its members go back unbatched and it is dropped, each step on its
    own so one failure leaves the rest done. Best effort: the drain's caller must never see an error from this."""
    for tid in members:
        try:
            m = c.find_brief(p, tid)
            if m and h is not None and m.meta.get("batched_in") == h.id:
                m.meta.pop("batched_in", None)
                m.append_log(f"unbatched: the drain host {h.id} could not be made")
                c.save_brief(p, m)
        except Exception:
            pass
    if h is not None:
        try:
            hh = c.find_brief(p, h.id)
            if hh:
                hh.meta["status"] = "dropped"
                hh.meta.pop("batch", None)
                hh.append_log("dropped: the drain could not finish making this host")
                c.save_brief(p, hh)
        except Exception:
            pass


def drain_follow_ups(p, host, keys=None, via=""):
    """T-5717: files every open follow-up of host (those named by keys, when given) as a task of its own (_take), marked
    captured and logged, saved as it goes so a failure part way leaves the rest open; then makes ONE batch host of the
    small ones (_small_follow, at most DRAIN_MAX, two or more) with the batch maker: tier S, harness plan drain, one
    criterion with a verify command, for one haiku builder (sampo lane brief HOST --model haiku, a line printed here,
    no agent started). The caller holds c.lock(p.dir) (it isn't reentrant: the batch is made with it held). A batch the
    maker refuses leaves its members filed alone. {"filed": [(key, id)], "batch": host id or None, "members": [ids],
    "line": the launch line or ""}."""
    from sampo import cli
    want = {str(k).removeprefix("f-") for k in keys or ()}
    filed, small = [], []
    for x in [x for x in follow_items(host) if _is_open(x) and (not want or x["key"] in want)]:
        tid = _take(p, host, x)
        _set_mark(host, {x["key"]: f"captured {tid}"})
        host.append_log(f"follow-ups settled{via}: f-{x['key']} captured {tid}")
        c.save_brief(p, host)
        filed.append((x["key"], tid))
        if len(small) < DRAIN_MAX and _small_follow(p, x, tid):
            small.append(tid)
    batch, line = None, ""
    if len(small) >= 2:
        h = None
        try:
            names = "; ".join(c.fit(c.find_brief(p, t).title, 30) for t in small)
            h, _ = cli._make_batch(p, small, c.fit(f"Drain {host.id}'s follow-ups: {names}", 120), held=True)
            h.meta["tier"] = "S"  # the maker rates a batch of three or more M; these are small by their score
            h.meta["harness_plan"] = "drain"  # own plan: plan_gaps skips the readings and options
            # the maker wrote each member's own criterion and a step each: they stay (a member closes done with the host,
            # so a host that holds only the shared check would close a member nobody did); the shared check joins them
            h.set_section("Interpretation", h.section("Interpretation").rstrip("\n") + " The host's criteria are every "
                          "member's own plus the drain's check: a member is closed done only when the host is, so a "
                          "member criterion not met keeps the host open (T-5717).")
            h.add_ac(*c.split_verify(DRAIN_AC))
            c.save_brief(p, h)
            batch, line = h.id, f"sampo lane brief {h.id} --model haiku"
            host.append_log(f"follow-ups drained{via}: batch {h.id} of {', '.join(small)}")
            c.save_brief(p, host)
        except Exception as e:  # the settle never raises from here: the members stay filed alone, the why on the Log
            batch, line = None, ""
            _undo_batch(p, h, small)
            try:
                host.append_log(f"follow-ups drained{via}: no batch ({c.fit(str(e) or type(e).__name__, 100)})")
                c.save_brief(p, host)
            except Exception:
                pass
    if filed:
        c.regen_views(p)
    return {"filed": filed, "batch": batch, "members": small if batch else [], "line": line}


def settle_follow_ups(p, b):
    """T-5535: `sampo task follow ID KEY --capture` for every open follow-up of a merged, reviewed lane's task: each is
    filed as a task of its own (_take), marked captured and named in a Log line of its own, saved as it goes, so a
    failure part way leaves the rest open for the next land. T-5717: the small ones are drained into one batch host
    (drain_follow_ups), its launch line printed on stderr. The caller holds c.lock(p.dir). [(key, the new task's id)]."""
    got = drain_follow_ups(p, b, via=" (land)")
    if got["line"]:
        print(f"{b.id}: small follow-ups batched in {got['batch']}: {got['line']}", file=sys.stderr)
    return got["filed"]
