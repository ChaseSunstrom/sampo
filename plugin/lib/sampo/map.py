"""fm map / fm impact (T-0044): a small map of the project for grounding — its gates, layout, entry points, the
files that change most, and which tests go with which source files (by name) — kept in the project's state dir and
rebuilt only when HEAD moves. Stdlib and git only; run by fm commands, never by a hook."""
import collections
import functools
import json
import os
import re

from sampo import core as c
from sampo.core import subprocess  # T-3577: imported on first use

_TEST = c.TESTISH  # T-0295: one test-file pattern
_ENTRY = c.lazy(r"(^|/)(__main__\.py|main\.\w+|cli\.\w+|app\.\w+|index\.\w+|manage\.py|bin/[^/]+)$")
_CODE = c.CODE
HOT = 8
OUT_MAX = 1500


_git = functools.partial(c._git, timeout=30)  # T-0961: fmcore's runner, stdout or ""


def _name_stem(path):
    base = os.path.basename(path).split(".")[0]
    return re.sub(r"^test_|_test$", "", base)


def _gates(root, files, checks):
    found = list(checks)
    names = set(files)
    runner = min((f for f in files if re.search(r"(^|/)(tests?/run(_tests)?|run_?tests)\.py$", f)), key=len, default=None)
    if runner:  # T-0987: the repo's own runner knows its import paths; unittest discover may not
        found.append(f"python3 {runner}")
    tests = [f for f in files if re.search(r"(^|/)(test_\w*|\w*_test)\.py$", f)]
    def mentions_pytest(name):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                return "pytest" in f.read(200_000)
        except OSError:
            return False
    pytest = names & {"pytest.ini", "conftest.py", "tox.ini"} or any(f.endswith("/conftest.py") for f in files) \
        or any(n in names and mentions_pytest(n) for n in ("pyproject.toml", "setup.cfg", "requirements-dev.txt"))
    if pytest:
        found.append("python3 -m pytest -q")  # T-0850: a command that runs, not "pytest (or unittest)"
    if "package.json" in names:
        try:
            scripts = json.load(open(os.path.join(root, "package.json"))).get("scripts") or {}
            found += [f"npm run {k}" for k in ("test", "lint", "typecheck", "build") if k in scripts]
        except (OSError, ValueError, AttributeError):
            pass
    if "Cargo.toml" in names:
        found.append("cargo test")
    if "go.mod" in names:
        found.append("go test ./...")
    if "Makefile" in names:
        try:
            with open(os.path.join(root, "Makefile"), encoding="utf-8", errors="replace") as f:
                targets = re.findall(r"(?m)^(test|lint|check)\s*:", f.read())
            found += [f"make {t}" for t in targets]
        except OSError:
            pass
    if tests and not pytest and not any("test" in g for g in found):  # T-0850: only when nothing else runs them
        found.append("python3 -m unittest discover -s tests -q" if all(f.startswith("tests/") for f in tests)
                     else "python3 -m unittest discover -q")
    return list(dict.fromkeys(found))


def build(p):
    root = p.root
    files = _git(root, "ls-files").splitlines()
    tracked = set(files)  # T-2838: once, not per churn line
    churn = collections.Counter(f for f in _git(root, "log", "--since=180.days", "--name-only", "--pretty=format:").splitlines()
                                if f and f in tracked)
    layout = collections.Counter(f.split("/", 1)[0] + "/" if "/" in f else "." for f in files)
    big = next((d for d, n in layout.items() if d != "." and n > 0.6 * len(files)), None)
    if big:  # one folder holds most of the repo: show what's inside it instead
        del layout[big]
        layout.update("/".join(f.split("/")[:2]) + "/" if f.count("/") >= 2 else big for f in files if f.startswith(big))  # not a list: a path or word fragment, not a list
    sources = [f for f in files if _CODE.search(f) and not _TEST.search(f)]
    tops = {f.split("/", 1)[0] for f in files if "/" in f}
    mix = collections.defaultdict(collections.Counter)  # T-2267: code vs not, per second-level folder (plugin/skills)
    for f in files:
        if f.count("/") >= 2:
            mix["/".join(f.split("/")[:2])][bool(_CODE.search(f))] += 1  # not a list: a path or word fragment, not a list
    prose = {d for d, n in mix.items() if n[False] > n[True]}
    links = {}
    for t in (f for f in files if _TEST.search(f) and _CODE.search(f)):
        stem, ext = _name_stem(t), os.path.splitext(t)[1]
        # same language; the source's name is the test's, or ends with it (test_guard.py → fmguard.py)
        # T-0709: a short name links when only a short prefix comes before it (test_bus.py → fmbus.py)
        srcs = [s for s in sources if s.endswith(ext) and (_name_stem(s) == stem or (_name_stem(s).endswith(stem) and (
            len(stem) >= 4 or len(_name_stem(s)) - len(stem) <= 2)))]
        srcs += _folders(root, t, tops, prose)
        srcs += [s for s in _marked(root, t) if s in tracked and s not in srcs]
        if srcs:
            links[t] = srcs
    lib = _lib(root, files)
    return {"version": VERSION, "head": _git(root, "rev-parse", "HEAD").strip(), "gates": _gates(root, files, c.read_meta(p).get("checks") or []),
            "layout": layout.most_common(12), "entry": [f for f in files if _ENTRY.search(f)][:8],
            "hot": [f for f, _ in churn.most_common(HOT)], "tests": links, "files": len(files),
            "ci": _ci(root, files), "pairs": _co_change(root, set(files)), "fixy": _fixy(root, set(files)),
            "broke": _broke(p, set(files)), "lib": [x["name"] for x in lib],
            "lib_cards": lib, "lib_stamp": _cards_stamp()}


def _cards_stamp():
    """How many stack cards there are: a Sampo update that adds cards rebuilds the map's list (a few listdirs)."""
    root = os.path.join(c.PLUGIN_ROOT, "skills", "library", "cards")
    try:
        return sum(len(os.listdir(os.path.join(root, d))) for d in os.listdir(root))
    except OSError:
        return 0


def _lib(root, files):
    try:
        from sampo import lib as slib
        return slib.for_map(root, files)
    except Exception:  # the library is a hint: never the map
        return []


