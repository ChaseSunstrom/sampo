"""T-3928: a restart nobody announced. Long jobs (lane land, lane train, run, night, ship) journal themselves while they
run (state/<project>/running/<boot>-<pid>.json: argv, cwd, boot id, start) and remove the entry on any clean exit, so
an entry from an older boot is a job the reboot interrupted. The first SessionStart of a new boot scans what a hard stop
leaves (those entries, a merge half-done in main or a lane, index.lock files, old-boot leases, dirty lanes whose builder
died) and says so in one line; `sampo recover` brings it back: restarts the jobs with their original arguments, aborts
the merges, drops the locks and leases, snapshots the dead builders' lane files under refs/foreman/snapshots (no commit), prints one short report.
Running it twice changes nothing the second time. Nothing here needs systemd or a session that remembers a script."""
import calendar
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


@functools.lru_cache(maxsize=1)
def _mac_boot():
    """T-4633: macOS has no /proc: its kernel's boot session id (kern.bootsessionuuid), '' when sysctl says nothing."""
    try:
        got = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return got.stdout.strip() if got.returncode == 0 else ""


def boot():
    """The kernel's boot id ('' where there is none: nothing here then claims a reboot)."""
    h = sys.modules.get("sampo.hooks")  # its BOOT_ID, without importing the hooks into every sampo call
    got = _read_boot(getattr(h, "BOOT_ID", "/proc/sys/kernel/random/boot_id"))
    return got or (_mac_boot() if sys.platform == "darwin" else "")


def btime():
    """Epoch seconds the machine booted (/proc/stat; kern.boottime on macOS, T-4633), or None."""
    try:
        with open("/proc/stat", encoding="ascii") as f:
            for ln in f:
                if ln.startswith("btime "):
                    return float(ln.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    if sys.platform == "darwin":
        try:  # "{ sec = 1731500000, usec = 123456 } Fri Nov 14 ..."
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"sec = (\d+)", out)
        return float(m.group(1)) if m else None
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


# T-5591: the verbs that register themselves in the jobs registry while they run (sampo pulse and sampo jobs show them),
# keyed by the verb's name: the registry's row is "sampo <verb>"
REGISTERED_WHY = {
    "lane land": "lands the reviewed lanes on the trunk",
    "lane train": "one gate run over the finished lanes, then their merges",
    "lane verify": "the full suite on each landed merge",
    "lane precheck": "a lane's criteria, checked ahead of its merge",
    "check": "the project's gate commands: the full gate",
    "task finish": "the one-call close: checks, audits, docs, done and commit",
}


def _registered(args):
    """The REGISTERED_WHY key of a verb that registers, or None. A --bg starter returns at once (the job it starts
    registers itself); a check of one affected test set, of one lock or of one step's evidence is a short run, not a gate."""
    cmd, action = getattr(args, "verb", None), getattr(args, "action", None)
    if getattr(args, "bg", False):
        return None
    if cmd == "lane" and action in ("land", "train", "verify", "precheck"):
        return f"lane {action}"
    if cmd == "check" and action in (None, "run") and not any(getattr(args, k, None) for k in ("affected", "lock", "evidence")):
        return "check"
    if cmd == "task" and getattr(args, "task_cmd", None) == "finish":
        return "task finish"
    return None


class Exit:
    """What the verb ends with: cli.main sets code to the rc it returns, and the registry row says 'exit N' (T-5591)."""
    code = None


def _mapped(err):
    """The exit code cli.main maps an error to, or None: a PolicyError is 2, a LockTimeout 3, a CorruptState 4, a
    UsageError, a KeyError or any other ValueError 1. Most specific first (CorruptState is a ValueError)."""
    from sampo import cli
    code = getattr(err, "exit_code", None)
    if isinstance(code, int):
        return code
    for kind, rc in ((c.PolicyError, 2), (c.LockTimeout, 3), (c.CorruptState, 4), (cli.UsageError, 1),
                     (KeyError, 1), (ValueError, 1)):
        if isinstance(err, kind):
            return rc
    return None


