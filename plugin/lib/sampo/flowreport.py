"""T-5503: the flow report `sampo digest` carries (the Flow section and the `flow` key of its --json): where the time from a
request to done goes, what each agent type costs, what the tests cost, what the guard stopped, and how many tasks were
made against closed; each number shown against the last earlier day's snapshot, so a round of improvements measures its
own effect and the next one starts from the biggest number. (flow.py is the browser journey runner: `sampo flow FILE`.)

Read-only readers over the ledger (task_new, focus, builder_mark, accepted, lane_landed, task_done, guard_block,
check_run, ambient_run, ship_test, subagent) and the subagent transcripts the `subagent` events name; no new verb.

  stages      per closed task, the seconds between its marks: wait (task_new to its first focus), build (focus to the
              builder's first return), review (that return to the last accept, review fixes included), land (accept to
              lane_landed), close (landed to task_done). A stage a task has no pair of marks for is left out of that
              stage's n: a task closed by hand, with no lane, has a wait and no build. The median and p90 are over the
              tasks that have the stage; n says how many.
  agents      per agent type: runs, the runs whose transcript was read, and the median turns (an assistant reply, once
              per message id) and input-equivalent tokens (cost.weighted) per read run. A transcript is read only from
              under Claude Code's projects folder, at most MAX_READ of the newest runs, MAX_BYTES a file.
  tests       per origin (train: the land's gate; task: a task's own check; sentinel; ambient; ship): runs and the
              seconds the gates ran (the ledger keeps wall seconds, not CPU: per-run CPU is not recorded).
  guard       guard_block events by category.
  made_closed per day: tasks made (task_new) and closed (task_done), in the window.

The snapshot (flow-baseline.jsonl in the project's state folder, one line a local day per window length, written under
the project lock, the newest KEEP lines kept) holds the whole flow; the delta shown is against the newest snapshot of an
earlier day with the same window."""
import collections
import datetime
import json
import math
import os
import statistics

from sampo import core as c

STAGES = ("wait", "build", "review", "land", "close")
KINDS = ("task_new", "focus", "builder_mark", "accepted", "lane_landed", "task_done", "guard_block", "check_run",
         "ambient_run", "ship_test", "subagent")
LOOKBACK_DAYS = 90  # how far before the window a closed task's early marks are looked for
MAX_READ = 300  # the newest subagent transcripts read in one report
MAX_BYTES = 50 << 20  # a transcript larger than this is counted as a run and not read
KEEP = 400  # snapshot lines kept
FILE = "flow-baseline.jsonl"


def _t(ts):
    d = c.parse_ts(ts)
    return d.timestamp() if d else None


def _tids(e):
    return [x.strip() for x in str(e.get("task") or "").split(",") if x.strip()]


def _p90(xs):
    xs = sorted(xs)
    return xs[max(0, math.ceil(0.9 * len(xs)) - 1)]


def _stages(evs, since):
    marks = collections.defaultdict(dict)
    for e in evs:
        ev, t, d = e.get("event"), _t(e.get("ts")), e.get("data")
        if t is None or ev not in ("task_new", "focus", "builder_mark", "accepted", "lane_landed", "task_done"):
            continue
        if ev == "builder_mark" and not (isinstance(d, dict) and d.get("returned")):
            continue
        for tid in _tids(e):
            m = marks[tid]
            if ev in ("task_new", "focus", "builder_mark"):  # the first
                m.setdefault(ev, t)
            else:  # the last (a reopened task's later accept, land and close)
                m[ev] = t
    pairs = {"wait": ("task_new", "focus"), "build": ("focus", "builder_mark"), "review": ("builder_mark", "accepted"),
             "land": ("accepted", "lane_landed"), "close": ("lane_landed", "task_done")}
    vals = {k: [] for k in STAGES}
    closed = [m for m in marks.values() if m.get("task_done", 0) >= since]
    for m in closed:
        for k, (a, b) in pairs.items():
            if a in m and b in m and m[b] >= m[a]:
                vals[k].append(m[b] - m[a])
    return {k: {"n": len(v), "median_s": round(statistics.median(v), 1) if v else None,
                "p90_s": round(_p90(v), 1) if v else None} for k, v in vals.items()}


