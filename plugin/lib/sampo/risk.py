"""T-1080: one daily risk budget for autonomy. Destructive guard blocks, failed closes and reverts spend weighted points
on the ledger's (UTC) day; past the cap, autonomy steps down a notch (full → standard, standard → slow) and optional
work waits, until the day rolls. Only the user moves the cap (the guard's USER_ONLY_FM): a brake the braked can lift
isn't one."""
import json
import math
import os
import time

from sampo import core as c

WEIGHTS = {"destructive block": 1, "failed close": 2, "revert": 3}
DESTRUCTIVE = {"rm-outside", "git-destructive", "system", "credentials", "self-authorize", "pipe-shell", "confine"}
CAP = 6
_MEMO = {}  # (slug, day, ledger size and mtime) -> status: one read per process while nothing new is logged
CACHE = "risk.json"  # T-2028: today's counts, the ledger offset they cover and the revert count, for the next process
_MARKS = c.lazy(rb'"guard_block"|"close_affected"|"guard_forgiven"')
OFF = "risk_off.json"  # T-3752: the user's switch, in Sampo state (no tool call writes there): everywhere, or one project's


def _day():
    return time.strftime("%Y-%m-%d", time.gmtime())


def cap(p, meta=None):
    v = (c.read_meta(p) if meta is None else meta).get("risk_cap")
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 else CAP


