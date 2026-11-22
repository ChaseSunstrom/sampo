"""T-1607: confine an fm code worker's filesystem writes with Landlock (Linux 5.13+), through ctypes, stdlib only.

The worker's whole process tree may read everything but write only beneath its writable set: the run's tree, its own git
dir, the shared objects, the folder of its own lane ref (refs/heads/fmw/<lane>/ and its reflog: HEAD points there for the
turn, T-5909; never trunk, another lane's ref, tags, packed-refs, hooks or config), scratch, a few devices, Claude Code's
working folders (never its settings, hooks, plugins or Sampo's code), Sampo's state as an allow-list (the guard's
events.jsonl and spend.jsonl, and a folder of its own for its nested session's files, worker-sessions/<lane>/: never
PANIC, the pause files, trust, sessions/ (budget's usage snapshots and bus's by-pid map are read as truth), the turn's
lane record or the projects) and the common tool caches.
The worker can write its lane folder, so the parent never trusts what it finds there (T-5909): the lane ref is read only
through one O_NOFOLLOW, O_NONBLOCK open as a plain file holding one commit id (never followed as a symref or link, never
re-resolved), and its whole folder removed after the turn by unlinking (never opened: git on a FIFO there would block); a
record of {lane, real, tip} in Sampo's state (which the worker cannot write) puts the real branch back when a turn died
before its judge. Because the tip is the worker's to choose, a confined turn's other refs are only reported, never undone.
ponytail: the shared objects/ stay writable, so a worker can still plant an object or pack there (T-5924 narrows it).
The ruleset is built in the parent (prepare); the child only locks itself (enter), so nothing is loaded after fork.
Inherited by everything the worker starts and impossible to undo from inside. Optional: on another OS, an older
kernel or with the LSM off, available() says why and nothing changes.
ponytail: enter() guards against a worker writing where it shouldn't by mistake, not against a hostile one: its reads,
the network and AF_UNIX sockets in /tmp (tmux, X11, systemd) aren't restricted. prepare_jail with syscall_filter is the
hostile-child jail (T-3160): reads and writes only where named, no socket, no signal or ptrace beyond its own domain."""
import ctypes
import hashlib
import os
import platform
import json
import re
import shutil
import stat
import struct
import subprocess

_NR = {"x86_64": (444, 445, 446), "aarch64": (444, 445, 446), "riscv64": (444, 445, 446)}  # create, add_rule, restrict
_PR_SET_NO_NEW_PRIVS = 38
_RULE_PATH_BENEATH = 1
_A = {n: 1 << i for i, n in enumerate(["EXECUTE", "WRITE_FILE", "READ_FILE", "READ_DIR", "REMOVE_DIR", "REMOVE_FILE",
                                       "MAKE_CHAR", "MAKE_DIR", "MAKE_REG", "MAKE_SOCK", "MAKE_FIFO", "MAKE_BLOCK",
                                       "MAKE_SYM", "REFER", "TRUNCATE"])}
_WRITES = ("WRITE_FILE", "REMOVE_DIR", "REMOVE_FILE", "MAKE_CHAR", "MAKE_DIR", "MAKE_REG", "MAKE_SOCK", "MAKE_FIFO",
           "MAKE_BLOCK", "MAKE_SYM")
# what a claude -p run was measured writing (T-1607): its working folders and files, never settings.json, hooks,
# plugins, rules, agents, skills, CLAUDE.md or Foreman's own checkout. The login file is named in parts so no scanner
# mistakes this list for a write to it.
_CLAUDE_WRITABLE = ("projects", "session-env", "sessions", "backups", "security", "shell-snapshots", "todos", "statsig",
                    "file-history", "paste-cache", "cache", "debug", "logs", "plans", "ide", "history.jsonl",
                    "." + "credentials" + ".json", "rate-limits.json")
_HOME_WRITABLE = (".claude.json", ".cache", ".npm", ".cargo/registry", ".cargo/git", ".cargo/.package-cache",
                  ".rustup/tmp", ".gradle", ".m2/repository", "go/pkg", ".local/share/pnpm", ".bun/install/cache")