def _read_transcript(path, base):
    """(turns, weighted tokens) of one subagent transcript, or None when it is not under `base`, is too large or is gone."""
    from sampo import cost
    try:
        real = os.path.realpath(str(path))
        if os.path.commonpath([real, base]) != base or os.path.getsize(real) > MAX_BYTES:
            return None
        seen = {}
        with open(real, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if '"usage"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") if isinstance(e, dict) and e.get("type") == "assistant" else None
                if isinstance(m, dict) and isinstance(m.get("usage"), dict):
                    seen[m.get("id") or n] = cost.weighted(m["usage"])
        return len(seen), sum(seen.values())
    except (OSError, ValueError):
        return None


def _agents(runs, base):
    """{agent type: {runs, read, turns, tokens}} from the (type, transcript path) of each subagent run, newest last."""
    out = collections.OrderedDict()
    got = collections.defaultdict(list)
    for i, (kind, path) in enumerate(runs):
        row = out.setdefault(kind, {"runs": 0, "read": 0})
        row["runs"] += 1
        if i >= len(runs) - MAX_READ and path and (r := _read_transcript(path, base)):
            got[kind].append(r)
    for kind, row in out.items():
        rs = got.get(kind, [])
        row["read"] = len(rs)
        row["turns"] = statistics.median(t for t, _ in rs) if rs else None
        row["tokens"] = round(float(statistics.median(k for _, k in rs)), 1) if rs else None
    return dict(out)


def _origin(e):
    d = e.get("data") if isinstance(e.get("data"), dict) else {}
    if e.get("event") == "check_run":
        return "train" if d.get("train") else "sentinel" if d.get("sentinel") else "task"
    return {"ambient_run": "ambient", "ship_test": "ship"}[e["event"]]


def _seconds(e):
    d = e.get("data") if isinstance(e.get("data"), dict) else {}
    xs = [d.get("s")] if e.get("event") == "ship_test" else [r.get("s") for r in d.get("results") or [] if isinstance(r, dict)]
    return sum(x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x))


def compute(p, days, now=None):
    """The flow over the last `days` days: {days, since, stages, agents, tests, guard, made_closed} (no baseline)."""
    from sampo import cost
    since_iso = cost._since(days)
    since = _t(since_iso + "Z")
    evs = c.ledger_events(p, KINDS, since=c.iso(since - LOOKBACK_DAYS * 86400))
    win = [e for e in evs if str(e.get("ts") or "")[:19] >= since_iso]
    tests = collections.OrderedDict()
    guard, made, closed, runs, seen = collections.Counter(), collections.defaultdict(set), collections.defaultdict(set), [], set()
    for e in win:
        ev, day = e.get("event"), str(e.get("ts") or "")[:10]
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        if ev == "guard_block":
            guard[str(d.get("category") or "?")] += 1
        elif ev in ("check_run", "ambient_run", "ship_test"):
            row = tests.setdefault(_origin(e), {"runs": 0, "seconds": 0.0})
            row["runs"] += 1
            row["seconds"] = round(row["seconds"] + _seconds(e), 1)
        elif ev == "task_new":
            made[day].update(_tids(e))
        elif ev == "task_done":
            closed[day].update(_tids(e))
        elif ev == "subagent" and d.get("agent_type") and (d.get("agent_id"), d.get("transcript")) not in seen:
            seen.add((d.get("agent_id"), d.get("transcript")))
            runs.append((str(d["agent_type"]), d.get("transcript")))
    base = os.path.dirname(os.path.realpath(cost.transcripts_dir(p.root)))
    return {"days": days, "since": since_iso, "stages": _stages(evs, since), "agents": _agents(runs, base),
            "tests": dict(tests), "guard": dict(guard.most_common()),
            "made_closed": [{"day": d, "made": len(made[d]), "closed": len(closed[d])} for d in sorted(set(made) | set(closed))]}


def today():
    """The local calendar day, as a snapshot is keyed."""
    return c.clock().astimezone().strftime("%Y-%m-%d")


def _snapshots(p):
    out = []
    for r in c.tail_jsonl(os.path.join(p.dir, FILE), KEEP):
        if isinstance(r, dict) and isinstance(r.get("day"), str) and isinstance(r.get("flow"), dict):
            out.append(r)
    return out


def last_before(p, day, days):
    """The newest snapshot of a day before `day` for this window length, or None."""
    return next((r for r in reversed(_snapshots(p)) if r["day"] < day and r.get("days") == days), None)


def save(p, flow, day):
    """Append today's snapshot unless this window length already has one for `day`; True when written. Under the project
    lock; the file is cut back to the newest KEEP lines."""
    with c.lock(p.dir, timeout=1):
        rows = _snapshots(p)
        if any(r["day"] == day and r.get("days") == flow["days"] for r in rows):
            return False
        rows.append({"day": day, "at": c.now(), "days": flow["days"], "flow": flow})
        path = os.path.join(p.dir, FILE)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows[-KEEP:]))
        os.replace(tmp, path)
        return True


