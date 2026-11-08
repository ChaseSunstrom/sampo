"""fm budget (T-0227): bounded use of model work, not avoidance. Everything Sampo spawns — tool-less children (fm ideas,
oracle, research ask, evolve's mutation), headless replays (fm bench) and Claude's Agent subagents — records what it
cost in one global ledger (state/spend.jsonl: USD from claude's JSON result, tokens for subagents), and caps
(state/budget.json) refuse a command whose estimate would pass the per-command or the day's limit. Past 80% of weekly
usage (or 90% of the 5-hour window, the statusline's snapshot) every cap halves, like economy mode. Raising a cap isn't
blocked: it is recorded as a costly decision, which the user reviews (fm decide --review). Subagents have no token cap
(T-0320): they wait only while usage runs ahead of the week's pace or the 5-hour window is nearly spent."""
import contextlib
import datetime
import glob
import json
import math
import os
import re
import threading
import time

from sampo import core as c

DEFAULTS = {"day": 15.0, "run": 6.0}  # USD per day, USD per command
PACE_FLOOR = 75  # weekly usage % below which subagents never wait for the pace (T-0364), when no horizon is set
SAMPLE_EVERY = 600  # T-3902: one weekly-usage sample per 10 min, from the statusline's snapshots
SAMPLE_KEEP = 1008  # the samples ring: a week of them (state/usage-samples.jsonl)
RATE_SPAN = 3 * 3600  # the recent burn rate: the last 3 h of this week's samples
PACE_HOLD = 1.5  # with a horizon, optional work waits while the recent rate is over this many times the allowance
_SAMPLE_LOCK = threading.Lock()


class BudgetError(ValueError):  # a ValueError: callers already turn those into a clean refusal
    pass


def _path(name):
    return os.path.join(c.state_dir(), name)