_CI_FILE = c.lazy(r"^(\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.yml)$")
_CI_RUN = c.lazy(r"(?m)^\s*(?:-\s+)?(?:run:\s*|-\s+)(?![|>])([^#\n]*\b(test|lint|check|typecheck|vet|clippy|fmt|"
                 r"format|build|mypy|ruff|flake8|eslint|pytest|tox)\b[^#\n]*)$")


def _ci(root, files):
    """R3 (mirror CI): the check-like commands the CI workflows run, so local gates can match what CI enforces."""
    found = []
    for f in (f for f in files if _CI_FILE.match(f)):
        try:
            with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                found += [m.group(1).strip().strip("'\"") for m in _CI_RUN.finditer(fh.read())]
        except OSError:
            pass
    return list(dict.fromkeys(x for x in found if not x.startswith(("uses:", "name:"))))[:12]


def _co_change(root, files, min_together=3, share=0.6):
    """R2 (companion edits): {file: [files it changed with in ≥ 60% of its last year's commits, ≥ 3 times]}."""
    log = _git(root, "log", "--since=365.days", "--name-only", "--pretty=format:%x00")
    alone, together = collections.Counter(), collections.Counter()
    for commit in log.split("\0"):
        names = sorted({f for f in commit.split("\n") if f in files})
        if not 2 <= len(names) <= 20:  # a sweeping commit says nothing about companions
            alone.update(names)
            continue
        alone.update(names)
        together.update((a, b) for a in names for b in names if a != b)
    pairs = {}
    for (a, b), n in together.items():
        if n >= min_together and n / alone[a] >= share:
            pairs.setdefault(a, []).append(b)
    return {a: sorted(bs)[:3] for a, bs in pairs.items()}


def drift(root, top=8, owed_min=2.0, min_together=3, share=0.5):
    """T-2073: files the recent commits would normally have brought along, learned from the repo's history, not a
    list: for each commit since file B last changed, B is owed max P(B|A) over that commit's files A (P from the last
    year's commits of at most 20 files, pairs seen 3+ times, B in at least half of A's commits: a file in every commit, like a
    changelog, says nothing about the rest). [(B, owed, the A that owes most, how many of those
    commits had it, P(B|A))] for files still present and owed at least owed_min, most owed first; None when git fails."""
    r = c._git(root, "log", "--since=365.days", "--name-only", "--pretty=format:%x00", timeout=30, proc=True)
    if r.returncode != 0:
        return None  # no git or no history: unknown, never "nothing owed"
    log = r.stdout
    commits = [x for x in (sorted(set(filter(None, s.split("\n")))) for s in log.split("\0")) if x]  # newest first
    alone, together = collections.Counter(), collections.Counter()
    for names in commits:
        alone.update(names)
        if len(names) <= 20:  # a sweeping commit says nothing about companions
            together.update((a, b) for a in names for b in names if a != b)
    partners = collections.defaultdict(dict)
    for (a, b), n in together.items():
        if n >= min_together and n / alone[a] >= share:
            partners[a][b] = n / alone[a]
    owed, why, seen = collections.Counter(), collections.defaultdict(collections.Counter), set()
    for names in commits:
        seen.update(names)  # a file changed here or later owes nothing for this commit
        best = {}
        for a in names:
            for b, pr in partners.get(a, {}).items():
                if b not in seen and pr > best.get(b, (0, ""))[0]:
                    best[b] = (pr, a)
        for b, (pr, a) in best.items():
            owed[b] += pr
            why[b][a] += 1
    out = []
    for b, n in owed.most_common():
        if n < owed_min or len(out) >= top:
            break
        if os.path.exists(os.path.join(root, b)):
            a, k = why[b].most_common(1)[0]
            out.append((b, n, a, k, partners[a][b]))
    return out


_FIXY = c.lazy(r"\b(fix(es|ed)?|revert(ed)?|bugs?|hotfix)\b", re.I)


def _fixy(root, files):
    """Hotspots (wild C18): {file: n} for files in 3+ fix/revert/bug commits of the last 180 days, worst first, 50 at most.
    A sweeping commit (over 20 files) says nothing about one file."""
    n = collections.Counter()
    for commit in _git(root, "log", "--since=180.days", "--name-only", "--format=%x00%s").split("\0")[1:]:
        subject, _, rest = commit.partition("\n")
        names = {f for f in rest.split("\n") if f in files}
        if _FIXY.search(subject) and len(names) <= 20:
            n.update(names)
    return {f: k for f, k in n.most_common(50) if k >= 3}


def _broke(p, files):
    """T-2148: {file: [test files]} from closed baseline-red tasks: each file its fix commit (subject naming "(ID)")
    changed outside tests, pointed at the tests the gate named failing. A hub file (MASTER.md, in every task's pairs)
    carries no co-change signal, yet an edit to it broke test_docs once (T-2147): this brings that test back.
    A fix's docs (its CHANGELOG line) are a record, not the cause, unless docs were all the fix changed; then a doc
    links only to a test whose source names it (test_docs reads MASTER.md, not CHANGELOG.md). Archived tasks count."""
    named, tests = {}, [f for f in files if _TEST.search(f)]
    for b in c.load_briefs(p, include_archive=True):
        if not (b.meta.get("baseline") and b.status == "done"):
            continue
        m = re.search(r"` fails (.+?)\. Captured", b.section("Raw request"))
        segs = set()
        for n in (m.group(1).replace(" …", "").split(", ") if m else []):  # a/test_x.py::t, tests.test_x.C.t
            segs |= {n.split("::")[0], *re.split(r"[./:]+", n)}
        hit = [f for f in tests if f in segs or os.path.basename(f).rsplit(".", 1)[0] in segs]
        if hit:
            named[b.id] = hit
    out = collections.defaultdict(set)
    if named:
        for commit in _git(p.root, "log", "--since=365.days", "--name-only", "--format=%x00%s").split("\0")[1:]:
            subject, _, rest = commit.partition("\n")
            changed = [f for f in rest.split("\n") if f]
            for tid in set(re.findall(r"\((T-\d+)\)", subject)) & set(named) if len(changed) <= 20 else ():
                fixed = [f for f in changed if f in files and not _TEST.search(f)]
                code = [f for f in fixed if not f.endswith(".md")]
                for f in code:
                    out[f].update(named[tid])
                for f in [] if code else fixed:
                    out[f].update(t for t in named[tid] if os.path.basename(f) in _source(p.root, t))
    return {f: sorted(ts) for f, ts in out.items() if ts}


