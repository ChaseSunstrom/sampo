"""T-3222: Sampo's sandbox, in layers. The job tier runs a command in its own user, mount, pid, ipc and uts namespaces
through bwrap (already on most Linux machines; nothing is installed): the system read-only, /tmp and /run fresh (no
session bus, no docker socket), only the job's project and the folders it names writable, credential folders empty
and credential files /dev/null, secret-looking variables gone from its environment, and the network off unless the
job asks for it, or only an allowlist through a filtering proxy outside (net="allow"). What would run later outside
the job (git hooks and config, Claude Code's project settings) is read-only even in its project, and a job may name a
docker image instead (docker_argv). status() also reports the session tier (Claude Code's own sandbox) and Landlock.
Without bwrap or unprivileged user namespaces, available() says why and run() refuses unless its caller accepts
running plain."""
import functools
import ipaddress
import json
import os
import pwd
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

# home-relative: a folder reads as empty, a file as /dev/null. ponytail: a fixed list of the usual places; a project's
# own .env or a browser profile elsewhere stays readable (reads are the session tier's denyRead and Landlock's job too)
CREDENTIALS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config/gcloud", ".config/gh", ".config/sampo",
               ".password-store", ".local/share/keyrings", ".mozilla", ".config/google-chrome", ".config/chromium",
               ".config/BraveSoftware", ".var/app", ".pki", ".netrc", ".git-credentials", ".pypirc", ".npmrc",
               ".cargo/credentials.toml", ".claude/.credentials.json")
SECRET_PARTS = {"KEY", "KEYS", "APIKEY", "PAT", "PWD", "PASS", "AUTH"}  # whole _-separated words
SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "CREDENTIAL", "APIKEY", "PRIVATEKEY")  # anywhere
# in a project, what runs later outside the job: Claude Code's and editors' project settings and direnv, and in every
# git dir (the project's, its submodules', nested repos') the hooks and config (core.fsmonitor, filters): read-only in
# there, an empty read-only stand-in where one is missing, and each git dir a mount point so it can't be swapped
PROTECTED = (".claude", ".mcp.json", ".envrc", ".vscode")
PROTECTED_FILES = (".mcp.json", ".envrc", "config")  # the rest are folders
GIT_PROTECTED = ("hooks", "config")
NO_WALK = {"node_modules", ".venv", "venv", "target", "dist", "build", "__pycache__", ".tox", ".git"}
AGENT_VARS = ("SSH_AUTH_SOCK", "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS")  # a door to a key holder outside
NETS = ("off", "allow", "on")
NET_DIR, RELAY_PORT = "/run/sampo-net", 3128  # inside: the proxy's socket folder, and the relay the job's proxy vars name
# inside the job's own network namespace: 127.0.0.1:RELAY_PORT to the proxy's unix socket, then the job itself
RELAY = r'''
import socket, subprocess, sys, threading
sock, cmd = sys.argv[1], sys.argv[2:]
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", %d))
srv.listen(64)
def pipe(a, b):
    try:
        while d := a.recv(65536):
            b.sendall(d)
    except OSError:
        pass
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass
def serve():
    while True:
        c, _ = srv.accept()
        u = socket.socket(socket.AF_UNIX)
        try:
            u.connect(sock)
        except OSError:
            c.close()
            continue
        threading.Thread(target=pipe, args=(c, u), daemon=True).start()
        threading.Thread(target=pipe, args=(u, c), daemon=True).start()
threading.Thread(target=serve, daemon=True).start()
sys.exit(subprocess.call(cmd))
''' % RELAY_PORT


class Unavailable(RuntimeError):
    """The job tier can't run here; the message says why."""


def secret(name):
    """Whether a variable name looks like it holds a secret: a KEY/PWD/PASS/AUTH word, TOKEN/SECRET/PASSWORD/PASSPHRASE
    anywhere in it (PGPASSWORD, NGROK_AUTHTOKEN), AWS_*, an agent socket. The shell's own PWD and OLDPWD aren't."""
    up = name.upper()
    if up in ("PWD", "OLDPWD"):
        return False
    return bool(set(up.split("_")) & SECRET_PARTS) or any(w in up for w in SECRET_WORDS) or up.startswith("AWS_") \
        or up in AGENT_VARS


def clean_env(env):
    return {k: v for k, v in env.items() if not secret(k)}


