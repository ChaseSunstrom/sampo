"""T-5710: the dead-feature check. A feature is dead when it declared a real-use signal (its Outcome line, or the calling
cards' own calls) and shows no real use in the last DEAD_DAYS days, having landed LANDED_DAYS days ago or more. The
worst (oldest, then costliest per turn) is filed as 'Wire or retire: X': a capture to wire it or retire it, never a
close, and at most one a day. Real use is what outcomes.py counts (a builder's run left out); for the calling cards it
is a call whose via is a real origin (T-5613's row key; a bench or test call, and a row with no via, never counts), so a
bench or test run never keeps a card alive. Filings are read from the archive too, so a filed brief tidied away is not
filed again."""
import json
import os

from sampo import core as c
from sampo import outcomes

DEAD_DAYS, LANDED_DAYS = 14, 7  # the window a real use must fall in; a feature this young is not yet judged
FILED = "Wire or retire: "  # the capture's title: a brief that starts with it is a filing, open or closed
CARD_ID = "seed:call-cards"  # outcomes.SEEDS' calling-card feature: judged on its real calls, not the generic verb probe
DAY = 86400


def _days(ts, now):
    """Days from an ISO stamp to now (the caller's clock), None when the stamp isn't one."""
    t = c.parse_ts(ts)
    return None if t is None else (now - t.timestamp()) / DAY


def _real_vias():
    """The via values that count as real use of a card: callcards' ORIGINS less its NOT_REAL (T-5613's, a bench arm's or a
    test's call says nothing of the card in use). Until T-5613 lands in this lane, callcards has neither, so the same two
    tuples are kept here, in step with T-5613 by hand: when it lands, its callcards.ORIGINS wins and this copy is dead."""
    from sampo import callcards
    origins = getattr(callcards, "ORIGINS", ("cli", "smith", "harness", "bench", "test", "mcp"))
    not_real = getattr(callcards, "NOT_REAL", ("bench", "test"))
    return tuple(o for o in origins if o not in not_real)


def real_card_calls(since):
    """Calling-card calls from a real session since an ISO time: calls.jsonl rows (and the file it rotated to) whose via
    is a real origin (_real_vias). A row with no via is unknown and never counts (the calls write none until T-5613).
    A torn line is skipped, as call_stats skips it."""
    from sampo import callcards
    real = _real_vias()
    n = 0
    for path in (callcards.calls_path() + ".1", callcards.calls_path()):
        try:
            with open(path, errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):  # a torn line, or one nested past the parser's depth
                continue
            if (isinstance(row, dict) and row.get("via") in real
                    and str(row.get("at") or "")[:19] >= since[:19]):
                n += 1
    return n


def _declared(p, briefs):
    """(task, title, signal, landed) for each done brief that declared an Outcome within outcomes' window, plus the seeds
    the outcome probes add in Foreman's own repo (outcomes._evaluate's own filter, so both checks agree on the set)."""
    out = []
    for b in briefs:
        if b.status != "done" or (c.age_days(b.meta.get("updated")) or 0) > outcomes.WINDOW_DAYS + 5:
            continue
        sig = (b.section("Outcome").strip().splitlines() or [""])[0]
        landed = outcomes.done_at(b)
        if sig and (age := c.age_days(landed)) is not None and age <= outcomes.WINDOW_DAYS:
            out.append((b.id, b.title, sig, landed))
    if os.path.isfile(os.path.join(p.root, "plugin", "lib", "sampo", "outcomes.py")):  # Foreman's own repo
        out += list(outcomes.SEEDS)
    return out


def _events(p, since, names):
    """The events a generic probe reads: the project's and the hooks' since an ISO time, plus the ledger's named events."""
    ev = [e for e in c.global_events(since) if e.get("project") == p.slug or e.get("kind") == "inject"]
    if names:
        ev += list(c.ledger_events(p, tuple(sorted(names)), since=since))
    return ev


