"""T-4223 (big tiers part D, T-4219): what a big task is doing, in numbers. sampo big status ID (or --all) shows its tree:
n of m children done, the wave now, minutes, close attempts and cost against an estimate. big estimate forecasts the
children's cost from finished tasks of the same type and tier; big report names each child and its state. The status
line and sampo status carry one segment, "T-… XL 3/7 · wave 2". Closing a big task appends one row to big.jsonl, once
per tree state. Nothing reads big.jsonl yet: the estimate reads the briefs and the cost cache, and the rows are kept for
a later estimate and for a reviewer. Reads the briefs, the cost cache and the ledger's bounded tail; no model call."""
import json
import os

from sampo import core as c

LEDGER_TAIL = 5000  # the close's minutes and attempts are read from the newest of these ledger lines (run_stats' bound)


def big_tiers():
    return tuple(c.BIG_TIERS)  # one constant (part A's): every big-tier check reads it


def jsonl_path(p):
    return os.path.join(p.dir, "big.jsonl")


def _fin(status):
    return status in c.CLOSED  # done or dropped: one rule for the wave, the counts and the hook (T-4223)


def _finished(k):
    return _fin(k.status)


def _num(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):  # a hand-edited value is no number: it counts as nothing
        return 0


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):  # no value recorded: unknown, which is not 0
        return None


def _kids(briefs, tid):
    return sorted((b for b in briefs if b.meta.get("parent") == tid), key=lambda b: c.id_num(b.id))


def _layers(kids):
    """Each child's wave: one more than the deepest sibling it waits on; a cycle is cut where it closes."""
    by = {k.id: k for k in kids}
    memo = {}

    def layer(i, stack):
        if i in memo:
            return memo[i]
        if i in stack:
            return 1
        memo[i] = v = 1 + max((layer(d, stack + (i,)) for d in c._deps(by[i]) if d in by), default=0)
        return v
    return {i: layer(i, ()) for i in by}


def _wave(kids):
    """The lowest wave that still holds a child not finished: the wave the tree is in now (None when all are)."""
    lay = _layers(kids)
    return min((lay[k.id] for k in kids if not _finished(k)), default=None)


def _ctx(p, briefs):
    """What the trees read, once per command: the cost cache as it is (never a transcript walk) with when it was made,
    and each child's minutes and attempts from its latest task_done in the ledger's bounded tail."""
    from sampo import cost
    costs, made_at = cost.cache_as_is(p)
    ids = {b.id for b in briefs if b.meta.get("parent")}
    stats = {}
    if ids:
        for e in c.ledger_tail(p, LEDGER_TAIL, kinds=["task_done"]):  # oldest first: the last close wins
            if e.get("task") in ids:
                d = e.get("data") if isinstance(e.get("data"), dict) else {}
                stats[e["task"]] = (_int(d.get("minutes")), _int(d.get("attempts")))
    return {"costs": costs, "made_at": made_at, "stats": stats}


def _cost_known(ctx, b):
    """A task's spend is known when the cache was made after the task was created: a task newer than the cache has
    no figure yet, which is not a spend of 0."""
    t = c.parse_ts(b.meta.get("created"))
    return ctx["made_at"] is not None and t is not None and t.timestamp() <= ctx["made_at"]


def _estimate(p, kids, briefs, ctx):
    """The children's forecast cost, summed over those with history; None when none has any."""
    from sampo import cost
    got = [f for k in kids if (f := cost.forecast_task(p, k, ctx["costs"], briefs=briefs))]
    return {"cost": sum(f[0] for f in got), "covered": len(got), "of": len(kids)} if got else None


def _tree(p, b, briefs, ctx):
    kids = _kids(briefs, b.id)
    lay = _layers(kids)
    stats = ctx["stats"]
    got = [stats.get(k.id, (None, None)) for k in kids]
    timed = [m for m, _ in got if m is not None]
    tried = [a for _, a in got if a is not None]
    known = [x for x in [b] + kids if _cost_known(ctx, x)]
    return {
        "id": b.id, "title": b.title, "tier": b.tier, "status": b.status,
        "done": sum(_finished(k) for k in kids), "total": len(kids),
        "wave": _wave(kids), "waves": max(lay.values(), default=0),
        "time_min": sum(timed) if timed else None, "timed": len(timed),
        "attempts": sum(tried) if tried else None,
        "cost": sum(_num(ctx["costs"].get(x.id)) for x in known) if known else None,
        "cost_unknown": len(kids) + 1 - len(known), "cost_cache": ctx["made_at"] is not None,
        "estimate": _estimate(p, kids, briefs, ctx),
        "children": [{"id": k.id, "title": k.title, "type": k.type, "tier": k.tier, "status": k.status,
                      "wave": lay[k.id], "minutes": m, "attempts": a}
                     for k, (m, a) in zip(kids, got)],
    }


def _human(n):
    from sampo import cost
    return cost._human(n)


def _line(t):
    if not t["total"]:
        state = "not expanded: sampo big expand " + t["id"]
    else:
        state = f"{t['done']} of {t['total']} done · " + (f"wave {t['wave']}" if t["wave"] else "all in")
    if t["time_min"] is None:
        timed = "time unknown"
    else:
        timed = f"{t['time_min']} min" + ("" if t["timed"] == t["total"] else f" ({t['timed']} of {t['total']} timed)")
    tried = "attempts unknown" if t["attempts"] is None else f"{t['attempts']} attempts"
    est = t["estimate"]
    if t["cost"] is None:
        spend = ("cost unknown (no cost cache yet: run sampo cost once)" if not t["cost_cache"]
                 else "cost unknown (newer than the cost cache: run sampo cost once)")
    else:
        spend = f"cost {_human(t['cost'])}" + (f" + {t['cost_unknown']} unknown" if t["cost_unknown"] else "")
        spend += (f" of ~{_human(est['cost'])} ({est['covered']}/{est['of']} forecast)" if est else " (no estimate yet)")
    return f"{t['id']} {t['tier']} {c.fit(t['title'], 48)}: {state} · {timed} · {tried} · {spend}"


