"""T-3291 (moon C-000930): sampo kickoff ID dry-runs a task's plan before its first edit, from the brief alone.
touch() lists per step the existing files its scope globs and its step text name, with the def/class symbols the text
names grepped in them (a path that doesn't exist yet is a "creates"); collide() names the other open tasks whose scope or
steps share a file or a symbol; report() adds the tests map.tests_for links to them, the gates cli.gates names for the
task's type and tier, the gates gatemap.predict expects and the lane preview, the last two only when importable.
The lanes preview (T-3268) is unverified: its signature is guessed, and a call that raises says so on stderr.
Read-only: no code runs, no test runs, no brief changes. Proof decides; this only advises."""
# map-links: plugin/tests/test_kickoff.py
import bisect
import importlib
import os
import re
import sys

from sampo import core as c

MAX_SCAN = 300  # ponytail: files read for symbols per task; a wider scope is read in its first 300 files, in git order
MAX_BYTES = 2_000_000  # the most one file is read for its defs
MAX_LISTED = 8  # files or symbols a collision row names
STEPS_SHOWN, COLLIDERS_SHOWN = 6, 3  # the screen: under 20 lines

_PATH = re.compile(r"(?<![\w./@-])((?:[\w@+-][\w@+.-]*/)*[\w@+-][\w@+.-]*\.[A-Za-z]\w{0,7})(?![\w/-])")
_DEFS = re.compile(r"^[ \t]*(?:async[ \t]+|export[ \t]+|pub[ \t]+)*(?:def|class|function|fn|func|sub)[ \t]+"
                   r"([A-Za-z_$][\w$]*)", re.M)
