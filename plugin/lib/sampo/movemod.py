"""sampo move OLD NEW: renames a Sampo module (plugin/lib/sampo/OLD.py, its fm shim and its test) and rewrites every
reference to it in code, tests and docs, then proves it by importing every module in a fresh interpreter (T-3274,
Moon C-010030). A dry run writes nothing; a failed proof rolls the move back. CHANGELOG.md is history: never rewritten.
--alias leaves a sys.modules shim at the old path (and the old fm name keeps working)."""
import ast
import io
import json
import keyword
import os
import re
import subprocess
import sys
import tokenize

from sampo import core as c

PKG = "plugin/lib/sampo"
TESTS = "plugin/tests"
HISTORY = ("CHANGELOG.md",)
PROOF_TIMEOUT = 120  # ponytail: one interpreter imports every module; a slow machine gets this long, then the move rolls back
PROBE = """import importlib, json, sys
sys.path.insert(0, sys.argv[1])
out = {'imported': 0, 'failed': {}}
for m in sys.argv[2:]:
    try:
        importlib.import_module(m)
        out['imported'] += 1
    except BaseException as e:
        out['failed'][m] = f'{type(e).__name__}: {e}'
print(json.dumps(out))
"""


class MoveError(Exception):
    """A refusal, or a move that was rolled back; the message says which."""


def _git(root, *args):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise MoveError(f"git {args[0]} timed out")
    if r.returncode:
        raise MoveError(f"git {args[0]} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def _tracked(root):
    """Files git knows or would add (ignored ones skipped); -z so odd names come through raw."""
    names = _git(root, "ls-files", "-z", "-co", "--exclude-standard").split("\0")
    return [f for f in names if f and not f.startswith((*(m + "/" for m in c.MIRRORS), ".git/"))
            and os.path.isfile(os.path.join(root, f))]


def _ident(s):
    """A plain module name: no keyword, no dunder (moving __init__.py would unmake the package)."""
    return s.isidentifier() and not keyword.iskeyword(s) and not s.startswith("__")


def _lines(text):
    """The text split into lines that keep their own newline (splitlines would also split on form feeds)."""
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _name_hits(text, name):
    """(row, col) of each NAME token `name` that isn't an attribute (no dot before it); strings and comments skipped."""
    hits, prev = [], None
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if (t.type == tokenize.NAME and t.string == name
                    and not (prev and prev.type == tokenize.OP and prev.string == ".")):
                hits.append(t.start)
            if t.type not in (tokenize.NL, tokenize.COMMENT):
                prev = t
    except (tokenize.TokenError, SyntaxError):
        return []
    return hits


def _from_import(st, name):
    """Indexes of the names that a `from NAME import ...` statement binds (not after `as`, not dotted); none for another module."""
    if not st or st[0].type != tokenize.NAME or st[0].string != "from":
        return set()
    cut = next((i for i, t in enumerate(st) if t.type == tokenize.NAME and t.string == "import"), None)
    if cut is None or "".join(t.string for t in st[1:cut]) != name:
        return set()
    return {i for i in range(cut + 1, len(st)) if st[i].type == tokenize.NAME and st[i - 1].string not in (".", "as")}


def _module_uses(text, name, old):
    """(row, col) of each bare OLD that means the module NAME: the one `from NAME import OLD` binds, and each `OLD.attr`.
    Kwargs, parameters, locals and attributes named OLD keep their names. ponytail: a token rule, no scope analysis, so
    an OLD that shadows the module before an `OLD.attr` is renamed too; the import proof catches only import-time breaks."""
    try:
        toks = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)
                if t.type not in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)]
    except (tokenize.TokenError, SyntaxError):
        return []
    stmts, cur = [], []
    for t in toks:
        if t.type in (tokenize.NEWLINE, tokenize.ENDMARKER) or (t.type == tokenize.OP and t.string == ";"):
            stmts.append(cur)
            cur = []
        else:
            cur.append(t)
    stmts.append(cur)
    hits = set()
    for st in stmts:
        binds = _from_import(st, name)
        for i, t in enumerate(st):
            if t.type != tokenize.NAME or t.string != old or (i and st[i - 1].string == "."):
                continue
            if i in binds or (i + 1 < len(st) and st[i + 1].string == "."):
                hits.add(t.start)
    return sorted(hits)


def _rename_bare(text, name, old, new):
    lines = _lines(text)
    for r, col in sorted(_module_uses(text, name, old), reverse=True):
        ln = lines[r - 1]
        lines[r - 1] = ln[:col] + new + ln[col + len(old):]
    return "".join(lines)


