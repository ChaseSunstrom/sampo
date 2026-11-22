"""T-5017 (slice of T-4313): sampo localruns scores the local-model runs per arm and gives every stop reason its fix.

A row of local-runs.jsonl is one run of one arm (a local model, a profile) on one task: {arm, task, passed,
criteria_passed, commits, edits, stop, at}. `arm` may be named model or profile, `task` scenario or id, `stop` error,
stop_reason or reason, `at` ts: the reader takes the first it finds. An arm's score is the tasks it finished (its best
run of the task passed) of the tasks measured (every task any arm ran). The best run of a task is the one that
passed, then passed most criteria, made most commits, made most edits. The verb exits 0 when some arm finished
FINISH_BAR of the measured tasks, else 1: the bar T-4313 names.

Every run that did not pass has a stop reason, and classify_stop sorts it: a loop fault (Sampo's own loop is at fault;
the table names the Sampo fix) or a model fault (the model is at fault; the table names the route, a bigger local model
or Haiku for that step). A stop text no rule knows is listed as unclassified, never guessed. The rules read the texts
smith.py raises ("fm code: [model: ]stopped: the model used all N turns", "stopped: out of time (--deadline S s)";
the prefix is taken off first) and the measuring run's own leads for a run the caller's kill ended, "blocked" and an
empty reply, each anchored to the start of the text: a word in a test's output decides nothing. A failed row with no
stop text at all is unclassified ("no stop recorded"), never guessed. The empty-reply fix is marked not built yet.

The rows come from the path argument, else <state>/local-runs.jsonl. Nothing here gates a close or a merge: models
advise, proof decides; this reads a measuring run's result."""
import collections
import json
import os
import re
from datetime import datetime, timezone

from sampo import core as c

FINISH_BAR = 3  # tasks an arm finished of the measured ones, for exit 0
MAX_ROWS = 20000  # rows read: the newest
MAX_LINE = 200000  # characters of one row line read; a longer line is skipped
MAX_CHARS = 20000000  # characters of rows kept in all: the newest, so long lines cannot fill memory

LOOP, MODEL, UNCLASSIFIED = "loop fault", "model fault", "unclassified"

# (key, pattern, kind, what it is, the fix or the route). Each pattern is anchored to the lead clause of the text (after
# Smith's "fm code: [model: ]" prefix is taken off): a word inside a test's output or a traceback decides nothing. The
# first rule that matches wins; a text no rule matches is unclassified. "stopped: ..." is Smith's own (smith.py); the
# forced-stop, blocked and empty-reply leads are the measuring run's rows, not texts Smith raises.
RULES = (
    ("time-limit-forced", r"^time limit forced stop\b", LOOP,
     "the caller's time limit ended the run before the loop stopped itself",
     "Sampo fix: give the run --deadline below the caller's kill, so the loop stops with its trail and --resume carries on"),
    ("turns-used", r"^stopped: the model used all \d+ turns\b", LOOP,
     "all the turns were used before the step was done",
     "Sampo fix: --resume --max-steps N carries on; the explore budget's forced edit and test turns "
     "(forced_edit, forced_test) show whether the loop explored too long"),
    ("out-of-time", r"^stopped: out of time\b", MODEL,
     "the model was too slow for the step inside --deadline",
     "route: Haiku for that step (a bigger local model is slower still)"),
    ("blocked", r"^blocked\b", MODEL,
     "the model could not go on and blocked the task",
     "route: a bigger local model for that step"),
    ("keep-check-red", r"^(?:stopped: )?the keep check `[^`]*`(?: \([^)]*\))? is still red after "
     r"(?:\d+ tries to mend it|the work):", MODEL,
     "the keep check stayed red after the model's edits, through its mend tries or the last round",
     "route: a bigger local model for that step"),
    ("spend-cap", r"^stopped: the run spent \$\S+ of its \$\S+ cap \(--max-usd\)", MODEL,
     "the model spent more than the --max-usd cap before the step was done",
     "route: a cheaper model for that step, or --resume with the higher --max-usd the stop names"),
    ("empty", r"^(?:the model's reply was empty|empty reply)\b", LOOP,
     "the model's reply held no action and the run ended",
     "Sampo fix (not built yet): re-ask once with the offered actions, and force a tool call on the next turn"),
)
_PREFIX = re.compile(r"^(?:(?:fm|sampo) code:\s*)?(?:model:\s*)?", re.I)


def rows_path(path=None):
    """The rows file: the argument, else <state>/local-runs.jsonl."""
    return os.path.abspath(os.path.expanduser(path)) if path else os.path.join(c.state_dir(), "local-runs.jsonl")


