"""What became of finished work (T-0616): reverted in git, fixed later by a FIX task that names it, or held; and the
track record that follows (T-0641): per type and tier, how many closed on their first fm task finish and how many held.
Everything is derived on demand from git, the briefs and the ledger, so there is nothing extra to keep in sync."""
import bisect
import collections
import json
import os
import re
import statistics
import subprocess
import sys
import time

from sampo import core as c

_TID = re.compile(r"\bT-\d{4,}\b")
SMOKE_DAYS = 7  # a failing smoke run this soon after a close points at that task (weakly: smoke covers the whole UI)
SURVIVE_DAYS = 30  # T-1060: a close survives when nothing undid it this long after
_VERSION = re.compile(r"\b\d+\.\d+\.\d+\b")  # a release task names what it ships


def reverts(p, n=200):
    """[(date, revert subject, the reverted commit's subject, {task ids named})] from git's Revert commits, newest
    first; [] outside git. T-3692: kept in cache/reverts.json per checkout root and n while git's refs stand still
    (core.git_stamp, as T-3580 keeps ref answers): a focus asked three times, each a git process."""
    stamp, path, key, kept = c.git_stamp(p.root), os.path.join(p.dir, "cache", "reverts.json"), f"{p.root}\0{n}", {}
    if stamp:
        try:
            with open(path, encoding="utf-8") as f:
                got = json.load(f)
            if got["stamp"] == stamp:
                kept = dict(got["answers"])
                if key in kept:
                    return [(at, subj, orig, set(tids)) for at, subj, orig, tids in kept[key]]
        except (OSError, ValueError, LookupError, TypeError):  # a bad kept file is asked again
            kept = {}
    try:
        r = subprocess.run(["git", "-C", p.root, "log", f"-n{n}", "--grep=^Revert ", "--format=%cI%x1f%s%x1f%b%x1e"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for rec in r.stdout.split("\x1e"):
        parts = rec.strip().split("\x1f")
        if len(parts) == 3:
            orig = (re.match(r'Revert "(.*)"$', parts[1]) or [None, parts[1]])[1]
            rows.append((parts[0], parts[1], orig, set(_TID.findall(parts[1] + " " + parts[2]))))
    if stamp and r.returncode == 0:  # stamped before the log: refs that moved during it are asked again next time
        kept[key] = [(at, subj, orig, sorted(tids)) for at, subj, orig, tids in rows]
        try:
            c.write_atomic(path, json.dumps({"stamp": stamp, "answers": kept}))
        except OSError:
            pass
    return rows


def done_at(b):
    """When the brief was last closed done (its log), else its updated stamp."""
    hits = re.findall(r"(?m)^- (\S+) done\b", b.section("Log"))
    return hits[-1] if hits else str(b.meta.get("updated") or "")


def outcomes(p):
    """{task id: {fate, by, type, tier}} for every done task: "reverted" (a Revert commit names it), "fixed later" (a
    FIX task created after it closed names it), "corrected" (a user correction logged against it after it closed),
    else "held"."""
    briefs = c.load_briefs(p, include_archive=True)
    closed = {b.id: done_at(b) for b in briefs if b.status == "done" and not b.meta.get("done_in")}  # T-0852: a
    # task folded into another has no close of its own: its host's record is the one that counts
    reverted, when = collections.defaultdict(list), collections.defaultdict(list)  # when: T-1060, each bad event's time
    for at, subj, _, tasks in reverts(p):
        for t in tasks:
            reverted[t].append(subj)
            when[t].append((_ts(at), "reverted"))
    later = collections.defaultdict(list)
    for f in briefs:
        # ponytail: a FIX naming exactly one finished task is a follow-up fix; releases and batches name several
        # (or a version) and are skipped. Upgrade path: overlap of the two tasks' touched files.
        if f.type != "FIX" or f.status == "dropped" or _VERSION.search(f.title):
            continue
        named = (set(_TID.findall(" ".join([f.title, f.section("Raw request")]))) - {f.id}) & closed.keys()
        if len(named) == 1 and str(f.meta.get("created") or "") >= closed[next(iter(named))]:
            later[next(iter(named))].append(f.id)
            when[next(iter(named))].append((_ts(f.meta.get("created")), f"fixed by {f.id}"))
    corrected = collections.defaultdict(list)  # T-0602: the user corrected it after it closed
    events = c.ledger_events(p, ("correction", "smoke"))  # T-1060 review: the archived months too
    for e in events:
        t = e.get("task")
        if e.get("event") == "correction" and t in closed and str(e.get("ts") or "") > closed[t]:
            corrected[t].append(c.fit(str((e.get("data") or {}).get("text") or ""), 100))
            when[t].append((_ts(e.get("ts")), "corrected"))
    smoked = collections.defaultdict(list)  # T-0743: a smoke run that found defects within a week of the close
    for e in events:
        if e.get("event") == "smoke" and (e.get("data") or {}).get("defects"):
            at = c.parse_ts(e.get("ts"))
            for t, ts in closed.items():
                done = c.parse_ts(ts)
                if at and done and 0 <= (at - done).total_seconds() <= SMOKE_DAYS * 86400:
                    smoked[t].append(f"smoke {str(e.get('ts'))[:10]}: {e['data']['defects']} defect(s)")
                    # no survival loss: smoke covers the whole UI, too weak a link to grade a task lost for good
    fate = lambda i: ("reverted" if i in reverted else "fixed later" if i in later else
                      "corrected" if i in corrected else "smoke failed after" if i in smoked else "held")
    return {b.id: {"fate": fate(b.id), "by": reverted.get(b.id) or sorted(later.get(b.id, [])) or corrected.get(b.id, [])
                   or smoked.get(b.id, []),
                   "type": b.type, "tier": b.tier, **_survival(closed[b.id], when.get(b.id, []))}
            for b in briefs if b.id in closed}


def _ts(s):
    """A time from a ledger/brief stamp (UTC Z) or git's %cI (with an offset); None when unreadable."""
    t = c.parse_ts(s) if s else None
    if t is None and s:
        import datetime
        try:
            t = datetime.datetime.fromisoformat(str(s)).astimezone(datetime.timezone.utc)
        except ValueError:
            t = None
    return t


def _survival(closed_at, bad):
    """T-1060 (wild C1, 'grade closed work by whether it survived'): survived (nothing undid it within SURVIVE_DAYS of
    the close), lost (the first thing that did, and when), or settling (days left in the window)."""
    import datetime
    t0 = _ts(closed_at)
    if not t0:
        return {"survival": "unknown"}
    end = t0 + datetime.timedelta(days=SURVIVE_DAYS)
    now = c.clock()
    hits = sorted((at, why) for at, why in bad if at and t0 <= at <= min(end, now))  # a future-dated commit waits
    left = (end - now).total_seconds() / 86400  # T-2584: only a finished window decides held or lost for good
    if hits:
        return {"survival": "lost", "lost_at": c.iso(hits[0][0].timestamp()), "lost_by": hits[0][1], "window_done": left <= 0,
                **({"days_left": int(left) + 1} if left > 0 else {})}  # T-2610: the interim reads an early loss's age
    return ({"survival": "survived", "window_done": True} if left <= 0
            else {"survival": "settling", "days_left": int(left) + 1, "window_done": False})


def survival_lines(p, outs=None):
    """One line: how the closes graded, and the days since the last lost one (an accident-free counter)."""
    import datetime
    outs = outcomes(p) if outs is None else outs
    n = collections.Counter(o.get("survival") for o in outs.values())
    if not outs:
        return []
    lost = [_ts(o["lost_at"]) for o in outs.values() if o.get("lost_at")]
    since = (f"days since a lost task: {max(0, (c.clock() - max(lost)).days)}" if lost
             else "no lost task yet")
    total = n['survived'] + n['lost'] + n['settling']  # T-2037: a loss can land mid-window, survival only after it
    past = [o for o in outs.values() if o.get("window_done")]  # T-2584: the rate counts only finished windows
    lost_past = sum(o.get("survival") == "lost" for o in past)
    return [f"Survival ({SURVIVE_DAYS} days): of {total} closes, {len(past)} finished the window: {lost_past} lost "
            f"({round(100 * lost_past / max(len(past), 1))}%), {n['survived']} survived; {total - len(past)} still in it "
            f"({n['lost'] - lost_past} lost so far); {since}"]


def track(p, outs=None):
    """{(type, tier): Counter(n, first, held)}: first = its first fm task finish ran no failing check."""
    outs = outcomes(p) if outs is None else outs
    first = _first_finish(p)
    rows = collections.defaultdict(collections.Counter)
    for tid, o in outs.items():
        r = rows[(o["type"], o["tier"])]
        r["n"] += 1
        r["first"] += first.get(tid, True)
        done = bool(o.get("window_done"))  # T-2610: a miss lands mid-window, a hold only after it: both wait for the end
        r["settled"] += done
        r["held"] += o["fate"] == "held" and done
    return rows


def _row(r):
    left = r["n"] - r["settled"]
    return (f"{r['first']} of {r['n']} closed on the first finish, {r['held']} of {r['settled']} held"
            + (f", {left} still in the {SURVIVE_DAYS}-day window" if left else ""))


START_MIN, START_RATE = 5, 30  # T-2739: settled closes, and the share of them undone later, that move fm code's start


def harness_record(p, harness, typ, tier, outs=None):
    """T-2739: (settled closes, those fixed later or reverted) of this type and tier that `harness` made, by the
    harness each task_done event recorded (the last close per task); a close with none recorded belongs to no one."""
    outs = outcomes(p) if outs is None else outs
    made = {e.get("task"): (e.get("data") or {}).get("harness") for e in c.ledger_events(p, ("task_done",))}
    rows = [o for t, o in outs.items() if made.get(t) == harness and (o["type"], o["tier"]) == (typ, tier)
            and o.get("window_done")]
    return len(rows), sum(o["fate"] in ("fixed later", "reverted") for o in rows)


def track_line(p, typ, tier):
    """One line for fm focus: this project's record for the type and tier, once it has two finished tasks."""
    r = track(p).get((typ, tier))
    return f"Track record for {typ} {tier} here: {_row(r)} (no later fix or revert)." if r and r["n"] >= 2 else ""


def track_lines(p, outs=None):
    rows = sorted(track(p, outs).items(), key=lambda kv: -kv[1]["n"])
    return ["Track record (all time; held = no later fix or revert): " + " · ".join(
        f"{t} {tier}: {_row(r)}" for (t, tier), r in rows[:6])] if rows else []


def dropped_class_lines(p, outs):
    """T-2648: done tasks but not one task_done event in the ledger (a class dropped or rolled away): every close rate
    built from those events is unmeasured, and a bare zero would read as a measurement."""
    if outs and not c.ledger_events(p, ("task_done",)):
        return [f"{len(outs)} done task(s) but no task_done events in the ledger: the class is missing, so close "
                "times, calibration and rates built from it are not measured (fm verify-ledger)"]
    return []


def cmd_outcomes(args):
    from sampo import cli
    if getattr(args, "judgment", False):  # T-2461: fm calibration folded in here
        return cmd_calibration(args)
    p = cli.resolve(args)
    if getattr(args, "silent", False) or getattr(args, "census", False) or getattr(args, "backfill", False):  # T-3934
        return cmd_probes(args, p)
    if getattr(args, "predictions", False) or getattr(args, "unmute", None):  # T-2406: the prediction book
        from sampo import foresight
        if getattr(args, "ask_local", False):  # T-2912: the night job books the local model's guess first
            foresight.local_tick(p)
        if getattr(args, "unmute", None):
            kinds = [k["kind"] for k in foresight.prediction_kinds(p)]
            if args.unmute not in kinds:
                raise cli.UsageError(f"--unmute takes a prediction kind: {', '.join(kinds)}")
            with c.lock(p.dir):
                meta = c.read_meta(p)
                meta["predictions_unmuted"] = dict(meta.get("predictions_unmuted") or {}, **{args.unmute: c.now()})
                c.write_meta(p, meta)
        rows = foresight.prediction_kinds(p)
        pct = lambda x: "n/a" if x is None else f"{x:.0%}"  # noqa: E731
        return cli.out(args, {"kinds": rows}, "\n".join(
            f"{k['kind']}: {k['n']} scored, hit {pct(k['hit'])} vs baseline {pct(k['base_hit'])}"
            + f" ({foresight.lead_text(k['lead'])})"
            + (f"; shown at session start: {k['shown_n']} scored, hit {pct(k['shown_hit'])}" if k.get("shown_n") else "")
            + (f" (muted: it trails the baseline; fm outcomes --predictions --unmute {k['kind']})" if k["muted"] else "")
            for k in rows))
    if getattr(args, "questions", False):  # T-2840: AskUserQuestion calls since TS; --none fails when any was asked
        since = getattr(args, "since", None) or ""
        rows = [e.get("data") or {} for e in c.ledger_events(p, ("question_answered",), since=since)
                if str(e.get("ts") or "") >= since]
        n = {k: sum(int(r.get(k) or 0) for r in rows) for k in ("asked", "default", "other", "typed")}
        n.update(calls=len(rows), dismissed=sum(bool(r.get("dismissed")) for r in rows))
        n["prompts"] = sum(str(e.get("ts") or "") >= since for e in c.ledger_events(  # T-2857: an fm ask's yes or no
            p, ("approval_granted", "approval_declined"), since=since))
        cli.out(args, n, f"{n['calls']} AskUserQuestion call(s){' since ' + since if since else ''}: {n['asked']} "
                         f"question(s), {n['default']} took the default, {n['other']} another option, {n['typed']} "
                         f"typed, {n['dismissed']} dismissed; {n['prompts']} permission prompt(s) answered")
        return 1 if getattr(args, "none", False) and (n["calls"] or n["prompts"]) else 0
    outs = outcomes(p)
    if getattr(args, "readme", False):  # T-0960: the one sentence a README publishes
        held = sum(o["fate"] == "held" for o in outs.values())
        ungraded = sum(1 for b in c.load_briefs(p, include_archive=True) if b.id in outs and not b.meta.get("verified"))
        return cli.out(args, {"held": held, "closed": len(outs), "ungraded": ungraded},
                         f"{held} of {len(outs)} closed tasks held; {ungraded} ungraded.")
    bad = {t: o for t, o in outs.items() if o["fate"] != "held"}
    fates = collections.Counter(o["fate"] for o in bad.values())
    lines = [f"{len(outs)} finished task(s): {len(outs) - len(bad)} held"
             + "".join(f", {n} {k}" for k, n in fates.most_common())]
    lines += [f"- {t} {o['fate']}: {c.fit(c.joinmore(o['by'], 3), 140)}" for t, o in sorted(bad.items())[-20:]]
    if getattr(args, "atlas", False):  # T-0620
        return cli.out(args, atlas(p, outs), "\n".join(atlas_lines(p, outs)))
    if getattr(args, "calibration", False):  # T-2290
        return cli.out(args, {t: {"ratio": r, "n": n} for t, (r, n) in calibration_rows(p).items()},
                         "\n".join(calibration_report(p)))
    for flag in ("settle", "claims", "regret", "graveyard", "seen"):  # T-1060: reports that read the 30-day grades
        if getattr(args, flag, False):
            res = globals()["_" + flag](p, outs)
            return cli.out(args, res, "\n".join(res["lines"]))
    lines += dropped_class_lines(p, outs) + survival_lines(p, outs) + track_lines(p) + ledger_lines(p, outs)
    rows = track(p, outs)
    return cli.out(args, {"tasks": outs, "track": {f"{a} {b}": dict(r) for (a, b), r in rows.items()}},
                     "\n".join(lines))


def _settle(p, outs):
    """One `outcome` ledger event per task whose window has closed (survived or lost), once per task."""
    done = {e.get("task") for e in c.ledger_events(p, ("outcome",))}
    new = [(t, o) for t, o in sorted(outs.items()) if o.get("survival") in ("survived", "lost") and t not in done]
    for t, o in new:
        c.log_event(p, "outcome", task=t, data={k: o[k] for k in ("survival", "lost_at", "lost_by", "fate") if k in o})
    return {"graded": [t for t, _ in new], "lines": [f"{len(new)} graded"]
            + [f"- {t} {o['survival']}" + (f": {o['lost_by']}" if o.get("lost_by") else "") for t, o in new[-20:]]}


def _graded(p, outs):
    return {b.id: b for b in c.load_briefs(p, include_archive=True) if b.id in outs
            and outs[b.id].get("survival") in ("survived", "lost")}


def _claims(p, outs):
    """Did a strong close survive more often than a weak one? Survival by the grade the finish recorded."""
    rows = collections.defaultdict(lambda: [0, 0])
    for t, b in _graded(p, outs).items():
        r = rows[str(b.meta.get("verified") or "ungraded")]
        r[0] += 1
        r[1] += outs[t]["survival"] == "survived"
    return {"by_grade": {k: {"graded": n, "survived": ok} for k, (n, ok) in rows.items()},
            "lines": ["Survival by close grade:"] + ([f"- {k}: {ok} of {n} survived" for k, (n, ok) in sorted(rows.items())]
                                                     or [f"- nothing graded yet: a close is graded {SURVIVE_DAYS} days on"])}


def _regret(p, outs):
    """Lost tasks with the approach they took, and the decisions a later one reversed."""
    from sampo import cost
    lost = [(t, b) for t, b in _graded(p, outs).items() if outs[t]["survival"] == "lost"]
    lines = [f"Lost within {SURVIVE_DAYS} days ({len(lost)}):"] + [
        f"- {t} {c.fit(c.plain(b.title), 60)}: {outs[t].get('lost_by')}; approach: "
        f"{c.fit(c.plain(' '.join(b.section('Approach').split())) or '(none written)', 120)}" for t, b in lost[-15:]]
    rev = cost.reversals(p)
    lines += ["Decisions reversed, by kind: " + " · ".join(f"{k}: {r} of {n}" for k, (n, r) in rev.items())] \
        if any(r for _, r in rev.values()) else []
    return {"lost": [t for t, _ in lost], "lines": lines}


def _graveyard(p, outs):
    """What didn't make it: lost closes and dropped tasks."""
    lost = [t for t, o in outs.items() if o.get("survival") == "lost"]
    briefs = c.load_briefs(p, include_archive=True)
    dropped = [b for b in briefs if b.status == "dropped"]
    by = {b.id: b for b in briefs if b.id in lost}
    lines = [f"Lost ({len(lost)}):"] + [f"- {t} {c.fit(c.plain(by[t].title), 60)}: {outs[t].get('lost_by')}"
                                        for t in sorted(lost)[-15:] if t in by]
    lines += [f"Dropped ({len(dropped)}):"] + [f"- {b.id} {c.fit(c.plain(b.title), 60)}" for b in dropped[-15:]]
    return {"lost": sorted(lost), "dropped": [b.id for b in dropped], "lines": lines}


def _seen(p, outs):
    """Did a correction during the task (the user steering it) make it more or less likely to survive?"""
    graded = _graded(p, outs)
    during = {e.get("task") for e in c.ledger_events(p, ("correction",)) if e.get("task") in graded
              and str(e.get("ts") or "") <= str(done_at(graded[e["task"]]) or "")}
    rows = {k: [t for t in graded if (t in during) == k] for k in (True, False)}
    lines = [f"- {'corrected during the task' if k else 'not corrected'}: "
             f"{sum(outs[t]['survival'] == 'survived' for t in ts)} of {len(ts)} survived" for k, ts in rows.items()]
    return {"corrected": sorted(rows[True]), "lines": ["Survival by whether the user corrected the task:"] + lines}


BUCKETS = ((0, 59), (60, 79), (80, 94), (95, 100))


# --- T-2337 (Mission 5, #8): the calibration ledger. Every stated confidence is a write-once bet row in the project's
# calibration.jsonl; an outcome is a second row (never an edit); the signals that score a row are ones Foreman didn't
# author (a revert, a later fix, a user correction, a race pick, an assumption's own check, a reversed decision).
LEDGER_MIN = 20  # scored rows before a Brier, a bar move or a route is believed
BAR_DEFAULTS = {"research": 70, "race": 55}  # the confidence a plan or a race side needs before it skips research/the race
BAR_MAX, BAR_STEP, BAR_GAP = 90, 10, 15  # a bar rises at most BAR_STEP, never past BAR_MAX, on a gap of BAR_GAP points
KINDS = ("task", "choice", "assumption", "decision", "race", "ship")  # T-2519: ship shows in the tables; bar_rows never reads it
INTERIM_DAYS = 7  # T-2498: a task bet closed this long ago reads "held so far" (never scored) while survival settles


def _ledger_path(p):
    return os.path.join(p.dir, "calibration.jsonl")


def ledger(p):
    """([bet rows], {bet id: its outcome row}) from calibration.jsonl; a line that isn't JSON is skipped."""
    bets, grades = [], {}
    try:
        with open(_ledger_path(p), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and "kind" in r and "stated" in r:
                    bets.append(r)
                elif isinstance(r, dict) and r.get("of") and r.get("outcome") in ("held", "missed"):
                    grades.setdefault(r["of"], r)  # write-once: the first grade stands
    except OSError:
        pass
    return bets, grades


def _tree(p):
    try:
        r = subprocess.run(["git", "-C", p.root, "rev-parse", "-q", "--verify", "HEAD^{tree}"], capture_output=True,
                           text=True, timeout=5)
        return r.stdout.strip()[:40] or None
    except (OSError, subprocess.SubprocessError):
        return None


def _stated(v):
    try:
        n = int(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        raise ValueError(f"a stated confidence is a whole percent, 0-100: got {v!r}")
    if not 0 <= n <= 100:
        raise ValueError(f"a stated confidence is a whole percent, 0-100: got {v!r}")
    return n


def bet(p, kind, stated, task=None, key="", files=(), tree=None, tier=None, typ=None, base=None):
    """Append one write-once bet row (kind, stated, base rate, task, files, tree); the row for the same task, kind and
    key is never written twice and never edited, so a restated number can't rewrite the record. None if it existed."""
    stated, key = _stated(stated), c.fit(str(key), 80)
    b = c.find_brief(p, task) if task else None
    row = {"ts": c.now(), "id": "", "kind": kind, "stated": stated, "base": base, "task": task, "key": key,
           "files": [str(f) for f in files][:20], "tree": tree or _tree(p), "tier": tier or (b.tier if b else None),
           "type": typ or (b.type if b else None), "harness": c.harness()}  # T-2500
    with c.lock(p.dir):
        if any(x.get("task") == task and x.get("kind") == kind and x.get("key") == key for x in ledger(p)[0]):
            return None
        row["id"] = f"{len(ledger(p)[0]) + 1:05d}-{kind}"
        with open(_ledger_path(p), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def ship_bet(p, chance, task, commit):
    """T-2496: the stated chance a pushed commit's blocking CI jobs pass: one write-once row per commit (None when one
    exists). Its base is the held share of earlier settled ship bets, None before the first. fm ci-watch's verdict
    settles it (src ci): scored as its own kind, never moving a bar (bar_rows takes survival only)."""
    stated = _stated(chance)
    done = [r["outcome"] for r in scored(p) if r["kind"] == "ship" and r.get("outcome")]
    return bet(p, "ship", stated, task=task, key=str(commit)[:12],
               base=round(100 * done.count("held") / len(done)) if done else None)


SHIP_GAP = 15  # T-2611: points between a ship's stated chance and its record before the record is named


def ship_note(p, chance):
    """T-2611 (Mission 2, #8): one line when a ship's stated chance sits SHIP_GAP+ points from the held share of
    LEDGER_MIN+ settled ship bets, said where the number is typed (fm ship); '' otherwise. A note: never raises."""
    try:
        stated = _stated(chance)
        done = [r["outcome"] for r in scored(p) if r["kind"] == "ship" and r.get("outcome")]
    except Exception:
        return ""
    if len(done) < LEDGER_MIN:
        return ""
    held = done.count("held")
    pct = round(100 * held / len(done))
    if abs(stated - pct) < SHIP_GAP:
        return ""
    return (f"Ship record: {held} of {len(done)} ship bets held ({pct}%); {stated}% sits {abs(stated - pct)} points "
            f"{'under' if stated < pct else 'over'} it. State what you believe; fm ci-watch scores it.")


def base_note(p, b):
    """T-2497 (Mission 2, #8): one line when the brief's Choice N (NN%) sits 20+ points from the base rate of its type
    and tier (either way), naming both numbers and whose closes set the base; '' otherwise or with no history."""
    try:
        pct = c.stated_confidence(b.section("Approach (options → choice → why)"))
        if pct is None:
            return ""
        outs = outcomes(p)
        base = base_rate(outs, _first_finish(p), b.type, b.tier)
    except Exception:  # a note: it never stops a plan or a focus
        return ""
    if base is None or abs(pct - base) < 20:
        return ""
    pair = sum(1 for o in outs.values() if o.get("type") == b.type and o.get("tier") == b.tier)
    whose = f"{b.type} {b.tier} closes ({pair})" if pair >= LEDGER_MIN else f"all closes (only {pair} {b.type} {b.tier})"
    way, gap = ("under", base - pct) if pct < base else ("over", pct - base)
    return (f"Calibration: this plan states Choice {pct}% where {whose} held on the first finish {base}% of the time: "
            f"{gap} points {way} the record. State what you believe; the ledger scores it (fm outcomes --judgment).")


MISSES = ("reverted", "fixed later")  # T-2423: the scorer's survival misses; a correction or a smoke failure isn't one


def base_rate(outs, first, typ, tier):
    """The forecast with no stated number: the share of earlier closes of this type and tier that held on the first
    finish (a clean first fm task finish, then nothing undid it); every close counts while the pair has under
    LEDGER_MIN. None with no history."""
    outs = {t: o for t, o in outs.items() if o.get("window_done") is not False}  # T-2610: an open window isn't a hold yet
    pair = [t for t, o in outs.items() if o.get("type") == typ and o.get("tier") == tier]
    pool = pair if len(pair) >= LEDGER_MIN else list(outs)
    if not pool:
        return None
    return round(100 * sum(first.get(t, True) and outs[t]["fate"] not in MISSES for t in pool) / len(pool))


def record_stated(p, b):
    """The numbers a brief states: its task confidence, the choice's (Approach: 'Choice N: why (80%)') and each
    assumption's ('(70%)'). Each becomes a bet (write-once per key); called at first focus and when a confidence is
    stated after work began. Never raises: a ledger doesn't stop a focus. A number stated once the task closed or
    finished once is hindsight: never recorded. The cheap parse comes first; the outcomes, the first-finish scan, the
    tree and the base rate are only computed when a number is new."""
    try:
        want = []  # (kind, key, percent)
        if str(b.meta.get("confidence") or "").isdecimal():
            want.append(("task", "confidence", b.meta["confidence"]))
        pct = c.stated_confidence(b.section("Approach (options → choice → why)"))  # T-2338: the gate's own parser
        if pct is not None:
            want.append(("choice", "choice", pct))
        for n, _tag, _how, text in b.assumptions():
            pct = c.assumption_confidence(text, low=False)
            if pct is not None:
                want.append(("assumption", f"A{n}", pct))
        if not want or b.status in ("done", "dropped"):
            return
        have = {(x.get("kind"), x.get("key")) for x in ledger(p)[0] if x.get("task") == b.id}  # the one ledger read
        want = [w for w in want if (w[0], w[1]) not in have]
        if not want:
            return
        first = _first_finish(p)
        if b.id in first:  # it has finished once: a new number now is a bet placed with the answer in hand
            return
        base = base_rate(outcomes(p), first, b.type, b.tier)
        scope = list(b.meta.get("plan0_scope") or b.meta.get("scope") or [])
        kw = dict(task=b.id, files=scope, tree=_tree(p), tier=b.tier, typ=b.type, base=base)
        for kind, key, pct in want:
            bet(p, kind, pct, key=key, **kw)
    except Exception:
        pass


def _verdicts(p, bets, outs, first):
    """{bet id: (outcome, by, src)}: held, missed, or None while the signal hasn't spoken. src names the signal: only
    'survival' (fm outcomes' re-fix or revert, or a quiet window) is one Sampo didn't author; a failed first finish,
    an assumption mark, a reversed decision and a race pick are its own, and never move a bar."""
    import datetime
    briefs = {b.id: b for b in c.load_briefs(p, include_archive=True)}
    reversed_ = [(str(e.get("ts") or ""), str((e.get("data") or {}).get("reverses") or "").lower(),
                  c.fit(str((e.get("data") or {}).get("decision") or "").lower(), 80))
                 for e in c.ledger_events(p, ("decision",)) if (e.get("data") or {}).get("reverses")]
    picks = [e.get("data") or {} for e in c.ledger_events(p, ("race_pick",))]
    now = c.clock()
    ci = {}
    if any(r["kind"] == "ship" for r in bets):  # T-2496: the first finished run on a commit is what was bet on
        for e in c.ledger_events(p, ("ci_verdict",)):
            d = e.get("data") or {}
            ci.setdefault(str(d.get("commit") or "")[:12], bool(d.get("green")))
    got = {}
    for r in bets:
        k, t, v = r["kind"], r.get("task"), (None, "", "")
        if k in ("task", "choice"):
            o = outs.get(t) or {}
            if first.get(t) is False:
                v = ("missed", "its first finish failed a check", "first_finish")
            elif o.get("survival") == "lost":
                v = ("missed", c.fit(f"{o.get('fate')}: {', '.join(map(str, o.get('by') or []))}", 100), "survival")
            elif o.get("survival") == "survived":
                v = ("held", f"survived {SURVIVE_DAYS} days", "survival")
        elif k == "assumption" and t in briefs:
            tag = next((tg for n, tg, _, _ in briefs[t].assumptions() if f"A{n}" == r["key"]), None)
            v = {"verified": ("held", "marked verified", "mark"),
                 "false": ("missed", "marked false", "mark")}.get(tag, (None, "", ""))
        elif k == "decision":
            at = c.parse_ts(r.get("ts"))
            if any(ts >= str(r.get("ts")) and words and words in r["key"] and own != r["key"]
                   for ts, words, own in reversed_):  # its own --reverses words aren't a later reversal of it
                v = ("missed", "reversed by a later decision", "decision")
            elif at and (now - at).days >= SURVIVE_DAYS:
                v = ("held", f"not reversed in {SURVIVE_DAYS} days", "decision")
        elif k == "ship" and r.get("key") in ci:  # T-2496: GitHub's run on hosts Foreman doesn't control
            v = ("held", "CI's blocking jobs passed", "ci") if ci[r["key"]] else ("missed", "a blocking CI job failed", "ci")
        elif k == "race":
            side = str(r["key"]).split(">")[0]
            for d in picks:
                if side == d.get("winner") or side == d.get("loser") or side in (d.get("losers") or []):  # T-2339: 3 sides
                    v = ("held", "the side won the race", "race") if d.get("winner") == side else \
                        ("missed", "the side lost the race", "race")
        got[r["id"]] = v
    return got


def scored(p, outs=None, first=None):
    """Every bet with its outcome ('held', 'missed' or None while pending): a grade row already written stands,
    else the signals above decide. T-2610: a task or choice bet whose task's survival window is still open reads
    pending, its grade kept as `early`: a miss lands mid-window and a hold only after it, so every reader (buckets,
    Briers, routes, trails, the bar check, the trial grader) would otherwise read the record worse than it is."""
    bets, grades = ledger(p)
    if not bets:
        return []
    outs = outcomes(p) if outs is None else outs
    first = _first_finish(p) if first is None else first
    verdicts = _verdicts(p, bets, outs, first)
    out = []
    for r in bets:
        g = grades.get(r["id"])
        v = verdicts[r["id"]]
        row = dict(r, outcome=g["outcome"] if g else v[0], by=g.get("by") if g else v[1], src=g.get("src") if g else v[2])
        if row["outcome"] and r["kind"] in ("task", "choice") and (outs.get(r.get("task")) or {}).get("window_done") is False:
            row.update(outcome=None, early=row["outcome"])
        out.append(row)
    return out


def settle(p, outs=None):
    """Write one outcome row per bet whose signal has spoken (once each); the grade is a new row, the bet is untouched."""
    bets, grades = ledger(p)
    outs = outcomes(p) if outs is None else outs
    first = _first_finish(p)
    verdicts = _verdicts(p, bets, outs, first) if bets else {}
    new = [(i, v) for i, v in verdicts.items() if v[0] and i not in grades]
    if new:
        with c.lock(p.dir):
            done = ledger(p)[1]
            with open(_ledger_path(p), "a", encoding="utf-8") as f:
                for i, (outcome, by, src) in new:
                    if i not in done:
                        f.write(json.dumps({"ts": c.now(), "of": i, "outcome": outcome, "by": by, "src": src}) + "\n")
    return len(new)


def bars(p):
    """{research, race}: the confidence bars, from project meta over the defaults. Never below 0; a bar set by hand
    above BAR_MAX stays (only Sampo's own raises stop at it)."""
    try:
        saved = c.read_meta(p).get("calibration_bars") or {}
    except (OSError, c.CorruptState):
        saved = {}
    out = dict(BAR_DEFAULTS)
    for k in out:
        try:
            out[k] = max(0, int(saved.get(k, out[k])))
        except (TypeError, ValueError):
            pass
    return out


def _bucket(stated):
    return next(i for i, (lo, hi) in enumerate(BUCKETS) if lo <= stated <= hi)


def tables(rows):
    """{kind: [bucket dicts]} over the scored rows: n, held, the share held, the mean stated and the gap (stated
    minus held, in points)."""
    out = {}
    for kind in KINDS:
        for i, (lo, hi) in enumerate(BUCKETS):
            got = [r for r in rows if r["kind"] == kind and r["outcome"] and _bucket(r["stated"]) == i]
            if got:
                held = sum(r["outcome"] == "held" for r in got)
                mean = sum(r["stated"] for r in got) / len(got)
                out.setdefault(kind, []).append({"lo": lo, "hi": hi, "n": len(got), "held": held,
                                                 "pct": round(100 * held / len(got)), "stated": round(mean),
                                                 "gap": round(mean - 100 * held / len(got))})
    return out


def interim(rows, outs):
    """T-2498: per kind and bucket, the pending task and choice bets whose task closed INTERIM_DAYS+ ago, and how many
    held so far (nothing undid or corrected it yet). A read, never a score: it moves no bar and no Brier. T-2610: a
    task lost inside its open window is pending too (scored() holds its grade back), and counts as not held so far."""
    out = {}
    for r in rows:
        o = outs.get(r.get("task")) or {}
        if r["outcome"] or r["kind"] not in ("task", "choice") or o.get("survival") not in ("settling", "lost") \
                or o.get("days_left", SURVIVE_DAYS) > SURVIVE_DAYS - INTERIM_DAYS:
            continue
        lo, hi = BUCKETS[_bucket(r["stated"])]
        b = out.setdefault(r["kind"], {}).setdefault(lo, {"lo": lo, "hi": hi, "n": 0, "held": 0})
        b["n"] += 1
        b["held"] += o.get("fate") == "held"
    return {k: [dict(b, pct=round(100 * b["held"] / b["n"])) for _, b in sorted(v.items())] for k, v in out.items()}


def _interim_line(d):
    return (f"Interim (closed {INTERIM_DAYS}+ days ago, not scored): " + "; ".join(
        f"{k} {b['lo']}–{b['hi']}%: {b['held']} of {b['n']} held so far" for k, bs in d["interim"].items() for b in bs)
        if d.get("interim") else "")


def _trails(done):
    """T-2499: (kind, stated Brier, base Brier, n) for the kind whose stated Brier is furthest above its base rate's,
    over rows with a base (5+ per kind); None when no kind trails."""
    worst = None
    for k in KINDS:
        rows = [r for r in done if r["kind"] == k and r.get("base") is not None]
        if len(rows) >= 5:
            s, b = brier(rows), brier(rows, "base")
            if s > b and (worst is None or s - b > worst[1] - worst[2]):
                worst = (k, s, b, len(rows))
    return worst


def brier(rows, key="stated"):
    """The mean squared miss of a forecast against what happened (0 is perfect); None without rows that have it."""
    pairs = [(r[key] / 100, r["outcome"] == "held") for r in rows if r["outcome"] and r.get(key) is not None]
    return sum((x - h) ** 2 for x, h in pairs) / len(pairs) if pairs else None


def _pairs(rows):
    return {(r["type"], r["tier"]) for r in rows if r.get("type") and r.get("tier") and r["outcome"]}


def routes(p, rows=None):
    """{'TYPE TIER': base rate}: where the stated numbers (over LEDGER_MIN scored rows of the pair) score a worse Brier
    than the base rate did, the base rate's forecast is used in their place."""
    rows = scored(p) if rows is None else rows
    out = {}
    for typ, tier in sorted(_pairs(rows)):
        got = [r for r in rows if r["outcome"] and (r.get("type"), r.get("tier")) == (typ, tier)]
        based = [r for r in got if r.get("base") is not None]
        s, b = brier(based), brier(based, "base")
        if len(based) >= LEDGER_MIN and s is not None and b is not None and s > b:
            out[f"{typ} {tier}"] = based[-1]["base"]
    return out


def bar_rows(rows):
    """The scored rows allowed to move a bar: a task or choice bet whose outcome came from survival (fm outcomes' re-fix,
    revert or quiet window), the one signal Sampo didn't author; one row per task (its task bet over its choice bet),
    so a close counts once. Race picks, assumption marks, reversed decisions and a failed first finish score in the
    report but never move a bar."""
    out, tasks = [], set()
    for r in sorted((r for r in rows if r["kind"] in ("task", "choice") and r.get("outcome") and r.get("src") == "survival"),
                    key=lambda r: r["kind"] != "task"):  # stable: a task bet sorts before a choice bet
        if r.get("task") is not None:
            if r["task"] in tasks:
                continue
            tasks.add(r["task"])
        out.append(r)
    return out


def adjust_bars(p, rows):
    """Raise the research bar (never lower one) when a bucket of 20+ bar rows (see bar_rows) ran 15+ points over: at
    most BAR_STEP, at most BAR_MAX, once per 20 fresh rows in that bucket. The race bar stays where it is until a
    signal Sampo didn't author scores a race. A raise is an fm decide row and a registered trial on the Brier; the
    trial is registered first, so a failed add leaves nothing raised. A bucket 15+ points under is reported once as a
    costly decision, so fm decide --review carries it to the final report. Returns (raised [(bar, old, new)],
    under [text], warnings [text])."""
    from sampo import cli
    from sampo import trials
    table = tables(rows)
    meta = c.read_meta(p)
    cur, seen = bars(p), dict(meta.get("calibration_seen") or {})
    plan, under, warns = None, [], []
    for kind, buckets in table.items():
        for b in buckets:
            if b["n"] < LEDGER_MIN:
                continue
            key = f"{kind}:{b['lo']}"
            if b["gap"] >= BAR_GAP and not plan and b["n"] - int(seen.get(key, 0)) >= LEDGER_MIN \
                    and cur["research"] < BAR_MAX:
                plan = (key, b, kind)
            elif b["gap"] <= -BAR_GAP:
                under.append((key, b["n"], f"{kind} {b['lo']}–{b['hi']}%: underconfident by {-b['gap']} points over "
                              f"{b['n']} rows (stated {b['stated']}%, held {b['pct']}%); Sampo never lowers a bar: "
                              f"lower a bar yourself if you agree"))
    raised = []
    new = min(BAR_MAX, cur["research"] + BAR_STEP)
    if plan:
        try:  # first: if this fails nothing was raised and the next look tries again
            trials.add(p, f"research bar {cur['research']} → {new} (Brier of stated confidence)", "brier", 30)
        except Exception as e:
            warns.append(f"the research bar was not raised: its trial could not be registered ({e})")
            plan = None
    with c.lock(p.dir):  # ponytail: two racing looks could each register a trial; the re-check below keeps one raise
        meta = c.read_meta(p)
        cur, seen = bars(p), dict(meta.get("calibration_seen") or {})
        said = dict(meta.get("calibration_under") or {})
        if plan:
            key, b, kind = plan
            if b["n"] - int(seen.get(key, 0)) >= LEDGER_MIN and cur["research"] < BAR_MAX:
                old, cur["research"] = cur["research"], min(BAR_MAX, cur["research"] + BAR_STEP)
                seen[key] = b["n"]
                raised.append(("research", old, cur["research"]))
                cli._write_decision(
                    p, f"Raised the research bar {old} → {cur['research']}: {kind} bets stated {b['stated']}% in "
                       f"{b['lo']}–{b['hi']}% held {b['pct']}% over {b['n']} scored closes",
                    why="the calibration ledger: overconfident by 15+ points over 20+ closes; a bar only rises",
                    task=None, locked=True)
        for key, n, text in under:
            if n - int(said.get(key, -LEDGER_MIN)) >= LEDGER_MIN:
                said[key] = n
                cli._write_decision(p, "Calibration: " + text, why="the calibration ledger", kind="costly",
                                      task=None, locked=True)
        if raised or said != (meta.get("calibration_under") or {}):
            meta.update(calibration_bars=dict(meta.get("calibration_bars") or {}, **{"research": cur["research"]}),
                        calibration_seen=seen, calibration_under=said)
            c.write_meta(p, meta)
    return raised, [t for _k, _n, t in under], warns


def maintain(p):
    """Run at each fm task finish: write the outcome rows that are due and apply the bar check. Cheap when no bet was
    ever stated (one stat). Never raises: a ledger doesn't fail a close. Returns the outcome rows written (T-2520)."""
    try:
        if not os.path.exists(_ledger_path(p)):
            return 0
        outs = outcomes(p)
        n = settle(p, outs)
        adjust_bars(p, bar_rows(scored(p, outs)))
        return n
    except Exception:
        return 0


def ledger_data(p, outs=None):
    outs = outcomes(p) if outs is None else outs
    rows = scored(p, outs)
    done = [r for r in rows if r["outcome"]]
    table = tables(rows)
    based = [r for r in done if r.get("base") is not None]
    return {"total": len(rows), "scored": len(done), "pending": len(rows) - len(done), "kinds": table,
            "open_missed": sum(r.get("early") == "missed" for r in rows),  # T-2610
            "brier": {"stated": brier(done), "base": brier(based, "base")} if len(done) >= LEDGER_MIN else None,
            "bars": bars(p), "routes": routes(p, rows) if len(done) >= LEDGER_MIN else {}, "interim": interim(rows, outs),
            "trails": _trails(done) if len(done) >= LEDGER_MIN else None}  # T-2499


def _bucket_line(kind, b):
    return f"{kind} {b['lo']}–{b['hi']}%: n {b['n']}, held {b['held']} ({b['pct']}%), gap {b['gap']:+d}"


def _open_line(d):
    return (f"{d['open_missed']} task or choice bet(s) missed inside a {SURVIVE_DAYS}-day window still open: scored when "
            "it ends" if d.get("open_missed") else "")


def _too_few(d):
    return f"Calibration ledger: too few ({d['scored']} of {LEDGER_MIN} scored, {d['pending']} pending)"


def _brier_line(d):
    s, b = d["brier"]["stated"], d["brier"]["base"]
    return f"Brier: stated {s:.2f}, base rate {'%.2f' % b if b is not None else 'n/a'} (0 is perfect)"


def ledger_lines(p, outs=None):
    """The digest's lines: the ledger's buckets and both Briers, or 'too few'; [] with no bets at all."""
    d = ledger_data(p, outs)
    if not d["total"]:
        return []
    if not d["brier"]:
        return [_too_few(d)] + list(filter(None, [_open_line(d)]))
    return list(filter(None, [_open_line(d)])) + ["Calibration ledger: " + "; ".join(_bucket_line(k, b) for k, bs in d["kinds"].items() for b in bs)
            + f"; {_brier_line(d)}" + (  # T-2499
                "; trails its base rate most: {} (Brier stated {:.2f} vs base {:.2f}, n {})".format(*d["trails"])
                if d["trails"] else "; no kind's stated Brier trails its base rate")]


def extras(p, briefs=None):
    """T-2344: races run and how often the first choice lost (race_pick rows with a stated choice), and the open tasks
    parked at a one-way door now (the user's call)."""
    picks = [e.get("data") or {} for e in c.ledger_events(p, ("race_pick",))]
    judged = [d for d in picks if d.get("stated") is not None]
    parked = [b.id for b in (briefs if briefs is not None else c.load_briefs(p))
              if b.status not in c.CLOSED and c.door_park(b)]
    return {"races": {"run": len(picks), "judged": len(judged), "first_lost": sum(1 for d in judged if not d.get("first_won"))},
            "doors_parked": parked}


def priced_doors(p, briefs=None):
    """T-2347: each open task parked at a one-way door, priced for the final report: {id, step, days, blocks}: the step,
    days since its newest door_park event (None without one) and the open tasks that depend on it. Reads the ledger,
    so only fm decide --review calls it (never the band)."""
    briefs = c.load_briefs(p) if briefs is None else briefs
    parked = [(b, c.door_park(b)) for b in briefs if b.status not in c.CLOSED]
    parked = [(b, why) for b, why in parked if why]
    if not parked:
        return []
    since = {e["task"]: e.get("ts") for e in c.ledger_events(p, ("door_park",)) if e.get("task")}
    out = []
    for b, why in parked:
        days = c.age_days(since.get(b.id))
        out.append({"id": b.id, "step": c.fit(why.split(": ", 1)[-1], 90), "days": None if days is None else int(days),
                    "blocks": sorted(x.id for x in briefs if x.status not in c.CLOSED and b.id in c._deps(x))})
    return out


def snapshot(p):
    """T-2344: fm outcomes --judgment's data with none of its writes (no settle, no bar move): the MCP tool and the band read it."""
    d = ledger_data(p, outcomes(p))
    d["bars"] = bars(p)
    d.update(extras(p))
    return d


def band_line(p):
    """T-2344: one pane line when judgment needs the user, else '': tasks parked at a one-way door, and the stated numbers
    scoring a worse Brier than the base rate (the base rate routes in their place until they don't)."""
    d = snapshot(p)
    br = d.get("brier") or {}
    parts = [f"{len(d['doors_parked'])} parked at a one-way door ({', '.join(d['doors_parked'][:3])})"] \
        if d["doors_parked"] else []
    if br.get("base") is not None and br.get("stated") is not None and br["stated"] > br["base"]:
        parts.append(f"stated confidence scores Brier {br['stated']:.2f}, worse than the base rate's {br['base']:.2f}")
    return ("judgment: " + "; ".join(parts) + " (fm outcomes --judgment)") if parts else ""


def cmd_calibration(args):
    """fm outcomes --judgment: the ledger's buckets per kind, the Brier of the stated numbers beside the base rate's, the
    bars and where the base rate routes. Writes the outcome rows that are due and applies a bar raise."""
    from sampo import cli
    p = cli.resolve(args)
    outs = outcomes(p)
    settle(p, outs)
    d = ledger_data(p, outs)
    if getattr(args, "by", None) == "harness":  # T-2500: the same buckets and Briers, one group per harness
        rows = scored(p, outs)
        d["by"] = {}
        for h in sorted({r.get("harness") or "unknown" for r in rows}):
            mine = [r for r in rows if (r.get("harness") or "unknown") == h]
            done = [r for r in mine if r["outcome"]]
            based = [r for r in done if r.get("base") is not None]
            d["by"][h] = {"total": len(mine), "scored": len(done), "kinds": tables(mine),
                          "brier": {"stated": brier(done), "base": brier(based, "base")} if len(done) >= LEDGER_MIN
                          else None}
    raised, under, warns = adjust_bars(p, bar_rows(scored(p, outs)))
    d["bars"], d["raised"], d["under"] = bars(p), raised, under
    d.update(extras(p))  # T-2344: races and parked doors, so every surface reads one JSON
    lines = [_too_few(d) + ": no bar moves" if not d["brier"] else "Calibration ledger: " + _brier_line(d)]
    lines += [_bucket_line(k, b) for k, bs in d["kinds"].items() for b in bs]
    lines += [_interim_line(d)] if d["interim"] else []
    lines += list(filter(None, [_open_line(d)]))  # T-2610
    lines += [f"Raised the {bar} bar {old} → {new}." for bar, old, new in raised] + under + warns
    lines += [f"{pair}: the base rate's {v}% replaces the stated numbers (they scored a worse Brier)"
              for pair, v in d["routes"].items()]
    r = d["races"]
    lines += [f"Races: {r['run']} run; the stated first choice lost {r['first_lost']} of {r['judged']}"] if r["run"] else []
    lines += [f"Parked at a one-way door: {', '.join(d['doors_parked'])}"] if d["doors_parked"] else []
    lines.append(f"Bars: research {d['bars']['research']} · race {d['bars']['race']}")
    lines += [f"{h}: {g['scored']} of {g['total']} scored" + (f", Brier stated {g['brier']['stated']:.2f}" if g["brier"] else ", too few for a Brier")
              for h, g in (d.get("by") or {}).items()]  # T-2500
    return cli.out(args, d, "\n".join(lines))


PLAN_WINDOW, PLAN_MISS, PLAN_DAYS, PLAN_OUTSIDE = 5, 3, 14, 2  # T-1061: last 5 closes within 14 days; 3 missed = slow


def missed(d):
    """A close missed its plan: more than PLAN_OUTSIDE files outside the scope it had at first focus, more than twice the
    predicted minutes, or more attempts than predicted; None when nothing was compared."""
    checks = [d["outside"] > PLAN_OUTSIDE if "outside" in d else None,
              d["minutes"] > 2 * d["predict_minutes"] if d.get("predict_minutes") and "minutes" in d else None,
              d["attempts"] > d["predict_attempts"] if d.get("predict_attempts") and "attempts" in d else None]
    checks = [x for x in checks if x is not None]
    return any(checks) if checks else None


def active_minutes(stamps, idle=1800):
    """Minutes between consecutive epoch stamps, leaving out gaps of idle seconds or more (overnight, away)."""
    return round(sum(b - a for a, b in zip(stamps, stamps[1:]) if b - a < idle) / 60)


CLOSES_KEEP = 2000  # T-2290: rows kept in closes.jsonl; the predictor reads the last 30 per tier and type
PREDICT_N, PREDICT_MIN = 30, 5


def _closes_path(p):
    return os.path.join(p.dir, "closes.jsonl")


def _backfill(p):
    """Once, when closes.jsonl doesn't exist: the task_done events that logged minutes, joined to their brief's type and
    tier when the event lacks them. Writes the newest CLOSES_KEEP // 2 rows (an empty file too, so it runs once)."""
    kinds, rows = None, []
    for e in c.ledger_events(p, ("task_done",)):
        d = e.get("data") or {}
        if not isinstance(d.get("minutes"), int):
            continue
        if kinds is None and not (d.get("type") and d.get("tier")):
            kinds = {b.id: (b.type, b.tier) for b in c.load_briefs(p, include_archive=True)}  # once, only if needed
        typ, tier = (d.get("type"), d.get("tier")) if d.get("type") and d.get("tier") else (kinds or {}).get(e.get("task"), (None, None))
        if typ and tier:
            rows.append({"type": typ, "tier": tier, **{k: d[k] for k in ("minutes", "attempts", "predict_minutes",
                                                                         "predict_attempts") if k in d}})
    rows = rows[-CLOSES_KEEP // 2:]
    try:
        with open(_closes_path(p), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
    except OSError:
        pass
    return rows


def _closes(p):
    if not os.path.exists(_closes_path(p)):
        return _backfill(p)
    rows = []
    try:
        with open(_closes_path(p), encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except OSError:
        pass
    return rows


def history(p, typ, tier):
    """Rows of the last PREDICT_N closes of this type and tier that logged their minutes, oldest first."""
    return [r for r in _closes(p) if r.get("type") == typ and r.get("tier") == tier
            and isinstance(r.get("minutes"), int)][-PREDICT_N:]


def predict(p, typ, tier):
    """T-2290: (minutes, attempts) as the median of the last PREDICT_N same-tier, same-type closes; None under
    PREDICT_MIN samples (a median of three is noise)."""
    rows = history(p, typ, tier)
    if len(rows) < PREDICT_MIN:
        return None
    att = [r["attempts"] for r in rows if isinstance(r.get("attempts"), int)]
    return max(1, round(statistics.median(r["minutes"] for r in rows))), \
        max(1, round(statistics.median(att))) if att else 1


def run_stats(p, tid):
    """(active minutes, attempts) of the task since its latest focus, or None when that focus isn't in the ledger tail.
    Gaps of 30+ minutes between the task's events (overnight, away) don't count."""
    evs = [e for e in c.ledger_tail(p, 5000) if e.get("task") == tid]
    start = [i for i, e in enumerate(evs) if e.get("event") == "focus" and c.parse_ts(e.get("ts"))]
    if not start:
        return None
    since = evs[start[-1]:]
    return (active_minutes([c.parse_ts(e["ts"]).timestamp() for e in since if c.parse_ts(e.get("ts"))] + [time.time()]),
            1 + sum(1 for e in since if e.get("event") == "finish" and (e.get("data") or {}).get("failed")))


def overrun_note(p, b):
    """One note when an active task's minutes pass 3x its prediction; "" otherwise (and without a prediction)."""
    try:
        pm = int(b.meta.get("predict_minutes") or 0)
    except (TypeError, ValueError):
        return ""
    st = run_stats(p, b.id) if pm else None
    if st and st[0] > 3 * pm:
        return (f"{b.id} is at {st[0]} active min, past 3x its prediction ({pm}): re-plan, split it (fm split), or "
                f"ask for fresh eyes")
    return ""


def calibration_rows(p):
    """{tier: (median actual/predicted minutes, n)} over closes that carried both."""
    by = collections.defaultdict(list)
    for r in _closes(p):
        pm, m = r.get("predict_minutes"), r.get("minutes")
        if isinstance(pm, int) and pm > 0 and isinstance(m, int) and r.get("tier"):
            by[r["tier"]].append(m / pm)
    return {t: (statistics.median(v), len(v)) for t, v in sorted(by.items())}


def calibration_report(p):
    rows = calibration_rows(p)
    return [f"Prediction calibration (minutes): {t}: median actual/predicted {r:.1f}x over {n} close(s)"
            for t, (r, n) in rows.items()] or ["Prediction calibration: no close carried a prediction yet"]


def record_close(p, d):
    """Keep this close's verdict in project meta (the last PLAN_WINDOW), so reading it costs nothing per turn."""
    if isinstance(d.get("minutes"), int):  # T-2290: the history the next prediction reads; one small append
        row = {k: d[k] for k in ("type", "tier", "minutes", "attempts", "predict_minutes", "predict_attempts") if k in d}
        with c.lock(p.dir):
            rows = _closes(p)
            path = _closes_path(p)
            if len(rows) >= CLOSES_KEEP:  # bounded: rewrite the newest half
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(json.dumps(r) + "\n" for r in rows[-CLOSES_KEEP // 2:])
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
    m = missed(d)
    if m is not None:
        with c.lock(p.dir):  # read and append under one lock: two closes can't lose a verdict
            meta = c.read_meta(p)
            meta["plan_verdicts"] = ((meta.get("plan_verdicts") or []) + [[c.now(), m]])[-PLAN_WINDOW:]
            c.write_meta(p, meta)


def plan_window(p):
    """[bool] — missed or not — for the stored verdicts under PLAN_DAYS old; malformed entries are skipped."""
    rows = c.read_meta(p).get("plan_verdicts")
    return [bool(r[1]) for r in rows if isinstance(r, (list, tuple)) and len(r) == 2 and isinstance(r[0], str)
            and (c.age_days(r[0]) or 0) < PLAN_DAYS] if isinstance(rows, list) else []


def misses(p):
    """True while the plans keep missing, so the project slows to approval for M until closes recover."""
    w = plan_window(p)
    return len(w) >= PLAN_WINDOW and sum(w) >= PLAN_MISS


def plan_lines(p):
    w = plan_window(p)
    return [f"Plan calibration: {sum(w)} of the last {len(w)} closes missed their plan"
            + ("; autonomy slows to approval for M until they recover" if misses(p) else "")] if w else []


def _first_finish(p):
    first = {}
    for e in c.ledger_tail(p, 50000, kinds=("finish",)):
        if e.get("event") == "finish" and e.get("task") and e["task"] not in first:
            first[e["task"]] = not (e.get("data") or {}).get("failed")
    return first


ATLAS_MIN = 3  # tasks before a file or language counts


def atlas(p, outs=None):
    """T-0620: where work tends not to hold. {"files": [...], "languages": [...]}, each row (name, n, bad, weak):
    done tasks that touched it, how many were later fixed or reverted, how many closed with a weak grade; worst first."""
    outs = outcomes(p) if outs is None else outs
    files, langs = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    for b in c.load_briefs(p, include_archive=True):
        if b.id not in outs:
            continue
        touched = [x.lstrip("- ").strip() for x in b.section("Files touched").splitlines() if x.strip()]
        bad, weak = outs[b.id]["fate"] != "held", b.meta.get("verified") == "weak"
        for key, group in [(f, files) for f in touched] + [(e, langs) for e in
                                                             {os.path.splitext(f)[1] or "(none)" for f in touched}]:
            r = group[key]
            r["n"], r["bad"], r["weak"] = r["n"] + 1, r["bad"] + bad, r["weak"] + weak

    def rank(group):
        rows = [(k, r["n"], r["bad"], r["weak"]) for k, r in group.items() if r["n"] >= ATLAS_MIN]
        return sorted(rows, key=lambda x: (-(x[2] + x[3]) / x[1], -x[1], x[0]))
    return {"files": rank(files), "languages": rank(langs)}


def atlas_lines(p, outs=None, n=8):
    a = atlas(p, outs)
    out = []
    for title, rows in (("Files", a["files"]), ("Languages", a["languages"])):
        rows = [r for r in rows if max(r[2], r[3]) >= 2][:n]  # T-2764: caution()'s bar, two distinct tasks
        out += [f"{title} where work didn't hold (of {ATLAS_MIN}+ tasks):"] + [
            f"- {k}: {bad} of {total} needed a later fix or revert" + (f", {weak} closed weak" if weak else "")
            for k, total, bad, weak in rows] if rows else []
    return out or [f"No file or language with {ATLAS_MIN}+ finished tasks has needed a later fix yet."]


# ---- T-3934: outcome probes (a shipped feature is done when its outcome is seen) and the census of what exists

STAGES = (1, 7, 30)  # days after the merge a declared signal is checked
PROBE_TTL = 6 * 3600  # sampo next and doctor reuse a result this long; sampo outcomes --silent always re-reads
WINDOW_DAYS = 45  # a close older than this is out of the probe: its 30-day check has had its say, and the events are bounded
SIGNAL = re.compile(r"\s*(event|verb|file|row|metric):\s*(\S.*?)\s*$")
# the cases found 2026-11-08 in Foreman's own repo, probed like any declared signal: (id, title, signal, since)
SEEDS = [("seed:skills", "Sampo's skills are loaded (20 of 26 were never loaded in a month)", "metric:skills_loaded",
          "2026-11-05T00:00:00Z"),
         ("seed:model-bench", "the night's model bench writes a row", "row:research/models/bench.jsonl", "2026-11-05T00:00:00Z"),
         ("seed:call-cards", "call cards are run on a real task (they are unproven)", "verb:card", "2026-11-05T00:00:00Z")]
CENSUS_SEEDS = {"misses": "caught 2 of 7 asks", "repeats": "can't see ops scripts",
                "conductor": "remote was registered but the session relayed by tmux", "outcomes": "--predictions has 0 catches"}
ACTIONS = ("capture", "task_new", "task_split", "scope_note", "step_edit", "decision")  # what an output can lead to
ADJACENT = 600  # seconds: an action this soon after a use, in the same session, credits the use (ponytail: busy sessions
# over-credit; upgrade to explicit acted-on events if it proves noisy)


def stage(age):
    """The latest probe (1, 7 or 30) an age in days has reached; 0 before the first."""
    return max((s for s in STAGES if age >= s), default=0)


def _builder(e):
    return "builder" in str(e.get("agent_type") or "")  # ponytail: a builder's own run is no use, whatever lane scope it had


def used_counts(events, slug):
    """{"verb:X" | "skill:X" | "playbook:X" | "hook:KEY": [(session, ts)]} from global events, builders' runs left out."""
    out = collections.defaultdict(list)
    for e in events:
        if e.get("kind") == "inject":
            out[f"hook:{e.get('key')}"].append((None, e.get("ts")))
        if e.get("project") != slug or _builder(e):
            continue
        at, tool, target = (e.get("session_id"), e.get("ts")), e.get("tool"), str(e.get("target") or "")
        if e.get("kind") == "slash":
            for s in e.get("skills") or []:
                out[f"skill:{s}"].append(at)
        elif e.get("kind") != "tool":
            continue
        elif tool == "Skill":
            out[f"skill:{target.split(':')[-1]}"].append(at)
        elif tool == "Read" and "/skills/playbooks/references/" in target and target.endswith(".md"):
            out[f"playbook:{target.split('/skills/playbooks/references/')[1][:-3]}"].append(at)
        elif tool == "Bash" and (m := re.match(r"\s*(?:sampo|fm)\s+([a-z][a-z-]*)", target)):
            out[f"verb:{m.group(1)}"].append(at)
    return out


def _skills():
    root = os.path.join(c.PLUGIN_ROOT, "skills")
    return sorted(n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n, "SKILL.md")))


def _loaded_share(events, slug):
    names = _skills()
    used = used_counts(events, slug)
    return sum(bool(used.get(f"skill:{n}")) for n in names) / (len(names) or 1)


METRICS = {"skills_loaded": lambda ev, slug: _loaded_share(ev, slug) >= 0.5}  # half of the skills loaded in the window


def _safe(rel):
    return not (os.path.isabs(rel) or ".." in rel.split("/"))


def _fired(p, kind, sel, since, ev, slug):
    """"fired", "silent" or "manual" (a signal this can't read is never passing) for one signal since an ISO time."""
    from glob import glob
    mine = [e for e in ev if str(e.get("ts") or "")[:19] >= since[:19]]
    if kind == "event":
        return "fired" if any(e.get("kind") == sel or e.get("event") == sel for e in mine) else "silent"
    if kind == "verb":
        words = sel.split()
        rx = re.compile(r"\s*(?:sampo|fm)\s+" + r"\s+".join(map(re.escape, words)) + r"\b")
        return "fired" if any(e.get("kind") == "tool" and e.get("tool") == "Bash" and e.get("project") == slug
                              and not _builder(e) and rx.match(str(e.get("target") or "")) for e in mine) else "silent"
    if kind == "metric":
        return "manual" if sel not in METRICS else "fired" if METRICS[sel](mine, slug) else "silent"
    if not _safe(sel):
        return "manual"
    t0 = c.parse_ts(since).timestamp()
    if kind == "file":
        return "fired" if any(os.path.getmtime(f) >= t0 for f in glob(os.path.join(p.root, sel), recursive=True)
                              if os.path.lexists(f)) else "silent"
    path = next((x for x in (os.path.join(p.root, sel), os.path.join(c.state_dir(), sel)) if os.path.isfile(x)), None)
    if not path:  # row:
        return "silent"
    for r in c.tail_jsonl(path, 200) or []:
        stamp = r.get("at") or r.get("ts") if isinstance(r, dict) else None
        if (str(stamp)[:19] >= since[:19]) if stamp else os.path.getmtime(path) >= t0:
            return "fired"
    return "silent"


def _evaluate(p):
    declared = []  # [(id, title, signal, merged)]
    for b in c.load_briefs(p):
        if b.status != "done" or (c.age_days(b.meta.get("updated")) or 0) > WINDOW_DAYS + 5:
            continue
        sig = (b.section("Outcome").strip().splitlines() or [""])[0]
        if sig and (age := c.age_days(done_at(b))) is not None and age <= WINDOW_DAYS:
            declared.append((b.id, b.title, sig, done_at(b)))
    if os.path.isfile(os.path.join(p.root, "plugin", "lib", "sampo", "outcomes.py")):  # Foreman's own repo
        declared += list(SEEDS)
    rows, stamp, ev = [], c.now(), []
    due = [d for d in declared if stage(c.age_days(d[3]) or 0)]
    if due:
        oldest = min(d[3] for d in due)
        ev = [e for e in c.global_events(oldest) if e.get("project") == p.slug or e.get("kind") == "inject"]
        names = tuple({m.group(2) for d in due if (m := SIGNAL.match(d[2])) and m.group(1) == "event"})
        ev += [e for e in c.ledger_events(p, names, since=oldest)] if names else []
    for tid, title, sig, merged in declared:
        age = c.age_days(merged) or 0
        m = SIGNAL.match(sig)
        st = "waiting" if not stage(age) else "manual" if not m else _fired(p, m.group(1), m.group(2), merged, ev, p.slug)
        rows.append({"task": tid, "title": title[:100], "signal": sig, "merged": merged, "age_days": round(age, 1),
                     "stage": stage(age), "status": st, "checked": stamp})
    return rows


def _spawn(p):
    """Refresh the cache in a detached child, never waited on (the Stop hook can't pay the probe's seconds); at most
    one start per 10 minutes; FOREMAN_NO_BACKGROUND skips it."""
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        return
    mark = os.path.join(p.dir, "outcome-probes.refresh")
    try:
        if time.time() - os.path.getmtime(mark) < 600:
            return
    except OSError:
        pass
    try:
        with open(mark, "w") as f:
            f.write(c.now())
        subprocess.Popen([sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "sampo"), "-p", p.slug, "outcomes", "--silent"],
                         cwd=p.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def probes(p, fresh=False, cached=False):
    """Every declared outcome's probe result, each stamped with its check time. Cached per project for PROBE_TTL.
    cached=True (sampo next, doctor: the hook path) never evaluates inline: a stale or missing cache returns what it
    holds and starts a detached refresh."""
    path = os.path.join(p.dir, "outcome-probes.json")
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if not fresh and time.time() - cache.get("t", 0) < PROBE_TTL:
        return cache.get("rows") or []
    if cached and not fresh:
        _spawn(p)
        return cache.get("rows") or []
    rows = _evaluate(p)
    try:
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "rows": rows}, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return rows


def flagged(rows):
    """Due and not fired (silent, or manual: nobody can read it), oldest merge first."""
    return sorted((r for r in rows if r["status"] in ("silent", "manual")), key=lambda r: r["merged"])


def line(p):
    """The one sampo next line: the oldest feature whose outcome was never seen (cached; empty when nothing is)."""
    try:
        bad = flagged(probes(p, cached=True))
    except Exception:
        return ""
    if not bad:
        return ""
    r = bad[0]
    return (f" · outcome not seen: {r['task']} ({c.fit(r['title'], 60)}) shipped {r['age_days']:g} days ago, signal "
            f"{r['signal']} is {r['status']} (checked {r['checked']}); {len(bad)} in all: sampo outcomes --silent")


def _census_items(p, days):
    from sampo import cli
    since = c.iso(time.time() - days * 86400)
    ev = [e for e in c.global_events(since) if e.get("project") == p.slug or e.get("kind") == "inject"]
    used = used_counts(ev, p.slug)
    acts = collections.defaultdict(list)
    for e in c.ledger_events(p, ACTIONS + ("night",), since=since):
        if e.get("event") == "night":
            for j in (e.get("data") or {}).get("ran") or []:
                used[f"night:{j.get('name')}"].append((e.get("session_id"), e.get("ts")))
        elif e.get("session_id") and (t := c.parse_ts(e.get("ts"))):
            acts[e["session_id"]].append(t.timestamp())
    root = next(a for a in cli.build_parser()._actions if hasattr(a, "choices") and a.choices)
    ref = os.path.join(c.PLUGIN_ROOT, "skills", "playbooks", "references")
    exists = ([("verb", v) for v in sorted(root.choices)] + [("skill", s) for s in _skills()]
              + [("playbook", os.path.relpath(os.path.join(d, n), ref)[:-3]) for d, _, fs in os.walk(ref) for n in sorted(fs)
                 if n.endswith(".md")])
    try:
        from sampo import night
        exists += [("night", j[0]) for j in night.jobs(p)]
    except Exception:  # a night job list that can't be built (no local model, no budget) leaves the jobs out
        pass
    exists += [("hook", k.split(":", 1)[1]) for k in sorted(used) if k.startswith("hook:")]
    line = collections.defaultdict(list)  # session -> [(time, key)] of every verb, skill and playbook use
    for key, us in used.items():
        if key.split(":")[0] in ("verb", "skill", "playbook"):
            for sid, ts in us:
                if sid and (t := c.parse_ts(ts)):
                    line[sid].append((t.timestamp(), key))
    credit = collections.Counter()  # an action credits only the one use right before it: a busy session can't credit all
    for sid, times in acts.items():
        tl = sorted(line.get(sid, ()))
        for a in times:
            if (i := bisect.bisect_right([x[0] for x in tl], a) - 1) >= 0 and a - tl[i][0] <= ADJACENT:
                credit[tl[i][1]] += 1
    items = []
    for kind, name in exists:
        uses = used.get(f"{kind}:{name}", [])
        hit = None if kind in ("hook", "night") else min(credit[f"{kind}:{name}"], len(uses))  # hooks and night: unmeasured
        items.append({"kind": kind, "name": name, "exists": True, "used": len(uses), "effective": hit})
    return items


def census(p, days=14):
    """Every verb, skill, playbook, night job and hook note at three levels over the last days: exists, used (a run or
    load outside a builder), effective (an action followed in the same session)."""
    items = _census_items(p, days)
    for i in items:
        i["flag"] = "never used" if not i["used"] else "used, never effective" if i["effective"] == 0 else ""
    return items


def census_captures(p, items, limit=10):
    """Titles for the flagged items, the 2026-11-08 finds first, at most limit, none already on the board."""
    bad = [i for i in items if i["flag"] and i["kind"] != "hook"]
    bad.sort(key=lambda i: (i["name"] not in CENSUS_SEEDS or i["kind"] != "verb", i["kind"], i["name"]))
    have = {b.title for b in c.load_briefs(p)}
    out = []
    for i in bad:
        t = f"Outcome census: {i['kind']} {i['name']} is {i['flag']} in 14 days: wire it in (who should reach it, when) or retire it"
        if i["kind"] == "verb" and i["name"] in CENSUS_SEEDS:
            t += f" ({CENSUS_SEEDS[i['name']]})"
        if t not in have:
            out.append(t)
    return out[:limit]


VERB = re.compile(r"\b(?:sampo|fm) (?!task\b)([a-z][a-z-]{2,})")
GENERIC = {"check", "next", "status", "focus", "doctor", "evidence", "finish", "the", "and", "for", "run", "can"}


def backfill(p, days=30):
    """[(brief, signal or None)] for each FEATURE closed within days that declares no Outcome: the first `sampo VERB`
    its criteria or its CHANGELOG line names (a verb someone runs is an outcome we can see), else None. ponytail: only
    verbs; add file:/row: guesses when a pattern in the real proposals earns it."""
    try:
        with open(os.path.join(p.root, "CHANGELOG.md"), encoding="utf-8") as f:
            log = f.read().splitlines()
    except OSError:
        log = []
    from sampo import cli
    real = set(next(a for a in cli.build_parser()._actions if hasattr(a, "choices") and a.choices).choices)
    out = []
    for b in c.load_briefs(p):
        if b.status != "done" or b.meta.get("type") != "FEATURE" or b.section("Outcome").strip():
            continue
        if (age := c.age_days(done_at(b))) is None or age > days:
            continue
        text = b.section("Acceptance criteria") + " " + " ".join(l for l in log if f"({b.id})" in l)
        verbs = [v for v in VERB.findall(text) if v in real and v not in GENERIC]
        out.append((b, f"verb:{verbs[0]}" if verbs else None))
    return sorted(out, key=lambda r: done_at(r[0]))


def cmd_backfill(args, p):
    from sampo import cli
    rows = backfill(p, getattr(args, "days", None) or 30)
    todo = [(b, s) for b, s in rows if s]
    if getattr(args, "apply", False):
        for b, s in todo:
            cli.mutate(p, b.id, lambda x, s=s: x.set_section("Outcome", f"{s}\n\nRead it: sampo outcomes --silent"),
                       "outcome_backfill", {"signal": s})
    return cli.out(args, {"proposals": [{"task": b.id, "title": b.title, "signal": s} for b, s in rows],
                          "applied": bool(getattr(args, "apply", False))},
                   "\n".join([f"{len(todo)} of {len(rows)} FEATURE(s) get a signal" + (" (written)" if getattr(args, "apply", False)
                                                                                  else " (--apply writes them)") + ":"]
                             + [f"  {b.id} {s or '(none derivable)'}  {c.fit(b.title, 70)}" for b, s in rows]))


def cmd_probes(args, p):
    from sampo import cli
    if getattr(args, "backfill", False):
        return cmd_backfill(args, p)
    if getattr(args, "silent", False):
        rows = probes(p, fresh=True)
        bad = flagged(rows)
        return cli.out(args, {"silent": bad, "checked": len(rows)},
                       "\n".join([f"{len(bad)} of {len(rows)} declared outcome(s) never seen at 1, 7 or 30 days:"]
                                 + [f"  {r['task']} [{r['status']}, {r['age_days']:g} d, probe {r['stage']}] "
                                    f"{c.fit(r['title'], 70)}: {r['signal']} (checked {r['checked']})" for r in bad]))
    items = census(p, getattr(args, "days", None) or 14)
    flags = [i for i in items if i["flag"]]
    made = []
    if getattr(args, "capture", False):
        from sampo import cli as _cli
        with c.lock(p.dir):
            for t in census_captures(p, items):
                f = _cli._create(p, t, "FEATURE", "S", "captured", raw=t, source="discovered")
                c.log_event(p, "capture", task=f.id, data={"source": "discovered", "from": "outcomes --census"},
                            session=_cli.session())
                made.append(f.id)
            c.regen_views(p)
    by = collections.Counter((i["kind"], i["flag"]) for i in flags)
    head = [f"{len(items)} item(s), last 14 days: "
            + ", ".join(f"{n} {k} {f}" for (k, f), n in sorted(by.items())) + (f"; captured {', '.join(made)}" if made else "")]
    order = ("verb", "skill", "night", "playbook")  # each kind shows its first 12, ineffective before never used
    show = []
    for k in order:
        mine = sorted((i for i in flags if i["kind"] == k), key=lambda i: (
            not (k == "verb" and i["name"] in CENSUS_SEEDS), i["flag"] != "used, never effective", -i["used"], i["name"]))
        show += mine[:12]
    return cli.out(args, {"items": items, "captured": made},
                   "\n".join(head + [f"  {i['kind']} {i['name']}: {i['flag']} (used {i['used']}, effective {i['effective']})"
                                     for i in show] + [f"  … {len([i for i in flags if i['kind'] != 'hook']) - len(show)} more "
                                                       "(--json)"]))


def caution(p, scope):
    """One line for fm focus when the task's scope covers a file where 20%+ of past work (2+ tasks) needed a fix."""
    hits = []
    for k, total, bad, _ in atlas(p)["files"]:
        if bad >= 2 and bad / total >= 0.2 and any(k == s or k.startswith(s.rstrip("/") + "/") for s in scope):
            hits.append(f"{k}: {bad} of {total} tasks that touched it needed a later fix or revert")
    out = ("Caution: " + "; ".join(hits[:3]) + " — test its edges first.") if hits else ""  # not a list: text for a model, not a screen
    hot = []
    if scope and c.git_root(p.root):
        try:  # wild C18: files git history keeps fixing; the map is a hint, it never stops a focus
            from sampo import map as smap
            hot = [f"{f} ({n} fix/revert commits in 180 days)" for f, n in smap.load(p).get("fixy", {}).items()
                   if any(f == s or f.startswith(s.rstrip("/") + "/") for s in scope)]
        except Exception:
            pass
    return "\n".join(filter(None, [out, ("Hotspot: " + ", ".join(hot[:5])) if hot else ""]))  # not a list: text for a model, not a screen