def _result(job, err):
    if isinstance(err, SystemExit):  # argparse's usage error, a verb's sys.exit(N)
        return f"exit {err.code if isinstance(err.code, int) else 1}"
    if err is not None:
        code = _mapped(err)
        return f"exit {code}" if code is not None else f"stopped by {type(err).__name__}"
    return f"exit {job.code if isinstance(job.code, int) else 0}"


@contextlib.contextmanager
def registered(p, label, job):
    """Hand-register the running verb as a 'manual' job (the record sampo jobs add writes: nothing here restarts it).
    A registry that can't be written never stops the verb; the row is ended with its result on any exit."""
    from sampo import jobprog, jobsreg
    e = None
    with contextlib.suppress(Exception):
        log = jobprog.log_path(os.getpid())
        if log and not jobsreg.log_ok(p, log):  # a stdout the registry may not name (a credential): registered without it
            log = None
        e = jobsreg.add(p, f"sampo {label}", REGISTERED_WHY[label], pid=os.getpid(), log=log)
    err = None
    try:
        yield job
    except BaseException as x:
        err = x
        raise
    finally:
        if e:
            with contextlib.suppress(Exception):
                jobsreg.end(p, e["id"], _result(job, err))


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
    """cli.main's one call site: journal the command when it is one of the long jobs, and register the long verbs in the
    jobs registry (T-5591); nothing otherwise. Yields an Exit whose code the caller sets to the verb's rc."""
    kind, label, job = _kind(args), _registered(args), Exit()
    p = None
    if kind or label:
        try:
            from sampo import cli
            p = cli.resolve(args, create=False)  # no project here: nothing is made, and nothing is journaled or registered
        except Exception:  # run --all-projects, no project here: not journaled
            p = None
    if p is None:
        yield job
        return
    with contextlib.ExitStack() as stack:
        if kind:
            stack.enter_context(journal(p, kind, argv))
        if label:
            stack.enter_context(registered(p, label, job))
            fn = getattr(args, "fn", None)
            if callable(fn):  # the rc the verb returns is its exit code: set here, not in cli.main
                def run(a, _fn=fn):
                    job.code = _fn(a)
                    return job.code
                args.fn = run
        yield job


def entries(p):
    """Every restartable journal entry of this project, oldest first. A hand-registered job (kind "manual",
    sampo jobs add, T-5031) is a record, not a command: it is not in KINDS, so nothing here restarts or removes it."""
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


# ---------------------------------------------------------------- T-5903: what ended the session, before a relaunch

LOSS_WINDOW_S = 60  # a lane's command counts as just before the loss when it started within this many seconds of it
LOOKBACK_S = 6 * 3600  # how far back the journal and the kernel log are read for the kill
LOOKBACK = f"-{LOOKBACK_S // 3600}h"
STAMP_CMD = 1000  # the characters of a command the guard's stamp keeps (hooks._stamp_lastcmd cuts there, import-free)
JOURNAL_LINES = 20000 # the newest entries the journal read returns (a bound on its size)
DMESG_LINES = 20000  # the newest lines of the kernel log kept (it holds the whole boot)
CLUSTER_S = 120  # kill lines closer than this are one loss
HOLD_AT = 2  # kills (OOM or SIGKILL) a lane's command preceded before the lane is held
HOLDING = ("oom", "sigkill")  # the causes that can be a command's doing: a reboot or a logout is nobody's
LOSSES_KEPT = 20
STAMPS_KEPT_S = 7 * 86400
_RANK = {"oom": 0, "reboot": 1, "logout": 2, "sigkill": 3}  # one loss, several lines: the first of these that is there names it
# Every pattern is anchored at the start of the fixed message format (`match`): a unit's Description is free text the user
# writes, and it comes after "Started ", "Stopped " or "Reached target " in systemd's own lines, so text that only
# contains the words is no loss. A line that starts that way is dropped before any pattern is tried. Where systemd gives
# a MESSAGE_ID (its catalog: `journalctl --list-catalog`), that decides before any text does.
_PRE = r"(?:[\w@.\\:-]+\.(?:scope|service|slice): )?"  # the unit name systemd puts first (no space in it: no Description fits)
_OOM = re.compile(_PRE + r"A process of this unit has been killed by the OOM killer|Out of memory: Kill|oom-kill:|"
                  r"Memory cgroup out of memory: Kill|[\w@.\\:-]+\.(?:scope|service|slice): systemd-oomd killed|"
                  r"Killed \S+ due to (?:memory|swap)", re.I)
