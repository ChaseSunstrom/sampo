"""fm edit and fm sub (T-1089): change many spots in one call instead of one Edit (and its Read) per spot.

fm edit takes a batch of exact replacements on stdin, as JSON ([{"file", "old", "new", "all"?}]) or blocks:

    @@ path/to/file.py [all]
    <<<
    old text
    ===
    new text
    >>>

and applies them all or none: every old text must be there (once, unless all), every file inside the active task's
scope, and every file one the guard would let an Edit write (Sampo core, state, settings stay the guard's call).
fm sub PATTERN REPL --glob G is a regex codemod: a per-file count first, --apply to write, --max to cap the files."""
import glob
import json
import os
import re
import tempfile

from sampo import core as c

BLOCK = re.compile(r"^(?:@@ (.+?)(?: (all))?\n)?<<<\n(.*?)\n===\n(?:(.*?)\n)?>>>$", re.S | re.M)  # T-1213: === then >>> deletes


def parse_spec(text):
    """[(file, old, new, all)] from JSON or blocks; ValueError on anything else."""
    t = text.strip()
    if t.startswith("["):
        items = json.loads(t)
        out = []
        for i, x in enumerate(items):
            if not isinstance(x, dict) or not all(isinstance(x.get(k), str) for k in ("file", "old", "new")):
                raise ValueError(f"entry {i + 1}: needs file, old and new as text")
            out.append((x["file"], x["old"], x["new"], bool(x.get("all"))))
        return out
    out, head = [], None
    for m in BLOCK.finditer(t):  # T-1191: blocks after an @@ header belong to it until the next header
        if m.group(1):
            head = (m.group(1).strip(), bool(m.group(2)))
        if head is None:
            raise ValueError("a <<< block comes before any @@ file header: name the file first")
        out.append((head[0], m.group(3), m.group(4) or "", head[1]))
    if not out:
        raise ValueError("no edits: give JSON ([{\"file\", \"old\", \"new\"}]) or @@ file / <<< old / === new / >>> blocks")
    opened = len(re.findall(r"(?m)^<<<$", t))
    if opened != len(out):  # review: a block with a typo was skipped and the rest applied
        raise ValueError(f"{opened} <<< block(s) but {len(out)} complete: every block needs <<<, === and >>> on lines "
                         "of their own (an old text with a line that is exactly === goes in the JSON form)")
    return out


def _task(p):
    b = c.active_brief(c.load_briefs(p), p.lane)
    if not b:
        raise c.PolicyError("no task in focus: fm edit and fm sub --apply change files for the active task only "
                            "(fm task new … --focus, or fm focus ID)")
    return b


def _check(p, b, paths):
    """Why each path can't be written, or []: outside the task's scope, or refused by the guard as an Edit would be."""
    import fmguard
    home = os.path.expanduser("~")
    meta = c.read_meta(p)
    ctx = fmguard.Ctx(cwd=p.root, project_root=fmguard.project_root_for(p.root, home), home=home,
                      foreman_home=c.foreman_home(), state_dir=c.state_dir(), state_fallbacks=c.state_fallbacks(),
                      allow=set(b.meta.get("allow") or []), task_id=b.id, standing=set(meta.get("standing") or {}),
                      trusted=bool(c.trusted()))
    scope = list(b.meta.get("scope") or [])
    why = []
    for path in paths:
        rel = os.path.relpath(path, p.root).replace(os.sep, "/")
        if rel.startswith("../"):
            why.append(f"{rel}: outside the project")
            continue
        if scope and not any(c.glob_match(rel, s) for s in scope):  # review: scope entries are globs
            why.append(f"{rel}: outside {b.id}'s scope ({c.joinmore(scope, 4)}); widen it with fm task set {b.id} "
                       f"scope=… or use the Edit tool")
            continue
        blk = fmguard.check("Write", {"file_path": path}, ctx)
        if blk:
            why.append(f"{rel}: {fmguard.message(blk, ctx)}")
            continue
        # review: the user's own fm rules on paths apply as they do to an Edit (the bash rule only sees the command)
        hits = [h for h in c.rule_hits(p, "path", path) if h[1].get("action") == "block" or h[2]]
        if hits:
            r = hits[0][1]
            why.append(f"{rel}: the user's rule {r.get('pattern')!r} blocks it" + (f" ({r['why']})" if r.get("why") else ""))
    return why


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:  # review: CRLF stays CRLF
        return f.read()


def _write(changed, before):
    """All or none: every file is staged beside itself first, then each replaces its file; a file that changed since it
    was read (another session's edit) stops the write before anything lands."""
    for path, old in before.items():
        if path in changed and _read(path) != old:
            raise OSError(f"{path} changed since it was read")
    staged = []
    try:
        for path, text in changed.items():
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".fmedit-")
            staged.append((tmp, path))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    except BaseException:
        for tmp, _ in staged:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
    for tmp, path in staged:
        os.replace(tmp, path)
        drop_bytecode(path)


