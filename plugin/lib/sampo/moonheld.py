"""T-5180 (child k4 of T-4338, Moons at scale): did the bar hold? `held(p, n, local_only)` re-judges a seeded sample of n
kept moons (the funnel's survivors) with moonscale's kill and tier judges, blind: each moon is asked on a model that is
not one that kept it (the models on its kill, tier and novelty `kept` events), so a judge never grades its own verdict. A
moon whose keepers are the only models that answer stays unsampled, never re-judged by its keeper. A moon holds when the
kill test still passes and the tier is still high or more. One row per sampled moon is appended to moons/held.jsonl (each
row carries its run's sample, held and rate) and a `moons_held` event {sample, held, rate} is logged. A moon is sampled
again only after every kept one that can be sampled was (a round); `latest(p)` is the newest run's rate or None.
A re-judge only reads the store (moonscale's rows and fate, no store_lock), so it runs beside a funnel or a grow; its one
write is held.jsonl's append under core's lock. The rate advises (is the funnel's bar real?); it gates nothing."""
import collections
import json
import os
import random

from sampo import budget
from sampo import core as c
from sampo import models, moonscale as ms, moons

HELD = "held.jsonl"
SAMPLE = 20  # kept moons re-judged by default


def _path(p):
    return os.path.join(ms._dir(p), HELD)


def _rows(p):
    """The held rows in write order; a row without a string id is not one."""
    return [x for x in ms._read(_path(p)) if isinstance(x.get("id"), str)]


def latest(p):
    """The newest run's rate (held / sampled), or None before any moon was sampled."""
    for x in reversed(_rows(p)):
        if type(x.get("rate")) in (int, float):
            return x["rate"]
    return None


def _kept(rs, f):
    return [x for x in rs if (e := f.get(x.get("id"))) and e.get("stage") == "novelty" and e.get("verdict") == "kept"]


def _keepers(p, ids):
    """{id: the base models of its kill, tier and novelty `kept` events}: the models that kept it. One pass over the funnel."""
    out = collections.defaultdict(set)
    for e in ms._read(os.path.join(ms._dir(p), ms.FUNNEL)):
        if e.get("id") in ids and e.get("verdict") == "kept" and e.get("stage") in ("kill", "tier", "novelty") \
                and isinstance(e.get("model"), str) and e["model"]:
            out[e["id"]].add(ms._base(e["model"]))
    return out


def _candidates(sensitive, local_only):
    """[(model name, the kwargs _ask takes)]: each usable local model, then Haiku unless the run is local-only or the
    project sensitive (a sensitive project's records never go to the cloud)."""
    out, seen = [], set()
    for prof in ms._locals(sensitive):
        name = prof.get("name")
        if name and name not in seen:
            seen.add(name)
            out.append((name, {"writer": "local", "profile": name}))
    if not (sensitive or local_only):
        out.append(("haiku", {"writer": "haiku"}))
    return out


class _Stop(Exception):
    """The budget said no: the run ends with what it has."""


def _rejudge(p, system, batch, keys, parse, keepers, cands, run):
    """({n: answer}, model) from the first candidate that is not one of `keepers`, answers, and whose reply parses; None
    when none does. A reply from a keeper (a named profile that failed falls back to another local) is thrown away."""
    prompt = ms._moons_prompt(batch, keys)
    for name, kw in cands:
        if name in keepers:
            continue
        try:
            text, model = ms._ask(p, system, prompt, paid=run["paid"], feature="moons-held", **kw)
        except budget.BudgetError as e:
            run["error"] = c.fit(ms._norm(e), 300)
            raise _Stop() from None
        except (ValueError, OSError, models.ModelError) as e:  # LocalOnly is a ValueError
            run["error"] = c.fit(ms._norm(e), 300)
            continue
        run["calls"] += 1
        run["paid"] += ms._paid(model)
        if ms._base(model) in keepers:
            continue
        said = parse(text, len(batch))
        if said:
            return said, model
        run["error"] = f"{model}: no answers in the reply"
    return None


