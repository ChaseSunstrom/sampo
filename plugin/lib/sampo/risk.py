"""T-1080: one daily risk budget for autonomy. Destructive guard blocks, failed closes and reverts spend weighted points
on the ledger's (UTC) day; past the cap, autonomy steps down a notch (full → standard, standard → slow) and optional
work waits, until the day rolls. Only the user moves the cap (the guard's USER_ONLY_FM): a brake the braked can lift
isn't one."""
import json
import math
import os
import re
import shlex
import sys
import time

from sampo import core as c

# difflib, hashlib and subprocess are imported where they are used: PostToolUse imports this module on every call once a
# block has ever been noted, and `forgive` must cost one stat when the session has nothing pending (T-4742)

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
    counts = dict(counts)
    excused = counts.pop("excused", 0)  # T-4742: blocks the session rewrote and passed: counted, never spent
    spent = sum(WEIGHTS[k] * n for k, n in counts.items())
    limit = cap(p, meta)
    o = off(p)
    return {"spent": spent, "cap": limit, "slowed": spent >= limit and not o, "counts": counts, "excused": excused,
            "off": o}


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
    n, saved = {"destructive block": 0, "failed close": 0, "excused": 0}, old.get("n")  # T-4742: an old cache lacks excused: reread
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
                            n["excused"] += 1
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


def _revert_days(root, day):
    """{UTC day: reverts that still stand} for the commits since `day`. T-3492: a revert a later commit undid (a re-land
    citing 'This reverts commit <its sha>') was a false alarm, and the undoing commit is no revert of anyone's work:
    neither spends points."""
    log = c._git(root, "log", f"--since={day} 00:00:00 +0000", "--format=%H%x1f%s%x1f%b%x1f%at%x1e", fail="")
    rows = [r.strip("\n").split("\x1f") for r in log.split("\x1e") if r.strip()]
    undone = {m for r in rows if len(r) == 4 for m in _UNDOES.findall(r[2])}
    out = {}
    for r in rows:
        if len(r) == 4 and r[1].startswith("Revert ") and not r[1].startswith('Revert "Revert') \
                and not any(r[0].startswith(u) for u in undone) and r[3].strip().isdigit():
            d = time.strftime("%Y-%m-%d", time.gmtime(int(r[3])))
            out[d] = out.get(d, 0) + 1
    return out


def _reverts(root, day):
    """Today's reverts that still stand."""
    return sum(_revert_days(root, day).values())


def slowed(p, meta=None):
    """Why today's risk budget slows autonomy, or None."""
    try:
        s = status(p, meta)
    except Exception:  # an unreadable ledger never changes autonomy
        return None
    if not s["slowed"]:
        return None
    what = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in s["counts"].items() if n)
    if s.get("excused"):
        what += f"; {s['excused']} more excused: rewritten and passed"
    return f"today's risk budget is spent ({s['spent']:g} of {s['cap']:g}: {what}); autonomy steps down a notch until tomorrow"


