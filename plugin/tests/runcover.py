"""T-3593: run.py's coverage recorder. A .pth in the venv run.py starts each class with loads it in every Python of the
class's process tree, never from plugin/tests. It appends to the file RUNPY_COVER names the files under the folders
RUNPY_COVER_ROOTS names (the repo) that the process reads: "f PATH" for a module it runs or a file it opens, "s PATH"
for one it stats (exists, isdir, access), "d PATH" for a folder it lists (not the import system's own listings), and
"*" (the whole tree; with "g", git's HEAD and tags, when that child is git on the repo) when it starts a child it can't
follow: a Python without this hook or writing elsewhere (another interpreter, -S, another RUNPY_COVER), git on the
repo, a program working in the repo or given a path in it, or one that runs a python itself (a shell, env, bwrap).
T-3603: with sys.monitoring (Python 3.12+) a module the process runs is "m PATH" (its module and class bodies ran: the
file less its functions' bodies) and each function of it that starts "c PATH QUALNAME" (a nested one as the function
it is in), so run.py reruns a class for an edited function only when the class ran it; the import system reading a
module's source or .pyc to run it is that module's "m", not its whole file."""
import os
import sys

VAR, ROOTS_VAR = "RUNPY_COVER", "RUNPY_COVER_ROOTS"
PY = ""  # this venv's python: a child started with it records too
TOOL = 3  # T-3603: the sys.monitoring tool id; debug.py's flight recorder takes 4
_ST = {"ctx": None, "seen": set(), "fd": -1, "ino": None, "out0": None, "rs": None, "roots": (), "roots0": "",
       "mon": False, "funcs": set(), "where": {}, "wroots": None}


def setup(py):
    """The process's start: what it was given still counts if the code under test clears its environment."""
    global PY
    PY = py
    _ST["out0"], _ST["roots0"] = os.environ.get(VAR), os.environ.get(ROOTS_VAR, "")
    sys.addaudithook(_hook)
    os.stat, os.lstat, os.access = _watch(os.stat), _watch(os.lstat), _watch(os.access)
    try:
        m = sys.monitoring
        m.use_tool_id(TOOL, "runcover")
        m.register_callback(TOOL, m.events.PY_START, _start)
        m.set_events(TOOL, m.events.PY_START)
        _ST["mon"] = True
    except Exception:  # before 3.12, or the id taken: a module counts whole, as before T-3603
        pass


def _start(code, offset):
    """T-3603: a function's first start ("c PATH QUALNAME"); module and class bodies (not CO_OPTIMIZED) are their
    module's "m". Remembered even with nothing to write to, so a fork that records later names what its parent ran
    (a fork server's preloads): the code's event is off after its first start, in the fork too."""
    try:
        if code.co_flags & 1:
            f, roots = code.co_filename, _roots()
            if _ST["wroots"] is not roots:  # where each file is, judged against the repo it is now
                _ST["wroots"], _ST["where"] = roots, {}
            key = f if os.path.isabs(f) else (f, os.getcwd())
            if (p := _ST["where"].get(key, 0)) == 0:
                p = _ST["where"][key] = os.path.abspath(f) if f[:1] != "<" and _inside(f) else None
            if p:
                q = code.co_qualname.split(".<locals>", 1)[0]
                _ST["funcs"].add((p, q))
                _put([f"c {p} {q}"])
    except Exception:  # never fail the code under test
        pass
    return sys.monitoring.DISABLE


def _watch(fn):
    """fn recording the repo path it is asked about ("s PATH"): stat, exists, isdir, access raise no audit event.
    The watched one stands in os.supports_* where fn does, so shutil.rmtree keeps its fd-based walk."""
    def watched(path, *args, **kw):
        try:
            if kw.get("dir_fd") is None and isinstance(p := os.fspath(path), (str, bytes)) and _inside(
                    p := os.fsdecode(p)):
                _put([f"s {os.path.abspath(p)}"])
        except Exception:  # an fd, or nothing to record: fn answers as ever
            pass
        return fn(path, *args, **kw)
    watched.__name__, watched.__qualname__, watched.__doc__ = fn.__name__, fn.__qualname__, fn.__doc__
    for group in (os.supports_fd, os.supports_dir_fd, os.supports_follow_symlinks, os.supports_effective_ids):
        if fn in group:
            group.add(watched)
    return watched


def _out():
    return os.environ.get(VAR) or _ST["out0"]


def _roots():
    r = os.environ.get(ROOTS_VAR, "")
    if r != _ST["rs"]:  # a nested run.py names its own repo: the one this process started for stays too
        _ST["rs"], _ST["roots"] = r, tuple({x for x in (r + os.pathsep + _ST["roots0"]).split(os.pathsep) if x})
    return _ST["roots"]


