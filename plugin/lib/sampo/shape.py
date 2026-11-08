"""sampo shape FILE (T-3275, moon C-000723): the companions a new file is owed, learned from how earlier files of the
same shape were added (a test module, an fm alias, a MASTER.md line), and drafts of the missing ones. Stdlib and git
only. Nothing is written without --write, and --write only creates a stub that does not exist yet."""
import os
import re

from sampo import core as c

MIN_COUNT, MIN_SHARE = 3, 0.5  # owed: at least 3 earlier adds carried the companion, and at least half of them
MAX_FILES = 20  # a commit touching more says nothing about companions (map._co_change's bar)
MAX_COMMITS = 2000  # ponytail: the newest 2000 commits; a deeper history costs a longer git log, not a better answer
MAX_STUB = 65536  # a sibling bigger than this is not copied as a stub
MAX_TRIES = 5  # the newest adds asked for a shared file's last line before giving up
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _stem(rel):
    """The name companions are keyed by: a plain basename with an extension, 3+ characters, else None (a stem of '.py'
    or 'a' would match half the tree)."""
    base, ext = os.path.splitext(os.path.basename(rel))
    return base if ext and len(base) >= 3 and _NAME.match(base) else None


def _pattern(stem):
    """The stem as a whole word, with an optional fm prefix (fmfoo.py is foo's alias)."""
    return re.compile(rf"(?<![A-Za-z0-9])(fm)?{re.escape(stem)}(?![A-Za-z0-9])")


def _swap(text, old, new):
    return _pattern(old).sub(lambda m: (m.group(1) or "") + new, text)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def plain(text):
    """Text safe to print: a control character (an ESC sequence, a newline from history) becomes '?'."""
    return _CONTROL.sub("?", text)


def _inside(root, full):
    """True when full, through any symlink on its path, still lies inside root."""
    real_root, real = os.path.realpath(root), os.path.realpath(full)
    return real == real_root or real.startswith(real_root + os.sep)


def _commits(root):
    """[{sha, total, files}] newest first; files as (status, path, pure) for each added or modified file, pure when the
    add only gained lines (an add, or a modification that removed none). None when git can't read the history (unknown,
    never 'nothing owed')."""
    p = c._git(root, "log", "--no-renames", "--numstat", "--raw", "--format=%x00%H", f"--max-count={MAX_COMMITS}",
               timeout=30, proc=True)
    if p.returncode != 0:
        return None
    out = []
    for chunk in p.stdout.split("\0")[1:]:
        lines = chunk.splitlines()
        lost = {}  # path -> lines removed (None for binary); --numstat gives the counts, --raw the status
        for line in lines:
            parts = line.split("\t", 2)
            if len(parts) == 3 and not line.startswith(":"):
                lost[parts[2]] = int(parts[1]) if parts[1].isdigit() else None
        files, total = [], 0
        for line in lines:
            if line.startswith(":") and "\t" in line:
                meta, path = line[1:].split("\t", 1)
                total += 1
                st = meta.split()[-1]
                if st in ("A", "M"):
                    files.append((st, path, lost.get(path) == 0))
        out.append({"sha": chunk.split("\n", 1)[0].strip(), "total": total, "files": files})
    return out


def _rel(root, path):
    """A path as repo-relative POSIX: absolute, or relative to the cwd when it exists there or its folder does (a new
    file named from inside its folder), else to the repo. None when it lies outside the repo."""
    here = os.path.abspath(path)
    if os.path.isabs(path) or os.path.exists(here) or os.path.isdir(os.path.dirname(here)):
        full = here
    else:
        full = os.path.join(root, path)
    rel = os.path.relpath(os.path.realpath(full), os.path.realpath(root))
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, "/")


def _learn(root, rel):
    """What the history says rel is owed: [{template, kind, count, of, share, sibs}] most shared first, where kind is
    'stub' (a templated file, tests/test_<stem>.py) or 'line' (a shared file such as MASTER.md); sibs are (sha, path,
    old stem) of its newest adds. [] when no pattern; None when git can't read the history."""
    stem = _stem(rel)
    if stem is None:
        return []
    commits = _commits(root)
    if commits is None:
        return None
    folder, ext = os.path.dirname(rel), os.path.splitext(rel)[1]

    def sibling(path):  # same folder and extension: a sibling is a subject of its own, never a companion
        return os.path.dirname(path) == folder and os.path.splitext(path)[1] == ext

    of, seen = 0, {}
    for com in commits:
        if com["total"] > MAX_FILES:
            continue
        subject = next((path for st, path, _pure in com["files"]
                        if st == "A" and sibling(path) and path != rel and _stem(path)), None)
        if subject is None:
            continue
        of += 1
        old = _stem(subject)
        counted = set()
        for st, path, pure in com["files"]:
            if path == subject or sibling(path) or not pure:  # a companion counts when this add only appended to it
                continue
            pdir, base = os.path.split(path)
            if re.search(_pattern(old), base):
                tmpl = os.path.join(pdir, _swap(base, old, "<stem>"))
                kind = "stub"
            else:
                tmpl, kind = path, "line"
            entry = seen.setdefault(tmpl, {"count": 0, "kind": kind, "sibs": []})
            entry["sibs"].append((com["sha"], path, old))
            if tmpl not in counted:
                counted.add(tmpl)
                entry["count"] += 1
    out = []
    for tmpl, e in seen.items():
        if e["count"] < MIN_COUNT or e["count"] / of < MIN_SHARE:
            continue
        out.append({"template": tmpl, "kind": e["kind"], "count": e["count"], "of": of,
                    "share": round(e["count"] / of, 2), "sibs": e["sibs"]})
    out.sort(key=lambda o: (-o["share"], -o["count"], o["template"]))
    return out