def off(p):
    """T-3752: {"scope", "at", "via"} when the user turned the budget off for this project or everywhere, else None. Only the
    foreman-ui mod's /fm-risk off (typed) or the user's own `sampo risk off` writes it; an agent's call is refused."""
    for scope, d in ((("here", p.dir),) if p else ()) + (("everywhere", c.state_dir()),):
        try:
            with open(os.path.join(d, OFF), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("on") is True:
            return {"scope": scope, "at": c.plain(str(rec.get("at") or "?"))[:25], "via": c.plain(str(rec.get("via") or "?"))[:20]}
    return None


def off_line(p):
    o = off(p)
    return o and f"risk budget off (by you, {o['at']}; {'this project' if o['scope'] == 'here' else 'every project'})"


def status(p, meta=None):
    """{"spent", "cap", "slowed", "counts", "off"} for today."""
    day = _day()
    try:
        st = os.stat(os.path.join(p.dir, "ledger.jsonl"))
        key = (p.slug, day, st.st_size, st.st_mtime_ns)
    except OSError:
        key = (p.slug, day, 0, 0)
    counts = _MEMO.get(key)
    if counts is None:
        counts = _MEMO[key] = _counts(p, day)
    spent = sum(WEIGHTS[k] * n for k, n in counts.items())
    limit = cap(p, meta)
    o = off(p)
    return {"spent": spent, "cap": limit, "slowed": spent >= limit and not o, "counts": counts, "off": o}


def _counts(p, day):
    """Today's counts. T-2028: every hook process read the day's ledger and ran git log; the cache file lets one read
    only the lines past its offset (checked by the start of the last line read, which holds its unique chain hash, so
    fm tidy's in-place roll fails it and the day is read afresh) and run git log only when HEAD's reflog moved."""
    cpath = os.path.join(p.dir, CACHE)
    try:
        with open(cpath, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = None
    old = old if isinstance(old, dict) and old.get("day") == day else {}
    n, saved = {"destructive block": 0, "failed close": 0}, old.get("n")
    off, line, mark, ok = 0, 0, "", False
    try:
        with open(os.path.join(p.dir, "ledger.jsonl"), "rb") as f:
            at, at_line, m = old.get("off"), old.get("line"), old.get("mark")
            if isinstance(saved, dict) and all(isinstance(saved.get(k), int) for k in n) and isinstance(m, str) and m \
                    and isinstance(at, int) and isinstance(at_line, int) and 0 <= at_line < at:
                f.seek(at_line)
                ok = f.read(len(m.encode("latin-1"))) == m.encode("latin-1")
            if ok:
                n, off, line, mark = {k: saved[k] for k in n}, at, at_line, m
                f.seek(at)
            else:
                c._seek_since(f, day.encode())
                off = f.tell()
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # still being written: the next read takes it
                if _MARKS.search(raw):
                    try:
                        e = json.loads(raw)
                    except (ValueError, RecursionError):
                        e = None
                    d = e.get("data") if isinstance(e, dict) else None
                    d = d if isinstance(d, dict) else {}
                    if isinstance(e, dict) and str(e.get("ts") or "") >= day:
                        if e.get("event") == "guard_block" and d.get("category") in DESTRUCTIVE:
                            n["destructive block"] += 1
                        elif e.get("event") == "guard_forgiven" and d.get("category") in DESTRUCTIVE:  # T-3752
                            n["destructive block"] = max(0, n["destructive block"] - 1)
                        elif e.get("event") == "close_affected" and d.get("exit"):
                            n["failed close"] += 1
                line, mark, off = off, raw[:64].decode("latin-1"), off + len(raw)
    except OSError:
        pass
    root, gkey = c.git_root(p.root), None
    try:
        s = os.stat(_head_log(root)) if root else None
        gkey = [s.st_size, s.st_mtime_ns] if s else None
    except OSError:
        pass
    # T-3565: lanes share this file with main but each has its own HEAD: one [size, mtime_ns, reverts] per checkout,
    # so neither overwrites the other's on every call
    gits = dict(old["git"]) if isinstance(old.get("git"), dict) else {}
    had = gits.pop(root, None)
    if gkey and isinstance(had, list) and len(had) == 3 and had[:2] == gkey and isinstance(had[2], int):
        rev = had[2]
    else:
        rev = _reverts(p.root, day) if root else 0
    if gkey:
        gits[root] = gkey + [rev]
    if not ok or n != saved or gits != old.get("git") or off - old.get("off", 0) > 65536:
        try:  # most calls write nothing: the next process rereads the few lines since
            c.write_atomic(cpath, json.dumps({"day": day, "off": off, "line": line, "mark": mark, "n": n,
                                              "git": gits}))
        except OSError:
            pass
    return {**n, "revert": rev}


def _head_log(root):
    """HEAD's reflog. T-3565: a linked worktree's .git is a file naming its real git dir (<common>/worktrees/<name>)."""
    g = os.path.join(root, ".git")
    if os.path.isfile(g):
        with open(g, encoding="utf-8", errors="replace") as f:
            line = f.readline(4096)
        if line.startswith("gitdir:"):
            g = os.path.join(root, line[7:].strip())
    return os.path.join(g, "logs", "HEAD")


_UNDOES = c.lazy(r"This reverts commit ([0-9a-f]{7,40})")


def _reverts(root, day):
    """Today's reverts that still stand. T-3492: a revert a later commit undid (a re-land citing 'This reverts commit
    <its sha>') was a false alarm, and the undoing commit is no revert of anyone's work: neither spends points."""
    log = c._git(root, "log", f"--since={day} 00:00:00 +0000", "--format=%H%x1f%s%x1f%b%x1e", fail="")
    rows = [r.strip("\n").split("\x1f") for r in log.split("\x1e") if r.strip()]
    undone = {m for _, _, body in (r for r in rows if len(r) == 3) for m in _UNDOES.findall(body)}
    return sum(1 for r in rows if len(r) == 3 and r[1].startswith("Revert ") and not r[1].startswith('Revert "Revert')
               and not any(r[0].startswith(u) for u in undone))


def slowed(p, meta=None):
    """Why today's risk budget slows autonomy, or None."""
    try:
        s = status(p, meta)
    except Exception:  # an unreadable ledger never changes autonomy
        return None
    if not s["slowed"]:
        return None
    what = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in s["counts"].items() if n)
    return f"today's risk budget is spent ({s['spent']:g} of {s['cap']:g}: {what}); autonomy steps down a notch until tomorrow"


def collisions(p, days=7):
    """T-1875: the last days' guard blocks by rule, most first: [{rule, category, n, spends, detail, rewrite}], spends
    when its category counts against the budget, detail the newest, rewrite the guard's hint for it ("" when none)."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    n, last = {}, {}
    for e in c.ledger_events(p, kinds=["guard_block"], since=since):
        d = e.get("data") or {}
        r = str(d.get("rule") or d.get("category") or "?")
        n[r], last[r] = n.get(r, 0) + 1, d
    if not n:
        return []
    import fmguard  # about 12 ms, at SessionStart only, and only when the week had a block
    rows = []
    for r in sorted(n, key=lambda r: -n[r]):
        cat, detail = str(last[r].get("category") or ""), str(last[r].get("detail") or "")
        rows.append({"rule": c.plain(r), "category": cat, "n": n[r], "spends": cat in DESTRUCTIVE,
                     "detail": c.defang(c.fit(c.redact(c.plain(detail)), 120)),
                     "rewrite": fmguard._hint(detail, cat).strip() or (  # T-1897: the live brief block's own route
                         "Start one: fm task new \"<title>\" --type FIX --tier S --ac \"<done when>\" --step \"<step>\" "
                         "--focus, or fm focus ID as its own command." if cat == "brief" else "")})
    return rows


def collision_line(p, least=3):
    """T-1875: one SessionStart item for the week's most-hit guard rule when it fired least times or more, else None;
    fm risk has the rewrite (T-2117)."""
    top = next(iter(collisions(p)), None)
    if not top or top["n"] < least:
        return None
    return c.fit(f"{top['rule']} blocked {top['n']}× in 7 days → fm risk", 160)


PENDING, FORGIVE_S, SAME = "risk_pending.json", 600, 0.45  # a rewrite must come within ten minutes and read alike
# T-3812 (security review of T-3752): only categories whose false refusals have an obvious rewrite are forgiven
# (credentials, system, self-authorize and confine blocks are rarely false, and probing them must keep costing), and
# a session is forgiven FORGIVE_DAY times a day at most
FORGIVABLE, FORGIVE_DAY = {"rm-outside", "git-destructive", "pipe-shell"}, 3


def note_block(p, sid, tool, cmd, category):
    """T-3752: remember this session's latest destructive block, for `forgive` to settle."""
    if not sid or category not in DESTRUCTIVE:
        return
    path = os.path.join(p.dir, PENDING)
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        rec = {}
    rec = rec if isinstance(rec, dict) else {}
    kept = {k: v for k, v in rec.items() if k != "forgiven" and isinstance(v, dict) and time.time() - v.get("t", 0) < FORGIVE_S}
    if isinstance(rec.get("forgiven"), dict):  # T-3812: the day's forgiveness counts outlive each pending block
        kept["forgiven"] = {k: v for k, v in rec["forgiven"].items() if k.startswith(_day())}
    rec = kept
    rec[str(sid)[:80]] = {"t": time.time(), "tool": tool, "cmd": str(cmd)[:400], "category": category}
    c.write_atomic(path, json.dumps(rec))


def forgive(p, sid, tool, cmd):
    """T-3752: an allowed call from the session that was just blocked, of the same tool and reading alike, is the rewrite
    the hint asked for: the block was a false refusal, so it stops costing a point (only a block that stays refused
    counts). One stat per allowed call when nothing is pending."""
    path = os.path.join(p.dir, PENDING)
    if not sid or not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        hit = rec.get(str(sid)[:80])
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(hit, dict) or time.time() - hit.get("t", 0) >= FORGIVE_S or hit.get("tool") != tool:
        return False
    if hit.get("category") not in FORGIVABLE or str(cmd).split()[:1] != str(hit.get("cmd", "")).split()[:1]:
        return False  # T-3812: a sure category, or another program: no rewrite of the blocked call
    day = rec.get("forgiven") if isinstance(rec.get("forgiven"), dict) else {}
    key = f"{_day()} {str(sid)[:80]}"
    if day.get(key, 0) >= FORGIVE_DAY:
        return False
    import difflib
    if difflib.SequenceMatcher(None, str(cmd)[:400], hit.get("cmd", ""), autojunk=False).ratio() < SAME:
        return False
    del rec[str(sid)[:80]]
    rec["forgiven"] = {k: v for k, v in day.items() if k.startswith(_day())} | {key: day.get(key, 0) + 1}
    c.write_atomic(path, json.dumps(rec))
    c.log_event(p, "guard_forgiven", data={"category": hit.get("category")}, session=sid)
    return True


def cmd_risk(args):
    from sampo import cli
    p = cli.resolve(args)
    if args.action in ("off", "on"):  # T-3752: off is the user's (the guard refuses an agent's); the mod writes it too
        path = os.path.join(p.dir if args.here else c.state_dir(), OFF)
        if args.action == "off":
            c.write_atomic(path, json.dumps({"on": True, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                             "via": "cli"}))
        elif os.path.exists(path):
            os.remove(path)
        c.log_event(p, "risk_off" if args.action == "off" else "risk_on", data={"here": bool(args.here)})
        return cli.out(args, {"off": off(p)}, off_line(p) or "risk budget on")
    if args.action == "set":
        if args.day is None or not (math.isfinite(args.day) and args.day >= 0):
            raise cli.UsageError("sampo risk set --day N --because \"<why>\" (N ≥ 0 points a day)")
        if not args.because:
            raise cli.UsageError("fm risk set needs --because \"<why>\" (it is logged)")
        old = cap(p)
        c.update_meta(p, risk_cap=args.day)
        c.log_event(p, "risk_cap", data={"from": old, "to": args.day, "because": c.fit(c.plain(args.because), 200)})
        return cli.out(args, {"cap": args.day}, f"risk cap {old:g} → {args.day:g} points a day")
    s = status(p)
    lines = [f"Risk today: {s['spent']:g} of {s['cap']:g} points" + (" — slowed" if s["slowed"] else "")]
    if s["off"]:
        lines.insert(0, off_line(p))
    lines += [f"  {n} {k} × {WEIGHTS[k]}" for k, n in s["counts"].items() if n]
    if s["slowed"]:
        lines.append("  " + slowed(p))
    s["collisions"] = collisions(p)  # T-1875: a habit costs rewrites even where it spends no risk
    if s["collisions"]:
        lines.append("Guard collisions, 7 days (the rewrite that passes):")
        lines += [f"  {h['n']}× {h['rule']}" + (" (spends risk)" if h["spends"] else "")
                  + f" — {c.fit(h['rewrite'], 160) or 'last: ' + h['detail']}"
                  for h in sorted(s["collisions"], key=lambda h: not h["spends"])[:8]]  # T-1972: what spent it first
    return cli.out(args, s, "\n".join(lines))
