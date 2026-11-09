"""T-3928: a restart nobody announced. Long jobs (lane land, lane train, run, night, ship) journal themselves while they
run (state/<project>/running/<boot>-<pid>.json: argv, cwd, boot id, start) and remove the entry on any clean exit, so
an entry from an older boot is a job the reboot interrupted. The first SessionStart of a new boot scans what a hard stop
leaves (those entries, a merge half-done in main or a lane, index.lock files, old-boot leases, dirty lanes whose builder
died) and says so in one line; `sampo recover` brings it back: restarts the jobs with their original arguments, aborts
the merges, drops the locks and leases, snapshots the dead builders' lane files under refs/foreman/snapshots (no commit), prints one short report.
Running it twice changes nothing the second time. Nothing here needs systemd or a session that remembers a script."""
import contextlib
import functools
import json
import os
import re
import sys
import time

from sampo import core as c
from sampo.core import subprocess

KINDS = ("land", "train", "run", "night", "ship")  # the journaled verbs
_QUICK_NIGHT = ("stage", "install", "uninstall", "tick", "dry_run")  # night's one-shot flags: not a long job


@functools.lru_cache(maxsize=4)
def _read_boot(path):
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


def boot():
    """The kernel's boot id ('' where there is none: nothing here then claims a reboot)."""
    h = sys.modules.get("sampo.hooks")  # its BOOT_ID, without importing the hooks into every sampo call
    return _read_boot(getattr(h, "BOOT_ID", "/proc/sys/kernel/random/boot_id"))


def btime():
    """Epoch seconds the machine booted (/proc/stat), or None."""
    try:
        with open("/proc/stat", encoding="ascii") as f:
            for ln in f:
                if ln.startswith("btime "):
                    return float(ln.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _dir(p):
    return os.path.join(p.dir, "running")


# ---------------------------------------------------------------- the journal

def _kind(args):
    cmd, action = getattr(args, "verb", None), getattr(args, "action", None)
    if cmd == "lane" and action in ("land", "train") and not getattr(args, "bg", False):
        return action
    if cmd == "night" and not any(getattr(args, k, False) for k in _QUICK_NIGHT):
        return "night"
    return cmd if cmd in ("run", "ship") else None


@contextlib.contextmanager
def journal(p, kind, argv, cwd=None):
    """Record the job in the running journal for as long as the body runs; any exit that runs the finally (clean, an
    error, a KeyboardInterrupt) removes it, a SIGKILL or a power cut leaves it. A journal that can't be written never
    stops the job."""
    path = os.path.join(_dir(p), f"{boot()[:8] or 'noboot'}-{os.getpid()}.json")
    try:
        os.makedirs(_dir(p), exist_ok=True)
        c.write_atomic(path, json.dumps({"kind": kind, "argv": list(argv), "cwd": cwd or os.getcwd(), "pid": os.getpid(),
                                         "boot": boot(), "project": p.slug, "start": time.time()}))
    except OSError:
        path = None
    try:
        yield
    finally:
        if path:
            with contextlib.suppress(OSError):
                os.remove(path)


@contextlib.contextmanager
def around(args, argv):
    """cli.main's one call site: journal the command when it is one of the long jobs; nothing otherwise."""
    kind = _kind(args)
    p = None
    if kind:
        try:
            from sampo import cli
            p = cli.resolve(args)
        except Exception:  # run --all-projects, no project here: not journaled
            p = None
    if p is None:
        yield
    else:
        with journal(p, kind, argv):
            yield


def entries(p):
    """Every journal entry of this project, oldest first."""
    rows = []
    try:
        names = sorted(os.listdir(_dir(p)))
    except OSError:
        return rows
    for n in names:
        try:
            with open(os.path.join(_dir(p), n), encoding="utf-8") as f:
                e = json.load(f)
            if isinstance(e, dict) and isinstance(e.get("argv"), list) and e.get("kind") in KINDS:
                rows.append({**e, "file": os.path.join(_dir(p), n)})
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda e: e.get("start") or 0)


