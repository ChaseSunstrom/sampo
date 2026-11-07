"""T-3252: the tests' fm calls through a fork server. One process imports Sampo once; each call forks it, so a call
costs a fork and the verb's own work instead of a fresh interpreter's start and imports (60-200 ms under load).

A forked call gets exactly the argv, cwd, environment and stdin the test gave it, and returns a CompletedProcess like
subprocess.run. Only modules that read nothing from the environment at import are loaded ahead (PRELOAD; an audit test
holds them to it), so no fork inherits the server's settings. Any trouble reaching the server falls back to a fresh
subprocess, and FOREMAN_FORK_CLI=0 turns the server off. T-3572: run.py starts one server for all its classes
(share()), so a class starts its own only outside run.py, under --sandbox, or when the run's is gone.

Each request: the server forks the worker that runs the script and, when it exits, sends back its exit status (T-3678:
no manager process between them). The worker's pid goes to the client first, so a timeout kills it.
"""
import atexit
import json
import locale
import os
import selectors  # subprocess imports it already
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
# sampo.cli, then what its first parser build imports (15 of a cold call's ~18 ms): preloaded, a fork skips them
# T-3572: and ctypes, which every manager imported again for its prctl, and statistics, the hook's (~3 ms a call
# each); not sampo.hooks itself, which reads FOREMAN_HOOK_SLACK at import
PRELOAD = ("sampo.core", "sampo.cli", "sampo.design", "sampo.instr", "sampo.scene", "sampo.smoke", "inspect",
           "dataclasses", "tokenize", "glob", "base64", "colorsys", "copy", "ctypes", "statistics")
# what sampo.core does to the environment at import (SAMPO_X becomes FOREMAN_X; Claude's bin folder joins PATH): each
# call does it again on its own environment, as a fresh process would
REAPPLY = ("mirror_env", "claude_path")
# what a fresh interpreter can't be given after it starts: a call that changes one of these runs as a fresh process
# (PYTHON covers PYTHONPATH, PYTHONHASHSEED and every other interpreter setting)
START_ENV = ("PYTHON", "LC_", "LANG", "TZ")
_LOCK = threading.Lock()
_SERVER = {}  # pid -> (socket path, process), or (None, None) when it couldn't start: one try per test process
SHARED = "FOREMAN_FORK_SERVER"  # T-3572: run.py's server, which its classes call through: JSON [its LIB, its socket]
_BASE_PATH = []  # the server's sys.path after its script folder: what a fresh `python3 SCRIPT` has after its own


def _die_with_parent():
    """A forked worker dies with its server, as run.py's classes do (T-2034), so a killed test or server leaves no fm
    call running in its own session."""
    try:
        import ctypes
        ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def _send(conn, obj):
    data = json.dumps(obj).encode()
    conn.sendall(len(data).to_bytes(4, "big") + data)


def _recv(conn):
    head = b""
    while len(head) < 4:
        part = conn.recv(4 - len(head))
        if not part:
            raise ConnectionError("closed")
        head += part
    size, data = int.from_bytes(head, "big"), b""
    while len(data) < size:
        part = conn.recv(size - len(data))
        if not part:
            raise ConnectionError("closed")
        data += part
    return json.loads(data)


def _worker(req):
    """In the forked worker: become the fresh `python3 SCRIPT ARGS` the test asked for, then exit."""
    import runpy
    code = 1
    try:
        os.setsid()
        _die_with_parent()
        os.chdir(req["cwd"])
        os.environ.clear()
        os.environ.update(req["env"])
        tempfile.tempdir = None  # the server's cached temp folder: this call's TMPDIR decides, as in a fresh process
        core = sys.modules.get("sampo.core")
        for name in REAPPLY if core else ():
            getattr(core, name)(os.environ)
        for fd, path, mode in ((0, req["stdin"], os.O_RDONLY), (1, req["stdout"], os.O_WRONLY),
                               (2, req["stderr"], os.O_WRONLY)):
            f = os.open(path, mode)
            os.dup2(f, fd)
            os.close(f)
        sys.argv = [req["script"], *req["args"]]
        # T-3572: a fresh child's parent is its caller, not the server: what walks from os.getppid() (the hook's own
        # Claude Code, T-1226; SessionStart's recorded pids) sees the test; the server ends the call if the caller goes
        os.getppid = lambda: req["ppid"]
        sys.path[:] = [os.path.dirname(os.path.abspath(req["script"])), *_BASE_PATH]  # as `python3 SCRIPT` sets it
        try:
            runpy.run_path(req["script"], run_name="__main__")
            code = 0
        except SystemExit as e:
            if e.code is None:
                code = 0
            elif isinstance(e.code, int):
                code = e.code
            else:
                sys.stderr.write(f"{e.code}\n")
                code = 1
        except BaseException:  # noqa: BLE001 - a fresh interpreter prints the traceback and exits 1
            traceback.print_exc()
            code = 1
        threading._shutdown()  # what a fresh interpreter does at exit: join its threads, run atexit
        atexit._run_exitfuncs()
    finally:
        for s in (sys.stdout, sys.stderr):
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass
        os._exit(code & 0xFF)  # sys.exit(-1) is 255, as in a fresh process


