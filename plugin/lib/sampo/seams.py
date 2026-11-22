"""sampo seams FILE (T-3290, moon C-006917): the cleanest places to split a big Python module. The module's top-level
definitions (functions, classes, constants) are a graph, an edge wherever one names another; a hub (named by, or naming,
HUB or more others) and anything touching module-level mutable state is taken out; each connected piece left, of two or
more definitions, is a cluster. A cluster's `back` edges are what it still names outside itself (so a move would need
an import back), `into` is what outside names in it (a re-export covers those). Ranked: fewest boundary edges, then the
most recently edited (commits among the last 500 that touched its lines), then the smaller. Approximation: names are
matched by spelling (a local of the same name links too), and a commit's lines map onto the definitions as they are at HEAD."""
import ast
import os
import re
import subprocess
import sys

from sampo import core as c
from sampo import hotfn
from sampo.find import _git

HUB = 8  # a definition linked to this many others is shared infrastructure, not part of any one cluster
MIN_FUNCS = 2  # a cluster needs this many functions or classes; a lone constant is not a split
MAX_BYTES = 1_000_000
SAFE_CALLS = {"re.compile", "frozenset", "tuple", "str", "int", "float", "bytes"}  # a call whose result is immutable
IMMUTABLE = (ast.Constant, ast.Tuple, ast.JoinedStr, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Name, ast.Attribute)
PATCH = re.compile(r"""(?:patch(?:\.\w+)?|setattr)\(\s*[\w.]*,\s*["'](\w+)["']"""  # patch.object(louhi, "NAME", ...)
                   r"""|patch(?:\.\w+)?\(\s*["'][\w.]*\.(\w+)["']"""  # patch("sampo.louhi.NAME", ...)
                   r"""|\b\w+\.(\w+)\s*=(?!=)""")  # louhi.NAME = ...
LOG = hotfn.LOG  # the same read of git as hotfn: --relative, so the paths match a file below the git toplevel


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *parts[::-1]]) if isinstance(node, ast.Name) else ""


def _mutable(value):
    """True when a module-level value can change under a reader: a container, a comprehension, a call to anything but a
    known pure constructor (a lock, an object, a counter), a tuple holding such."""
    if isinstance(value, ast.Call):
        return _dotted(value.func) not in SAFE_CALLS
    if isinstance(value, (ast.Tuple, ast.JoinedStr, ast.BinOp, ast.UnaryOp, ast.Compare)):
        return any(_mutable(v) for v in ast.iter_child_nodes(value) if isinstance(v, ast.expr))
    return not isinstance(value, IMMUTABLE)


def _span(node):
    return min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", ()))]), node.end_lineno


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _imported(node):
    return {(a.asname or a.name).split(".")[0] for a in node.names}


def _scan(tree):
    """(nodes, imports, pinned, tainted, extra): nodes {name: (first, last, kind, ast)} of the module's functions, classes
    and constants; imports the names bound by an import; pinned the names that other module-level code (an `if`, a
    decorator call, a loop) uses by name; tainted the mutable globals, and every name declared `global`; extra the names
    bound by module-level code no cluster can take along."""
    nodes, imports, tainted, assigned = {}, set(), set(), {}
    rest = []
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first, last = _span(st)
            nodes[st.name] = (first, last, "def", st)
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            imports |= _imported(st)
        elif isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None and all(
                isinstance(t, ast.Name) for t in (st.targets if isinstance(st, ast.Assign) else [st.target])):
            for t in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                assigned[t.id] = assigned.get(t.id, 0) + 1
                nodes[t.id] = (st.lineno, st.end_lineno, "const", st)
                if _mutable(st.value):
                    tainted.add(t.id)
        else:
            rest.append(st)
    tainted |= {n for n, k in assigned.items() if k > 1}  # rebound: not a constant
    for fn in ast.walk(tree):
        if isinstance(fn, ast.Global):
            tainted |= set(fn.names)
        elif isinstance(fn, ast.AugAssign) and isinstance(fn.target, ast.Name) and fn.target.id in nodes:
            tainted.add(fn.target.id)
    pinned, extra = set(), set()
    for st in rest:
        pinned |= _names(st)
        for n in ast.walk(st):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                imports |= _imported(n)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                extra.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                extra.add(n.name)
    return nodes, imports, pinned & set(nodes), tainted & set(nodes), extra - set(nodes) - imports