@functools.lru_cache(maxsize=None)
def available():
    """(ok, why): whether bwrap can make the job's namespaces here, and its version or the reason it can't."""
    exe = shutil.which("bwrap")
    if not exe:
        return False, "no bwrap on PATH (the job tier needs bubblewrap)"
    try:
        r = subprocess.run([exe, "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
                            "--ro-bind", "/", "/", "true"],
                           capture_output=True, text=True, timeout=20)
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"bwrap didn't start: {e}"
    if r.returncode:
        return False, f"bwrap can't make namespaces here: {r.stderr.strip()[:160] or f'exit {r.returncode}'}"
    return True, v or "bwrap"


def _under(path, base):
    return path == base or path.startswith(base.rstrip("/") + "/")


def _entry(e):
    """(host, port or None) of an allowlist entry: pypi.org, *.pythonhosted.org, localhost:11434, [::1]:8080."""
    e = e.strip().lower().rstrip(".")
    if e.startswith("[") and "]:" in e:
        h, p = e[1:].split("]:", 1)
        return h, int(p) if p.isdigit() else None
    h, sep, p = e.rpartition(":")
    return (h, int(p)) if sep and p.isdigit() and ":" not in h else (e.strip("[]"), None)


def _host_is(host, pat):
    host = host.lower().rstrip(".")
    return host.endswith(pat[1:]) if pat.startswith("*.") else host == pat


def allowed(host, port, allow):
    """Whether an allowlist names host (exactly, or by a *.suffix) at port (an entry with no port: any port)."""
    return any(_host_is(host, h) and p in (None, port) for h, p in map(_entry, allow))


def vet(host, port, allow):
    """(ok, why, the addresses to try in order): on the allowlist, and every address it resolves to is public, or
    private or loopback with an entry that names this host and port; link-local and reserved addresses (cloud
    metadata) never. Only the addresses vetted are connected, so a second lookup can't swap one in (DNS rebinding)."""
    if not allowed(host, port, allow):
        return False, "not on the allowlist", None
    named = any(_host_is(host, h) and p == port for h, p in map(_entry, allow))
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        return False, f"doesn't resolve ({e})", None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_loopback and (ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved):  # ::1
            # sits in ::/8, which Python calls reserved: loopback is judged below, by name and port like private ones
            return False, "a link-local or reserved address (cloud metadata lives there)", None
        if not ip.is_global and not named:  # private, loopback, CGNAT 100.64/10, NAT64: only by name with its port
            return False, "not a public address: allow it by name with its port", None
    return True, "", list(dict.fromkeys(info[4][0] for info in infos))


def _pipe(a, b):
    try:
        while d := a.recv(65536):
            b.sendall(d)
    except OSError:
        pass
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass


class _Proxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _handler(allow, log):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):  # one CONNECT host:port, or one plain http:// request; nothing else
            c, head = self.request, b""
            c.settimeout(30)
            while b"\r\n\r\n" not in head and len(head) < 16384:
                d = c.recv(4096)
                if not d:
                    return
                head += d
            words = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            method, target = (words[0].upper(), words[1]) if len(words) == 3 else ("", "")
            try:
                if method == "CONNECT":
                    host, port = _entry(target)
                    rest = head.split(b"\r\n\r\n", 1)[1]
                else:
                    u = urlsplit(target)
                    host, port, rest = (u.hostname or "", u.port or 80, head) if u.scheme == "http" else ("", None, b"")
            except (ValueError, IndexError):  # a port past 65535, a head with no end: a bad request, not a crash
                host, port = "", None
            if port is not None and not 0 < port < 65536:
                port = None
            if not host or not port:
                c.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            ok, why, addrs = vet(host, port, allow)
            log.append((host, port, ok, why))
            if not ok:
                c.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            up = None
            for addr in addrs:  # each vetted address in turn (localhost is ::1 first in some namespaces)
                try:
                    up = socket.create_connection((addr, port), 15)
                    break
                except OSError:
                    continue
            if up is None:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            c.settimeout(None)
            up.settimeout(None)
            if method == "CONNECT":
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                up.sendall(rest)
            t = threading.Thread(target=_pipe, args=(up, c), daemon=True)
            t.start()
            _pipe(c, up)
            t.join()
            up.close()
    return Handler