_DEVICES = ("/dev/null", "/dev/zero", "/dev/full", "/dev/tty", "/dev/pts", "/dev/shm")


def _libc():
    lib = ctypes.CDLL(None, use_errno=True)
    lib.syscall.restype = ctypes.c_long
    return lib


try:  # loaded once at import: a forked child never loads anything
    _LIBC = _libc()
except OSError:
    _LIBC = None


def abi():
    """The kernel's Landlock ABI version, 0 when there is none (not Linux, an unknown CPU, disabled, too old)."""
    nr = _NR.get(platform.machine()) if os.uname().sysname == "Linux" else None
    if not nr or _LIBC is None:
        return 0
    try:
        v = _LIBC.syscall(nr[0], None, ctypes.c_size_t(0), ctypes.c_uint32(1))  # LANDLOCK_CREATE_RULESET_VERSION
    except (OSError, AttributeError):
        return 0
    return max(int(v), 0)


def available():
    """(True, 'ABI n') or (False, why), never raising."""
    if os.uname().sysname != "Linux":
        return False, f"Landlock is Linux only (this is {os.uname().sysname})"
    if platform.machine() not in _NR:
        return False, f"no Landlock syscall numbers known for {platform.machine()}"
    v = abi()
    return (True, f"ABI {v}") if v else (False, "the kernel has no Landlock (older than 5.13, or the LSM is off)")


def _writes(v):
    bits = 0
    for n in _WRITES:
        bits |= _A[n]
    return bits | (_A["REFER"] if v >= 2 else 0) | (_A["TRUNCATE"] if v >= 3 else 0)


def _git(root, flag):
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--path-format=absolute", flag], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


# T-5909: what a confined worker never writes in Sampo's state folder, so it is granted none of it: consent and trust, the
# profile and settings, the models, the guard's own script cache (the guard trusts it: a forged entry waves a script
# through) and what the plugin installs from. Every other child of the state folder is granted on its own: a Landlock
# rule on the folder itself would grant every file beneath it, and a new top-level file or an atomic replace of a
# granted one is refused (the rule names the file, not its folder).
STATE_FILES = ("events.jsonl", "spend.jsonl")  # appended to, in place
STATE_DIRS = ()  # none shared: sessions/ held files other code trusts (budget's usage snapshots, bus's by-pid map);
# the worker's own session folder (worker_sessions) is granted on its own, under worker-sessions/, which nobody reads


def state_grants(state):
    """T-5909: the Sampo state a worker may write, an allow-list and nothing broader: the two logs the guard's hooks
    append to, each file on its own. Measured: a quiet worker session's hooks (all eleven events) exit 0 with only these.
    Every other child (PANIC and the pause files, trust, profile, settings, the guard's script cache, worlds.json, locks,
    cache, sessions/ (budget reads its snapshots as usage, bus reads sessions/by-pid as who a process is: a forged file
    there is trusted), the turn's lane record, the projects' ledgers and task files) is never granted: it is Sampo's, and
    the worker's verbs run in the parent. A rule on the state folder itself would grant every file beneath it. (The
    worker's own session folder is granted by writable_for, worker_sessions.)"""
    if not os.path.isdir(state):
        return []
    for n in STATE_FILES:  # a rule needs the path to exist: a fresh state folder gets the empty log, not a wider rule
        try:
            open(os.path.join(state, n), "a").close()
        except OSError:
            pass
    for n in STATE_DIRS:
        try:
            os.makedirs(os.path.join(state, n), exist_ok=True)
        except OSError:
            pass
    return [os.path.join(state, n) for n in STATE_FILES + STATE_DIRS if os.path.lexists(os.path.join(state, n))]


WORKER_SESSIONS_ENV = "FOREMAN_WORKER_SESSIONS"  # the worker's session files folder, for the nested session's hooks
_SESSIONS_KEEP = 3 * 86400  # a worker's session file older than this is gone when its folder is next granted


