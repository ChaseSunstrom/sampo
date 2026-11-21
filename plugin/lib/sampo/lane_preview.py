"""fm lane list's early warning (T-3268): which open lane branches would conflict with main or with each other, found by a
trial merge (git merge-tree --write-tree) that checks nothing out and moves no ref. Only what git reports as a conflict
is a clash: a clean pair is no clash. A pair that gets no answer (a timeout, a spent budget, git too old, a bad ref) is
not checked, and the scan says how many pairs it did check, so a silent gap cannot pass for a clean list. Never raises."""
import math
import os
import re
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

from sampo import core as c


def _budget():  # the env var is for the test run under load; a bad value is the default, never an import error
    try:
        s = float(os.environ.get("SAMPO_LANE_PREVIEW_BUDGET_S") or 20)
    except ValueError:
        return 20.0
    return s if math.isfinite(s) and s >= 0 else 20.0


MAIN = "main"  # the label of the main checkout's HEAD when it has no branch name (detached)
BRANCHES_MAX = 12  # ponytail: n^2 merge-tree calls; the oldest branches past this are not tried, add a cache if more are needed
PAIR_S = 10  # seconds per merge-tree call
BUDGET_S = _budget()  # seconds for the whole scan; a pair not tried by then is not checked
WORKERS = 4  # ponytail: a small pool; more only oversubscribes the shared CPU
LINES_MAX = 10  # pairs named in the text (json has them all)
FILES_MAX = 3  # files named per pair in the text
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
WHY = {124: "timed out", 127: "git did not run", 129: "git older than 2.38 has no merge-tree --write-tree"}
_git = c._git  # a module name, so a test can stand in for git
Scan = namedtuple("Scan", "pairs checked total note")  # pairs: {(a, b): [files]} of the clashes; note: why some were not checked


def _conflicts(main, ref_a, ref_b, timeout):
    """(files, "") when git answered: [] for a clean pair, the names on a conflict; (None, why) when it did not. merge-tree
    exits 1 for a conflict and for a bad ref alike, so a 1 counts only with a tree id first (-z: NUL-separated, the file
    list ends at an empty entry, the messages follow it)."""
    r = _git(main, "merge-tree", "--write-tree", "--name-only", "-z", "--end-of-options", ref_a, ref_b,
             timeout=timeout, proc=True)
    if r.returncode == 0:
        return [], ""
    if r.returncode != 1:
        return None, WHY.get(r.returncode, "git failed")
    tree, *names = r.stdout.split("\0")
    if not SHA.fullmatch(tree):
        return None, "a branch did not resolve"
    files = []
    for f in names:
        if not f:
            break
        files.append(f)
    return files, ""


def _label(main):
    """The main checkout's branch name, the label its pairs carry; MAIN when it has none (a detached HEAD)."""
    r = _git(main, "rev-parse", "--abbrev-ref", "HEAD", proc=True)
    name = r.stdout.strip() if r.returncode == 0 else ""
    return name if name and name != "HEAD" else MAIN


def _newest_first(main, names):
    """The names, newest branch tip first (one for-each-ref call); a name git does not list keeps its place at the end."""
    when = {}
    r = _git(main, "for-each-ref", "--format=%(committerdate:unix)%09%(refname:strip=2)", "refs/heads", proc=True)
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            ts, _, ref = line.partition("\t")
            if ts.isdigit():
                when[ref] = int(ts)
    return sorted(names, key=lambda b: -when.get(b, 0))


def _pair(main, ra, rb, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        return None, "budget spent"
    files, why = _conflicts(main, ra, rb, min(PAIR_S, left))  # the call's timeout is never more than the time left
    if why == WHY[124] and left < PAIR_S:  # cut short by the budget, not by the call's own timeout
        return None, "budget spent"
    return files, why


def scan(main, branches):
    """The Scan of main's checkout (labelled with its branch) and the branches: every pair is tried in a small pool
    within BUDGET_S, the newest BRANCHES_MAX branches only (the rest are named). total counts every pair; checked counts
    the ones git answered."""
    names = _newest_first(main, list(dict.fromkeys(branches)))
    refs = [(_label(main), "HEAD")] + [(b, b) for b in names]
    total = len(refs) * (len(refs) - 1) // 2
    tried, untried = refs[:BRANCHES_MAX + 1], names[BRANCHES_MAX:]
    jobs = [(la, ra, lb, rb) for i, (la, ra) in enumerate(tried) for lb, rb in tried[i + 1:]]
    deadline = time.monotonic() + BUDGET_S
    with ThreadPoolExecutor(WORKERS) as pool:
        answers = list(pool.map(lambda j: _pair(main, j[1], j[3], deadline), jobs))
    why = []
    if untried:
        more = f" +{len(untried) - 3} more" if len(untried) > 3 else ""
        why.append(f"more than {BRANCHES_MAX} branches, not tried: {', '.join(untried[:3])}{more}")
    pairs, checked = {}, 0
    for (la, _, lb, _), (files, reason) in zip(jobs, answers):
        if reason:
            if reason not in why:
                why.append(reason)
            continue
        checked += 1
        if files:
            pairs[(la, lb)] = files
    return Scan(pairs, checked, total, "; ".join(why))


def clashes(main, branches):
    """{(a, b): [files]} of scan(): the clashing pairs only."""
    return scan(main, branches).pairs


def rows(pairs):
    """The json form: [{"pair": [a, b], "files": [...]}], every pair."""
    return [{"pair": [a, b], "files": files} for (a, b), files in pairs.items()]


def preview_json(s):
    """The json 'preview' key: how much of the check ran. s is None under --no-preview."""
    if s is None:
        return {"skipped": True, "checked": 0, "total": 0, "complete": None, "note": "--no-preview"}
    return {"skipped": False, "checked": s.checked, "total": s.total, "complete": s.checked == s.total, "note": s.note}


def incomplete_line(s):
    """The text line for a scan that did not check every pair; '' when it did, or when there was no scan."""
    if s is None or s.checked == s.total:
        return ""
    return f"(preview incomplete: {s.checked} of {s.total} pairs checked" + (f": {s.note})" if s.note else ")")


def warn_lines(s):
    """The text form: a 'Will conflict:' line per pair (LINES_MAX at most, FILES_MAX files each), a line for the pairs
    left out, and the incomplete line. Nothing when there was no scan, or no clash and every pair was checked."""
    if s is None:
        return []
    lines = []
    items = list(s.pairs.items())
    for (a, b), files in items[:LINES_MAX]:
        more = f" +{len(files) - FILES_MAX} more" if len(files) > FILES_MAX else ""
        lines.append(f"Will conflict: {a} and {b}: {', '.join(files[:FILES_MAX])}{more}")
    if len(items) > LINES_MAX:
        lines.append(f"(+{len(items) - LINES_MAX} more clashing pairs; fm lane list --json has them all)")
    inc = incomplete_line(s)
    return lines + ([inc] if inc else [])
