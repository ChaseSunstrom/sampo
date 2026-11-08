"""fmpy (T-0368): fm and every hook start with `env python3`; when that one is below Sampo's floor (JARVIS: 3.11.2,
where argparse drops `fm task evidence ID --step N CMD RESULT`), re-run under a supported interpreter found on this
machine. The first old-Python call searches once and saves the answer in state/python ("none" when there is none);
`fm doctor` searches again. Standard library only, and syntax old interpreters still parse."""
import os
import sys

NAMES = ("python3.14", "python3.13", "python3.12")


def ok(v):
    """3.12.7+, but not 3.13.0 (T-0319: argparse before 3.12.7 and in 3.13.0 drops trailing positionals)."""
    return tuple(v[:3]) >= (3, 12, 7) and tuple(v[:3]) != (3, 13, 0)


def saved_path():
    home = os.environ.get("SAMPO_HOME") or os.environ.get("FOREMAN_HOME") or os.path.join(  # before core's mirror
        os.path.expanduser("~"), ".claude", "foreman")
    return os.path.join(home, "state", "python")


def _read():
    """The saved answer: a path, "none", or None when there is no trustworthy file. It picks the interpreter the
    guard runs on, so a file someone else owns or can write is ignored."""
    path = saved_path()
    try:
        st = os.lstat(path)
        if not os.path.isfile(path) or os.path.islink(path) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            return None
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def find(names=NAMES, uv="python3.1[2-9]"):
    """A supported interpreter on this machine, or None: versioned names on PATH and in ~/.local/bin, then uv's.
    names, uv: only these (T-1805: fm check --python 3.12 passes ("python3.12",), "python3.12")."""
    import glob  # T-2023: only a search, rare, pays for these
    import subprocess
    dirs = os.environ.get("PATH", "").split(os.pathsep) + [os.path.join(os.path.expanduser("~"), ".local", "bin")]
    cands = [os.path.join(d, n) for n in names for d in dirs if d]
    cands += sorted(glob.glob(os.path.join(os.path.expanduser("~"), ".local", "share", "uv", "python",
                                           "cpython-3.1[2-9]*", "bin", uv)), reverse=True)
    seen = set()
    for exe in cands:
        real = os.path.realpath(exe)
        if real in seen or not os.access(exe, os.X_OK):
            continue
        seen.add(real)
        try:
            out = subprocess.run([exe, "-c", "import sys; print(*sys.version_info[:3])"], capture_output=True,
                                 text=True, timeout=10).stdout.split()
            if ok(tuple(int(x) for x in out)):
                return exe
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


def _save(answer):
    path = saved_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(answer + "\n")
        os.replace(tmp, path)
    except OSError:
        pass


def refresh():
    """Search again and save the answer (fm doctor): the interpreter, or None."""
    exe = find()
    _save(exe or "none")
    return exe


def target(v):
    """The interpreter to re-run under, or None: only below the floor, and never the one already running."""
    if ok(v) or os.environ.get("FOREMAN_PY_CHILD"):  # the child it started never starts another
        return None
    exe = _read()
    if exe is None and not os.path.lexists(saved_path()):
        exe = refresh() or "none"
    if not exe or exe == "none" or not os.access(exe, os.X_OK):
        return None
    return None if os.path.realpath(exe) == os.path.realpath(sys.executable) else exe


def _git(root, *args):
    """git's stdout (bytes) in root, or None on any failure; GIT_* variables ignored."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def heal(root, dry=False):
    """T-3727: a crash right after git wrote files leaves some empty on disk (ext4 drops data no fsync forced) while
    its commits stay whole. Restore from HEAD every tracked file that is empty here, staged as HEAD has it and
    non-empty in HEAD: nothing is lost, as the file holds nothing. A file emptied on purpose is staged (or empty in
    HEAD too) and stays. Only a repo's top folder. Returns (restored paths, paths git could not give back); dry: the
    paths it would restore, writing nothing.
    ponytail: a file emptied on purpose and left unstaged looks like crash damage; it is refilled from HEAD (a
    `git checkout` does the same). Content filters (autocrlf) are not applied to the blob written back."""
    root = os.path.realpath(root)
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None or os.path.realpath(top.decode("utf-8", "replace").strip()) != root:
        return [], []
    staged, head = _git(root, "ls-files", "-s", "-z"), _git(root, "ls-tree", "-r", "-z", "HEAD")
    if staged is None or head is None:
        return [], []
    want = {}
    for rec in head.split(b"\0"):
        meta, _, path = rec.partition(b"\t")
        if path and meta.split()[:2] and meta.split()[1] == b"blob":
            want[path] = meta.split()[2]
    healed, failed = [], []
    for rec in staged.split(b"\0"):
        meta, _, path = rec.partition(b"\t")
        bits = meta.split()
        if len(bits) != 3 or bits[2] != b"0" or bits[0] not in (b"100644", b"100755") or want.get(path) != bits[1]:
            continue
        full = os.path.join(root, os.fsdecode(path))
        try:
            st = os.lstat(full)
        except OSError:
            continue  # missing is a deletion, the user's to judge
        if st.st_size or not os.path.isfile(full) or os.path.islink(full):
            continue
        rel = os.fsdecode(path)
        size = _git(root, "cat-file", "-s", os.fsdecode(bits[1]))
        if dry and size and size.strip() != b"0":
            healed.append(rel)
            continue
        blob = _git(root, "cat-file", "blob", os.fsdecode(bits[1])) if size and size.strip() != b"0" and not dry else None
        if not blob:
            if size is None or size.strip() != b"0":
                failed.append(rel)  # HEAD's blob unreadable too (a crash's empty object)
            continue
        tmp = full + ".heal.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755 if bits[0] == b"100755" else 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o755 if bits[0] == b"100755" else 0o644)
            os.replace(tmp, full)
            healed.append(rel)
        except OSError:
            failed.append(rel)
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return healed, failed


FSYNC = "objects,reference,index"


def fsync_ok(root):
    """Whether root's own git config makes git fsync loose objects, refs and the index (core.fsync)."""
    got = (_git(root, "config", "--local", "--get", "core.fsync") or b"").decode("utf-8", "replace")
    parts = {p.strip() for p in got.replace(" ", "").split(",") if p.strip()}
    for alias, more in (("objects", ("loose-object", "pack")), ("committed", ("loose-object", "pack", "reference")),
                        ("added", ("loose-object", "pack", "reference", "index")),
                        ("all", ("loose-object", "pack", "reference", "index"))):
        if alias in parts:
            parts |= set(more)
    return {"loose-object", "reference", "index"} <= parts


