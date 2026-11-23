"""T-5900: the one place Sampo signals a process. Imports os, signal and subprocess (the last to record every Popen).

kill(-1, SIGKILL) is every process of the user, kill(0, ...) is our own group, killpg(1, ...) is kill(-1, ...), and a
Mock's pid converts to 1: on 2026-11-22 a test's mocked Popen sent exactly that and killed everything three times. Both
helpers return False without signalling unless `type(pid) is int and pid > 1` (a bool, a str, None and a Mock are not
plain ints), and True once the signal was sent. T-6095: `kill` and `killpg` also refuse a plain int that this process did
not start: a pid is ours when it is this process, a pid this process's real subprocess.Popen started (recorded when the
Popen is constructed, so a group kill after its leader was reaped still reaches the group), or a live child of this
process (its /proc/<pid>/stat ppid is our pid). A Mock(pid=N) stub never runs the real Popen.__init__, so it is never
recorded. kill_other is for the one case that signals a pid another process started, named by a record its caller
checked (a pidfile with its start time, a process's environment marker): it keeps the plain-int rule only.
The OSError of a process that is gone (ESRCH) or not ours (EPERM) is the caller's, as with os.kill itself: each site keeps
its own handling. A signal of 0 is an existence check and is not routed here (os.kill(pid, 0) signals nothing);
test_killguard holds that no other os.kill/os.killpg is left in lib."""
import os
import subprocess

REGISTRY_MAX = 4096  # the started pids kept for the life of this process; the oldest is dropped past this

_STARTED = {}  # pid -> None, insertion-ordered: the pids real Popens of this process started


def safe(pid):
    """True for a pid that names one process (or its group): a plain int above 1."""
    return type(pid) is int and pid > 1


def _remember(pid):
    """Record a started pid as the newest entry, dropping the oldest entries past REGISTRY_MAX."""
    if not safe(pid):
        return
    _STARTED.pop(pid, None)
    _STARTED[pid] = None
    while len(_STARTED) > REGISTRY_MAX:
        del _STARTED[next(iter(_STARTED))]


def _wrap_popen_init():
    """Wrap subprocess.Popen.__init__ once: call the real one, then record self.pid. A stub (a Mock) never runs it."""
    original = subprocess.Popen.__init__
    if getattr(original, "_killsafe_records", False):  # already wrapped (a re-import): wrap nothing twice
        return

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        _remember(getattr(self, "pid", None))

    __init__._killsafe_records = True
    subprocess.Popen.__init__ = __init__


_wrap_popen_init()


def started(proc):
    """Explicit alias for the recording the Popen wrap already does: registers a real subprocess.Popen (the exact type,
    not a Mock, a MagicMock with a spec, or a subclass). Returns proc."""
    if type(proc) is subprocess.Popen:
        _remember(proc.pid)
    return proc


def _ours(pid):
    """True for this process, a pid a real Popen of this process started, or a live child of this process (by /proc)."""
    if pid == os.getpid() or pid in _STARTED:
        return True
    try:  # "pid (comm) state ppid ...": comm may hold spaces and ')', so the fields are the ones after the last ')'
        with open(f"/proc/{pid}/stat", "rb") as f:
            fields = f.read().rsplit(b")", 1)[1].split()
    except OSError:  # no /proc (not Linux) or no such process: not one this process can show it started
        return False
    return len(fields) > 1 and fields[1] == str(os.getpid()).encode()


def ours(pid):
    """True for a plain int above 1 that this process started (killpg and kill would signal it); no signal is sent."""
    return safe(pid) and _ours(pid)


def killpg(pid, sig):
    """os.killpg(pid, sig) for a plain int pid above 1 that this process started; False, nothing sent, for any other.
    Raises what os.killpg raises."""
    if not (safe(pid) and _ours(pid)):
        return False
    os.killpg(pid, sig)
    return True


def kill(pid, sig):
    """os.kill(pid, sig) for a plain int pid above 1 that this process started; False, nothing sent, for any other.
    Raises what os.kill raises."""
    if not (safe(pid) and _ours(pid)):
        return False
    os.kill(pid, sig)
    return True


def kill_other(pid, sig):
    """os.kill(pid, sig) for a plain int pid above 1 that another process started, when the caller has checked the record
    it came from; False, nothing sent, for any other value. Raises what os.kill raises."""
    if not safe(pid):
        return False
    os.kill(pid, sig)
    return True


def killpg_other(pid, sig):
    """os.killpg(pid, sig): kill_other for a process group (T-6184: a session's runner leads its own group, and `fm pause
    --now` runs in another process, so the group is no child of this one). Same plain-int rule, same checked record."""
    if not safe(pid):
        return False
    os.killpg(pid, sig)
    return True