def _homes(home=None):
    """The home folders whose credentials are masked: the one given (else $HOME's) and the account's own, whatever
    $HOME says."""
    try:
        real = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        real = None
    return list(dict.fromkeys(os.path.realpath(h) for h in (home or os.path.expanduser("~"), real) if h))


def _writable(root, writes, homes):
    """[root, writes...] as real paths; ValueError for one that is /, a home folder or above one, or ~/.claude."""
    rw = list(dict.fromkeys([os.path.realpath(root)] + [os.path.realpath(w) for w in writes if os.path.isdir(w)]))
    for w in rw:
        for h in homes:
            if _under(h, w) or w == os.path.join(h, ".claude"):
                raise ValueError(f"{w} would make {h if _under(h, w) else w} writable: name a project folder")
    return rw


def _git_dirs(w):
    """The git dirs in a job's folder: its own .git, nested repos' up to three folders down, and their submodules'
    (.git/modules/**). ponytail: vendor and build folders aren't walked, so a repo inside one isn't found."""
    out = []
    for d, subs, _ in os.walk(w):
        g = os.path.join(d, ".git")
        if os.path.isdir(g) and not os.path.islink(g):
            out.append(g)
        subs[:] = [s for s in subs if s not in NO_WALK] if d[len(w):].count(os.sep) < 3 else []
    for g in list(out):
        out += [d for d, _, files in os.walk(os.path.join(g, "modules")) if "config" in files and "HEAD" in files]
    return sorted(set(out))


def _protected(w):
    """What in a writable folder runs later outside the job, as (path, kind) in mount order: "pin" a git dir (bound onto
    itself: a mount point can't be renamed away and replaced), "have" one that exists (bound read-only), "dir" or
    "file" one that doesn't (an empty read-only stand-in, so the job can't create it)."""
    out = []
    for g in _git_dirs(w):
        out.append((g, "pin"))
        out += [(p, "have" if os.path.lexists(p) else "file" if x in PROTECTED_FILES else "dir")
                for x, p in ((x, os.path.join(g, x)) for x in GIT_PROTECTED)]
    out += [(p, "have" if os.path.lexists(p) else "file" if x in PROTECTED_FILES else "dir")
            for x, p in ((x, os.path.join(w, x)) for x in PROTECTED)]
    if os.path.isfile(os.path.join(w, ".git")):  # a worktree's pointer to its git dir
        out.append((os.path.join(w, ".git"), "have"))
    return out


def _tidy(made):
    """Remove the stand-ins' mount points a run left (bwrap and docker create them in the project), if still empty."""
    for p in made:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                os.rmdir(p)
            elif os.path.isfile(p) and not os.path.islink(p) and os.path.getsize(p) == 0:
                os.remove(p)
        except OSError:
            pass


def argv(cmd, root, writes=(), net="off", home=None, cwd=None, net_dir=None, nest=False, reads=()):
    """bwrap's argv for cmd: root and each existing folder in writes writable, everything else read-only, credentials
    masked (a credential folder that holds a writable one is masked first, then the writable one bound back over it),
    PROTECTED read-only in root. net allow: no network but the proxy's socket folder (net_dir), bound at NET_DIR.
    nest: the job may make namespaces of its own (run.py --sandbox: tests that run bwrap); else --disable-userns."""
    if net not in NETS:
        raise ValueError(f"net is one of {', '.join(NETS)}, not {net!r}")
    homes = _homes(home)
    root = os.path.realpath(root)
    rw = _writable(root, writes, homes)
    a = [shutil.which("bwrap") or "bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
         "--unshare-cgroup-try", *([] if nest else ["--disable-userns"]), "--die-with-parent", "--new-session",
         "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/run",
         "--perms", "0700", "--dir", f"/run/user/{os.getuid()}"]  # XDG_RUNTIME_DIR: there, empty, private
    if net != "on":
        a.append("--unshare-net")
    if net == "allow":
        a += ["--bind", net_dir, NET_DIR]
    # under a fresh tmpfs, back and read-only: a home there, what the job reads (T-3246: sampo ship's scratch tree is
    # under /tmp) and its working folder
    for r in dict.fromkeys(os.path.realpath(x) for x in [*homes, *reads, cwd or root] if x):
        if any(_under(r, t) for t in ("/tmp", "/run")) and os.path.isdir(r) and not any(_under(r, w) for w in rw):
            a += ["--ro-bind", r, r]
    creds = [p for p in dict.fromkeys(os.path.realpath(os.path.join(h, x)) for h in homes for x in CREDENTIALS)
             if os.path.exists(p)]  # a link to nothing has nothing to read (and nothing to mount over: nested runs)

    def mask(p):
        return ["--tmpfs", p] if os.path.isdir(p) else ["--ro-bind", "/dev/null", p]
    holding = [p for p in creds if any(_under(w, p) for w in rw)]
    for p in holding:
        a += mask(p)
    for w in rw:
        a += ["--bind", w, w]
    for p, kind in _protected(root):
        a += {"pin": ["--bind", p, p], "have": ["--ro-bind", os.path.realpath(p), p],
              "file": ["--ro-bind", "/dev/null", p], "dir": ["--tmpfs", p, "--remount-ro", p]}[kind]
    for p in creds:  # after the binds, so one inside a writable folder is masked all the same
        if p not in holding:
            a += mask(p)
    return a + ["--chdir", os.path.realpath(cwd or root), "--", *cmd]


