#!/usr/bin/env python3
"""The test suite across the cores (T-0133): the tests `unittest discover` finds, one test class per process (each
test already gets its own FOREMAN_HOME and repo), so the gate waits for the slowest class instead of the sum of all.
Exit 1 on any failure, with each failing class's report.

  python3 plugin/tests/run.py [-j N] [-k PATTERN] [-p GLOB] [--load N] [NAME ...]
"""
import argparse
import ast
import atexit
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

try:
    import fcntl
except ImportError:  # Windows: no flock, so no slot pool; classes run as before
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))


def child_env(tmp):
    """A class's environment: its own TMPDIR, and local models hidden as on CI unless the caller set the switch
    (T-3229: the moons tests leaned on this box's Ollama embedders, passed here and failed on every CI run)."""
    env = {k: v for k, v in os.environ.items() if not (k.startswith(("FOREMAN_", "SAMPO_")) and k.split("_", 1)[1] in OPERATOR_SWITCHES)}  # T-3625
    return dict(env, TMPDIR=tmp, FOREMAN_NO_LOCAL_MODELS=os.environ.get("FOREMAN_NO_LOCAL_MODELS", "1"))


# T-3625: switches an operator sets for one run (either prefix); a test that needs one sets it itself
OPERATOR_SWITCHES = {"LOAD_GATE", "LOAD_WAIT", "FULL_GATE", "SENTINEL", "SENTINEL_RUN", "SENTINEL_TREE", "NO_BACKGROUND",
                     "SLOT", "SLOTS", "SLOT_PRIORITY", "NO_SUITE_SLOT", "GATE_HELD", "GATE_WAIT", "SLOW_GATE", "INHIBITED",
                     "DRIVE_TASK", "QUIET", "NO_NET", "NO_LINK", "CONFINE", "SMOKE_RUNNER", "UI_CACHE_S", "NO_TEST_CACHE",
                     "FAST"}  # T-3571: SAMPO_FAST=0 sends every call past sampo-fast; test_fast_equiv needs it on


SLOT_ENV = "SAMPO_SLOT"  # T-3252: set in a class's children, so a run.py inside a slot never waits on its parent's pool
CGROUP_ROOT = "/sys/fs/cgroup"


def _env(name):
    """T-3442: SAMPO_X, or FOREMAN_X once importing sampo renamed it (core.mirror_env), as in every test process."""
    return os.environ.get(name) or os.environ.get("FOREMAN_" + name[len("SAMPO_"):])


def _cgroup_dir():
    """This process's cgroup v2 folder, or None."""
    try:
        with open("/proc/self/cgroup") as f:
            rel = next((ln.split("::", 1)[1].strip() for ln in f if ln.startswith("0::")), None)
    except OSError:
        return None
    return os.path.join(CGROUP_ROOT, rel.lstrip("/")) if rel else None


def slot_count():
    """T-3252: the CPUs every run.py on the machine shares: SAMPO_SLOTS, else the CPU count cut to the tightest cgroup
    quota above this process (sampo cpu cap 50 on 16 CPUs gives 8)."""
    try:
        return max(0, int(_env("SAMPO_SLOTS")))
    except (TypeError, ValueError):
        pass
    n, d = os.cpu_count() or 4, _cgroup_dir()
    while d and os.path.realpath(d).startswith(os.path.realpath(CGROUP_ROOT)):
        try:
            with open(os.path.join(d, "cpu.max")) as f:
                quota, period = f.read().split()[:2]
            if quota != "max":
                n = min(n, max(1, math.ceil(int(quota) / int(period))))
        except (OSError, ValueError):
            pass
        if os.path.realpath(d) == os.path.realpath(CGROUP_ROOT):
            break
        d = os.path.dirname(d)
    return n


@contextlib.contextmanager
def cpu_slot(wait=3600.0, poll=0.05):
    """T-3252: one of the machine's CPU slots, shared by every run.py on it (mine, each builder's, the ship's, the
    post-merge verify), held while the block runs: its number, or None when nested, off (SAMPO_SLOTS=0) or the wait ran
    out (the class then runs anyway: a slow run beats a hung one). A slot is a lock file, so the kernel frees it when
    its holder dies and a crashed runner leaks nothing. T-3614: while a SAMPO_SLOT_PRIORITY=1 run waits, other runs
    leave freed slots to it (the merge train's gate got 1/N of them beside ~10 testing builders) for half their wait,
    then compete again, so a lane's run under a deadline isn't left NOT_STARTED by a long gate."""
    n = slot_count()
    folder = _slot_folder() if n and not _env(SLOT_ENV) and fcntl else None
    if not folder:  # nested, off, no flock (Windows) or a folder that isn't safely ours: run without one
        yield None
        return
    now, order, prio, mark = time.monotonic(), random.sample(range(n), n), _env(PRIO_ENV) == "1", None
    end, defer = now + wait, now + wait / 2
    try:
        while not (got := _take(folder, order) if prio or time.monotonic() >= defer or not _prio_waiting(folder)
                   else None):
            if time.monotonic() >= end:
                break
            if prio and not mark:
                mark = _mark(folder)
            time.sleep(poll * random.uniform(1, 5))  # spread the retries of many waiters
    finally:
        if mark:
            with contextlib.suppress(OSError):
                os.unlink(mark[1])
            os.close(mark[0])
    if not got:
        yield None
        return
    try:
        yield got[0]
    finally:
        os.close(got[1])  # closing it frees the slot


def _take(folder, order):
    """A free slot: (its number, its locked fd), or None."""
    for i in order:
        try:
            fd = os.open(os.path.join(folder, str(i)), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        return i, fd
    return None


PRIO_ENV = "SAMPO_SLOT_PRIORITY"  # T-3614: =1 (the train's gate and probes, ship's suite, sampo check outside a lane)


def _mark(folder):
    """T-3614: a waiting priority run's marker, (fd, path): locked before it takes its prio- name, so a prio- file
    nobody holds is a dead waiter's. None when it can't be made (the run then just competes)."""
    name = f"prio-{os.getpid()}-{random.getrandbits(32):08x}"
    tmp, path = os.path.join(folder, "." + name), os.path.join(folder, name)
    try:
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.rename(tmp, path)
    except OSError:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return None
    return fd, path


def _prio_waiting(folder):
    """T-3614: whether a priority run waits for a slot (holds a prio- marker); a dead waiter's marker is removed."""
    try:
        names = [x for x in os.listdir(folder) if x.startswith("prio-")]
    except OSError:
        return False
    for name in names:
        path = os.path.join(folder, name)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)
    return False


def _slot_folder():
    """The pool's folder, made if missing: None unless it's a real folder of mine that nobody else may write (a
    squatted /tmp path or an unwritable XDG_RUNTIME_DIR runs the class without a slot)."""
    uid = getattr(os, "getuid", lambda: 0)()
    folder = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), f"sampo-slots-{uid}")
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        st = os.lstat(folder)
    except OSError:
        return None
    import stat
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o022:
        return None
    return folder


SUITE_ENV = "SAMPO_NO_SUITE_SLOT"  # T-3541: =1 runs a full suite without the machine-wide slot


