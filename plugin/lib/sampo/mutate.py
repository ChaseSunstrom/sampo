"""T-1066: mutation-test the guard. One mutant per rule site in fmguard.py (a statement that records a block: a
`cats.append("core")`, a `found.append(("pipe-shell", ...))`, a `return Block("plugin", ...)`), made by ast. Drop mode
replaces the statement with `pass`; weaken mode negates the innermost `if` test guarding it. Each mutant runs the
guard's own tests in a throwaway copy; one the tests still pass on is a rule no test catches."""
import ast
import os
import re
import shutil
import subprocess
import tempfile
import time

from sampo import core as c


def _cats(v, cats):
    """The categories an expression records: "cat", ("cat", ...), Block("cat", ...), a list of those, or a comprehension
    of them ('*' when its category is a variable: classify() hands them back)."""
    if isinstance(v, ast.Constant):
        return [v.value] if v.value in cats else []
    if isinstance(v, ast.Tuple):
        return _cats(v.elts[0], cats) if v.elts else []
    if isinstance(v, ast.List):
        return [x for e in v.elts for x in _cats(e, cats)]
    if isinstance(v, ast.ListComp):
        return _cats(v.elt, cats) or (["*"] if isinstance(v.elt, ast.Tuple) and v.elt.elts
                                      and isinstance(v.elt.elts[0], ast.Name) else [])
    if isinstance(v, ast.Call) and getattr(v.func, "id", "") == "Block" and v.args:
        return _cats(v.args[0], cats)
    return []


def _cat(node, cats):
    """The categories a statement records (sorted, comma-joined), or None: x.append(..) / x.extend(..), x += [..],
    return ..; each of the shapes _cats reads."""
    v = None
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) \
            and node.value.func.attr in ("append", "extend") and len(node.value.args) == 1:
        v = node.value.args[0]
    elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
        v = node.value
    elif isinstance(node, ast.Return):
        v = node.value
    found = _cats(v, cats) if v is not None else []
    return ",".join(sorted(set(found))) or None


def _drop(node):
    """What replaces a rule site: a return of a list gives [], any other return None, a plain statement pass."""
    if isinstance(node, ast.Return):
        listy = isinstance(node.value, (ast.List, ast.ListComp))
        return ast.copy_location(ast.Return(ast.List([], ast.Load()) if listy else ast.Constant(None)), node)
    return ast.copy_location(ast.Pass(), node)


def mutants(src, cats, weaken=False):
    """[{line, category, stmt, src}]: each rule site once, the mutated source ready to write."""
    tree = ast.parse(src)
    parents = {}
    for p in ast.walk(tree):
        for ch in ast.iter_child_nodes(p):
            parents[ch] = p
    sites = [(n, _cat(n, cats)) for n in ast.walk(tree) if isinstance(n, (ast.Expr, ast.Return, ast.AugAssign))]
    out = []
    for node, cat in sorted((s for s in sites if s[1]), key=lambda s: s[0].lineno):
        stmt = " ".join((ast.get_source_segment(src, node) or "").split())
        if weaken:
            p = parents.get(node)
            while p is not None and not (isinstance(p, ast.If) and any(node is x or _within(x, node) for x in p.body)):
                p = parents.get(p)
            if p is None:
                continue  # no guarding if: nothing to weaken
            old, p.test = p.test, ast.copy_location(ast.UnaryOp(ast.Not(), p.test), p.test)
            out.append({"line": node.lineno, "category": cat, "stmt": stmt, "src": ast.unparse(tree)})
            p.test = old
        else:
            body = getattr(parents[node], _field(parents[node], node))
            idx = body.index(node)
            body[idx] = _drop(node)
            out.append({"line": node.lineno, "category": cat, "stmt": stmt, "src": ast.unparse(tree)})
            body[idx] = node
    return out


FLIPS = [(" == ", " != "), (" != ", " == "), (" not in ", " in "), (" >= ", " < "), (" <= ", " > "),
         (" > ", " <= "), (" < ", " >= "), (" and ", " or "), (" or ", " and "), ("True", "False"), ("False", "True")]


