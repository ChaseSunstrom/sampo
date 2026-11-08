"""T-3285 (moon C-005835): two open builder lanes that add the same function, or touch the same file, overlap. The lead
sees one OVERLAP line per pair in fm lane list and merges one lane or stops the other before the two conflict.
facts() reads a lane folder's work since it forked from base (committed, uncommitted and untracked); overlap() pairs
lanes that share a def (ranked first) or a file; lines() prints the pairs, then one note for the lanes not scanned.
The diff is the ground truth: brief text isn't compared."""
# map-links: plugin/lib/sampo/lanes.py plugin/tests/test_lane_overlap.py
import itertools
import os
import re
import subprocess

from sampo import core as c
from sampo import lanes

MAX_PAIRS = 8  # ponytail: the pairs listed; the rows beyond them are cut, not paged
MAX_NAMES = 4  # the shared defs or files a line names before "+N more"
MAX_LINES = 5000  # ponytail: changed lines per lane; a bigger lane is not scanned, so it claims no overlap
TIMEOUT = 10  # seconds per git call
HUB = frozenset({"CHANGELOG.md", "MASTER.md", "README.md"})  # files every lane edits; docs/map/ is hub too (_hub)
# ponytail: a name every file defines (main, setUp, run …) isn't a shared function; test_* aren't either
GENERIC = frozenset({"main", "run", "setUp", "tearDown", "setup", "teardown", "init", "execute", "handle", "get",
                     "set", "update", "load", "save", "helper", "cmd"})
DIFF = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--src-prefix=a/", "--dst-prefix=b/"]
LINE = 240  # the widest OVERLAP line


def _hub(f):
    return f in HUB or f.startswith("docs/map/")


def _git(cwd, *args):
    """stdout of one git call in cwd; raises on a non-zero exit, a timeout, or a missing git."""
    return subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", *args], cwd=cwd,
                          capture_output=True, text=True, errors="replace", timeout=TIMEOUT, check=True).stdout


def _added(diff):
    """{file: [def, …]}: every file the diff touches, with the defs its '+' lines open (lanes' _SIGNED_DEF). Context
    names and removed defs don't count; a dunder (__init__ and its kin sit in every class), a test_* and a GENERIC name
    don't either; prose that opens with a keyword ("function returns …") doesn't either: a def is followed by one of
    ( : { < [ ="""
    out, cur = {}, None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/.+? b/(.+)$", line)  # a git-quoted name doesn't match: that file is skipped
            cur = m.group(1) if m else None
            if cur is not None:
                out.setdefault(cur, [])
        elif cur is not None and line.startswith("+") and not line.startswith("+++"):
            d = lanes._SIGNED_DEF.match(line)
            name = d.group(1) if d else ""
            if name and re.match(r"\s*[(:{<\[=]", line[d.end():]) and not name.startswith(("__", "test_")) \
                    and name not in GENERIC and name not in out[cur]:
                out[cur].append(name)
    return out


def _unscanned():
    return {"files": [], "defs": [], "scanned": False}


def facts(lane, base):
    """{files, defs, scanned}: what a lane folder adds since it forked from base (a commit): its committed work, its
    uncommitted tracked edits and its untracked files. A folder git can't read, or more than MAX_LINES changed lines,
    is not scanned: it is named, never paired."""
    if not lane:
        return _unscanned()
    try:
        mb = _git(lane, "merge-base", base, "HEAD").strip()
        changed = sum(int(n) for n in re.findall(r"(\d+) (?:insertion|deletion)", _git(lane, *DIFF, "--shortstat", mb)))
        if changed > MAX_LINES:
            return _unscanned()
        added = _added(_git(lane, *DIFF, "-U0", mb))
        files = set(added)
        defs = {d for ds in added.values() for d in ds}
        budget = MAX_LINES - changed
        for f in _git(lane, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            path = os.path.join(lane, f)
            if not f or os.path.islink(path) or not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = list(itertools.islice(fh, budget + 1))  # reads at most the budget left, never the whole file
            if len(body) > budget:
                return _unscanned()
            budget -= len(body)
            files.add(f)
            defs |= {d for ds in _added(f"diff --git a/{f} b/{f}\n" + "".join("+" + x for x in body)).values()
                     for d in ds}
    except (OSError, subprocess.SubprocessError, ValueError):
        return _unscanned()
    return {"files": sorted(files), "defs": sorted(defs), "scanned": True}


def overlap(lanes_facts):
    """Pairs of lanes {task id: facts} that share an added def or a file (hub files don't count): [{a, b, defs, files,
    advice}], a before b, the pairs sharing defs first (more first), then the ones sharing only files; at most MAX_PAIRS."""
    ids = sorted(lanes_facts)
    rows = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            defs = sorted(set(lanes_facts[a].get("defs", [])) & set(lanes_facts[b].get("defs", [])))
            files = sorted(f for f in set(lanes_facts[a].get("files", [])) & set(lanes_facts[b].get("files", []))
                           if not _hub(f))
            if defs or files:
                rows.append({"a": a, "b": b, "defs": defs, "files": files,
                             "advice": _advice(f"OVERLAP {a} and {b}: ", defs, files)})
    rows.sort(key=lambda r: (not r["defs"], -len(r["defs"]), -len(r["files"]), r["a"], r["b"]))
    return rows[:MAX_PAIRS]


def _advice(head, defs, files):
    """'both add …[ and touch …]: <tail>' that fits LINE after head. The names are cut to fit; the tail stays whole."""
    what = ([f"add {c.joinmore(defs, MAX_NAMES)}"] if defs else []) + ([f"touch {c.joinmore(files, MAX_NAMES)}"]
                                                                    if files else [])
    tail = ("merge one and stop the other's copy before they conflict" if defs
            else "merge one first; the other rebases on it")
    room = LINE - len(head) - len("both ") - len(": ") - len(tail)
    return f"both {c.fit(c.plain(' and '.join(what)), max(room, 1))}: {tail}"


def lines(ov):
    """fm lane list's OVERLAP lines: one per pair, naming both tasks and what they share; then one note naming the
    open lanes that could not be scanned."""
    out = [f"OVERLAP {r['a']} and {r['b']}: {r['advice']}" for r in ov["overlaps"]]
    if ov["unscanned"]:
        out.append(f"OVERLAP ?: {', '.join(ov['unscanned'])} not scanned")
    return out


def scan(root, rows):
    """overlap() over fm lane list's rows: the open builder lanes (a branch, a folder, a task not closed) by that task.
    {overlaps, unscanned}: an open lane git can't read is named in unscanned and paired with nothing."""
    base = ""
    try:
        base = _git(root, "rev-parse", "HEAD").strip()
    except (OSError, subprocess.SubprocessError):
        pass  # no base: every lane is unscanned
    open_, unscanned = {}, []
    for r in rows:
        tid = next((t["id"] for t in r["tasks"] if t["status"] not in c.CLOSED), None)
        if not (tid and r["branch"] and not r["missing"]):
            continue
        f = facts(r.get("path", ""), base)
        if f["scanned"]:
            open_[tid] = f
        else:
            unscanned.append(tid)
    return {"overlaps": overlap(open_), "unscanned": unscanned}