def serve(path, parent):
    """The server: import ahead, then fork one worker per request until the test process is gone; its folder goes
    with it. T-3678: one loop and no manager per call (a fork fewer each): it reads every connection without blocking,
    so a slow client stalls no other; a worker's exit (SIGCHLD, through a wakeup pipe) sends its status; a connection
    that closes while its call runs (the client killed or timed out) takes the call with it."""
    me = os.getpid()
    _BASE_PATH[:] = sys.path[1:]
    sys.path.insert(0, LIB)
    import argparse  # noqa: F401 - warm what every call needs
    import runpy  # noqa: F401
    for mod in PRELOAD:
        __import__(mod)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # stop(): the finally below still runs
    wake_r, wake_w = os.pipe()
    for fd in (wake_r, wake_w):
        os.set_blocking(fd, False)
    signal.signal(signal.SIGCHLD, lambda *_: None)  # not ignored, which drops the workers' CPU time (T-3572)
    signal.set_wakeup_fd(wake_w)
    sel = selectors.DefaultSelector()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    calls = {}  # a running worker's pid -> its client's connection
    try:
        srv.bind(path)
        srv.listen(64)
        srv.setblocking(False)
        sel.register(srv, selectors.EVENT_READ)
        sel.register(wake_r, selectors.EVENT_READ)
        print("ready", flush=True)
        while os.getppid() == parent:
            for key, _ in sel.select(1.0):  # the 1 s: a test process gone ends the server
                if key.fileobj is srv:
                    try:
                        conn, _ = srv.accept()
                    except OSError:  # EAGAIN, or EMFILE: keep serving
                        continue
                    conn.setblocking(False)
                    sel.register(conn, selectors.EVENT_READ, b"")
                elif key.fileobj == wake_r:
                    try:
                        while os.read(wake_r, 4096):
                            pass
                    except BlockingIOError:
                        pass
                else:
                    _read(sel, key, calls, wake_w)
            _reap(sel, calls)
    finally:
        if os.getpid() == me:  # never a forked child's exit
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def _drop(sel, conn):
    sel.unregister(conn)
    conn.close()


def _read(sel, key, calls, wake_w):
    """One connection's turn: more of its request (the whole one forks its worker), or its end."""
    conn, got = key.fileobj, key.data
    try:
        part = conn.recv(1 << 16)
    except BlockingIOError:
        return
    except OSError:
        part = b""
    if isinstance(got, int):  # its call runs: the client sends nothing more, so this is its end
        if not part:  # T-3572: a client gone (run.py killed its class) takes its call with it
            calls.pop(got, None)
            try:
                os.killpg(got, signal.SIGKILL)
            except OSError:  # not its own group yet
                try:
                    os.kill(got, signal.SIGKILL)
                except OSError:
                    pass
            _drop(sel, conn)
        return
    if not part:  # gone before its whole request
        return _drop(sel, conn)
    got += part
    size = int.from_bytes(got[:4], "big") if len(got) >= 4 else -1
    if size < 0 or len(got) < 4 + size:
        return sel.modify(conn, selectors.EVENT_READ, got)
    try:
        req = json.loads(got[4:4 + size])
        pid = os.fork()
    except (ValueError, OSError):  # a bad request, or no fork (EAGAIN): the client reads a closed connection
        return _drop(sel, conn)
    if pid == 0:  # the worker keeps nothing of the server's: no other call's connection, no wakeup, no handlers
        try:
            signal.set_wakeup_fd(-1)
            for sig in (signal.SIGCHLD, signal.SIGTERM):
                signal.signal(sig, signal.SIG_DFL)
            for k in list(sel.get_map().values()):
                k.fileobj.close() if hasattr(k.fileobj, "close") else os.close(k.fileobj)
            sel.close()
            os.close(wake_w)
            _worker(req)
        finally:
            os._exit(1)  # never back into the server's loop
    calls[pid] = conn
    sel.modify(conn, selectors.EVENT_READ, pid)
    try:
        _send(conn, {"pid": pid})
    except OSError:  # its client is gone already: the next read sees the end and kills the call
        pass


def _reap(sel, calls):
    """Wait for every worker that ended and send its exit status to its client (T-3572: waited for, so a run's
    rusage counts its forked calls the way it counts fresh ones)."""
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            return
        conn = calls.pop(pid, None)
        if conn is None:  # its client went first
            continue
        try:
            _send(conn, {"code": os.waitstatus_to_exitcode(status)})
        except OSError:
            pass
        _drop(sel, conn)