def drop_bytecode(path):
    """T-4206: Python runs a .pyc whose recorded source mtime (whole seconds) and size still match, so a same-size
    edit inside the second the file was last written would run the old code (a fixed test read red, one run in three).
    The written file's cached bytecode goes with the write; Python compiles it again."""
    if not path.endswith(".py"):
        return
    import glob
    cache = os.path.join(glob.escape(os.path.dirname(path)), "__pycache__", glob.escape(os.path.basename(path)[:-3]))
    for pyc in glob.glob(cache + ".*.pyc"):
        try:
            os.unlink(pyc)
        except OSError:
            pass


def cmd_edit(args):
    import sys
    from sampo import cli
    p = cli.resolve(args)
    try:
        src = getattr(args, "file", None)
        if src:  # a builder writes the batch with its Write tool: its isolation refuses heredocs
            with open(src, encoding="utf-8") as f:
                spec = parse_spec(f.read())
        else:
            spec = parse_spec(sys.stdin.read())
    except OSError as e:
        raise cli.UsageError(f"fm edit: can't read {e.filename}: {e.strerror}")
    except (ValueError, json.JSONDecodeError) as e:
        raise cli.UsageError(f"fm edit: {e}")
    b = _task(p)
    paths = {os.path.realpath(os.path.join(p.root, f)) for f, *_ in spec}
    problems = _check(p, b, sorted(paths))
    texts, counts, originals = {}, {}, {}
    for f, old, new, every in spec:
        path = os.path.realpath(os.path.join(p.root, f))
        if path not in texts:
            try:
                texts[path] = originals[path] = _read(path)
            except (OSError, UnicodeDecodeError) as e:
                problems.append(f"{f}: can't read ({e.__class__.__name__})")
                texts[path] = None
        if texts[path] is None:
            continue
        if "\0" in texts[path]:  # T-1424: binary, as fm code refuses it too
            problems.append(f"{f}: has NUL bytes: fm edit rewrites text files only")
            texts[path] = None
            continue
        n = texts[path].count(old) if old else 0
        crlf = texts[path].count("\r\n") * 2 > texts[path].count("\n")
        if not n and old and crlf and "\r" not in old and "\n" in old:  # T-1424: LF text, as read, in a CRLF file
            old, new = old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
            n = texts[path].count(old)
        if not n:
            problems.append(f"{f}: old text not found: {c.fit(old.splitlines()[0] if old else '(empty)', 60)!r}")
        elif n > 1 and not every:
            problems.append(f"{f}: old text found {n} times; make it unique or mark the entry all")
        else:
            texts[path] = texts[path].replace(old, new)
            counts[path] = counts.get(path, 0) + n
    if problems:
        raise cli.UsageError("nothing written:\n" + "\n".join(f"  {x}" for x in problems))
    changed = {path: t for path, t in texts.items() if t is not None and t != originals[path]}
    lines = [f"{os.path.relpath(path, p.root)}: {counts.get(path, 0)} replacement{'s' * (counts.get(path, 0) != 1)}"
             for path in sorted(texts)]
    if not getattr(args, "dry_run", False):
        try:
            _write(changed, originals)
        except OSError as e:
            raise cli.UsageError(f"nothing written: {e}")
    cli.out(args, {"files": {os.path.relpath(k, p.root): counts.get(k, 0) for k in texts},
                     "written": not getattr(args, "dry_run", False)},
              "\n".join(lines) + ("\n(dry run: nothing written)" if getattr(args, "dry_run", False) else ""))


def cmd_sub(args):
    from sampo import cli
    p = cli.resolve(args)
    try:
        rx = re.compile(args.pattern, re.M)
    except re.error as e:
        raise cli.UsageError(f"fm sub: bad pattern: {e}")
    root = os.path.realpath(p.root)
    files = sorted({r for g in args.glob or ["**/*"] for f in glob.glob(os.path.join(p.root, g), recursive=True)
                    for r in [os.path.realpath(f)] if os.path.isfile(r) and r.startswith(root + os.sep)
                    and "/.git/" not in r[len(root):]})  # review: inside the project only, never .git
    hits, skipped, before = {}, 0, {}
    for path in files:
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        try:
            new, n = rx.subn(args.repl, text)
        except (re.error, IndexError) as e:
            raise cli.UsageError(f"fm sub: bad replacement: {e}")
        if n:
            hits[path] = (new, n)
            before[path] = text
    lines = [f"{os.path.relpath(k, p.root)}: {n}" for k, (_, n) in hits.items()]
    total = sum(n for _, n in hits.values())
    if args.apply:
        if len(hits) > args.max:
            raise cli.UsageError(f"{len(hits)} files match, more than --max {args.max}: narrow --glob or raise --max")
        b = _task(p)
        why = _check(p, b, list(hits))
        if why:
            raise cli.UsageError("nothing written:\n" + "\n".join(f"  {x}" for x in why))
        try:
            _write({k: new for k, (new, _) in hits.items()}, before)
        except OSError as e:
            raise cli.UsageError(f"nothing written: {e}")
    cli.out(args, {"files": {os.path.relpath(k, p.root): n for k, (_, n) in hits.items()}, "written": bool(args.apply)},
              (("\n".join(lines) + f"\n{total} match(es) in {len(hits)} file(s)" + ("" if args.apply else
               "; preview only: --apply writes them")) if hits else "No match.")
              + (f" ({skipped} file(s) not UTF-8 text, not searched)" if skipped else ""))