def worker_sessions(root):
    """T-5909: the folder a confined worker's nested session keeps its own session files in (its taint store: the guard
    flags a Bash command built from fetched text), <state>/worker-sessions/<lane id>/. The worker writes it whole, so
    it is never a place the lead trusts: budget's usage snapshots and bus's by-pid map read sessions/, not this."""
    from sampo import core
    return os.path.join(os.path.realpath(core.state_dir()), "worker-sessions", _lane_id(root))


def _worker_sessions_grant(root, state):
    """The worker's session folder, made (and its old files pruned) so a rule can name it; [] when it can't be made."""
    path = worker_sessions(root)
    if not os.path.isdir(state) or not path.startswith(os.path.join(state, "worker-sessions") + os.sep):
        return []  # no state folder: nothing is made for it
    try:
        os.makedirs(path, exist_ok=True)
        import time
        for n in os.listdir(path):
            f = os.path.join(path, n)
            if os.path.isfile(f) and not os.path.islink(f) and time.time() - os.lstat(f).st_mtime > _SESSIONS_KEEP:
                os.unlink(f)
    except OSError:
        pass
    return [path] if os.path.isdir(path) and not os.path.islink(path) else []


def _branch(root):
    """The branch HEAD names in root ('' when detached or unborn)."""
    try:
        r = subprocess.run(["git", "-C", root, "symbolic-ref", "-q", "--no-recurse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


LANE = "fmw"  # T-5909: the folder of refs/heads a confined worker commits in, one subfolder per worktree


def _lane_id(root):
    """A ref-safe name for a worktree: its folder name (cleaned) and a hash of its real path (two lanes never share)."""
    real = os.path.realpath(root)
    clean = re.sub(r"[^A-Za-z0-9_-]", "-", os.path.basename(real))[:40].strip("-") or "lane"
    return f"{clean}-{hashlib.sha1(real.encode('utf-8', 'surrogateescape')).hexdigest()[:8]}"


def lane_ref(root):
    """The ref a confined worker commits on in root's worktree: refs/heads/fmw/<lane>/tip, in a folder of its own."""
    return f"refs/heads/{LANE}/{_lane_id(root)}/tip"


def _lane_folder(branch):
    """The lane folder name (fmw/<lane>) a branch named fmw/<lane>/<anything> lives in, '' for any other branch: the worker
    may write every ref in its folder, so none of them is ever taken for a real branch."""
    parts = branch.split("/") if isinstance(branch, str) else []
    return "/".join(parts[:2]) if len(parts) >= 3 and parts[0] == LANE and parts[1] else ""


def _own_lane(root, ref):
    """True for a full ref name inside this worktree's own lane folder (the lane ref, or any other a worker made there)."""
    return isinstance(ref, str) and ref.startswith("refs/heads/") and \
        _lane_folder(ref[len("refs/heads/"):]) == f"{LANE}/{_lane_id(root)}"


def ref_grants(common, branch):
    """T-5909: the shared git folders a commit on branch needs, each on its own: the objects, and, only for a worker's
    own lane ref (fmw/<lane>/tip), that ref's folder and its reflog's (git writes <ref>.lock beside the ref and renames
    it: a rule on the file itself cannot allow that, measured). Any other branch gets no ref folder at all: a folder
    rule covers every ref beneath it, and refs/heads holds trunk and the other lanes. Never refs/tags, refs/remotes,
    refs/stash, packed-refs or the other logs: the kernel refuses a write there.
    ponytail: objects/ stays writable (T-5924: a private object folder with alternates takes it back)."""
    out = [os.path.join(common, "objects")]
    if sub := _lane_folder(branch):
        out += [os.path.join(common, "refs", "heads", sub), os.path.join(common, "logs", "refs", "heads", sub)]
    return out


def _txt(x):
    """str for a git answer that came as bytes (the judge's own _head holds bytes); '' for nothing."""
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")


def lane_begin(root, head):
    """T-5909: before a confined worker's turn, point root's worktree HEAD at a new ref in a folder of its own
    (lane_ref), at the tip of the branch it was on, so the worker's commits land there and the kernel set needs no
    shared ref folder. head is (HEAD's symbolic ref, its commit). Returns {"lane", "real", "tip"}, or None when there is
    nothing to swap (a detached or unborn HEAD, a plain checkout whose refs sit inside the tree, a git failure): the
    worker then has no ref folder of its own. In a linked worktree a commit then fails closed (the kernel set holds no
    ref folder); in a plain checkout the refs are inside the writable tree, so the kernel guards nothing there and the
    judge's refs_undo backstop is the only guard."""
    real, tip = _txt(head[0]), _txt(head[1])
    common = _git(root, "--git-common-dir")
    if not (real and tip and real.startswith("refs/heads/") and common and refs_guarded(root)):
        return None
    if _lane_folder(real[len("refs/heads/"):]):
        return None  # HEAD is on a lane ref: lane_recover (before the snapshot) puts the branch back first
    lane = lane_ref(root)
    folders = _lane_folders(common, lane)
    try:
        for f in folders:  # whatever a past turn left there is not trusted: gone before the ref is made
            _wipe(f)
            os.makedirs(f, exist_ok=True)
        if not _record(root, {"lane": lane, "real": real, "tip": tip}):
            return None
        # --no-deref and the empty old value: made only while nothing (a planted symref or file) is there
        if _jgit(root, "update-ref", "--no-deref", lane, tip, "").returncode:
            _record(root, None)
            return None
        if _jgit(root, "symbolic-ref", "HEAD", lane).returncode:
            _jgit(root, "update-ref", "--no-deref", "-d", lane, tip)
            _record(root, None)
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {"lane": lane, "real": real, "tip": tip}


def _lane_folders(common, lane):
    """The ref folder and the reflog folder of a lane ref (refs/heads/fmw/<id>/tip): the only folders a worker may write."""
    sub = lane[len("refs/heads/"):].rsplit("/", 1)[0]
    return [os.path.join(common, top, "heads", *sub.split("/")) for top in ("refs", "logs/refs")]


def refs_guarded(root):
    """True when a confined worker in root is kernel-refused every shared ref (a linked worktree: its refs are in a folder
    its grants leave out); False for a plain checkout, whose refs sit inside the writable tree."""
    gd, common = _git(root, "--git-dir"), _git(root, "--git-common-dir")
    return bool(gd and common and gd != common)


def _wipe(path):
    """Remove a folder a worker could write in, whatever it put there: a link is unlinked, never followed."""
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _record(root, rec):
    """The {lane, real, tip} of a turn in flight, kept in Sampo's state folder (a confined worker holds no grant there, so
    it cannot forge it), so a turn that died before the judge can still be put back. rec None drops it. False when it
    could not be kept (then no lane is made: the worker's commit fails closed)."""
    from sampo import core
    path = os.path.join(core.state_dir(), "lanes", _lane_id(root) + ".json")
    try:
        if rec is None:
            os.unlink(path)
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, path)
        return True
    except FileNotFoundError:
        return rec is None
    except OSError:
        return False


def _recorded(root):
    """The record of root's lane in flight, or None when there is none or it is not a branch and a commit."""
    from sampo import core
    try:
        with open(os.path.join(core.state_dir(), "lanes", _lane_id(root) + ".json")) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    ok = (isinstance(rec, dict) and all(isinstance(rec.get(k), str) for k in ("lane", "real", "tip"))
          and rec["lane"] == lane_ref(root) and rec["real"].startswith("refs/heads/")
          and not _lane_folder(rec["real"][len("refs/heads/"):]) and re.fullmatch(r"[0-9a-f]{40,64}", rec["tip"]))
    return rec if ok else None


def _lane_tip(root, common, lane):
    """The commit a worker left on its lane ref, or '' when the ref is not a plain file holding one object id of a commit:
    the worker can write that folder, so a planted symref, link or odd file is never followed. One open decides it all: no
    link is followed (O_NOFOLLOW), a FIFO never blocks it (O_NONBLOCK), and what it holds is judged on the open fd (fstat:
    a plain file, at most 100 bytes) and read from that fd, so a swap after the look changes nothing."""
    path = os.path.join(common, "refs", "heads", *lane[len("refs/heads/"):].split("/"))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 100:
            return ""
        oid = os.read(fd, 101)
        if len(oid) > 100:
            return ""  # it grew after the fstat: not one commit id
        oid = oid.decode("ascii", "replace").strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
            return ""
        return oid if _jgit(root, "cat-file", "-e", oid + "^{commit}").returncode == 0 else ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""
    finally:
        os.close(fd)


def lane_end(root, lane):
    """T-5909: after the turn, in the unconfined parent: the worker's tip on its lane ref ('' when it holds none, or the
    ref is not a plain object id: it is untrusted, never followed), the lane's whole folder and reflog folder removed
    (unlinked, never opened: git resolving a FIFO the worker left there would block for good), then HEAD back on the real
    branch when it still names a ref of the lane. The record is dropped once HEAD is back, so a restore that failed is
    tried again by lane_recover. The real branch is never moved here: the judge puts a worker's commit back as staged
    changes anyway."""
    if not lane:
        return ""
    tip = ""
    try:
        common = _git(root, "--git-common-dir")
        if common and lane["lane"] == lane_ref(root):
            tip = _lane_tip(root, common, lane["lane"])
            cur = _jgit(root, "symbolic-ref", "-q", "--no-recurse", "HEAD")  # a planted symref there is not followed
            on_lane = cur.returncode == 0 and _own_lane(root, cur.stdout.strip())
            for f in _lane_folders(common, lane["lane"]):  # before any git call resolves HEAD through the lane ref
                _wipe(f)
            back = True
            if on_lane:
                back = lane["real"].startswith("refs/heads/") and _jgit(root, "symbolic-ref", "HEAD", lane["real"]).returncode == 0
            if back:
                _record(root, None)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return tip


def lane_recover(root):
    """T-5909: before a turn's snapshot: when HEAD still names this worktree's lane ref, or any ref in its lane folder (the
    worker may write them all; a turn died before its judge), and
    Sampo's record says which branch it was on, put it back (lane_end) so the snapshot sees the real branch. The lane's
    commits are not kept as a branch: their changes stay staged. Returns the record, or None when there was nothing."""
    try:
        cur = _jgit(root, "symbolic-ref", "-q", "--no-recurse", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cur.returncode or not _own_lane(root, cur.stdout.strip()):  # the lane ref or any other ref in the lane's folder
        return None
    rec = _recorded(root)
    if rec:
        lane_end(root, rec)
    return rec


def _jgit(root, *a):
    """The judge's own git: no fsmonitor or hook runs, whatever config a worker planted."""
    return subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", root, *a],
                          capture_output=True, text=True, timeout=60)


def refs_snapshot(root):
    """T-5909: {ref: object id} for every direct ref of root's repository, shared ones included; None when git can't
    say (then nothing is judged). A symbolic ref (refs/remotes/origin/HEAD) is left out: it holds no object of its own."""
    try:
        r = _jgit(root, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split("\0")
        if len(parts) == 3 and parts[0] and parts[1] and not parts[2]:
            out[parts[0]] = parts[1]
    return out


def _is_ancestor(root, a, b):
    try:
        return _jgit(root, "merge-base", "--is-ancestor", a, b).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _trunks(root):
    """The branches that are trunk: what the main checkout has out, and main and master."""
    out = {"refs/heads/main", "refs/heads/master"}
    try:
        r = _jgit(root, "worktree", "list", "--porcelain")
    except (OSError, subprocess.TimeoutExpired):
        return out
    first = r.stdout.split("\n\n", 1)[0] if r.returncode == 0 else ""
    out |= {ln[len("branch "):] for ln in first.splitlines() if ln.startswith("branch ")}
    return out


def refs_undo(root, before, tip_before, tip_now, own="", switched="", confined=False):
    """T-5909 backstop (the kernel set is the guard: a confined worker holds no shared ref folder, so this finds what an
    unconfined one did): ([(ref, what was done)] undone, [(ref, what was seen)] left) after a worker's turn. confined (the
    turn ran in a lane, T-5909): nothing is undone, only reported: the worker could write the lane ref, so tip_now is
    its to choose (a commit built on trunk's newer tip would make trunk's own value 'its' and roll trunk back), and the
    kernel already refused it every shared ref. A ref other than
    `own` (the branch HEAD named at the start, and the lane ref) or `switched` (where the worker left HEAD, only when
    it made that branch this turn and it is not trunk) is its own. Another ref that moved or appeared and now holds one of the worker's own new commits (reachable
    from its tip now, not from its tip before) is put back with a compare-and-swap (only while it still holds that
    value) or removed. Any other change is only reported: another lane or the train moves refs legitimately in the same
    minutes (a fast-forward of trunk, a lane branch gone after its merge), and a blind restore would undo that.
    ponytail: a hostile worker that builds a commit outside its own history (commit-tree on trunk) and points a ref at
    it is only reported here; the land's moved-trunk check and the kernel's narrowed set (ref_grants) are the rest."""
    now = refs_snapshot(root)
    if before is None or now is None:
        return [], []
    tip_before, tip_now = _txt(tip_before), _txt(tip_now)
    own = {_txt(o) for o in (own if isinstance(own, (tuple, list, set)) else (own,))} - {""}
    switched = _txt(switched)
    if switched in before or switched in _trunks(root):
        switched = ""  # only a branch the worker made this turn is its own, never one that was there or trunk
    undone, seen = [], []
    for ref in sorted(set(before) | set(now)):
        old, new = before.get(ref), now.get(ref)
        if old == new or ref in own or ref == switched:
            continue
        mine = not confined and bool(new and tip_now and _is_ancestor(root, new, tip_now) and not (
            tip_before and _is_ancestor(root, new, tip_before)))
        what = "created" if old is None else "deleted" if new is None else "moved"
        if not mine:
            seen.append((ref, what))
            continue
        try:
            r = _jgit(root, "update-ref", "-d", ref, new) if old is None else _jgit(root, "update-ref", ref, old, new)
        except (OSError, subprocess.TimeoutExpired):
            seen.append((ref, what + ", could not be put back"))
            continue
        (undone if r.returncode == 0 else seen).append((ref, what if r.returncode == 0 else what + ", could not be put back"))
    return undone, seen


class Refused(OSError):
    """The worker can't be confined in this tree without covering what it must not write."""


def writable_for(root):
    """The writable set for a worker in root (only paths that exist; a missing one has nothing to allow). Raises Refused
    when the tree holds Sampo's state folder (a plain checkout of a self-hosting repo): the grant on the tree would cover
    the state and void the narrowing."""
    from sampo import core
    home, state = os.path.expanduser("~"), os.path.realpath(core.state_dir())
    tree = os.path.realpath(root)
    if state == tree or state.startswith(tree + os.sep):
        raise Refused(f"this tree holds Sampo's state folder ({state}): a write grant on the tree would cover it; run the "
                      "worker from a git worktree of the repository instead")
    out = [tree, "/tmp", "/var/tmp", *_DEVICES]
    git_dir, common = _git(root, "--git-dir"), _git(root, "--git-common-dir")
    if git_dir:
        out.append(git_dir)  # a worktree's own HEAD and index; a plain checkout's .git is inside the tree anyway
    if common and common != git_dir:  # shared with the main checkout: its objects and its own branch's folders only
        out += ref_grants(common, _branch(root))
    out += [os.path.join(home, ".claude", x) for x in _CLAUDE_WRITABLE]
    out += [os.path.join(home, x) for x in _HOME_WRITABLE]
    out += state_grants(state)
    out += _worker_sessions_grant(root, state)
    for var, sub in (("TMPDIR", ""), ("XDG_CACHE_HOME", ""), ("CARGO_HOME", "registry"), ("GOPATH", "pkg")):
        if os.environ.get(var):
            out.append(os.path.join(os.environ[var], sub) if sub else os.environ[var])
    seen = []
    for p in out:
        if os.path.lexists(p) and os.path.realpath(p) not in seen:
            seen.append(os.path.realpath(p))
    return seen


def _ruleset(attr, rules):
    """A ruleset fd from a packed landlock_ruleset_attr and (path, rights on a dir, rights on a file) rules."""
    nr = _NR.get(platform.machine())
    rs = _LIBC.syscall(nr[0], ctypes.c_char_p(attr), ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    if rs < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    try:
        for p, on_dir, on_file in rules:
            try:
                fd = os.open(p, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # gone since the set was made: nothing to allow
            try:
                allowed = on_dir if stat.S_ISDIR(os.fstat(fd).st_mode) else on_file  # a file rule: file rights only
                rule = struct.pack("=Qi", allowed, fd)  # struct landlock_path_beneath_attr is packed
                if _LIBC.syscall(nr[1], ctypes.c_int(rs), ctypes.c_int(_RULE_PATH_BENEATH), ctypes.c_char_p(rule),
                                 ctypes.c_uint32(0)) < 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {p}")
            finally:
                os.close(fd)
    except BaseException:
        os.close(rs)
        raise
    return rs


def prepare(paths):
    """In the parent: a Landlock ruleset fd allowing writes only beneath paths (close it after starting the child).
    Raises OSError when it can't, so a caller never starts a worker it believes confined without confinement."""
    nr, v = _NR.get(platform.machine()), abi()
    if not nr or v < 1:
        raise OSError("Landlock isn't available")
    handled = _writes(v)
    return _ruleset(struct.pack("Q", handled), [(p, handled, handled & (_A["WRITE_FILE"] | _A["TRUNCATE"]))
                                                for p in paths])


_READS = _A["EXECUTE"] | _A["READ_FILE"] | _A["READ_DIR"]
NET_ABI, SCOPE_ABI = 4, 6  # TCP bind/connect rules; abstract unix sockets and signals scoped to the domain


def prepare_jail(reads, writes):
    """T-3160: a ruleset for a hostile child: it reads and runs only beneath reads, writes only beneath writes, and from
    ABI 4 binds and connects no TCP port; from ABI 6 its abstract unix sockets and signals stay inside. Raises OSError
    when Landlock isn't there (the caller then refuses to run anything)."""
    nr, v = _NR.get(platform.machine()), abi()
    if not nr or v < 1:
        raise OSError("Landlock isn't available")
    handled = _writes(v) | _READS
    attr = struct.pack("Q", handled) + (struct.pack("Q", 3) if v >= NET_ABI else b"") + \
        (struct.pack("Q", 3) if v >= SCOPE_ABI else b"")  # no net rule follows: every TCP bind and connect is refused
    files = _A["EXECUTE"] | _A["READ_FILE"]
    wfiles = (files | _A["WRITE_FILE"] | _A["TRUNCATE"]) & handled
    return _ruleset(attr, [(p, _READS, files) for p in reads] + [(p, handled, wfiles) for p in writes])


# T-3160: seccomp refuses with EPERM: socket(2) and io_uring_setup(2) (its ops open sockets too); ptrace and reading or
# writing another process's memory; setsid and setpgid (a run can't leave the process group killpg ends); setpriority,
# and prlimit64 on another process; and below SCOPE_ABI, where Landlock doesn't scope signals, every syscall that
# signals another process, and fcntl F_SETOWN. ponytail: sched_setaffinity and ioprio_set on another process of the
# uid still pass (a slowdown, never a kill); deny them here if that matters. Another syscall ABI
# (i386 through int 0x80) kills the process, and x32 numbers are refused. (audit arch, {syscall: nr}) per CPU.
_GENERIC = {"socket": 198, "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271, "pidfd_getfd": 438,
            "setsid": 157, "setpgid": 154, "kill": 129, "tkill": 130, "tgkill": 131, "rt_sigqueueinfo": 138,
            "rt_tgsigqueueinfo": 240, "pidfd_send_signal": 424, "io_uring_setup": 425, "setpriority": 140,
            "prlimit64": 261, "fcntl": 25}  # asm-generic/unistd.h
_SECCOMP_ARCH = {"x86_64": (0xC000003E, {"socket": 41, "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311,
                                         "pidfd_getfd": 438, "setsid": 112, "setpgid": 109, "kill": 62, "tkill": 200,
                                         "tgkill": 234, "rt_sigqueueinfo": 129, "rt_tgsigqueueinfo": 297,
                                         "pidfd_send_signal": 424, "io_uring_setup": 425, "setpriority": 141,
                                         "prlimit64": 302, "fcntl": 72}),
                 "aarch64": (0xC00000B7, _GENERIC), "riscv64": (0xC00000F3, _GENERIC)}
_DENY = ("socket", "io_uring_setup", "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd", "setsid",
         "setpgid", "setpriority")
_SIGNALS = ("kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo", "pidfd_send_signal")


class _FProg(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def syscall_filter():
    """In the parent: a seccomp program for enter_jail, or None when this CPU's numbers aren't known. Below SCOPE_ABI
    it refuses signals too: a run can't even kill its own children then (killpg from the parent still ends them)."""
    arch = _SECCOMP_ARCH.get(platform.machine())
    if not arch:
        return None
    nr, scoped = arch[1], abi() >= SCOPE_ABI
    # (code, jt, jf, k): a jump is an offset or "ok"/"no", resolved below. ld 0x20, jeq 0x15, jge 0x35, ret 0x06.
    ins = [(0x20, 0, 0, 4), (0x15, 1, 0, arch[0]), (0x06, 0, 0, 0x80000000),  # arch: same ABI or kill the process
           (0x20, 0, 0, 0), (0x35, "no", 0, 0x40000000)]  # the syscall number; x32's are refused
    ins += [(0x15, "no", 0, nr[s]) for s in _DENY + (() if scoped else _SIGNALS)]
    # prlimit64 on itself only (pid 0, as glibc's setrlimit calls it): another process's RLIMIT_CPU kills it at any
    # ABI. The args sit at offset 16, 8 bytes each, low word first (all three CPUs are little-endian).
    ins += [(0x15, 0, 4, nr["prlimit64"]), (0x20, 0, 0, 16), (0x15, 0, "no", 0), (0x20, 0, 0, 20),
            (0x15, "ok", "no", 0)]
    if not scoped:  # F_SETOWN and F_SETOWN_EX aim SIGIO, any signal with F_SETSIG, at another process
        ins += [(0x15, 0, 3, nr["fcntl"]), (0x20, 0, 0, 24), (0x15, "no", 0, 8), (0x15, "no", "ok", 15)]
    ins += [(0x06, 0, 0, 0x7FFF0000), (0x06, 0, 0, 0x00050000 | 1)]  # ok: allow; no: SECCOMP_RET_ERRNO | EPERM
    to = {"ok": len(ins) - 2, "no": len(ins) - 1}
    ins = [(op, *(to[j] - i - 1 if isinstance(j, str) else j for j in (jt, jf)), k) for i, (op, jt, jf, k) in
           enumerate(ins)]
    buf = ctypes.create_string_buffer(b"".join(struct.pack("=HBBI", *i) for i in ins))
    prog = _FProg(len(ins), ctypes.cast(buf, ctypes.c_void_p))
    prog.buf = buf  # kept alive with the program
    return prog


def enter_jail(rs, prog):
    """In the child: no new privileges, the seccomp program (when there is one), then the Landlock ruleset."""
    if _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if prog is not None and _LIBC.prctl(22, 2, ctypes.byref(prog), 0, 0) != 0:  # PR_SET_SECCOMP, MODE_FILTER
        raise OSError(ctypes.get_errno(), "seccomp failed")
    if _LIBC.syscall(_NR[platform.machine()][2], ctypes.c_int(rs), ctypes.c_uint32(0)) < 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")


def enter(rs):
    """In the child (subprocess preexec_fn): no new privileges, then lock to the ruleset. Two syscalls, nothing loaded."""
    if _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if _LIBC.syscall(_NR[platform.machine()][2], ctypes.c_int(rs), ctypes.c_uint32(0)) < 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
