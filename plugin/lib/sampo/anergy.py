"""T-2563 (gen-2 moonshot, anergy): a permission prompt a person approves by reflex has stopped carrying a decision.
PermissionRequest appends {id, cls, ro, at} for every prompt; PostToolUse appends {id, at} for a call that was asked
about; the report pairs them offline and names the command classes approved within REFLEX_S in MIN_N or more prompts,
with the allow rule that would retire each. Only classes whose every prompt only read (fmguard.read_only, or a reading
tool) are ever named, so a destructive or outward class never is, and Sampo never applies a rule: the user does."""
import json
import os
import re
import time

from sampo import core as c

statistics = c._Later("statistics")  # T-3577: ~3 ms (decimal, fractions) off every PostToolUse

STORE, CAP = "anergy.jsonl", 4000  # rows; the oldest half is dropped past CAP
REFLEX_S, MIN_N = 2.0, 5
READ_TOOLS = ("Read", "Grep", "Glob", "LS", "NotebookRead")


def _path():
    return os.path.join(c.state_dir(), STORE)


def klass(tool, inp, cwd=None):
    """(class, read only): a tool's name, or for Bash its program and subcommand as an allow-rule pattern. T-5916: cwd is
    the folder the call runs in, where a git read's repo config is judged (the process's own when None)."""
    if tool != "Bash":
        return str(tool or "?"), tool in READ_TOOLS
    import fmguard
    cmd = str((inp or {}).get("command") or "")
    words = cmd.split()
    prog = os.path.basename(words[0]) if words else ""
    sub = words[1] if len(words) > 1 and re.fullmatch(r"[a-z][\w-]*", words[1]) else ""
    return f"Bash({prog}{' ' + sub if sub else ''} *)", bool(cmd) and fmguard.read_only(cmd, cwd)


def _tail_ids(n=200):
    try:
        with open(_path(), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 32768))
            lines = f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return set()
    return {r.get("id") for r in map(_row, lines) if r and r.get("ev") == "ask"}


def _row(line):
    try:
        r = json.loads(line)
        return r if isinstance(r, dict) else None
    except ValueError:
        return None


def record(ev, pl):
    """ev 'ask' (PermissionRequest) or 'done' (PostToolUse, only for a call asked about). Never raises."""
    try:
        tid = pl.get("tool_use_id")
        if not tid or (ev == "done" and tid not in _tail_ids()):
            return
        row = {"id": str(tid)[:80], "ev": ev, "at": round(time.time(), 3)}
        if ev == "done" and isinstance(pl.get("duration_ms"), (int, float)):
            row["ms"] = max(0, int(pl["duration_ms"]))  # the tool's own run, taken out of the person's time
        if ev == "ask":
            cls, ro = klass(pl.get("tool_name"), pl.get("tool_input"), pl.get("cwd") or None)
            row.update(cls=c.fit(c.plain(cls), 120), ro=bool(ro))
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        c.append_line(_path(), json.dumps(row) + "\n")
    except Exception:  # a note for a report; a hook never fails over it
        pass


def rows():
    try:
        with open(_path(), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    if len(lines) > CAP:  # keep the newest half
        lines = lines[-CAP // 2:]
        c.write_atomic(_path(), "\n".join(lines) + "\n")
    return [r for r in map(_row, lines) if r]


def report(rs=None):
    """{prompts, timed, reflex: [(class, n, median s)]}: classes with MIN_N+ timed prompts, a median within REFLEX_S, and
    every prompt read-only."""
    rs = rows() if rs is None else rs
    asks = {r["id"]: r for r in rs if r.get("ev") == "ask" and r.get("id")}
    done = {r["id"]: r["at"] - (r.get("ms") or 0) / 1000 for r in rs if r.get("ev") == "done" and r.get("id") in asks}
    by = {}
    for i, a in asks.items():
        by.setdefault(a.get("cls") or "?", []).append((done[i] - a["at"] if i in done else None, bool(a.get("ro"))))
    reflex = []
    for cls, xs in sorted(by.items()):
        lats = [x for x, _ in xs if x is not None and x >= 0]
        denied = len(xs) - len(lats) > 1  # a prompt that never ran was refused (one may still be open): a decision
        if len(lats) >= MIN_N and not denied and all(ro for _, ro in xs) and statistics.median(lats) <= REFLEX_S:
            reflex.append((cls, len(lats), round(statistics.median(lats), 2)))
    return {"prompts": len(asks), "timed": len(done), "reflex": reflex}