def _imports_from(text, name, old):
    """True when the file does `from NAME import OLD` (with or without `as`), so OLD is a bare name in it."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(n, ast.ImportFrom) and n.level == 0 and n.module == name
               and any(a.name == old for a in n.names) for n in ast.walk(tree))


def _rules(name, old, new, pkgdir, shimdir):
    """(regex, replacement) for the dotted, path, fm-shim and test-name forms; lookarounds keep whole names whole."""
    return [
        (re.compile(r"(?<![\w.])" + re.escape(f"{name}.{old}") + r"(?!\w)"), f"{name}.{new}"),
        (re.compile(r"(?<![\w./-])" + re.escape(f"{pkgdir}/{old}.py") + r"(?!\w)"), f"{pkgdir}/{new}.py"),
        (re.compile(r"(?<![\w./-])" + re.escape(f"{shimdir}/fm{old}.py") + r"(?!\w)"), f"{shimdir}/fm{new}.py"),
        (re.compile(r"(?<![\w.-])" + re.escape(f"test_{old}") + r"(?!\w)"), f"test_{new}"),
        (re.compile(r"(?<![\w.-])fm" + re.escape(old) + r"(?!\w)"), f"fm{new}"),
    ]


def _rewrite(text, rules, bare, name, old, new):
    if bare:
        text = _rename_bare(text, name, old, new)
    for rx, rep in rules:
        text = rx.sub(lambda m, rep=rep: rep, text)
    return text


def _shim(name, new):
    """The alias module: importing the old name gives the new module (the shape of plugin/lib/fmcli.py)."""
    target = f"{name}.{new}"
    return (f'"""Old import path, kept as an alias by sampo move (T-3274): the module is {target}."""\n'
            f"import sys\nimport {target}\nsys.modules[__name__] = sys.modules[\"{target}\"]\n")


def _plan(root, old, new, pkg, alias):
    pkgdir = pkg.rstrip("/")
    name, shimdir = os.path.basename(pkgdir), os.path.dirname(pkgdir)
    mod, shim_old, shim_new = f"{pkgdir}/{old}.py", f"{shimdir}/fm{old}.py", f"{shimdir}/fm{new}.py"
    test_old, test_new = f"{TESTS}/test_{old}.py", f"{TESTS}/test_{new}.py"
    blockers = []
    if not _ident(new):
        blockers.append(f"{new!r} is not a module name")
    if old == new:
        blockers.append("OLD and NEW are the same name")
    if not _ident(old) or not os.path.isfile(os.path.join(root, mod)):
        blockers.append(f"{old} is not a module: {mod} does not exist")
    for f in (f"{pkgdir}/{new}.py", shim_new, test_new):
        if os.path.lexists(os.path.join(root, f)):
            blockers.append(f"{f} already exists")

    moves, created = [(mod, f"{pkgdir}/{new}.py")], {}
    if os.path.isfile(os.path.join(root, shim_old)):
        if alias:
            created[shim_new] = _shim(name, new)  # the old fm name stays as it is; the new one is the shim
        else:
            moves.append((shim_old, shim_new))
    if os.path.isfile(os.path.join(root, test_old)):
        moves.append((test_old, test_new))
    if alias:
        created[mod] = _shim(name, new)  # written after the git mv, at the old path
    skip = {shim_old} if alias else set()
    moved, rules = dict(moves), _rules(name, old, new, pkgdir, shimdir)

    changed = {}
    for f in _tracked(root):
        if f in HISTORY or f in skip:
            continue
        try:
            with open(os.path.join(root, f), "rb") as fh:
                text = fh.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if old not in text:
            continue
        bare = f.endswith(".py") and _imports_from(text, name, old)
        if bare and _name_hits(text, new):
            blockers.append(f"{f} imports {old} bare and already has a name {new}: rename that first")
        out = _rewrite(text, rules, bare, name, old, new)
        if out != text:
            changed[f] = (text, out)

    sites = []
    for f, (before, after) in changed.items():
        for i, (a, b) in enumerate(zip(_lines(before), _lines(after)), 1):
            if a != b:
                sites.append({"file": f, "line": i, "was": a.strip()[:120], "now": b.strip()[:120]})
    touched = {moved.get(f, f) for f in changed} | {n for _, n in moves}
    tests = sorted(os.path.basename(f)[:-3] for f in touched
                   if os.path.dirname(f) == TESTS and os.path.basename(f).startswith("test_"))
    rep = {"old": old, "new": new, "pkg": pkgdir, "alias": alias, "applied": False, "blockers": blockers,
           "moves": [list(m) for m in moves], "created": sorted(created), "files": sorted(changed),
           "sites": sites, "tests": tests}
    return rep, changed, moves, created


def _prove(root, pkgdir, tests):
    """Imports every module of the package in one fresh interpreter (-I -B: no cwd, no bytecode written)."""
    name = os.path.basename(pkgdir)
    mods = []
    for f in sorted(os.listdir(os.path.join(root, pkgdir))):
        stem = f[:-3] if f.endswith(".py") else ""
        if stem and stem.isidentifier():
            mods.append(name if stem == "__init__" else f"{name}.{stem}")
    try:
        r = subprocess.run([sys.executable, "-I", "-B", "-c", PROBE, os.path.join(root, os.path.dirname(pkgdir)), *mods],
                           capture_output=True, text=True, timeout=PROOF_TIMEOUT, cwd=root)
        lines = r.stdout.strip().splitlines()
        res = (json.loads(lines[-1]) if lines
               else {"imported": 0, "failed": {"proof": r.stderr.strip()[-300:] or "no output"}})
    except subprocess.TimeoutExpired:
        res = {"imported": 0, "failed": {"proof": f"timed out after {PROOF_TIMEOUT} s"}}
    except ValueError:
        res = {"imported": 0, "failed": {"proof": "unreadable output"}}
    res["run"] = " ".join(["python3 plugin/tests/run.py", *tests])
    return res


def _rollback(root, done, made, originals):
    """Undoes what apply did, in reverse: created files, then the git mvs, then the original bytes. Returns errors."""
    errs = []
    for p in made:
        try:
            if os.path.lexists(os.path.join(root, p)):
                os.remove(os.path.join(root, p))
        except OSError as e:
            errs.append(str(e))
    for a, b in reversed(done):
        try:
            _git(root, "mv", b, a)
        except MoveError as e:
            errs.append(str(e))
    for f, data in originals.items():
        try:
            c.write_atomic(os.path.join(root, f), data)
        except OSError as e:
            errs.append(str(e))
    return errs


def _apply(root, rep, changed, moves, created):
    base = _prove(root, rep["pkg"], [])  # before anything is written: which modules already fail, and can the probe run at all
    if "proof" in base["failed"]:
        raise MoveError(f"the import probe cannot run, so nothing was moved: {base['failed']['proof']}")
    moved, originals, done, made = dict(moves), {}, [], []
    try:
        for a, b in moves:
            _git(root, "mv", a, b)
            done.append((a, b))
        for f, (before, after) in changed.items():
            originals[f] = before.encode("utf-8")
            c.write_atomic(os.path.join(root, moved.get(f, f)), after)
        for path, text in created.items():
            c.write_atomic(os.path.join(root, path), text)
            made.append(path)
        proof = _prove(root, rep["pkg"], rep["tests"])
        new = {m: e for m, e in proof["failed"].items() if m == "proof" or m not in base["failed"]}
        if new:
            raise MoveError(f"the proof failed on {sorted(new.items())}")
        proof["was_failing"], proof["failed"] = sorted(proof["failed"]), {}  # failing before the move: not blamed on it
    except BaseException as e:  # Ctrl-C and SystemExit roll back too, then go on up
        errs = _rollback(root, done, made, originals)
        if not isinstance(e, Exception):
            if errs:
                sys.stderr.write(f"rollback incomplete: {'; '.join(errs)}\n")
            raise
        why = str(e) if isinstance(e, MoveError) else f"{type(e).__name__}: {e}"
        raise MoveError(f"{why}; the move is rolled back"
                        + (f" (rollback incomplete: {'; '.join(errs)})" if errs else ""))
    rep["applied"], rep["proof"] = True, proof
    return rep


def run(root, old, new, apply=False, alias=False, pkg=PKG):
    """The move's report (dry run unless apply). With apply, refusals raise MoveError before anything is written."""
    rep, changed, moves, created = _plan(root, old, new, pkg, alias)
    if not apply:
        return rep
    if rep["blockers"]:
        raise MoveError("refused: " + "; ".join(rep["blockers"]))
    return _apply(root, rep, changed, moves, created)


def cmd_move(args):
    from sampo import cli
    p = cli.resolve(args)
    try:
        rep = run(p.root, args.old, args.new, apply=args.apply, alias=args.alias)
    except MoveError as e:
        raise cli.UsageError(str(e))
    text = (f"{args.old} → {args.new}: {len(rep['moves'])} move(s), {len(rep['sites'])} site(s) in "
            f"{len(rep['files'])} file(s)" + ("" if args.apply else " (dry run: --apply moves and rewrites)"))
    if rep["blockers"]:
        text += "\n  blocked: " + "; ".join(rep["blockers"])
    if rep["applied"]:
        proof = rep["proof"]
        text += (f"\n  proof: {proof['imported']} module(s) imported in a fresh interpreter"
                 + (f"; already failing before the move, not blamed on it: {', '.join(proof['was_failing'])}"
                    if proof["was_failing"] else "")
                 + f"; run the affected tests: {proof['run']}")
    text += "".join(f"\n  {s['file']}:{s['line']}: {c.fit(s['now'], 120)}" for s in rep["sites"][:20])
    text += f"\n  … {len(rep['sites']) - 20} more" if len(rep["sites"]) > 20 else ""
    return cli.out(args, rep, text)
