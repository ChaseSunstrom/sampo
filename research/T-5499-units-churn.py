"""T-5499: how often a commit moves a module's '' key in the test cache (units(), plugin/tests/run.py), which reruns
every test class that ran the module, measured on the last N commits that touched plugin/lib/sampo (read-only).

Usage: python3 -I research/T-5499-units-churn.py REPO [N] [OLD_REV]
  OLD_REV (default HEAD~1) is the commit whose run.py is the "before"; the working tree's run.py is the "after".
Prints: before, after, the rule with no exclusion at all (every undecorated module-level def its own unit, methods
kept in the body), and "after" with each exclusion left out in turn (what each one costs), plus units() seconds on
the biggest changed file.
"""
import importlib.util
import re
import subprocess
import sys
import time

repo = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 300
old_rev = sys.argv[3] if len(sys.argv) > 3 else "HEAD~1"


def git(*a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def load(src, name):
    spec = importlib.util.spec_from_loader(name, loader=None)
    m = importlib.util.module_from_spec(spec)
    m.__file__ = repo + "/plugin/tests/run.py"
    sys.path.insert(0, repo + "/plugin/tests")
    exec(compile(src, m.__file__, "exec"), m.__dict__)
    return m


SRC = open(repo + "/plugin/tests/run.py").read()
ABLATE = {  # clause of _own()'s return -> what replaces it
    "no constant-default rule": ("and all(const(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d])", ""),
    "no annotation rule": ("and (lazy or all(isinstance(a, ast.Constant) for a in _notes(n)))", ""),
    "no bound-elsewhere rule": ("and binds.count(n.name) == 1", ""),
    "no used-at-import rule": ("and n.name not in loads", ""),
    "no builtin rule": ("and not hasattr(builtins, n.name)", ""),
    "no globals()/dir()/vars() rule": ("elif isinstance(n, ast.Name) and n.id in _BY_NAME and id(n) not in of_other:\n            return set()",
                                       "elif False:\n            pass"),
}
variants = {"before": load(git("show", f"{old_rev}:plugin/tests/run.py"), "run_old"), "after": load(SRC, "run_new")}
bare = SRC.replace("def _own(tree):", "def _own(tree):\n    return {id(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))"
                   " and not n.decorator_list}\n\n\ndef _own_full(tree):")
variants["no exclusion at all (methods still kept)"] = load(bare, "run_bare")
for what, (old, new) in ABLATE.items():
    assert old in SRC, what
    variants[f"after, {what}"] = load(SRC.replace(old, new), "run_" + re.sub(r"\W", "_", what))

blobs = {}
commits = git("rev-list", "--no-merges", f"-{n}", "HEAD", "--", "plugin/lib/sampo").split()
pairs = []
for sha in commits:
    for path in git("diff-tree", "--no-commit-id", "--name-only", "-r", sha, "--", "plugin/lib/sampo").split():
        if path.endswith(".py"):
            a, b = git("show", f"{sha}^:{path}"), git("show", f"{sha}:{path}")
            if a and b:
                pairs.append((a, b))
moved = {k: 0 for k in variants}
files = 0
for a, b in pairs:
    got = {k: (m.units(a), m.units(b)) for k, m in variants.items()}
    if not all(x and y for x, y in got.values()):
        continue
    files += 1
    for k, (x, y) in got.items():
        moved[k] += x[1] != y[1]
print(f"{len(commits)} commits, {files} changed module files; '' moved in:")
for k, v in moved.items():
    print(f"  {v:4d} ({100 * v / files:.0f}%)  {k}")
big = max(pairs, key=lambda p: len(p[1]))[1]
for k in ("before", "after"):
    t = time.perf_counter()
    for _ in range(5):
        variants[k].units(big)
    print(f"units() of the biggest changed file ({big.count(chr(10))} lines), {k}: {(time.perf_counter() - t) / 5 * 1000:.1f} ms")