def read_rows(path=None, stats=None):
    """The newest rows (dicts) of the file, oldest first: at most MAX_ROWS lines and MAX_CHARS characters; [] for a
    missing or unreadable file. A line that is not JSON, not an object or longer than MAX_LINE is skipped. A dict given
    as stats gets the counts: lines (non-blank lines seen), unreadable (not a JSON object), too_long (a line over
    MAX_LINE), cut (older lines the row or character budget dropped)."""
    p = rows_path(path)
    st = stats if stats is not None else {}
    st.update(lines=0, unreadable=0, too_long=0, cut=0)
    lines, size = collections.deque(), 0
    try:
        if not os.path.isfile(p):
            return []
        with open(p, encoding="utf-8", errors="replace") as f:
            while True:
                ln = f.readline(MAX_LINE + 1)
                if not ln:
                    break
                if len(ln) > MAX_LINE:
                    st["lines"] += 1
                    st["too_long"] += 1
                    while ln and not ln.endswith("\n"):  # drain the rest of the long line without keeping it
                        ln = f.readline(MAX_LINE + 1)
                    continue
                if not ln.strip():
                    continue
                st["lines"] += 1
                lines.append(ln)
                size += len(ln)
                while len(lines) > MAX_ROWS or size > MAX_CHARS:
                    size -= len(lines.popleft())
                    st["cut"] += 1
    except OSError:
        return []
    rows = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            r = None
        if isinstance(r, dict):
            rows.append(r)
        else:
            st["unreadable"] += 1
    return rows


def _first(row, keys, default=""):
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return default


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return 0


def _passed(row):
    v = row.get("passed")
    return v is True or str(v).strip().lower() in ("true", "pass", "passed", "yes", "1")


def _epoch(v):
    """Seconds since the epoch of a number or a date(time) string; None when it is neither. A number counts only as
    epoch seconds between 1e9 and 1e10 (2001 to 2286) or milliseconds between 1e12 and 1e13 (divided by 1000): 2026
    or 20261001 as a number are not seconds, and NaN, infinity and a bool are nothing. A string may also be an ISO
    date or datetime, with or without dashes (20261001, 20261001T120000Z); no timezone means UTC."""
    if isinstance(v, bool) or v is None:
        return None
    s = str(v).strip()
    if not isinstance(v, (int, float)):
        if re.fullmatch(r"\d{8}", s):  # a compact date, not 8 digits of seconds
            try:
                return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                return None
        try:
            n = float(s)
        except ValueError:
            n = None
    else:
        try:
            n = float(v)
        except (OverflowError, ValueError):  # an int too big for a float is no time
            return None
    if n is not None:
        if n != n or n in (float("inf"), float("-inf")):
            return None
        if 1e12 <= n < 1e13:
            n /= 1000.0
        return n if 1e9 <= n < 1e10 else None
    if re.fullmatch(r"\d{8}T\d{6}(?:Z|[+-]\d{2}:?\d{2})?", s):  # the compact datetime fromisoformat takes too
        s = f"{s[:4]}-{s[4:6]}-{s[6:11]}:{s[11:13]}:{s[13:]}"
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _line(v):
    """One printable line: whitespace collapsed, control characters (ESC, BEL, bidi) dropped."""
    return c.plain(" ".join(str(v).split()))


def arm_of(row):
    return _line(_first(row, ("arm", "model", "profile")))


def task_of(row):
    return _line(_first(row, ("task", "scenario", "id")))


def stop_of(row):
    return _line(_first(row, ("stop", "stop_reason", "error", "reason", "why")))


def _rank(row):
    return (_passed(row), _int(row.get("criteria_passed")), _int(row.get("commits")), _int(row.get("edits")))


def classify_stop(text):
    """{key, kind, what, fix} for a stop text: a loop fault with its Sampo fix, a model fault with its route, or
    kind 'unclassified' (key 'unclassified', fix '') for a text no rule knows."""
    t = " ".join(str(text or "").split())
    if not t:  # a failed row with no stop text: nothing says why, so no rule is guessed
        return {"key": UNCLASSIFIED, "kind": UNCLASSIFIED, "what": "no stop recorded", "fix": ""}
    t = t[_PREFIX.match(t).end():].lstrip()  # Smith prints every stop as "fm code: [model: ]stopped: ..."
    for key, pat, kind, what, fix in RULES:
        if re.search(pat, t, re.I):
            return {"key": key, "kind": kind, "what": what, "fix": fix}
    return {"key": UNCLASSIFIED, "kind": UNCLASSIFIED, "what": "no rule knows this stop reason", "fix": ""}