def held(p, n=SAMPLE, local_only=False, seed=None):
    """Re-judge up to n kept moons blind; {sample, held, rate, unsampled, round, calls, error}: rate is None for no moon
    sampled. `seed` (default: how many rows held.jsonl has) makes the draw repeatable."""
    if type(n) is not int or n < 0:
        raise ValueError("n is a count of moons, 0 or more")
    sensitive = bool(c.read_meta(p).get("sensitive"))
    rs, f = ms.rows(p), ms.fate(p)
    kept = _kept(rs, f)
    prior = _rows(p)
    rnd = max((x["round"] for x in prior if type(x.get("round")) is int), default=1)
    keepers = _keepers(p, {x["id"] for x in kept})
    cands = _candidates(sensitive, local_only) if kept and n else []

    def able(x):  # a model outside its keepers answers, as far as the probe says
        return any(name not in keepers[x["id"]] for name, _ in cands)
    done = {x["id"] for x in prior if x.get("round") == rnd}
    pool = [x for x in kept if x["id"] not in done and able(x)]
    if not pool and done:  # every kept moon that can be sampled was: the next round starts
        rnd, pool = rnd + 1, [x for x in kept if able(x)]
    pool.sort(key=lambda x: x["id"])
    rng = random.Random(len(prior) if seed is None else seed)
    draw = rng.sample(pool, min(n, len(pool)))
    run = {"calls": 0, "paid": 0, "error": ""}
    groups = collections.defaultdict(list)
    for x in draw:
        groups[tuple(sorted(keepers[x["id"]]))].append(x)
    out, at = [], c.now()
    try:
        for key, moons_ in sorted(groups.items()):
            for k in range(0, len(moons_), ms.JUDGE_BATCH):
                batch = moons_[k:k + ms.JUDGE_BATCH]
                kill = _rejudge(p, ms.KILL_SYSTEM + ms.SAMPO_NOTE, batch, ("what", "kill"), ms._yesno, set(key), cands, run)
                tier = kill and _rejudge(p, moons.RATE_SYSTEM, batch, ("today", "with_this", "what", "prior_art"), ms._rated,
                                         set(key), cands, run)
                if not (kill and tier):
                    continue
                for i, x in enumerate(batch, 1):
                    if i in kill[0] and i in tier[0]:  # both judges answered it, else it stays unsampled
                        t, wild, _ = tier[0][i]
                        out.append({"id": x["id"], "at": at, "round": rnd, "kill": kill[0][i][0], "kill_model": kill[1],
                                    "tier": t, "wild": wild, "tier_model": tier[1], "keepers": list(key),
                                    "held": bool(kill[0][i][0] and t in moons.TIERS[2:])})
    except _Stop:
        pass
    sample, ok = len(out), sum(x["held"] for x in out)
    rate = round(ok / sample, 3) if sample else None
    if out:
        with c.lock(p.dir):  # core's lock, never moonscale.store_lock: a funnel running beside it is no reason to wait
            c.append_line(_path(p), "\n".join(json.dumps({**x, "sample": sample, "held_n": ok, "rate": rate}, ensure_ascii=False) for x in out))
        c.log_event(p, "moons_held", data={"sample": sample, "held": ok, "rate": rate})
    return {"sample": sample, "held": ok, "rate": rate, "unsampled": len(draw) - sample, "round": rnd,
            "calls": run["calls"], "error": run["error"]}


def held_line(r):
    if not r["sample"]:
        return "Moons held: no kept moon could be re-judged on another model" + (f" ({r['error']})" if r["error"] else "")
    return (f"Moons held: {r['held']} of {r['sample']} kept moons passed a blind re-judge ({round(100 * r['rate'])}%)"
            + (f"; {r['unsampled']} unanswered" if r["unsampled"] else ""))