_REBOOT = re.compile(r"System is (?:rebooting|powering off|halting)\.?$|Journal stopped\.?$", re.I)
_REACHED = re.compile(r"Reached target ", re.I)  # which target it was is the line's UNIT or USER_UNIT field, not its text
_SIGKILL = re.compile(r"([\w@.\\:-]+\.(?:scope|service|slice)): (?:Killing process \d+ \([^)]*\) with signal SIGKILL|"
                      r"Failed with result 'signal'|Main process exited, code=killed, status=9/KILL)")
_NOISE = re.compile(r"(?:Start(?:ed|ing)|Stop(?:ped|ping)|Failed to start)\b", re.I)
_MID_OOM = frozenset(("fe6faa94e7774663a0da52717891d8ef", "d989611b15e44c9dbf31e3c81256e4ed"))  # unit OOM kill; systemd-oomd kill
_MID_SHUTDOWN = "98268866d1d54a499c4e98921d93bc40"  # system shutdown initiated
_STOP_SYSTEM = {"reboot.target": "reboot", "poweroff.target": "reboot", "halt.target": "reboot", "shutdown.target": "logout"}
_STOP_ANY = {"shutdown.target": "logout"}  # the user manager has one of its own; only a logout, never a reboot
_UNIT = re.compile(r"^([\w@.\\:-]+\.(?:scope|service|slice)): ")
_ISO = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:[,.]\d+)?(?:([+-])(\d\d):?(\d\d)|Z)?\s+(.*)$")
# the journal fields journald itself sets (the ones that start with `_`: a client cannot write them) say who wrote a line:
# only systemd's own binaries (the system manager as PID 1, a user manager, its logind, journald and oomd) and the kernel
# are read, so a `logger` or `systemd-cat` line cannot forge a loss
_TRUSTED_EXE = frozenset(f"{d}/{n}" for d in ("/usr/lib/systemd", "/lib/systemd")
                         for n in ("systemd", "systemd-journald", "systemd-logind", "systemd-oomd"))


def _safe(s, n=200):
    """Text from the journal or a command, cut and made plain (no control or format characters) and defanged: it reaches
    the terminal and the lead's model."""
    return c.defang(c.plain(c.fit(str(s), n)))


def _scope():
    """The unit this process runs in (the last part of its cgroup path: the session's own scope), or ''."""
    try:
        with open("/proc/self/cgroup", encoding="ascii") as f:
            return f.readline().rstrip("\n").rsplit("/", 1)[-1][:100]
    except (OSError, ValueError):
        return ""


def _own_units(path="/proc/self/cgroup"):
    """The units this process sits in, all the way up (its scope, user@UID.service, user-UID.slice): the user-wide ones
    stay the same after a session is lost, so a new session knows them too."""
    try:
        with open(path, encoding="ascii") as f:
            return {part for ln in f for part in ln.rstrip("\n").rsplit(":", 1)[-1].split("/")
                    if part.endswith((".scope", ".service", ".slice"))}
    except (OSError, ValueError):
        return set()


def _read_stamps(p):
    """{task id: stamp} of every readable stamp, open lane or not."""
    d, rows = os.path.join(p.dir, "lastcmd"), {}
    try:
        names = os.listdir(d)
    except OSError:
        return rows
    for n in names:
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        if n.endswith(".json") and isinstance(e, dict) and isinstance(e.get("ts"), (int, float)):
            rows[n[:-5]] = e
    return rows