def collisions(p, days=7):
    """T-1875: the last days' guard blocks by rule, most first: [{rule, category, n, excused, spends, detail, rewrite}],
    spends when its category counts against the budget, excused (T-4742) how many of them the session rewrote and passed
    (they spent nothing), detail the newest, rewrite the guard's hint for it ("" when none)."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    n, last, ex, ex_cat = {}, {}, {}, {}
    for e in c.ledger_events(p, kinds=["guard_block", "guard_forgiven"], since=since):
        d = e.get("data") or {}
        if e.get("event") == "guard_forgiven":
            if d.get("rule"):
                ex[str(d["rule"])] = ex.get(str(d["rule"]), 0) + 1
            else:  # an event from before it carried the rule: the category's blocks share it
                ex_cat[str(d.get("category") or "?")] = ex_cat.get(str(d.get("category") or "?"), 0) + 1
            continue
        r = str(d.get("rule") or d.get("category") or "?")
        n[r], last[r] = n.get(r, 0) + 1, d
    if not n:
        return []
    for cat, k in ex_cat.items():  # the most-hit rule of that category takes the old events
        top = max((r for r in n if str(last[r].get("category")) == cat), key=lambda r: n[r], default=None)
        if top:
            ex[top] = ex.get(top, 0) + k
    import fmguard  # about 12 ms, at SessionStart only, and only when the week had a block
    rows = []
    for r in sorted(n, key=lambda r: -n[r]):
        cat, detail = str(last[r].get("category") or ""), str(last[r].get("detail") or "")
        rows.append({"rule": c.plain(r), "category": cat, "n": n[r], "excused": min(ex.get(r, 0), n[r]),
                     "spends": cat in DESTRUCTIVE,
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


FORGIVE_S, SAME, PENDING_KEEP = 600, 0.45, 4  # a rewrite must come within ten minutes and read alike
# T-4742 (over T-3812's security review of T-3752): a block the session rewrote and passed is a collision, not a risk:
# 10 of 10 destructive blocks on 2026-11-15 were, and read as a spent budget. An excuse means "the same step, done the
# allowed way": the passing call no longer names what the guard refused (its detail) and does the same work on a target
# inside the project. A credentials or self-authorize block is never excused (the agent reaching for a secret or its own
# brake is no false refusal); a system or confine block only when the guard has a mechanical rewrite for it (its own
# _hint), so one it can't tell from a real attempt spends. What keeps probing costing: a block retried unchanged or
# re-dressed (whitespace, quotes, a sh -c, env or cd wrapper) is spent for good, a pass that only re-dresses the blocked
# call is no rewrite, a project is excused half its cap a day at most (not per session: sessions multiply), and one pass
# settles one block (the closest of the session's last PENDING_KEEP). The excuse is given on PostToolUse, once the call
# ran: a call a later hook denied settles nothing.
NEVER_EXCUSED = {"self-authorize", "credentials"}
EXCUSABLE = DESTRUCTIVE - NEVER_EXCUSED
PAIRED_ONLY = {"system", "confine"}
SEG_MAX, PASS_MAX = 160, 120  # a block's command longer than SEG_MAX, or a pass of PASS_MAX or more: too long to compare
# (the global event keeps a window of the one and the first 120 characters of the other, so the replay couldn't)
NOTICE = "risk_notice.json"  # {"day", "tried": when a notice last started, "told": true once it was delivered}
NOTICE_RETRY_S = 900  # a notice that was started and never reported delivery is started again after this long
RISK_DIR = "risk"  # per-session pending files and their lock, in the project's state
_WRAPPERS = re.compile(r"(?:(?:sudo|env|command|exec|nohup|time)\s+|nice\s+(?:-n\s*-?\d+\s+|-\d+\s+)?"
                       r"|timeout\s+(?:-\S+\s+)*\d+[smhd]?\s+)")
_SHELL_C = re.compile(r"(?:sh|bash|zsh|dash)\s+-c\s+(.*)\Z", re.S)
_ASSIGN = re.compile(r"[A-Za-z_]\w*=\S*\s+")
_CD = re.compile(r"(?:cd|pushd)\s+\S+\s*(?:&&|;)\s*")
_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_SKIP = {"cd", "pushd", "popd", "export", "set", "unset", "true", ":", "source", "."}
_SUB = {"git", "docker", "podman", "systemctl", "kubectl", "npm", "pnpm", "yarn", "pip", "pip3", "cargo", "gh", "sampo",
        "fm", "apt", "apt-get", "brew", "helm", "terraform"}
_PATHW = re.compile(r"(?<![\w:/.~$-])(?:~|/)[^\s'\"()<>|;&,]*")
_LONG = {"--recursive": "r", "--force": "f"}


def _unq(s):
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"" else s


def _peel(s):
    """The call under its dress: wrappers (sudo, env, nice, time, timeout N), VAR= words, a leading cd X && and sh -c
    layers taken off."""
    s = str(s).strip()
    for _ in range(8):
        m = _WRAPPERS.match(s) or _ASSIGN.match(s) or _CD.match(s)
        if m:
            s = s[m.end():].lstrip()
            continue
        m = _SHELL_C.match(s)
        if m:
            s = _unq(m.group(1).strip())
            continue
        break
    return s


def cosmetic_key(cmd):
    """T-4742: a command with its dress peeled (_peel) and its whitespace, quotes, backslashes and semicolons dropped:
    two commands with one key are the same call dressed differently."""
    return re.sub(r"[\s'\"\\;]+", "", _peel(cmd))


def cosmetic(a, b):
    return cosmetic_key(a) == cosmetic_key(b)


def _h(s):
    import hashlib
    return hashlib.sha1(str(s).encode("utf-8", "replace")).hexdigest()[:16]


def _segs(cmd):
    """The command's simple commands, peeled, with the cd/export-style ones left out."""
    out = []
    for part in _SPLIT.split(_peel(cmd)):
        s = _peel(part)
        if s and os.path.basename(s.split(None, 1)[0].strip("'\"")) not in _SKIP:
            out.append(s)
    return out