def score(rows, since=None):
    """{arms: [{arm, finished, measured, runs, best: {task: row-summary}}], tasks, stops: [...], unclassified: [...],
    ok, skipped: {no_arm_or_task, before_since}}. since: epoch seconds (or milliseconds) or an ISO date, with or without
    dashes: older rows, and rows whose time can't be read, are left out. A row with no arm or no task is skipped."""
    cut = _epoch(since) if since not in (None, "") else None
    if since not in (None, "") and cut is None:
        raise ValueError(f"since {_line(since)[:60]!r} is not an ISO date (2026-10-01 or 20261001) or epoch seconds")
    use, skipped = [], {"no_arm_or_task": 0, "before_since": 0}
    for r in rows:
        if not isinstance(r, dict):
            continue
        if not arm_of(r) or not task_of(r):
            skipped["no_arm_or_task"] += 1
            continue
        if cut is not None:
            at = _epoch(_first(r, ("at", "ts", "time"), None))
            if at is None or at < cut:
                skipped["before_since"] += 1
                continue
        use.append(r)
    tasks = sorted({task_of(r) for r in use})
    best, runs = {}, {}
    for r in use:
        a, t = arm_of(r), task_of(r)
        runs[a] = runs.get(a, 0) + 1
        if (a, t) not in best or _rank(r) > _rank(best[(a, t)]):
            best[(a, t)] = r
    arms = []
    for a in sorted(runs):
        mine = {t: best[(a, t)] for t in tasks if (a, t) in best}
        arms.append({"arm": a, "finished": sum(1 for r in mine.values() if _passed(r)), "measured": len(tasks),
                     "runs": runs[a],
                     "best": {t: {"passed": _passed(r), "criteria_passed": _int(r.get("criteria_passed")),
                                  "commits": _int(r.get("commits")), "edits": _int(r.get("edits"))}
                              for t, r in mine.items()}})
    groups, unclassified = {}, []
    for r in use:
        if _passed(r):
            continue
        text = stop_of(r)
        v = classify_stop(text)
        if v["kind"] == UNCLASSIFIED:
            unclassified.append({"arm": arm_of(r), "task": task_of(r), "stop": text[:300]})
            continue
        g = groups.setdefault(v["key"], dict(v, count=0, arms=set()))
        g["count"] += 1
        g["arms"].add(arm_of(r))
    stops = [dict(g, arms=sorted(g["arms"])) for g in sorted(groups.values(), key=lambda g: (-g["count"], g["key"]))]
    return {"arms": arms, "tasks": tasks, "stops": stops, "unclassified": unclassified,
            "bar": FINISH_BAR, "ok": any(x["finished"] >= FINISH_BAR for x in arms), "skipped": skipped}


def read_line(s):
    """The file read and what was left out of it: 'Read PATH: 5 lines, 1 row scored; skipped: 2 unreadable, ...'."""
    rd, sk = s.get("read") or {}, s.get("skipped") or {}
    n = sum(a["runs"] for a in s["arms"])
    left = [(rd.get("unreadable", 0), "unreadable"),
            (rd.get("too_long", 0) + rd.get("cut", 0), "over the size limit"),
            (sk.get("no_arm_or_task", 0), "without an arm or task"),
            (sk.get("before_since", 0), "before --since or with no readable time")]
    left = [f"{k} {w}" for k, w in left if k]
    ln = rd.get("lines", 0)
    return (f"Read {_line(s.get('rows_file', ''))}: {ln} line{'s' if ln != 1 else ''}, {n} row{'s' if n != 1 else ''} "
            f"scored" + (f"; skipped: {', '.join(left)}" if left else "") + ".")


def render(s):
    if not s["arms"]:
        return read_line(s) + "\nNo local-model runs to score."
    out = [read_line(s),
           f"Local-model runs: {len(s['tasks'])} task{'s' if len(s['tasks']) != 1 else ''} measured, "
           f"the bar is {s['bar']} finished by one arm."]
    for a in s["arms"]:
        out.append(f"  {a['arm']}: {a['finished']} of {a['measured']} tasks finished ({a['runs']} runs)"
                   + (" ✓" if a["finished"] >= s["bar"] else ""))
    if s["stops"]:
        out.append("Stop reasons (runs that did not finish):")
        for g in s["stops"]:
            out.append(f"  {g['count']:>3} x {g['kind']}: {g['what']} ({', '.join(g['arms'])})\n        {g['fix']}")
    if s["unclassified"]:
        out.append(f"Unclassified stop reasons ({len(s['unclassified'])}): no rule in localruns.RULES knows the text, "
                   "or no stop was recorded:")
        for u in s["unclassified"][:20]:
            out.append(f"  {u['arm']} / {u['task']}: {u['stop'] or '(no stop recorded)'}")
        if len(s["unclassified"]) > 20:
            out.append(f"  ... and {len(s['unclassified']) - 20} more")
    out.append("Result: " + (f"✓ an arm finished {s['bar']} or more of the measured tasks." if s["ok"]
                             else f"✗ no arm finished {s['bar']} of the measured tasks yet."))
    return "\n".join(out)


def cmd_localruns(args):
    from sampo import cli
    path = getattr(args, "path", None)
    st = {}
    rows = read_rows(path, st)
    try:
        s = score(rows, getattr(args, "since", None))
    except ValueError as e:
        raise cli.UsageError(str(e))
    s["rows_file"], s["read"] = rows_path(path), st
    cli.out(args, s, render(s))
    return 0 if s["ok"] else 1
