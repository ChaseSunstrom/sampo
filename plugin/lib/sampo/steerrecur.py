"""T-4191: three avoidable steers of one class in 7 days file a meta-task. From the steer meter's rows (steermeter.rows,
avoidable ones only) one root (R1..R8), or one concern (concerns' fold: a repeat_of chain is one concern), recurring
MIN_ROWS times in WINDOW_D days captures one FIX: why did the mechanism that should have caught this not act. The
mechanism is data (plugin/data/steer-mechanisms.json: root to a one-line name and the ledger event kinds it writes), its
last FIRINGS are read from the ledger (core.ledger_events), and the fix is to that mechanism, never a new one beside it.
No model is called: models advise, and this is evidence from rows and the ledger. One task per group per ISO week
(eco.capture_once with a key); while one is open for a group, the next week files no second. The steer meter's tick
runs it beside the concerns pass (steermeter.cmd_steers); a failure is logged and never stops the meter."""
import json
import os
import re

from sampo import core as c

MIN_ROWS = 3        # avoidable steers of one root or concern that make a recurrence
WINDOW_D = 7        # ... within this many days
FIRINGS = 3         # the mechanism's last firings shown
LOOKBACK_D = 30     # a firing older than this is not "last": the mechanism is said not to have acted
SAID = 3            # the user's missed thoughts shown
_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "data", "steer-mechanisms.json")
KEY = "CONTEXT: steer-recur"


def mechanisms():
    """{"roots": {id: {"mechanism", "kinds", "match"}}, "concern": {...} or None} from the data file; empty when it can't
    be read (no group then names a mechanism, so none is filed)."""
    def one(d):
        if isinstance(d, dict) and isinstance(d.get("mechanism"), str) and isinstance(d.get("short"), str) \
                and isinstance(d.get("kinds"), list) and d["kinds"] and all(isinstance(k, str) for k in d["kinds"]):
            m = d.get("match")
            return {"short": d["short"], "mechanism": d["mechanism"], "kinds": tuple(d["kinds"]),
                    "match": {k: v for k, v in m.items() if isinstance(k, str)} if isinstance(m, dict) else {}}
        return None
    try:
        with open(os.path.normpath(_DATA), encoding="utf-8") as f:
            d = json.load(f)
        roots = {k: v for k, v in ((k, one(x)) for k, x in d["roots"].items()) if v}
        return {"roots": roots, "concern": one(d.get("concern"))}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return {"roots": {}, "concern": None}


def week(t):
    """The ISO week of an aware datetime as 2026-W47."""
    y, w, _ = t.isocalendar()
    return f"{y}-W{w:02d}"


def groups(p, now=None):
    """The classes that recur: [{"group" (a root id or a K- concern id), "kind" ("root" or "concern"), "rows" (oldest
    first)}] for each root or concern with MIN_ROWS or more avoidable steers read in the last WINDOW_D days. A root the
    table doesn't know ('new', or one added to the roots file but not the table) is not grouped."""
    from sampo import concerns as cn
    from sampo import steermeter as sm
    t = now if now is not None else c.clock().timestamp()
    since = c.iso(t - WINDOW_D * 86400)
    known = mechanisms()["roots"]
    out = []

    def recent(rs):
        return [r for r in rs if r["avoidable"] and r["at"] >= since]
    by = {}
    for r in recent(sm._read_rows(sm.rows(p))):
        if r.get("root") in known:
            by.setdefault(r["root"], []).append(r)
    out += [{"group": k, "kind": "root", "rows": v} for k, v in sorted(by.items()) if len(v) >= MIN_ROWS]
    rs = cn._rows(p)  # steers and new-requests that carry a concern, oldest first; a chain of repeat_of is one concern
    root = cn._components(rs, cn._parents(rs), {e["concern"] for e in cn._events(p) if e["event"] == "split"})
    cons = {}
    for i, r in enumerate(rs):
        if r["avoidable"] and r["at"] >= since:
            f = rs[root[i]]
            cons.setdefault(cn.concern_id(f["session"], f["uuid"]), []).append(r)
    out += [{"group": k, "kind": "concern", "rows": v} for k, v in sorted(cons.items()) if len(v) >= MIN_ROWS]
    return out


def firings(p, spec, now=None):
    """The mechanism's last FIRINGS ledger events (oldest first) of the kinds its spec names, within LOOKBACK_D days."""
    t = now if now is not None else c.clock().timestamp()
    got = []
    for e in c.ledger_events(p, kinds=spec["kinds"], since=c.iso(t - LOOKBACK_D * 86400)):
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        if all(d.get(k) == v for k, v in spec["match"].items()):
            got.append(e)
    return got[-FIRINGS:]