def _source(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


VERSION = 13  # bump when build() changes, so older maps are rebuilt


def load(p, rebuild=False):
    """The map, rebuilt when HEAD has moved since it was built, it was built by older code, or on request."""
    path = os.path.join(p.dir, "map.json")
    try:
        m = json.load(open(path))
    except (OSError, ValueError):
        m = None
    head = _git(p.root, "rev-parse", "HEAD").strip()
    if rebuild or not m or m.get("head") != head or m.get("version") != VERSION or m.get("lib_stamp") != _cards_stamp():
        m = build(p)
        c.write_atomic(path, json.dumps(m))
    return m


_MARKED = c.lazy(r"^\s*#\s*map-links:\s*(.+)$", re.M)


def _marked(root, test):
    """T-2838: files a test names on a '# map-links: PATH …' line, for a test that guards them without being named after
    them (it reads their source as text: test_core counts sandbox_env calls in louhi.py and long.py)."""
    try:
        with open(os.path.join(root, test), encoding="utf-8", errors="replace") as f:
            text = f.read(200_000)
    except OSError:
        return []
    return [w for line in _MARKED.findall(text) for w in line.split()]


def _folders(root, test, tops, prose=()):
    """T-2086: top-level folders a test names as a string literal ("site"), as "site/" links: test_design_lint lints
    all of site/ and is named after no file in it. The test's own folder (tests/, plugin/) would link everything.
    T-2267: also a mostly non-code folder beside the test's own (plugin/rules beside plugin/tests); a code folder
    (plugin/lib) links by file name only, or every lib edit would pull in each test that names "lib"."""
    path = os.path.join(root, test)
    try:
        if os.path.islink(path) or not _TEST.search(os.path.basename(test)):  # a fixture or helper under tests/
            return []
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(200_000)
    except OSError:
        return []
    words = set(re.findall(r"""["']([^"'/\s]+)["']""", text))
    up, own = test.split("/", 1)[0], "/".join(test.split("/")[:2])  # not a list: a path or word fragment, not a list
    side = {d for d in prose if d.startswith(up + "/") and d != own and d.split("/", 1)[1] in words}
    return [d + "/" for d in sorted(words & (tops - {up}))] + [d + "/" for d in sorted(side)]


def tests_for(m, paths):
    """Tests linked by name to any of paths (files or globs), or naming a folder that holds one (T-2086)."""
    import fnmatch
    return sorted({t for t, srcs in m["tests"].items()
                   if any(fnmatch.fnmatch(s, g) or s == g.rstrip("/") or s.startswith(g.rstrip("*/") + "/")
                          or (s.endswith("/") and g.startswith(s)) for s in srcs for g in paths)})


_HUNK = c.lazy(r"(?m)^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _bound(node):
    """Names a top-level assignment or import binds; None for module-level code that runs something (T-3306)."""
    import ast
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        if any(a.name == "*" for a in node.names):  # binds names we can't list
            return None
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    return None


def _defined(node):
    import ast
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    return _bound(node)


def _verbs(tree, names):
    """Verbs registered as add("verb", handler) or add("verb", lazy(mod, "cmd_x")) with a handler in names."""
    import ast
    out = set()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "add" and len(n.args) >= 2
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
            continue
        h = n.args[1]
        if (isinstance(h, ast.Name) and h.id in names) or (
                isinstance(h, ast.Call) and isinstance(h.func, ast.Name) and h.func.id == "lazy" and len(h.args) >= 2
                and isinstance(h.args[1], ast.Constant) and h.args[1].value in names):
            out.add(n.args[0].value)
    return out


def _close(src, names):
    """names plus every top-level name whose code (body, defaults, decorators) mentions one, repeated until nothing
    is added, plus the verbs registered to a handler in the set (and x for a cmd_x). None when module-level code that
    runs something mentions one (T-3306 review: a dropped test hides a regression, so over-select)."""
    import ast
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    names, seen = set(names), True
    while seen:
        seen = False
        for node in tree.body:
            if not any(isinstance(x, ast.Name) and x.id in names for x in ast.walk(node)):
                continue
            if (d := _defined(node)) is None:
                if not isinstance(node, ast.Expr):  # a bare docstring mentions nothing
                    return None
                continue
            if not d <= names:
                names |= d
                seen = True
    names |= {n[4:] for n in names if n.startswith("cmd_")}
    return names | _verbs(tree, names)


def _names_at(src, lines):
    """Symbols of the top-level defs, classes and bindings that source lines `lines` (1-based) touch; None when one
    can't be told (syntax error, a docstring or code that runs at module level)."""
    import ast
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    rows, found = src.splitlines(), set()
    for n in lines:
        node = next((x for x in tree.body if min([x.lineno] + [d.lineno for d in getattr(x, "decorator_list", [])]) <= n
                     <= x.end_lineno), None)
        if node is None:  # between nodes: a blank line or comment is nothing, anything else we can't place
            if 0 < n <= len(rows) and rows[n - 1].strip() and not rows[n - 1].lstrip().startswith("#"):
                return None
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
            for sub in getattr(node, "body", []) if isinstance(node, ast.ClassDef) else []:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.lineno <= n <= sub.end_lineno:
                    found.add(sub.name)
        elif (b := _bound(node)) is not None:
            found |= b
        else:
            return None
    return found


def _diff_lines(root, base, tree, paths):
    """{path: (old text, new text, old lines, new lines)} for each of paths that differs between `base` and git tree
    `tree` in both (one `git diff -U0`; the lines 1-based); a path missing from either side or the diff is left out."""
    out = {}
    if not tree or not paths:
        return out
    diff = c._git(root, "diff", "-U0", "--no-renames", base, tree, "--", *paths, timeout=300, fail=None)
    if not diff:
        return out
    parts = re.split(r"(?m)^diff --git ", diff)
    for p in paths:
        sec = next((s for s in parts if s.startswith(f"a/{p} b/{p}\n")), None)
        if sec is None or not _HUNK.search(sec):
            continue
        old = c._git(root, "show", f"{base}:{p}", fail=None)
        new = c._git(root, "show", f"{tree}:{p}", fail=None)
        if old is None or new is None:
            continue
        ranges = ([], [])
        for a, b, x, y in _HUNK.findall(sec):
            ranges[0].extend(range(int(a), int(a) + int(b or 1)) if b != "0" else [])
            ranges[1].extend(range(int(x), int(x) + int(y or 1)) if y != "0" else [])
        out[p] = (old, new, *ranges)
    return out


def changed_symbols(root, base, tree, paths):
    """{path: set of top-level names (and methods) | None}: the names whose lines differ between `base` and git tree
    `tree`, from one `git diff -U0` mapped through ast on both versions, then closed over in-file use (_close); None
    when that can't be told, so the caller falls back to the whole file (T-3306)."""
    out = {p: None for p in paths}
    for p, (old, new, lo, ln) in _diff_lines(root, base, tree, paths).items():
        olds, news = _names_at(old, lo), _names_at(new, ln)
        if olds is None or news is None:
            continue
        names = olds | news
        co, cn = _close(old, names), _close(new, names)
        out[p] = None if co is None or cn is None else co | cn
    return out


_FUNC = ("FunctionDef", "AsyncFunctionDef")


def _first(node):
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])


