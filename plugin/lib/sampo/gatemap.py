"""T-3278: sampo gatemap FILES forecasts which self-change gates a change will hit, from past refusals, with one smallest
fix each. learn() turns refusals (self-change holds, merge refusals, guard blocks, failed closes) into per-gate records
of trigger (file, rule or command) and count; predict() names the gates a file list will likely hit, with confidence
h/(h+1) from the history it matched. Read-only, and it runs no gate: selfgate.gate is minutes of bench. Proof decides;
this only advises."""
import collections
import json
import os
import re

from sampo import core as c
from sampo import selfgate

SHA = re.compile(r"^[0-9a-f]{7,40}$")  # a ledger sha reaches git's argv: a leading '-' would read as an option
SHIM = re.compile(r"plugin/lib/fm[^/]*\.py$")  # an fm shim aliases its sampo module, which owes the test
LEARN_EVENTS = 5000  # the newest ledger events learn() reads
SHAS = 50  # distinct self-change shas, newest first, whose changed files learn() looks up (a git call each, timeout 5 s)
MAX_FILES = 200
SELF_GATES = ("footprint", "verbs", "timing", "guard case")  # what selfgate.gate holds on, from history

FIX = {
    "footprint": "cut one always-on line or hook elsewhere in the same change, so the growth has a cut to pay for it",
    "verbs": "remove one fm verb in the same change, or fold it into an existing one",
    "timing": "move the new work off the hook's path: a lazy import, or after the hook has answered",
    "guard case": "keep the guard case's text; add the new case beside it rather than changing it",
    "changelog": "give one CHANGELOG line in your report, in the style of the existing entries",
    "merge refused": "fix what the merge refusal named, then merge again",
    "close failed": "run the failing command shown in its tail, fix it, then finish",
    "guard block": "use the allowed route for this command, or ask for its category with sampo ask",
}


def row_gate(row):
    """The self-change gate a selfgate refusal row belongs to (its text starts the way selfgate.gate writes it)."""
    if row.startswith("footprint "):
        return "footprint"
    if row.startswith("verbs "):
        return "verbs"
    if row.startswith("hook "):
        return "timing"
    if row.startswith(("guard: ", "guard case ")):
        return "guard case"
    return None  # "the self-change gate couldn't run" and anything new: not a gate to learn from


def clean(f):
    """A repo-relative path from a CLI arg or a git line, or None: no absolute path, no '..' escape, no NUL."""
    f = str(f or "").strip()
    if not f or "\x00" in f or os.path.isabs(f):
        return None
    f = os.path.normpath(f).replace(os.sep, "/")
    return None if f == "." or f == ".." or f.startswith("../") else f


def _files_of(p, sha):
    if not SHA.match(sha):
        return []
    out = c._git(p.root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha, timeout=5, fail="")
    return [f for f in (clean(x) for x in out.splitlines()) if f][:MAX_FILES]


def learn(p, gates=None):
    """[{gate, count, triggers: [{kind, value, count}]}], most refused first. gates limits what is learned (None: all);
    predict asks for the self-change gates only, so the other ledger sources are not read. kind is file (a changed file
    of a refused commit), command (a failed close or a guard block) or rule (a merge refusal's reason). count is
    refusals: a refusal event counts once for a gate however many of its rows name it; a trigger counts once per
    refusal it was part of."""
    def wanted(gate):
        return gates is None or gate in gates

    records = {}

    def add(gate, pairs):
        g = records.setdefault(gate, {"count": 0, "triggers": collections.Counter()})
        g["count"] += 1
        for kind, value in pairs:
            g["triggers"][(kind, value)] += 1

    kinds = tuple(k for k, on in (
        ("self_change_refused", any(wanted(g) for g in SELF_GATES)),
        ("enforce", wanted("merge refused")),
        ("first_brief_close_failed", wanted("close failed"))) if on)
    events = c.ledger_events(p, kinds)[-LEARN_EVENTS:][::-1] if kinds else []  # newest first
    files_of = {}
    for e in events:
        d, ev = e.get("data") or {}, e.get("event")
        if ev == "self_change_refused":
            sha = str(d.get("sha") or "")
            if sha not in files_of and len(files_of) < SHAS:
                files_of[sha] = _files_of(p, sha)
            files = files_of.get(sha, [])
            seen = set()
            for row in d.get("rows") or []:
                gate = row_gate(str(row))
                if gate and wanted(gate) and gate not in seen:
                    seen.add(gate)
                    add(gate, [("file", f) for f in files] or [("rule", "files unknown")])
        elif ev == "enforce" and d.get("kind") == "merge_refused":
            add("merge refused", [("rule", c.fit(re.sub(r"\d+", "N", c.plain(str(d.get("reason") or "?"))), 60))])
        elif ev == "first_brief_close_failed":
            add("close failed", [("command", c.fit(c.plain(str(d.get("cmd") or "?")), 80))])
    if wanted("guard block"):
        for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 30000):
            if e.get("kind") == "guard_block" and e.get("project") == p.slug:
                add("guard block", [("command", c.fit(c.plain(str(e.get("cmd") or e.get("category") or "?")), 80))])
    return [{"gate": k, "count": v["count"],
             "triggers": [{"kind": kind, "value": val, "count": n} for (kind, val), n in v["triggers"].most_common(20)]}
            for k, v in sorted(records.items(), key=lambda kv: (-kv[1]["count"], kv[0]))]