def last_cmds(p):
    """{task id: {ts, cmd, boot, scope}} of the lanes still open (a brief with a lane folder, not closed), from the
    stamps. The stamp holds the command as the guard saw it, up to 1 KB; here, when reported, the whole text is made
    plain and redacted first and cut to 200 after, so a secret never straddles the cut."""
    live = {}
    for tid, e in _read_stamps(p).items():
        b = c.find_brief(p, tid)
        if b and b.status not in c.CLOSED and os.path.isdir(str(b.meta.get("lane") or "")):
            live[tid] = dict(e, cmd=_report_cmd(e.get("cmd")))
    return live


_KEY_TAIL = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?!-----END)[\s\S])*\Z")
_HEAD_TAIL = re.compile(r"-----BEGIN [A-Z ]*\Z")


def _report_cmd(raw):
    """A stamped command as it is reported: redacted whole, then cut to 200. A stamp at its 1000-character cap lost
    what followed, so its last word may be half a token (dropped) and a private key may have lost its end (the whole
    tail, from its BEGIN line, reads as one marker): neither matches a secret pattern once cut."""
    text = str(raw or "")[:STAMP_CMD]
    if len(text) >= STAMP_CMD:
        text = _HEAD_TAIL.sub("", text)
        text = text.rsplit(None, 1)[0] if len(text.split(None, 1)) > 1 and not text[-1:].isspace() else text
    text = _KEY_TAIL.sub("[REDACTED PRIVATE KEY]", text)
    return _safe(c.redact(c.plain(text)))


def _run(argv, keep=None):
    """(stdout, '') or ('', why): one read-only command, never past 20 s; off where background starts are off (tests).
    Undecodable bytes read as replacement characters; `keep` bounds the output to its newest lines."""
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        return "", "reads are off (FOREMAN_NO_BACKGROUND)"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"{argv[0]} did not run ({type(e).__name__})"
    if r.returncode != 0:
        return "", f"{argv[0]} exit {r.returncode}: {c.fit((r.stderr or r.stdout).strip().splitlines()[0] if (r.stderr or r.stdout).strip() else 'no output', 100)}"
    return ("\n".join(r.stdout.splitlines()[-keep:]) if keep else r.stdout), ""


def read_journal():
    """(text, why): the user journal of the last LOOKBACK as `-o json` (one object a line, the `_` fields journald sets
    among them), the newest JOURNAL_LINES entries."""
    return _run(["journalctl", "--user", f"--since={LOOKBACK}", "-o", "json", "-n", str(JOURNAL_LINES),
                 "--output-fields=MESSAGE,MESSAGE_ID,UNIT,USER_UNIT,_PID,_COMM,_EXE,_TRANSPORT", "--no-pager", "-q"])


def read_dmesg():
    """(text, why): the newest DMESG_LINES of the kernel log with ISO times; '' and the reason where the host keeps it
    from this user."""
    return _run(["dmesg", "--time-format=iso"], keep=DMESG_LINES)


def _iso(m):
    sign, hh, mm = m.group(7), m.group(8), m.group(9)
    off = (-1 if sign == "-" else 1) * (int(hh) * 3600 + int(mm) * 60) if sign else 0
    return calendar.timegm(tuple(int(m.group(i)) for i in range(1, 7))) - off


def _trusted(e):
    return e.get("_TRANSPORT") == "kernel" or e.get("_PID") == "1" or e.get("_EXE") in _TRUSTED_EXE


def _text(msg):
    """A journal MESSAGE as text: journald gives one with bytes that are no UTF-8 as a list of numbers."""
    if isinstance(msg, list) and all(isinstance(x, int) and 0 <= x < 256 for x in msg):
        return bytes(msg).decode("utf-8", "replace")
    return msg if isinstance(msg, str) else ""