def flat(flow):
    """Every number of a flow by a dotted name, for the delta against a snapshot."""
    out = {}
    for k, v in (flow.get("stages") or {}).items():
        out[f"stage.{k}"] = v.get("median_s")
    for k, v in (flow.get("agents") or {}).items():
        out.update({f"agent.{k}.{f}": v.get(f) for f in ("runs", "turns", "tokens")})
    for k, v in (flow.get("tests") or {}).items():
        out.update({f"tests.{k}.{f}": v.get(f) for f in ("runs", "seconds")})
    out.update({f"guard.{k}": v for k, v in (flow.get("guard") or {}).items()})
    mc = flow.get("made_closed") or []
    out["made"], out["closed"] = sum(r["made"] for r in mc), sum(r["closed"] for r in mc)
    return out


def _dur(s):
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.0f}m" if s < 7200 else f"{s / 3600:.1f}h"


def _num(v):
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}k" if v >= 1e3 else f"{v:g}"


def _chg(cur, was, fmt):
    """' (+1)' the change from the snapshot's number, ' (new)' when it had none, '' when there is no snapshot."""
    if was is _NO:
        return ""
    if was is None:
        return " (new)"
    d = cur - was
    return " (=)" if d == 0 else f" ({'+' if d > 0 else '-'}{fmt(abs(d))})"


_NO = object()


def lines(flow, base):
    """The Flow section: a header naming the snapshot compared against, then one line a group, biggest number first."""
    f0 = flat(base["flow"]) if base else None

    def was(key):
        return _NO if f0 is None else f0.get(key)

    head = (f"Flow, last {flow['days']:g} day(s), against {base['day']}'s snapshot:" if base
            else f"Flow, last {flow['days']:g} day(s) (first snapshot: nothing to compare against yet):")
    out = [head]
    st = flow["stages"]
    have = sorted((k for k in STAGES if st[k]["median_s"] is not None), key=lambda k: -st[k]["median_s"])
    gone = [k for k in STAGES if st[k]["median_s"] is None]
    if have or gone:
        out.append("- Request to done, median by stage: " + " · ".join(
            f"{k} {_dur(st[k]['median_s'])}{_chg(st[k]['median_s'], was('stage.' + k), _dur)} (p90 {_dur(st[k]['p90_s'])}, n {st[k]['n']})"
            for k in have) + (f"{' · ' if have else ''}unread: {', '.join(gone)} (no marks in the ledger)" if gone else ""))
    ag = sorted(flow["agents"].items(), key=lambda kv: -(kv[1]["tokens"] or 0) * kv[1]["runs"])
    if ag:
        out.append("- Per agent run (median, input-equivalent tokens and turns): " + " · ".join(
            (f"{k} {_num(v['tokens'])}{_chg(v['tokens'], was(f'agent.{k}.tokens'), _num)} tokens, "
             f"{v['turns']:g} turns{_chg(v['turns'], was(f'agent.{k}.turns'), _num)}, " if v["read"] else f"{k} unread, ")
            + f"{v['runs']} run(s){_chg(v['runs'], was(f'agent.{k}.runs'), _num)}" for k, v in ag))
    ts = sorted(flow["tests"].items(), key=lambda kv: -kv[1]["seconds"])
    if ts:
        out.append("- Test runs by origin (gate seconds, wall): " + " · ".join(
            f"{k} {v['runs']} run(s){_chg(v['runs'], was(f'tests.{k}.runs'), _num)}, {_dur(v['seconds'])}"
            f"{_chg(v['seconds'], was(f'tests.{k}.seconds'), _dur)}" for k, v in ts))
    if flow["guard"]:
        out.append("- Guard blocks by category: " + " · ".join(
            f"{k} {n}{_chg(n, was('guard.' + k), _num)}" for k, n in flow["guard"].items()))
    now = flat(flow)
    out.append(f"- Made {now['made']}{_chg(now['made'], was('made'), _num)}, closed {now['closed']}"
               f"{_chg(now['closed'], was('closed'), _num)}"
               + (" (per day " + ", ".join(f"{r['day'][5:]} {r['made']}/{r['closed']}" for r in flow["made_closed"][-7:]) + ")"
                  if flow["made_closed"] else ""))
    return out


def report(p, days, write=True):
    """(flow with its `baseline`, the Flow lines). Never raises: a report that cannot be read is None and []. Writes
    today's snapshot once per window length a local day (`write`)."""
    try:
        flow = compute(p, days)
        base = last_before(p, today(), days)
        shown = lines(flow, base)
        if write:
            try:
                save(p, flow, today())
            except (OSError, c.LockTimeout):  # the snapshot is bookkeeping: the report stands without it
                pass
        return {**flow, "baseline": None if base is None else {**base["flow"], "day": base["day"]}}, shown
    except Exception as e:  # a digest never fails on a derived report
        return {"error": f"{type(e).__name__}: {e}"[:200]}, []