def _tops(briefs):
    return [b for b in briefs if b.tier in big_tiers()]


def _need(p, args):
    from sampo import cli
    b = cli.need_brief(p, args.id)
    if b.tier not in big_tiers():
        raise cli.UsageError(f"{b.id} is tier {b.tier}: big status, estimate and report are for "
                             f"{', '.join(big_tiers())} tasks")
    return b


def task_status(p, args):
    from sampo import cli
    briefs = c.load_briefs(p)
    ctx = _ctx(p, briefs)
    if getattr(args, "all", False):
        rows = [_tree(p, b, briefs, ctx) for b in _tops(briefs)]
        text = "\n".join(_line(t) for t in rows) or "No big task yet: sampo task new … --tier XL, then sampo big plan ID"
        return cli.out(args, {"tasks": rows}, text)
    if not args.id:
        raise cli.UsageError("sampo big status ID, or sampo big status --all")
    b = _need(p, args)
    t = _tree(p, b, briefs, ctx)
    lines = [_line(t)] + [f"  {k['id']} {k['status']:<9} {k['tier']} {c.fit(k['title'], 50)}"
                          + (f" (wave {k['wave']})" if not _fin(k["status"]) and k["wave"] else "")
                          for k in t["children"]]
    return cli.out(args, t, "\n".join(lines))


def task_estimate(p, args):
    from sampo import cli
    b = _need(p, args)
    briefs = c.load_briefs(p)
    kids = _kids(briefs, b.id)
    ctx = _ctx(p, briefs)
    est = _estimate(p, kids, briefs, ctx)
    if not kids:
        text = f"{b.id}: not expanded yet, so there are no children to estimate (sampo big expand {b.id})"
    elif est is None:
        text = (f"{b.id}: no history for its {len(kids)} children yet: an estimate needs two finished tasks of the "
                "same type and tier, so none is made")
    else:
        spent = _tree(p, b, briefs, ctx)["cost"]
        text = (f"{b.id}: about {_human(est['cost'])} for its children, from {est['covered']} of {est['of']} that have "
                f"history; {'spend unknown' if spent is None else _human(spent) + ' spent so far'}")
    return cli.out(args, {"id": b.id, "estimate": est, "children": len(kids)}, text)


def task_report(p, args):
    from sampo import cli
    b = _need(p, args)
    briefs = c.load_briefs(p)
    t = _tree(p, b, briefs, _ctx(p, briefs))
    lines = [f"{b.id} {b.tier} {b.title}: {t['done']} of {t['total']} children done ({b.status})"]
    lines += [f"  {k['id']} {k['status']:<9} wave {k['wave']} {k['tier']} {c.fit(k['title'], 60)}" for k in t["children"]]
    nxt = next((k for k in t["children"] if not _fin(k["status"])), None)
    if nxt:
        lines.append(f"Next: sampo focus {nxt['id']}")
    return cli.out(args, t, "\n".join(lines))


def segment(briefs, active_id=None):
    """The big task in view for the status surfaces: 'T-… XL 3/7 · wave 2'. The focused tree first, else the oldest
    open tree that has children; None when no big task has children."""
    byid = {b.id: b for b in briefs}
    parents = {b.meta.get("parent") for b in briefs if b.meta.get("parent")}
    open_trees = [b for b in briefs if b.tier in big_tiers() and b.status not in c.CLOSED and b.id in parents]
    if not open_trees:
        return None
    act = byid.get(active_id)
    parent = act.meta.get("parent") if act else None
    pick = next((b for b in open_trees if b.id in (active_id, parent)), None) or min(open_trees, key=lambda b: c.id_num(b.id))
    kids = _kids(briefs, pick.id)
    wave = _wave(kids)
    done = sum(_finished(k) for k in kids)
    return f"{pick.id} {pick.tier} {done}/{len(kids)} · " + (f"wave {wave}" if wave else "all in")


def _rows(p):
    try:
        with open(jsonl_path(p), encoding="utf-8") as f:
            out = []
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:  # a torn line is no row: the next close appends after it
                    continue
            return out
    except FileNotFoundError:
        return []


def record_close(p, b):
    """Append one row for a closed big task: its tree as it closed. Once per tree state: a second close of the same
    tree adds nothing (returns False), a changed tree adds its row (True). Read and append under one lock."""
    briefs = c.load_briefs(p)
    kids = _kids(briefs, b.id)
    state = c.sha1(json.dumps([b.status] + [[k.id, k.status] for k in kids]).encode()).hexdigest()[:12]
    t = _tree(p, b, briefs, _ctx(p, briefs))
    row = {"id": b.id, "title": b.title, "tier": b.tier, "children": t["total"], "done": t["done"],
           "time_min": t["time_min"], "attempts": t["attempts"], "cost": t["cost"], "cost_unknown": t["cost_unknown"],
           "estimate": t["estimate"], "state": state, "closed": c.now()}
    os.makedirs(p.dir, exist_ok=True)
    with c.lock(p.dir):
        if any(r.get("id") == b.id and r.get("state") == state for r in _rows(p)):
            return False
        with open(jsonl_path(p), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return True