def interrupted(p):
    """Journal entries from an older boot, one per distinct (kind, argv, cwd): a job listed twice is restarted once."""
    now, seen, out = boot(), set(), []
    for e in entries(p):
        if not now or e.get("boot") in (now, "", None):
            continue
        key = (e["kind"], tuple(e["argv"]), e.get("cwd"))
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


# ---------------------------------------------------------------- what a hard stop leaves

def _trees(p):
    main = c.main_worktree(p.root) or p.root
    lanes = [b.meta["lane"] for b in c.load_briefs(p) if b.meta.get("lane") and b.status not in c.CLOSED
             and os.path.isdir(str(b.meta["lane"]))]
    return main, [t for t in dict.fromkeys([main] + lanes)]


def _gitpath(tree, name):
    out = subprocess.run(["git", "-C", tree, "rev-parse", "--git-path", name], capture_output=True, text=True,
                         timeout=20).stdout.strip()
    return out if os.path.isabs(out) else os.path.join(tree, out)


def _old(path, bt):
    try:
        return bt is not None and os.path.getmtime(path) < bt
    except OSError:
        return False


def stale_git(p):
    """([trees with a MERGE_HEAD from the old boot], [index.lock paths older than the boot])."""
    bt = btime()
    merges, locks = [], []
    for t in _trees(p)[1]:
        try:
            m, lk = _gitpath(t, "MERGE_HEAD"), _gitpath(t, "index.lock")
        except (OSError, subprocess.SubprocessError):
            continue
        if _old(m, bt):
            merges.append(t)
        if _old(lk, bt):
            locks.append(lk)
    return merges, locks


_SAMPO_MERGE = re.compile(r"foreman/T-|worktree-agent-|Merge T-\d")


def sampos_merge(p, tree):
    """A merge Sampo started: in a lane, or one whose MERGE_MSG names a lane branch or 'Merge T-NNNN'. A person's hand
    merge in the main checkout is not (aborting it would throw their resolved conflicts away)."""
    if os.path.realpath(tree) != os.path.realpath(_trees(p)[0]):
        return True
    try:
        with open(_gitpath(tree, "MERGE_MSG"), encoding="utf-8", errors="replace") as f:
            return bool(_SAMPO_MERGE.search(f.readline()))
    except (OSError, subprocess.SubprocessError):
        return False


def _stale_lease(v, now_boot, bt):
    if v.get("boot"):
        return v["boot"] != now_boot
    from sampo import bus  # a lease from before this field: renewed before the boot, then
    return bt is not None and v.get("until", 0) - bus.LEASE_S < bt