def owed(root, rel):
    """The companions rel is owed, learned from the history: [{template, kind, count, of, share}]. [] when history shows
    no such pattern; None when git can't read the history."""
    found = _learn(root, rel)
    return None if found is None else [{k: v for k, v in o.items() if k != "sibs"} for o in found]


def _stub_draft(root, sibs, new):
    """The newest sibling still on disk with its stem swapped for new's, or None."""
    for _sha, path, old in sibs:
        full = os.path.join(root, path)
        if (os.path.isfile(full) and not os.path.islink(full) and _inside(root, full)
                and os.path.getsize(full) <= MAX_STUB):
            try:
                with open(full, encoding="utf-8") as f:
                    return _swap(f.read(), old, new), path
            except UnicodeDecodeError:  # a binary or latin-1 sibling would come out changed: not a stub to copy
                continue
    return None, None  # no sibling on disk: nothing to copy


def _line_draft(root, sibs, new):
    """The newest add's last added line that names its stem, with the stem swapped for new's; None when the last few
    adds show none (a line naming no stem would paste another file's entry, so the user adapts it by hand)."""
    for sha, path, old in sibs[:MAX_TRIES]:  # ponytail: a few git shows at most, not one per add in 2000 commits
        diff = c._git(root, "show", "--format=", "-U0", sha, "--", path, timeout=30)
        named = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
                 and _pattern(old).search(line[1:])]
        if named:
            return _swap(named[-1], old, new).rstrip()
    return None


def draft(root, rel, write=False):
    """What rel is owed, drafted: [{path, kind, count, of, state, text, from}]. state 'copy' is a missing stub that
    write would create, 'wrote' one it created, 'exists' a stub already there (left alone), 'outside' a path that links
    out of the repo (never read, never written), 'line' a shared file whose line is for the user to place, 'adapt' a
    shared file whose recent adds name no stem to copy from (the user writes the line), 'none' a stub with no sibling on
    disk to copy. Nothing is written unless write. None when git can't read the history."""
    found = _learn(root, rel)
    if found is None:
        return None
    stem = _stem(rel)
    out = []
    for o in found:
        path = o["template"].replace("<stem>", stem)
        full = os.path.join(root, path)
        item = {"path": path, "kind": o["kind"], "count": o["count"], "of": o["of"], "text": None, "from": None}
        if o["kind"] == "line":
            item["text"] = _line_draft(root, o["sibs"], stem)
            item["state"] = "line" if item["text"] else "adapt"
        elif not _inside(root, full):
            item["state"] = "outside"
        elif os.path.lexists(full):
            item["state"] = "exists"
        else:
            item["text"], item["from"] = _stub_draft(root, o["sibs"], stem)
            item["state"] = "none" if item["text"] is None else "copy"
            if write and item["state"] == "copy":
                os.makedirs(os.path.dirname(full), exist_ok=True)
                try:
                    with open(full, "x", encoding="utf-8") as f:  # "x": never over a file that appeared meanwhile
                        f.write(item["text"])
                    item["state"] = "wrote"
                except FileExistsError:
                    item["state"] = "exists"
        out.append(item)
    return out


def cmd_shape(args):
    """sampo shape FILE: the companions FILE is owed by how earlier files of its shape were added, and drafts of them;
    --write creates the missing stubs."""
    from sampo import cli
    p = cli.resolve(args)
    rel = _rel(p.root, args.file)
    if rel is None:
        raise cli.UsageError(f"{args.file} is outside the repo {p.root}")
    found = draft(p.root, rel, write=args.write)
    if found is None:
        cli.out(args, None, plain(f"{rel}: git can't read this repo's history, so what it is owed is unknown"))
        return
    lines = []
    for it in found:  # history text is printed plain: a control character from a commit never reaches the tty
        head = f"{it['path']}  {it['count']} of {it['of']} adds"
        how = {"copy": f"missing: a copy of {it['from']} with the stem swapped (--write creates it)",
               "wrote": f"wrote a stub copied from {it['from']}", "exists": "exists: left alone",
               "outside": "a folder on its path links out of the repo: not read, not written",
               "line": f"shared: add this line by hand: {it['text']}",
               "adapt": "shared: no recent add's line names this stem, so add one by hand",
               "none": "no sibling on disk to copy"}[it["state"]]
        lines.append(plain(f"{head}, {how}"))
    empty = f"{rel}: nothing owed (no companion repeats in its history)"
    cli.out(args, found, "\n".join(lines) if lines else plain(empty))