def _holder(fd):
    """Who holds the suite slot, from what it wrote into the lock file."""
    try:
        h = json.loads(os.pread(fd, 4096, 0))
        return f"the full suite running in {h['cwd']} (pid {int(h['pid'])}, {(time.time() - float(h['at'])) // 60:.0f} min)"
    except (OSError, ValueError, TypeError, KeyError):  # not written yet, or garbled
        return "another full suite"


@contextlib.contextmanager
def suite_slot(until):
    """T-3541: the machine's one full-suite slot, held while the block runs: three full suites at once (two builders'
    and the ship's) each took 3x as long and slowed everything else. A second full run waits, saying for whom every
    minute, and goes ahead beside the holder once until (an epoch) passes. None when nested in a class (SAMPO_SLOT),
    SAMPO_NO_SUITE_SLOT=1 or no flock. The holder's pid, cwd and start are in the lock file; the kernel frees the lock
    when the holder dies."""
    fd = None
    if not (_env(SLOT_ENV) or _env(SUITE_ENV) or not fcntl):
        path = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "sampo", "suite.lock")
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    said = 0.0
    try:
        while fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass
            else:
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, 0)
                    os.pwrite(fd, json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "at": time.time()}).encode(), 0)
                break
            now = time.time()
            if now >= until:
                sys.stderr.write(f"run.py: {_holder(fd)} still holds the slot; going ahead beside it\n")
                break
            if now - said >= 60:
                sys.stderr.write(f"run.py: waiting for {_holder(fd)}\n")
                sys.stderr.flush()
                said = now
            time.sleep(0.2)  # the next suite starts within 0.2 s of the slot freeing
        yield
    finally:
        if fd is not None:
            os.close(fd)  # closing it frees the slot


def skipped():
    """T-1101: the modules FOREMAN_SKIP_TESTS names (fm check: another open task's red-first test files)."""
    return {x.strip() for x in os.environ.get("FOREMAN_SKIP_TESTS", "").split(",") if x.strip()}


SPLIT_S = 5.0  # T-3166: a class whose last run took this long runs a job per test
START_S = 0.3  # ... what each job's interpreter start-up costs, left out of a split class's work time
# Classes seen slow (tonight's measurements): split with no times yet (a first run, a --shard), where every other class
# stays one job (an interpreter per test of 1400 classes would swamp a 2-core runner).
SEEDED = {"LeanKind", "Stop", "ExamAuthors", "ExamGen", "Page", "ClockWindows", "PreToolUse", "SessionStart",
          "Worker", "GenesisRead"}


def keeps_whole(t):
    """T-3166: a class with setUpClass/tearDownClass, a module with setUpModule/tearDownModule, a module that won't
    import, or a class marked `run_whole = True` shares state between its tests: one job."""
    c, tc = type(t), unittest.TestCase
    m = sys.modules.get(c.__module__)
    own = lambda n: getattr(getattr(c, n, None), "__func__", None) is not getattr(tc, n).__func__  # noqa: E731  (None: whole)
    return (isinstance(t, unittest.loader._FailedTest) or getattr(c, "run_whole", False)
            or own("setUpClass") or own("tearDownClass")
            or any(hasattr(m, n) for n in ("setUpModule", "tearDownModule")))


def times_path():
    return os.environ.get("FM_TEST_TIMES") or os.path.join(
        tempfile.gettempdir(), f"fmtest-times-{getattr(os, 'getuid', lambda: 0)()}-{hashlib.sha1(os.fsencode(HERE)).hexdigest()[:10]}.json")


def load_times():
    """T-3166: seconds each job took the last time it ran ({job id: s}); {} when there is none yet."""
    try:
        with open(times_path()) as f:
            if os.fstat(f.fileno()).st_uid != getattr(os, "getuid", lambda: 0)():  # someone else's file in a shared /tmp
                return {}
            d = json.load(f)
        return {k: float(v) for k, v in d.items()}
    except Exception:  # OverflowError, RecursionError ...: times only order the jobs
        return {}


def save_times(new):
    """Merged into the file's, so a run of a few modules keeps the rest's; capped, newest kept."""
    d = load_times()
    for k, v in new.items():
        d.pop(k, None)
        d[k] = round(v, 2)
    _write_json(times_path(), dict(list(d.items())[-20000:]))


def _write_json(path, d):
    p = f"{path}.{os.getpid()}"
    try:
        with contextlib.suppress(OSError):
            os.unlink(p)  # a stale or planted one: O_EXCL below then never follows a symlink
        with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600), "w") as f:
            json.dump(d, f)
        os.replace(p, path)  # atomic: a run beside this one reads the old file or the new, never half
    except OSError:
        pass  # times only order the jobs, and the cache only saves reruns


NO_CACHE = "SAMPO_NO_TEST_CACHE"  # T-3502: =1 (or FOREMAN_NO_TEST_CACHE) runs every class, cached or not
CACHE_FULL = "SAMPO_CACHE_FULL"  # T-3638: =1 lets a full run (no names) skip cached classes; only the post-merge verify sets it
SAMPLE = 0.1 # the share of cached classes rerun anyway: a file the coverage missed shows, and the cache is dropped
STATE = ".sampo/"  # Sampo's synced task state: every sampo call writes it, no test reads it (each has its own home)
PRUNE_S = 14 * 86400  # a repo's cache unused this long (a removed lane's) is deleted
KEEP = 3  # T-3593: records kept per class, newest first: main's code beside the lanes' that changed what it reads
COVERED = {}  # T-3593: job id -> what its processes recorded (runcover.py's lines), filled by _run_one
GIT_STATE = "\0git"  # tree()'s key for HEAD and the tags (a recorded "g": git run on the repo); no path is named so,
# nor any of its other "\0" keys (T-3691)


def _cache_base():
    return os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")


def cache_path(repo):
    return os.path.join(_cache_base(), "sampo", "run-cache", hashlib.sha1(os.fsencode(repo)).hexdigest()[:16] + ".json")


def load_cache(path):
    """(generation, {class id: [[digest, tests, [path, ...]], ...]}): per class up to KEEP records of a whole green
    run, each the paths it read and their digest then (T-3593); (0, {}) when missing or corrupt. The generation goes up
    each time a run finds the cache wrong and drops it (T-3502). On disk the paths are indexes into one table."""
    try:
        with open(path) as f:
            d = json.load(f)
        table = d.get("paths", [])
        # ponytail: whole file parsed and indexes resolved each run (~0.1-0.3 s at the full suite's ~1500 classes x 3);
        # store the indexes as strings and decode only the named classes' if that shows in a profile
        recs = {c: [[v[0], int(v[1]), [table[i] for i in v[2]]] for v in vs] for c, vs in d["classes"].items()}
        return int(d["gen"]), recs
    except Exception:  # a corrupt cache, or T-3502's keys, only costs reruns
        return 0, {}


@contextlib.contextmanager
def _cache_lock(path):
    """The cache's lock file held, so a read-merge-write is one step among the runs beside this one; no lock without
    flock (Windows) or a folder it can't write."""
    fd = None
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.utime(path + ".lock")  # in use: another repo's save never prunes it from under its holder
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)  # closing it frees the lock