def old_leases(p):
    from sampo import bus
    bt, now = btime(), boot()
    try:
        with open(os.path.join(bus._dir(), "leases.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [k for k, v in data.items() if v.get("project") == p.slug and _stale_lease(v, now, bt)]


def dead_dirty_lanes(p):
    """{task id: lane path} for lanes whose builder's process ended and whose tree holds uncommitted work."""
    from sampo import lanes
    out = {}
    for tid in lanes.stopped(p):
        b = c.find_brief(p, tid)
        path = str(b.meta.get("lane") or "") if b else ""
        if os.path.isdir(path) and subprocess.run(["git", "-C", path, "status", "--porcelain"], capture_output=True,
                                                  text=True, timeout=30).stdout.strip():
            out[tid] = path
    return out


def scan(p):
    merges, locks = stale_git(p)
    return {"jobs": [{"kind": e["kind"], "argv": e["argv"], "cwd": e.get("cwd")} for e in interrupted(p)],
            "merges": [t for t in merges if sampos_merge(p, t)], "locks": locks, "leases": len(old_leases(p)), "lanes": sorted(dead_dirty_lanes(p))}


def _empty(s):
    return not (s["jobs"] or s["merges"] or s["locks"] or s["leases"] or s["lanes"])


def line(s):
    parts = [f"{len(s['jobs'])} job(s) ({', '.join(sorted({j['kind'] for j in s['jobs']}))})" if s["jobs"] else "",
             f"{len(s['merges'])} half-done merge(s)" if s["merges"] else "",
             f"{len(s['locks'])} stale lock(s)" if s["locks"] else "",
             f"{s['leases']} lease(s)" if s["leases"] else "",
             f"{len(s['lanes'])} lane(s) of dead builders with unsaved work" if s["lanes"] else ""]
    return ("The machine restarted and interrupted " + ", ".join(x for x in parts if x)
            + ": sampo recover brings it back (run it now, unasked).")


# ---------------------------------------------------------------- the SessionStart note and sampo next's lead

def _mark(p):
    return os.path.join(p.dir, "reboot.json")


def note(p):
    """The first SessionStart of a new boot scans once and, when the reboot left anything, writes the record and says so
    in one line; a later session says it again until sampo recover runs. '' otherwise (and on any error)."""
    try:
        now = boot()
        if not now:
            return ""
        seen = os.path.join(p.dir, "recovered-boot")
        if _read_boot_file(seen) == now:
            return ""
        rec = _record(p)
        if rec is None or rec.get("boot") != now:
            s = scan(p)
            if _empty(s):
                c.write_atomic(seen, now)
                return ""
            rec = {"boot": now, "scan": s}
            c.write_atomic(_mark(p), json.dumps(rec))
        return line(rec["scan"])
    except Exception:  # a scan that fails must never cost the session its start
        return ""


def _read_boot_file(path):
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


def _record(p):
    try:
        with open(_mark(p), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def next_lead(p):
    """sampo next's leading clause while a scanned reboot is unrecovered (one small file read; '' otherwise)."""
    rec = _record(p)
    if rec and rec.get("boot") and rec["boot"] == boot() and isinstance(rec.get("scan"), dict):
        return line(rec["scan"]) + " · then: "
    return ""


# ---------------------------------------------------------------- sampo recover

def spawn(p, e):
    """Restart one job detached in its own session with its original arguments, its output appended to recover.log.
    The pid, or None when background starts are off (FOREMAN_NO_BACKGROUND, paused: the job then stays listed)."""
    if os.environ.get("FOREMAN_NO_BACKGROUND") or c.panicked():
        return None
    cwd = e.get("cwd") if os.path.isdir(str(e.get("cwd"))) else p.root
    with open(os.path.join(p.dir, "recover.log"), "a", encoding="utf-8") as out:  # ponytail: appends only
        return subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), *[a for a in e["argv"] if a != "--bg"]],
                                cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True).pid


def _git(tree, *args):
    return subprocess.run(["git", "-C", tree, "-c", "user.name=Sampo", "-c", "user.email=sampo@localhost", *args],
                          capture_output=True, text=True, timeout=60)