def _server():
    """This process's server's socket path, started on first use; None when off or it couldn't start (tried once)."""
    if os.environ.get("FOREMAN_FORK_CLI") == "0":
        return None
    lib, path = json.loads(os.environ.get(SHARED) or "[null, null]")
    if lib == LIB and os.path.exists(path):  # another tree's server would run its own preloaded Sampo, not this one
        return path
    with _LOCK:
        got = _SERVER.get(os.getpid())
        if got and (got[1] is None or got[1].poll() is None):
            return got[0]
        base = tempfile.gettempdir()  # the class TMPDIR run.py removes after the class, so a killed server leaks nothing
        folder = tempfile.mkdtemp(prefix=f"fmfork-{os.getpid()}-", dir=base if len(base) < 60 else ("/tmp" if os.path.isdir("/tmp") else None))
        path, p = os.path.join(folder, "s"), None
        env = {k: v for k, v in os.environ.items() if not k.startswith("FOREMAN_")}
        try:
            p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", path, str(os.getpid())],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=env, start_new_session=True)
            ok = p.stdout.readline().strip() == b"ready"
        except OSError:
            ok = False
        if not ok:
            if p:
                p.kill()
                p.wait()
            shutil.rmtree(folder, ignore_errors=True)
            _SERVER[os.getpid()] = (None, None)
            return None
        _SERVER[os.getpid()] = (path, p)
        atexit.register(stop)  # T-3572: waited for at exit, so its calls' CPU counts in the class's rusage
        return path


def share():
    """T-3572: run.py's one server for all its classes, named in the environment they inherit (never an outer run's);
    None, naming none, when off or it couldn't start."""
    os.environ.pop(SHARED, None)
    path = _server()
    if path:
        os.environ[SHARED] = json.dumps([LIB, path])
    return path


def stop():
    """Stop this process's server and remove its folder (tests; the server also ends by itself, folder and all, when
    its test process exits)."""
    with _LOCK:
        got = _SERVER.pop(os.getpid(), None)
    if got and got[1]:
        got[1].terminate()
        try:
            got[1].wait(10)
        except subprocess.TimeoutExpired:
            got[1].kill()
            got[1].wait()
        got[1].stdout.close()  # its ready line's pipe (a ResourceWarning each stop)
        shutil.rmtree(os.path.dirname(got[0]), ignore_errors=True)


def run(script, args, cwd=None, env=None, input=None, timeout=None):
    """subprocess.run([sys.executable, script, *args], capture_output=True, text=True, ...) through the server."""
    argv = [sys.executable, script, *args]
    cwd = os.path.abspath(cwd or os.getcwd())
    env = dict(os.environ if env is None else env)
    # an interpreter or locale setting (PYTHONPATH, -X options, LC_ALL, TZ) shapes a fresh start, which a fork lacks
    same_start = all(env.get(k) == os.environ.get(k) for k in set(env) | set(os.environ) if k.startswith(START_ENV))
    path = _server() if os.path.isdir(cwd) and same_start else None
    fresh = lambda: subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,  # as a fork:
                                   **({"input": input} if input is not None else {"stdin": subprocess.DEVNULL}))  # no input reads /dev/null
    if path is None:
        return fresh()
    with tempfile.TemporaryDirectory(prefix="fmcall-") as d:
        files = {k: os.path.join(d, k) for k in ("stdin", "stdout", "stderr")}
        with open(files["stdin"], "w", encoding=locale.getpreferredencoding(False)) as f:
            f.write(input or "")
        for k in ("stdout", "stderr"):
            open(files[k], "w").close()
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(path)
        except OSError:  # the server is gone: nothing ran yet, so a fresh process is safe
            conn.close()
            return fresh()
        pid = None
        try:
            conn.settimeout(timeout)
            _send(conn, {"script": script, "args": list(args), "cwd": cwd, "env": env,
                         **files, "stdin": files["stdin"] if input is not None else os.devnull, "ppid": os.getpid()})
            pid = _recv(conn)["pid"]
            code = _recv(conn)["code"]
        except socket.timeout:
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
            raise subprocess.TimeoutExpired(argv, timeout)
        except (OSError, ConnectionError, ValueError, KeyError) as e:  # never rerun: the call may have started
            raise RuntimeError(f"forkcli: the fork server dropped the call ({e}); FOREMAN_FORK_CLI=0 runs fresh") from e
        finally:
            conn.close()
        out = []
        for k in ("stdout", "stderr"):
            with open(files[k], encoding=locale.getpreferredencoding(False), errors="replace") as f:
                out.append(f.read())
    return subprocess.CompletedProcess(argv, code, out[0], out[1])


if __name__ == "__main__" and sys.argv[1:2] == ["serve"]:
    serve(sys.argv[2], int(sys.argv[3]))
