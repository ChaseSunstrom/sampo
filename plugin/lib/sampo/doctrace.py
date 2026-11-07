"""sampo docs --trace: each doc claim (docs.claims) with the code lines that back it, and --since REF: which backing
changed since a git ref, so a claim may be stale (T-3287). Report-only. The verdicts are docs.claims' own; this adds
where each claim is backed and whether that backing moved."""
import os
import re
import subprocess

from sampo import docs

BACKING_MAX = 5  # backing entries a row keeps; stale and the target are judged on all of them
_WORD = re.compile(r"\w+")
_TEST = re.compile(r"(?:^|/)(?:tests?|__tests__)/|(?:^|/)test_[^/]*$|[._](?:test|spec)\.[^/]*$")  # ponytail: by name
_DEF = re.compile(r"\badd_(?:argument|option|parser)\(|\bdef\s|\bfn\s|\bfunction\s")  # ponytail: a definition by its line's call or keyword, not a parse
_BUILD = {"makefile", "gnumakefile", "package.json", "justfile"}  # the files a make or npm target is defined in


def _changed(root, ref):
    """Repo files that differ from ref, committed or not (git diff REF), and the ones git doesn't track yet. A ref git
    can't resolve is an error, never 'nothing changed'."""
    if not ref or ref.startswith("-"):  # an option, not a ref: git would read it as one
        raise ValueError(f"--since {ref!r}: not a git ref")
    try:
        d = subprocess.run(["git", "-C", root, *docs._SAFE, "diff", "--name-only", "-z", "--relative", ref, "--"],
                           capture_output=True, text=True, timeout=60)
        u = subprocess.run(["git", "-C", root, *docs._SAFE, "ls-files", "-z", "--others", "--exclude-standard"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"--since {ref}: git can't diff here ({e})") from e
    if d.returncode != 0:
        raise ValueError(f"--since {ref}: not a git ref ({d.stderr.strip()[:200] or 'git diff failed'})")
    return {os.path.normpath(f) for f in (d.stdout + u.stdout).split("\0") if f}


def _plan(cl):
    """(words, rank) of the code lines that back a claim: the \\w+ words a backing line holds, and rank(line, rel): 0 when
    the line doesn't back the claim, 1 for a mention, 2 for a definition (a line that adds the flag or subcommand, or
    defines the target). None: a path (its own file is the backing), a failed claim, or a kind with no definition."""
    kind, detail = cl["kind"], cl["detail"]
    if cl["verdict"] == "failed" or kind == "path":
        return None
    if kind == "flag":
        n = detail[2:]  # the words a literal "--x", a "x" string, x_y or a Python parameter would carry

        def rank(line, rel):
            if not docs._flag_in(detail, line, line if rel.endswith(".rs") else "", line if rel.endswith(".py") else ""):
                return 0
            return 2 if _DEF.search(line) else 1
        return {n.split("-")[0], n.replace("-", "_")}, rank
    if kind in ("subcommand", "command"):
        word = detail.split()[-1] if kind == "subcommand" else (docs.words(detail.split()[1:])[0] or [""])[0]
        if not word:
            return None  # a command with no subcommand of its own (a bare program name)

        def rank(line, rel):
            if f'"{word}"' not in line and f"'{word}'" not in line:
                return 0
            return 2 if _DEF.search(line) else 1
        return {word}, rank
    if kind == "target":  # make X: a Makefile line "X:"; npm run X: the "X" key of a package.json script
        name = detail.split()[-1]
        words = _WORD.findall(name)
        if not words:
            return None

        def rank(line, rel):
            if os.path.basename(rel).lower() not in _BUILD:
                return 0
            return 2 if (re.match(rf"\s*{re.escape(name)}\s*:", line) or f'"{name}"' in line) else 0
        return {words[0]}, rank
    return None


def _code_hits(root, words):
    """{word: [(file, line no, line)]}: the repo's code lines (not docs, not tests) that hold each word. One pass over the
    files docs._corpus reads, with its caps: a search cut short leaves a backing unlisted, never a wrong one."""
    hits = {w: [] for w in words}
    if not words:
        return hits
    files = size = 0
    for rel in docs._tree_files(root):
        path = os.path.join(root, rel)
        if rel.lower().endswith(docs.DOC_EXT) or _TEST.search(rel) or not docs.regular(path):
            continue
        files += 1
        if files > docs.CORPUS_FILES or size > docs.CORPUS_BYTES:
            break
        try:
            with open(path, "rb") as f:
                raw = f.read(docs.CORPUS_FILE)
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", "replace")
        size += len(text)
        for no, line in enumerate(text.split("\n"), 1):  # \n only: the line numbers an editor or grep shows
            for w in set(_WORD.findall(line)) & words:
                hits[w].append((rel, no, line))
    return hits


def _path_backing(root, cl):
    """A proven path is backed by its own file or folder (line None), found as docs.claims found it: beside the doc or
    from the root. Inside the repo only."""
    if cl["verdict"] != "proven":
        return []
    tok = cl["detail"].rstrip("/")
    for base in (os.path.dirname(os.path.join(root, cl["file"])), root):
        p = os.path.join(base, tok)
        if os.path.exists(p):
            rel = os.path.normpath(os.path.relpath(p, root))
            return [] if rel.startswith("..") else [(rel, None)]
    return []


def _doc_line(text, detail):
    """The first line of the doc that shows the claim (its words, whitespace-flexible; a flag not part of a longer one).
    A fenced command the doc wraps shows only its first word there. None when the text has neither."""
    for words in (detail.split(), detail.split()[:1]):
        if not words:
            continue
        m = re.search(r"(?<![\w./-])" + r"\s+".join(map(re.escape, words)) + r"(?![\w/-])", text)
        if m:
            return text.count("\n", 0, m.start()) + 1
    return None


def trace(root, since=None):
    """One row per claim docs.claims returns: {file, line (the doc's; None when the doc shows neither the claim nor its
    first word), kind, detail, verdict, why, backing: [{file, line, def, stale}] (definitions first, then mentions; at
    most BACKING_MAX), backing_total, target, stale}. def: the line defines the claim (a mention only backs it when no
    definition does). stale and target are judged on the definitions when there are any, so a mention-only change does
    not stale a defined flag. target: the first stale judged backing, else the first; None: UNBACKED. since: a git ref;
    a backing file in git's diff against it (or untracked) is stale. A failed claim has no backing; a path's backing is
    its file (line None)."""
    changed = _changed(root, since) if since is not None else None
    found, _ = docs.claims(root)
    plans = [_plan(cl) for cl in found]
    words = set().union(*(p[0] for p in plans if p))
    hits = _code_hits(root, words)
    texts, rows = {}, []
    for cl, plan in zip(found, plans):
        if cl["kind"] == "path":
            entries = [{"file": rel, "line": None, "def": True, "stale": _stale(root, rel, None, changed)}
                       for rel, _ in _path_backing(root, cl)]
        elif plan is None:
            entries = []
        else:
            terms, rank = plan
            best = {}  # (file, line) -> the highest rank a term gives it
            for w in terms:
                for rel, no, line in hits[w]:
                    r = rank(line, rel)
                    if r > best.get((rel, no), 0):
                        best[(rel, no)] = r
            entries = [{"file": rel, "line": no, "def": best[(rel, no)] == 2, "stale": _stale(root, rel, no, changed)}
                       for rel, no in sorted(best, key=lambda k: (-best[k], k))]  # definitions first, then file order
        judged = [e for e in entries if e["def"]] or entries
        if cl["file"] not in texts:
            texts[cl["file"]] = _doc_text(os.path.join(root, cl["file"]))
        rows.append({"file": cl["file"], "line": _doc_line(texts[cl["file"]], cl["detail"]), "kind": cl["kind"],
                     "detail": cl["detail"], "verdict": cl["verdict"], "why": cl["why"],
                     "backing": entries[:BACKING_MAX], "backing_total": len(entries),
                     "target": next((e for e in judged if e["stale"]), judged[0] if judged else None),
                     "stale": any(e["stale"] for e in judged)})
    return rows


def _stale(root, rel, line, changed):
    """A backing is stale when its file is in the diff; a folder when anything under it is."""
    if changed is None:
        return False
    if rel in changed:
        return True
    return line is None and os.path.isdir(os.path.join(root, rel)) and any(c.startswith(rel + "/") for c in changed)


def _doc_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(1_000_000)
    except OSError:
        return ""


def render(row):
    """doc:line kind detail -> file:line | file | UNBACKED, with STALE after a backing that changed."""
    t = row["target"]
    if t is None:
        tgt = "UNBACKED"
    else:
        tgt = t["file"] if t["line"] is None else f"{t['file']}:{t['line']}"
        tgt += " STALE" if t["stale"] else ""
    where = row["file"] if row["line"] is None else f"{row['file']}:{row['line']}"
    return f"{where} {row['kind']} {row['detail']} -> {tgt}"


def cmd_trace(args):
    """The docs verb with --trace or --since (docs.cmd_docs hands these over): one row per claim, text or --json. --strict
    exits 1 when a claim has no backing; the rows print first."""
    from sampo import cli as scli
    from sampo import core as c
    if not getattr(args, "trace", False):
        raise scli.UsageError("--since needs --trace")
    start = os.path.abspath(args.path or os.getcwd())
    root = c.git_root(start) or start
    try:
        rows = trace(root, since=args.since)
    except ValueError as e:
        raise scli.UsageError(str(e)) from e
    scli.out(args, {"root": root, "since": args.since, "rows": rows},
             "\n".join(render(r) for r in rows) or f"No doc claims in {root}.")
    unbacked = sum(r["target"] is None for r in rows)
    if getattr(args, "strict", False) and unbacked:
        raise scli.UsageError(f"{unbacked} doc claim(s) with no backing")