def damaged(p):
    """T-4074: what git fsck --connectivity-only finds wrong in the project's repo, '' when it is whole (or no git repo,
    or git can't tell in time): the 2026-11-09 reboot left 11 loose objects empty, and recover restarted a land on them."""
    if not c.git_root(p.root):
        return ""
    try:
        r = subprocess.run(["git", "-C", p.root, "fsck", "--connectivity-only", "--no-dangling", "--no-progress"],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode == 0:
        return ""
    return c.joinmore([x.strip() for x in (r.stderr + r.stdout).splitlines() if x.strip()], 3, "; ") or f"exit {r.returncode}"


def recover(p):
    """Bring back what the reboot interrupted; the report as {restarted, left, merges, locks, leases, snapshots,
    relaunch, fsync}. Idempotent: a second run finds nothing to do. A repo git finds broken holds every restart."""
    from sampo import bus, py
    rep = {"restarted": [], "left": [], "merges": [], "locks": [], "leases": 0, "snapshots": [], "relaunch": [],
           "fsync": py.ensure_fsync(c.foreman_home())}  # T-4074: T-3727's setting, for installs that never rerun setup
    bad = damaged(p)
    if bad:  # the jobs stay listed: the run after the repair restarts them
        rep["left"].append(f"nothing restarted: the repo is damaged (git fsck: {bad}); move the empty objects aside, "
                           f"rebuild the commits they held, then run sampo recover again")
        return rep
    now, kept = boot(), set()
    for e in interrupted(p):
        print(f"restarting {e['kind']}: sampo {' '.join(e['argv'])}", file=sys.stderr, flush=True)
        pid = spawn(p, e)
        if pid is None:  # the entry stays: the next run (with background starts on) restarts it
            kept.add((e["kind"], tuple(e["argv"]), e.get("cwd")))
            rep["left"].append(f"{e['kind']}: background starts are off (FOREMAN_NO_BACKGROUND or paused): "
                               f"sampo {' '.join(e['argv'])}")
            continue
        rep["restarted"].append(f"{e['kind']} (pid {pid}): sampo {' '.join(e['argv'])}")
    for e in entries(p):  # every old-boot entry (a duplicate of a restarted job too) goes, except the ones left
        if now and e.get("boot") not in (now, "", None) and (e["kind"], tuple(e["argv"]), e.get("cwd")) not in kept:
            with contextlib.suppress(OSError):
                os.remove(e["file"])
    merges, locks = stale_git(p)
    for lk in locks:  # first: git merge --abort needs the index
        with contextlib.suppress(OSError):
            os.remove(lk)
            rep["locks"].append(lk)
    for t in merges:
        if not sampos_merge(p, t):
            rep["left"].append(f"merge in {t} from before the reboot: not Sampo's, finish or abort it by hand")
        elif _git(t, "merge", "--abort").returncode == 0:
            rep["merges"].append(t)
        else:
            rep["left"].append(f"merge in {t}: git merge --abort failed; resolve it by hand")
    with c.lock(bus._dir()):  # old-boot leases
        stale = set(old_leases(p))
        if stale:
            path = os.path.join(bus._dir(), "leases.json")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            c.write_atomic(path, json.dumps({k: v for k, v in data.items() if k not in stale}, indent=1))
            rep["leases"] = len(stale)
    for tid, path in dead_dirty_lanes(p).items():
        try:  # refs only: the branch stays put (a WIP commit would make the lane look finished) and the files stay
            if c.worktree_tree(path) in _git(path, "for-each-ref", "--format=%(tree)", c.SNAPSHOTS).stdout.split():
                continue  # this exact tree is already saved: a second run changes nothing
            if c.snapshot(path, f"restart: {tid}"):
                rep["snapshots"].append(tid)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            rep["left"].append(f"{tid}: its snapshot failed; the lane {path} keeps its files")
    from sampo import lanes
    rep["relaunch"] = lanes.stopped(p)
    if not rep["left"]:
        now = boot()
        if now:
            c.write_atomic(os.path.join(p.dir, "recovered-boot"), now)
        with contextlib.suppress(OSError):
            os.remove(_mark(p))
    return rep


def cmd_recover(args):
    from sampo import cli
    p = cli.resolve(args)
    rep = recover(p)
    todo = any(rep[k] for k in ("restarted", "merges", "locks", "leases", "snapshots", "fsync"))
    lines = [f"restarted {x}" for x in rep["restarted"]] + [f"aborted the half-done merge in {t}" for t in rep["merges"]]
    lines += ["set core.fsync on Sampo's repo, so a crash can't leave its git objects empty"] * bool(rep["fsync"])
    lines += [f"removed {len(rep['locks'])} stale index.lock file(s)"] * bool(rep["locks"])
    lines += [f"dropped {rep['leases']} lease(s) of the old boot"] * bool(rep["leases"])
    lines += [f"snapshot of {t}'s lane files saved (refs/foreman/snapshots; the files stay for the relaunch)" for t in rep["snapshots"]]
    lines += [f"left: {x}" for x in rep["left"]]
    if rep["relaunch"]:
        lines.append(f"builders to relaunch: {', '.join(rep['relaunch'])} (sampo lane recover --all)")
    return cli.out(args, rep, "\n".join(lines) if lines else
                   ("Recovered." if todo else "Nothing to recover: no reboot interrupted anything."))