def _quals_at(tree, rows, lines):
    """(function qualnames, class prefixes, bound names) that source lines `lines` touch, as the test runner's record
    names them (runcover: "f", "K.meth"; a nested def is its outer function's): a function's lines (decorators and
    signature too) are its own; a class's own lines (header, attributes, a nested class) are every method's, and bind
    the class's name; a top-level assignment or import binds its names. None when one can't be told: module code that
    runs something, a docstring."""
    import ast
    quals, prefixes, bound = set(), set(), set()
    for n in lines:
        node = next((x for x in tree.body if _first(x) <= n <= x.end_lineno), None)
        if node is None:  # between nodes: a blank line or comment is nothing, anything else we can't place
            if 0 < n <= len(rows) and rows[n - 1].strip() and not rows[n - 1].lstrip().startswith("#"):
                return None
        elif type(node).__name__ in _FUNC:
            quals.add(node.name)
        elif isinstance(node, ast.ClassDef):
            sub = next((s for s in node.body if type(s).__name__ in _FUNC and _first(s) <= n <= s.end_lineno), None)
            if sub is not None:
                quals.add(f"{node.name}.{sub.name}")
            else:
                prefixes.add(node.name)
                bound.add(node.name)
        elif (b := _bound(node)) is not None:
            bound |= b
        else:
            return None
    return quals, prefixes, bound


def _reach(tree, quals, prefixes, bound):
    """quals and prefixes plus the top-level code that reads a bound name, repeated until nothing is added: a def
    reading one is run with the new value (its own record names it; its callers' records name it too, so no caller is
    added); a class reading one (a base, an attribute) is every method's and binds its name; an assignment or import
    reading one binds its names. None when module code that runs something reads one."""
    import ast
    reads = [{x.id for x in ast.walk(node) if isinstance(x, ast.Name)} for node in tree.body] if bound else []
    seen = bool(bound)
    while seen:
        seen = False
        for node, names in zip(tree.body, reads):
            if names.isdisjoint(bound):
                continue
            if type(node).__name__ in _FUNC:
                quals.add(node.name)
            elif isinstance(node, ast.ClassDef):
                prefixes.add(node.name)
                seen |= node.name not in bound
                bound.add(node.name)
            elif (b := _bound(node)) is not None:
                seen |= not b <= bound
                bound |= b
            else:
                return None
    return quals, prefixes


def _methods(tree):
    import ast
    return {f"{k.name}.{s.name}" for k in tree.body if isinstance(k, ast.ClassDef)
            for s in k.body if type(s).__name__ in _FUNC}


def _ran_quals(old, new, lo, ln):
    """(function qualnames, class prefixes) a change from source old to new reaches (both versions mapped and closed
    over bindings; a method added or removed is its class's, as it may now hide or show a base's), None when it can't
    be told."""
    import ast
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            to, tn = ast.parse(old), ast.parse(new)
    except (SyntaxError, ValueError, RecursionError):
        return None
    a, b = _quals_at(to, old.splitlines(), lo), _quals_at(tn, new.splitlines(), ln)
    if a is None or b is None:
        return None
    quals, prefixes, bound = a[0] | b[0], a[1] | b[1], a[2] | b[2]
    for m in _methods(to) ^ _methods(tn):
        prefixes.add(m.split(".")[0])
        bound.add(m.split(".")[0])
    ra, rb = _reach(to, set(quals), set(prefixes), set(bound)), _reach(tn, set(quals), set(prefixes), set(bound))
    if ra is None or rb is None:
        return None
    return ra[0] | rb[0], ra[1] | rb[1]