def docker_argv(cmd, root, image, net="off", writes=(), cwd=None, home=None):
    """docker's argv for cmd in image: no capabilities, no new privileges, a read-only root filesystem with a fresh
    /tmp, this account's uid, only root and writes mounted (at their own paths, PROTECTED read-only), no network unless
    net is on (no allow mode: the proxy is the job tier's). ponytail: a path holding ':' can't be mounted with -v."""
    if net not in ("off", "on"):
        raise ValueError("an image job's network is off or on")
    rw = _writable(root, writes, _homes(home))
    a = ["docker", "run", "--rm", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt",
         "no-new-privileges", "--pids-limit", "1024", "--user", f"{os.getuid()}:{os.getgid()}",
         "--network", "none" if net == "off" else "bridge"]
    for w in rw:
        a += ["-v", f"{w}:{w}"]
    for p, kind in _protected(rw[0]):  # a missing folder: a tmpfs, so what the job writes there never reaches here
        a += {"pin": ["-v", f"{p}:{p}"], "have": ["-v", f"{os.path.realpath(p)}:{p}:ro"],
              "file": ["-v", f"/dev/null:{p}:ro"], "dir": ["--mount", f"type=tmpfs,destination={p}"]}[kind]
    return a + ["-w", os.path.realpath(cwd or rw[0]), image, *cmd]


def run(cmd, root, writes=(), net="off", home=None, cwd=None, env=None, fallback=False, allow=(), image=None, **kw):
    """subprocess.run of cmd in the job tier. Unavailable here: Unavailable(why), or with fallback=True cmd runs plain,
    exactly as it would without Sampo (its environment untouched). net allow: the job reaches only allow, through a
    proxy outside; the result's .net lists (host, port, ok, why) per request, and each refusal is named on stderr.
    image: the job runs in that docker image instead (docker_argv), with no variables from here."""
    base = dict(os.environ if env is None else env)
    if image:
        if not shutil.which("docker"):
            if not fallback:
                raise Unavailable("no docker on PATH for --image")
            return subprocess.run(list(cmd), cwd=cwd or root, env=base, **kw)
        a = docker_argv(cmd, root, image, net, writes, cwd, home)
        go = lambda: subprocess.run(a, env=clean_env(base), **kw)  # noqa: E731
    else:
        ok, why = available()
        if not ok:
            if not fallback:
                raise Unavailable(why)
            return subprocess.run(list(cmd), cwd=cwd or root, env=base, **kw)
        if net == "allow":
            go = lambda: _run_allowed(cmd, root, writes, home, cwd, base, list(allow), kw)  # noqa: E731
        else:
            a = argv(cmd, root, writes, net, home, cwd)
            go = lambda: subprocess.run(a, env=job_env(base, root, writes), **kw)  # noqa: E731
    made = [p for p, kind in _protected(os.path.realpath(root)) if kind in ("dir", "file")]
    try:
        return go()
    finally:
        _tidy(made)


