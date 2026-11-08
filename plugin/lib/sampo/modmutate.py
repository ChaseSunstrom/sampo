"""T-3273: mutation-test any Sampo module, not only the guard. mutants() flips one comparison, boolean operator or int/bool
constant at a time, in every function of the module (or one, by name), made by ast. tests_for() finds the test files that
import the module; run() runs only those, at idle priority, against each mutant and lists the survivors: the flips no test
notices. mutate.py keeps the guard's rule sites and its check-function flips; this file takes any module."""
import ast
import os
import shutil
import sys

from sampo import mutate as smutate

CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
       ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
BOOL = {ast.And: ast.Or, ast.Or: ast.And}
OP_TEXT = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.In: "in",
           ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not", ast.And: "and", ast.Or: "or"}
RUN_PY = os.path.join("plugin", "tests", "run.py")  # from the copy's root, where the mutant tree sits


def _sites(tree, func):
    """(node, box, key, old, new) for each flip under the functions named func (all when None), in line order: a
    comparison's operator, a boolean operator, an int or bool constant. A string or a float is never flipped."""
    fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and (func is None or n.name == func)]
    nodes = {id(n): n for f in fns for n in ast.walk(f)}.values()
    out = []
    for n in nodes:
        if isinstance(n, ast.Compare):
            out += [(n, n.ops, i, op, CMP[type(op)]()) for i, op in enumerate(n.ops) if type(op) in CMP]
        elif isinstance(n, ast.BoolOp) and type(n.op) in BOOL:
            out.append((n, n, "op", n.op, BOOL[type(n.op)]()))
        elif isinstance(n, ast.Constant) and isinstance(n.value, int):
            v = n.value
            out.append((n, n, "value", v, (not v) if isinstance(v, bool) else v + 1))
    return sorted(out, key=lambda s: (s[0].lineno, s[0].col_offset))


def _put(box, key, val):
    if isinstance(box, list):
        box[key] = val
    else:
        setattr(box, key, val)


def _text(v):
    return OP_TEXT[type(v)] if type(v) in OP_TEXT else repr(v)


def mutants(src, func=None):
    """[{line, category: 'flip', stmt, src}]: one flip per mutant, the mutated source ready to write (unparsed, as the
    baseline copy is, so a mutant differs from the module only by its flip). func: only the functions with this name."""
    tree = ast.parse(src)
    lines = src.split("\n")
    out = []
    for node, box, key, old, new in _sites(tree, func):
        _put(box, key, new)
        mutated = ast.unparse(tree)
        _put(box, key, old)
        code = " ".join(lines[node.lineno - 1].split())
        out.append({"line": node.lineno, "category": "flip", "stmt": f"{_text(old)} → {_text(new)}: {code}",
                    "src": mutated})
    return out


def module_name(path, root):
    """The dotted name a test imports a module by: plugin/lib/sampo/replay.py under root gives sampo.replay."""
    rel = os.path.relpath(os.path.abspath(path), os.path.join(os.path.abspath(root), "lib"))
    if rel.split(os.sep)[0] == os.pardir or not rel.endswith(".py"):
        raise ValueError(f"{path}: not a module under {os.path.join(root, 'lib')}")
    return rel[:-3].replace(os.sep, ".")


def _imported(tree):
    """The dotted names a module imports: `import a.b`, `from a import b` (as a.b) and `from a.b import c`."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            out.add(n.module)
            out.update(f"{n.module}.{a.name}" for a in n.names)
    return out


def tests_for(module, root):
    """The test files (test_*.py, sorted) under root/tests that import module: `import sampo.replay`, `from sampo import
    replay` or `from sampo.replay import f`. A test that reaches the module only through another module, or a subprocess,
    isn't listed; pass --tests to add it. ponytail: a name scan, not a dependency graph."""
    name = module_name(module, root)
    tests = os.path.join(root, "tests")
    try:
        files = sorted(f for f in os.listdir(tests) if f.startswith("test_") and f.endswith(".py"))
    except OSError:
        files = []
    out = []
    for f in files:
        try:
            with open(os.path.join(tests, f), encoding="utf-8") as h:
                tree = ast.parse(h.read())
        except (OSError, ValueError, SyntaxError):  # ValueError: undecodable or NUL bytes; such a file imports nothing
            continue
        if name in _imported(tree):
            out.append(f)
    return out


def test_cmd(names):
    """The runner's command for these test modules (names without .py), from the copy's root."""
    return [sys.executable, RUN_PY, *names]


def niced(cmd):
    """cmd at idle priority (nice 19), so a mutant's test run never takes the cores from the work beside it."""
    nice = shutil.which("nice")
    return [nice, "-n", "19", *cmd] if nice else list(cmd)


def run(module, rel, tree_root, dest, cmd, func=None, limit=40, timeout=300, log=None, extra=()):
    """Each flip of module (a path; rel is where it sits in the copy) run against cmd, niced, in a copy of tree_root under
    dest. Returns {total, tried, killed, survived: [{line, category, stmt}]}. The real tree is never written; a hung
    mutant counts as killed; a failing baseline raises ValueError (see mutate.run_each).
    ponytail: one run of cmd per mutant; limit (--max) samples the mutants evenly, timeout kills a hung one."""
    with open(module, encoding="utf-8") as f:
        text = f.read()
    if func and not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func
                        for n in ast.walk(ast.parse(text))):
        raise ValueError(f"{module} has no function {func}: a typo would pass with no mutant run")
    ms = mutants(text, func)
    total, ms = len(ms), smutate.sample(ms, limit)
    killed, survived = smutate.run_each(ms, module, rel, tree_root, dest, niced(cmd), text, timeout, log, extra)
    return {"total": total, "tried": len(ms), "killed": killed, "survived": survived}