def save_cache(path, new, gen):
    """New records ({class id: [digest, tests, paths]}) merged into the file's under its lock, each first among its
    class's KEEP, newest 20000 classes kept; nothing when the cache was dropped since this run read it at gen (its
    records came from a cache found wrong). The other repos' caches unused for PRUNE_S go."""
    with _cache_lock(path):
        cur_gen, recs = load_cache(path)
        if cur_gen != gen:
            return
        for c, v in new.items():
            recs[c] = [v] + [r for r in recs.pop(c, []) if r[0] != v[0]][:KEEP - 1]  # newest last: the cap keeps them
        index = {}
        recs = {c: [[r[0], r[1], [index.setdefault(p, len(index)) for p in r[2]]] for r in vs]
                for c, vs in list(recs.items())[-20000:]}
        _write_json(path, {"gen": gen, "paths": list(index), "classes": recs})
    folder = os.path.dirname(path)
    with contextlib.suppress(OSError):
        for name in os.listdir(folder):
            q = os.path.join(folder, name)
            with contextlib.suppress(OSError):
                if q not in (path, path + ".lock") and time.time() - os.lstat(q).st_mtime > PRUNE_S:
                    os.unlink(q)


def drop_cache(path):
    """T-3502: the cache found wrong: emptied at the next generation, so a run that read it before can't save it back."""
    with _cache_lock(path):
        _write_json(path, {"gen": load_cache(path)[0] + 1, "classes": {}})