def _journal_events(journal, units):
    """[(time, cause, message)] of the trusted journal lines that name a loss; a new boot id between two entries is a
    reboot at the last entry of the old boot."""
    out, prev = [], None
    for ln in (journal or "").splitlines():
        try:
            e = json.loads(ln)
            ts = int(e["__REALTIME_TIMESTAMP"]) / 1e6
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(e, dict):
            continue
        boot_id = str(e.get("_BOOT_ID") or "")
        if prev and boot_id and prev[1] and boot_id != prev[1]:
            out.append((prev[0], "reboot", "the journal's boot id changed"))
        prev = (ts, boot_id or (prev[1] if prev else ""))
        if not _trusted(e):
            continue
        msg = _text(e.get("MESSAGE"))
        cause = _cause(e, msg, units)
        if cause:
            out.append((ts, cause, msg))
    return out


def _cause(e, msg, units):
    """The loss a trusted journal entry names, or None. The entry's MESSAGE_ID decides first (the OOM kills, the system
    shutdown); a "Started ..." or "Stopped ..." line is none; the rest is the fixed message formats, anchored at the
    start, and a stopping target is read from the entry's UNIT or USER_UNIT field, never from its text."""
    mid, user, system = e.get("MESSAGE_ID"), e.get("USER_UNIT"), e.get("UNIT")
    if any(v is not None and not isinstance(v, str) for v in (mid, user, system)):
        return None  # a field sent twice reads as a list: never a loss
    if mid in _MID_OOM:
        return "oom"
    if mid == _MID_SHUTDOWN:
        return "reboot"
    if _NOISE.match(msg):
        return None
    if _OOM.match(msg):
        return "oom"
    if _REBOOT.match(msg):
        return "reboot"
    if _REACHED.match(msg):
        # the user manager can run a reboot.target of its own: only the system manager's UNIT names a reboot
        return _STOP_SYSTEM.get(system) if user is None else _STOP_ANY.get(user)
    hit = _SIGKILL.match(msg)
    field = user or system  # when systemd names the unit in a field too, the two must agree
    return "sigkill" if hit and hit.group(1) in units and field in (None, hit.group(1)) else None


def events(journal, dmesg, now=None, units=None):
    """[(time, cause, line)] oldest first for the lines that name a loss: an OOM kill, a shutdown, a SIGKILL of the
    session's own scope or a user-wide unit (`units`; this process's own units by default). Kernel log events older
    than LOOKBACK_S before `now` are dropped: dmesg holds the whole boot."""
    now = time.time() if now is None else now
    out = _journal_events(journal, _own_units() if units is None else set(units))
    for ln in (dmesg or "").splitlines():
        m = _ISO.match(ln.strip())
        if m and _OOM.match(m.group(10)) and now - _iso(m) <= LOOKBACK_S:
            out.append((float(_iso(m)), "oom", m.group(10)))
    return sorted(out)


def classify(journal, dmesg, boot_changed_at=None, now=None, units=None):
    """The latest loss as {cause, when, detail}, or None: the newest cluster of loss lines (CLUSTER_S apart at most), named
    by its first OOM kill, else its shutdown, else its logout, else its SIGKILL. A boot id that changed since the stamps
    (boot_changed_at, the newest old-boot stamp) is a reboot when the journal says nothing more specific."""
    evs = events(journal, dmesg, now, units)
    if evs:
        end = len(evs)
        start = end - 1
        while start > 0 and evs[start][0] - evs[start - 1][0] <= CLUSTER_S:
            start -= 1
        got = min(evs[start:end], key=lambda e: (_RANK[e[1]], e[0]))
        return {"cause": got[1], "when": got[0], "detail": _safe(got[2])}
    if boot_changed_at is not None:
        return {"cause": "reboot", "when": boot_changed_at, "detail": "the boot id changed since the lanes' last commands"}
    return None


CAUSE_WORDS = {"oom": "an out-of-memory kill", "reboot": "a reboot or shutdown of the machine",
               "logout": "a logout or the user manager stopping (not a kill)",
               "sigkill": "a SIGKILL of the session's processes (user-wide kill, not a reboot)"}


def _losses_path(p):
    return os.path.join(p.dir, "losses.json")


