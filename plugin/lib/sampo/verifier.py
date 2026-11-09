"""T-2977: the close-holds verifier. A logistic head that scores a close, P(it holds: not reverted, not fixed later),
from machine signals only: counts and parsed test totals from the brief's evidence, never a word Claude wrote (the
lake's agent text stays off). Trained on the lake's outcome labels with whole projects held out (`sampo exam models
--verifier`); `sampo task finish` logs a close_score event in shadow and asks for the missing evidence only when
models.live names the verifier. Pure Python (no numpy here): batch gradient descent, L2, balanced class weights."""
import json
import math
import os
import re

from sampo import core as c

TYPES = ("FIX", "FEATURE", "CLEAN", "PERF", "SECURITY", "RESEARCH")
FEATURES = ("criteria", "criteria_verified", "steps", "steps_done", "evidence", "red_green", "tests_run",
            "tests_passed", "audits", "lenses", "files", "minutes", "tier_s", "tier_m", "tier_l") + tuple(
    "type_" + t.lower() for t in TYPES)
USE = "verifier"
WEIGHTS = os.path.join("research", "models", "verifier.json")  # in the project's state dir
BENCH = os.path.join("research", "models", "bench.jsonl")  # in the project's state dir (T-3064: not committed), beside the model bench
FALSE_ALARM = 0.10
LIVE_AUC, LIVE_PROJECTS = 0.65, 3  # T-3073: advice only until the held-out AUC over this many projects passes this
ITERS, LR, L2 = 300, 0.5, 0.01
FALSE = ("reverted", "regret")  # a close later reverted or fixed counts as false; "settling" is not yet known

_LINE = re.compile(r"`([^`]*)` → (✗ )?exit (\d+)")
_RAN_N = re.compile(r"\bRan (\d+) tests?\b")
_FAILED = re.compile(r"\bFAILED \((?:failures|errors)=(\d+)(?:, (?:failures|errors)=(\d+))?")
_PASSED = re.compile(r"(\d+) passed")
_BAD = re.compile(r"(\d+) (?:failed|errors?)\b")
_TS = re.compile(r"\((\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\)\s*$")
_KIND = re.compile(r"^-\s+\((step|ac|audit)\b")


def _tests(line):
    """(run, passed) the evidence line's output shows: unittest's Ran N / FAILED (failures=k), pytest's N passed, M failed."""
    if m := _RAN_N.search(line):
        bad = sum(int(x) for f in _FAILED.findall(line) for x in f if x)
        return int(m.group(1)), max(0, int(m.group(1)) - bad)
    ok = sum(int(x) for x in _PASSED.findall(line))
    return ok + sum(int(x) for x in _BAD.findall(line)), ok


def features(b, minutes=None):
    """The text-free feature dict of a brief (a done one, or one about to close): numbers only."""
    from sampo import outcomes
    ev = b.evidence()
    f = dict.fromkeys(FEATURES, 0)
    acs, steps = b.acceptance(), b.steps()
    f.update(criteria=len(acs), criteria_verified=sum(1 for a in acs if c.verify_of(a.text)), steps=len(steps),
             steps_done=sum(1 for s in steps if s.done), files=len(b.meta.get("scope") or []))
    seen, last_red, stamps, ran = set(), set(), [], {}
    for line in ev:
        if m := _TS.search(line):
            if t := c.parse_ts(m.group(1)):
                stamps.append(t.timestamp())
        # only what fm ran counts (a typed line is the agent's word), and only the output after the arrow
        if (_KIND.match(line) or [None, None])[1] == "audit" or c._RAN_MARK not in line or not (m := _LINE.search(line)):
            continue
        cmd, red = m.group(1), bool(m.group(2) or int(m.group(3)))
        ran[cmd] = _tests(line[m.end():])  # distinct commands: a retried close's repeat run changes nothing
        if red:
            last_red.add(cmd)
        elif cmd in last_red:
            last_red.discard(cmd)
            seen.add(cmd)
    f.update(evidence=len(ran), red_green=len(seen), tests_run=sum(r for r, _ in ran.values()),
             tests_passed=sum(o for _, o in ran.values()))
    lens = {lens for lens, *_ in b.audits()}  # distinct lenses: add_audit doesn't dedupe a retry
    f["audits"] = len(lens)
    f["lenses"] = len(lens - {"self"})
    f["minutes"] = minutes if minutes is not None else outcomes.active_minutes(sorted(stamps))
    if str(b.tier or "").lower() in ("s", "m", "l"):
        f["tier_" + b.tier.lower()] = 1
    if (b.type or "").upper() in TYPES:
        f["type_" + b.type.lower()] = 1
    return {k: f[k] for k in FEATURES}


