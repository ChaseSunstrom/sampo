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
module's source or .pyc to run it is that module's "m", not its whole file.
T-3691: git on the repo asked only where the repo is, its branch ("r TOP\\0HEAD", a remote's default branch "r
TOP\\0refs/remotes/X/HEAD") or a path at HEAD ("h TOP\\0PATH", HEAD's commit for an empty PATH) records those reads
instead of "*"; a browser helpers.devtools or shot.Browser starts counts by the pages they send it (a "sampo.page"
audit event: one an http, https, about: or data: page can't load the repo's files from, else "*"). A process writes
no line its record file already holds whole (a fork server's every call rewrote all the server had run). The hook
starts once per process, judges a path it stats once, and drops the audit events it doesn't read at a set lookup:
the guard ran 2.7 times slower under it (T-3712's red latency tests)."""
import os
import sys

VAR, ROOTS_VAR = "RUNPY_COVER", "RUNPY_COVER_ROOTS"
PY = ""  # this venv's python: a child started with it records too
TOOL = 3  # T-3603: the sys.monitoring tool id; debug.py's flight recorder takes 4
HELD_MAX = 8 << 20  # T-3691: a record file read back past this costs more than the lines it saves
MEMO_MAX = 1 << 16  # T-3691: the paths a process stats it remembers judging, cleared past this
_ST = {"ctx": None, "seen": set(), "fd": -1, "ino": None, "out0": None, "rs": None, "roots": (), "roots0": "",
       "mon": False, "funcs": set(), "where": {}, "wroots": None, "raw": (), "out": None, "memo": {}, "env": None}
_LSTAT = os.lstat  # before setup watches it: finding git's repo records nothing
_RAW = (os.fsencode(VAR), os.fsencode(ROOTS_VAR))  # their names in os.environ._data (POSIX, where run.py makes the venv)


def setup(py):
    """The process's start: what it was given still counts if the code under test clears its environment. Once: site
    runs the .pth twice in a venv that sees the system's packages (T-3691: each stat and event was judged twice)."""
    global PY
    if PY:
        return
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
        if code.co_flags & 1 and (p := _where(code.co_filename)):
            q = code.co_qualname.split(".<locals>", 1)[0]
            _ST["funcs"].add((p, q))
            _put([f"c {p} {q}"])
    except Exception:  # never fail the code under test
        pass
    return sys.monitoring.DISABLE


def _where(f):
    """Code file f's path when it is in the repo, else None: remembered per file, judged against the repo it is now."""
    roots = _roots()
    if _ST["wroots"] is not roots:
        _ST["wroots"], _ST["where"] = roots, {}
    key = f if os.path.isabs(f) else (f, os.getcwd())
    if (p := _ST["where"].get(key, 0)) == 0:
        p = _ST["where"][key] = os.path.abspath(f) if f[:1] != "<" and _inside(f) else None
    return p


def _watch(fn):
    """fn recording the repo path it is asked about ("s PATH"): stat, exists, isdir, access raise no audit event.
    The watched one stands in os.supports_* where fn does, so shutil.rmtree keeps its fd-based walk. T-3691: a path
    asked again (by name, and working folder for a relative one) costs a lookup: the guard's isdir and realpath calls
    made a test 2.7 times slower."""
    st = _ST

    def watched(path, *args, **kw):
        try:
            if not kw or kw.get("dir_fd") is None:
                memo = st["memo"] if os.environ is st["env"] else _env()  # unchanged since _env read it
                p = path if path.__class__ is str else os.fsdecode(os.fspath(path))  # an fd raises: nothing to record
                if (key := p if p[:1] == "/" else (p, os.getcwd())) not in memo:
                    if _inside(p):
                        _put([f"s {os.path.abspath(p)}"])
                    if len(memo) >= MEMO_MAX:
                        memo.clear()
                    memo[key] = None  # after _put: a line a full disk refused is tried again
        except Exception:  # nothing to record: fn answers as ever
            pass
        return fn(path, *args, **kw) if args or kw else fn(path)
    watched.__name__, watched.__qualname__, watched.__doc__, watched.__wrapped__ = (
        fn.__name__, fn.__qualname__, fn.__doc__, fn)
    for group in (os.supports_fd, os.supports_dir_fd, os.supports_follow_symlinks, os.supports_effective_ids):
        if fn in group:
            group.add(watched)
    return watched


def _env():
    """T-3691: the paths _watch judged for the record file and repo the environment names now, read at a dict
    lookup's cost from os.environ's raw values (its .get encodes the name and decodes the value each call); a change
    to either starts them afresh, and a fork keeps them with its file's lines (_put's seen). _watch asks again only
    once os.environ changed (its putenv and unsetenv raise audit events) or is another object."""
    _ST["env"] = e = os.environ  # before the read: a change from here on asks again
    try:
        d = e._data
        raw = d.get(_RAW[0]), d.get(_RAW[1])
    except AttributeError:  # the code under test made os.environ a plain dict: read on every call
        raw, _ST["env"] = (e.get(VAR), e.get(ROOTS_VAR)), None
    if raw != _ST["raw"]:
        out, r = (os.fsdecode(v) if v is not None else None for v in raw)
        _ST["raw"], _ST["out"], _ST["memo"] = raw, out, {}
        if (r := r or "") != _ST["rs"]:  # a nested run.py names its own repo: the one this process started for stays too
            _ST["rs"], _ST["roots"] = r, tuple({x for x in (r + os.pathsep + _ST["roots0"]).split(os.pathsep) if x})
    return _ST["memo"]


def _out():
    _env()
    return _ST["out"] or _ST["out0"]


def _roots():
    _env()
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
    if new:  # before ctx: _put's check never pairs this file with what another one holds
        _ST["seen"] = _held(out, st.st_size)
    _ST["ctx"], _ST["fd"], _ST["ino"] = ctx, fd, (st.st_dev, st.st_ino)
    return fd, new


def _held(out, size):
    """T-3691: the lines the record file already holds, which this process needn't write again (every process of a
    class appends to it: each call a fork server forks wrote all the server had run, 84% of what classes recorded).
    One a writer is still appending (no newline yet) isn't held; a file too big to read back saves nothing."""
    if not 0 < size <= HELD_MAX:
        return set()
    try:
        fd = os.open(out, os.O_RDONLY | os.O_CLOEXEC)
        try:
            data = os.read(fd, size)
        finally:
            os.close(fd)
    except OSError:
        return set()
    return set(data[:data.rfind(b"\n") + 1].decode("utf-8", "surrogateescape").split("\n"))


def _put(lines):
    out = _out()
    if not out or _ST["ctx"] and _ST["ctx"][1] == out and all(ln in _ST["seen"] for ln in lines):
        return  # T-3691: seen holds only lines the file has (this process wrote them, or read them back, or its parent)
    fd, new = _fd(out)
    if new:  # what the process already runs counts too (a fork server's preloaded modules, the functions they ran)
        kind = "m" if _ST["mon"] else "f"
        lines = [*lines, *(f"{kind} {p}" for m in list(sys.modules.values())
                           if isinstance(f := getattr(m, "__file__", None), str) and os.path.isabs(f) and (p := _where(f))),
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


# T-3623: read the repo only through a path they are given (judged below), never through their working folder; a
# script by one of these names is judged as any program (T-3691)
QUIET = frozenset(("sleep", "true", "false", "echo", "nvidia-smi", "nproc", "uname", "whoami", "hostname", "cat", "printf",
                   "ps", "rsvg-convert"))  # rsvg-convert loads only files beside or under the one it is given
BROWSERS = ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome")  # what helpers.headless_chrome finds
# T-3691: the functions that start a browser and tell each page they send it (a "sampo.page" audit event)
DRIVERS = frozenset((("helpers.py", "devtools"), ("shot.py", "Browser.__init__")))
WIDE = ("--disable-web-security", "--allow-file-access")  # a page that may load files: no driver judges it
# T-3691: git reading another repo, config or ref store than the one at its working folder (judged unbounded)
GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
           "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS",
           "GIT_CEILING_DIRECTORIES", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")


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
    """The program a script's #! line runs (resolved on PATH for /usr/bin/env), else None."""
    try:
        with open(prog, "rb") as f:
            line = f.read(256).split(b"\n", 1)[0].decode()
    except (OSError, UnicodeDecodeError):
        return None
    words = line[2:].split() if line.startswith("#!") else []
    if words[:1] and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if w != "-S"]
        words[:1] = [_which(words[0], path) or words[0]] if words else []
    return words[0] if words else None


def _top(where):
    """T-3691: the work tree git finds from `where` (its .git), or None inside a .git folder or a bare repo."""
    d = os.path.abspath(where)
    while True:
        for name, found in ((".git", d), ("HEAD", None)):
            try:
                _LSTAT(os.path.join(d, name))
                return found
            except OSError:
                pass
        if d == (d := os.path.dirname(d)):
            return None


def _clean(p):
    return p.isprintable() and "\\" not in p and all(x not in ("", ".", "..") for x in p.split("/"))


def _git_reads(sub, args, top):
    """T-3691: the lines that key what this git command reads of the repo at `top` ([] for where the repo is, or a
    remote's URL from .git/config, which no key ever held), or None when its reads have no bound here (*)."""
    if sub == "rev-parse":
        args = [a for a in args if a not in ("-q", "--quiet", "--git-dir", "--git-common-dir", "--absolute-git-dir",
                                             "--show-toplevel", "--show-prefix", "--show-cdup", "--is-inside-work-tree",
                                             "--is-inside-git-dir", "--is-bare-repository",
                                             "--show-superproject-working-tree") and not a.startswith("--path-format=")]
        if not args:
            return []
        if args in (["--abbrev-ref", "HEAD"], ["--symbolic-full-name", "HEAD"]):
            return [f"r {top}\0HEAD"]
        if args in (["HEAD"], ["--verify", "HEAD"]):
            return [f"h {top}\0"]
    elif sub == "symbolic-ref":
        args = [a for a in args if a not in ("-q", "--quiet", "--short")]
        if len(args) == 1 and (args[0] == "HEAD" or args[0].startswith("refs/remotes/") and args[0].endswith("/HEAD")
                               and args[0].count("/") == 3 and _clean(args[0])):
            return [f"r {top}\0{args[0]}"]
    elif sub == "show":
        if len(args) == 1 and args[0].startswith("HEAD:") and _clean(args[0][5:]):
            return [f"h {top}\0{args[0][5:]}"]
    elif sub == "remote":
        if len(args) == 2 and args[0] == "get-url" and not args[1].startswith("-"):
            return []
    return None


def _blind(prog, argv, cwd, env, wrapper=False, driven=False):
    """Why a child would read what no hook records, a word ("g": git on the repo), else ""; a wrapper's own part is not
    judged by its working folder; driven: a DRIVERS function started it."""
    e = os.environ if env is None else env
    path = e.get("PATH", os.defpath)
    if "/" not in prog:  # found on the child's PATH, as subprocess and exec find it
        prog = _which(prog, path) or prog
    sb = _shebang(os.path.join(cwd, prog), path)
    if sb and os.path.basename(sb).startswith("python"):  # a python script run as a program: fm, the hook
        prog, argv = sb, [sb, os.path.join(cwd, prog), *argv[1:]]
    name = os.path.basename(prog)
    if name.startswith("python"):
        return "" if _python_follows(prog, argv, cwd, e) else "python"
    if name in SEALED:
        return "sealed"
    at = next((i for i, a in enumerate(argv[1:], 1) if os.path.abspath(os.path.join(cwd, a)) == PY), None)
    if at is not None:  # a wrapper (env, timeout, a claude stand-in) running this venv's python: that one records
        return _blind(prog, argv[:at], cwd, env, True) or ("" if _python_follows(PY, argv[at:], cwd, e) else "python")
    devtools = name in BROWSERS and any(a.startswith("--remote-debugging") for a in argv)
    if devtools and not (driven and not any(a.startswith(WIDE) for a in argv)):
        return "browser"  # it loads pages later (Page.navigate) that no driver of ours tells the recorder
    if name == "git":
        where, it, sub, plain = cwd, iter(argv[1:]), None, True  # plain: no option moving what it reads (T-3691)
        for a in it:
            if a in ("-C", "--git-dir", "--work-tree"):
                where = os.path.join(where, next(it, ""))
                plain = plain and a == "-C"
            elif a.startswith(("--git-dir=", "--work-tree=")):
                where, plain = os.path.join(where, a.split("=", 1)[1]), False
            elif a in ("-c", "--namespace", "--exec-path"):
                v = next(it, "")
                plain = plain and a == "-c" and v.lower().startswith("core.quotepath=")
            elif not a.startswith("-"):
                sub = a
                break
            elif a not in ("--no-pager", "-P", "--no-optional-locks"):
                plain = False
        cwd, rest = where, list(it)  # git reads its paths from there
        if sub not in ("init", "clone") and _inside(where) or any(_inside(v) for k in ("GIT_DIR", "GIT_WORK_TREE")
                                                                   if (v := e.get(k))):
            if plain and not any(k in e for k in GIT_ENV) and os.path.isdir(real := os.path.realpath(where)) and (
                    top := _top(real)) and (reads := _git_reads(sub, rest, top)) is not None:  # isdir: an "s" line
                if reads:
                    _put(reads)
                return ""
            return "g"  # it reads the files and git's own state (HEAD, tags)
    elif _inside(cwd) and not wrapper and (name not in QUIET or sb) and not devtools and not _one_shot(name, argv):
        return "cwd"  # it may read the repo through its working folder (ls, make, grep -r)
    toks = [_path(t) for a in [prog, *argv] for w in a.split() for t in (w, *w.split("=")[1:2])]
    if name in ("sh", "bash", "dash", "zsh"):  # a command line: a python it runs must be this one, a program not ours
        for t in toks[1:]:
            w = _which(t, path) if t and "/" not in t else t
            if w and os.path.basename(w).startswith("python") and (os.path.abspath(os.path.join(cwd, w)) != PY
                                                                   or e.get(VAR) != _out()):
                return "shell-python"
            if w and "/" not in t and _inside(w):
                return "shell-program"
    elif any(os.path.basename(t).startswith("python") for t in toks[1:]):  # a python it runs another way
        return "runs-python"
    return "path" if any(t and _inside(t, cwd) and os.path.exists(os.path.join(cwd, t)) for t in toks) else ""


EVENTS = frozenset(("exec", "open", "sqlite3.connect", "cpython.run_file", "os.listdir", "os.scandir",
                    "subprocess.Popen", "os.exec", "os.posix_spawn", "os.system", "sampo.page",
                    "os.putenv", "os.unsetenv"))  # what _hook judges


def _hook(ev, args):
    if ev not in EVENTS:  # T-3691: the rest (id(), sys._getframe, compile) cost a set lookup
        return
    try:
        if ev in ("os.putenv", "os.unsetenv"):  # T-3691: os.environ's every change but to its raw dict: _env reads it
            _ST["env"] = None
        elif ev == "exec":
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
            driven = False
            if ev == "os.posix_spawn":
                code = sys._getframe(1).f_code
                if code.co_filename.endswith("subprocess.py"):
                    return  # its Popen event was judged
                driven = (os.path.basename(code.co_filename), getattr(code, "co_qualname", code.co_name)) in DRIVERS \
                    and _inside(code.co_filename)
            exe, argv, cwd, env = args if ev == "subprocess.Popen" else (*args[:2], None, args[2])
            argv = [os.fsdecode(a) for a in argv] if isinstance(argv, (list, tuple)) else [os.fsdecode(argv)]
            cwd = os.fsdecode(cwd) if cwd else os.getcwd()
            if why := _blind(os.fsdecode(exe) if exe else argv[0], argv, cwd, env, driven=driven):  # "* WHY COMMAND"
                _put([f"* {'git' if why == 'g' else why} {' '.join(' '.join(argv).split())[:200]}"]
                     + (["g"] if why == "g" else []))
        elif ev == "os.system":
            _put([f"* system {' '.join(os.fsdecode(args[0]).split())[:200]}"])
        elif ev == "sampo.page":  # T-3691: a page a driver sends its browser; one of these can't load a file:// URL
            if not str(args[0]).lower().startswith(("about:", "data:", "http://", "https://")):
                _put([f"* page {' '.join(str(args[0]).split())[:200]}"])
    except Exception:  # never fail the code under test (a line a full disk refused is lost)
        pass