def _inside(path, cwd=None):
    """path (relative to cwd, else this process's) is in the repo; an absolute one never asks for a gone cwd."""
    return (os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path))
            + os.sep).startswith(_roots())


def _fd(out):
    """The open record file and whether this process (a fork) or file is new; reopened when the code under test
    closed the descriptor or reused its number (os.closerange), so a line never lands in a file of its own."""
    ctx = (os.getpid(), out)
    if _ST["ctx"] == ctx:
        try:
            st = os.fstat(_ST["fd"])
            if (st.st_dev, st.st_ino) == _ST["ino"]:
                return _ST["fd"], False
        except OSError:
            pass
    fd = os.open(out, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
    st, new = os.fstat(fd), _ST["ctx"] != ctx
    _ST["ctx"], _ST["fd"], _ST["ino"] = ctx, fd, (st.st_dev, st.st_ino)
    if new:
        _ST["seen"] = set()
    return fd, new


def _put(lines):
    out = _out()
    if not out:
        return
    fd, new = _fd(out)
    if new:  # what the process already runs counts too (a fork server's preloaded modules, the functions they ran)
        kind = "m" if _ST["mon"] else "f"
        lines = [*lines, *(f"{kind} {f}" for m in list(sys.modules.values())
                           if isinstance(f := getattr(m, "__file__", None), str) and os.path.isabs(f) and _inside(f)),
                 *(f"c {p} {q}" for p, q in list(_ST["funcs"]))]
    new = [ln for ln in dict.fromkeys(lines) if ln not in _ST["seen"]]
    if new:  # no lock: two threads racing write a line twice at worst, never miss one
        os.write(fd, "".join(ln + "\n" for ln in new).encode("utf-8", "surrogateescape"))
        _ST["seen"].update(new)


def _python_follows(prog, argv, cwd, e):
    it = iter(argv[1:])
    for a in it:  # its options, up to the script, -c or -m
        if a in ("-X", "-W"):
            next(it, None)
        elif a.startswith(("-X", "-W")):
            continue
        elif a == "-" or not a.startswith("-") or a.startswith("--"):
            break
        elif "S" in a:  # -S: no site, so no .pth and no hook
            return False
        elif "c" in a or "m" in a:
            break
    return os.path.abspath(os.path.join(cwd, prog)) == PY and e.get(VAR) == _out()


def _which(name, path):
    import shutil  # here: its import costs ~10 ms, too much for every Python's start
    return shutil.which(name, path=path)


SEALED = ("bwrap", "docker", "podman", "firejail", "unshare", "chroot", "ssh", "sudo")  # another view of the files


# T-3623: read the repo only through a path they are given (judged below), never through their working folder
QUIET = frozenset(("sleep", "true", "false", "echo", "nvidia-smi", "nproc", "uname", "whoami", "hostname"))
BROWSERS = ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome")  # what helpers.headless_chrome finds


def _one_shot(name, argv):
    """A browser given one page to dump or shoot (--dump-dom, --screenshot) and no devtools pipe, which loads pages
    later (Page.navigate) where no hook sees. Its page is judged by its arguments; the page's own relative
    subresources (scripts, styles) are not keyed."""
    return name in BROWSERS and not any(a.startswith("--remote-debugging") for a in argv) and any(
        a.startswith(("--dump-dom", "--screenshot")) for a in argv) and any("://" in a for a in argv[1:])


def _path(t):
    """A word as a file name: --opt=value's value, file://host/x?a=1#f as /x, %-escapes undone."""
    from urllib.parse import unquote
    t = t.strip("'\"();|&<>=")
    if t.startswith("file://"):
        t = "/" + t[7:].partition("/")[2]
    return unquote(t.split("?")[0].split("#")[0])


def _shebang(prog, path):
    """The python a script's #! line runs (resolved on PATH for /usr/bin/env), else None."""
    try:
        with open(prog, "rb") as f:
            line = f.read(256).split(b"\n", 1)[0].decode()
    except (OSError, UnicodeDecodeError):
        return None
    words = line[2:].split() if line.startswith("#!") else []
    if words[:1] and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if w != "-S"]
        words[:1] = [_which(words[0], path) or words[0]] if words else []
    return words[0] if words and os.path.basename(words[0]).startswith("python") else None


