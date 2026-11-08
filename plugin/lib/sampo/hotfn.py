"""fm hotfn (T-3271, moon C-001076): Sampo's own functions ranked by churn x lines x (1 + FIX touches). Churn is how many
of the last 500 non-merge commits changed a function's lines; lines is what it owns; FIX touches are the fix commits
that changed it (a FIX word, or a done FIX brief's id in the subject), and a done FIX brief no commit names counts
its scope files' functions. The top rows
file as refactor (CLEAN) tasks, once per function. Approximation: a commit's changed lines map onto the functions as
they are at HEAD, so a line that moved since then can land on its neighbour."""
import ast
import os
import re
import subprocess
import sys

from sampo import core as c
from sampo.find import FIX, _git

HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
TID = re.compile(r"\bT-\d+\b")
MAX_BYTES = 1_000_000  # ponytail: a bigger file (generated) is skipped, not streamed; upgrade if one matters
LOG = ("log", "-500", "--no-merges", "--no-show-signature", "-M", "--relative", "-p", "-U0", "--no-ext-diff",
       "--no-textconv", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--format=%x01%H%x01%s")


def rank(rows):
    """Best first: churn x lines x (1 + fixes); no churn, no rank. Ties break by file, then function. Pure: the rows
    are not changed, and each ranked row is a copy with its score."""
    out = [{**r, "score": r["churn"] * r["lines"] * (1 + r["fixes"])} for r in rows if r["churn"] > 0]
    return sorted(out, key=lambda r: (-r["score"], r["file"], r["func"]))


def _owners(tree):
    """{line: qualname} for the innermost def owning each line: a method is K.m, a class only prefixes its names."""
    spans = []

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + ch.name
                if not isinstance(ch, ast.ClassDef):
                    first = min([ch.lineno, *(d.lineno for d in ch.decorator_list)])
                    spans.append((first, ch.end_lineno, name))
                walk(ch, name + ".")
            else:
                walk(ch, prefix)

    walk(tree, "")
    owner = {}
    for first, last, name in sorted(spans, key=lambda s: s[1] - s[0], reverse=True):  # outer first: inner overwrite
        for line in range(first, last + 1):
            owner[line] = name
    return owner


def _parse(text):
    """[(sha, subject, {file: set of new-side lines})], newest first, from `git log -p -U0`. A hunk's body is read by
    its counts, so a removed line that looks like a '--- ' header is never taken for one. A rename maps the old name
    onto the new one for every older commit."""
    commits, now, lines, i = [], {}, text.split("\n"), 0
    cur, fname, ren_from = None, None, None
    while i < len(lines):
        ln = lines[i]
        i += 1
        if ln.startswith("\x01"):
            parts = ln.split("\x01", 2) + ["", ""]
            cur = (parts[1], parts[2], {})
            commits.append(cur)
            fname = None
        elif cur is None:
            continue
        elif ln.startswith("diff --git "):
            fname = None
        elif ln.startswith("rename from "):
            ren_from = ln[12:]
        elif ln.startswith("rename to "):
            now[ren_from] = now.get(ln[10:], ln[10:])
        elif ln.startswith("+++ b/"):
            fname = now.get(ln[6:], ln[6:])
        elif ln.startswith("+++ "):  # /dev/null: a deletion, no new-side lines
            fname = None
        elif ln.startswith("@@"):
            m = HUNK.match(ln)
            if not m:
                continue
            old_n, new_at, new_n = int(m.group(1) or 1), int(m.group(2)), int(m.group(3) or 1)
            k = old_n + new_n
            while k > 0 and i < len(lines):  # the body is exactly its counts; "\ No newline" is not a line
                if not lines[i].startswith("\\"):
                    k -= 1
                i += 1
            if fname is not None:
                span = range(new_at, new_at + new_n) if new_n else ([new_at] if new_at else [])
                cur[2].setdefault(fname, set()).update(span)
    return commits


def collect(root, p=None):
    """[{file, func, lines, churn, fixes}] for every function in a .py file tracked under root, as it is now, or None
    when root isn't a git checkout (or git timed out). p: the Sampo project, for its done FIX briefs."""
    # ponytail: the whole 500-commit patch log is held in memory (41 MB for Sampo); stream it per commit if a repo's log passes ~200 MB
    try:
        ls = _git(root, "ls-files", "-z", "--", "*.py", timeout=60)
        log = _git(root, *LOG, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if ls is None or log is None:
        return None
    owner, size = {}, {}
    for rel in (os.fsdecode(x) for x in ls.split(b"\0") if x):
        try:
            if os.path.getsize(os.path.join(root, rel)) > MAX_BYTES:
                continue
            with open(os.path.join(root, rel), "rb") as f:
                lm = _owners(ast.parse(f.read(), rel))
        except (OSError, SyntaxError, ValueError, RecursionError):
            continue
        if lm:
            owner[rel] = lm
            for name in lm.values():
                size[(rel, name)] = size.get((rel, name), 0) + 1
    briefs = c.load_briefs(p, include_archive=True) if p else []
    done_fix = {b.id: b for b in briefs if b.type == "FIX" and b.status == "done"}
    churn, fixes, named = {}, {}, set()
    for _sha, subject, files in _parse(log.decode("utf-8", "replace")):
        ids = set(TID.findall(subject)) & set(done_fix)  # a commit names its task: "... (T-3271)"
        named |= ids
        fix = bool(FIX.search(subject)) or bool(ids)
        for file, nums in files.items():
            lm = owner.get(file, {})
            for fn in {lm[n] for n in nums if n in lm}:
                churn[(file, fn)] = churn.get((file, fn), 0) + 1
                if fix:
                    fixes[(file, fn)] = fixes.get((file, fn), 0) + 1
    for bid, b in done_fix.items():  # no commit in the window names it: each function in its scope files, once
        if bid not in named:
            for file in map(str, b.meta.get("scope") or []):
                for fn in set(owner.get(file, {}).values()):
                    fixes[(file, fn)] = fixes.get((file, fn), 0) + 1
    return [{"file": f, "func": fn, "lines": n, "churn": churn.get((f, fn), 0), "fixes": fixes.get((f, fn), 0)}
            for (f, fn), n in size.items()]


def capture(p, ranked, k):
    """Up to k refactor (CLEAN) tasks for the best functions not yet filed. Each function's key (file::func) stays on
    its brief (meta hotfn), archived or not: churn is history, so a second run would file it again otherwise."""
    from sampo import cli
    made = []
    with c.lock(p.dir):
        seen = {b.meta.get("hotfn") for b in c.load_briefs(p, include_archive=True)}
        for r in ranked:
            key = f"{r['file']}::{r['func']}"
            if len(made) >= k:
                break
            if key in seen:
                continue
            what = c.plain(f"refactor {r['func']} in {r['file']}: churn {r['churn']}, fixes {r['fixes']}, "
                           f"{r['lines']} lines")
            b = cli._create(p, c.fit(c.defang(what), 120), "CLEAN", "M", "captured", source="discovered",
                            scope=(r["file"],), raw=c.defang(c.plain(
                                f"Ranked by fm hotfn (score {r['score']}: churn x lines x (1 + fixes)): a refactor "
                                f"candidate, not a bug (data).\n> {key}")))
            b.meta["hotfn"] = key
            c.save_brief(p, b, touch=False)
            seen.add(key)
            made.append(key)
    return made


def _table(top, made):
    if not top:
        return "No function with churn in this checkout's last 500 commits." + "".join(f"\ncaptured {m}" for m in made)
    text = [f"{'score':>8} {'churn':>5} {'fixes':>5} {'lines':>5}  function"]
    text += [f"{r['score']:>8} {r['churn']:>5} {r['fixes']:>5} {r['lines']:>5}  "
             f"{c.plain(r['file'] + '::' + r['func'])}" for r in top]
    return "\n".join(text + [f"captured {m}" for m in made])


def cmd_hotfn(args):
    from sampo import cli
    p = c.find_project(os.getcwd())
    root = os.path.abspath(args.path) if args.path else (os.path.realpath(p.root) if p else os.getcwd())
    if not os.path.isdir(root):
        print(f"sampo hotfn: not a directory: {args.path}", file=sys.stderr)
        return 2
    if args.capture and (not p or os.path.realpath(root) != os.path.realpath(p.root)):
        print("sampo hotfn: --capture files tasks for this project: run it inside a Sampo project, on its own checkout",
              file=sys.stderr)
        return 2
    if args.capture < 0:
        print("sampo hotfn: --capture K: K is how many tasks to file", file=sys.stderr)
        return 2
    rows = collect(root, p)
    if rows is None:
        print(f"sampo hotfn: git failed or timed out under {root} (not a checkout?): nothing to rank", file=sys.stderr)
        return 2
    ranked = rank(rows)
    top = ranked[:max(args.top, 0)]
    made = capture(p, ranked, args.capture) if args.capture else []
    cli.out(args, {"root": root, "functions": top, "captured": made}, _table(top, made))
    return 0