# ---------------------------------------------------------------- the head
def _x(feats):
    return [math.log1p(max(0, feats.get(k) or 0)) for k in FEATURES]


def _sig(z):
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def labelled(rows):
    """[(project, features, 1 holds / 0 false)] of the rows with a known outcome and a features dict."""
    return [((r.get("prov") or {}).get("project"), r["features"], 0 if r["label"] in FALSE else 1)
            for r in rows if isinstance(r.get("features"), dict) and (r.get("label") in FALSE or r.get("label") == "held")]


def train(rows):
    """The head on rows (lake rows): {features, mu, sd, w, b}; None when a class is missing. Deterministic."""
    return _fit([(_x(f), y) for _, f, y in labelled(rows)])


def _fit(xy):
    pos = sum(y for _, y in xy)
    if not xy or pos in (0, len(xy)):
        return None
    n, d = len(xy), len(FEATURES)
    mu = [sum(x[i] for x, _ in xy) / n for i in range(d)]
    sd = [math.sqrt(sum((x[i] - mu[i]) ** 2 for x, _ in xy) / n) or 1.0 for i in range(d)]
    cw = {1: n / (2 * pos), 0: n / (2 * (n - pos))}  # balanced: 92 false against ~500 held weigh the same
    z = [([(x[i] - mu[i]) / sd[i] for i in range(d)], y) for x, y in xy]
    w, b = [0.0] * d, 0.0
    for _ in range(ITERS):
        gw, gb = [0.0] * d, 0.0
        for x, y in z:
            e = (_sig(b + sum(wi * xi for wi, xi in zip(w, x))) - y) * cw[y]
            gb += e
            for i in range(d):
                gw[i] += e * x[i]
        w = [wi - LR * (g / n + L2 * wi) for wi, g in zip(w, gw)]
        b -= LR * gb / n
    return {"features": list(FEATURES), "mu": mu, "sd": sd, "w": w, "b": b}


def score(model, feats):
    """P(the close holds), 0..1."""
    x = _x(feats)
    return _sig(model["b"] + sum(w * (xi - m) / s for w, xi, m, s in zip(model["w"], x, model["mu"], model["sd"])))


def auc(scores, labels):
    """P(a held close outscores a false one), ties half; None without both classes."""
    hi = [s for s, y in zip(scores, labels) if y]
    lo = [s for s, y in zip(scores, labels) if not y]
    if not hi or not lo:
        return None
    return round(sum((h > l) + 0.5 * (h == l) for h in hi for l in lo) / (len(hi) * len(lo)), 4)


def threshold(scores, labels, fa=FALSE_ALARM):
    """The score below which a close is flagged so that at most `fa` of the held ones are (a false alarm)."""
    hi = sorted(s for s, y in zip(scores, labels) if y)
    return hi[int(fa * len(hi))] if hi else None


def caught(scores, labels, thr):
    """The share of false closes under thr; None when there are none (or no threshold)."""
    lo = [s for s, y in zip(scores, labels) if not y]
    return None if not lo or thr is None else round(sum(s < thr for s in lo) / len(lo), 4)


def _metrics(scores, labels):
    thr = threshold(scores, labels)
    return {"n": len(labels), "false_done": sum(1 for y in labels if not y), "auc": auc(scores, labels),
            "caught_at_10pct_false_alarms": caught(scores, labels, thr)}


def evaluate(rows):
    """Leave one project out: train on the others, score it; each project's AUC and false-done caught at 10% false
    alarms, and the same pooled over every held-out score. `today` is the false-done share a rule-only close lets by."""
    data = labelled(rows)
    out, pool_s, pool_y = {}, [], []
    for proj in sorted({p for p, _, _ in data if p}):
        held = [(f, y) for p, f, y in data if p == proj]
        model = _fit([(_x(f), y) for p, f, y in data if p != proj])
        if model is None:
            out[proj] = _metrics([], [y for _, y in held])
            continue
        s = [score(model, f) for f, _ in held]
        out[proj] = _metrics(s, [y for _, y in held])
        pool_s += s
        pool_y += [y for _, y in held]
    ys = [y for _, _, y in data]
    return {"projects": out, "pooled": _metrics(pool_s, pool_y), "threshold": threshold(pool_s, pool_y),
            "today": round(sum(1 for y in ys if not y) / len(ys), 4) if ys else None, "rows": len(ys)}