def ran_tests(root, base, tree, paths):
    """T-3603: the test files of the classes the test runner's cache (plugin/tests/run.py: per class, what its last
    green runs read and the functions they ran) says ran a changed function of paths or read a changed file whole,
    between `base` and git tree `tree`: a test that drives code only through a hook's JSON or a child process (T-3670's
    test_firewall.OutlineFirst) names none of it, so no map link or symbol search finds it. A .py change that can't be
    told by function selects every class that ran any of the file. Only adds: a class without a record, or whose test
    file is gone, adds nothing."""
    common = c._git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", fail=None)
    if not paths or not tree or not common:
        return []
    import hashlib
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "sampo",
                         "run-cache", hashlib.sha1(os.fsencode(os.path.realpath(common.strip()))).hexdigest()[:16] + ".json")
    try:  # run.py's format: {"paths": [key, ...], "classes": {id: [[digest, tests, [index, ...]], ...]}}
        with open(cache) as f:
            d = json.load(f)
        table, classes = d["paths"], d["classes"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    changed, funcs, fn = set(paths), {}, set()
    for i, k in enumerate(table):
        f, sep, q = k.partition("::")
        if sep:
            fn.add(i)
            if f in changed and f.endswith(".py"):
                funcs.setdefault(f, []).append((i, q))
    hits = {i for i, k in enumerate(table) if k in changed or (k[:1] == "?" and k[1:] in changed)}
    lines = _diff_lines(root, base, tree, sorted(funcs))
    for f, keys in funcs.items():  # a file added, deleted or not text (no lines) is told by file
        reach = _ran_quals(*lines[f]) if f in lines else None
        heads = reach and reach[0] | reach[1]
        for i, q in keys:
            if reach is None or (q and (q in heads or any(q.startswith(s + ".") for s in heads))):
                hits.add(i)
    out = set()
    for cid, recs in classes.items() if hits else ():
        # a record from before function keys names each module the class imported as read whole: only a newer one counts
        # (T-3711: as a fallback it selected nearly every class for any change until a full re-record)
        idx = [i for r in recs if not fn.isdisjoint(r[2]) for i in r[2]]
        if not idx:
            continue
        if hits.isdisjoint(idx):
            continue
        mod = cid.rsplit(".", 1)[0].rpartition(".")[2] + ".py"  # run.py's id: the TestCase's module, then its name
        own = next((table[i].partition("::")[0] for i in idx if os.path.basename(table[i].partition("::")[0]) == mod), None)
        if own and c.TESTISH.search(mod) and os.path.isfile(os.path.join(root, own)):
            out.add(own)
    return sorted(out)


def render(m):
    lines = [f"Project map ({m['files']} files, HEAD {m['head'][:8]}; data, rebuilt when HEAD moves):"]
    lines.append("gates: " + ("; ".join(m["gates"]) or "none found (fm check add '<cmd>')"))
    lines.append("layout: " + ", ".join(f"{d} ({n})" for d, n in m["layout"]))
    if m["entry"]:
        lines.append("entry points: " + ", ".join(m["entry"]))
    if m["hot"]:
        lines.append("most changed (180 days): " + ", ".join(m["hot"]))
    if m["tests"]:
        tpairs, tmore = c.more(list(m["tests"].items()), 10)
        lines.append("tests → sources: " + "; ".join(f"{t} → {', '.join(s[:2])}" for t, s in tpairs)
                     + (f" ({tmore})" if tmore else ""))
    if m.get("ci"):
        lines.append("CI runs: " + "; ".join(m["ci"]))
    if m.get("lib"):
        lines.append("stack cards: " + ", ".join(m["lib"]) + " (fm lib show NAME)")
    out = "\n".join(c.plain(x) for x in lines)
    return out if len(out) <= OUT_MAX else out[:OUT_MAX - 1] + "…"


def compact(p, limit=500):
    """T-0215: one line of the cached map for the session-start context, or None. Never builds in the hook (1.5 s on a
    7k-file repo): a missing or stale map starts a detached `fm map`, and the next session has it."""
    try:
        with open(os.path.join(p.dir, "map.json"), encoding="utf-8") as f:
            m = json.load(f)
        if m.get("version") != VERSION:
            m = None
    except (OSError, ValueError):
        m = None
    head = _git(p.root, "rev-parse", "HEAD").strip()
    if (not m or m.get("head") != head or m.get("lib_stamp") != _cards_stamp()) and not os.environ.get("FOREMAN_NO_BACKGROUND"):
        try:
            subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "map", "--json"], cwd=p.root,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            pass
    if not m:
        return None
    parts = [f"gates: {c.joinmore(m['gates'], 3, '; ') or 'none'}"]
    if m["entry"]:
        parts.append(f"entry: {c.joinmore(m['entry'], 4)}")
    parts.append("layout: " + c.joinmore([f"{d} ({n})" for d, n in m["layout"]], 5))
    if m["tests"]:
        parts.append("tests→src: " + "; ".join(f"{t}→{s[0]}" for t, s in list(m["tests"].items())[:4] if s))
    if m.get("lib"):  # T-0933: before the tests, which the 500-char cut trims first
        parts.insert(-1 if m["tests"] else len(parts), "cards: " + ", ".join(m["lib"][:6]) + " (fm lib show NAME)")
    line = c.plain(f"Map ({m['files']} files{'' if m.get('head') == head else ', as of an older HEAD'}; fm map for "
                   f"more): " + " · ".join(parts))
    return line if len(line) <= limit else line[:limit - 1] + "…"


COLD_DAYS = 90  # how far back "hot" looks


def cold(p, n=30):
    """T-0660: [(file, commits)] among the n files changed most in the last COLD_DAYS that no session in this project
    has read or edited (the events' Read and Edit targets): the hot path nobody has looked at."""
    try:
        log = subprocess.run(["git", "-C", p.root, "log", f"--since={COLD_DAYS} days ago", "--name-only", "--format="],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    counts = collections.Counter(x for x in log.splitlines() if x.strip())
    seen = set()
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 50000):
        if e.get("kind") == "tool" and e.get("tool") in ("Read", "Edit") and e.get("project") == p.slug \
                and e.get("target"):
            t = str(e["target"])
            seen.add(os.path.relpath(t, p.root) if os.path.isabs(t) else t)
    return [(f, k) for f, k in counts.most_common(n) if f not in seen and os.path.isfile(os.path.join(p.root, f))]


def cmd_map(args):
    from sampo import cli
    p = cli.resolve(args)
    if not c.git_root(p.root):
        raise cli.UsageError("fm map needs a git repository")
    if getattr(args, "cold", False):
        rows = cold(p)
        made = None
        if rows and args.capture and not any(b.meta.get("source") == "cold" and b.status not in c.CLOSED
                                             for b in c.load_briefs(p)):  # one open item at a time
            files = c.joinmore([f for f, _ in rows], 8)
            with c.lock(p.dir):
                made = cli._create(p, f"Read the hot files no session has read: {c.fit(files, 160)}", "RESEARCH",
                                     "S", "captured", raw=f"Research debt (fm map --cold, T-0660): read and note what "
                                     f"each does — {files}. Local reading only.", source="cold")
                c.log_event(p, "capture", task=made.id, data={"source": "cold", "type": "RESEARCH"})
                c.regen_views(p)
        return cli.out(args, {"cold": [{"file": f, "commits": k} for f, k in rows], "captured": made.id if made else None},
                         (f"Hot files no session has read (commits in {COLD_DAYS} days):\n" + "\n".join(
                             f"  {f} ({k})" for f, k in rows[:20]) if rows else "Every hot file has been read.")
                         + (f"\nCaptured {made.id}." if made else ""))
    m = load(p, args.rebuild)
    cli.out(args, m, render(m))