def _blind(prog, argv, cwd, env, wrapper=False):
    """Truthy when a child would read what no hook records ("g": git on the repo; a wrapper's own part: not judged by
    its working folder)."""
    e = os.environ if env is None else env
    path = e.get("PATH", os.defpath)
    if "/" not in prog:  # found on the child's PATH, as subprocess and exec find it
        prog = _which(prog, path) or prog
    if (py := _shebang(os.path.join(cwd, prog), path)):  # a python script run as a program: fm, the hook
        prog, argv = py, [py, os.path.join(cwd, prog), *argv[1:]]
    name = os.path.basename(prog)
    if name.startswith("python"):
        return not _python_follows(prog, argv, cwd, e)
    if name in SEALED:
        return True
    at = next((i for i, a in enumerate(argv[1:], 1) if os.path.abspath(os.path.join(cwd, a)) == PY), None)
    if at is not None:  # a wrapper (env, timeout, a claude stand-in) running this venv's python: that one records
        return _blind(prog, argv[:at], cwd, env, True) or not _python_follows(PY, argv[at:], cwd, e)
    if name == "git":
        where, it, sub = cwd, iter(argv[1:]), None
        for a in it:
            if a in ("-C", "--git-dir", "--work-tree"):
                where = os.path.join(where, next(it, ""))
            elif a.startswith(("--git-dir=", "--work-tree=")):
                where = os.path.join(where, a.split("=", 1)[1])
            elif a in ("-c", "--namespace", "--exec-path"):
                next(it, None)
            elif not a.startswith("-"):
                sub = a
                break
        cwd = where  # git reads its paths from there
        if sub not in ("init", "clone") and _inside(where) or any(_inside(v) for k in ("GIT_DIR", "GIT_WORK_TREE")
                                                                   if (v := e.get(k))):
            return "g"  # it reads the files and git's own state (HEAD, tags)
    elif _inside(cwd) and not wrapper and name not in QUIET and not _one_shot(name, argv):
        return True  # it may read the repo through its working folder (ls, make, grep -r)
    toks = [_path(t) for a in [prog, *argv] for w in a.split() for t in (w, *w.split("=")[1:2])]
    if name in ("sh", "bash", "dash", "zsh"):  # a command line: a python it runs must be this one, a program not ours
        for t in toks[1:]:
            w = _which(t, path) if t and "/" not in t else t
            if w and os.path.basename(w).startswith("python") and (os.path.abspath(os.path.join(cwd, w)) != PY
                                                                   or e.get(VAR) != _out()):
                return True
            if w and "/" not in t and _inside(w):
                return True
    elif any(os.path.basename(t).startswith("python") for t in toks[1:]):  # a python it runs another way
        return True
    return any(t and _inside(t, cwd) and os.path.exists(os.path.join(cwd, t)) for t in toks)


def _hook(ev, args):
    try:
        if ev == "exec":
            f = getattr(args[0], "co_filename", "")
            if f[:1] != "<" and _inside(f):
                _put([f"{'m' if _ST['mon'] else 'f'} {os.path.abspath(f)}"])
        elif ev in ("open", "sqlite3.connect", "cpython.run_file"):
            p = args[0]
            if isinstance(p, (str, bytes)) and _inside(p := os.fsdecode(p)) and p != _out() and (
                    os.path.isabs(p) or os.path.exists(p)):  # a missing relative name: likely a dir_fd's (rmtree)
                fr = sys._getframe(1)  # T-3603: the import system reading a module (or its .pyc) to run it, not to show it
                run = _ST["mon"] and fr.f_code.co_name == "get_data" and fr.f_code.co_filename.startswith(
                    "<frozen importlib") and fr.f_back is not None and fr.f_back.f_code.co_name == "get_code"
                _put([f"{'m' if run else 'f'} {os.path.abspath(p)}"])
        elif ev in ("os.listdir", "os.scandir"):
            p = "." if args[0] is None else args[0]
            if isinstance(p, (str, bytes)) and _inside(p := os.fsdecode(p)) and \
                    not sys._getframe(1).f_code.co_filename.startswith("<frozen importlib"):  # its path cache
                _put([f"d {os.path.abspath(p)}"])
        elif ev in ("subprocess.Popen", "os.exec", "os.posix_spawn"):
            if ev == "os.posix_spawn" and sys._getframe(1).f_code.co_filename.endswith("subprocess.py"):
                return  # its Popen event was judged
            exe, argv, cwd, env = args if ev == "subprocess.Popen" else (*args[:2], None, args[2])
            argv = [os.fsdecode(a) for a in argv] if isinstance(argv, (list, tuple)) else [os.fsdecode(argv)]
            cwd = os.fsdecode(cwd) if cwd else os.getcwd()
            if why := _blind(os.fsdecode(exe) if exe else argv[0], argv, cwd, env):
                _put(["*", "g"] if why == "g" else ["*"])
        elif ev == "os.system":
            _put(["*"])
    except Exception:  # never fail the code under test (a line a full disk refused is lost)
        pass