def flips(src):
    """[{line, category: 'flip', stmt, src}]: one operator or constant flipped per mutant, inside the check functions
    (names starting check or _check), from T-0496's mutate_guard.py (T-1121)."""
    lines = src.split("\n")
    spans = [(n.lineno, n.end_lineno) for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.FunctionDef) and re.match(r"_?check", n.name)]
    seen, out = set(), []
    for a, b in spans:
        for i in range(a, b + 1):
            code = lines[i - 1].split("#", 1)[0]
            if code.strip().startswith(("def ", '"""')) or '"' in code and code.count('"') % 2:
                continue
            for old, new in FLIPS:
                if old in code and (i, old) not in seen:
                    seen.add((i, old))
                    mutated = lines[:i - 1] + [lines[i - 1].replace(old, new, 1)] + lines[i:]
                    out.append({"line": i, "category": "flip", "stmt": f"{old.strip()} → {new.strip()}: "
                                f"{' '.join(code.split())}", "src": "\n".join(mutated)})
    return sorted(out, key=lambda m: m["line"])


def _within(top, node):
    return any(n is node for n in ast.walk(top))


def _field(parent, node):
    return next(f for f, v in ast.iter_fields(parent) if isinstance(v, list) and any(x is node for x in v))


def sample(items, n):
    """n items spread evenly across the list."""
    if n <= 0 or len(items) <= n:
        return items
    return [items[i * len(items) // n] for i in range(n)]


def _sh(cmd, cwd, timeout):
    """(returncode or None on timeout, the last output lines). The tests run in their own process group and the whole
    group is killed afterwards, so a hung mutant leaves no orphan racing the next one."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return None, ""
    finally:
        c.kill_group(p)
        p.communicate()


def run(module, rel, tree_root, dest, cmd, cats, weaken=False, only=None, limit=40, timeout=300, log=None, flip=False,
        extra=()):
    """Mutate module (a path); copy tree_root to dest in a temp dir, write each mutant at rel there and run cmd (a list,
    cwd = the copy's root). Returns {total, tried, killed, survived: [site], not_mutated: [categories with no site]}.
    The real tree is never written; a mutant whose tests hang counts as killed.
    ponytail: one full run of the tests per mutant (the guard's suite is the cost, tens of seconds each): --max caps it;
    run only the tests that import the line if that is ever too slow."""
    with open(module, encoding="utf-8") as f:
        text = f.read()
    ms = flips(text) if flip else mutants(text, cats, weaken)
    have = {x for m in ms for x in m["category"].split(",")}
    not_mutated = [] if flip else [x for x in cats if x not in have]
    if only:
        ms = [m for m in ms if only in m["category"].split(",")]
    total, ms = len(ms), sample(ms, limit)
    killed, survived = run_each(ms, module, rel, tree_root, dest, cmd, text, timeout, log, extra)
    return {"total": total, "tried": len(ms), "killed": killed, "survived": survived, "not_mutated": not_mutated}


def run_each(ms, module, rel, tree_root, dest, cmd, text, timeout=300, log=None, extra=()):
    """(killed, survived) for the mutants ms of module (text is its source): tree_root copied to dest in a temp dir, the
    unmutated module written at rel there and cmd run once as the baseline (it must pass), then each mutant written and
    cmd run. T-3273: shared with modmutate.run. The real tree is never written."""
    killed, survived = 0, []
    with tempfile.TemporaryDirectory(prefix="fm-mutate-") as tmp:
        shutil.copytree(tree_root, os.path.join(tmp, dest), ignore=shutil.ignore_patterns("__pycache__"))
        for src, at in extra:  # T-1605: files outside the tree its tests import (site/bench_safety.py)
            os.makedirs(os.path.dirname(os.path.join(tmp, at)), exist_ok=True)
            shutil.copy2(src, os.path.join(tmp, at))
        target = os.path.join(tmp, rel)
        with open(target, "w", encoding="utf-8") as f:
            f.write(ast.unparse(ast.parse(text)))  # the mutants are unparsed too
        if ms:
            rc, out = _sh(cmd, tmp, timeout)
            if rc != 0:
                tail = "\n".join(out.splitlines()[-20:]) if rc is not None else f"timed out after {timeout} s"
                raise ValueError("the tests fail on the unmutated module in the copy, so every mutant would count as "
                                 f"killed:\n{tail}")
        for i, m in enumerate(ms, 1):
            with open(target, "w", encoding="utf-8") as f:
                f.write(m["src"])
            t0 = time.monotonic()
            rc, _ = _sh(cmd, tmp, timeout)
            if rc == 0:
                survived.append({k: m[k] for k in ("line", "category", "stmt")})
            else:
                killed += 1
            if log:
                log(f"mutant {i}/{len(ms)} {os.path.basename(module)}:{m['line']} {m['category']}: "
                    f"{'survived' if rc == 0 else 'killed'} ({time.monotonic() - t0:.0f} s)")
    return killed, survived