def ensure_fsync(root):
    """Set core.fsync so a crash can't leave empty objects, refs or index. True when it changed something; False
    when already right or root is no repo."""
    if fsync_ok(root) or _git(root, "rev-parse", "--git-dir") is None:
        return False
    return _git(root, "config", "--local", "core.fsync", FSYNC) is not None


def empty_modules(plugin):
    """Sampo's own .py files that are empty on disk (an empty __init__.py is normal): the ones whose emptiness
    breaks an import. [paths]"""
    out = []
    for sub in ("lib", os.path.join("lib", "sampo")):
        try:
            for e in os.scandir(os.path.join(plugin, sub)):
                if e.name.endswith(".py") and e.name != "__init__.py" and e.is_file() and e.stat().st_size == 0:
                    out.append(e.path)
        except OSError:
            pass
    return sorted(out)


def crash_heal(plugin, err=None):
    """bin/sampo, before it imports anything of Sampo's: if a module is empty, restore it (and any other empty tracked
    file) from HEAD and say so; the paths still empty come back, with the one fix said on err. []: nothing wrong."""
    err = err or sys.stderr
    if not empty_modules(plugin):
        return []
    root = os.path.dirname(plugin)
    healed, _ = heal(root)
    if healed:
        print("sampo: restored from git HEAD (a crash emptied them): " + ", ".join(healed), file=err)
    left = empty_modules(plugin)
    for path in left:
        rel = os.path.relpath(path, root)
        print("sampo: %s is empty on disk and git has no content for it. Fix: git -C %s checkout HEAD -- %s"
              % (rel, root, rel), file=err)
    return left


def lane_reexec(me):
    """T-2843: run from inside a lane (a git worktree of this checkout), the main install's fm hands over to the lane's own
    plugin/bin/<same name>, so PLUGIN_ROOT is the lane (fm lib check lints its cards; writes land in it). Once (SAMPO_LANE_CHILD).
    No git call: the nearest `.git` above the cwd must be a file naming a worktrees/<x> folder of the .git dir beside this bin
    whose own `gitdir` file names that .git file back, and the lane, that file and the bin must be ours and not group/world-writable."""
    try:
        if os.environ.get("SAMPO_LANE_CHILD"):
            return
        me = os.path.realpath(me)
        git = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(me))), ".git")
        if not os.path.isdir(git):  # not the main checkout (a copy, a lane itself, the plugin cache)
            return
        d = os.path.realpath(os.getcwd())
        while not os.path.exists(os.path.join(d, ".git")):
            if os.path.dirname(d) == d:
                return
            d = os.path.dirname(d)
        link = os.path.join(d, ".git")
        if not os.path.isfile(link):
            return
        with open(link, encoding="utf-8") as f:
            line = f.readline(4096)
        if not line.startswith("gitdir:"):
            return
        # The folder's own text proves nothing (a tarball can say anything): the main repo's registry, which git writes
        # when it makes the worktree, must point back at this very .git file.
        gd = os.path.realpath(os.path.join(d, line[7:].strip()))
        if os.path.dirname(gd) != os.path.join(os.path.realpath(git), "worktrees") or not os.path.isdir(gd):
            return
        with open(os.path.join(gd, "gitdir"), encoding="utf-8") as f:
            back = f.readline(4096).strip()
        cand = os.path.join(d, "plugin", "bin", os.path.basename(me))
        if not back or os.path.realpath(os.path.join(gd, back)) != os.path.realpath(link) or not os.path.isfile(cand) \
                or os.path.realpath(cand) == me:
            return
        for p in (d, link, cand):  # ours alone: nobody else can swap what we are about to run
            st = os.stat(p)
            if st.st_uid != os.getuid() or st.st_mode & 0o022:
                return
        os.environ["SAMPO_LANE_CHILD"] = "1"
        sys.stderr.write("fm: running the lane's own plugin at %s\n" % d)
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, cand] + sys.argv[1:])
    except Exception:  # fail closed: any doubt runs the main install as before
        return