def _base(p):
    """The diff base a lane's change is measured from: the closest of its merge-bases with main or master, local or
    remote, and (from a lane) the main checkout's HEAD. T-4060: the closest, because a local main is often stale (ship
    pushes the work branch to origin/main) and a stale base made every module since read as new. HEAD when none."""
    refs = ["origin/main", "main", "origin/master", "master"]
    main = c.main_worktree(p.root)
    if main and os.path.realpath(main) != os.path.realpath(p.root):
        refs.append(c._git(main, "rev-parse", "HEAD", timeout=5).strip())
    bases = {b for b in (c._git(p.root, "merge-base", "HEAD", r, timeout=5, fail="").strip() for r in refs if r) if b}
    return min(bases, key=lambda b: int(c._git(p.root, "rev-list", "--count", f"{b}..HEAD", timeout=10,
                                                fail="").strip() or 10 ** 9), default="HEAD")


def _new_since(p, f, base):
    """True when f is not in the base: a module committed on this branch is new to the change too. A git that timed
    out (124) or couldn't run (127) says nothing about the file (T-4060: load made the old 2 s default read as new)."""
    return c._git(p.root, "cat-file", "-e", f"{base}:{f}", timeout=10, proc=True).returncode not in (0, 124, 127)


def _shim(p, f):
    """An fm shim: plugin/lib/fmX.py that aliases its sampo module (sys.modules[__name__] = ...), so it owes no test."""
    if p is None or not SHIM.match(f):
        return False
    try:
        with open(os.path.join(p.root, f), encoding="utf-8", errors="replace") as fh:
            return "sys.modules[__name__]" in fh.read(4096)
    except OSError:
        return False


def _cached_map(p):
    try:
        with open(os.path.join(p.dir, "hook-modules.json"), encoding="utf-8") as fh:
            m = json.load(fh).get("map")
        return m if isinstance(m, dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def predict(files, p=None, records=None):
    """[{gate, confidence, fix, why}] the gates a change to these files will likely hit, most confident first. Rules
    (tests, the docs of a new module, the changelog) are certain; self-change gates need history on a changed file
    (confidence h/(h+1), h the refusals that named the most-refused changed file); the timing bench is named by the hook
    map when one is cached. p adds history and new-file detection; without it, rules only."""
    files = [f for f in (clean(x) for x in files) if f][:MAX_FILES]
    rows = []

    def row(gate, conf, why, fix=None):
        rows.append({"gate": gate, "confidence": round(conf, 2), "fix": fix or FIX[gate], "why": why})

    owed = [f"plugin/tests/test_{os.path.basename(f)[:-3]}.py" for f in files
            if f.startswith("plugin/lib/") and f.endswith(".py") and not _shim(p, f)]
    if owed:
        row("tests", 1.0, "rule: a changed module owes its test file",
            f"add or update {c.joinmore(owed, 3)} and run it niced: nice -n 10 python3 plugin/tests/run.py <name>")
    if p is not None:
        base = _base(p)
        new = [f for f in files[:20] if f.startswith("plugin/lib/") and f.endswith(".py") and _new_since(p, f, base)]
        if new:
            row("docs", 1.0, "rule: a new module owes its shim, a MASTER.md bullet and a docs/map line",
                "add the fm shim, a MASTER.md bullet and a docs/map line for " + c.joinmore(new, 3))
    if files:
        row("changelog", 1.0, "rule: every change gets a CHANGELOG line")
    judged = any(selfgate._judged(f) or selfgate._benched(f) for f in files)
    hmap = _cached_map(p) if p is not None else None
    timing_on = selfgate.needs_timing(files, hmap) if hmap is not None else None
    if timing_on:
        row("timing", 1.0, "hook map: a hook imports a changed module, so the timing bench runs")
    if judged and p is not None:
        for rec in records if records is not None else learn(p, gates=SELF_GATES):
            gate = rec["gate"]
            if gate not in SELF_GATES or gate == "timing" and timing_on is not None:
                continue  # the cached hook map already decided timing, yes or no
            h = max((t["count"] for t in rec["triggers"] if t["kind"] == "file" and t["value"] in files), default=0)
            if h:
                row(gate, h / (h + 1), f"{h} past refusal{'s' if h > 1 else ''} on a changed file")
    return sorted(rows, key=lambda r: (-r["confidence"], r["gate"]))


def _worktree_files(p):
    """The changed files: tracked changes against the merge-base (deleted ones owe nothing), plus untracked ones."""
    names = c._git(p.root, "diff", "--name-only", "--diff-filter=d", _base(p), timeout=10, fail="").splitlines()
    names += c._git(p.root, "ls-files", "--others", "--exclude-standard", timeout=10, fail="").splitlines()
    seen = []
    for f in (clean(x) for x in names):
        if f and f not in seen:
            seen.append(f)
    return seen[:MAX_FILES]


def cmd_gatemap(args):
    from sampo import cli
    p = cli.resolve(args)
    raw = list(getattr(args, "files", None) or [])
    files = [f for f in (clean(x) for x in raw) if f]
    if raw and not files:
        raise cli.UsageError("gatemap takes repo-relative paths, e.g. plugin/lib/sampo/friction.py")
    if not raw:
        files = _worktree_files(p)
    rows = predict(files, p=p)
    head = (f"gatemap: {len(files)} changed file{'s' if len(files) != 1 else ''}: "
            + ", ".join(c.plain(f) for f in files[:3]) + (" …" if len(files) > 3 else "")) if files else "gatemap: no changed files"
    lines = [head] + [f"  {r['gate']:<12} {round(r['confidence'] * 100):>3}%  {c.fit(c.plain(r['fix']), 90)}" for r in rows[:10]]
    if len(rows) > 10:
        lines.append(f"  … {len(rows) - 10} more")
    if not rows:
        lines.append("  no self-change gate predicted from history or rules")
    return cli.out(args, {"files": files[:50], "gates": rows}, "\n".join(lines))
