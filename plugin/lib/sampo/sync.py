"""fm sync: an opt-in mirror of a project's Sampo state inside the repo (.sampo/, formerly .foreman/), committed with the code, so
another clone or machine can pick the work up.

Briefs (and their archive), decisions and research travel; the ledger, meta, sessions, pending asks, gates and views
stay local. Nothing from the mirror counts for this machine's gates: grants (`allow`, `approved`) are stripped both
ways, fm's `[ran]`/`[tree]` marks and audits recorded elsewhere are neutralized on import, and focus (active) is per
machine. fm writes the folder (the guard treats hand edits as state-direct); its markdown is left out of the worktree
id, so exports don't stale audits, while anything else put there still shows.

Git is the transport and the conflict detector. meta["sync_base"] keeps a hash of what fm last wrote to or took from
each mirror file: a file that differs from it changed in the repo (a pull or merge). Exports leave such a file alone;
the next fm command (or session start, or `fm sync import`) takes it in before working. If the local copy changed too,
the local one wins and theirs is kept under sync-conflicts/ for a manual merge; files with unresolved merge markers
wait until git's conflict is resolved. Callers hold the project lock.
"""
import filecmp
import hashlib
import json
import os
import re
import shutil
import sys

from sampo import core as c

DIR = c.MIRROR
OLD = c.MIRRORS[1]  # T-3342: the folder before the rename; read while DIR is absent, moved by move()
STAMPS = "sync-stamps.json"  # T-3349: per mirrored file, its source's and its mirror copy's (mtime_ns, size) last export
SCRUB = 2  # T-3508 (2: T-3547): the stamps' "v"; bump when _outgoing changes, so the next export rewrites every mirrored file
LOCAL_ONLY = ("allow", "approved", "plugin_pin")
LEASE_MARKS = ("lane", "lane_branch", "builder", "builder_proc", "cost_cap", "cap_hit")  # one machine's marks on a brief


def settle(b, new, why="it arrived from the repo's .sampo/ (fm sync), not from a request here"):
    """T-1749: a brief that came from elsewhere (fm sync import, fm home import), as this machine takes it: not active
    or leased here, and, when it is new here and open, waiting for the user's own yes (fm ask ID confirm): a repo's
    .sampo/ can hold tasks nobody here asked for."""
    if b.meta.get("status") in ("active", "verifying"):
        b.meta["status"] = "planned"  # focus is per machine
    for k in LEASE_MARKS:
        b.meta.pop(k, None)
    if new and b.meta.get("status") in c.OPEN:
        b.meta["confirm"], b.meta["confirm_why"] = True, why  # T-1750: the confirm gap says why it waits
    return b
BRIEFS = ("tasks/", "archive/")
README = """# .sampo

Sampo's task briefs, decisions and research for this repository, mirrored by `sampo sync` so another clone or machine
can pick the work up (`sampo sync import`, or automatically at session start). Sampo writes this folder; change the
work through sampo, not by editing these files. Approvals, run marks, audits and focus never count on another machine;
home folders read as ~ and private hosts as <host>.
"""
MAX_IMPORT = 1_000_000  # bytes; a mirrored note bigger than this is left in the repo
_ID = re.compile(r"^(T-\d{4,})")
_CONFLICT = re.compile(r"(?m)^(<{7}|={7}|>{7})( |$)")
_AUDIT_LINE = re.compile(r"(?m)^(-\s+\(audit\s+[a-z]+)\)")


def mirror(p):
    root = c.main_view(p).root  # T-0134: one mirror, the main checkout's, never a lane's copy
    new = os.path.join(root, DIR)
    if (not os.path.lexists(new) or not _ours(new)) and os.path.isdir(os.path.join(root, OLD)):
        return os.path.join(root, OLD)  # T-3342: not moved yet (or .sampo/ is the user's own, not fm's)
    return os.path.join(root, DIR)