def reexec():
    """Call first in an entry point, before stdin is read: re-runs this process under a supported Python if needed."""
    exe = target(sys.version_info)
    if exe:
        os.environ["FOREMAN_PY_CHILD"] = "1"
        os.execv(exe, [exe] + sys.argv)


# T-3584: each hook call is a new process, so re's own cache starts empty: a PreToolUse Bash compiled ~120 patterns
# (none twice) for ~8.7 ms CPU. keep_regexes keeps the compiled programs per hook event in this module's __pycache__.
RX = {"have": {}, "new": {}, "mtime": 0.0}
RX_MIN_NEW = 4  # a process saves once it compiled this many new patterns (one built from a command's text stays out)
RX_STALE = 3600  # or when the file is an hour old, so a rare path's few patterns get in too
RX_LIMIT = 1000  # entries (an event uses ~50-200, ~0.5 us each to load); past it the file restarts from this process's


def keep_regexes(event, folder=None):
    """Route re's compiles through the programs kept for this hook event. A hit rebuilds the Pattern from its program,
    without parsing; a miss returns re's own Pattern and keeps the program only when it rebuilds to an equal one (flags
    and code compared), so a kept program is the one re makes. Every doubt compiles as re would. The file sits in the
    protected plugin/lib, read like the .pyc beside it (marshal; a valid program swapped for another can't be told
    apart, so no tool may write there: the guard's core). Never raises."""
    try:
        import atexit
        import _sre
        from re import _compiler, _parser
        if "orig" in RX or not sys.implementation.cache_tag or _sre.CODESIZE != 4:
            return
        RX["path"] = os.path.join(folder or os.path.join(os.path.dirname(os.path.abspath(__file__)), "__pycache__"),
                                  "regex-%s.%s.marshal" % ("".join(c for c in event if c.isalnum())[:40],
                                                           sys.implementation.cache_tag))
        RX["stamp"] = (sys.version, _sre.MAGIC, sys.byteorder)
        have, new, orig = RX["have"], RX["new"], _compiler.compile

        def build(p, e):  # the code is kept as bytes: one object to load, a list only for the patterns used
            return _sre.compile(p, e[0], memoryview(e[1]).cast("I").tolist(), *e[2:])

        def compile_(p, flags=0):
            if type(p) not in (str, bytes) or not isinstance(flags, int) or flags & _compiler.SRE_FLAG_DEBUG:
                return orig(p, flags)
            key = (type(p) is bytes, p, int(flags))
            e = have.get(key)
            if e is not None:
                try:
                    return build(p, e)
                except Exception:  # a bad entry: compiled below and replaced
                    pass
            pat = orig(p, flags)
            try:
                import array
                s = _parser.parse(p, flags)
                names = [None] * s.state.groups
                for k, i in s.state.groupdict.items():
                    names[i] = k
                e = (int(flags | s.state.flags), array.array("I", _compiler._code(s, flags)).tobytes(),
                     s.state.groups - 1, dict(s.state.groupdict), tuple(names))
                x = build(p, e)
                if x == pat and x.groups == pat.groups and x.groupindex == pat.groupindex:
                    have[key] = new[key] = e
            except Exception:
                pass
            return pat
        have.update(_rx_load())
        RX["orig"] = orig
        _compiler.compile = compile_
        atexit.register(_rx_save)
    except Exception:
        return


def _rx_load():
    """The kept programs; {} for a missing, unreadable or other Python's file, or one another user owns or can write."""
    try:
        import marshal
        with open(RX["path"], "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_uid != os.getuid() or st.st_mode & 0o022:
                return {}
            stamp, have = marshal.loads(f.read())  # load(f) reads object by object: 10x slower
        if stamp != RX["stamp"] or type(have) is not dict:
            return {}
        RX["mtime"] = st.st_mtime
        return have
    except Exception:
        return {}


def _rx_save():
    """At exit: this process's new programs merged into the file (atomically; a lost race only loses a few for now)."""
    import time
    new = RX["new"]
    if not new or sys.dont_write_bytecode or len(new) < RX_MIN_NEW and time.time() - RX["mtime"] < RX_STALE:
        return
    tmp = "%s.%d.tmp" % (RX["path"], os.getpid())
    try:
        import marshal
        have = _rx_load()
        have.update(new)
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            marshal.dump((RX["stamp"], have if len(have) <= RX_LIMIT else new), f)
        os.replace(tmp, RX["path"])
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