def cmd_impact(args):
    """Likely tests (by name) and dependents (files that name the module) for a path."""
    from sampo import cli
    p = cli.resolve(args)
    m = load(p)
    rel = os.path.relpath(os.path.abspath(args.path), p.root) if os.path.isabs(args.path) or os.path.exists(args.path) \
        else args.path
    stem = _name_stem(rel)
    users = [f for f in _git(p.root, "grep", "-l", "-w", "-I", "-e", stem).splitlines() if f != rel and _CODE.search(f)]
    tests = tests_for(m, [rel])
    shown, umore = c.more(users, 15)
    data = {"path": rel, "tests": tests, "dependents": users[:15]}
    cli.out(args, data, c.plain_lines(f"{rel}\n  likely tests: {', '.join(tests) or 'none linked by name'}\n  mention "
                                 f"'{stem}': {', '.join(shown) or 'none'}{f' ({umore})' if umore else ''}"))


_COMMON_STEMS = {"index", "main", "init", "__init__", "utils", "util", "types", "setup", "config", "common", "helpers"}


def tour(p, b, since=None):
    """T-0248: the task's changed files (T-0885: or everything since `since`'s merge base with HEAD, uncommitted work
    included) in reading order — a file before the files that name it (fm impact's name-stem heuristic) — each with
    its +/- lines, the files it uses, the step being worked when it was last edited, and a review-risk verdict."""
    import graphlib
    commits = _git(p.root, "log", "--format=%H", "-F", f"--grep=({b.id}").split() if b and b.status == "done" else []
    if len(commits) == 1:  # finished and committed: its own commit, not everything since
        numstat = _git(p.root, "show", "--numstat", "--no-renames", "--format=", commits[0])
        diff = _git(p.root, "show", "--no-renames", "--format=", commits[0])
    else:
        base = _git(p.root, "merge-base", since, "HEAD").strip() if since else c.task_base(p.root, b)
        numstat = (c.task_diff(p.root, base, "--numstat", "--no-renames") or "") if base else ""
        diff = (c.task_diff(p.root, base, "--no-renames") or "") if base else ""
    sizes = {}
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            sizes[parts[2]] = (int(parts[0]) if parts[0].isdigit() else 0, int(parts[1]) if parts[1].isdigit() else 0)
    files = sorted(sizes)
    texts = {}
    for f in files:  # ponytail: today's text of each file; read it at the commit if old tours start to mislead
        try:
            with open(os.path.join(p.root, f), encoding="utf-8", errors="replace") as fh:
                texts[f] = fh.read(200_000)
        except OSError:
            texts[f] = ""  # deleted
    usable = [g for g in files if _CODE.search(g) and not c.TESTISH.search(g) and len(_name_stem(g)) >= 4
              and _name_stem(g).lower() not in _COMMON_STEMS]  # a test uses code; "index" or "ui" names everything
    uses = {f: sorted(g for g in usable if g != f and re.search(rf"\b{re.escape(_name_stem(g))}\b", texts[f]))
            if _CODE.search(f) else [] for f in files}
    try:
        order = list(graphlib.TopologicalSorter(uses).static_order())
    except graphlib.CycleError:  # a cycle: the most-used first
        order = sorted(files, key=lambda f: (-sum(f in u for u in uses.values()), f))
    order = [f for f in order if _CODE.search(f)] + [f for f in order if not _CODE.search(f)]  # docs and data last
    first, touched = (sorted(b.first_evidence().items()), c.task_touches(p, b.id)) if b else ([], {})
    blocks = {(re.search(r"^diff --git a/.+? b/(.+)$", x, re.M) or [None, None])[1]: x
              for x in re.split(r"(?m)^(?=diff --git )", diff)}
    m, rows = load(p), []
    for f in order:
        at = touched.get(f)
        step = next((n for n, ts in first if at and ts >= at), None) if at else None
        add, rm = sizes.get(f, (0, 0))
        gone = not os.path.exists(os.path.join(p.root, f))
        risk, why, score = _risk(p, m, f, add, rm, gone, set(files), blocks.get(f, ""))
        rows.append({"path": f, "add": add, "del": rm, "uses": uses[f], "step": step, "deleted": gone, "risk": risk,
                     "why": why, "score": score})
    return rows


def _risk(p, m, f, add, rm, gone, changed, diff):
    """T-0885: (review | glance | skim, [reasons]) for one changed file, from signals Sampo already keeps."""
    if c.TESTISH.search(f):
        return ("review", [f"tests removed (−{rm})"], 3) if rm > 20 and add < rm / 2 else ("skim", ["test"], 0)
    if c.DOC.search(f):
        return "skim", ["docs"], 0
    if is_generated(os.path.join(p.root, f)):
        return "skim", ["generated"], 0
    why, score = [], 0
    hit = c.sensitive([f], diff)
    if hit:
        why.append(f"security-sensitive ({c.joinmore(hit, 3)})")
        score += 3
    if c._RUNS_ELSEWHERE.search(f) or c._MANIFEST.search(f):
        why.append("runs in CI, on install, as a hook or brings dependencies")
        score += 2
    if add + rm > 50:
        why.append(f"{add + rm} lines")
        score += 2 if add + rm > 200 else 1
    if _CODE.search(f) and not gone and not (set(tests_for(m, [f])) & changed) and not any(
            c.TESTISH.search(g) and _name_stem(g) == _name_stem(f) for g in changed):
        why.append("no test changed with it")
        score += 1
    if gone and _CODE.search(f):
        why.append("deleted code")
        score += 1
    if f in m.get("hot", []):
        why.append("a hot file (changed often)")
        score += 1
    return ("review" if score >= 3 else "glance" if score else "skim"), why or ["small, with its test"], score