def turn_cost(m, ev):
    """The injected characters a hook event carries per turn (the mean of its inject rows, hooks.py's own count), or None:
    a signal that names no hook carries no per-turn number, and such a feature ranks by age alone."""
    if m.group(1) != "event":
        return None
    sizes = [e["chars"] for e in ev if e.get("kind") == "inject" and e.get("event") == m.group(2)
             and isinstance(e.get("chars"), (int, float))]
    return round(sum(sizes) / len(sizes), 1) if sizes else None


def rank(rows):
    """Worst first: score = age in days × (1 + injected characters per turn / 1000), so a feature with no per-turn number
    ranks by its age alone; ties go to the lower task id. Each row gets its score."""
    for r in rows:
        r["score"] = round(r["age_days"] * (1 + (r.get("cost") or 0) / 1000), 2)
    return sorted(rows, key=lambda r: (-r["score"], r["task"]))


def dead_rows(p, briefs, now, extra=()):
    """The features with no real use in the window, worst first: [{task, title, signal, age_days, cost, score}]. A
    feature is judged when it landed LANDED_DAYS days or more ago (and within outcomes' window), its window is the last
    DEAD_DAYS days or its landing when that is later, and no 'Wire or retire' brief names it yet. extra: more
    (task, title, signal, landed) rows, the way a caller adds a feature the briefs don't hold."""
    filed = [str(b.title) for b in c.load_briefs(p, include_archive=True) if str(b.title).startswith(FILED)]
    cand = []
    for tid, title, sig, landed in list(_declared(p, briefs)) + list(extra):
        age, m = _days(landed, now), outcomes.SIGNAL.match(sig)
        if age is None or age < LANDED_DAYS or age > outcomes.WINDOW_DAYS or not m:
            continue  # young, old, or a signal this can't read (a manual one is never called dead)
        if any(f.startswith(FILED + tid + " ") for f in filed):  # a whole id: T-57 is not T-5710
            continue
        cand.append((tid, title, sig, landed, age, m))
    if not cand:
        return []
    since = c.iso(now - DEAD_DAYS * DAY)
    ev = _events(p, since, {m.group(2) for *_, m in cand if m.group(1) == "event"})
    rows = []
    for tid, title, sig, landed, age, m in cand:
        win = c.iso(max(now - DEAD_DAYS * DAY, c.parse_ts(landed).timestamp()))
        if tid == CARD_ID:
            status = "fired" if real_card_calls(win) else "silent"
        else:
            status = outcomes._fired(p, m.group(1), m.group(2), win, ev, p.slug)
        if status != "silent":
            continue  # used, or a signal this can't read
        rows.append({"task": tid, "title": str(title)[:100], "signal": sig, "age_days": round(age, 1),
                     "cost": turn_cost(m, ev)})
    return rank(rows)


def hit(p, briefs, now, extra=()):
    """selfcheck's dead_feature: the worst dead feature as one Hit whose action files its 'Wire or retire' capture. A
    filing made in the last day holds the check back, so a day files at most one (the brief the capture makes is the
    record, and a rerun finds it)."""
    from sampo.selfcheck import Hit
    for b in c.load_briefs(p, include_archive=True):
        age = _days(b.meta.get("created"), now) if str(b.title).startswith(FILED) else None
        if age is not None and age < 1:  # a brief with no creation stamp holds nothing back
            return None
    rows = dead_rows(p, briefs, now, extra)
    if not rows:
        return None
    r = rows[0]
    window = int(min(DEAD_DAYS, r["age_days"]))  # the days actually judged: the window, or the feature's life when younger
    per_turn = f", {r['cost']:g} chars injected per turn" if r["cost"] else ""
    text = (f"{r['task']} ({c.fit(r['title'], 60)}) landed {r['age_days']:g} days ago, no real use in {window} days "
            f"(signal {r['signal']}){per_turn} → wire or retire it; {len(rows)} silent in all")
    title = (f"{FILED}{r['task']} {c.fit(r['title'], 80)}: no real use in {window} days, landed "
             f"{r['age_days']:g} days ago")
    return Hit(text, action=[os.path.join(c.PLUGIN_ROOT, "bin", "sampo"), "capture", "--source", "discovered", title])
