"""T-5900: the one place Sampo signals a process. Only os and signal are imported, so a hook may import it.

kill(-1, SIGKILL) is every process of the user, kill(0, ...) is our own group, killpg(1, ...) is kill(-1, ...), and a
Mock's pid converts to 1: on 2026-11-22 a test's mocked Popen sent exactly that and killed everything three times. Both
helpers return False without signalling unless `type(pid) is int and pid > 1` (a bool, a str, None and a Mock are not
plain ints), and True once the signal was sent. The OSError of a process that is gone (ESRCH) or not ours (EPERM) is the
caller's, as with os.kill itself: each site keeps its own handling. A signal of 0 is an existence check and is not
routed here (os.kill(pid, 0) signals nothing); test_killguard holds that no other os.kill/os.killpg is left in lib."""
import os


def safe(pid):
    """True for a pid that names one process (or its group): a plain int above 1."""
    return type(pid) is int and pid > 1


def killpg(pid, sig):
    """os.killpg(pid, sig) for a plain int pid above 1; False, nothing sent, for any other. Raises what os.killpg raises."""
    if not safe(pid):
        return False
    os.killpg(pid, sig)
    return True


def kill(pid, sig):
    """os.kill(pid, sig) for a plain int pid above 1; False, nothing sent, for any other. Raises what os.kill raises."""
    if not safe(pid):
        return False
    os.kill(pid, sig)
    return True
