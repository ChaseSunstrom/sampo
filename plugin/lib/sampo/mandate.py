"""fm mandate (T-1400): a sweeping request kept as one standing record (its text, its clauses, an end date, a spend
bound) that fm scout and fm night read. While it stands, what scout captures says it was found under it; once it ends,
scout captures one decision (renew it or let it lapse) and nothing more under it until the user renews or clears it."""
import argparse
import json
import statistics
import time

from sampo import core as c


def parts(p):
    """T-3334: the drift fingerprint's parts, each a short hash ('' for none): the mandate, the root gen-0 seal, the
    towers' gen-0s, the user's steers, the standing-intent file and the plugin version. Claims carry them (claims.py)."""
    import hashlib
    import os
    from sampo import rune

    def h(x):
        return hashlib.sha256(x.encode("utf-8", "replace")).hexdigest()[:12] if x else ""

    def file(path):
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:12]
        except OSError:
            return ""
    m = c.read_meta(p).get("mandate")
    tdir = os.path.join(rune.root(p), "towers")
    towers = [f"{n}:{file(os.path.join(tdir, n, 'gen-0.md'))}"
              for n in (sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []) if os.path.isdir(os.path.join(tdir, n))]
    try:
        with open(os.path.join(c.PLUGIN_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            ver = str(json.load(f).get("version", ""))
    except (OSError, ValueError):
        ver = ""
    tower, rune._TOWER = rune._TOWER, None  # the root's seal, even inside a --tower process
    try:
        seal = rune.seal(p)[:12]
    finally:
        rune._TOWER = tower
    # the text only: `until` is a local-day end that differs by time zone
    return {"mandate": h(m.get("text", "")) if isinstance(m, dict) and m.get("text") else "",
            "seal": seal, "towers": h("\n".join(towers)),
            "steers": h("\n".join(f"{d} {t}" for d, t in c.steer_rules(p))),
            "intent": file(rune.INTENT), "plugin": ver}


def get(p):
    """The project's mandate with `expired` worked out, or None."""
    m = c.read_meta(p).get("mandate")
    if not isinstance(m, dict) or not m.get("text"):
        return None
    until = c.parse_ts(m.get("until"))  # a datetime, or None for a missing or damaged one (never ends)
    return dict(m, expired=bool(until and until.timestamp() <= time.time()))


def day(iso):
    """T-2135: the local day an until falls on, the day the user typed (it's stored as the end of that day in UTC, so
    its first 10 characters read a day late west of UTC); '?' for a missing or damaged one."""
    t = c.parse_ts(iso)
    return time.strftime("%Y-%m-%d", time.localtime(t.timestamp())) if t else "?"


def _archive(meta, status):
    """T-2135: a goal with items is kept in goal_history (the last 20) when anything replaces, overwrites or clears it."""
    old = meta.get("mandate")
    if isinstance(old, dict) and old.get("items"):
        meta["goal_history"] = (meta.get("goal_history") or [])[-19:] + [dict(old, status=status, replaced_at=c.now())]


def put(p, text, until, usd=None, status="overwritten"):
    """Set the mandate: text, its clauses (fm clauses), until (epoch seconds; None for a goal with no end date), an
    optional USD bound. An old goal with items is archived first (status says how it went)."""
    from sampo import recall
    m = {"text": c.plain(text).strip(), "clauses": recall.clauses(text)[:20], "until": c.iso(until) if until else None,
         "set_at": c.now(), **({"usd": usd} if usd is not None else {})}
    if (lines := done_when_lines(text)):  # T-2557: from the raw text; plain() drops the newlines that separate them
        m["done_when"] = [c.fit(c.plain(x), 200) for x in lines[:20]]
    with c.lock(p.dir):
        meta = c.read_meta(p)
        _archive(meta, status)
        meta["mandate"] = m
        meta.pop("goal_verdict", None)  # T-2137: a verdict belongs to the goal it judged
        c.write_meta(p, meta)
    c.log_event(p, "mandate", data={"text": c.fit(m["text"], 200), "until": m["until"]})
    return m


def clear(p):
    with c.lock(p.dir):
        meta = c.read_meta(p)
        _archive(meta, "cleared")
        had = meta.pop("mandate", None)
        meta.pop("goal_verdict", None)
        c.write_meta(p, meta)
    return had


def lapse_finding(m):
    """The one decision an ended mandate leaves: a scout Finding keyed by when it was set, so it's captured once."""
    from sampo import scout
    return scout.Finding(f"mandate:{m.get('set_at')}", f"Mandate ended: {c.fit(m['text'], 90)}", "RESEARCH", "S",
                           f"the standing mandate set {day(m.get('set_at'))} ended {day(m.get('until'))}; "
                           "nothing more is captured under it. Decide: renew it (sampo mandate set \"<text>\" --until "
                           "YYYY-MM-DD) or let it lapse (fm mandate off).")


def _until(day):
    from sampo import cli
    try:
        t = time.mktime(time.strptime(day, "%Y-%m-%d")) + 86400 - 1  # the end of that local day
    except (TypeError, ValueError):
        raise cli.UsageError(f"--until takes a date, YYYY-MM-DD, got {day!r}")
    if t <= time.time():
        raise cli.UsageError(f"--until {day} has already passed: give a later date")
    return t


def cmd_mandate(args):
    from sampo import cli
    p = cli.resolve(args)
    if args.action == "set":
        if not args.text or not args.until:
            raise cli.UsageError('fm mandate set "<the sweeping request>" --until YYYY-MM-DD [--usd N]')
        m = put(p, " ".join(args.text), _until(args.until), args.usd)
        return cli.out(args, m, f"Mandate set until {day(m['until'])}: {c.fit(m['text'], 120)} "
                                  f"({len(m['clauses'])} clause(s)); fm scout and fm night read it.")
    if args.action == "off":
        had = clear(p)
        return cli.out(args, {"cleared": bool(had)}, "Mandate cleared." if had else "No mandate was set.")
    m = get(p)
    if not m:
        return cli.out(args, {"mandate": None}, 'No mandate. fm mandate set "<request>" --until YYYY-MM-DD sets one.')
    if m.get("items") or not m.get("until"):  # T-2135: a goal speaks as one (read-only here: no checks run, exit 0)
        show(p, args, key="mandate", run=False)
        return 0
    state = "ended" if m["expired"] else "stands"
    return cli.out(args, {"mandate": m}, "\n".join(
        [f"Mandate {state} (until {day(m.get('until'))}): {m['text']}"] + [f"  - {x}" for x in m["clauses"][:10]]))


# T-2135: fm goal, the mandate grown into a ledger: items (text, source, a done-when check, the tasks that build it).
# One standing goal; --replace archives the old one under goal_history. Links are made, never inferred.
def standing(p):
    """The standing goal, or a UsageError saying why there's none to add to (an ended one is the user's to renew)."""
    from sampo import cli
    m = get(p)
    if not m:
        raise cli.UsageError('no goal stands: fm goal set "<the request>" [--until YYYY-MM-DD] [--usd N] sets one')
    if m["expired"]:
        raise cli.UsageError(f"the goal ended {day(m.get('until'))}; renewing or replacing it is the user's "
                               "call (T-2186): they run sampo mandate set \"<text>\" --until YYYY-MM-DD, or sampo mandate off")
    return m


def item(p, n):
    """Item n (1-based) of the standing goal, or a UsageError."""
    from sampo import cli
    items = standing(p).get("items") or []
    if not str(n).isdecimal() or not str(n).isascii() or not 1 <= int(n) <= len(items):
        raise cli.UsageError(f"the goal has no item {n} (it has {len(items)}): fm goal item TEXT --done-when CMD")
    return items[int(n) - 1]


def set_goal(p, text, until=None, usd=None, replace=False):
    from sampo import cli
    old = get(p)
    if old and old["expired"]:
        standing(p)  # raises: the user renews an ended goal
    if old and not replace:
        raise cli.UsageError(f"a goal stands ({c.fit(old['text'], 80)}): --replace archives it with its items")
    m = put(p, text, until, usd, status="replaced")
    c.log_event(p, "goal_set", data={"text": c.fit(m["text"], 200), "until": m["until"], "replaced": bool(old)})
    return m


def _locked_items(meta):
    from sampo import cli
    m = meta.get("mandate")
    if not isinstance(m, dict) or not m.get("text"):  # cleared since standing() looked
        raise cli.UsageError("no goal stands any more")
    return m.setdefault("items", [])


def add_item(p, text, done_when, source=None):
    from sampo import cli
    standing(p)
    done_when = c.plain(done_when or "").strip()
    if not done_when:
        raise cli.UsageError("--done-when needs a check command (an empty one would always pass)")
    cli._refuse_unrunnable(p, [done_when])
    cli.guard_stored(p, None, done_when)  # T-0901: refused now, not at the first fm goal show
    with c.lock(p.dir):
        meta = c.read_meta(p)
        items = _locked_items(meta)
        it = {"n": len(items) + 1, "text": c.plain(text).strip(), "source": c.plain(source or "").strip() or None,
              "done_when": done_when, "tasks": []}
        items.append(it)
        c.write_meta(p, meta)
    c.log_event(p, "goal_item", data={"n": it["n"], "text": c.fit(it["text"], 200)})
    return it


def link(p, n, tid):
    """Task tid builds item n: listed on the item, stamped goal: n on its brief."""
    from sampo import cli
    item(p, n)
    b = c.find_brief(p, tid)
    if not b:
        raise cli.UsageError(f"no task {tid}")
    with c.lock(p.dir):
        meta = c.read_meta(p)
        items = _locked_items(meta)
        for other in items:  # a task builds one item: a new link moves it
            other["tasks"] = [t for t in other["tasks"] if t != b.id]
        items[int(n) - 1]["tasks"].append(b.id)
        c.write_meta(p, meta)
    b.meta["goal"] = str(int(n))
    c.save_brief(p, b, touch=False)
    c.log_event(p, "goal_link", task=b.id, data={"item": int(n)})


CHECK_S, CHECKS_S = 300, 900  # one done-when check, and all of one show's together


def drift(p, m, run=True):
    """(outside, uncovered, unmet, unchecked): open tasks under way (not inbox captures) made since the goal was set
    that build no item; items with no task that still exists; items whose tasks all closed while their done-when fails,
    as (item, exit code or 'refused'); items whose check wasn't run (run=False, or past CHECKS_S). A check gets the
    guard's checks first (T-0901), as every stored command fm runs does."""
    from sampo import cli
    items = m.get("items") or []
    briefs = {b.id: b for b in c.load_briefs(p, include_archive=True)}
    linked = {t for it in items for t in it["tasks"]}
    outside = [b for b in briefs.values() if b.status not in c.CLOSED and b.status != "captured" and b.id not in linked
               and str(b.meta.get("created") or "") >= str(m.get("set_at") or "")]
    unmet, unchecked, deadline = [], [], time.monotonic() + CHECKS_S
    for it in items:
        mine = [t for t in it["tasks"] if t in briefs]
        if not mine or not all(briefs[t].status in c.CLOSED for t in mine):
            continue
        if not run or time.monotonic() >= deadline:
            unchecked.append(it)
            continue
        try:
            cli.guard_stored(p, None, it["done_when"])
        except c.PolicyError:
            unmet.append((it, "refused by the guard"))
            continue
        code, _ = c.run_command(p.root, it["done_when"], min(CHECK_S, max(1, deadline - time.monotonic())))
        if code != 0:
            unmet.append((it, f"exit {code}"))
    uncovered = [it for it in items if not any(t in briefs for t in it["tasks"])]
    return sorted(outside, key=lambda b: b.id), uncovered, unmet, unchecked


def _span(mins):
    return f"{mins // 60}h" + (f" {mins % 60}m" if mins % 60 else "") if mins >= 60 else f"{mins}m"


def estimate(p, m, briefs):
    """T-2145: time left from the goal's own history: each done item's minutes are its linked tasks' active minutes
    (the task_done event's `minutes`, last close per task); the left time is the open items times the median, with the
    25th-75th percentile as the range. left is None under MIN_DONE sampled items. None without items."""
    items = m.get("items") or []
    if not items:
        return None
    mine = {t for it in items for t in it.get("tasks") or []}
    mins = {}
    for e in c.ledger_events(p, ("task_done",)):
        v = (e.get("data") or {}).get("minutes")
        if e.get("task") in mine and isinstance(v, (int, float)) and not isinstance(v, bool):
            mins[e["task"]] = v  # oldest first: the last close wins
    sample = [sum(mins[t] for t in it["tasks"] if t in mins) for it in items
              if item_state(it, briefs) == "done" and any(t in mins for t in it["tasks"])]
    left = sum(item_state(it, briefs) != "done" for it in items)
    out = {"n": len(sample), "remaining": left, "left": None}
    if len(sample) >= 3:
        q = statistics.quantiles(sample, n=4, method="inclusive")
        out.update(median=round(statistics.median(sample)), p25=round(q[0]), p75=round(q[2]))
        out.update(left=out["median"] * left, low=out["p25"] * left, high=out["p75"] * left)
    return out


def estimate_line(e):
    if not e or not e["remaining"]:
        return None
    if e["left"] is None:
        return f"Time left: no estimate yet (needs 3 done items with recorded minutes; {e['n']} so far)"
    return (f"Time left: about {_span(e['left'])} left (median {_span(e['median'])} per item over {e['n']} done items, "
            f"{_span(e['low'])}–{_span(e['high'])})")


def show(p, args, key="goal", run=True):
    from sampo import cli
    m = get(p)
    if not m:
        return cli.out(args, {key: None}, 'No goal. fm goal set "<the request>" [--until YYYY-MM-DD] sets one.')
    if not m.get("items") and m.get("until"):  # a plain mandate prints as it always did
        return cmd_mandate(argparse.Namespace(**dict(vars(args), action="show")))
    outside, uncovered, unmet, unchecked = drift(p, m, run) if m.get("items") else ([], [], [], [])
    briefs = {b.id: b for b in c.load_briefs(p, include_archive=True)}
    state = "ended" if m["expired"] else "stands"
    when = f"until {day(m['until'])}" if m.get("until") else "no end date"
    money = f"budget ${m['usd']:g}" if m.get("usd") is not None else "no budget"
    lines = [f"Goal {state} ({when}, {money}): {m['text']}"] + [f"  - {x}" for x in m["clauses"][:10]]
    if m.get("items"):
        lines += ["Items:"] + [
            f"  {it['n']}. {it['text']}: " + (", ".join(f"{t} ({briefs[t].status if t in briefs else 'gone'})"
                                                         for t in it["tasks"]) or "no task")
            + f" · done when: {c.plain(it['done_when'])}" for it in m["items"]]
        lines += ["Drift: none." if not (outside or uncovered or unmet) else "Drift:"]
        if uncovered:
            lines += [f"  items with no task ({len(uncovered)}): " + "; ".join(f"{i['n']}. {i['text']}" for i in uncovered)]
        if outside:
            lines += [f"  outside the goal ({len(outside)}): " + "; ".join(f"{b.id} {c.fit(b.title, 60)}" for b in outside)]
        if unmet:
            lines += [f"  claimed, not met ({len(unmet)}): " + "; ".join(
                f"{i['n']}. {i['text']}: its tasks closed and its done-when fails ({how})" for i, how in unmet)]
        if unchecked:
            lines += [f"  closed, check not run ({len(unchecked)}; fm goal show runs them): "
                      + "; ".join(f"{i['n']}. {i['text']}" for i in unchecked)]
        est = estimate(p, m, briefs)
        if estimate_line(est):
            lines.append(estimate_line(est))
    else:
        est = None
    cli.out(args, {key: m, "estimate": est, "drift": {"outside": [b.id for b in outside], "uncovered": [i["n"] for i in uncovered],
                                       "unmet": [i["n"] for i, _ in unmet],
                                       "unchecked": [i["n"] for i in unchecked]}}, "\n".join(lines))
    return 1 if outside or uncovered or unmet else 0


RED_KEEP = 500  # T-2416: cached start-tree results kept in meta (a base never changes; past this the cache restarts)


def start_tree(p, it, briefs):
    """T-2416: the tree the item's work started from: the earliest start (c.task_base) among its done tasks; None when
    none has one any more (never focused, or git pruned the snapshot)."""
    bases = sorted((str(b.meta.get("created") or ""), b.id, base) for t in it["tasks"] if (b := briefs.get(t))
                   and b.status == "done" and (base := c.task_base(p.root, b)))
    return bases[0][2] if bases else None


def red_before(p, cmd, base, cache, deadline):
    """T-2416: None when cmd fails on the tree base (the done-when tests the item's work); else why it doesn't count.
    Runs guarded (fmcli._run_at, as fm task prove --vacuous does), CHECK_S at most; the result is kept in cache,
    keyed by (cmd, base) as "it passed there", unless the run timed out or couldn't be made."""
    from sampo import cli
    import hashlib
    key = hashlib.sha256(f"{cmd}\0{base}".encode()).hexdigest()[:16]
    if key not in cache:
        left = deadline - time.monotonic()
        if left <= 0:
            return "is unproven: its check wasn't run on the start tree (out of time)"
        try:
            code, _ = cli._run_at(p, base, cmd, min(CHECK_S, max(1, left)))
        except c.PolicyError:
            return "check refused by the guard"
        except cli.UsageError as e:
            return f"is unproven: its check couldn't run on the start tree ({c.fit(str(e), 80)})"
        if code == 124:
            return "is unproven: its check timed out on the start tree"
        cache[key] = code == 0
    return "check passes before the work: it proves nothing" if cache[key] else None


def verdict(p, m):
    """T-2137: the causes a goal isn't finished (empty: finished). Reuses drift's guarded checks; deterministic."""
    import re
    from sampo import recall
    items = m.get("items") or []
    briefs = {b.id: b for b in c.load_briefs(p, include_archive=True)}
    _, uncovered, unmet, unchecked = drift(p, m)
    causes = [] if items else ["the goal has no items"]
    causes += [f"item {i['n']} has no task" for i in uncovered]
    causes += [f"item {i['n']} has {w}" for i in items if (w := {"open": "open tasks", "no done task": "no done task"}.get(
        item_state(i, briefs)))]
    causes += [f"item {i['n']} check fails ({how})" for i, how in unmet]
    causes += [f"item {i['n']} check not run" for i in unchecked]
    # T-2416 review: an item drift refused or didn't reach already has its cause; never run its check here instead,
    # where the active task's grants would apply
    unmet_n = {i["n"] for i, _ in unmet} | {i["n"] for i in unchecked}
    red = dict(c.read_meta(p).get("goal_red") or {})
    seen, deadline = len(red), time.monotonic() + CHECKS_S
    for i in items:
        if item_state(i, briefs) != "done" or i["n"] in unmet_n:  # no cause to add: another cause names it
            continue
        base = start_tree(p, i, briefs)
        if base:  # T-2416: the done-when counts when it fails on the tree the item's work started from
            why = red_before(p, i["done_when"], base, red, deadline)
            causes += [f"item {i['n']} {why}"] if why else []
            continue
        # no start tree to run it on: T-2332's format check, and the item is named unproven, never finished
        causes.append(f"item {i['n']} is unproven: no start tree to run its check on")
        hollow = sorted((x for x in c.lint_verify(i["done_when"], p.root) if "can't fail" in x or "piped program" in x),
                        key=lambda x: "piped program" not in x)  # a pipe is the more specific reason
        causes += [f"item {i['n']} check can't fail ({re.sub(r'^it can.t fail [(](.*)[)]$', r'\1', hollow[0])})"] if hollow else []
    if len(red) != seen:  # a base never changes: only a run that ended is kept (a timeout may pass next time)
        c.update_meta(p, goal_red=red if len(red) <= RED_KEEP else {})
    # a clause is accounted when an item (text, done-when, task titles) shares 2 of its words (1 if it has one)
    bags = [set(recall.words(" ".join([i["text"], i["done_when"]] + [briefs[t].title for t in i["tasks"] if t in briefs])))
            for i in items]
    for x in m.get("clauses") or []:
        cw = set(recall.words(x))
        if not any(len(cw & b) >= min(2, len(cw)) for b in bags):
            causes.append(f"clause unaccounted: {c.fit(x, 80)}")
    # T-2557: a stated Done-when line counts only when an item's done-when command names it (2 of its words, with
    # identifiers split at _ / . -), not when an item's prose shares them: the long tier's sessions claimed done with
    # stated lines no check tested (research long-false-done)
    cmds = [set(recall.words(re.sub(r"[_/.-]+", " ", i.get("done_when") or ""))) for i in items]
    for x in m.get("done_when") or []:
        cw = set(recall.words(x))
        if cw and not any(len(cw & b) >= min(2, len(cw)) for b in cmds):
            causes.append(f"done-when line unchecked: {c.fit(x, 80)}")
    runs = c.ledger_events(p, kinds=["check_run"])
    last = runs[-1].get("data") or {} if runs else None
    if last is None:
        causes.append("gates never run (fm check)")
    elif last.get("tree") != c.worktree_id(p.root):
        causes.append("gates not run on this tree (fm check)")
    elif any((r or {}).get("exit") for r in last.get("results") or []):
        causes.append("gates red (fm check)")
    elif last.get("skip_tests"):  # T-2332: green only because red-first tests were left out; named, so whose shows
        causes.append(f"gates skipped red tests: {c.fit(', '.join(map(str, last['skip_tests'])), 120)} (fm check)")
    return causes


def cmd_verdict(p, args):
    from sampo import cli
    m = get(p)
    if not m or m["expired"]:
        c.update_meta(p, goal_verdict=None)
        cli.out(args, {"verdict": None}, "no goal")
        return 2  # finished is 0, stopped 1
    causes = verdict(p, m)
    word = "finished" if not causes else "stopped"
    c.log_event(p, "goal_verdict", data={"verdict": word})  # T-2136: the done claim fm goal plan's checklist reads
    # the tree is the worktree id; root lets Stop (which runs no git) tell another checkout's verdict from this one's
    c.update_meta(p, goal_verdict={"verdict": word, "causes": causes[:10], "at": c.now(),
                                   "tree": c.worktree_id(p.root) or "", "root": p.root,
                                   "set_at": m.get("set_at")})
    cli.out(args, {"verdict": word, "causes": causes}, word + (": " + "; ".join(causes) if causes else ""))
    return 1 if causes else 0


PLAN_SYSTEM = ("List best-in-class contents for a goal like the user's, one per line, each starting with '- ', at most 10, "
               "each naming a file path, a surface of the project or an `fm` verb it touches. No other text.")


def _norm(text):
    import re
    return re.sub(r"\s+", " ", c.plain(text or "")).strip().lower().rstrip(".,;:!")


def _grounded(root, line, found, files, verbs):
    """T-2136: item text built from what the line names that the repo has, never the line itself: a tracked path (a /
    or an extension, inside the repo by realpath), a surface found (whole word), or an fm verb; else None."""
    import os
    import re
    top = os.path.realpath(root)
    for t in (x.rstrip(".,;:)") for x in re.findall(r"[\w./-]+", line)):
        if ("/" in t or re.search(r"\.\w+$", t)) and ".." not in t and not t.startswith("/") and (
                t in files or any(f.startswith(t.rstrip("/") + "/") for f in files)) and os.path.realpath(
                os.path.join(root, t)).startswith(top + os.sep):
            return f"cover {t}"
    for v in re.findall(r"\b(?:fm|sampo) ([a-z][\w-]*)", line.lower()):
        if v in verbs:
            return f"fm {v} works for the goal"
    for sf in found:
        if re.search(rf"\b{re.escape(sf)}\b", line, re.I):
            return f"works on the {sf} surface"
    return None


def _target(text):
    """T-2913: what an item covers: ('surface', S), ('path', P) or ('verb', V), else None."""
    import re
    for kind, rx in (("surface", r"works on the (.+) surface$"), ("path", r"cover (\S+)$"), ("verb", r"fm (\S+) works")):
        if m := re.search(rx, text):
            return kind, m[1]
    return None


def _dedupe(items):
    """T-2913: items in order, none twice by normalized text; a model item whose target (surface, path or verb) an
    earlier item already has is dropped too."""
    seen, targets, out = set(), set(), []
    for i in items:
        n, t = _norm(i["text"]), _target(_norm(i["text"]))
        if n in seen or (i["tag"] == "model" and t and t in targets):
            continue
        seen.add(n)
        targets.add(t)
        out.append(i)
    return out


DOC_CAP = 200_000
DOC_ITEMS = 40
DOC_CHECKS = (("exam", "sampo exam scorecard"), ("sweep", "sampo rune check"))  # a clause's word -> the Sampo check it names


def _doc_path(text, roots):
    """T-2913: the first file the goal's words name that resolves (realpath) inside a root: a repo-relative or
    research-relative .md/.txt token; a link, an absolute or '..' path, or a symlink out is never read."""
    import os
    import re
    for tok in re.findall(r"[^\s()\[\]<>\"'`,;]+\.(?:md|txt)\b", text):
        if "://" in tok or tok.startswith(("/", "~")) or ".." in tok.split("/"):
            continue
        for root in roots:
            f = os.path.realpath(os.path.join(root, tok))
            if os.path.isfile(f) and any(f.startswith(os.path.realpath(r) + os.sep) for r in roots):
                return tok, f
    return None, None


def _section(body, text, tok):
    """The lines of the section the goal names after the file (§N), else the whole file."""
    import re
    m = re.search(re.escape(tok) + r"\W{0,3}§\s*(\d+)", text) or re.search(r"§\s*(\d+)", text)
    if not m:
        return body.splitlines()
    out, level = [], 0
    for ln in body.splitlines():
        h = re.match(r"(#{1,6})\s*(\d+)[.\s]", ln)
        if h and not level and h[2] == m[1]:
            level = len(h[1])
        elif level and (h2 := re.match(r"(#{1,6})\s", ln)) and len(h2[1]) <= level:
            break
        elif level:
            out.append(ln)
    return out


def _clauses(line):
    """A DONE-WHEN line split on its top-level commas (a leading 'and' dropped), cut at the first sentence end."""
    import re
    line = re.split(r"\.\s", line, 1)[0].rstrip(".")
    out, depth, cur = [], 0, ""
    for ch in line:
        depth += (ch in "([") - (ch in ")]")
        if ch == "," and depth <= 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return [x for x in (re.sub(r"(?i)^and\s+", "", s.strip()) for s in out + [cur]) if x]


def doc_items(text, roots):
    """T-2913: [(item text, check or None)] from the document a goal's words cite: one per top-level numbered entry
    of the named section (§N; the whole file when none, bullets if nothing is numbered) and one per DONE-WHEN clause
    of the file, the clause's own Sampo check where its words name one. Pure: no model, no write."""
    import re
    tok, f = _doc_path(text, roots)
    if not f:
        return []
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            body = c.plain_lines(fh.read(DOC_CAP))
    except OSError:
        return []
    lines = _section(body, text, tok)
    ent = [ln for ln in lines if re.match(r"\d+[.)]\s+\S", ln)] or [ln for ln in lines if re.match(r"[-*]\s+\S", ln)]
    out = []
    for ln in ent:
        s = re.sub(r"^(?:\d+[.)]|[-*])\s+", "", ln)
        b = re.match(r"\*\*(.+?)\*\*", s)
        out.append((c.fit((b[1] if b else re.split(r"(?<=[.!?])\s", s, 1)[0]).strip().rstrip(".:"), 120), None))
    for ln in body.splitlines():
        if (d := re.match(r"(?i)done[ -]when:\s*(.+)", ln)):
            for cl in _clauses(d[1]):
                out.append((c.fit(cl, 120), next((chk for w, chk in DOC_CHECKS if w in cl.lower()), None)))
    return out[:DOC_ITEMS]


def _misses(p):
    """T-2136: the top 5 recent past misses: items added after a finished verdict (the goal_verdict event), and tasks
    reopened (a task_set to an open status) within 7 days of their task_done, and (T-2417) the newest done tasks fm
    outcomes says a later fix or a revert touched."""
    t = lambda e: c.parse_ts(e.get("ts"))  # noqa: E731
    out, done, claimed = [], {}, False
    for e in c.ledger_events(p, kinds=["goal_set", "goal_verdict", "goal_item", "task_done", "task_set"]):
        k, d = e.get("event"), e.get("data") or {}
        if not t(e):
            continue  # a malformed ts
        if k == "goal_set":
            claimed = False  # a new goal starts with no done claim
        elif k == "goal_verdict":
            claimed = d.get("verdict") == "finished"
        elif k == "goal_item" and claimed:
            out.append(f"item added after a done claim: {c.fit(c.plain(d.get('text', '')), 80)}")
        elif k == "task_done" and e.get("task"):
            done[e["task"]] = t(e)
        elif k == "task_set" and e.get("task") in done:
            st = d.get("status") or (d.get("changes") or {}).get("status")
            if st and st not in c.CLOSED and (t(e) - done.pop(e["task"])).days < 7:
                out.append(f"{c.plain(e['task'])} reopened within 7 days of closing: check what it missed")
    try:  # T-2417: the done tasks fm outcomes says a later fix or a revert touched, newest last
        from sampo import outcomes
        later = [f"{c.plain(t)} was {o['fate']} by "
                 f"{c.fit(c.plain(c.joinmore(map(str, o.get('by') or []), 2)), 80) or 'a later change'}: "
                 f"check what it missed" for t, o in outcomes.outcomes(p).items()
                 if isinstance(o, dict) and o.get("fate") in ("fixed later", "reverted")]
    except Exception:  # noqa: BLE001 — a history read never costs the plan
        later = []
    return (later[-3:] + list(dict.fromkeys(out))[-3:])[-5:]  # reopens and late items last: rarer, and the user's


def plan(p, m):
    """T-2136: the proposed contents list: {"items": [{tag, text}], "proposals": [ungrounded model lines], "checklist"}."""
    from sampo import ideas
    from sampo import mission
    import os
    found = mission.surfaces(p.root)
    files = c._git(p.root, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "ls-files",
                   timeout=10).splitlines()
    from sampo import cli
    verbs = set(cli.build_parser()._subparsers._group_actions[0].choices)  # once per plan; no public helper
    top = {f.split("/")[0].lower() for f in files}
    docs = doc_items(m["text"], [p.root, p.dir])  # T-2913: the document the goal cites is the user's own list
    items = [{"tag": "user", "text": t, "check": k} for t, k in docs] or [
        {"tag": "user", "text": x} for x in m.get("clauses") or [m["text"]]]
    std = ["tests cover the work"] if any("test" in f.lower() for f in files) else []
    std += [f"{n} updated" for n, k in (("MASTER.md", "master.md"), ("README", "readme.md"), ("CHANGELOG entry", "changelog.md"))
            if k in top]
    std += ["a site page if it is user-facing"] if "site" in top else []
    std += ["a pane or status surface shows its state"] if any(s in found for s in mission.CLIENTS) else []
    std += [f"it works on the {s} surface" for s in found]
    items += [{"tag": "standard", "text": s} for s in std]
    proposals, note = [], None
    if c.read_meta(p).get("sensitive"):
        note = "sensitive project: no model call"
    else:
        try:  # the reply is untrusted: plain text, a line is kept only when grounded
            reply = ideas.run_child("goal-plan", PLAN_SYSTEM, f"Goal: {m['text']}\nSurfaces: {', '.join(found) or 'none'}",
                                      "haiku", 120, project=p.slug)
        except ValueError as e:
            reply, note = "", f"no model items: {c.fit(c.plain(str(e)), 120)}"
        for l in c.plain_lines(reply).splitlines()[:10]:
            if l.strip().startswith("- ") and l.strip()[2:].strip():
                l = c.fit(l.strip()[2:].strip(), 120)
                g = _grounded(p.root, l, found, set(files), verbs)
                (items.append({"tag": "model", "text": g}) if g else proposals.append(l))
    return {"items": _dedupe(items), "proposals": proposals, "checklist": _misses(p), "note": note}


def cmd_plan(p, args):
    from sampo import cli
    m = standing(p)
    if args.apply and not args.done_when:
        raise cli.UsageError('fm goal plan --apply needs --done-when CMD, the check each added item gets')
    got = plan(p, m)
    if args.apply:
        have = {_norm(i["text"]) for i in m.get("items") or []}
        got["added"] = []
        for i in got["items"]:
            if _norm(i["text"]) not in have:
                have.add(_norm(i["text"]))
                try:  # T-2913: a clause's own check, else the floor every item gets
                    it = add_item(p, i["text"], i.get("check") or args.done_when, i["tag"])
                except cli.UsageError:
                    if not i.get("check"):
                        raise
                    it = add_item(p, i["text"], args.done_when, i["tag"])
                got["added"].append(it["text"])
    lines = [f"[{i['tag']}] {i['text']}" for i in got["items"]]
    lines += ["proposals (not added):"] * bool(got["proposals"]) + [f"  {x}" for x in got["proposals"]]
    lines += ["checklist:"] * bool(got["checklist"]) + [f"  {x}" for x in got["checklist"]]
    lines += [got["note"]] if got["note"] else []
    lines += [f"added {len(got['added'])} item(s)"] if args.apply else ["--apply --done-when CMD adds them"]
    return cli.out(args, got, "\n".join(lines))


def cmd_goal(args):
    from sampo import cli
    p = cli.resolve(args)
    words = args.text or []
    if args.action == "plan":
        return cmd_plan(p, args)
    if args.action == "verdict":
        return cmd_verdict(p, args)
    if args.action == "set":
        if not words or not (args.until or args.no_end):  # an endless goal is said so, never a default
            raise cli.UsageError('fm goal set "<the request>" --until YYYY-MM-DD | --no-end [--usd N] [--replace]')
        m = set_goal(p, " ".join(words), _until(args.until) if args.until else None, args.usd, args.replace)
        return cli.out(args, m, f"Goal set ({'until ' + day(m['until']) if m['until'] else 'no end date'}): "
                                  f"{c.fit(m['text'], 120)}; add its contents with fm goal item TEXT --done-when CMD.")
    if args.action == "item":
        if not words or not args.done_when:
            raise cli.UsageError('fm goal item "<what it covers>" --done-when "<check command>" [--source S]')
        it = add_item(p, " ".join(words), args.done_when, args.source)
        return cli.out(args, it, f"Item {it['n']}: {it['text']}; link its tasks with fm goal link {it['n']} ID "
                                   f"or fm task new … --goal {it['n']}.")
    if args.action == "link":
        if len(words) != 2:
            raise cli.UsageError("fm goal link ITEM TASK")
        link(p, words[0], words[1])
        return cli.out(args, {"item": int(words[0]), "task": words[1].upper()},
                         f"{words[1].upper()} builds item {words[0]}.")
    return show(p, args)


def done_when_lines(text):
    """T-2557: the bullets under a 'Done when:' line of a goal's raw text (a blank or other line ends the list)."""
    import re
    out, on = [], False
    for ln in (text or "").splitlines():
        s = ln.strip()
        if re.fullmatch(r"(?i)done[ -]when:?", s):
            on = True
        elif on and s[:2] in ("- ", "* "):
            out.append(s[2:].strip())
        elif on and s:
            on = False
    return out


def item_state(it, by):
    """T-2137, T-2139: the one item-done rule: 'done' when a linked task is done and none is open; 'open' while one is;
    'no done task' when its tasks are all dropped; 'no task' when it has none (a gone task is ignored). by: id -> brief."""
    st = [by[t].status for t in it.get("tasks") or [] if t in by]
    return ("open" if any(s not in c.CLOSED for s in st) else "done" if "done" in st else "no done task") if st else "no task"


def progress(p, briefs=None):
    """T-2139: (goal, items done, next item, its first open brief or None) of the standing goal, or None when none stands,
    it has ended or it has no items. An item is done when a linked task is done and none is still open (dropped ones
    are ignored: an item with only dropped tasks isn't done). Meta and briefs only; briefs: ones the caller loaded."""
    m = get(p)
    items = (m or {}).get("items") or []
    if not m or m["expired"] or not items:
        return None
    by = {b.id: b for b in (briefs if briefs is not None else c.load_briefs(p, include_archive=True))}  # T-2141: fm tidy mustn't move it
    done = lambda it: item_state(it, by) == "done"
    nxt = next((it for it in items if not done(it)), None)
    open_ = next((by[t] for t in (nxt or {}).get("tasks") or [] if t in by and by[t].status not in c.CLOSED), None)
    return m, sum(map(done, items)), nxt, open_


def goal_line(p, briefs=None):
    """T-2139: the one line every surface prints while a goal stands: its text (quoted, it's the user's words), items
    done/total, the next item."""
    g = progress(p, briefs)
    if not g:
        return None
    m, done, nxt, b = g
    tail = f" · next: {nxt['n']}. {c.fit(nxt['text'], 60)} ({b.id if b else 'no task yet'})" if nxt else " · all items done"
    return c.plain(f"Goal: {json.dumps(c.fit(m['text'], 80), ensure_ascii=False)} · {done}/{len(m['items'])} items done{tail}")


def fresh_verdict(p, m=None):
    """T-2141: the stored goal_verdict when it still speaks for the standing goal: the goal stands, set_at matches, it's
    under a day old and this checkout stored it; else None. Meta only: no check, no git."""
    m = m or get(p)
    v = c.read_meta(p).get("goal_verdict")
    if not m or m["expired"] or not isinstance(v, dict) or v.get("set_at") != m.get("set_at") \
            or v.get("root") != p.root:
        return None
    age = c.age_days(v.get("at"))
    return v if age is not None and age <= 1 else None