def _losses(p):
    try:
        with open(_losses_path(p), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [x for x in data if isinstance(x, dict) and isinstance(x.get("lanes"), dict)] if isinstance(data, list) else []


def held(p):
    """{task id: why} for each lane whose last command came before HOLD_AT or more recorded kills (an OOM or a SIGKILL: a
    reboot or a logout is nobody's command). The reason names them, its text plain and defanged."""
    seen = {}
    for loss in _losses(p):
        if loss.get("cause") in HOLDING:
            for tid, cmd in loss["lanes"].items():
                seen.setdefault(tid, []).append((loss.get("cause"), cmd))
    return {tid: f"its commands came right before {len(xs)} session losses ({'; '.join(f'{k}: {_safe(v, 60)}' for k, v in xs[-3:])}): "
                 f"suspect it before relaunching; sampo recover --release {_safe(tid, 40)} --why \"...\" lifts the hold"
            for tid, xs in seen.items() if len(xs) >= HOLD_AT}


def refuse_held(p, tid):
    """Raise the PolicyError naming the hold when the lane is held: every way back into a lane (recover, resume, the
    builder's brief) asks this first."""
    why = held(p).get(tid)
    if why:
        raise c.PolicyError(f"{tid} is held: {why}")


def release(p, tid):
    """Drop a lane from every recorded loss, so it is no longer held; whether anything was dropped."""
    data, hit = _losses(p), False
    for loss in data:
        hit = loss["lanes"].pop(tid, None) is not None or hit
    if hit:
        c.write_atomic(_losses_path(p), json.dumps(data, indent=1))
    return hit


def diagnose(p):
    """{cause, when, detail, journal, lanes, held}: what likely ended the session (read from the user journal and the
    kernel log, or 'unknown' with the reason they could not say), the open lanes' last command in the LOSS_WINDOW_S
    before it, and the lanes held. A loss is recorded once (the same cause within CLUSTER_S of one recorded is the same
    loss), so a second run adds nothing. Nothing raised here may stop a recovery."""
    stamps, now, at = last_cmds(p), boot(), time.time()
    jtext, jwhy = read_journal()
    dtext, dwhy = read_dmesg()
    old = [e["ts"] for e in stamps.values() if e.get("boot") and now and e["boot"] != now]
    units = _own_units() | {str(e["scope"]) for e in _read_stamps(p).values() if e.get("scope")}  # a lost session's scope too
    got = classify(jtext, dtext, max(old) if old else None, at, units)
    why = "; ".join(x for x in (f"journal: {jwhy}" if jwhy else "", f"dmesg: {dwhy}" if dwhy else "") if x)
    rep = {"cause": got["cause"] if got else "unknown", "when": got["when"] if got else None,
           "detail": got["detail"] if got else "", "journal": why, "lanes": {}, "held": {}}
    if got:
        near = {tid: {"cmd": e.get("cmd", ""), "ago": max(0, round(got["when"] - e["ts"]))} for tid, e in stamps.items()
                if -5 <= got["when"] - e["ts"] <= LOSS_WINDOW_S}
        rep["lanes"] = near
        data = _losses(p)  # one loss whatever the lines called it: an OOM kill ends in SIGKILLs, a shutdown in more
        if not any(abs((x.get("ts") or 0) - got["when"]) <= CLUSTER_S for x in data):
            data.append({"ts": got["when"], "cause": got["cause"],
                         "lanes": {t: v["cmd"] for t, v in near.items()}})
            c.write_atomic(_losses_path(p), json.dumps(data[-LOSSES_KEPT:], indent=1))
    else:  # no kill found, no time to measure from: each open lane's last command and its age, nothing recorded
        rep["lanes"] = {tid: {"cmd": e.get("cmd", ""), "ago": max(0, round(at - e["ts"]))} for tid, e in stamps.items()
                        if at - e["ts"] <= 600}
    rep["held"] = held(p)
    d = os.path.join(p.dir, "lastcmd")
    with contextlib.suppress(OSError):  # a stamp a week old belongs to no loss anyone is asking about
        for n in os.listdir(d):
            if time.time() - os.path.getmtime(os.path.join(d, n)) > STAMPS_KEPT_S:
                os.remove(os.path.join(d, n))
    return rep


def loss_lines(loss):
    """The report's lines for a diagnosis, the cause first."""
    if not loss or (loss["cause"] == "unknown" and not loss["lanes"] and not loss["held"]):
        return []  # nothing found and nothing to name: the report has no loss to speak of
    out = []
    if loss["cause"] == "unknown":
        out.append("likely cause: unknown (no kill, OOM or shutdown found in the user journal or kernel log"
                   + (f"; {loss['journal']}" if loss.get("journal") else "") + ")")
    else:
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(loss["when"]))
        out.append(f"likely cause: {CAUSE_WORDS[loss['cause']]} at {t} ({loss['detail']})"
                   + (f"; {loss['journal']}" if loss.get("journal") else ""))
    for tid, v in sorted(loss["lanes"].items()):
        out.append(f"  {tid} last ran, {v['ago']} s {'before the loss' if loss['cause'] != 'unknown' else 'ago'}: {v['cmd']}")
    for tid, why in sorted(loss["held"].items()):
        out.append(f"HELD {tid}: {why}")
    return out