def _firing(e):
    """One line of a ledger event: its time, kind, task and data, cut and with no secret or markup in it."""
    data = json.dumps(e.get("data"), ensure_ascii=False, sort_keys=True) if e.get("data") else ""
    task = f" {e['task']}" if isinstance(e.get("task"), str) and re.fullmatch(r"T-\d+", e["task"]) else ""
    return c.fit(c.defang(c.plain(c.redact(f"{e.get('ts')} {e.get('event')}{task} {data}".strip()))), 200)


def _text(p, g, spec, name, wk, now):
    rows = g["rows"]
    said = [c.fit(c.defang(c.plain(c.redact(r.get("missed_thought") or ""))), 140) for r in rows if r.get("missed_thought")]
    last = firings(p, spec, now)
    what = f"the {name!r} root" if g["kind"] == "root" else f"the concern {g['group']}"
    head = what if g["kind"] == "root" else "a concern said again and again"  # a K- id on the first line links the task to the concern
    lines = [f"Why did the {spec['short']} not catch {head}? ({len(rows)} steers in {WINDOW_D} days)",
             f"{KEY} {g['group']} {wk}: {len(rows)} avoidable steers of {what} between {rows[0]['at']} and {rows[-1]['at']}; "
             f"the mechanism that should have caught it is {spec['mechanism']}.",
             "CONTEXT: " + (f"its last {len(last)} firings in the ledger (kinds {', '.join(spec['kinds'])}): "
                            + "; ".join(_firing(e) for e in last) if last else
                            f"no firing in the last {LOOKBACK_D} days in the ledger (kinds {', '.join(spec['kinds'])}): it did not act "
                            "at all, or does not write one"),
             "CONTEXT: the thoughts the agent skipped, as the meter read them: " + (" | ".join(said[-SAID:]) or "(none given)"),
             "CONTEXT: the fix is to that mechanism (why it did not act, or acted on the wrong thing), never a new one beside it."]
    return "\n".join(lines)


def _filed_at(p, group):
    """The created time of the newest brief (closed and archived too) that carries this group's recurrence line, else ''."""
    line = re.compile(r"(?m)^(?:> ?)?" + re.escape(f"{KEY} {group} "))
    return max((str(b.meta.get("created") or "") for b in c.load_briefs(p, include_archive=True)
                if line.search(b.section("Raw request"))), default="")


def _open_for(p, group):
    """The open brief that already carries this group's recurrence line (any week), else None."""
    line = re.compile(r"(?m)^(?:> ?)?" + re.escape(f"{KEY} {group} "))
    return next((b for b in c.load_briefs(p) if b.status not in c.CLOSED and line.search(b.section("Raw request"))), None)


def tick(p):
    """File one FIX per recurring group not yet filed this ISO week and not open: [{"group", "kind", "task", "new"}] for
    every recurring group (new False: this week's, or an open one of an earlier week, already stood for it)."""
    from sampo import eco
    now = c.clock()
    specs = mechanisms()
    out = []
    for g in groups(p, now.timestamp()):
        spec = specs["concern"] if g["kind"] == "concern" else specs["roots"].get(g["group"])
        if not spec:
            continue
        wk = week(now)
        held = _open_for(p, g["group"])
        if held:
            out.append({"group": g["group"], "kind": g["kind"], "task": held.id, "new": False})
            continue
        done = _filed_at(p, g["group"])  # a recurrence is MIN_ROWS new steers after the last filing, not the same ones again
        g = dict(g, rows=[r for r in g["rows"] if r["at"] > done])
        if len(g["rows"]) < MIN_ROWS:
            continue
        name = _root_name(g["group"]) if g["kind"] == "root" else ""
        key = f"{KEY} {g['group']} {wk}"
        b, new = eco.capture_once(p, _text(p, g, spec, name, wk, now.timestamp()), "FIX", "steer-recur", key=key)
        if key not in b.section("Raw request"):  # a mute or budget joined an unrelated capture: nothing was filed
            continue
        out.append({"group": g["group"], "kind": g["kind"], "task": b.id, "new": new})
    return out


def _root_name(rid):
    from sampo import steermeter as sm
    return sm.roots().get(rid, {}).get("name", rid)


def safe_tick(p):
    """tick for the steer meter's pass: a failure (a torn line, a full disk, a refused capture) is logged where the hooks'
    errors go and never stops the meter."""
    try:
        return tick(p)
    except Exception:
        import traceback
        try:
            path = os.path.join(c.state_dir(), "logs", "hooks.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            c.append_line(path, f"{c.now()} steerrecur.tick {c.redact(traceback.format_exc()).rstrip()}\n")
        except OSError:
            pass
        return []