def _git(cwd, *args, data=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # a hook's GIT_DIR would hash another tree
    return subprocess.run(["git", "-C", cwd, *args], input=data, capture_output=True, text=True, errors="surrogateescape", env=env, timeout=60,
                          check=True).stdout


def tree():
    """T-3502: (repo root, {path: "mode blob"}, the repo's shared git folder) of the files as they are on disk: the
    index's, a modified or untracked file hashed again; GIT_STATE: HEAD and the tags, hashed. None outside git. T-3593: the cache is the git folder's,
    so every worktree of the repo (a lane's, a train's scratch tree) reads and adds to one. T-3691: "\\0HEAD" HEAD's
    commit, "\\0ref:HEAD" its branch's full name, "\\0ref:refs/remotes/X/HEAD" a remote's default branch (both with
    the tags' and remote branches' names)."""
    try:
        repo = _git(HERE, "rev-parse", "--show-toplevel").strip()
        common = os.path.realpath(os.path.join(repo, _git(repo, "rev-parse", "--git-common-dir").strip()))
        blobs = {}
        for ln in _git(repo, "ls-files", "-s", "-z").split("\0"):
            if ln:
                meta, path = ln.split("\t", 1)
                blobs[path] = " ".join(meta.split()[:2])  # mode and blob: a chmod -x is a change (review)
        dirty = _git(repo, "ls-files", "-z", "-m", "-o", "--exclude-standard").split("\0")
        dirty = list(dict.fromkeys(f for f in dirty if f))
        for f in dirty:
            blobs.pop(f, None)  # deleted ones stay out
        live = [f for f in dirty if os.path.isfile(os.path.join(repo, f))]
        if any("\n" in f for f in live):
            return None  # hash-object reads a path a line
        if live:
            hashed = _git(repo, "hash-object", "--stdin-paths", data="\n".join(live) + "\n").split()
            blobs.update((f, f"{'100755' if os.stat(os.path.join(repo, f)).st_mode & 0o100 else '100644'} {b}")
                         for f, b in zip(live, hashed))
        try:  # what git itself says (log, describe, rev-parse) to a class that ran git on the repo: HEAD and the tags,
            head = _git(repo, "rev-parse", "HEAD", "--symbolic-full-name", "HEAD")  # not every ref: a lane's commit
        except subprocess.CalledProcessError:  # no commit yet                       moves one, review
            head = ""
        rows = [ln.split(" ", 2) for ln in _git(repo, "for-each-ref", "--format=%(symref) %(objectname) %(refname)",
                                                "refs/tags", "refs/remotes").splitlines() if ln]
        tags = "".join(f"{o} {r}\n" for _, o, r in rows if r.startswith("refs/tags/"))
        blobs[GIT_STATE] = hashlib.sha1((head + tags).encode()).hexdigest()
        # T-3691: a short ref name's ambiguity (abbrev-ref, --short) rides on these names: the mark (not refs/heads,
        # which every lane adds to)
        mark = hashlib.sha1("".join(r + "\n" for _, _, r in rows).encode()).hexdigest()[:12]
        h = head.split("\n")
        blobs["\0HEAD"] = h[0] if head else ""
        blobs["\0ref:HEAD"] = f"{h[1]} {mark}" if head else os.urandom(8).hex()  # unborn: abbrev-ref fails, rerun
        blobs.update(("\0ref:" + r, f"{s} {mark}") for s, _, r in rows
                     if r.startswith("refs/remotes/") and r.endswith("/HEAD"))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return repo, blobs, common


def _roots(repo):
    """The repo's folder as git names it, resolved, and as this run.py reaches it, each ending in a separator."""
    return sorted({os.path.join(r, "") for r in (repo, os.path.realpath(repo), os.path.dirname(os.path.dirname(HERE)))})


def cover_python():
    """T-3593: a venv whose python loads runcover.py (through a .pth) in every Python started with it, so each process
    records the repo files it reads; that python, or None where it can't be made (not POSIX, no runcover.py beside
    run.py, a python that doesn't start). One per interpreter and runcover.py under the cache folder, outside /tmp,
    which a job sandbox (bwrap --tmpfs /tmp) hides; made aside and renamed in, so a run sees it whole or not at all;
    one no run used for PRUNE_S goes."""
    src = os.path.join(HERE, "runcover.py")
    if os.name != "posix" or not os.path.isfile(src):
        return None
    real = os.path.realpath(sys.executable)
    own = [p for p in site.getsitepackages() + ([site.getusersitepackages()] if site.ENABLE_USER_SITE else [])
           if os.path.isdir(p)]  # run.py's own packages (a venv's) stay importable
    with open(src, "rb") as f:
        key = hashlib.sha1(f"{real}\0{sys.version}\0{own}\0python python3\0".encode() + f.read()).hexdigest()[:16]
    top = os.path.join(_cache_base(), "sampo", "run-py")
    d = os.path.join(top, key)
    py = os.path.join(d, "bin", "python3")
    try:
        if not os.path.exists(py):
            os.makedirs(top, exist_ok=True)
            new = tempfile.mkdtemp(dir=top, prefix=".new-")
            try:
                lib = os.path.join(new, os.path.relpath(site.getsitepackages([d])[0], d))
                os.makedirs(lib)
                os.mkdir(os.path.join(new, "bin"))
                for name in ("python", "python3"):  # first on a class's PATH: `python3 x.py`, fm's #! line record too
                    os.symlink(real, os.path.join(new, "bin", name))
                with open(os.path.join(new, "pyvenv.cfg"), "w") as f:
                    f.write(f"home = {os.path.dirname(real)}\ninclude-system-site-packages = true\n")
                shutil.copy(src, lib)
                with open(os.path.join(lib, "runcover.pth"), "w") as f:
                    f.write("".join(p + "\n" for p in own) + f"import runcover; runcover.setup({py!r})\n")
                subprocess.run([os.path.join(new, "bin", "python3"), "-c", "import runcover, sys; sys.exit(not runcover.PY)"],
                               check=True, timeout=120, capture_output=True)
                with contextlib.suppress(OSError):  # a run beside this one's won: the same venv
                    os.rename(new, d)
            finally:
                shutil.rmtree(new, ignore_errors=True)  # gone once renamed
        os.utime(d)  # in use: the pruning below keeps it
        for name in os.listdir(top):
            if name != key and time.time() - os.lstat(os.path.join(top, name)).st_mtime > PRUNE_S:
                shutil.rmtree(os.path.join(top, name), ignore_errors=True)
    except (OSError, IndexError, subprocess.SubprocessError):
        return None
    return py if os.path.exists(py) else None


def covered(text, repo, blobs):
    """T-3593: the paths a class's recorded lines name, relative to the repo: its files (a module's source for its
    .pyc), its listed folders ('x/', './' the root), '*' for the whole tree. A file on disk that git doesn't see
    (ignored, or under .git/) and Sampo's task state are left out; a missing one stays, so creating it reruns.
    Accepted gaps (the 1-in-10 rerun is their backstop): the import system's own folder listings, so a new module
    shadowing one later on sys.path goes unseen, and where an in-repo symlink points.
    T-3603: a module it ran is 'x.py::' (the file less its functions' bodies), a function it ran 'x.py::QUALNAME' and
    its module's 'x.py::'; a lambda's, a generator expression's or an annotation's outside any function counts as the
    module's. A file it read whole stands for its parts. Accepted gaps: output that depends on where in the file a
    function is (a line number logged, not a traceback, which reads the file) and code read without being run
    (f.__code__)."""
    roots, out = _roots(repo), set()
    for ln in text.splitlines():
        kind, _, p = ln.partition(" ")
        if kind in ("*", "g"):  # a child it couldn't follow; "g": that child was git on the repo
            out.add("*" if kind == "*" else GIT_STATE)
            continue
        if kind in ("r", "h"):  # T-3691: git on a repo read a ref or a path at HEAD: this tree's, else unbounded
            top, _, name = p.partition("\0")
            out.update((("\0ref:" + name) if kind == "r" else "\0HEAD" + (":" + name if name else ""),)
                       if os.path.join(top, "") in roots else ("*", GIT_STATE))
            continue
        qual = None
        if kind == "c":
            p, _, qual = p.rpartition(" ")
            qual = "" if qual.rsplit(".", 1)[-1].startswith("<") else qual  # no def names it: the module's code
        root = next((r for r in roots if (p + os.sep).startswith(r)), None)
        if root is None or kind not in ("f", "d", "s", "m", "c"):
            continue
        rel = p[len(root):].replace(os.sep, "/")
        if kind == "s":  # stat, exists, access: a file's content and mode, else whether it is there (review)
            if rel and not rel.startswith(STATE):
                out.add(rel if rel in blobs else "?" + rel)
            continue
        if kind == "d":
            if not (rel + "/").startswith(STATE):
                out.add(rel.rstrip("/") + "/" if rel else "./")
            continue
        if "__pycache__/" in rel and rel.endswith(".pyc"):
            with contextlib.suppress(ValueError):
                rel = importlib.util.source_from_cache(rel)
        if rel and not rel.startswith(STATE) and (rel in blobs or not os.path.lexists(os.path.join(repo, rel))):
            out.update([rel] if kind == "f" else [rel + "::", rel + "::" + qual] if qual else [rel + "::"])
    return {p for p in out if p in blobs or "::" not in p or p.rpartition("::")[0] not in out}  # a part of a whole one


def shared(repo, blobs):
    """T-3593: what every class depends on: run.py and the helpers beside it (helpers.py, forkcli.py, runcover.py)."""
    rel = os.path.relpath(os.path.realpath(HERE), os.path.realpath(repo)).replace(os.sep, "/")
    return {f for f in blobs if f.startswith(rel + "/") and f.endswith(".py") and "/" not in f[len(rel) + 1:]
            and not os.path.basename(f).startswith("test")}


def _yields(body):
    """Whether a function's own code (not a function or class inside it, whose defaults and bases are its) yields."""
    todo = list(body)
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(c for name, v in ast.iter_fields(n) if name != "body"
                        for c in (v if isinstance(v, list) else [v]) if isinstance(c, ast.AST))
        else:
            todo.extend(ast.iter_child_nodes(n))
    return False


def units(data):
    """T-3603: a Python source's parts, ({function qualname: its body's hash}, the rest's hash), None when it doesn't
    parse. A function's body with its docstring (no code here reads a __doc__ it didn't call) and the functions and
    classes in it ("f.<locals>.g" is f's); the rest is the file with each body cut to whether it yields (a generator
    never started ran none of its body: turning one into a function reruns every class that ran its module). Neither
    holds line numbers or comments. A qualname defined twice (an if/else) is one part of both bodies."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # an invalid escape's SyntaxWarning is the module's business
            tree = ast.parse(data)
    except Exception:  # not Python: a syntax error, a null byte, too deep
        return None
    funcs = {}

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                b, name = node.body, prefix + node.name
                funcs[name] = funcs.get(name, "") + "\0" + "\n".join(ast.dump(s) for s in b)
                node.body = [ast.Expr(ast.Constant(_yields(b)))]
            elif isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")
            else:  # if, for, while, with, try, match: a def in them is the module's (or class's)
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field, None) or [], prefix)
                for sub in (getattr(node, "handlers", None) or []) + (getattr(node, "cases", None) or []):
                    walk(sub.body, prefix)
    walk(tree.body, "")
    h = lambda s: hashlib.sha1(s.encode("utf-8", "surrogateescape")).hexdigest()  # noqa: E731
    return {q: h(s) for q, s in funcs.items()}, h(ast.dump(tree))


_UNITS = {}  # T-3603: blob -> units() of its bytes, for every values() of this run


def _units(repo, rel, blob):
    """units() of a tree's file ("mode blob"): its bytes from disk when they are the blob's, else from git; None for a
    symlink or a submodule, or bytes git can't give (a dirty file a filter changed)."""
    mode, _, sha = blob.partition(" ")
    if mode not in ("100644", "100755"):
        return None
    if sha not in _UNITS:
        data = None
        with contextlib.suppress(OSError):
            with open(os.path.join(repo, rel), "rb") as f:
                data = f.read()
        if data is None or hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != sha:
            data = None
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                data = subprocess.run(["git", "-C", repo, "cat-file", "blob", sha], capture_output=True, timeout=60,
                                      check=True, env={k: v for k, v in os.environ.items()
                                                       if not k.startswith("GIT_")}).stdout
        _UNITS[sha] = None if data is None else units(data)
    return _UNITS[sha]


_HEADS = {}  # T-3691: commit -> {path: "mode type object"} of its tree, for every values() of this run


def _at_head(repo, commit, path):
    """T-3691: what `git show HEAD:path` prints rides on: path's entry in the commit's tree ('' when absent, '-' with
    no commit); a git that fails gives a value no record has (rerun)."""
    if not commit:
        return "-"
    if commit not in _HEADS:
        try:
            rows = _git(repo, "ls-tree", "-r", "-t", "-z", "--full-tree", commit).split("\0")
            _HEADS[commit] = {name: meta for meta, _, name in (ln.partition("\t") for ln in rows if ln)}
        except (OSError, subprocess.SubprocessError):
            return os.urandom(8).hex()
    return _HEADS[commit].get(path, "")


def values(repo, blobs):
    """T-3593: what a recorded path is worth in a tree: a file its mode and blob ('' when gone), a folder ('x/') the
    names in it, '?x' whether x is a file, a folder or absent, '*' the whole tree's files (Sampo's task state left
    out), GIT_STATE HEAD and the tags. T-3603: 'x.py::QUALNAME' its function's body, 'x.py::' its mode and the rest
    of the file (units()); either the file's blob when it doesn't parse or no def has that name (code compiled from
    a string with the file's name), an annotation function's the rest's."""
    memo, kids = {}, {}

    def value(p):
        if p not in memo:
            if p[:1] == "\0":  # T-3691: git's own state (tree()); a path at HEAD its entry in HEAD's tree
                memo[p] = _at_head(repo, blobs.get("\0HEAD", ""), p[6:]) if p.startswith("\0HEAD:") else blobs.get(p, "")
            elif p == "*":
                memo[p] = hashlib.sha1("".join(f"{f}\0{b}\0" for f, b in sorted(blobs.items())
                                               if not f.startswith(STATE) and f[:1] != "\0").encode("utf-8", "surrogateescape")).hexdigest()
            elif p.endswith("/") or p.startswith("?"):
                if not kids:
                    for f in blobs:
                        parts = f.split("/") if f[:1] != "\0" else []
                        for i, name in enumerate(parts):
                            kids.setdefault("/".join(parts[:i]) + "/" if i else "./", set()).add(name)
                q = p[1:]
                memo[p] = ("/".join(sorted(kids.get(p, ()))) if p.endswith("/") else
                           "file" if q in blobs else "dir" if q + "/" in kids else "")
            elif p in blobs or "::" not in p:
                memo[p] = blobs.get(p, "")
            else:
                rel, _, q = p.rpartition("::")
                b = blobs.get(rel, "")
                u = _units(repo, rel, b) if b else None
                memo[p] = (b if u is None else u[0][q] if q in u[0] else
                           f"{b.partition(' ')[0]} {u[1]}" if not q or q.rsplit(".", 1)[-1] == "__annotate__" else b)
        return memo[p]
    return value


def digest(paths, value):
    """T-3593: one hash of the paths' values (sorted paths), Python's version, --sandbox and the local-models switch."""
    h = hashlib.sha1(f"{sys.version}\0{bool(SANDBOX)}\0{os.environ.get('FOREMAN_NO_LOCAL_MODELS', '1')}".encode())
    for p in paths:
        h.update(f"{p}\0{value(p)}\0".encode("utf-8", "surrogateescape"))  # T-3651: a name with a stray byte
    return h.hexdigest()


def work_time(secs):
    """A split class's work: its tests' seconds less each one's start-up, so it stays split only while it is slow."""
    return round(max(0.0, sum(secs) - START_S * len(secs)), 2)


def plan(todo, parts, whole, times):
    """T-3166: the jobs for these classes, longest first. A class that may be split (not in whole) is, once its work
    took SPLIT_S or more last time (or, never timed, when SEEDED names it): its tests become jobs."""
    jobs = []
    for cid in todo:
        ids = parts.get(cid, [])
        split = cid not in whole and len(ids) > 1 and (times[cid] >= SPLIT_S if cid in times else cid.rsplit(".", 1)[-1] in SEEDED)
        jobs += ids if split else [cid]

    def est(j):
        if j in times:
            return times[j]
        if j in parts:  # a whole class never timed: half a second a test
            return 0.5 * len(parts[j])
        c = j.rsplit(".", 1)[0]  # a test never timed: its class's average
        return times[c] / len(parts[c]) if c in times and c in parts else 0.5
    return sorted(jobs, key=lambda j: -est(j))  # stable: ties keep the biggest-class-first order


def classes(patterns, k, hit=None, names=(), ids=None, parts=None, whole=None):
    """Every test class id (module.Class) discover would run, biggest first so the long ones start early; hit (a set)
    gets each -k pattern or name that matched a test. T-1994: with names and -k both, a test matches one of each.
    ids (a list) gets every test id walked, before any filter. T-3166: parts (a dict) gets each class's matching
    test ids, and whole (a set) the classes that keeps_whole."""
    sys.path.insert(0, HERE)
    counts, skip, hit = {}, skipped(), set() if hit is None else hit

    def walk(suite):
        for t in suite:
            if isinstance(t, unittest.TestSuite):
                walk(t)
                continue
            if skip and (t.id().rsplit(".", 1)[-1] if type(t).__name__ == "_FailedTest"  # a module that won't import
                           else type(t).__module__) in skip:
                continue
            if ids is not None:
                ids.append(t.id())
            if (not k or any(x in t.id() for x in k)) and (not names or any(x in t.id() for x in names)):
                hit.update(x for x in (*k, *names) if x in t.id())
                cid = f"{type(t).__module__}.{type(t).__name__}"
                counts[cid] = counts.get(cid, 0) + 1
                if parts is not None and t.id() not in parts.setdefault(cid, []):  # an imported TestCase is walked twice
                    parts[cid].append(t.id())
                if whole is not None and keeps_whole(t):
                    whole.add(cid)
    for pattern in dict.fromkeys(patterns):
        walk(unittest.TestLoader().discover(HERE, pattern=pattern))  # not the shared one: unittest -k filters it
    return sorted(counts, key=lambda c: -counts[c])


def annotations(cid, err):
    """T-1248: one GitHub ::error line per failing test (its title, the line that failed and the exception); output
    with no FAIL:/ERROR: blocks (an import error) keeps its last four lines."""
    out = []
    for name, block in re.findall(r"(?m)^(?:FAIL|ERROR): (\w+) \([^)]*\)\n-+\n(.*?)(?=\n=+\n|\n-+\nRan |\Z)", err, re.S):
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        code = [x for x in lines if not x.startswith(("Traceback", "File ")) and lines.index(x) < len(lines) - 1]
        body = ([code[-1]] if code else []) + lines[-1:]
        out.append(f"::error title={cid}.{name}::" + "%0A".join(x[:250] for x in body).replace("::", ": :"))
    if not out:  # T-1911: lines cut to 250, and a class that wrote nothing says so (an empty message isn't shown)
        tail = [x.strip()[:120] for x in err.splitlines() if x.strip() and not set(x.strip()) <= set("-=~^")][-4:]
        out.append(f"::error title={cid}::" + ("%0A".join(tail) or "no output").replace("::", ": :"))
    return out


NOT_STARTED = "run.py: not started: the run reached its --deadline (T-1809)\n"
TMP_PREFIX = "fmtest-"  # not fm-: fm tidy reads ~/.claude/projects/-tmp-fm-* as its own claude -p runs' folders


# T-2034: a class dies with run.py, even a SIGKILLed one (a Bash timeout): its own session kept it alive, and one
# orphan spun a core for a day. Like -m unittest after that; off Linux the prctl is a no-op.
BOOT = ("import ctypes, os, signal, sys, unittest\n"
        "try:\n    ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG\nexcept Exception:\n    pass\n"
        "if sys.argv[1] != '0' and os.getppid() != int(sys.argv[1]):\n    os._exit(1)  # run.py was gone before the prctl\n"
        "unittest.main(module=None, argv=['python -m unittest', *sys.argv[2:]])\n")


SANDBOX = []  # T-3222: run.py --sandbox, once its job sandbox is known to work here
COVER_PY = []  # T-3593: the venv python recording each class's reads (cover_python), when there is one


def sandboxed(argv, tmp, env):
    """T-3222: argv and env for a class in Sampo's job sandbox: the repo read-only, only its own temp folder
    writable, no network, credential folders empty, secret variables gone."""
    lib = os.path.join(os.path.dirname(HERE), "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from sampo import sandbox
    repo = os.path.dirname(os.path.dirname(HERE))  # T-3246: readable even under /tmp (sampo ship's scratch tree)
    return (sandbox.argv(argv, tmp, cwd=HERE, nest=True, reads=[repo]),  # nest: tests that run bwrap themselves
            sandbox.job_env(env, tmp))


def run_one(cid, k, timeout=480, deadline=None):
    """T-3252: the class runs in one of the machine's CPU slots; waiting for it counts against neither its timeout nor
    its recorded time, and the run's deadline still applies once it starts."""
    wait = max(0.0, deadline - time.time()) if deadline is not None else 3600.0
    with cpu_slot(wait) as slot:
        return _run_one(cid, k, timeout, deadline, slot)


def _run_one(cid, k, timeout, deadline, slot):
    """T-1808: a class still running after timeout s is killed and fails by name, with every thread's stack
    (faulthandler on SIGABRT); its own session, so a child holding the pipes dies with it. T-1809: the run's deadline
    (an epoch) shortens that, and a class not started by then fails as NOT_STARTED."""
    t0 = time.time()
    why = f"still running after {timeout}s: killed (T-1808)"
    if deadline is not None:
        if deadline - t0 < 1:  # review: round() of under 0.5 s left would start the class and kill it at once
            return cid, 1, NOT_STARTED, 0, 0.0
        if deadline - t0 < timeout:  # review: said apart from the per-class cap
            timeout = round(deadline - t0)
            why = f"still running at the run's deadline ({timeout}s in): killed (T-1809)"
    extra = [a for x in k for a in ("-k", x)]
    try:
        tmp = tempfile.mkdtemp(prefix=f"{TMP_PREFIX}{os.getpid()}-")  # T-2019: whatever the class leaks goes with it
    except OSError as e:  # T-3236: /tmp out of space or inodes fails this class by name, not the whole run
        return cid, 1, f"run.py: couldn't make the class's temp folder: {e}\n", 0, 0.0
    argv, env =[sys.executable, "-X", "faulthandler", "-c", BOOT, "0" if SANDBOX else str(os.getpid()), "-q", cid,
                 *extra], child_env(tmp)
    env[SLOT_ENV] = "none" if slot is None else str(slot)  # T-3442: with no slot too, its runs never queue again
    if COVER_PY:  # T-3593: its processes append the repo files they read here, the venv's python first on PATH
        env["RUNPY_COVER"] = os.path.join(tmp, ".runcover")
        env["PATH"] = os.path.dirname(COVER_PY[0]) + os.pathsep + env.get("PATH", os.defpath)
    if SANDBOX:  # its own pid namespace: it dies with bwrap (--die-with-parent), not by a parent pid check
        argv, env = sandboxed(argv, tmp, env)
    p = subprocess.Popen(argv, cwd=HERE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    try:
        _, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.send_signal(signal.SIGABRT)  # the stack, to stderr
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
        _, err = p.communicate()
        err += f"\nrun.py: {cid} {why}\n"
    finally:
        if COVER_PY:
            with contextlib.suppress(OSError), open(os.path.join(tmp, ".runcover"), encoding="utf-8",
                                                    errors="surrogateescape") as f:
                COVERED[cid] = f.read()
        shutil.rmtree(tmp, ignore_errors=True)
    m = re.search(r"Ran (\d+) tests?", err)
    return cid, p.returncode, err, int(m.group(1)) if m else 0, time.time() - t0  # killed: negative


def sweep(base=None):
    """T-2019: a run killed before its cleanup leaves its classes' temp dirs; the next run removes dead runs' ones."""
    base = base or tempfile.gettempdir()
    for name in os.listdir(base):
        m = re.match(rf"(?:{TMP_PREFIX}|fmfork-)(\d+)-", name)  # T-3572: and a run's fork server's folder
        if not m:
            continue
        try:
            os.kill(int(m.group(1)), 0)
        except ProcessLookupError:
            shutil.rmtree(os.path.join(base, name), ignore_errors=True)
        except PermissionError:
            pass  # alive, someone else's


def slowest(results, n):
    return ", ".join(f"{'.'.join(c.split('.')[1:][-2:])} {s:.0f}s" for c, _, _, _, s in sorted(results, key=lambda r: -r[4])[:n])


def summary_notes(results, elapsed, deadline):
    """T-1809: the run's time and five slowest classes as a public ::notice, a ::warning past 75% of the deadline,
    and one ::error for the classes the deadline left unstarted."""
    notes = [f"::notice title=run.py::Ran {sum(r[3] for r in results)} tests in {elapsed:.0f}s; "
             f"slowest: {slowest(results, 5)}"]
    if deadline and elapsed > 0.75 * deadline:
        notes.append(f"::warning title=run.py::the suite took {elapsed:.0f}s, {100 * elapsed / deadline:.0f}% of its "
                     f"{deadline}s deadline: speed up the slowest classes or raise the job's timeout")
    unstarted = sum(r[2] == NOT_STARTED for r in results)
    if unstarted:
        notes.append(f"::error title=run.py::{unstarted} classes never started: the run reached its {deadline}s deadline")
    broke = [f"{r[0]} (exit {r[1]})" for r in results if r[1] != 0 and r[2] != NOT_STARTED]
    if broke:  # T-1911: one line naming them all, should a class's own annotations go missing
        notes.append(f"::error title=run.py::{len(broke)} classes failed: {', '.join(broke)}"[:1000])
    return notes


@contextlib.contextmanager
def burn(n):
    """T-1730: n busy-loop children for the block's duration (a load flake on demand), killed after; each also stops
    by itself once its parent is gone (a run killed with SIGKILL leaves no spinner behind)."""
    procs, spin = [], f"import os\nwhile os.getppid() == {os.getpid()}: pass"
    try:
        for _ in range(max(0, n)):
            procs.append(subprocess.Popen([sys.executable, "-c", spin], stdin=subprocess.DEVNULL))
        yield procs
    finally:
        for p in procs:
            p.kill()
        for p in procs:
            p.wait()


def load_room(n, cpus=None, load=None, spinners=None):
    """T-3441: how many of --load's n busy loops fit under twice the CPU count, given the 1-minute load and the burn
    spinners already running (counted from /proc: the average lags a run started seconds ago)."""
    cpus = cpus or os.cpu_count() or 4
    if load is None:
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    if spinners is None:
        spinners = 0
        for pid in os.listdir("/proc") if os.path.isdir("/proc") else ():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    spinners += b"while os.getppid() == " in f.read()
            except OSError:
                continue
    return max(0, min(n, int(2 * cpus - max(load, spinners))))


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-j", type=int, default=slot_count() or os.cpu_count() or 4)  # T-3252: the pool
    ap.add_argument("-k", action="append", default=[], help="only tests whose id contains this (repeatable)")
    ap.add_argument("-p", action="append", help="module glob, as unittest discover's -p (repeatable; default "
                                                "test*.py)")  # T-0389: a second -p replaced the first
    ap.add_argument("--load", type=int, default=0, metavar="N",
                    help="run beside N busy-loop processes (T-1730: reproduce a flake that shows only under load)")
    ap.add_argument("--timeout", type=int, default=480, metavar="S",
                    help="kill a test class still running after S seconds and fail it with its stack (T-1808)")
    ap.add_argument("--deadline", type=int, metavar="S",
                    help="end the whole run after S seconds: running classes are killed with their stacks, unstarted "
                         "ones fail (T-1809: set it below a CI job's cap, so the run names what was slow)")
    ap.add_argument("--shard", metavar="K/N",
                    help="run only the Kth of N interleaved slices of the classes (T-1842: CI splits the suite "
                         "across N jobs; biggest-first order keeps the slices even)")
    ap.add_argument("--sandbox", action="store_true", help="run each class in Sampo's job sandbox (T-3222): the repo "
                    "read-only, only its own temp folder writable, no network; plain, with a note, where it can't run")
    ap.add_argument("names", nargs="*", help="test module names (T-0808: fm check affected's {names}); with -k, only "
                                             "their tests that match a -k (T-1994)")
    a = ap.parse_intermixed_args(argv)  # names may sit on either side of -k
    for i, n in enumerate(a.names):  # T-3428: a path into the tests dir names its module
        if "/" in n or n.endswith(".py"):
            d, f = os.path.split(n)
            if not (f.startswith("test") and f.endswith(".py") and (not d or os.path.realpath(d) == os.path.realpath(HERE))):
                ap.error(f"{n}: not a test name or a test_*.py file in {HERE}")
            a.names[i] = f[:-3]
    if a.deadline is not None and a.deadline < 1:  # review: 0 is falsy and turned the deadline off
        ap.error("--deadline takes a number of seconds, 1 or more")
    if a.shard is not None:
        k, _, n = a.shard.partition("/")
        if not (k.isdigit() and n.isdigit() and 1 <= int(k) <= int(n)):
            ap.error("--shard takes K/N with 1 <= K <= N, like 1/2")
        a.shard = int(k), int(n)
    a.error = ap.error
    return a


def main():
    try:  # T-3503: a worker killed with SIGABRT must not leave a core dump; children inherit the limit
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    a = parse()
    ap = a
    if any(re.search(r"\s", k) for k in a.k + a.names):  # T-0357: '-k "a or b"' matched nothing and failed only at the end
        ap.error("-k matches a substring of test ids; repeat -k for each pattern (-k test_guard -k test_cli)")
    if a.sandbox:
        lib = os.path.join(os.path.dirname(HERE), "lib")
        sys.path.insert(0, lib)
        from sampo import sandbox
        ok, why = sandbox.available()
        if ok:
            SANDBOX.append(why)
        else:
            sys.stderr.write(f"run.py: --sandbox: not available here ({why}); running plain\n")
    t0 = time.time()
    sweep()
    hit, ids = set(), []
    parts, whole = {}, set()
    todo = classes(a.p or ["test*.py"], a.k, hit, a.names, ids, parts, whole)
    missing = [n for n in a.names if n not in hit]
    if missing:  # T-1287: a name that matches nothing (a test file that doesn't exist) fails, not passes unseen
        fold = lambda s: s.lower().replace("_", "")  # noqa: E731
        near = dict.fromkeys(seg for i in ids if any(n in i for n in missing) for seg in i.split(".")
                             if any(fold(x) in fold(seg) for x in a.k))  # T-2003: -k core_integrity meant CoreIntegrity
        sys.stderr.write(f"run.py: no test matches {', '.join(missing)}"  # T-1999: say what filtered it out
                         + (f" with -k {' -k '.join(a.k)}" if a.k else "")
                         + (f"; did you mean {' or '.join('-k ' + s for s in list(near)[:3])}?" if near else "") + "\n")
        return 1
    if a.shard:
        todo = todo[a.shard[0] - 1::a.shard[1]]
    # T-3502: a run naming modules skips the classes green at the same code, but 1 in 10 drawn; every run records
    # them. Not with -k (part of a class), switched off, or nested in a class's slot (a test's own run.py).
    # T-3593: "the same code" is what the class read when it passed (its coverage), so a change elsewhere skips it
    # T-3638: the post-merge verify alone sets SAMPO_CACHE_FULL=1 to let a run naming no modules skip too; taken out
    # of the environment here so no class or child run inherits it (ship and a plain full run never skip)
    incr = (_env(CACHE_FULL) or "0") != "0"
    for k in (CACHE_FULL, "FOREMAN_" + CACHE_FULL[len("SAMPO_"):]):
        os.environ.pop(k, None)
    skip, drawn, got = {}, set(), None
    if not (a.k or (_env(NO_CACHE) or "0") != "0" or _env(SLOT_ENV)):
        got = tree()
    if got:
        cache, value = cache_path(got[2]), values(*got[:2])
        COVERED.clear()
        gen, recs = load_cache(cache)
        hits = {c: r[1] for c in todo for r in recs.get(c, ())[::-1] if r[0] == digest(r[2], value)} if a.names or incr else {}
        drawn = {c for c in hits if random.random() < SAMPLE}
        skip = {c: n for c, n in hits.items() if c not in drawn}
        todo = [c for c in todo if c not in skip]
        if skip:
            sys.stderr.write(f"run.py: skipped {len(skip)} classes green at the same code; "
                             "FOREMAN_NO_TEST_CACHE=1 runs them\n")
        if not SANDBOX and (py := cover_python()):
            COVER_PY.append(py)
            sys.executable = py  # every Python run.py starts records, a fork server it starts (T-3572) too
            os.environ["RUNPY_COVER_ROOTS"] = os.pathsep.join(_roots(got[0]))  # what runcover.py records
    owner = lambda j: j if j in parts else j.rsplit(".", 1)[0]  # noqa: E731  (a split class's job is one test)
    times = {} if a.shard else load_times()  # a shard's slice must not depend on this machine's last run
    todo = plan(todo, parts, whole, times)  # T-3166: jobs, a big class's tests apart
    if a.load and (room := load_room(a.load)) < a.load:
        sys.stderr.write(f"run.py: --load {a.load} cut to {room}: the host already runs near twice its CPU count\n")
        a.load = room
    try:  # T-3572: one fork server answers every class's fm calls
        import forkcli
    except ImportError:  # a copy of run.py alone (test_check_paths) has no forkcli
        forkcli = None
    if forkcli and SANDBOX:  # a sandboxed class can't reach a server outside its sandbox
        os.environ.pop(forkcli.SHARED, None)
    elif forkcli:
        forkcli.share()  # stopped and waited for at exit, so the run's rusage counts its forked calls
    try:
        import helpers  # T-3338: lazily, after classes() put the tests folder on sys.path
        busy = helpers.busy_factor()
    except ImportError:  # a copy of run.py alone (test_check_paths) has no helpers
        busy = 1
    cap = round(a.timeout * busy)  # a loaded host's class runs slower; the deadline still clamps
    # T-3338 review: --deadline, else the gate's (core.run_command sets FM_RUN_DEADLINE under its own timeout), else
    # 8 class timers: the scaled timers never outlast the caller's, so a kill names the class
    dl = a.deadline if a.deadline is not None else int(os.environ.get("FM_RUN_DEADLINE") or 0) or max(8 * cap, 600)
    a.deadline, end = dl, t0 + dl
    # T-3541: the whole suite (the gate: no names, -k or -p) waits for any other on the machine, at most half its deadline
    slot = suite_slot(t0 + dl / 2) if not (a.names or a.k or a.p) else contextlib.nullcontext()
    with slot, burn(a.load), concurrent.futures.ThreadPoolExecutor(max(1, a.j)) as ex:
        go = lambda c: run_one(c, a.k or a.names, cap, end)  # noqa: E731  # T-1994: the class is in a named module, so -k alone
        results = list(ex.map(go, todo))
        bad = [r[0] for r in results if r[1] != 0 and owner(r[0]) in drawn  # a deadline's stop says nothing of it
               and r[2] != NOT_STARTED and "the run's deadline" not in r[2]]
        if bad:  # T-3502: green in the cache at this code, red now: the cache is wrong, so nothing it said holds
            drop_cache(cache)
            sys.stderr.write(f"run.py: {bad[0]} failed though the cache had it green at the same code: the cache is "
                             f"wrong, dropped" + (f"; running the {len(skip)} classes it skipped\n" if skip else "\n"))
            more = plan(list(skip), parts, whole, times)
            results += list(ex.map(go, more))
            todo, skip, got = todo + more, {}, None
    if got:  # T-3502: each class that ran whole and green, none skipped, recorded with what it read (T-3593)
        size, ran, cov = {}, {}, {}
        for t in set(ids):
            size[t.rsplit(".", 1)[0]] = size.get(t.rsplit(".", 1)[0], 0) + 1
        for job, code, err, n, _ in results:  # a skip (skipUnless a browser) passes here, maybe not elsewhere
            ok, m = ran.get(owner(job), (True, 0))
            ran[owner(job)] = ok and code == 0 and not re.search(r"\bskipped=\d", err), m + n
            cov.setdefault(owner(job), []).append(COVERED.get(job))  # None: a job nothing recorded for
        new = {c: n for c, (ok, n) in ran.items()
               if ok and None not in cov[c] and n == size.get(c) == len(parts.get(c, ()))}
        again = tree() if new else None  # a file edited while the run ran: its class ran the old code or the new
        if again and again[0] == got[0]:
            base, after = shared(*got[:2]), values(*again[:2])
            paths = {c: sorted(base.union(*(covered(t, *got[:2]) for t in cov[c]))) for c in new}
            new = {c: [digest(paths[c], value), n, paths[c]] for c, n in new.items()}
            new = {c: r for c, r in new.items() if digest(r[2], after) == r[0]}
            if new:
                save_cache(cache, new, gen)
    if not a.shard:
        seen = set(ids)
        # T-3338: a killed class (negative rc) counts as slow, so the next run splits it instead of killing it again
        timer = lambda r: r[1] < 0 and "killed (T-1808)" in r[2]  # noqa: E731  # not a deadline kill or a SIGKILL
        new = {r[0]: max(r[4], SPLIT_S) if timer(r) else r[4]
               for r in results if (r[1] >= 0 or timer(r)) and r[2] != NOT_STARTED and r[0] in seen | set(parts)}
        for cid, js in parts.items():  # a split class's work, so the next run still knows if it is slow
            if js and all(j in new for j in js):
                new[cid] = work_time([new[j] for j in js])
        if a.k:  # a -k run ran part of each class: its time is no class's
            new = {j: s for j, s in new.items() if j not in parts}
        if new:
            save_times(new)
    failed = [r for r in results if r[1] != 0]
    ci = os.environ.get("GITHUB_ACTIONS")  # T-1223: a run's log needs a login; its annotations are public
    for cid, _, err, _, _ in failed:
        if err == NOT_STARTED:
            continue  # one line for all of them, below
        sys.stderr.write(f"\n==== {cid}\n{err}")
        if ci:
            print("\n".join(annotations(cid, err)), flush=True)
    late = [r[0] for r in failed if r[2] == NOT_STARTED]
    if late:  # review: outside CI too, which classes the deadline left out
        sys.stderr.write(f"\nrun.py: not started (the run's {a.deadline}s deadline): {', '.join(late)}\n")
    if ci:
        print("\n".join(summary_notes(results, time.time() - t0, a.deadline)), flush=True)
    total = sum(r[3] for r in results)
    cached = f"; {sum(skip.values())} more green in the cache" if skip else ""  # T-3502: core._SOME_TESTS reads it
    slow = slowest(results, 3)
    if skipped():
        sys.stderr.write(f"\nleft out (FOREMAN_SKIP_TESTS): {', '.join(sorted(skipped()))}")
    names = [r[0] for r in failed]  # T-2319: the last line says which classes, since tails cut the ==== blocks above
    said = ", ".join(names[:5]) + (f", +{len(names) - 5} more" if len(names) > 5 else "")
    sys.stderr.write("".join(f"\n  failed class: {n}" for n in names) + ("\n" if names else ""))  # T-3397: all of them, one per line
    sys.stderr.write(f"\nRan {total} tests in {time.time() - t0:.1f}s ({len(todo)} jobs, {a.j} at a time; "
                     f"slowest: {slow}){cached}\n\n"
                     f"{f'FAILED ({len(failed)} classes: {said})' if failed else 'OK' if total or skip else 'NO TESTS RAN'}\n")
    # T-2944: stopped at its own deadline with nothing really failed: exit 124 and say so last, so a caller (the
    # sentinel's re-verify) reads a busy machine, not a regression
    stopped = [r for r in failed if r[2] == NOT_STARTED or "the run's deadline" in r[2]]
    if stopped and len(stopped) == len(failed):
        sys.stderr.write("run.py: stopped at its deadline\n")
        return 124
    return 1 if failed or not (total or skip) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # T-1911: a crash after the notes was a bare exit 1 in CI
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=run.py::run.py crashed: {e!r}"[:1000], flush=True)
        raise