def recover(p):
    """Bring back what the reboot interrupted; the report as {restarted, left, merges, locks, leases, snapshots,
    relaunch, fsync, loss}. Idempotent: a second run finds nothing to do. A repo git finds broken holds every restart.
    The loss (T-5903) is diagnosed first, before any job or lane is relaunched."""
    from sampo import bus, py
    try:
        loss = diagnose(p)
    except Exception as e:  # a diagnosis that fails says so and never costs the recovery
        loss = {"cause": "unknown", "when": None, "detail": "", "journal": f"the diagnosis failed ({type(e).__name__})",
                "lanes": {}, "held": {}}
    rep = {"restarted": [], "left": [], "merges": [], "locks": [], "leases": 0, "snapshots": [], "relaunch": [],
           "fsync": py.ensure_fsync(c.foreman_home()), "loss": loss}  # T-4074: T-3727's setting, for installs that never rerun setup
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
    rep["relaunch"] = [t for t in lanes.stopped(p) if t not in loss["held"]]  # a held lane is not offered again
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
    tid = getattr(args, "release", None)
    if tid:  # T-5903: the lead has read the lane's command and lifts its hold, and says why
        why = (getattr(args, "why", None) or "").strip()
        # like --unverified: a builder can't judge its own lane (and -p SLUG names the main project: ask the caller's folder)
        if p.lane or c.caller_is_lane():
            raise c.PolicyError("--release is for the main thread: a builder can't lift its own lane's hold")
        if not why:
            raise c.PolicyError("--release needs --why \"what you read in its commands and what you changed\"")
        got = release(p, tid)
        if got:
            c.log_event(p, "loss_released", task=tid, data={"why": why[:500]})
        return cli.out(args, {"released": got, "id": tid},
                       f"{tid}: hold lifted" if got else f"{tid}: not held (no recorded loss names it)")
    rep = recover(p)
    todo = any(rep[k] for k in ("restarted", "merges", "locks", "leases", "snapshots", "fsync"))
    lines = loss_lines(rep.get("loss"))
    lines += [f"restarted {x}" for x in rep["restarted"]] + [f"aborted the half-done merge in {t}" for t in rep["merges"]]
    lines += ["set core.fsync on Sampo's repo, so a crash can't leave its git objects empty"] * bool(rep["fsync"])
    lines += [f"removed {len(rep['locks'])} stale index.lock file(s)"] * bool(rep["locks"])
    lines += [f"dropped {rep['leases']} lease(s) of the old boot"] * bool(rep["leases"])
    lines += [f"snapshot of {t}'s lane files saved (refs/foreman/snapshots; the files stay for the relaunch)" for t in rep["snapshots"]]
    lines += [f"left: {x}" for x in rep["left"]]
    if rep["relaunch"]:
        lines.append(f"builders to relaunch: {', '.join(rep['relaunch'])} (sampo lane recover --all)")
    return cli.out(args, rep, "\n".join(lines) if lines else
                   ("Recovered." if todo else "Nothing to recover: no reboot interrupted anything."))