def cmd_tour(args):
    from sampo import cli
    p = cli.resolve(args)
    if not args.id and not args.since:
        raise cli.UsageError("fm tour ID (a task's changes) or fm tour --since REF (everything since REF, e.g. main)")
    b = cli.need_brief(p, args.id) if args.id else None
    if args.since and not _git(p.root, "merge-base", args.since, "HEAD").strip():
        raise cli.UsageError(f"no common history with {args.since}")
    rows, name = tour(p, b, args.since if not b else None), b.id if b else f"since {args.since}"
    text = "\n".join(
        f"{i}. [{r['risk']}] {r['path']}  +{r['add']} −{r['del']}" + (" (deleted)" if r["deleted"] else "")
        + (f"  step {r['step']}" if r["step"] else "") + (f"  — {'; '.join(r['why'])}" if r["risk"] != "skim" else "")
        + (f"  — uses {', '.join(r['uses'])}" if r["uses"] else "")
        for i, r in enumerate(rows, 1))
    group = lambda k: [r["path"] for r in sorted(rows, key=lambda r: -r["score"]) if r["risk"] == k]  # noqa: E731
    first = group("review")
    head = (f"Review first: {', '.join(first[:8]) or 'nothing'}{f' and {len(first) - 8} more' if len(first) > 8 else ''}"
            f" · glance: {len(group('glance'))} · "
            f"safe to skim: {len(group('skim'))} (tests, docs, generated, small tested changes)")
    cli.out(args, {"id": b.id if b else None, "since": args.since, "files": rows},
              c.plain_lines(f"{name}: {head}\nReading order (a file before the files that use it):\n{text}") if rows
              else f"{name}: no changes{' since it was focused' if b else ''}.")


_DEF = c.lazy(r"^(\s*)(?:(?:export|default|pub(?:\([\w:]+\))?|async|static|public|private|protected|abstract|final|"
              r"override|inline|unsafe|extern)\s+)*(def|class|function|fn|func|struct|enum|trait|impl|interface|"
              r"module|type|object)\s+([\w.$:<>]+)")


def outline(path):
    """[(first line, last line, depth, "kind name")] for a source file (T-0067): exact for Python (ast), by
    definition keywords and indentation for other languages."""
    import ast
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.splitlines()
    heads = None  # T-0724: Markdown by its headings, a diff by its files
    if path.endswith((".md", ".markdown")):
        heads = [(i + 1, len(m.group(1)) - 1, m.group(2).strip()) for i, line in enumerate(lines)
                 if (m := re.match(r"(#{1,4})\s+(.+)", line))]
    elif path.endswith((".diff", ".patch")):
        heads = [(i + 1, 0, "file " + m.group(1)) for i, line in enumerate(lines)
                 if (m := re.match(r"diff --git a/\S+ b/(\S+)", line))]
    if heads is not None:
        return [(n, next((h[0] - 1 for h in heads[k + 1:] if h[1] <= d), len(lines)), d, name)
                for k, (n, d, name) in enumerate(heads)], len(lines)
    if path.endswith(".py"):
        try:
            out = []

            def walk(node, depth):
                for n in ast.iter_child_nodes(node):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        out.append((n.lineno, n.end_lineno, depth, ("class " if isinstance(n, ast.ClassDef) else
                                                                    "def ") + n.name))
                        walk(n, depth + 1)
            walk(ast.parse(text), 0)
            return out, len(lines)
        except (SyntaxError, ValueError):
            pass
    hits = [(i + 1, len(m.group(1).expandtabs()), f"{m.group(2)} {m.group(3)}")
            for i, line in enumerate(lines) if (m := _DEF.match(line))]
    indents = sorted({h[1] for h in hits})
    out = []
    for k, (n, ind, name) in enumerate(hits):
        end = next((h[0] - 1 for h in hits[k + 1:] if h[1] <= ind), len(lines))
        while end > n and not lines[end - 1].strip():
            end -= 1
        out.append((n, end, indents.index(ind), name))
    return out, len(lines)


def cmd_outline(args):
    from sampo import cli
    try:
        defs, total = outline(args.path)
    except OSError as e:
        raise cli.UsageError(f"can't read {args.path}: {e.strerror}")
    rows = [f"{a:>6}-{b:<6} {'  ' * d}{name}" for a, b, d, name in defs[:400]]
    cli.out(args, {"path": args.path, "lines": total, "defs": [dict(zip(("start", "end", "depth", "name"), x))
                                                                 for x in defs]},
              c.plain_lines(f"{args.path}: {total} lines, {len(defs)} definitions (Read a range with offset/limit)\n"
                            + "\n".join(rows) + ("\n  …" if len(defs) > 400 else "")))


_ADDED = c.lazy(r"(?m)^\+(?!\+\+)(.*)$")
_DEBUG = c.lazy(r"\b(breakpoint\(\)|pdb\.set_trace|ipdb|console\.log\(|debugger;|dbg!\(|var_dump\(|binding\.pry)")
_MARKER = c.lazy(r"\b(TODO|FIXME|XXX|HACK)\b")


_SKIP = c.lazy(r"@(unittest\.skip|pytest\.mark\.(skip|xfail))|\bpytest\.skip\(|\b(xit|xdescribe|xtest)\(|"
               r"\.(skip|only)\(|\bt\.Skip\(|#\[ignore\]")
_ASSERT = c.lazy(r"\bassert|\bexpect\(|\.should\b|\bt\.(Error|Fatal)")


def _tampering(diff):
    """R3: weakened tests in the diff — assertions or test files removed, skips/only added."""
    found, cur, removed = [], "", collections.Counter()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur = line.rsplit(" b/", 1)[-1]
        elif line.startswith("deleted file mode") and _TEST.search(cur):
            found.append(f"test file deleted: {cur}")
        elif _TEST.search(cur) and line.startswith("-") and not line.startswith("---") and _ASSERT.search(line):
            removed[cur] += 1
        elif _TEST.search(cur) and line.startswith("+") and not line.startswith("+++") and _SKIP.search(line):
            found.append(f"test skipped or narrowed in {cur}: {c.fit(line[1:].strip(), 80)}")
    added = collections.Counter()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur = line.rsplit(" b/", 1)[-1]
        elif line.startswith("+") and not line.startswith("+++") and _ASSERT.search(line):
            added[cur] += 1
    found += [f"{n} assertion(s) removed from {f} ({added[f]} added)" for f, n in removed.items() if n > added[f]]
    return found


_REMOVED_DEF = c.lazy(r"^-(?:export\s+)?(?:async\s+)?(?:def|class|function|func|fn)\s+([A-Za-z]\w{2,})")