def gate_open(model):
    """T-3073: may the head be live at all: its pooled held-out AUC, over LIVE_PROJECTS+ projects, passes LIVE_AUC."""
    t = (model or {}).get("trained") or {}
    return (t.get("projects") or 0) >= LIVE_PROJECTS and (t.get("auc") or 0) > LIVE_AUC


def auc_note(model):
    t = (model or {}).get("trained") or {}
    return f"pooled held-out AUC {t['auc']}" if t.get("auc") is not None else "pooled held-out AUC unmeasured"


def ask(tid, s, model, feats):
    """The fixed ask: the features whose weight pulls the score up and that this close is furthest under typical."""
    x = _x(feats)
    gap = sorted(((model["w"][i] * (model["mu"][i] - x[i]) / model["sd"][i], k) for i, k in enumerate(FEATURES)), reverse=True)
    want = [f"{k} (has {feats.get(k) or 0})" for g, k in gap[:3] if g > 0 and model["w"][FEATURES.index(k)] > 0]
    return (f"{tid}: close score {s:.2f} is under {model.get('threshold', 0):.2f}: closes like this were reverted or fixed "
            f"later ({auc_note(model)}, advice only). Record more evidence, then finish again: " + (", ".join(want) or "more passing runs and audits"))


def run(p):
    """sampo exam models --verifier: the held-out numbers to bench.jsonl (one row per project, one pooled) and the head,
    trained on every project, with its flag threshold to the project's state. No weights when a class is missing."""
    from sampo import lake
    rows = [r for r in lake.collect()[0] if not r.get("canary")]
    res = evaluate(rows)
    stamp = c.now()
    path = c.bench_path(p)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for name, m in list(res["projects"].items()) + [("pooled", res["pooled"])]:
        c.append_line(path, json.dumps({"arm": "verifier", "use": USE, "backend": "logistic", "heldout": name,
                                        "run": "verifier-" + stamp, "stamp": stamp, "today_false_done": res["today"], **m}) + "\n")
    model = train(rows)
    if model:
        model["threshold"] = res["threshold"]  # the pooled held-out scores' 10% false-alarm cut, not the head's own training rows
        model["trained"] = {"rows": res["rows"], "at": stamp, "auc": res["pooled"].get("auc"),
                            "projects": sum(m.get("auc") is not None for m in res["projects"].values())}
        c.write_atomic(os.path.join(p.dir, WEIGHTS), json.dumps(model, indent=2, sort_keys=True) + "\n")
    wp = os.path.join(p.dir, WEIGHTS)
    if model is None and os.path.exists(wp):
        os.remove(wp)  # a stale head must stop scoring
    return dict(res, weights=bool(model))


# ---------------------------------------------------------------- at close
def close_score(p, b):
    """Called by `sampo task finish` just before it marks the task done: logs a close_score event (shadow, always) and
    returns the fixed ask when models.live names the verifier and the score is under its threshold, else None. Never
    raises: a missing or broken weights file is no score."""
    try:
        try:
            with open(os.path.join(p.dir, WEIGHTS), encoding="utf-8") as f:
                model = json.load(f)
        except (OSError, ValueError):
            model = None  # untrained: the event still says so
        if model is None:
            return None  # untrained: no event (one ledger line per finish in every project otherwise)
        feats = features(b)
        s = score(model, feats)
        thr = model.get("threshold")
        from sampo import models
        live = bool(models.live(p, USE)) and gate_open(model)  # T-3073: a decision row alone can't make a weak head live
        low = thr is not None and s < thr
        c.log_event(p, "close_score", b.id, {"score": round(s, 4), "threshold": thr, "live": live, "low": low, "auc": (model.get("trained") or {}).get("auc"),
                                              "features": feats})
        return ask(b.id, s, model, feats) if live and low else None
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
        return None