def caps():
    try:
        with open(_path("budget.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    out = dict(DEFAULTS)
    for k, v in DEFAULTS.items():  # a corrupt or hand-edited file falls back per cap, never crashes or disables
        try:
            x = type(v)(data.get(k, v)) if isinstance(data, dict) else v
            out[k] = x if math.isfinite(x) and x >= 0 else v
        except (TypeError, ValueError, OverflowError):
            pass
    return out


def _raw():
    """budget.json as written (the caps and the horizon); {} when unreadable or not an object."""
    try:
        with open(_path("budget.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_caps(**new):
    cur = caps()
    cur.update({k: v for k, v in new.items() if v is not None and k in DEFAULTS})
    os.makedirs(c.state_dir(), exist_ok=True)
    c.write_atomic(_path("budget.json"), json.dumps(dict(_raw(), **cur)))  # the horizon stays
    return cur


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_until(text, now=None):
    """T-3902: `budget set --until`: an epoch or an ISO time (UTC when it says so, else local), 'reset' (the week's reset
    as the newest snapshot has it), or 'off' (None). A time already past is refused."""
    now = now or time.time()
    t = str(text or "").strip()
    if t.lower() in ("off", "none"):
        return None
    if t.lower() == "reset":
        return "reset"
    try:
        if re.fullmatch(r"\d+(\.\d+)?", t):
            at = float(t)
        else:
            at = datetime.datetime.fromisoformat(re.sub(r"\s*(UTC|GMT|Z)$", "+00:00", t, flags=re.I)).timestamp()
    except (ValueError, OverflowError):
        raise BudgetError(f"budget: --until is a time like 2026-11-11 07:00 UTC, an epoch, 'reset' or 'off', not {t!r}")
    if not math.isfinite(at) or at <= now:
        raise BudgetError(f"budget: the horizon {t!r} is not in the future")
    return at


def set_horizon(until):
    """T-3902: the usage horizon in budget.json: an epoch, 'reset', or None to clear it. Not a cap: it changes no limit."""
    data = _raw()
    if until is None:
        data.pop("until", None)
    else:
        data["until"] = until
    os.makedirs(c.state_dir(), exist_ok=True)
    c.write_atomic(_path("budget.json"), json.dumps(data))


def horizon(now=None):
    """T-3902: the usage horizon's epoch, or None: no horizon set, a horizon already past (the 75% floor again), or
    'reset' with no weekly reading to take the reset from."""
    now = now or time.time()
    at = _raw().get("until")
    if at == "reset":
        w = forecast(now).get("seven_day")
        at = w["resets_at"] if w else None
    return float(at) if _num(at) and at > now else None


def _today():
    return datetime.date.today().isoformat()  # the user's local day


def ledger(days=1):
    """The spend rows of the last `days` local days (of the newest 20,000). T-3580: read from a day before since by the
    rows' UTC stamps (a local day starts up to 14 h either side), not parsed whole: fm ui asked on every pane poll."""
    first = datetime.date.today() - datetime.timedelta(days=days - 1)
    since = first.isoformat()
    try:
        with open(_path("spend.jsonl"), "rb") as f:
            c._seek_since(f, (first - datetime.timedelta(days=1)).isoformat().encode())
            lines = f.read().splitlines()[-20000:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and str(e.get("day", "")) >= since:
            out.append(e)
    return out


_LOCK, _FLIGHT = threading.RLock(), {}  # T-2292: sessions running at once under one process, by feature


@contextlib.contextmanager
def reserve(feature, est, smaller=""):
    """One session's spend slot: the check counts `est` for this session and for each one still running (their cost
    isn't in the ledger yet), under a lock so parallel sessions can't all pass the same headroom. Record inside the block."""
    with _LOCK:
        check(feature, est * (_FLIGHT.get(feature, 0) + 1), smaller)
        _FLIGHT[feature] = _FLIGHT.get(feature, 0) + 1
    try:
        yield
    finally:
        with _LOCK:
            _FLIGHT[feature] -= 1


def record(feature, usd=None, runs=1, tokens=None, project=None, detail=""):
    with _LOCK:
        return _record(feature, usd, runs, tokens, project, detail)


RUN = {"usd": 0.0}  # T-2871: what this command has spent so far; one process is one command (no caller is a server)


def _record(feature, usd, runs, tokens, project, detail):
    feature = os.environ.get("FOREMAN_SPEND_AS") or feature  # T-3081: a Kalevala tower's job spends under Kalevala's line
    tower = os.environ.get("FOREMAN_SPEND_TOWER")  # T-3082: and its spend names the tower, so watch can read each tower's own
    if tower:
        detail = f"tower:{tower} {detail}"
    if usd:
        RUN["usd"] += usd
    e = {"ts": c.now(), "day": _today(), "feature": feature, "usd": round(usd, 4) if usd is not None else None,
         "runs": runs, "tokens": tokens, "project": project, "detail": c.fit(str(detail), 120)}
    os.makedirs(c.state_dir(), exist_ok=True)
    c.append_line(_path("spend.jsonl"), json.dumps(e) + "\n")
    return e


def spent(day_rows=None):
    rows = ledger() if day_rows is None else day_rows
    return (round(sum(e.get("usd") or 0 for e in rows), 4),
            sum(e.get("tokens") or 0 for e in rows if str(e.get("feature", "")).startswith("subagent")))


def estimate(feature, runs, fallback):
    """USD for `runs` more runs of a feature: its recent mean cost per run, else the fallback per run."""
    recent = [e for e in c.tail_jsonl(_path("spend.jsonl"), 5000) if e.get("feature") == feature and e.get("usd")
              and e.get("runs")][-20:]
    per = sum(e["usd"] for e in recent) / sum(e["runs"] for e in recent) if recent else fallback
    return round(per * runs, 4)


SNAP_SPAN = 6 * 3600  # T-2752: a snapshot this much older than the newest is another session's past, not the usage now


def _recent_snaps():
    """The newest statusline snapshots, newest first, none more than SNAP_SPAN older than the newest: another session's
    stale reading (a previous subscription's) must never stand in for this session's."""
    snaps = sorted(glob.glob(os.path.join(c.state_dir(), "sessions", "*.json")), key=os.path.getmtime)
    if not snaps:
        return []
    newest = os.path.getmtime(snaps[-1])
    return [s for s in reversed(snaps[-3:]) if newest - os.path.getmtime(s) <= SNAP_SPAN]


def rate_limits():
    """{"five_hour": %, "seven_day": %, "week_gone": share of the week gone by} from the newest statusline snapshot
    that has any; a window whose reset time has passed reads as 0 (it emptied since the snapshot)."""
    now = time.time()
    for path in _recent_snaps():
        try:
            with open(path, encoding="utf-8") as f:
                rl = json.load(f).get("rate_limits") or {}
            out = {}
            for k in ("five_hour", "seven_day"):
                w = rl.get(k) or {}
                used, resets = w.get("used_percentage"), w.get("resets_at")
                if not isinstance(used, (int, float)) or isinstance(used, bool):
                    continue
                known = isinstance(resets, (int, float))
                out[k] = 0 if known and resets <= now else used
                if k == "seven_day" and known and 0 < resets - now <= 7 * 86400:  # else (ms epoch, skew) pace unknown
                    out["week_gone"] = 1 - (resets - now) / (7 * 86400)
        except (OSError, ValueError, AttributeError):
            continue
        if out:
            return out
    return {}


WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}


def forecast(now=None):
    """T-0973: per usage window in the newest statusline snapshot, {used, resets_at, projected (percent at reset at
    the pace so far), full_at (when it fills, if before the reset; else None)}. A straight line from the window's
    start: early in a window the projection swings, which the caller says."""
    now = now or time.time()
    for path in _recent_snaps():
        try:
            with open(path, encoding="utf-8") as f:
                rl = json.load(f).get("rate_limits") or {}
        except (OSError, ValueError, AttributeError):
            continue
        out = {}
        for k, span in WINDOWS.items():
            w = rl.get(k) or {}
            used, resets = w.get("used_percentage"), w.get("resets_at")
            if not isinstance(used, (int, float)) or isinstance(used, bool) or not isinstance(resets, (int, float)) \
                    or not 0 < resets - now <= span:
                continue  # no reading, a reset already past, or a millisecond epoch: no pace to tell
            gone = span - (resets - now)
            rate = used / gone if gone > 0 else 0  # percent per second so far
            full_at = now + (100 - used) / rate if rate and used < 100 else (now if used >= 100 else None)
            out[k] = {"used": used, "resets_at": resets, "projected": round(used + rate * (resets - now), 1),
                      "full_at": full_at if full_at is not None and full_at < resets else None}
        if out:
            return out
    return {}


def samples():
    """T-3902: the weekly usage samples, oldest first: {ts, used, resets} (the newest SAMPLE_KEEP; bad rows skipped)."""
    rows = []
    for e in c.tail_jsonl(_path("usage-samples.jsonl"), SAMPLE_KEEP):
        if isinstance(e, dict) and all(_num(e.get(k)) for k in ("ts", "used", "resets")):
            rows.append(e)
    return rows


def note_snapshot(rate_limits_obj, now=None):
    """T-3902: the statusline's snapshot adds this week's reading to the samples ring under the project's state: one row
    per SAMPLE_EVERY (a reading within that of the last row is not kept), the newest SAMPLE_KEEP kept. A reading of a
    week already reset, or with no reset time, is not a sample."""
    now = now or time.time()
    w = rate_limits_obj.get("seven_day") if isinstance(rate_limits_obj, dict) else None
    if not isinstance(w, dict):
        return
    used, resets = w.get("used_percentage"), w.get("resets_at")
    if not _num(used) or not _num(resets) or not 0 < resets - now <= WINDOWS["seven_day"]:
        return
    path = _path("usage-samples.jsonl")
    with _SAMPLE_LOCK:
        last = c.tail_jsonl(path, 1)
        prev = last[-1].get("ts") if last and isinstance(last[-1], dict) else None
        if _num(prev) and 0 <= now - prev < SAMPLE_EVERY:
            return
        os.makedirs(c.state_dir(), exist_ok=True)
        c.append_line(path, json.dumps({"ts": round(now, 1), "used": used, "resets": int(resets)}) + "\n")
        with open(path, "rb") as f:
            lines = f.read().splitlines()
        if len(lines) > SAMPLE_KEEP:  # the ring: the newest rows only
            c.write_atomic(path, b"\n".join(lines[-SAMPLE_KEEP:]).decode("utf-8") + "\n")


def weekly_rate(now=None):
    """T-3902: this week's burn rate over the last RATE_SPAN, in % per hour: the change between its oldest and newest
    samples in that span. None when there are fewer than two of them, or they are under 15 min apart (a 1% step over a
    shorter span can't tell a rate)."""
    now = now or time.time()
    rows = [e for e in samples() if e["ts"] >= now - RATE_SPAN]  # no upper bound: a row stamped a tick ahead (rounding) still counts
    if not rows:
        return None
    same = [e for e in rows if e["resets"] == rows[-1]["resets"]]
    span = same[-1]["ts"] - same[0]["ts"]
    if len(same) < 2 or span < 900:
        return None
    return max(0.0, (same[-1]["used"] - same[0]["used"]) * 3600 / span)


def pace(week, now=None):
    """T-3902: the weekly pace against the horizon, or None (no horizon, or no weekly reading). allowance: the % per hour
    left for the hours to the horizon; rate: the recent burn (weekly_rate); projected: the % at the horizon at that rate;
    hold: the rate is over PACE_HOLD times the allowance (an unknown rate never holds), or the week is full."""
    now = now or time.time()
    until = horizon(now)
    if until is None or not _num(week):
        return None
    hours = (until - now) / 3600
    allowance = max(0.0, 100 - week) / hours
    rate = weekly_rate(now)
    return {"used": week, "rate": None if rate is None else round(rate, 3), "allowance": round(allowance, 3),
            "projected": None if rate is None else round(week + rate * hours, 1), "until": until,
            "hold": week >= 100 or (rate is not None and rate > PACE_HOLD * allowance)}


def weekly_lean(p=None):
    """T-3049: whether weekly usage leans the project (p, else the one holding the cwd): False once the user turned its
    pace off (sampo drive on --pace off: "remove the weekly usage 80% lean on this project specifically"); the 5-hour
    window and the spend caps still hold. No project, or an unreadable one, leans as before."""
    try:
        p = p or c.find_project(os.getcwd())
        return not (p and c.read_meta(p).get("pace_drops") is False)
    except Exception:
        return True


def usage_high(p=None):
    """'weekly usage 85%' when the newest statusline snapshot is past 80% weekly or 90% of the 5-hour window, else
    None: the same thresholds as foreman-ui's economy mode (T-0198). The weekly rule skips a project that turned it off."""
    u = rate_limits()
    if u.get("seven_day", 0) >= 80 and weekly_lean(p):
        return f"weekly usage {u['seven_day']:g}%"
    if u.get("five_hour", 0) >= 90:
        return f"5-hour usage {u['five_hour']:g}%"
    return None


def subagent_pause(p=None):
    """Why subagents should wait, or None (T-0320): the 5-hour window at 90%+, or weekly usage ahead of the week's
    pace — more than 10 points past the share of the week gone by, never before 75% (the user, T-0364: "the budget
    stuff shouldn't matter until 75% usage") and never past 90%. Unknown usage never pauses. T-3902: with a horizon
    (budget set --until) the weekly part is the recent rate against the allowance to it instead: it holds while the
    rate is over PACE_HOLD times the allowance, and releases when it falls under, with no 75% floor."""
    u = rate_limits()
    if u.get("five_hour", 0) >= 90:
        return f"5-hour usage {u['five_hour']:g}%"
    week, gone = u.get("seven_day"), u.get("week_gone")
    if horizon() is not None:
        pc = pace(week)
        if pc and pc["hold"] and weekly_lean(p):
            return f"weekly usage {week:g}%, {pc['rate'] or 0:g}%/h recent rate over the {pc['allowance']:g}%/h allowance"
        return None
    if week is not None and week >= max(PACE_FLOOR, min(90, 100 * gone + 10 if gone is not None else 80)) \
            and weekly_lean(p):  # the project lookup only once past the bar
        return f"weekly usage {week:g}%" + (f" with {100 * gone:.0f}% of the week gone" if gone is not None else "")
    return None


def degrade(p=None):
    """T-0449: why optional work (the drive's side tasks, the friction pass, brainstorm deepening) is dropped, or None:
    the pace subagents wait on, or (T-1080, given a project) its spent risk budget. Never a cap: check() still refuses
    what's over one, and required gates still run."""
    pace = None if p is not None and c.read_meta(p).get("pace_drops") is False else subagent_pause(p)  # T-2327
    if pace or p is None:
        return pace
    from sampo import risk
    return risk.slowed(p)


def effective(p=None):
    """(caps after economy, why they were halved or None)."""
    cur, high = caps(), usage_high(p)
    if high:
        cur = {k: v / 2 for k, v in cur.items()}
    return cur, high


def check(feature, est, smaller=""):
    """Raise BudgetError when a command estimated at `est` USD would pass the per-command cap or today's."""
    cur, high = effective()
    usd, _ = spent()
    why = f" (halved: {high})" if high else ""
    hint = f"; {smaller}" if smaller else ""
    if est > cur["run"]:
        raise BudgetError(f"budget: {feature} would spend about ${est:.2f}, over the ${cur['run']:.2f} per command "
                          f"cap{why}{hint}; or raise it: fm budget set --run N --because \"<why>\"")
    if RUN["usd"] + est > cur["run"]:  # T-2871: many small calls, each under the cap alone
        raise BudgetError(f"budget: {feature} would spend about ${est:.2f} more, after ${RUN['usd']:.2f} already this "
                          f"command, over the ${cur['run']:.2f} per command cap{why}{hint}; or raise it: fm budget set "
                          f"--run N --because \"<why>\"")
    if usd + est > cur["day"]:
        raise BudgetError(f"budget: {feature} would spend about ${est:.2f}; today ${usd:.2f} of ${cur['day']:.2f} is "
                          f"spent{why}{hint}; or raise it: fm budget set --day N --because \"<why>\"")


def check_subagent():
    why = subagent_pause()
    if why:
        raise BudgetError(f"budget: subagents wait while usage is ahead of pace ({why}); do it in the main thread")


def result(stdout):
    """(text, usd) from `claude -p --output-format json`; plain text (an older claude, a stub) gives (text, None)."""
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "result" in data:
            return str(data.get("result") or ""), data.get("total_cost_usd")
    return stdout or "", None


def transcript_tokens(path):
    """Input-equivalent tokens in a subagent transcript, each message counted once."""
    seen = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") if isinstance(e, dict) else None
                if isinstance(m, dict) and isinstance(m.get("usage"), dict):
                    seen[m.get("id") or len(seen)] = m["usage"]
    except OSError:
        return 0
    from sampo import cost
    return int(sum(cost.weighted(u) for u in seen.values()))


def cmd_budget(args):
    from sampo import cli
    p = cli.resolve(args)
    if args.action == "set":
        if getattr(args, "until", None) is not None:  # T-3902: the horizon (not a cap: no decision to record)
            set_horizon(parse_until(args.until))
            if args.day is None and args.run is None:
                return cli.out(args, {"until": horizon()}, "horizon: " + _horizon_text())
        cur = caps()
        new = {"day": args.day, "run": args.run}
        bad = [k for k, v in new.items() if v is not None and not (math.isfinite(v) and v >= 0)]
        if bad:
            raise cli.UsageError(f"caps are finite numbers ≥ 0: {', '.join(bad)}")
        up = [k for k, v in new.items() if v is not None and v > cur[k]]
        if up and not args.because:
            raise cli.UsageError("raising a cap needs --because \"<why>\" (it is recorded as a costly decision)")
        res = set_caps(**new)
        if up:  # not blocked: recorded for the user's review of costly decisions
            import argparse
            cli.cmd_decide(argparse.Namespace(
                project=getattr(args, "project", None), json=False, list=False, review=False, n=30, task=None,
                decision=f"budget raised: {', '.join(f'{k} {cur[k]:g} → {res[k]:g}' for k in up)}", why=args.because,
                rejected="", kind="costly", reverses=None))
        return cli.out(args, res, "caps: " + ", ".join(f"{k} {v:g}" for k, v in res.items()))
    rows = ledger(args.days)
    today = [e for e in rows if e.get("day") == _today()]
    usd, tokens = spent(today)
    cur, high = effective(p)
    by = {}
    for e in rows:
        f = by.setdefault(e.get("feature"), {"usd": 0.0, "runs": 0, "tokens": 0})
        f["usd"] += e.get("usd") or 0
        f["runs"] += e.get("runs") or 0
        f["tokens"] += e.get("tokens") or 0
    pause = subagent_pause(p)
    week = rate_limits().get("seven_day")
    pc = pace(week)  # T-3902: the weekly pace against the horizon, when one is set
    lines = [f"today: ${usd:.2f} of ${cur['day']:.2f} · per command ≤ ${cur['run']:.2f}" + (f" (halved: {high})" if high else ""),
             f"subagents: {tokens:,} tokens today · " + (f"waiting: {pause}" if pause else "running (usage on pace)"),
             f"weekly: {_pace_text(week, pc)}",
             f"horizon: {_horizon_text()}"]
    lines += [f"  {k}: ${v['usd']:.2f} over {v['runs']} run(s)" + (f", {v['tokens']:,} tokens" if v["tokens"] else "")
              for k, v in sorted(by.items(), key=lambda kv: -kv[1]["usd"] - kv[1]["tokens"] / 1e6)]
    cli.out(args, {"today_usd": usd, "today_subagent_tokens": tokens, "caps": cur, "halved": high,
                     "subagents_paused": pause, "by_feature": by, "week": week, "pace": pc,
                     "horizon": horizon(), "samples": len(samples())},
              "\n".join(lines))


def _when(at):
    return datetime.datetime.fromtimestamp(at, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _horizon_text():
    """The horizon as the user set it (T-3902): the time, 'the weekly reset' (its time as the snapshot has it), or none."""
    if _raw().get("until") == "reset":
        at = horizon()
        return f"the weekly reset ({_when(at)})" if at else "the weekly reset (no reading yet)"
    at = horizon()
    return _when(at) if at else ("none" if _raw().get("until") is None else "past, the 75% floor applies")


def _pace_text(week, pc):
    """T-3902: used %, the last 3 h's rate, the allowance to the horizon and the projection there, and the state."""
    if week is None:
        return "no reading yet (a statusline snapshot supplies it)"
    if pc is None:
        return f"{week:g}% used · no horizon, so the 75% floor applies (sampo budget set --until TIME)"
    rate = "unknown (under two samples 15 min apart)" if pc["rate"] is None else f"{pc['rate']:g}%/h"
    proj = "" if pc["projected"] is None else f" · projected {pc['projected']:g}% at {_when(pc['until'])}"
    return (f"{week:g}% used · last 3 h {rate} · allowance {pc['allowance']:g}%/h to {_when(pc['until'])}{proj} · "
            + ("hold: optional work waits" if pc["hold"] else "release: optional work runs"))