def _prog(seg):
    """(program, subcommand): the subcommand only for the programs that have them (git reset, docker rm …)."""
    ws = seg.split()
    prog = os.path.basename(ws[0].strip("'\"\\")) if ws else ""
    sub = next((w for w in ws[1:] if not w.startswith("-")), "") if prog in _SUB else ""
    return prog, sub


def _flags_args(seg):
    """(sorted unique flag letters, the non-flag words) of a simple command, or (None, None) when it doesn't parse."""
    try:
        ws = shlex.split(seg)
    except ValueError:
        return None, None
    flags, args = set(), []
    for w in ws[1:]:
        if w.startswith("--") and len(w) > 2:
            flags.update(_LONG.get(w, ""))
        elif w.startswith("-") and len(w) > 1:
            flags.update(w[1:].replace("R", "r"))
        else:
            args.append(w)
    return "".join(sorted(flags)), args


def _targets(detail):
    """The absolute paths a guard detail names, normalised (~ expanded)."""
    out = []
    for m in _PATHW.findall(str(detail)[:400]):
        m = m.rstrip(".:")
        a = os.path.normpath(os.path.expanduser(m)) if m else ""
        if a and a not in out:
            out.append(a)
    return out[:6]


def _words(s):
    try:
        lx = shlex.shlex(str(s), posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        return list(lx)
    except ValueError:
        return None


def _names(cmd, tgts, root, single=False):
    """True when the call names one of the paths (or one above it or below it) or can't be shown not to: a word with a
    variable left after expansion, a command substitution, a line the parser can't read."""
    if not tgts:
        return False
    cmd = str(cmd)
    if "`" in cmd or "$(" in cmd:
        return True
    ws = [cmd] if single else _words(cmd)
    if ws is None:
        return True
    for w in ws:
        for v in {w, w.split("=", 1)[-1]}:
            v = os.path.expanduser(os.path.expandvars(v))
            if "$" in v:
                return True
            if v.startswith("/"):
                a = os.path.normpath(v)
            elif root and v and not v.startswith("-"):
                a = os.path.normpath(os.path.join(root, v))
            else:
                continue
            for t in tgts:
                if a == t or a.startswith(t.rstrip("/") + "/") or t.startswith(a.rstrip("/") + "/"):
                    return True
    return False


def _under(path, root):
    roots = {os.path.normpath(root), os.path.realpath(root)}
    return any(path != r and path.startswith(r.rstrip("/") + "/") for r in roots)


def _rm_inside(seg, flags, root):
    """An rm that does the blocked rm's work (same flag letters, at least one target) on targets all inside the project."""
    got, args = _flags_args(seg)
    if got is None or got != flags or not args or not root:
        return False
    for a in args:
        if "$" in a or "`" in a or a.startswith("~"):
            return False
        n = os.path.normpath(a if a.startswith("/") else os.path.join(".", a))
        if a.startswith("/"):
            if not _under(n, root):
                return False
        elif n in (".", "..") or n.startswith("../"):
            return False
    return True


def _like(a, b):
    import difflib
    sm =difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.real_quick_ratio() < SAME or sm.quick_ratio() < SAME:
        return 0.0
    return sm.ratio()


def _rm_like(a, b):
    """Likeness of two rm calls by their targets' last names (a path's length and prefix say nothing: ~/x/zzz and ./zzz are
    one step); a blocked target that is a variable or a substitution is the mechanical case: any rm then reads alike."""
    (_, xa), (_, xb) = _flags_args(a), _flags_args(b)
    if xa is None or xb is None:
        return _like(a, b)
    if any("$" in w or "`" in w for w in xb):
        return 0.5
    return _like(" ".join(os.path.basename(w.rstrip("/")) for w in xa), " ".join(os.path.basename(w.rstrip("/")) for w in xb))


def _pick_seg(segs, tgts, category, root):
    """The simple command the guard refused: the only one, else the only one naming the detail's path, else (a pipe into a
    shell, a destructive git call) the first of its program; '' when it can't be told."""
    if len(segs) == 1:
        return segs[0]
    named = [s for s in segs if _names(s, tgts, root)]
    if len(named) == 1:
        return named[0]
    if category == "pipe-shell" and segs:
        return segs[0]
    if category == "git-destructive":
        return next((s for s in segs if _prog(s)[0] == "git"), "")
    return ""


def block_fields(tool, cmd, category, rule, detail, root=None, cut=False):
    """What a pending block keeps, none of it the command whole: program and subcommand, the blocked segment (redacted,
    SEG_MAX), its flag letters (rm), the paths its detail names, and hashes of the call's and the segment's cosmetic keys
    (to spot a retry). A command longer than SEG_MAX, or cut (the replay's window) has no program: never excused."""
    cmd = c.redact(str(cmd))
    f = {"tool": tool, "category": category, "rule": rule, "prog": "", "sub": "", "seg": "", "flags": "",
         "tgt": [c.redact(t) for t in _targets(detail)], "ck": _h(cosmetic_key(cmd)), "sk": "", "paired": False}
    if category in PAIRED_ONLY:
        import fmguard
        f["paired"] = bool(fmguard._hint(str(detail), category).strip())  # the guard's own record of a mechanical collision
    if tool != "Bash":
        f["prog"] = "@" + str(tool)
        f["base"] = c.redact(os.path.basename(cmd.rstrip("/")))[:SEG_MAX]  # a file tool's call is its path
        return f
    if len(cmd) > SEG_MAX or cut:
        return f
    seg = _pick_seg(_segs(cmd), f["tgt"], category, root)
    if seg:
        prog, sub = _prog(seg)
        f.update(prog=prog, sub=sub, seg=c.redact(seg)[:SEG_MAX], sk=_h(cosmetic_key(seg)),
                 flags=(_flags_args(seg)[0] or "") if prog == "rm" else "")
    return f


def add_entry(entries, now, tool, cmd, category, rule=None, detail="", root=None, cut=False):
    """The session's pending blocks with this one added (T-4742: a list of PENDING_KEEP, newest last). A block that reads
    like one still pending is a retry: both are marked retried and neither is ever excused."""
    f = block_fields(tool, cmd, category, rule, detail, root, cut)
    kept = [e for e in entries if isinstance(e, dict) and now - e.get("t", 0) < FORGIVE_S]
    retried = False
    for e in kept:
        if e.get("tool") == tool and (e.get("ck") == f["ck"] or (f["sk"] and e.get("sk") == f["sk"])):
            e["retried"] = retried = True
    kept.append({**f, "t": now, "retried": retried})
    return kept[-PENDING_KEEP:]


def _rewrite(e, tool, cmd, root):
    """How alike (0-1) the allowed call is to the block's step done the allowed way, or None when it isn't."""
    tg = [t for t in e.get("tgt") or [] if isinstance(t, str)]
    if tool != "Bash":
        p = os.path.normpath(os.path.expanduser(cmd) if cmd.startswith("~") else cmd if cmd.startswith("/")
                             else os.path.join(root or "/", cmd))
        base = e.get("base")  # the same file, written inside the project: a write elsewhere is another step
        ok = root and base and c.redact(os.path.basename(p))[:SEG_MAX] == base and _under(p, root) and not _names(p, tg, root, single=True)
        return 1.0 if ok else None
    if _names(cmd, tg, root):
        return None
    prog, sub, flags, sk = e.get("prog"), e.get("sub") or "", e.get("flags") or "", e.get("sk")
    segs = _segs(cmd)
    if prog == "rm" and not all(_rm_inside(s, flags, root) for s in segs if _prog(s)[0] == "rm"):
        return None
    best = None
    for s in segs:
        rs = c.redact(s)
        if _prog(s) != (prog, sub) or _h(cosmetic_key(rs)) == sk:
            continue
        r = _rm_like(rs[:SEG_MAX], str(e.get("seg") or "")) if prog == "rm" else _like(rs[:SEG_MAX], str(e.get("seg") or ""))
        if r >= SAME and (best is None or r > best):
            best = r
    return best


def pick(entries, tool, cmd, now, root=None):
    """T-4742: the index of the pending block that an allowed call `cmd` is the rewrite of, or None: the same tool, inside
    the window, an EXCUSABLE category (a PAIRED_ONLY one only when the guard has its own rewrite), not retried, not the
    same call re-dressed, no longer naming what the block named and doing the same work (`_rewrite`). Of several, the
    closest."""
    cmd = str(cmd)
    if len(cmd) >= PASS_MAX:
        return None
    ck, best, at = None, -1.0, None
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or e.get("retried") or e.get("tool") != tool or e.get("category") not in EXCUSABLE \
                or now - e.get("t", 0) >= FORGIVE_S or not e.get("prog") \
                or (e["category"] in PAIRED_ONLY and not e.get("paired")):
            continue
        ck = _h(cosmetic_key(c.redact(cmd))) if ck is None else ck  # redacted, as the block's hashes were
        if e.get("ck") == ck:
            continue
        r = _rewrite(e, tool, cmd, root)
        if r is not None and r > best:
            best, at = r, i
    return at


def _dir(p):
    return os.path.join(p.dir, RISK_DIR)


def _path(p, sid):
    """The session's pending file. Named by the id itself (a session id is a uuid) so that finding it takes no hash: a
    name that isn't plain, or is long, is hashed (never a path of its own making)."""
    s = str(sid)
    if len(s) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", s):
        s = _h(s)
    return os.path.join(_dir(p), "pending-" + s + ".json")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
    except (OSError, ValueError):
        return []
    return [e for e in v if isinstance(e, dict)] if isinstance(v, list) else []


def _write(path, entries):
    if entries:
        c.write_atomic(path, json.dumps(entries))
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _sweep(p, now):
    """Pending files an hour old go, and the file T-3752 kept every session's blocks in, unlocked, raw."""
    d = _dir(p)
    for n in os.listdir(d):
        try:
            if n.startswith("pending-") and now - os.stat(os.path.join(d, n)).st_mtime > 3600:
                os.remove(os.path.join(d, n))
        except OSError:
            pass
    try:
        os.remove(os.path.join(p.dir, "risk_pending.json"))
    except OSError:
        pass


def note_block(p, sid, tool, cmd, category, rule=None, detail=""):
    """T-3752: remember this session's excusable block for `forgive` to settle. T-4742: the last PENDING_KEEP of them (a
    builder's subagents share their parent's session id), kept as comparison fields, never the command, under a lock;
    a busy lock keeps nothing (the block spends: the safe side)."""
    if not sid or category not in EXCUSABLE:
        return
    path, now = _path(p, sid), time.time()
    try:
        with c.lock(_dir(p), timeout=1.0):
            _write(path, add_entry(_read(path), now, tool, cmd, category, rule, detail, p.root))
            _sweep(p, now)
    except (c.LockTimeout, OSError):
        return


def forgive(p, sid, tool, cmd):
    """T-3752: an allowed call from the session that was just blocked is the rewrite the hint asked for when `pick` says
    so: the block was a false refusal, so it stops costing a point. Called from PostToolUse, after the call ran. One stat
    when the session has nothing pending (no file, or one older than the window); the lock only on a hit, where the
    project's excused count today must be under half its cap in force."""
    if not sid:
        return False
    path, now = _path(p, sid), time.time()
    try:
        if now - os.stat(path).st_mtime >= FORGIVE_S:
            return False
    except OSError:
        return False
    if pick(_read(path), tool, cmd, now, p.root) is None:
        return False
    try:
        with c.lock(_dir(p), timeout=1.0):
            entries = _read(path)
            i = pick(entries, tool, cmd, now, p.root)
            if i is None or status(p)["excused"] >= math.floor(cap(p) / 2):
                return False
            hit = entries.pop(i)
            _write(path, entries)
            c.log_event(p, "guard_forgiven", data={"category": hit.get("category"), "rule": hit.get("rule")}, session=sid)
    except (c.LockTimeout, OSError):
        return False
    return True


def step_down(p, meta=None, briefs=None):
    """T-4742: {"line", "held"} when today's budget stepped a project the user set to full autonomy down to standard, else
    None: why, and how many plans now wait for the user's yes that full autonomy would have self-approved."""
    meta = c.read_meta(p) if meta is None else meta
    if c.project_autonomy(meta) != "full" or c.panicked():
        return None
    why = slowed(p, meta)
    if not why:
        return None
    held = sum(1 for b in (c.load_briefs(p) if briefs is None else briefs) if b.status not in c.CLOSED
               and c.needs_approval(b, "standard") and not c.needs_approval(b, "full"))
    plans = f"{held} plan{'s' if held != 1 else ''} wait{'' if held != 1 else 's'} for plan approval"
    return {"held": held, "line": f"Autonomy stepped down full → standard today: {why.split('; autonomy')[0]}; "
                                  f"{plans} until tomorrow (sampo risk shows what spent it)"}


_TELL = ("import os, sys; os.nice(19); sys.path.insert(0, sys.argv[1]); from sampo import risk; risk.tell(sys.argv[2])")


def _spawn_tell(p):
    """Start the notice as its own process (notify-send waits up to 10 s, a user's notify command 30): True when started.
    Nothing starts under FOREMAN_NO_BACKGROUND (the tests', and a user's switch)."""
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        return False
    try:
        import subprocess
        libdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.Popen([sys.executable, "-c", _TELL, libdir, p.root], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return True
    except OSError:
        return False


def tell(root):
    """What the detached notice runs: the step-down line to the alerts and the user's notifier; once the user's notifier
    (when one is set) reports its delivery ok, the day's marker says told. -> the line or None."""
    p = c.find_project(root)
    sd = step_down(p) if p else None
    if not sd:
        return None
    from sampo import selfcheck
    t0 = c.now()
    selfcheck.notify(p, f"{p.slug}: {sd['line']}")
    meta = c.read_meta(p)
    res = meta.get("notify_result") if meta.get("notify") else {"ok": True, "at": t0}  # no notifier: the alert row is it
    if isinstance(res, dict) and res.get("ok") and str(res.get("at") or "") >= t0:
        c.write_atomic(os.path.join(p.dir, NOTICE), json.dumps({"day": _day(), "told": True}))
    return sd["line"]


def _notice(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def announce(p, meta=None, briefs=None):
    """T-4742: tell the user, once a UTC day, that the risk budget stepped a full-autonomy project down (the daily
    step-down was silent: 16 plans waited and the user asked why the project was slow). Called where a point is spent (a
    block, a failed close, a revert), never from a read: the notice runs detached (`_spawn_tell`) and its child writes the
    day's marker once the delivery went through; a notice that couldn't start, or whose notifier failed, is tried again,
    but not within NOTICE_RETRY_S of the last start (a burst of blocks starts one process). Returns the line when a
    notice was started, else None."""
    try:
        meta = c.read_meta(p) if meta is None else meta
        if c.project_autonomy(meta) != "full":
            return None
        path, day = os.path.join(p.dir, NOTICE), _day()
        st = _notice(path)
        if st.get("day") == day and (st.get("told") or time.time() - float(st.get("tried") or 0) < NOTICE_RETRY_S):
            return None
        sd = step_down(p, meta, briefs)
        if not sd:
            return None
        with c.lock(_dir(p), timeout=1.0):
            st = _notice(path)
            if st.get("day") == day and (st.get("told") or time.time() - float(st.get("tried") or 0) < NOTICE_RETRY_S):
                return None
            c.write_atomic(path, json.dumps({"day": day, "tried": time.time()}))  # before the child can write told
            if not _spawn_tell(p):
                c.write_atomic(path, json.dumps(st)) if st else os.remove(path)
                return None
        return sd["line"]
    except Exception:  # a notice must never cost the call it rides on
        return None


def replay(p, days=7):
    """T-4742: what the last `days` UTC days would have read under today's rule, from the project's ledger and the
    global events (read-only; never a command's text): {"cap", "days": [{day, blocks, excused, before, after,
    stepped_before, stepped_after}], "changed": the days that would no longer step down}.
    before: the points the days did read (blocks less the T-3752 forgiveness events, plus 2 per failed close and 3 per
    revert). after: blocks less what the excuse rule settles, found by replaying every session's blocks and the calls the
    guard let through (tool events, time-ordered) through add_entry and pick, with the same rules and the same daily
    cap as live; the larger of that and the old forgiveness.
    ponytail: a block event keeps a ~160-character window of the command and a tool event its first 120, so a long block
    (cut) is never excused and a pass of 120 characters or more never counts: live applies the same two limits."""
    import calendar
    first = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
    rows = {}

    def row(d):
        return rows.setdefault(d, {"day": d, "blocks": 0, "old_excused": 0, "failed": 0, "reverts": 0, "excused": 0})
    for e in c.ledger_events(p, kinds=["guard_block", "guard_forgiven", "close_affected"], since=first):
        d, data = str(e.get("ts") or "")[:10], e.get("data") or {}
        if d < first:
            continue
        if e.get("event") == "guard_block" and data.get("category") in DESTRUCTIVE:
            row(d)["blocks"] += 1
        elif e.get("event") == "guard_forgiven" and data.get("category") in DESTRUCTIVE:
            row(d)["old_excused"] += 1
        elif e.get("event") == "close_affected" and data.get("exit"):
            row(d)["failed"] += 1
    root = c.git_root(p.root)
    for d, n in (_revert_days(root, first).items() if root else ()):
        row(d)["reverts"] = n
    timed = []
    for e in c.global_events(first):
        if e.get("project") == p.slug and e.get("kind") in ("guard_block", "tool", "tool_fail") and e.get("session_id"):
            try:
                timed.append((calendar.timegm(time.strptime(str(e.get("ts") or "")[:19], "%Y-%m-%dT%H:%M:%S")), e))
            except ValueError:
                continue
    limit, half = cap(p), math.floor(cap(p) / 2)
    pending, used = {}, {}
    for t, e in sorted(timed, key=lambda x: x[0]):
        sid, day = str(e["session_id"]), str(e["ts"])[:10]
        if e["kind"] == "guard_block":
            if e.get("category") in EXCUSABLE:
                cmd = str(e.get("cmd") or "")
                pending[sid] = add_entry(pending.get(sid, []), t, e.get("tool"), cmd, e["category"], e.get("rule"),
                                         e.get("target") or "", p.root, cut=cmd.startswith("…") or cmd.endswith("…"))
            continue
        if day < first or used.get(day, 0) >= half:
            continue
        i = pick(pending.get(sid, []), e.get("tool"), e.get("target") or "", t, p.root)
        if i is not None:
            pending[sid].pop(i)
            used[day] = used.get(day, 0) + 1
            row(day)["excused"] += 1
    out = []
    for d in sorted(rows):
        r = rows[d]
        extra = WEIGHTS["failed close"] * r["failed"] + WEIGHTS["revert"] * r["reverts"]
        r["before"] = max(0, r["blocks"] - r["old_excused"]) + extra
        r["after"] = max(0, r["blocks"] - max(r["old_excused"], r["excused"])) + extra
        r["stepped_before"], r["stepped_after"] = r["before"] >= limit, r["after"] >= limit
        out.append(r)
    return {"cap": limit, "days": out, "changed": [r for r in out if r["stepped_before"] and not r["stepped_after"]]}


def _replay_text(rep):
    lines = [f"Risk replay: what these days would have read under the excuse rule (cap {rep['cap']:g} points a day, a project "
             f"excused half of it at most; no command text shown):",
             "  day         blocks  excused  before  after"]
    lines += [f"  {r['day']}  {r['blocks']:>6}  {max(r['old_excused'], r['excused']):>7}  {r['before']:>6}  {r['after']:>5}"
              + ("  steps down → would have read below the cap" if r in rep["changed"]
                 else "  steps down" if r["stepped_before"] else "")
              for r in rep["days"]]
    lines.append(f"{len(rep['changed'])} day{'s' if len(rep['changed']) != 1 else ''} would have read below the cap.")
    lines.append("Blocks already logged are not retroactively excused: a day that stepped autonomy down stays stepped down "
                 "until the UTC day turns; the rule applies to blocks from now on.")
    lines.append("The excused column is a lower bound: the event log keeps only a window of a long command, and a block "
                 "whose command was cut is never excused (live applies the same limit).")
    return "\n".join(lines)


def cmd_risk(args):
    from sampo import cli
    p = cli.resolve(args)
    if args.action == "replay":  # T-4742: read-only
        rep = replay(p, min(max(int(args.days or 7), 1), 30))
        return cli.out(args, rep, _replay_text(rep))
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
    if s["excused"]:  # T-4742: rewritten and passed: they cost nothing
        lines.append(f"  {s['excused']} excused (a block the session rewrote and passed spends nothing)")
    if s["slowed"]:
        lines.append("  " + slowed(p))
    s["collisions"] = collisions(p)  # T-1875: a habit costs rewrites even where it spends no risk
    if s["collisions"]:
        lines.append("Guard collisions, 7 days (the rewrite that passes):")
        lines += [f"  {h['n']}× {h['rule']}"
                  + (f" (spends risk; {h['excused']} excused)" if h["spends"] and h["excused"]
                     else " (spends risk)" if h["spends"] else "")
                  + f" — {c.fit(h['rewrite'], 160) or 'last: ' + h['detail']}"
                  for h in sorted(s["collisions"], key=lambda h: not h["spends"])[:8]]  # T-1972: what spent it first
    return cli.out(args, s, "\n".join(lines))