def _dangling(root, diff):
    """R3: public names the diff removed that the tree still mentions (a rename or delete left callers behind)."""
    removed = {m.group(1) for line in diff.splitlines() if (m := _REMOVED_DEF.match(line))}
    readded = {m.group(1) for line in diff.splitlines() if line.startswith("+")
               and (m := _REMOVED_DEF.match("-" + line[1:]))}
    found = []
    for name in sorted(removed - readded)[:10]:
        users = _git(root, "grep", "--untracked", "-l", "-w", "-I", "-e", name).splitlines()
        if users:
            found.append(f"removed {name} is still named in {c.joinmore(users, 4)}")
    return found


_GENERATED_PATH = c.lazy(r"(^|/)(vendor|node_modules|third_party|dist|__generated__|generated)/|\.min\.(js|css)$|"
                         r"_pb2(_grpc)?\.py$|\.pb\.go$|\.generated\.\w+$|(^|/)(package-lock\.json|yarn\.lock)$")
_GENERATED_HEAD = c.lazy(r"DO NOT EDIT|@generated|auto-?generated|generated by", re.I)


def is_generated(path):
    """R4: a file a tool writes (vendored, built, generated): hand edits get overwritten or drift from the source."""
    if _GENERATED_PATH.search(path.replace(os.sep, "/")):
        return True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = "".join(next(f, "") for _ in range(5))
    except OSError:
        return False
    return bool(_GENERATED_HEAD.search(head))


def pre_audit(root, diff, files, m=None):
    """Mechanical findings from a task's diff (T-0068), so the reviewer spends its reading on judgement: debug
    leftovers, conflict markers, new TODOs, secret-looking values, changed source with no test changed, big files,
    security-sensitive code."""
    found = []
    cur = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        if _DEBUG.search(added):
            found.append(f"debug leftover in {cur}: {c.fit(added.strip(), 100)}")
        if re.match(r"^(<<<<<<<|>>>>>>>)( |$)|^=======$", added):
            found.append(f"conflict marker in {cur}")
        if _MARKER.search(added):
            found.append(f"new {_MARKER.search(added).group(1)} in {cur}: {c.fit(added.strip(), 100)}")
        if c.redact(added) != added:
            found.append(f"secret-looking value added in {cur}")
    code = [f for f in files if _CODE.search(f) and not _TEST.search(f)]
    if code and not any(_TEST.search(f) for f in files):
        found.append(f"source changed with no test changed: {c.joinmore(code, 6)}")
    for f in files:
        try:
            if os.path.getsize(os.path.join(root, f)) > 200_000:
                found.append(f"large file in the change: {f} ({os.path.getsize(os.path.join(root, f)) // 1024} KB)")
        except OSError:
            pass
    found += _tampering(diff) + _dangling(root, diff)
    if m and m.get("pairs"):  # R2: files that nearly always change together, one of them left out
        changed = set(files)
        for f in sorted(changed):
            missing = [b for b in m["pairs"].get(f, []) if b not in changed]
            if missing:
                found.append(f"{f} usually changes with {', '.join(missing)} (git history); check it needs no edit")
    found += [f"hotspot: {f} has {m['fixy'][f]} fix/revert commits in 180 days (git history); read its edges twice"
              for f in files if f in (m or {}).get("fixy", ())][:5]
    found += [f"generated or vendored file edited: {f} (change its source or generator instead)" for f in files
              if is_generated(os.path.join(root, f))][:5]
    risky = c.sensitive(files, diff)
    if risky:
        found.append(f"security-sensitive: {', '.join(risky)} (adversary lens required)")
    return list(dict.fromkeys(found))[:30]


def changed(root, base):
    """Files changed since base (a commit, or a task's snapshot of the working files: T-0078), committed or not,
    untracked included, relative to root."""
    # T-2268: --no-renames, or a moved file lists only its new name and the old one's deletion is left uncommitted
    names = c.task_diff(root, base, "--name-only", "--no-renames")
    if names is not None:
        return sorted(set(names.splitlines()))
    return sorted(set(_git(root, "diff", "--name-only", "--no-renames", base).splitlines())
                  | set(_git(root, "ls-files", "--others", "--exclude-standard").splitlines()))


def cmd_why(args):
    """fm why FILE[:LINE] (R2): the commits behind a line (or a file's last five), the Sampo tasks they name, and
    each task's title, outcome and lesson — why the code is the way it is, without reading history by hand."""
    from sampo import cli
    p = cli.resolve(args)
    path, _, line = args.target.rpartition(":")
    if not line.isdigit():
        path, line = args.target, ""
    rel = os.path.relpath(os.path.abspath(path), p.root) if os.path.exists(path) else path
    if line:
        blame = _git(p.root, "blame", "-L", f"{line},{line}", "--porcelain", "--", rel)
        shas = [blame.split()[0]] if blame.strip() and not blame.startswith("0" * 40) else []
    else:
        shas = _git(p.root, "log", "-n", "5", "--format=%H", "--", rel).split()
    briefs = {b.id: b for b in c.load_briefs(p, include_archive=True)}
    rows, lines = [], []
    for sha in shas:
        msg = _git(p.root, "log", "-1", "--format=%h %as %s%n%b", sha).strip()
        if not msg:
            continue
        ids = list(dict.fromkeys(re.findall(r"\bT-\d{4,}\b", msg)))
        rows.append({"commit": msg.split()[0], "subject": msg.splitlines()[0], "tasks": ids})
        lines.append(c.fit(msg.splitlines()[0], 160))
        for tid in ids:
            b = briefs.get(tid)
            if b:
                lesson = next((x.lstrip("- ").strip() for x in b.section("Lessons").splitlines() if x.strip()), "")
                lines.append(c.fit(f"  {tid} [{b.type} {b.tier}, {b.status}] {b.title}"
                                   + (f" — lesson: {lesson}" if lesson else ""), 220))
                asked = " ".join(x.lstrip("> ").strip() for x in b.section("Raw request").splitlines() if x.strip())
                if asked and asked.lower().rstrip(".") != b.title.lower().rstrip("."):  # T-0889: the user's own words
                    lines.append(c.fit(f"    asked: \"{asked}\"", 240))
    cli.out(args, {"path": rel, "line": int(line) if line else None, "commits": rows},
              c.plain_lines("\n".join(lines)) if lines else f"No commits found for {rel}{':' + line if line else ''} "
                                                            f"(uncommitted, or not in git).")