def analyse(text, hub=HUB):
    """{clusters, hubs, tainted} for one module's source. A cluster: {funcs (source order), spans, lines, back, into,
    uses (the module-level imports it needs)}. An unparsable text has no clusters."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {"clusters": [], "hubs": [], "tainted": []}
    nodes, imports, pinned, tainted, extra = _scan(tree)
    body = {n: _names(v[3].value if v[2] == "const" else v[3]) for n, v in nodes.items()}  # a constant: its value only
    refs = {n: (names & set(nodes)) - {n} for n, names in body.items()}
    loose = {n: len(names & extra) for n, names in body.items()}  # names bound by code no cluster can take along
    tainted |= {n for n, bs in refs.items() if bs & tainted}  # what names a mutable global touches shared state
    edges = {(a, b) for a, bs in refs.items() for b in bs}
    nbr = {n: set() for n in nodes}
    for a, b in edges:
        nbr[a].add(b)
        nbr[b].add(a)
    hubs = {n for n in nodes if len(nbr[n]) >= hub and n not in tainted}
    dropped = hubs | tainted
    seen, clusters = set(), []
    for start in sorted(nodes, key=lambda n: nodes[n][0]):
        if start in seen or start in dropped:
            continue
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            n = stack.pop()
            comp.append(n)
            for m in nbr[n]:
                if m not in dropped and m not in seen:
                    seen.add(m)
                    stack.append(m)
        if sum(1 for n in comp if nodes[n][2] == "def") < MIN_FUNCS:
            continue
        inside = set(comp)
        comp.sort(key=lambda n: nodes[n][0])
        used = set().union(*(_names(nodes[n][3]) for n in comp)) & imports
        clusters.append({
            "funcs": comp,
            "spans": [(nodes[n][0], nodes[n][1]) for n in comp],
            "lines": sum(nodes[n][1] - nodes[n][0] + 1 for n in comp),
            "back": sum(1 for a, b in edges if a in inside and b not in inside) + sum(loose[n] for n in comp),
            "into": sum(1 for a, b in edges if b in inside and a not in inside) + len(inside & pinned),
            "uses": sorted(used)})
    return {"clusters": clusters, "hubs": sorted(hubs), "tainted": sorted(tainted)}


def rank(clusters):
    """Cleanest first (fewest boundary edges, a name some test patches counting as one), then the most edited, then the
    smaller; each row a copy. Pure."""
    return sorted((dict(k) for k in clusters),
                  key=lambda k: (k["back"] + k["into"] + len(k.get("patched", ())), -k.get("churn", 0), k["lines"],
                                 k["funcs"]))


def patched(root, rel, names):
    """The `names` a test patches on the module `rel` (mock.patch.object(louhi, "NAME"), patch("sampo.louhi.NAME"),
    monkeypatch.setattr(louhi, "NAME", ...), or `louhi.NAME = ...`): a moved copy would not see such a patch, so a
    cluster holding one is not a clean seam. Reads the tracked test files; a name quoted on a patch line that also
    names the module counts, whichever object it patches (over-reports on purpose)."""
    stem = os.path.splitext(os.path.basename(rel))[0]
    try:
        ls = _git(root, "ls-files", "-z", "--", "*test*.py", timeout=60)
    except (OSError, subprocess.SubprocessError):
        ls = None
    found = set()
    for path in (os.fsdecode(x) for x in (ls or b"").split(b"\0") if x):
        try:
            if os.path.getsize(os.path.join(root, path)) > MAX_BYTES:
                continue
            with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError:
            continue
        for ln in lines:
            if stem not in ln:
                continue
            for m in PATCH.finditer(ln):
                found.add(m.group(1) or m.group(2) or m.group(3))
    return sorted(found & set(names))


def collect(root, rel, hub=HUB):
    """The ranked clusters of the tracked file `rel` under the checkout `root`, each with `churn` (commits among the
    last 500 that touched its lines), or None when root isn't a git checkout, git timed out, or the file is unreadable."""
    try:
        if os.path.getsize(os.path.join(root, rel)) > MAX_BYTES:
            return None
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            text = f.read()
        log = _git(root, *LOG, "--", rel, timeout=120)
    except (OSError, UnicodeDecodeError, ValueError, subprocess.SubprocessError):
        return None
    if log is None:
        return None
    got = analyse(text, hub)
    changed = [files.get(rel, set()) for _sha, _subject, files in hotfn._parse(log.decode("utf-8", "replace"))]
    for k in got["clusters"]:
        own = {n for first, last in k["spans"] for n in range(first, last + 1)}
        k["churn"] = sum(1 for lines in changed if not own.isdisjoint(lines))
    hit = set(patched(root, rel, [n for k in got["clusters"] for n in k["funcs"]]))
    for k in got["clusters"]:
        k["patched"] = [n for n in k["funcs"] if n in hit]
    got["clusters"] = rank(got["clusters"])
    return got


def _table(got, top):
    rows = got["clusters"][:max(top, 0)]
    if not rows:
        return "No cluster of two or more definitions is left once hubs and mutable state are taken out."
    out = [f"{'#':>2} {'back':>4} {'into':>4} {'churn':>5} {'lines':>5}  functions"]
    for i, k in enumerate(rows, 1):
        names = ", ".join(k["funcs"])
        note = f"  [patched by tests: {', '.join(k['patched'])}]" if k.get("patched") else ""
        out.append(f"{i:>2} {k['back']:>4} {k['into']:>4} {k.get('churn', 0):>5} {k['lines']:>5}  {c.plain(names)}{note}")
    if got["hubs"]:
        out.append("hubs (left out): " + c.plain(", ".join(got["hubs"][:20])) + (" ..." if len(got["hubs"]) > 20 else ""))
    return "\n".join(out)


def cmd_seams(args):
    from sampo import cli
    p = c.find_project(os.getcwd())
    root = os.path.realpath(p.root) if p else os.getcwd()
    full = os.path.realpath(os.path.join(os.getcwd(), args.file))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        print(f"sampo seams: {args.file}: not a file inside the checkout", file=sys.stderr)
        return 2
    rel = os.path.relpath(full, root)
    got = collect(root, rel, args.hub)
    if got is None:
        print(f"sampo seams: {args.file}: unreadable, too big, or git failed under {root}", file=sys.stderr)
        return 2
    got = {**got, "file": rel, "clusters": got["clusters"][:max(args.top, 0)]}
    cli.out(args, got, _table(got, args.top))
    return 0
