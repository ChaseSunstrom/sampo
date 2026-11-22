"""T-5055 (a slice of T-4419): the hard exam's grade. grade(result, events) turns one run's result and its event stream
into one plain-JSON row: the fix checks with partial credit (2 of 4 reads as half, not as zero), the keep checks that
broke, a false done (said done with a fix check unmet or a keep broken), and the cost, the minutes and the instruments
read from the events. Pure: no run, no model, no network and nothing written; the runner that makes the result and the
events, and the pack that holds the checks, are other tasks'. Unknown is never credit: a check that is not a clear True
did not pass.

result: {"fix": CHECKS, "keep": CHECKS, "done": bool, "cost": number}. CHECKS is {id: bool} or a list of
{"id": str, "passed": bool} (a bare string is an id that did not pass); "keep" and "done" may be missing, and without a
bool "done" the events' own `done` action (result ok) says it. events: Smith's --json events (smith.EVENTS): `ts` seconds,
`action` events (action, target, result), `tokens` (call_in, call_out, total_in, total_out), and the `closed` event's `usd` (the run's dollars). An instrument (instr.TOOLS: sym, fail,
logs, data, trace) counts as used when an action event names it as the action or a `run` starts `sampo NAME` / `fm NAME`
(also behind `python3 .../bin/sampo`, `env` and VAR=x) as a command of its own, and the call came back ok."""
import json
import math
import os
import re
import shlex

from sampo import core as c
from sampo import instr

INSTRUMENTS = tuple(sorted(instr.TOOLS))
ID_MAX = 80  # an id is cut here: the row is printed
CHECKS_MAX = 2000  # more checks than any pack holds: refused, not scored
CMD_MAX = 2000  # a command is read to here
EVENTS_MAX, LINE_MAX = 100_000, 200_000  # read_events: lines kept, and the longest line read
LAUNCHERS = ("sampo", "fm")
SEPARATORS = (";", "&&", "||", "|", "&")


def _finite(x):
    """x when it is a finite number (never a bool; a huge integer is not finite here), else None."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    try:
        return x if math.isfinite(x) else None
    except (OverflowError, ValueError):
        return None


def _num(x):
    """x when it is a finite non-negative number (never a bool), else None."""
    x = _finite(x)
    return x if x is not None and x >= 0 else None


def _ident(x):
    return c.fit(c.plain(str(x)), ID_MAX)


def _checks(field, name):
    """{id: passed} in the order the ids came; a duplicate id passes only when every copy did."""
    if field is None:
        return {}
    if isinstance(field, dict):
        items = [(k, v) for k, v in field.items()]
    elif isinstance(field, list):
        items = []
        for it in field:
            if isinstance(it, str):
                items.append((it, False))
            elif isinstance(it, dict) and isinstance(it.get("id"), str):
                items.append((it["id"], it.get("passed")))
            else:
                raise ValueError(f"{name}: each check is an id or an {{id, passed}} object")
    else:
        raise ValueError(f"{name}: checks are an object {{id: bool}} or a list")
    if len(items) > CHECKS_MAX:
        raise ValueError(f"{name}: {len(items)} checks (at most {CHECKS_MAX})")
    out = {}
    for k, v in items:
        k = _ident(k)
        out[k] = out.get(k, True) and v is True
    return out


def read_events(path):
    """The JSON objects of an events file, in order: a missing file is none, a line that is not an object is skipped, a
    line over LINE_MAX is dropped unread, and at most EVENTS_MAX are kept."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            while len(out) < EVENTS_MAX:
                raw = f.readline(LINE_MAX + 1)
                if not raw:
                    break
                if len(raw) > LINE_MAX and not raw.endswith("\n"):
                    while raw and not raw.endswith("\n"):  # drain the rest of the long line without keeping it
                        raw = f.readline(LINE_MAX)
                    continue
                try:
                    e = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError:
        return []
    return out


