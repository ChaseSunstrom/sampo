"""T-5900: the test runner's refusal to signal a pid that is not a real process of its own.

On 2026-11-22 a test stubbed subprocess.Popen with a MagicMock; its pid converts to 1, and os.killpg(1, SIGKILL) is
kill(-1, SIGKILL): every process of the user died, three times. `install()` swaps os.kill and os.killpg for wrappers that
raise KillRefused for a pid that is not a plain int (a Mock, None, True, '7') whatever the signal, and for a pid <= 1 with
a nonzero signal (0 and 1 are 'every process' or 'our own group'; -1 is every process). Signal 0 (an existence check) with
an int pid passes. A test that patches os.kill itself replaces the wrapper for its with-block and is unaffected.

Installed by run.py's BOOT (every class, helpers-based or plain unittest) and by helpers.py (a plain `python -m unittest`).
Stdlib only, idempotent, and a failure to install is not swallowed: BOOT imports this before the class runs."""
import os
import sys


class KillRefused(AssertionError):
    """A test reached os.kill/os.killpg with a pid that could signal every process or none that exists."""


def refusal(pid, sig):
    """Why (pid, sig) must not be sent, or '' when it may be."""
    if type(pid) is not int:
        return f"pid {pid!r} is a {type(pid).__name__}, not a plain int (a stubbed Popen's pid converts to 1)"
    try:
        nonzero = int(sig) != 0
    except (TypeError, ValueError):
        return f"signal {sig!r} is not a number"
    if pid <= 1 and nonzero:
        return f"pid {pid} with signal {int(sig)} reaches every process or our own group"
    return ""


def _guard(real, name):
    def guarded(pid, sig, *rest, **kw):
        why = refusal(pid, sig)
        if why:
            msg = f"killguard: os.{name} refused: {why}"
            try:
                sys.stderr.write(msg + "\n")
            except (OSError, ValueError):
                pass
            raise KillRefused(msg)
        return real(pid, sig, *rest, **kw)
    guarded.__name__ = getattr(real, "__name__", name)
    guarded.__wrapped__ = real
    guarded.killguard = True
    return guarded


def install():
    """Replace os.kill and os.killpg with the refusing wrappers, once per process."""
    for name in ("kill", "killpg"):
        fn = getattr(os, name, None)
        if fn is not None and not getattr(fn, "killguard", False):
            setattr(os, name, _guard(fn, name))