def job_env(base, root, writes=()):
    """base without secrets, TMPDIR the job's own fresh /tmp unless the caller's is in a folder bound in, and Sampo's
    state read-only in place (FOREMAN_STATE_FALLBACK=0: no copy of it all into the job's /tmp)."""
    env = clean_env(base)
    env["FOREMAN_STATE_FALLBACK"] = "0"
    rw = [os.path.realpath(root)] + [os.path.realpath(w) for w in writes]
    tmp = env.get("TMPDIR")
    if not tmp or not any(_under(os.path.realpath(tmp), w) for w in rw):
        env["TMPDIR"] = "/tmp"
    return env


def _run_allowed(cmd, root, writes, home, cwd, base, allow, kw):
    log = []
    with tempfile.TemporaryDirectory(prefix="sampo-net-") as d:
        srv = _Proxy(os.path.join(d, "proxy.sock"), _handler(allow, log))
        threading.Thread(target=srv.serve_forever, args=(0.1,), daemon=True).start()
        try:
            proxy = f"http://127.0.0.1:{RELAY_PORT}"
            env = {k: v for k, v in base.items() if k.lower() not in ("no_proxy", "all_proxy")}
            env.update({k: proxy for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")})
            relay = [sys.executable, "-I", "-c", RELAY, f"{NET_DIR}/proxy.sock", *cmd]
            r = subprocess.run(argv(relay, root, writes, "allow", home, cwd, net_dir=d),
                               env=job_env(env, root, writes), **kw)
        finally:
            srv.shutdown()
            srv.server_close()
    refused = "".join(f"sampo sandbox: refused {h}:{p} ({why})\n" for h, p, ok, why in log if not ok)
    if refused and isinstance(r.stderr, str):
        r.stderr += refused
    elif refused and isinstance(r.stderr, bytes):
        r.stderr += refused.encode()
    elif refused:
        sys.stderr.write(refused)
    r.net = log
    return r


# what Claude Code's sandbox runs outside it: plain ssh can't pass its proxy (git over ssh.github.com:443 among them)
EXCLUDED = ("git push *", "git fetch *", "git pull *", "git clone *", "git ls-remote *", "sampo ship *", "fm ship *",
            "ssh *", "scp *")
GITHUB = ("github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com",
          "raw.githubusercontent.com")


def _settings_path():
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"),
                        "settings.json")


def _backup_path():
    from sampo import core as c
    return os.path.join(c.state_dir(), "sandbox-session.json")


def session_domains(p=None):
    """The hosts a session's Bash may reach: GitHub, the project's list (its remote and package registries, or sampo
    sandbox allow) and the hosts of configured model endpoints that aren't this machine."""
    from sampo import models, serve
    out = list(GITHUB)
    if p is not None:
        out += serve._sandbox_domains(p)
    try:
        hosts = [urlsplit(x.get("base_url") or "").hostname for x in models.configured()[1].values()]
    except Exception:  # noqa: BLE001 - an unreadable models.json names no hosts
        hosts = []
    out += [h for h in hosts if h and h not in ("localhost", "127.0.0.1", "::1")]
    return list(dict.fromkeys(out))


def session_on(p=None):
    """Claude Code's own sandbox in settings.json: enabled, credential folders read-denied, ssh git and sampo ship
    excluded, session_domains allowed. The first on keeps the file as it was, for off. (path, the sandbox written)"""
    from sampo import core as c
    path, bak = os.path.realpath(_settings_path()), _backup_path()  # a linked settings.json is written through it
    before = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    data = json.loads(before) if before and before.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} isn't a JSON object")
    if os.path.exists(bak):
        rec = json.load(open(bak, encoding="utf-8"))
        if before != rec.get("wrote"):  # edited since the last on: off puts back this, with the sandbox key as it was
            kept = {k: v for k, v in data.items() if k != "sandbox"}
            if rec.get("had"):
                kept["sandbox"] = rec.get("prior")
            rec["before"] = json.dumps(kept, indent=2, ensure_ascii=False) + "\n"
    else:
        rec = {"path": path, "before": before, "had": "sandbox" in data, "prior": data.get("sandbox")}
    box = {"enabled": True, "filesystem": {"denyRead": ["~/" + x for x in CREDENTIALS]},
           "excludedCommands": list(EXCLUDED), "network": {"allowedDomains": session_domains(p)}}
    data["sandbox"] = box = _merge(rec.get("prior") if rec.get("had") else None, box)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c.write_atomic(path, text)
    rec["wrote"] = text
    os.makedirs(os.path.dirname(bak), exist_ok=True)
    c.write_atomic(bak, json.dumps(rec))
    return path, box