def _segments(cmd):
    """The commands of one shell line, each a list of words: split on ; && || | & and newlines outside quotes. An
    unbalanced quote falls back to whitespace words."""
    cmd = cmd[:CMD_MAX].replace("\\\n", " ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""  # a # is a word here, never a comment that swallows the later commands
        words = list(lex)
    except ValueError:
        words = cmd.split()
    segs, cur = [], []
    for w in words:
        if w in SEPARATORS:
            segs.append(cur)
            cur = []
        else:
            cur.append(w)
    segs.append(cur)
    return [s for s in segs if s]


def _instrument_of(words):
    """The instrument a command starts, or None: [env] [VAR=x ...] [python3 PATH/]sampo|fm NAME."""
    i = 0
    if words and os.path.basename(words[0]) == "env":
        i = 1
    while i < len(words) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[i]):
        i += 1
    if i < len(words) and re.fullmatch(r"python[0-9.]*", os.path.basename(words[i])):
        i += 1
    if i + 1 < len(words) and os.path.basename(words[i]) in LAUNCHERS and words[i + 1] in INSTRUMENTS:
        return words[i + 1]
    return None


def _instruments(events):
    """({name: calls that came back ok}, calls tried) from the action events."""
    used, tried = {}, 0
    for e in events:
        if not isinstance(e, dict) or e.get("event") != "action" or not isinstance(e.get("action"), str):
            continue
        if e["action"] in INSTRUMENTS:
            names = [e["action"]]
        elif e["action"] == "run" and isinstance(e.get("target"), str):
            names = [n for n in (_instrument_of(s) for s in _segments(e["target"])) if n]
        else:
            continue
        tried += len(names)
        if e.get("result") == "ok":
            for n in names:
                used[n] = used.get(n, 0) + 1
    return dict(sorted(used.items())), tried


def _said_done(result, events):
    if isinstance(result.get("done"), bool):
        return result["done"]
    if result.get("done") is not None:
        return False
    return any(isinstance(e, dict) and e.get("event") == "action" and e.get("action") == "done" and e.get("result") == "ok"
               for e in events)


def grade(result, events):
    """One run's row (see the module text). Raises ValueError for a result that is not an object or whose checks are
    malformed or past CHECKS_MAX; events that are not objects, and fields of the wrong type, are skipped."""
    if not isinstance(result, dict):
        raise ValueError("the result is an object {fix, keep, done, cost}")
    events = [e for e in events or [] if isinstance(e, dict)]
    fix, keep = _checks(result.get("fix"), "fix"), _checks(result.get("keep"), "keep")
    passed = sum(fix.values())
    broken = [k for k, ok in keep.items() if not ok]
    whole = bool(fix) and passed == len(fix)
    done = _said_done(result, events)
    cost = _num(result.get("cost"))
    if cost is None:
        # Smith's `closed` event carries the run's dollars as `usd`, already the total: the last good one, never a sum
        cost = next((u for u in (_num(e.get("usd")) for e in reversed(events) if e.get("event") == "closed")
                     if u is not None), None)
    stamps = [t for t in (_finite(e.get("ts")) for e in events) if t is not None]
    span = (max(stamps) - min(stamps)) / 60 if len(stamps) > 1 else None
    span = _finite(span)
    tokens = next((e for e in reversed(events) if e.get("event") == "tokens" and _num(e.get("total_in")) is not None), {})
    used, tried = _instruments(events)
    return {"fix_total": len(fix), "fix_passed": passed, "partial": round(passed / len(fix), 3) if fix else None,
            "whole": whole, "fix_missed": [k for k, ok in fix.items() if not ok],
            "keep_total": len(keep), "keep_broken": len(broken), "broken_keeps": broken,
            "done": done, "false_done": done and not (whole and not broken),
            "cost": cost, "tokens_in": _num(tokens.get("total_in")), "tokens_out": _num(tokens.get("total_out")),
            "minutes": round(span, 1) if span is not None else None,
            "instruments": used, "instruments_used": list(used), "instrument_calls": sum(used.values()),
            "instruments_tried": tried}
