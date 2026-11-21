"""T-3330: focus takes a task's fleet claim (sampo.claims); the edit hook reads a local cache of the last claim state and
refuses once another machine holds it. The cache is fleet-claims.json in the project's state folder:
{task: {by: the holder's claim body or null, session: ours, checked: epoch}}. Only focus, release and refresh() touch
the network; the hook never does: it reads the file and, when an entry is over 10 minutes old, starts refresh()
detached. Fleet is opt-in: FOREMAN_CLAIMS_REMOTE names the remote (an origin alone never turns it on), and it stays off
for a sensitive project or under FOREMAN_NO_NET."""
import json
import os
import subprocess
import sys
import time

from sampo import claims, core as c


def on(p):
    return bool(os.environ.get("FOREMAN_CLAIMS_REMOTE")) and not os.environ.get("FOREMAN_NO_NET") \
        and not c.read_meta(p).get("sensitive")


def _path(p):
    return os.path.join(p.dir, "fleet-claims.json")


def _read(p):
    try:
        with open(_path(p), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(p, tid, by, now, session=None):
    d = _read(p)
    if session is None:
        session = (d.get(tid) or {}).get("session")
    d[tid] = {"by": by, "checked": now, "session": session}
    try:
        c.write_atomic(_path(p), json.dumps(d))
    except OSError:  # a cache: losing it only loses the early refusal
        pass


def _clean(v, n):
    """A remote claim's field, as text the model reads: printable characters only, at most n of them."""
    return "".join(ch for ch in str(v if v is not None else "") if ch.isprintable())[:n].strip()


def message(tid, by):
    who = _clean(by.get("name"), 40) or _clean(by.get("machine"), 8) or "another machine"
    return (f"{tid} is claimed by machine {who} until {_clean(by.get('until'), 20)} (fleet claim); take another task, "
            f"or wait for its lease to lapse.")


def take(p, tid, remote=None, now=None):
    """Focus's call: (refusal or None, note or None). Never raises: a broken claims layer must not stop a focus."""
    try:
        if not (remote or on(p)):
            return None, None
        now = time.time() if now is None else now
        cl = claims.Claims(p.root, remote=remote)
        r = cl.take(tid, now=now, same_machine=True)
        if r.get("result") == "lost":  # the race was lost: look once more (the winner may be us, or held)
            r = cl.take(tid, now=now, same_machine=True)
    except Exception:
        return None, None
    res = r.get("result")
    if res == "held":
        by = r.get("by") or {}
        if not claims.expired(by, now):
            _write(p, tid, by, now, cl.session)
            return message(tid, by), None
    if res == "taken":
        _write(p, tid, None, now, cl.session)
        return None, None
    if res == "lost":
        return f"{tid}: another machine won the claim race just now; run sampo focus {tid} again.", None
    if res == "offline":
        return None, "claims remote unreachable: focusing without a fleet claim"
    return None, f"fleet claim not taken ({res}): focusing anyway"


def refresh(p, tid, remote=None, now=None):
    """Renew (or find lost) this task's claim and rewrite the cache. Offline leaves the cache as it was. Uses the
    session the claim was taken with (this process, started detached, has no session env)."""
    if remote is None and not on(p):  # sensitive or no-net since the claim was cached: nothing leaves this machine
        return None
    now = time.time() if now is None else now
    session = (_read(p).get(tid) or {}).get("session")
    try:
        cl = claims.Claims(p.root, remote=remote, session=session)
        r = cl.take(tid, now=now)
    except Exception:
        return None
    if r.get("result") == "taken":
        _write(p, tid, None, now, cl.session)
    elif r.get("result") == "held":
        _write(p, tid, r.get("by") or {}, now, cl.session)
    return r.get("result")


def release(p, tid):
    """A task that stops being worked (done, blocked, dropped, deferred) frees its claim. Only if fleet took one."""
    e = _read(p).get(tid)
    if not isinstance(e, dict):
        return
    try:
        if not e.get("by") and on(p):
            if claims.Claims(p.root, session=e.get("session")).release(tid).get("result") == "offline":
                return  # kept: the next release retries
        d = _read(p)
        d.pop(tid, None)
        c.write_atomic(_path(p), json.dumps(d))
    except Exception:
        pass


def refusal(p, tid, now=None):
    """The hook's call: a message when the cached state says another machine holds tid, else None. No network;
    an entry over 10 minutes old starts a detached refresh (stamped first, so one per interval)."""
    now = time.time() if now is None else now
    e = _read(p).get(tid)
    if not isinstance(e, dict):
        return None
    if not on(p):  # sensitive or no-net since it was cached: no refresh, and the stale entry goes
        d = _read(p)
        d.pop(tid, None)
        try:
            c.write_atomic(_path(p), json.dumps(d))
        except OSError:
            pass
        return None
    by = e.get("by")
    try:
        checked = float(e.get("checked") or 0)
    except (TypeError, ValueError):
        checked = 0.0
    if now - checked >= claims.RENEW_EVERY and not os.environ.get("FOREMAN_NO_BACKGROUND"):
        _write(p, tid, by, now)
        lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from sampo import fleet, core as c; "
                "fleet.refresh(c.find_project(sys.argv[2]), sys.argv[3])")
        try:
            subprocess.Popen([sys.executable, "-c", code, lib, p.root, tid], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass
    if isinstance(by, dict) and by and not claims.expired(by, now):
        return message(tid, by)
    return None