def _merge(old, new):
    """new's keys over old's: dicts merged, lists unioned (old's entries first), old's other keys kept."""
    out = dict(old) if isinstance(old, dict) else {}
    for k, v in new.items():
        if isinstance(v, dict):
            out[k] = _merge(out.get(k), v)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = list(dict.fromkeys(out[k] + v))
        else:
            out[k] = v
    return out


def session_off():
    """Undo session_on: the file as it was when nothing changed since, else only its sandbox key put back. The path,
    or None when on never ran here."""
    from sampo import core as c
    bak = _backup_path()
    if not os.path.exists(bak):
        return None
    rec = json.load(open(bak, encoding="utf-8"))
    path, before = rec["path"], rec.get("before")
    now = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    if now == rec.get("wrote"):
        if before is None:
            os.remove(path)
        else:
            c.write_atomic(path, before)
    elif now is not None:
        data = json.loads(now) if now.strip() else {}
        data.pop("sandbox", None)
        if rec.get("had"):
            data["sandbox"] = rec.get("prior")
        c.write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.remove(bak)
    return path


def _session(settings):
    try:
        with open(settings, encoding="utf-8") as f:
            box = (json.load(f) or {}).get("sandbox") or {}
    except (OSError, ValueError, AttributeError):
        box = {}
    return bool(box.get("enabled")) if isinstance(box, dict) else False


def status():
    """[(layer, live, what it confines or why not)] for the job tier, the session tier, Landlock and docker."""
    from sampo import landlock
    ok, why = available()
    ll_ok, ll_why = landlock.available()
    on = _session(_settings_path())
    return [("job", ok, f"{why}: sampo sandbox run confines a job's writes, credential reads, network and processes"
             if ok else why),
            ("session", on, "Claude Code's sandbox confines this session's Bash commands" if on
             else "Claude Code's sandbox is off for this session's Bash commands"),
            ("landlock", ll_ok, f"{ll_why}: fm code workers write only in their run's tree" if ll_ok else ll_why),
            ("docker", bool(shutil.which("docker")), "present; used only when a job names an image (--image)"
             if shutil.which("docker") else "not installed (never needed)")]


def cmd_sandbox(args):
    from sampo import cli, serve
    if args.action in ("on", "off") and args.session:
        if args.action == "on":
            try:
                p = cli.resolve(args)
            except Exception:  # noqa: BLE001 - outside a project: GitHub and the model hosts
                p = None
            path, box = session_on(p)
            print(f"Claude Code's sandbox is on for your sessions ({path}): credential folders unreadable, "
                  f"{len(box['network']['allowedDomains'])} domains allowed, ssh git and sampo ship run outside it; "
                  "it applies from the next session. sampo sandbox off --session puts the file back.")
        else:
            path = session_off()
            print(f"Claude Code's sandbox settings are back as they were ({path})." if path
                  else "Nothing to undo: sampo sandbox on --session hasn't run here.")
        return 0
    if args.action in ("on", "off", "allow", "show"):  # T-1086: sampo run's Claude Code sandbox, a project setting
        args.domains = list(args.rest or [])
        return serve.cmd_sandbox(args)
    if args.action == "status":
        rows = status()
        if args.json:
            print(json.dumps([{"layer": n, "live": ok, "what": w} for n, ok, w in rows]))
        else:
            for n, ok, w in rows:
                print(f"{'✓' if ok else '·'} {n}: {w}")
        return 0
    cmd = list(args.rest or [])
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if not cmd:
        raise cli.UsageError("sampo sandbox run -- COMMAND [ARGS...]")
    root = os.path.realpath(args.root or os.getcwd())
    try:
        allow = list(args.allow or [])
        if args.net == "allow":
            try:
                allow += serve._sandbox_domains(cli.resolve(args))
            except Exception:  # noqa: BLE001 - outside a project: the --allow hosts alone
                pass
        return run(cmd, root, writes=args.write or (), net=args.net, allow=allow, image=args.image).returncode
    except ValueError as e:
        raise cli.UsageError(f"sampo sandbox run: {e}")
    except Unavailable as e:
        print(f"sampo: not run: the job sandbox isn't available here: {e}", file=sys.stderr)
        return 3