_SYM = (re.compile(r"`([A-Za-z_][\w.]*)(?:\([^`]*\))?`"),  # `name`, `mod.name`, `name(args)`
        re.compile(r"\b(?:def|class|function|fn|func)\s+([A-Za-z_]\w*)"),
        re.compile(r"\b[A-Za-z_]\w*\.([A-Za-z_]\w*)\("),  # mod.name(
        re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b"))  # snake_case


def _generic():
    from sampo import lane_overlap
    return lane_overlap.GENERIC


class _Tree(list):
    """The repo's files, sorted, with a set beside them for membership."""

    def __init__(self, files):
        super().__init__(sorted(set(files)))
        self.members = frozenset(self)
        self.matched = {}  # glob -> the tree's files it matches, computed once however many tasks carry it

    def glob(self, g):
        """The files of the tree a scope glob matches: a literal one by a prefix slice of the sorted list, a wildcard
        one by one scan, both remembered for every other open task that carries the same glob."""
        if g not in self.matched:
            pat = g.strip().rstrip("/").removeprefix("./")
            if not any(ch in pat for ch in "*?["):
                i = bisect.bisect_left(self, pat + "/")
                out = ([pat] if pat in self.members else [])
                while i < len(self) and self[i].startswith(pat + "/"):
                    out.append(self[i])
                    i += 1
                self.matched[g] = frozenset(out)
            else:
                self.matched[g] = frozenset(f for f in self if c.glob_match(f, g))
        return self.matched[g]


def _tree(p):
    """Every tracked or untracked-but-not-ignored file of the repo, as repo-relative paths ([] outside git)."""
    out = c._git(p.root, "ls-files", "--cached", "--others", "--exclude-standard", timeout=10)
    return _Tree(f for f in out.splitlines() if f)


def names(text):
    """The def/class-like names a step's text mentions, in order: `backticked`, after def/class, mod.name( and
    snake_case words; dunders, test_* and names every file defines (main, run …) are not a plan's symbols."""
    skip, found = _generic(), []
    for rx in _SYM:
        for m in rx.finditer(text or ""):
            n = m.group(1).split(".")[-1]
            if len(n) >= 3 and not n.startswith(("__", "test_")) and n not in skip and n not in found:
                found.append(n)
    return found


def _paths(text, tree):
    """([existing files], [paths to create]) a step's text names: a repo-relative path with an extension. Nothing
    absolute and nothing that climbs out ('../x/y.py' reads as no path: the regex refuses a '.' or '/' before it)."""
    tree = getattr(tree, "members", tree)
    have, new = [], []
    for m in _PATH.finditer(text or ""):
        f = os.path.normpath(m.group(1).removeprefix("./")).replace(os.sep, "/")
        if f.startswith(("../", "/")) or f in (".", ".."):
            continue
        if f in tree:
            if f not in have:
                have.append(f)
        elif "/" in f and f not in new:  # a bare "notes.txt" in prose is no plan to create a file
            new.append(f)
    return have, new


class _Defs:
    """The defs of each file read, once per report: a file unread, unreadable or past the scan cap has none."""

    def __init__(self, root):
        self.root, self.seen, self.scoped, self.capped = root, {}, 0, False

    def of(self, f, named=False):
        """named: the step text names f, so the scan cap (which only bounds a wide scope's files) doesn't apply."""
        if f not in self.seen:
            if not named:
                self.scoped += 1
                if self.scoped > MAX_SCAN:  # not remembered: a later step that names f reads it
                    self.capped = True
                    return set()
            self.seen[f] = set()
            path = os.path.join(self.root, f)
            if os.path.isfile(path) and not os.path.islink(path):
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        self.seen[f] = set(_DEFS.findall(fh.read(MAX_BYTES)))
                except OSError:
                    pass
        return self.seen[f]


def _scope(b, tree):
    globs = [str(g) for g in (b.meta.get("scope") or []) if str(g).strip()]
    tree = tree if hasattr(tree, "glob") else _Tree(tree)
    return globs, sorted(set().union(*(tree.glob(g) for g in globs)))


def touch(p, b, tree=None, defs=None):
    """[{n, text, done, files, symbols: {file: [name]}, creates}] per step of b. files: the existing files the step's
    text names, and the scope files that define a symbol it names; creates: the paths it names that don't exist."""
    tree = _tree(p) if tree is None else tree
    defs = defs or _Defs(p.root)
    _, scoped = _scope(b, tree)
    rows = []
    for s in b.steps():
        have, new = _paths(s.text, tree)
        wanted, syms = names(s.text), {}
        if wanted:
            for f in dict.fromkeys(have + [x for x in scoped if x not in have]):
                hit = sorted(n for n in wanted if n in defs.of(f, named=f in have))
                if hit:
                    syms[f] = hit
        files = sorted(set(have) | set(syms))
        rows.append({"n": s.n, "text": c.plain(s.text), "done": s.done, "files": files, "symbols": syms, "creates": new})
    return rows


def _claim(b, tree):
    """What a task's plan reaches: its scope globs, the files they and its step text name, paths to create, symbols."""
    globs, scoped = _scope(b, tree)
    text = "\n".join(s.text for s in b.steps())
    have, new = _paths(text, tree)
    literal = [g.strip().rstrip("/") for g in globs if not any(ch in g for ch in "*?[")]
    files = set(scoped) | set(have) | set(new) | {c.plain(x) for x in literal if "/" in x or "." in x}
    return {"globs": globs, "files": files, "names": set(names(text)), "scoped": set(scoped), "have": set(have),
            "tree": tree}


def _reach(a, b):
    """The files of b's plan a's plan reaches: the ones it names or scopes, and the ones its globs match (a file in the
    tree is matched by the tree's remembered globs; one not there yet, a path to create, by the glob itself)."""
    tree = a["tree"]
    hit = {f for f in b["files"] if f in a["files"]}
    for f in b["files"] - hit:
        if f not in tree.members and any(c.glob_match(f, g) for g in a["globs"]):
            hit.add(f)
    return hit | {f for f in b["scoped"] if any(f in tree.glob(g) for g in a["globs"])}


def _defined(name, claims, defs, tree):
    """Whether a file of either claim, one that exists, defines name: a word two plans share in prose is no collision.
    Only a file a step's text names is read past the scan cap; the files a wide scope glob matches count against it."""
    return any(name in defs.of(f, named=f in cl["have"]) for cl in claims for f in sorted(cl["files"])
               if f in tree.members)


def collide(p, b, tree=None, briefs=None, defs=None):
    """[{id, title, status, files, symbols}]: the other open tasks (any lane) whose scope or step text reaches a file or
    names a symbol this task's does. A hub file every task edits (CHANGELOG.md, docs/map/) is not a collision. Those
    sharing a symbol come first, then the most files."""
    from sampo import lane_overlap
    tree = _tree(p) if tree is None else tree
    defs = defs or _Defs(p.root)
    mine = _claim(b, tree)
    rows = []
    for o in (c.load_briefs(p) if briefs is None else briefs):
        if o.id == b.id or o.status not in c.OPEN:
            continue
        theirs = _claim(o, tree)
        files = sorted(f for f in _reach(theirs, mine) | _reach(mine, theirs) if not lane_overlap._hub(f))
        syms = sorted(n for n in mine["names"] & theirs["names"] if _defined(n, (mine, theirs), defs, tree))
        if files or syms:
            rows.append({"id": o.id, "title": c.plain(o.title), "status": o.status, "files": files[:MAX_LISTED],
                         "more": max(0, len(files) - MAX_LISTED), "symbols": syms[:MAX_LISTED]})
    return sorted(rows, key=lambda r: (not r["symbols"], -len(r["files"]) - r["more"], r["id"]))


def _soft(module, attr):
    """module.attr when the module imports and has it, else None: a gatemap or lane preview not merged yet is skipped."""
    try:
        return getattr(importlib.import_module(module), attr, None)
    except Exception:  # absent, or broken: an advisory part never stops the report
        return None


def _predicted(p, files):
    predict = _soft("sampo.gatemap", "predict")
    if not predict or not files:
        return []
    try:
        return [{"gate": r["gate"], "confidence": r["confidence"], "fix": r["fix"]} for r in predict(files, p=p)]
    except Exception:
        return []


def _preview(p, b):
    fn = _soft("sampo.lanes", "preview")
    if not fn:
        return []
    try:
        r = fn(p, b)
    except Exception as e:  # unverified against T-3268's real signature: say so rather than stay blank
        print(f"kickoff: lanes.preview failed ({type(e).__name__}: {c.plain(str(e))[:80]})", file=sys.stderr)
        return []
    return [c.plain(str(x)) for x in ([r] if isinstance(r, str) else r or [])][:2]


def _tests(p, files, scope):
    from sampo import map as smap
    try:
        return smap.tests_for(smap.load(p), sorted(set(files) | set(scope)))
    except Exception:  # not a git repo, or a map that won't build: no tests named, not a crash
        return []


def report(p, b):
    """{id, steps, tests, gates, collisions, predicted, preview, verdict}: everything kickoff prints, as data."""
    from sampo import cli
    tree = _tree(p)
    defs = _Defs(p.root)
    steps = touch(p, b, tree, defs)
    scope, scoped = _scope(b, tree)
    files = sorted({f for s in steps for f in s["files"]} | set(scoped))
    creates = sorted({f for s in steps for f in s["creates"]})
    hits = collide(p, b, tree, defs=defs)
    verdict = _verdict(steps, scope, hits, tree)
    return {"id": b.id, "steps": steps, "tests": _tests(p, files, scope),
            "gates": [{"gate": g, "how": h} for g, h in cli.gates(b.type, b.tier)], "collisions": hits,
            "predicted": _predicted(p, sorted(set(files) | set(creates))), "preview": _preview(p, b), "verdict": verdict,
            "capped": defs.capped}


def _verdict(steps, scope, hits, tree):
    if not steps and not scope:
        return "no steps and no scope: nothing to dry-run; plan the task first"
    if not hits and not tree:  # a non-git project or an empty tree: nothing matched because nothing was there
        return "nothing to compare: no tracked files found (not a git repo?)"
    if not hits:
        return "clear: no other open task reaches these files or symbols"
    ids = ", ".join(r["id"] for r in hits[:COLLIDERS_SHOWN])
    return f"{len(hits)} open task{'s' if len(hits) != 1 else ''} overlap ({ids}): agree the order or split the files before the first edit"


def _more(items, n):
    return ", ".join(items[:n]) + (f" +{len(items) - n}" if len(items) > n else "")


def lines(r):
    """The screen: under 20 lines whatever the plan's size."""
    out = [f"kickoff {r['id']}: {len(r['steps'])} step{'s' if len(r['steps']) != 1 else ''}" if r["steps"]
           else f"kickoff {r['id']}: no steps"]
    for s in r["steps"][:STEPS_SHOWN]:
        syms = [f"{f.rsplit('/', 1)[-1]}: {_more(n, 3)}" for f, n in s["symbols"].items()]
        reach = [f"creates {_more(s['creates'], 2)}"] if s["creates"] else []
        reach = ([_more(s["files"], 3)] if s["files"] else []) + reach
        tail = " -> " + "; ".join(reach) if reach else " -> scope only"
        tail += f" ({'; '.join(syms[:2])})" if syms else ""
        out.append(f"  {s['n']}. {'[x]' if s['done'] else '[ ]'} {c.fit(s['text'], 60)}{c.fit(tail, 80)}")
    if len(r["steps"]) > STEPS_SHOWN:
        out.append(f"  … {len(r['steps']) - STEPS_SHOWN} more steps")
    out.append("tests: " + (_more(r["tests"], 4) if r["tests"] else "none linked by map"))
    out.append("gates: " + _more([g["gate"] for g in r["gates"]], 8))
    if r["predicted"]:
        out.append(c.fit("predicted: " + _more([f"{x['gate']} {round(x['confidence'] * 100)}%" for x in r["predicted"]], 5), 140))
    for x in r["preview"]:
        out.append(c.fit("lanes: " + x, 140))
    for h in r["collisions"][:COLLIDERS_SHOWN]:
        what = _more(h["files"] + [f"def {s}" for s in h["symbols"]], 4)
        out.append(c.fit(f"collides {h['id']} [{h['status']}] {c.fit(h['title'], 40)}: {what}", 140))
    if len(r["collisions"]) > COLLIDERS_SHOWN:
        out.append(f"  … {len(r['collisions']) - COLLIDERS_SHOWN} more open tasks overlap")
    if r.get("capped"):
        out.append(f"scan capped: only the first {MAX_SCAN} scope files were read for symbols")
    out.append("verdict: " + r["verdict"])
    return out


def cmd_kickoff(args):
    from sampo import cli
    p = cli.resolve(args)
    r = report(p, cli.need_brief(p, args.id))
    return cli.out(args, r, "\n".join(lines(r)))