def _busy(root):
    """A merge, rebase, cherry-pick or detached HEAD: not the moment to commit a rename."""
    gd = c._git(root, "rev-parse", "--absolute-git-dir").strip()
    return not gd or c._git(root, "symbolic-ref", "-q", "HEAD") == "" or any(
        os.path.exists(os.path.join(gd, n)) for n in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"))


def _ours(d):
    """A folder that looks like fm's mirror: a tasks/ folder, or a README whose first line is fm's heading."""
    try:
        if os.path.isdir(os.path.join(d, "tasks")):
            return True
        with open(os.path.join(d, "README.md"), encoding="utf-8") as f:
            return f.readline().strip() in ("# " + DIR, "# " + OLD)
    except (OSError, ValueError):
        return False


def _aside(p, rel, path):
    dest = os.path.join(p.dir, "sync-conflicts", OLD + "__" + rel.replace("/", "__"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(path, dest)


def _fold(p, old, new):
    """Fold the old folder's files into .sampo/ with no loss and no clock: a file .sampo lacks is copied (any kind of
    regular file, never a symlink); decisions.md/fixes.md keep every line of both; of two briefs the later `updated`
    wins; any other differing file keeps .sampo's and the old copy is set aside under sync-conflicts/."""
    rnew = os.path.realpath(new)
    for d, dirs, files in os.walk(old):
        dirs[:] = sorted(x for x in dirs if x != ".git" and not os.path.islink(os.path.join(d, x)))
        for f in sorted(files):
            a = os.path.join(d, f)
            rel = os.path.relpath(a, old).replace(os.sep, "/")
            b = os.path.join(new, rel)
            if os.path.islink(a) or not os.path.isfile(a) or rel == "README.md":
                continue
            if not os.path.realpath(b).startswith(rnew + os.sep):
                continue  # a pulled link inside .sampo/ pointing out: never written through
            if not os.path.lexists(b):
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copyfile(a, b)
                continue
            if os.path.islink(b) or not os.path.isfile(b) or filecmp.cmp(a, b, shallow=False):
                continue
            try:
                ta, tb = _read(a), _read(b)
            except (OSError, ValueError):
                _aside(p, rel, a)
                continue
            if rel in ("decisions.md", "fixes.md"):  # append-only: keep every line of both
                have = tb.splitlines()
                extra = [x for x in ta.splitlines() if x.strip() and x not in have]
                if extra:
                    _write(b, tb.rstrip("\n") + "\n" + "\n".join(extra) + "\n")
            elif rel.startswith(BRIEFS) and rel.endswith(".md") and \
                    str(c.Brief.parse(ta).meta.get("updated") or "") > str(c.Brief.parse(tb).meta.get("updated") or ""):
                _aside(p, rel, b)
                _write(b, ta)
            else:
                _aside(p, rel, a)


def move(p, retry=False):
    """T-3342: the mirror was .foreman/ before the product was renamed. Move it to .sampo/ once: git mv and one logged
    commit when tracked, a plain rename when ignored or outside git. Only from a CLI export (the caller holds the
    lock), never a hook. A failed commit is put back and noted in meta (sync status shows it); only `retry` (fm sync
    export) tries again. Returns the number of files moved, or None."""
    root = c.main_view(p).root
    old, new = os.path.join(root, OLD), os.path.join(root, DIR)
    if os.path.islink(old) or not os.path.isdir(old) or os.path.islink(new) or (os.path.isdir(new) and not _ours(new)):
        return None  # a .sampo/ that isn't fm's (the user's own) is never merged into
    # a lane (or any other checkout) never moves or commits in the main checkout: only from the cwd's own toplevel
    cwd = os.path.realpath(os.getcwd())
    if getattr(p, "lane", None) or os.path.realpath(root) != os.path.realpath(c.git_root(cwd) or cwd):
        return None
    meta = c.read_meta(p)
    if meta.get("sync_move_error") and not retry:
        return None
    git = bool(c.git_root(root)) and bool(c._git(root, "rev-parse", "--git-dir"))
    if git and _busy(root):
        return None
    tracked = git and c._git(root, "ls-files", "--", OLD, timeout=30).strip() != ""
    ignored = git and not tracked and c.mirror_ignored(root, OLD)
    n, both = len(list(_walk(old))), os.path.isdir(new)
    try:
        if both:  # old code (or a pull) brought the old folder back: fold it in; it is dropped after the commit
            _fold(p, old, new)
        elif tracked:
            r = c._git(root, "mv", OLD, DIR, proc=True, timeout=60)
            if r.returncode:
                raise OSError(r.stderr.strip() or "git mv failed")
        else:
            os.rename(old, new)
        readme = os.path.join(new, "README.md")
        retitled = os.path.isfile(readme) and _read(readme).startswith("# " + OLD + "\n")
        if retitled:
            _write(readme, "# " + DIR + _read(readme)[len("# " + OLD):])
        sha = None
        if tracked:
            if both:  # out of the way for the commit (it records the old files as gone); back if the commit fails
                aside = old + ".moving"
                if os.path.lexists(aside):
                    raise OSError(f"{OLD}.moving is in the way")
                os.rename(old, aside)
            c._git(root, "add", "--", DIR, timeout=60)
            r = c._git(root, "commit", "-q", "-m", f"Move fm sync's mirror {OLD}/ to {DIR}/", "--", OLD, DIR,
                       proc=True, timeout=120)
            if r.returncode:
                c._git(root, "reset", "-q", "--", OLD, DIR, timeout=60)  # never a checkout: uncommitted mirror edits stay
                if both:
                    os.rename(aside, old)
                else:
                    if retitled:
                        _write(readme, "# " + OLD + _read(readme)[len("# " + DIR):])
                    os.rename(new, old)
                raise OSError("the move commit failed: " + (r.stderr.strip() or r.stdout.strip())[:200])
            sha = c._git(root, "rev-parse", "--short", "HEAD").strip()
        if both:
            shutil.rmtree(aside if tracked else old)
        if not tracked:
            if ignored and not c.mirror_ignored(root, DIR) and not c._git(root, "ls-files", "--", DIR, timeout=30).strip():
                ex = c._git(root, "rev-parse", "--git-path", "info/exclude").strip()
                ex = ex if os.path.isabs(ex) else os.path.join(root, ex)
                os.makedirs(os.path.dirname(ex), exist_ok=True)
                with open(ex, "a", encoding="utf-8") as f:
                    f.write(f"/{DIR}/\n")
    except OSError as e:
        meta = c.read_meta(p)
        meta["sync_move_error"] = str(e)[:300]
        c.write_meta(p, meta)
        return None
    meta = c.read_meta(p)
    if meta.pop("sync_move_error", None) is not None:
        c.write_meta(p, meta)
    c.log_event(p, "sync_moved", data={"files": n, "sha": sha})
    return n


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c.write_atomic(path, text)


def _walk(base):
    """Regular markdown files under base, as relative paths; symlinks are never followed (a pulled one could point
    anywhere)."""
    if os.path.islink(base) or not os.path.isdir(base):
        return
    for d, dirs, files in os.walk(base):
        dirs[:] = sorted(x for x in dirs if not os.path.islink(os.path.join(d, x)))
        for f in sorted(files):
            path = os.path.join(d, f)
            if (f.endswith(".md") or f in LINE_FILES) and not os.path.islink(path) and os.path.isfile(path):  # no FIFO or device reads
                yield os.path.relpath(os.path.join(d, f), base).replace(os.sep, "/")


LINE_FILES = ("exam.jsonl", "acts.jsonl")  # T-3333: Kalevala's logs merge by line across machines
MAX_LINE_FILE = 50_000_000  # bytes; the logs only grow, so they get a far higher import bound than a note


def _limit(rel):
    return MAX_LINE_FILE if os.path.basename(rel) in LINE_FILES else MAX_IMPORT


def merge_lines(a, b):
    """The lines of a and b, each once (the same line twice is one), oldest first by its "at"/"ts" (a line without one
    sorts by its text): one order whichever side merges, so two machines converge and a repeat merge changes nothing."""
    def key(line):
        try:
            d = json.loads(line)
            t = d.get("at") or d.get("ts") or "" if isinstance(d, dict) else ""
        except ValueError:
            t = ""
        return (str(t), line)
    lines = sorted({l.rstrip() for l in (a + "\n" + b).splitlines() if l.strip()}, key=key)
    return "\n".join(lines) + "\n" if lines else ""


def _travels(rel):
    return rel.startswith(BRIEFS) or rel.startswith("research/") or rel in ("decisions.md", "fixes.md")  # T-1032


def _for_mirror(text):
    b = c.Brief.parse(text)
    for k in LOCAL_ONLY:
        b.meta.pop(k, None)
    return b.render()


def _outgoing(rel, text):
    """A local file as the mirror holds it: a brief without grants; no secret, home folder or private host (T-3508).
    Export writes it and import compares with it, so a redacted copy never reads as a local change."""
    return c.redact_local(c.redact(_for_mirror(text) if rel.startswith(BRIEFS) else text))


def _namer():
    """T-3574: rel -> the path the mirror gives a brief or research note. A brief's name is kebab(title) from its
    creation (a research note's is often slugged from free text too), so a title holding the home folder or a private
    term named the file with it. In kebab form the home (home-/users-<login>, or the home's own kebab when it has two
    parts) reads home-user and a private term private, a host glob's (*.lan.example.test) with the one label before
    it; either cut short at the end of a name (kebab cuts at 48) too. Other names are kept. Built once per export or
    import (the terms are read once). An ip stays: in kebab form 10-22-08-37 is as likely a date."""
    kebab = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")  # noqa: E731  c.kebab, uncut, never "task"

    def rx(alts, cuts, low):  # whole matches, or at the end of a name a cut of `low` or more characters
        cut = [lead + t[:k] + "$" for lead, t in cuts for k in range(len(t) - 1, low - 1, -1) if t[k - 1] != "-"]
        return re.compile(r"(?i)(?<![a-z0-9])(?:" + "|".join([a + r"(?![a-z0-9])" for a in alts] + cut) + ")")
    home, subs = os.path.expanduser("~").rstrip("/"), []
    if login := kebab(os.path.basename(home)):
        own = kebab(home)
        subs.append((rx([f"(?:home|users)-{login}"] + [own] * ("-" in own), [("(?:home|users)-", login)], 3),
                     "home-user"))
    terms = sorted({(kebab(t.replace("*", "")), t.startswith("*.")) for t in c.private_terms()},
                   key=lambda x: -len(x[0]))
    if terms := [("(?:[a-z0-9]+-)?" if glob else "", t) for t, glob in terms if t]:
        subs.append((rx([lead + t for lead, t in terms], terms, 4), "private"))

    def name(rel):
        if rel.startswith(BRIEFS):
            d, f = os.path.split(rel)
            m = _ID.match(f)
            if not m or not f.endswith(".md"):
                return rel
            head, stem = (d and d + "/") + m.group(1), f[len(m.group(1)):-3]
        elif rel.startswith("research/") and rel.endswith(".md"):
            head, stem = "research", rel[len("research"):-3]
        else:
            return rel
        red = stem
        for r, repl in subs:
            red = r.sub(repl, red)
        return rel if red == stem else head + red + ".md"
    return name


def _from_mirror(text):
    """A mirrored brief as this machine may take it: no grants, no run or worktree marks, audits relabelled so they
    don't count here (this machine's gates need its own runs and audits)."""
    # a pulled brief is untrusted text that fm task show and other listings print: no terminal sequences. Plain first
    # (T-1665 review): an invisible character inside a mark hid it from the subs, then plain took it out
    b = c.Brief.parse(_AUDIT_LINE.sub(r"\1, imported)", c._unmarked(c.plain_lines(text))))
    for k in LOCAL_ONLY:
        b.meta.pop(k, None)
    if isinstance(b.meta.get("scope"), list):  # T-3508: the export showed this machine's home as ~
        b.meta["scope"] = [os.path.expanduser(s) if isinstance(s, str) and s.startswith("~/") else s
                           for s in b.meta["scope"]]
    return b


def _save_base(p, base):
    meta = c.read_meta(p)
    if meta.get("sync_base") != base:
        meta["sync_base"] = base
        c.write_meta(p, meta)


def export(p):
    """Bring the mirror up to date with local state, leaving files that changed in the repo for the import.
    Returns the number of files written."""
    root = mirror(p)
    if os.path.islink(root):
        raise OSError(f"{root} is a symlink; fm sync writes only a real folder")
    base, written, here, name, gone = dict(c.read_meta(p).get("sync_base") or {}), 0, {}, _namer(), set()
    old, stamps = _stamps(p), {"v": _version()}
    readme = os.path.join(root, "README.md")  # an older fm wrote its own wording: refreshed with the first re-scrub
    if not os.path.exists(readme) or not old and os.path.isfile(readme) and _read(readme) != README \
            and _read(readme).startswith(("# " + DIR + "\n", "# " + OLD + "\n")):  # fm's heading: not the user's own
        _write(readme, README)
    if not os.path.exists(os.path.join(root, ".gitattributes")):  # T-3333: git keeps both sides of concurrent appends
        _write(os.path.join(root, ".gitattributes"), "*.jsonl merge=union\n")
    for lrel in _local(p):
        m = _ID.match(os.path.basename(lrel)) if lrel.startswith(BRIEFS) else None
        if lrel.startswith(BRIEFS) and not m:
            continue  # not a brief (fm names them T-NNNN-…)
        rel = name(lrel)  # T-3574: the mirror's name for it, redacted; base and stamps key by it
        if rel != lrel and not m:
            gone.add(lrel)  # a research note's old-named copy, dropped below while it is still ours
        src, dest = os.path.join(p.dir, lrel), os.path.join(root, rel)
        seen = [_stamp(src), _stamp(dest)]
        if seen[1] and old.get(rel) == seen and rel in base:  # T-3349: neither side moved since the last export
            stamps[rel] = seen
            if m:
                here[m.group(1)] = rel
            continue
        try:
            text = _outgoing(rel, _read(src))  # the mirror is committed: no secret from any older note may reach it
        except ValueError:
            continue
        if m:
            here[m.group(1)] = rel
        cur = _read(dest) if os.path.isfile(dest) and not os.path.islink(dest) else None
        if cur is not None and cur != text and _sha(cur) != base.get(rel):
            if (clean := c.redact_local(c.redact(cur))) != cur:  # T-3547: but never kept with a home or private host
                _write(dest, clean)
                written += 1
            continue  # changed in the repo (pull, merge): the import takes it first
        if cur != text:
            _write(dest, text)
            written += 1
        base[rel] = _sha(text)
        stamps[rel] = [_stamp(src), _stamp(dest)]
    for rel in list(_walk(root)):  # a brief archived here moved (or renamed): drop its old copy if it is still ours
        m = _ID.match(os.path.basename(rel))
        if (rel in gone or rel.startswith(BRIEFS) and m and here.get(m.group(1), rel) != rel) \
                and _sha(_read(os.path.join(root, rel))) == base.get(rel):
            os.remove(os.path.join(root, rel))
            base.pop(rel, None)
            stamps.pop(rel, None)
    import time
    from sampo import eco, mandate
    # one file per machine, so pulls never conflict; recomputed at most every 10 minutes (the seal scans the ledger)
    mid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(eco.machine()["id"]))
    fdest = os.path.join(root, "fleet", mid + ".md")
    if not os.path.isfile(fdest) or time.time() - os.path.getmtime(fdest) > 600:
        fleet = "# fleet\n\nT-3334: this machine's drift fingerprint parts (hashes only).\n\n" \
                + "".join(f"- {k}: {v or '-'}\n" for k, v in mandate.parts(p).items())
        if not os.path.isfile(fdest) or _read(fdest) != fleet:
            _write(fdest, fleet)
    _save_base(p, base)
    if stamps != old:
        c.write_atomic(os.path.join(p.dir, STAMPS), json.dumps(stamps, sort_keys=True))
    return written


def _local(p):
    """The local files that travel, walking only their folders: state also holds folders of ~300,000 files that never
    travel (exam runs), and walking those cost every export seconds (T-3349)."""
    for top in sorted({x.rstrip("/").split("/")[0] for x in BRIEFS + ("research/",)}):
        yield from (f"{top}/{rel}" for rel in _walk(os.path.join(p.dir, top)) if _travels(f"{top}/{rel}"))
    for name in ("decisions.md", "fixes.md"):
        path = os.path.join(p.dir, name)
        if os.path.isfile(path) and not os.path.islink(path):
            yield name


def _stamp(path):
    """[mtime_ns, size] of the path itself (lstat: a file swapped for a symlink reads as changed), None when absent."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _version():
    """The stamps' "v": SCRUB, and a digest of the user's private terms once there are any (T-3529), so adding or
    removing a term rewrites every mirrored file on the next export."""
    terms = c.private_terms()
    return f"{SCRUB}:{c.private_digest(terms)}" if terms else SCRUB


def _stamps(p):
    try:
        with open(os.path.join(p.dir, STAMPS), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("v") == _version() else {}  # older stamps: rewrite all
    except (OSError, ValueError):
        return {}


def _set_aside(p, rel, text):
    dest = os.path.join(p.dir, "sync-conflicts", rel.replace("/", "__"))
    _write(dest, text)
    return os.path.relpath(dest, p.dir)


def import_(p):
    """Take in what changed in the mirror since fm last wrote or read it. Returns {added, updated, conflicts}."""
    root, res, name = mirror(p), {"added": 0, "updated": 0, "conflicts": []}, _namer()
    base = dict(c.read_meta(p).get("sync_base") or {})
    mine = {b.id: b for b in c.load_briefs(p, include_archive=True)}
    active = {b.id for b in mine.values() if b.status in ("active", "verifying")}
    notes = {n: r for r in ("research/" + x for x in _walk(os.path.join(p.dir, "research"))) if (n := name(r)) != r}
    for rel in _walk(root):
        if not _travels(rel):
            continue
        if os.path.getsize(os.path.join(root, rel)) > _limit(rel):
            res["conflicts"].append(f"{rel}: too large to import (over {_limit(rel) // 1000} kB); left in the repo")
            continue
        cur = _read(os.path.join(root, rel))
        h = _sha(cur)
        if base.get(rel) == h:
            continue  # nothing new from the repo
        if _CONFLICT.search(cur):
            res["conflicts"].append(f"{rel}: unresolved merge conflict (resolve it in git, then fm sync import)")
            continue
        dest = os.path.join(p.dir, notes.get(rel, rel))  # T-3574: a note the mirror renamed is this machine's own
        if rel.startswith(BRIEFS):
            try:
                theirs = _from_mirror(cur)
            except ValueError:
                res["conflicts"].append(f"{rel}: not a brief")
                continue
            local = mine.get(theirs.id)
            if local and name(os.path.relpath(local.path, p.dir).replace(os.sep, "/")) == rel:
                dest = local.path  # T-3574: the mirror's redacted name for this machine's own file: it keeps its name
            # T-3396: focus is per checkout: a task active here (main or a lane) stays active whatever the repo's older
            # copy says (a merged lane commit carries a planned one); only a closed copy ends it
            keep = theirs.id in active and theirs.status in c.RUNNABLE and local.status
            settle(theirs, local is None)
            if keep:  # this machine's own focus stays
                theirs.meta["status"] = keep
            if local is None:
                _write(dest, theirs.render())
                res["added"] += 1
            else:
                mine_text = _outgoing(rel, _read(local.path))
                if base.get(rel) is None and (theirs.meta.get("updated") or "") <= (local.meta.get("updated") or ""):
                    base[rel] = h  # never synced, and this copy is as new: keep it (the next export writes it)
                    continue
                if base.get(rel) is not None and _sha(mine_text) != base[rel]:
                    res["conflicts"].append(f"{theirs.id}: changed here and in the repo; kept this one, theirs is in "
                                            f"{_set_aside(p, rel, cur)}")
                    base[rel] = h
                    continue
                if local.meta.get("created") and theirs.meta.get("created") != local.meta.get("created"):
                    res["conflicts"].append(f"{theirs.id}: the repo has a different task with this id; kept this one, "
                                            f"theirs is in {_set_aside(p, rel, cur)}")
                    base[rel] = h
                    continue
                theirs.meta.pop("confirm_why", None)  # T-1750: only this machine says why a task waits
                for k in LOCAL_ONLY + LEASE_MARKS + ("confirm", "confirm_why"):  # T-1749: this machine's own marks
                    if k in local.meta:  # and yes stay
                        theirs.meta[k] = local.meta[k]
                if os.path.abspath(local.path) != os.path.abspath(dest):
                    os.remove(local.path)  # archived (or restored) on the other side
                _write(dest, theirs.render())
                res["updated"] += 1
        elif rel in ("decisions.md", "fixes.md") or os.path.basename(rel) == "steers.md":  # T-3333: steers too, # append-only: take the lines this copy lacks (T-1032: fixes)
            have = _read(dest) if os.path.exists(dest) else ""
            seen = set(have.splitlines()) | set(_outgoing(rel, have).splitlines())  # ours, as exported too
            extra = [l for l in c.plain_lines(cur).splitlines() if l.strip() and l not in seen]
            if extra:
                _write(dest, have.rstrip("\n") + ("\n" if have else "") + "\n".join(extra) + "\n")
                res["updated"] += 1
        elif os.path.basename(rel) in LINE_FILES:  # T-3333: both sides' lines, each once, in one order
            have = _read(dest) if os.path.exists(dest) else ""
            ours = set(_outgoing(rel, have).splitlines()) - set(have.splitlines())  # T-3508: ours, as exported
            merged = merge_lines(have, "\n".join(l for l in c.plain_lines(cur).splitlines() if l not in ours))
            if merged != have:
                _write(dest, merged)
                res["updated" if have else "added"] += 1
        elif not os.path.exists(dest):
            _write(dest, c.plain_lines(cur))  # pulled text: no terminal sequences
            res["added"] += 1
        elif cur not in (ours := _read(dest), _outgoing(rel, ours)):  # T-3508: or the same, as exported
            res["conflicts"].append(f"{rel}: differs here; kept this one, theirs is in {_set_aside(p, rel, cur)}")
        base[rel] = h
    _save_base(p, base)
    return res


def incoming(p):
    """Has the repo changed any mirrored file since fm last wrote or read it?"""
    base, root = c.read_meta(p).get("sync_base") or {}, mirror(p)
    return any(_travels(rel) and os.path.getsize(os.path.join(root, rel)) <= _limit(rel)
               and base.get(rel) != _sha(_read(os.path.join(root, rel))) for rel in _walk(root))


def mirrored_ids(p):
    return {m.group(1) for rel in _walk(mirror(p)) if rel.startswith(BRIEFS)
            for m in [_ID.match(os.path.basename(rel))] if m}


def status(p):
    root, meta = mirror(p), c.read_meta(p)
    base = meta.get("sync_base") or {}
    local = {b.id for b in c.load_briefs(p, include_archive=True)}
    theirs, incoming = set(), []
    for rel in _walk(root):
        if rel.startswith(BRIEFS) and (m := _ID.match(os.path.basename(rel))):
            theirs.add(m.group(1))
            if base.get(rel) != _sha(_read(os.path.join(root, rel))):
                incoming.append(m.group(1))
    st = {"on": bool(meta.get("sync")), "dir": root, "briefs": len(theirs), "local": len(local),
          "repo_only": sorted(theirs - local), "local_only": sorted(local - theirs), "incoming": sorted(incoming)}
    if meta.get("sync_move_error"):  # T-3342: the .foreman -> .sampo move failed; `sampo sync export` retries it
        st["move_error"] = meta["sync_move_error"]
    return st


def cmd_sync(args):
    from sampo import cli
    p = cli.resolve(args)
    if args.action == "on":
        with c.lock(p.dir):
            try:
                move(p)
                res = import_(p)  # a clone that already has a mirror takes it before writing its own
                meta = c.read_meta(p)
                meta["sync"] = True
                c.write_meta(p, meta)
                n = export(p)
            except (OSError, ValueError) as e:
                meta = c.read_meta(p)
                meta["sync"] = False
                c.write_meta(p, meta)
                raise cli.UsageError(f"can't write {mirror(p)}: {e}; sync stays off")
            c.log_event(p, "sync", data={"on": True, "written": n, "imported": res["added"] + res["updated"]},
                        session=cli.session())
            c.regen_views(p)
        name = os.path.basename(mirror(p))
        notes = [f" {name}/ is ignored by git here: add `!{name}/` to .gitignore, or it never reaches another "
                 "clone." if c.mirror_ignored(c.main_view(p).root, name) else "",
                 f" This repo is marked sensitive: review {name}/ before pushing (evidence is redacted, but briefs "
                 "describe the work)." if c.read_meta(p).get("sensitive") else ""]
        return print(f"Mirroring Sampo state to {mirror(p)} ({n} file(s) written); commit it with your work. "
                     f"Approvals stay on this machine." + "".join(notes)
                     + "".join(f"\n  conflict: {x}" for x in res["conflicts"]))
    if args.action == "off":
        with c.lock(p.dir):  # with the lock, no export can recreate the folder mid-removal
            meta = c.read_meta(p)
            meta["sync"] = False
            c.write_meta(p, meta)
            if args.remove and os.path.isdir(mirror(p)) and not os.path.islink(mirror(p)) and _ours(mirror(p)):
                shutil.rmtree(mirror(p))
            c.log_event(p, "sync", data={"on": False, "removed": bool(args.remove)}, session=cli.session())
        return print(f"Sync off; {mirror(p)} " + ("removed." if args.remove else "left as it is (fm sync off --remove "
                                                                                    "deletes it)."))
    if args.action in ("import", "export"):
        with c.lock(p.dir):
            if args.action == "export":
                move(p, retry=True)
                res = {"written": export(p)}
                text = f"{res['written']} file(s) written to {mirror(p)}."
            else:
                res = import_(p)
                text = (f"{res['added']} added, {res['updated']} updated from {mirror(p)}."
                        + "".join(f"\n  conflict: {x}" for x in res["conflicts"]))
            c.log_event(p, "sync_" + args.action, data=dict(res), session=cli.session())
            c.regen_views(p)
        return cli.out(args, res, text)
    st = status(p)
    cli.out(args, st, f"Sync {'on' if st['on'] else 'off'} · {st['dir']} · {st['briefs']} brief(s) mirrored, "
                        f"{st['local']} here" + (f"\n  move to {DIR}/ failed: {st['move_error']} (sampo sync export retries)"
                                                 if st.get("move_error") else "") + "".join(f"\n  {k.replace('_', ' ')}: {', '.join(st[k])}" for k in
                                                         ("incoming", "repo_only", "local_only") if st[k])
              + ("\n  A teammate joins from their clone, once .sampo/ is pushed: fm sync on" if st["on"] else ""))


def cmd_private(args):
    """T-3529: sampo private add|rm|list TERM — the user's private terms (a host glob like *.home.example.test, or a
    plain word), one JSON file in the state folder. The mirror export writes <host> or <private> in their place and
    sampo ship refuses a push carrying one. A term is never printed or logged: a digest and a count are."""
    from sampo import cli
    path, term = os.path.join(c.state_dir(), c.PRIVATE_TERMS), (args.term or "").strip().lower()
    if args.action == "list":
        terms = c.private_terms()
        if terms and sys.stdout.isatty():
            return print("\n".join(terms))
        return print(f"{len(terms)} private terms ({', '.join(c.private_digest([t]) for t in terms) or 'none'}); "
                     "run sampo private list in a terminal to read them")
    if not c.private_ok(term):
        raise cli.UsageError("a private term is a host glob (*.home.example.test) or a word: 3 or more characters "
                             "besides * and dots, no spaces or control characters")
    with c.lock(c.state_dir()):
        terms = c.private_terms()
        if args.action == "rm" and term not in terms:
            raise cli.UsageError(f"no private term with digest {c.private_digest([term])}; sampo private list")
        terms = sorted(set(terms) - {term} if args.action == "rm" else set(terms) | {term})
        if not os.path.exists(path):
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))  # write_atomic keeps this mode
        c.write_atomic(path, json.dumps(terms))
    try:
        p = cli.resolve(args, create=False)
    except cli.UsageError:
        p = None  # outside a project: the terms are the user's, not a project's; nothing to log in
    if p:
        c.log_event(p, "private_terms", data={"op": args.action, "digest": c.private_digest([term]), "count": len(terms)},
                    session=cli.session())
    print(f"{'Removed' if args.action == 'rm' else 'Added'} a private term (digest {c.private_digest([term])}); "
          f"{len(terms)} in all. The next sync export rewrites the mirror.")
