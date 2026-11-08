"""T-3160: calling cards, verified generators a small model calls by name with typed inputs.

A card is a folder: card.json (name, purpose, examples, kind py|template, an inputs JSON schema, the files it reads or
changes, the files it may create), gen.py (or templates) and check.py. A call checks the inputs against the schema
before anything runs, copies the files in scope to a scratch folder, runs the generator there and the checker over its
output, both confined (landlock.prepare_jail: they read only the interpreter, the card and the scratch copy and write
only scratch; landlock.syscall_filter: no socket, ptrace, setsid, or below Landlock ABI 6 signal; a clean env; rlimits
on file size, memory, CPU and processes; a cap on the scratch's bytes; a timeout), and writes to the worktree exactly
the bytes the checker passed, only while the files are still what the generator saw. Without that confinement nothing
runs. A Session bounds a model's use of cards (three failed calls per card, a repeat stops the card, a deterministic
card never retries, one escalation), and pick() ranks the cards for a task or files the miss as a card request."""
import base64
import difflib
import fnmatch
import glob
import hashlib
import html
import importlib.util
import json
import keyword
import math
import os
import platform
import re
import resource
import shutil
import signal
import stat
import string
import subprocess
import sys
import tempfile
import time
import types

from sampo import core as c
from sampo import landlock

TIMEOUT = 60  # seconds per generator or checker run
FSIZE = 32 << 20  # the largest file a run may write (RLIMIT_FSIZE: its stdout and stderr files too)
MEMORY = 4 << 30  # RLIMIT_AS
NPROC = 1024  # the processes a run may add to its user's (RLIMIT_NPROC counts every one the uid has, so a busy test run's own jobs eat a small margin: 256 failed a plain fork under 58 parallel jobs; playbooks.PROCS_HEADROOM is the same)
SCRATCH_CAP = 512 << 20  # the bytes a run's whole scratch folder may hold
OUT_CAP = 64 << 10  # what is read back of a run's stdout or stderr
FILE_CAP = 2 << 20  # the largest file a card takes in
MAX_FILES, MAX_BYTES = 3000, 32 << 20  # a call's scope
ATTEMPTS = 3  # failed calls per card in a Session
CACHE_KEEP, CACHE_WRITES = 256, 1 << 20  # cached results kept; the most a cached result's files may hold
DIFF_CAP = 8 << 10
TEXT_CAP = 8000  # text(): what a model reads back from one call
LEXICAL_FLOOR = 0.15  # pick(): a card scoring under this doesn't fit
OK = ("applied", "facts")
RUN_FAILED = ("generator_failed", "checker_failed", "timeout", "scope")
NAME = re.compile(r"[a-z][a-z0-9-]{1,40}")
# fmguard._is_credential (what Smith.hidden uses) decides; this adds the names it judges only under home
SECRET = re.compile(r"(^|/)(\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.ssh/.*|\.aws/.*|\.gnupg/.*)$")
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".fm-scratch"}
LIB = os.path.join(c.PLUGIN_ROOT, "cards", "_lib")
# the child's sys.path is the card's folder and the shared helpers only: never the worktree's files (a planted json.py)
_BOOT = ("import runpy, sys; sys.path[:0] = sys.argv[1:3]; script = sys.argv[3]; sys.argv = [script]; "
         "runpy.run_path(script, run_name='__main__')")
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def _result(card, status, **kw):
    r = {"card": card, "status": status, "ok": status in OK, "changed": [], "created": [], "facts": None, "error": None,
         "attempt": None, "cached": False, "escalated": False, "diff": ""}
    r.update(kw)
    return r


def _near(word, options, cutoff=0.0):
    m = difflib.get_close_matches(str(word), [str(o) for o in options], n=1, cutoff=cutoff)
    return m[0] if m else None


# --- cards ---------------------------------------------------------------------------------------------------------

def card_dirs(root=None):
    """Sampo's own cards, then each folder in SAMPO_CALLCARDS (os.pathsep-separated; core.mirror_env renames it
    FOREMAN_CALLCARDS at startup) that is absolute and outside root: a repo's own files never become a card."""
    extra = os.environ.get("SAMPO_CALLCARDS") or os.environ.get("FOREMAN_CALLCARDS") or ""
    inside = os.path.realpath(root) if root else None
    out = [os.path.join(c.PLUGIN_ROOT, "cards")]
    for d in extra.split(os.pathsep):
        if not os.path.isabs(d):
            continue
        real = os.path.realpath(d)
        if inside and os.path.commonpath([real, inside]) in (real, inside):
            continue  # inside the repo, or the repo inside it
        out.append(d)
    return out


def _digest(d, h=None):
    h = h or hashlib.sha256()
    for top, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for n in sorted(files):
            p = os.path.join(top, n)
            with open(p, "rb") as f:
                h.update(os.path.relpath(p, d).encode() + b"\0" + hashlib.sha256(f.read()).digest())
    return h


def _strings(spec, key):
    v = spec.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{key} must be a list of strings")
    return v


def _load(d):
    """A card from its folder, or ValueError naming what's wrong."""
    with open(os.path.join(d, "card.json")) as f:
        spec = json.load(f)
    if not isinstance(spec, dict):
        raise ValueError("card.json isn't an object")
    name, kind = spec.get("name"), spec.get("kind", "py")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"name {name!r}: lowercase words joined by '-'")
    if kind not in ("py", "template"):
        raise ValueError(f"kind {kind!r}: py or template")
    for fn in ["check.py"] + (["gen.py"] if kind == "py" else []):
        if not os.path.isfile(os.path.join(d, fn)):
            raise ValueError(f"no {fn}")
    inputs = spec.get("inputs")
    if not isinstance(inputs, dict) or inputs.get("type") != "object" or not isinstance(inputs.get("properties", {}),
                                                                                       dict):
        raise ValueError("inputs must be a JSON schema of type object")
    render = spec.get("render", {})
    if kind == "template" and (not isinstance(render, dict) or not render or not all(
            isinstance(t, str) and os.path.basename(t) == t and os.path.isfile(os.path.join(d, t))
            for t in render.values())):
        raise ValueError("a template card's render maps each target to a template file in its folder")
    for key in ("files", "creates", "examples"):
        _strings(spec, key)
    lib = _digest(LIB) if os.path.isdir(LIB) else hashlib.sha256()
    return dict(spec, name=name, kind=kind, dir=d, read=bool(spec.get("read")), render=render,
                deterministic=bool(spec.get("deterministic")), digest=_digest(d, lib).hexdigest())


def _stamp(bases):
    """Every file's (path, mtime, ctime, size, inode) under bases and LIB: what cards() is cached by."""
    out, tops = [tuple(bases)], [LIB]
    for base in bases:
        try:
            tops += [os.path.realpath(os.path.join(base, n)) for n in sorted(os.listdir(base))]  # a card may be a link
        except OSError:
            continue
    for t in tops:
        paths = [t]
        for top, dirs, files in os.walk(t):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            paths += [os.path.join(top, n) for n in sorted(files)]
        for p in paths:
            try:
                s = os.stat(p)
            except OSError:
                continue
            out.append((p, s.st_mtime_ns, s.st_ctime_ns, s.st_size, s.st_ino))
    return out


_CARDS = [None, None]  # the last (stamp, (found, broken))


def cards(root=None):
    """({name: card}, {folder name: why it didn't load}); the first folder holding a name wins. Read again only when a
    file in a card folder changes (its stat, not its bytes, is compared on each call)."""
    bases = card_dirs(root)
    stamp = _stamp(bases)
    if _CARDS[0] != stamp:
        _CARDS[:] = [stamp, _cards(bases)]
    found, broken = _CARDS[1]
    return {k: dict(v) for k, v in found.items()}, dict(broken)


def _cards(bases):
    found, broken = {}, {}
    for base in bases:
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for n in names:
            d = os.path.realpath(os.path.join(base, n))
            if n[:1] in "._" or not os.path.isfile(os.path.join(d, "card.json")):
                continue
            try:
                card = _load(d)
            except (OSError, ValueError) as e:
                broken[n] = str(e)
                continue
            if card["name"] in found:
                broken[n] = f"a second card named {card['name']!r} in {d}; the one in {found[card['name']]['dir']} wins"
            else:
                found[card["name"]] = card
    return found, broken


def summary(card, score=None):
    out = {"name": card["name"], "purpose": card.get("purpose", ""), "read": card["read"], "inputs": card["inputs"],
           "examples": card.get("examples", [])[:3]}
    if score is not None:
        out["score"] = round(score, 3)
    return out


# --- inputs ----------------------------------------------------------------------------------------------------------

def _rel_ok(rel):
    """Why a relative path (or a glob pattern) can't be a card's file, or None."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or os.path.isabs(rel) or rel.startswith("~"):
        return "isn't a relative path inside the worktree"
    norm = os.path.normpath(rel)
    parts = norm.split(os.sep)
    if parts[0] in ("..", ".") or ".git" in parts:
        return "is outside the worktree or inside .git"
    import fmguard  # plugin/lib, beside sampo/
    if SECRET.search(norm) or fmguard._is_credential(os.sep + norm, _CRED_CTX):
        return "looks like a credential file; cards never touch one"
    return None


_CRED_CTX = types.SimpleNamespace(home=os.path.expanduser("~"), scratch=())


def _regular(root, rel):
    """True when root/rel is a regular file reached without a symlink."""
    full = os.path.join(root, os.path.normpath(rel))
    try:
        return stat.S_ISREG(os.lstat(full).st_mode) and os.path.realpath(full) == full
    except OSError:
        return False


def _new_ok(root, rel):
    """Why rel can't be created in root, or None: its nearest existing folder must be a real one inside root."""
    why = _rel_ok(rel)
    if why:
        return why
    full = os.path.join(root, os.path.normpath(rel))
    if os.path.lexists(full):
        return "already exists"
    d = os.path.dirname(full)
    while not os.path.lexists(d):
        d = os.path.dirname(d)
    if os.path.realpath(d) != d or not os.path.isdir(d) or not (d + os.sep).startswith(root + os.sep) and d != root:
        return "would be created through a symlink or outside the worktree"
    return None


def _check(value, schema, where, root, errors):
    t = schema.get("type")
    if t in _TYPES and (not isinstance(value, _TYPES[t]) or (t in ("integer", "number") and isinstance(value, bool))):
        errors.append(f"{where} must be {'an' if t[0] in 'aeiou' else 'a'} {t}, not {type(value).__name__} "
                      f"{json.dumps(value, default=str)[:40]}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where} {json.dumps(value, default=str)[:40]} isn't one of {json.dumps(schema['enum'])}: "
                      f"did you mean {_near(value, schema['enum'])!r}?")
        return
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", 10_000):
            errors.append(f"{where} is {len(value)} characters; at most {schema.get('maxLength', 10_000)}")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{where} is {len(value)} characters; at least {schema['minLength']}")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            errors.append(f"{where} {value[:40]!r} doesn't match {schema['pattern']}")
        fmt = schema.get("format")
        if fmt == "identifier" and (not value.isidentifier() or keyword.iskeyword(value)):
            errors.append(f"{where} {value[:40]!r} isn't a Python identifier")
        elif fmt == "path":
            why = _rel_ok(value)
            if why is None and not _regular(root, value):
                rel = os.path.normpath(value)
                if os.path.lexists(os.path.join(root, rel)):
                    why = "is a symlink, a folder or reached through a symlink; cards take regular files only"
                else:
                    near = _near(value, repo_files(root)[:20_000], 0.6)
                    why = "doesn't exist" + (f": did you mean {near!r}?" if near else "")
            if why:
                errors.append(f"{where} {value[:80]!r} {why}")
        elif fmt in ("newpath", "glob"):  # a glob: a pattern over the worktree's files, never outside it
            why = _new_ok(root, value) if fmt == "newpath" else _rel_ok(value)
            if why:
                errors.append(f"{where} {value[:80]!r} {why}")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where} {value} is under the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where} {value} is over the maximum {schema['maximum']}")
    elif isinstance(value, list):
        if len(value) > schema.get("maxItems", 100) or len(value) < schema.get("minItems", 0):
            errors.append(f"{where} has {len(value)} items; {schema.get('minItems', 0)}-{schema.get('maxItems', 100)}")
            return
        for i, v in enumerate(value):
            _check(v, schema.get("items", {}), f"{where}[{i}]", root, errors)


def check_inputs(card, inputs, root):
    """(the inputs with defaults filled, [errors]): every error names what would be valid."""
    if not isinstance(inputs, dict):
        return inputs, ["inputs must be an object of named values"]
    props, errors = card["inputs"].get("properties", {}), []
    for k in inputs:
        if k not in props:
            near = _near(k, props)
            errors.append(f"unknown input {k!r}" + (f": did you mean {near!r}?" if near else f"; this card takes none"))
    for k in card["inputs"].get("required", []):
        if k not in inputs:
            errors.append(f"{k!r} is required")
    for k, v in inputs.items():
        if k in props:
            _check(v, props[k], repr(k), root, errors)
    clean = {k: s["default"] for k, s in props.items() if isinstance(s, dict) and "default" in s}
    clean.update(inputs)
    return clean, errors


# --- scope -----------------------------------------------------------------------------------------------------------

def repo_files(root):
    """The worktree's files: git's tracked and unignored ones, else a walk past the usual heavy folders."""
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, timeout=30)
        if r.returncode == 0:
            return sorted({x for x in r.stdout.decode("utf-8", "surrogateescape").split("\0") if x})
    except (OSError, subprocess.TimeoutExpired):
        pass
    out = []
    for top, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        out += [os.path.relpath(os.path.join(top, n), root) for n in files]
        if len(out) > 100_000:
            break
    return sorted(out)


def _globs(card):
    """The inputs a card declares as glob patterns (format glob, or an array of them): only these stay patterns."""
    out = set()
    for k, s in card.get("inputs", {}).get("properties", {}).items():
        if isinstance(s, dict) and (s.get("format") == "glob" or (isinstance(s.get("items"), dict)
                                                                  and s["items"].get("format") == "glob")):
            out.add(k)
    return out


def _expand(patterns, inputs, card=None):
    """Each pattern with its $inputs filled; a pattern that is one list input gives one pattern per item. Given the
    card, each input value is a literal name (its * ? [ escaped) unless the card declares that input a glob."""
    keep = _globs(card) if card is not None else None

    def val(k, v):
        return v if keep is None or k in keep else glob.escape(v)
    out = []
    for p in patterns:
        m = re.fullmatch(r"\$(\w+)", p)
        if m:
            v = inputs.get(m.group(1))
            out += [val(m.group(1), x) for x in (v if isinstance(v, list) else [v]) if isinstance(x, str)]
        else:
            out.append(string.Template(p).safe_substitute({k: val(k, v) for k, v in inputs.items()
                                                           if isinstance(v, str)}))
    return out


def _match(rel, pattern):
    return fnmatch.fnmatchcase(rel, pattern) or (pattern.startswith("**/") and fnmatch.fnmatchcase(rel, pattern[3:]))


def _scope(card, inputs, root):
    """{rel: bytes} of the files the card reads or changes; ValueError when they are too many or too big."""
    rels, listing = {}, None
    for pat in _expand(card.get("files", []), inputs, card):
        lit = re.sub(r"\[(.)\]", r"\1", pat)
        if glob.escape(lit) != pat:  # a pattern, not one name (an escaped input is one name)
            listing = repo_files(root) if listing is None else listing
            hits = [f for f in listing if _match(f, pat)]
        else:
            hits = [os.path.normpath(lit)]
        for h in hits:
            if h not in rels and _rel_ok(h) is None and _regular(root, h):
                rels[h] = None
                if len(rels) > MAX_FILES:
                    raise ValueError(f"over {MAX_FILES} files in the card's scope; name fewer")
    total = 0
    for rel in rels:
        size = os.lstat(os.path.join(root, rel)).st_size
        if size > FILE_CAP:
            raise ValueError(f"{rel} is {size} bytes; a card takes files up to {FILE_CAP >> 20} MB")
        total += size
        if total > MAX_BYTES:
            raise ValueError(f"the card's files pass {MAX_BYTES >> 20} MB; name fewer")
        with open(os.path.join(root, rel), "rb") as f:
            rels[rel] = f.read(FILE_CAP + 1)
    return rels


# --- the confined run --------------------------------------------------------------------------------------------------

def confinement():
    """(True, how) when a card's run can be confined here, else (False, why): then calls refuse to run anything."""
    ok, why = landlock.available()
    if not ok:
        return False, why
    if landlock.syscall_filter() is None:
        return False, f"no seccomp syscall numbers are known for {platform.machine()}, so sockets can't be refused"
    v = landlock.abi()
    return True, f"Landlock {why}, sockets refused by seccomp" + (
        ", TCP refused by Landlock" if v >= landlock.NET_ABI else "") + (
        ", signals scoped by Landlock" if v >= landlock.SCOPE_ABI else ", signals refused by seccomp")


def _system_reads():
    """What the interpreter needs to start: never home or an ancestor of it (~/.config holds the secrets)."""
    home = os.path.realpath(os.path.expanduser("~"))
    out = []
    for p in ("/usr", "/lib", "/lib64", "/bin", "/etc/ld.so.cache", "/etc/localtime", "/dev/urandom", sys.prefix,
              sys.base_prefix, os.path.dirname(os.path.realpath(sys.executable))):
        p = os.path.realpath(p)
        if os.path.exists(p) and p not in out and os.path.commonpath([p, home]) != p:
            out.append(p)
    return out


_SIGNALS = {signal.SIGXFSZ: f"wrote a file over {FSIZE >> 20} MB", signal.SIGXCPU: "used up its CPU time",
            signal.SIGKILL: "was killed (out of memory?)", signal.SIGSYS: "made a forbidden system call"}


def _tasks():
    """The tasks (processes and threads) this uid runs now: what RLIMIT_NPROC counts."""
    uid, n = os.getuid(), 0
    for pid in os.listdir("/proc"):
        try:
            if pid.isdigit() and os.stat(f"/proc/{pid}").st_uid == uid:
                n += len(os.listdir(f"/proc/{pid}/task"))
        except OSError:
            pass
    return n


def _du(top, cap):
    """True once the files beneath top hold more than cap bytes (stops counting there)."""
    total = 0
    for base, dirs, files in os.walk(top):
        for n in dirs + files:
            try:
                total += os.lstat(os.path.join(base, n)).st_size
            except OSError:
                pass
            if total > cap:
                return True
    return False


def _spawn(argv, cwd, stdin, reads, writes, logs, timeout):
    """One confined run: {rc, out, err, timed_out, over, jail}. over: the scratch (logs) held more than SCRATCH_CAP;
    jail names a confinement that couldn't be entered.
    ponytail: the scratch is measured every 0.25 s, so a fast writer can pass the cap by what it writes in that time
    (FSIZE bounds each file); a filesystem quota would be exact."""
    rs, prog = landlock.prepare_jail(reads, writes), landlock.syscall_filter()
    home = os.path.join(logs, "tmp")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": home, "TMPDIR": home, "PYTHONDONTWRITEBYTECODE": "1"}
    cpu = int(timeout) + 2
    nproc = _tasks() + NPROC  # ponytail: the uid's other processes may start or end meanwhile; NPROC is the margin

    def jail():  # in the child, after fork: limits, then seccomp and Landlock (nothing is loaded here)
        resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE, FSIZE))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY, MEMORY))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
        landlock.enter_jail(rs, prog)

    fo, out_p = tempfile.mkstemp(dir=logs, suffix=".out")
    fe, err_p = tempfile.mkstemp(dir=logs, suffix=".err")
    timed, over, rc = False, False, None
    try:
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=fo, stderr=fe, pass_fds=(rs,),
                                 start_new_session=True, preexec_fn=jail)
        except (OSError, subprocess.SubprocessError) as e:
            return {"rc": None, "out": "", "err": "", "timed_out": False, "over": False,
                    "jail": f"the confinement failed: {e}"}
        deadline, data = time.monotonic() + timeout, stdin.encode("utf-8")
        try:
            while True:
                try:
                    p.communicate(data, timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    data = None  # communicate takes the input once
                over = _du(logs, SCRATCH_CAP)
                if over:
                    break
                if time.monotonic() > deadline:
                    timed = True
                    break
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)  # seccomp keeps all it started in this group: they go with it
            except (ProcessLookupError, PermissionError):
                pass
            rc = p.wait()
        over = over or _du(logs, SCRATCH_CAP)
    finally:
        os.close(rs)
        os.close(fo)
        os.close(fe)
    return {"rc": rc, "out": _head_tail(out_p), "err": _head_tail(err_p), "timed_out": timed, "over": over,
            "jail": None}


def _head_tail(path):
    """The first OUT_CAP bytes, and the last 2 KB when there is more (an error is at the end)."""
    with open(path, "rb") as f:
        head = f.read(OUT_CAP)
        size = f.seek(0, os.SEEK_END)
        tail = b""
        if size > OUT_CAP:
            f.seek(max(OUT_CAP, size - 2048))
            tail = b"\n[...]\n" + f.read(2048)
    return (head + tail).decode("utf-8", "replace")


def _why(run, who):
    if run.get("over"):
        return f"the {who} wrote over {SCRATCH_CAP >> 20} MB in its scratch"
    if run["rc"] is not None and run["rc"] < 0:
        sig = -run["rc"]
        what = _SIGNALS.get(sig, f"was killed by signal {sig}")
        return f"the {who} {what}"
    text = (run["out"][-1200:].strip() + "\n" + run["err"][-1200:].strip()).strip()
    return text[-2000:] or f"the {who} exited {run['rc']} and said nothing"


def _argv(card, script):
    return [sys.executable, "-I", "-B", "-c", _BOOT, card["dir"], LIB, os.path.join(card["dir"], script)]


def _render(card, inputs, work):
    """A template card: each target from its template, inputs XML-escaped for markup; parts pick a fixed snippet."""
    parts = card.get("parts", {})
    for target, tmpl in card["render"].items():
        rel = _expand([target], inputs)[0]
        if _rel_ok(rel):
            raise ValueError(f"render target {rel!r} {_rel_ok(rel)}")
        markup = rel.endswith((".svg", ".xml", ".html", ".htm"))
        ctx = {}
        for k, v in inputs.items():
            if isinstance(parts.get(k), dict) and str(v) in parts[k]:
                ctx[k] = parts[k][str(v)]
            else:
                ctx[k] = html.escape(str(v), quote=True) if markup else str(v)
        with open(os.path.join(card["dir"], tmpl)) as f:
            text = string.Template(f.read()).substitute(ctx)
        p = os.path.join(work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)


def _diff(work, before, creates, root):
    """({rel: bytes} changed or created, why): read once, so what is checked and applied is this snapshot, whatever a
    process left behind does to work afterwards. why names a deletion, a symlink or a file outside the card's scope."""
    writes, seen = {}, set()
    for top, dirs, files in os.walk(work):
        for n in sorted(dirs) + sorted(files):
            p = os.path.join(top, n)
            rel, mode = os.path.relpath(p, work), os.lstat(p).st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                return {}, f"{rel} isn't a regular file (a symlink, a socket or a device)"
            seen.add(rel)
            if rel not in before and not (any(_match(rel, pat) for pat in creates) and _new_ok(root, rel) is None):
                return {}, f"{rel} is outside the card's scope"
            with open(p, "rb") as f:
                data = f.read(FSIZE + 1)
            if before.get(rel) != data:
                writes[rel] = data
    gone = sorted(set(before) - seen)
    if gone:
        return {}, f"{gone[0]} was deleted"
    return writes, None


def _unified(before, writes):
    out = []
    for rel, data in sorted(writes.items()):
        a = before.get(rel, b"").decode("utf-8", "replace").splitlines(keepends=True)
        b = data.decode("utf-8", "replace").splitlines(keepends=True)
        out += difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}")
    text = "".join(out)
    return text if len(text) <= DIFF_CAP else text[:DIFF_CAP] + "\n[diff cut]\n"


def _apply(root, before, writes):
    """Write the checked files, only while every file the generator saw is unchanged: the reason it didn't, or None.
    ponytail: a file edited between the check and its os.replace is overwritten; a lock shared with editors would
    close that window."""
    for rel, data in before.items():
        try:
            cur = open(os.path.join(root, rel), "rb").read() if _regular(root, rel) else None
        except OSError:
            cur = None
        if cur != data:
            return f"{rel} changed during the call, so the result wasn't applied: call again on the new text"
    for rel in writes:
        if rel not in before and _new_ok(root, rel):
            return f"{rel} {_new_ok(root, rel)}: the result wasn't applied"
    done, made = [], []
    try:
        for rel, data in sorted(writes.items()):
            p = os.path.join(root, rel)
            d, new = os.path.dirname(p), []
            while not os.path.lexists(d):  # the folders this write makes, so a rollback removes them too
                new.append(d)
                d = os.path.dirname(d)
            made += new
            os.makedirs(os.path.dirname(p), exist_ok=True)
            done.append(rel)  # before the write: a write that fails half way is put back too
            c.write_atomic(p, data)
    except BaseException as e:
        for rel in done:  # put back what was written: all of it lands or none
            try:
                c.write_atomic(os.path.join(root, rel), before[rel]) if rel in before else os.unlink(
                    os.path.join(root, rel))
            except OSError:
                pass
        for d in sorted(made, key=len, reverse=True):
            try:
                os.rmdir(d)
            except OSError:
                pass
        if not isinstance(e, Exception):
            raise
        return f"writing the result failed ({e}); nothing was kept"
    return None


def _run(card, inputs, root, before, inject, inject_facts=None):
    scratch = tempfile.mkdtemp(prefix="sampo-card-")
    try:
        orig, work, tmp = (os.path.join(scratch, x) for x in ("orig", "work", "tmp"))
        for d in (orig, work, tmp):
            os.mkdir(d, 0o700)
        for rel, data in before.items():
            for base in (orig, work):
                p = os.path.join(base, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
        reads = _system_reads() + [card["dir"], LIB, scratch]
        name, facts, gen = card["name"], None, None
        if inject is not None or inject_facts is not None:  # a card's own test: this stands in for the generator
            for rel, text in (inject or {}).items():
                if _rel_ok(rel):
                    return _result(name, "bad_input", error=f"inject {rel!r} {_rel_ok(rel)}")
                p = os.path.join(work, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(text)
        elif card["kind"] == "template":
            try:
                _render(card, inputs, work)
            except (KeyError, ValueError, OSError) as e:
                return _result(name, "generator_failed", error=f"the template couldn't be filled: {e!r}")
        else:
            gen = _spawn(_argv(card, "gen.py"), work, json.dumps({"inputs": inputs, "files": sorted(before)}), reads,
                         [work, tmp, "/dev/null"], scratch, TIMEOUT)
            failed = _failed(name, gen, "generator")
            if failed:
                return failed
        writes, why = _diff(work, before, _expand(card.get("creates", []), inputs, card), root)
        changed, created = sorted(r for r in writes if r in before), sorted(r for r in writes if r not in before)
        if why or (card["read"] and writes):
            return _result(name, "scope", error=why or f"a read card changed {sorted(writes)[0]}")
        view = os.path.join(scratch, "view")  # the snapshot the checker reads and the call applies, nothing later
        for rel, data in list(before.items()) + list(writes.items()):
            p = os.path.join(view, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        if inject_facts is not None:
            facts = inject_facts
        elif card["read"]:
            out = gen["out"] if gen else ""
            try:
                facts = json.loads(out)
            except ValueError:
                facts = out
        with open(os.path.join(card["dir"], "check.py")) as f:
            checks = f.read().strip()
        if checks:
            chk = _spawn(_argv(card, "check.py"), view, json.dumps({"inputs": inputs, "files": sorted(before),
                                                                    "changed": changed, "created": created,
                                                                    "orig": orig, "facts": facts}),
                         reads, [tmp, "/dev/null"], scratch, TIMEOUT)
            failed = _failed(name, chk, "checker")
            if failed:
                return failed
        if card["read"]:
            return _result(name, "facts", facts=facts)
        return _result(name, "applied", changed=changed, created=created, diff=_unified(before, writes),
                       writes=writes)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _failed(name, run, who):
    """The typed result of a generator or checker run that failed, or None. A run that says
    'environment: missing X' or can't import a module failed for want of X here, not because of its input."""
    if run["jail"]:
        return _result(name, "confinement", error=run["jail"])
    if run["over"]:
        return _result(name, f"{who}_failed", error=_why(run, who))
    if run["timed_out"]:
        return _result(name, "timeout", error=f"the {who} ran past {TIMEOUT} s")
    if run["rc"] == 0:
        return None
    m = re.search(r"^environment: (missing .+)$", run["err"], re.M) or re.search(
        r"ModuleNotFoundError: No module named '([^']+)'", run["err"])
    if m:
        what = m.group(1) if m.group(1).startswith("missing ") else f"missing {m.group(1).split('.')[0]}"
        return _result(name, "environment", error=f"{what[:200]}: install it, or do this without the card")
    return _result(name, f"{who}_failed", error=_why(run, who))


# --- the cache -------------------------------------------------------------------------------------------------------

def _cache_dir():
    return os.path.join(c.state_dir(), "callcards", "cache")


def _env():
    """What a card's result may depend on besides its inputs and files: the interpreter and whether pytest is here."""
    spec = importlib.util.find_spec("pytest")
    return [sys.executable, sys.version, spec.origin if spec else None]


def _key(card, inputs, before):
    blob = json.dumps([card["name"], card["digest"], _env(), inputs,
                       {k: hashlib.sha256(v).hexdigest() for k, v in before.items()}], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _cache_get(key):
    try:
        with open(os.path.join(_cache_dir(), key + ".json")) as f:
            row = json.load(f)
        writes = {k: base64.b64decode(v) for k, v in row.get("writes", {}).items()}
        return row["result"], writes
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _cache_put(key, r, writes):
    if sum(len(v) for v in writes.values()) > CACHE_WRITES:
        return
    keep = {k: v for k, v in r.items() if k not in ("writes", "attempt", "cached", "escalated")}
    try:
        c.write_atomic(os.path.join(_cache_dir(), key + ".json"), json.dumps(
            {"at": c.now(), "result": keep, "writes": {k: base64.b64encode(v).decode() for k, v in writes.items()}}))
        rows = sorted(os.scandir(_cache_dir()), key=lambda e: e.stat().st_mtime)
        for e in rows[:-CACHE_KEEP]:
            os.unlink(e.path)
    except OSError:
        pass  # a cache that can't be written is a cache miss next time, nothing worse


# --- calls -----------------------------------------------------------------------------------------------------------

def call(name, inputs, root, *, cache=True, inject=None, inject_facts=None, guard=None):
    """Call a card: the result's status is applied or facts (ok), else bad_input, no_card, confinement, environment
    (something the card needs isn't installed here), generator_failed, checker_failed, timeout, scope, stale or
    refused, with an error that says what to change. guard({rel: bytes}) runs before a checked change lands: a reason
    it may not (the caller's own write rules), or None."""
    root = os.path.realpath(root)
    found, broken = cards(root)
    card = found.get(name)
    if card is None:
        near = _near(name, found, 0.0) if found else None
        why = f"no card is named {name!r}" + (f" ({broken[name]})" if name in broken else "")
        return _result(name, "no_card", error=why + (f": did you mean {near!r}?" if near else ""))
    clean, errors = check_inputs(card, inputs, root)
    if errors:
        return _result(name, "bad_input", error="; ".join(errors))
    ok, why = confinement()
    if not ok:
        return _result(name, "confinement", error=f"cards run only confined, and here {why}")
    try:
        before = _scope(card, clean, root)
    except (ValueError, OSError) as e:
        return _result(name, "bad_input", error=str(e))
    key = _key(card, clean, before)
    testing = inject is not None or inject_facts is not None
    hit = _cache_get(key) if cache and not testing else None
    if hit:
        r, writes = hit
        r = _result(name, r.get("status", "applied"), **{k: v for k, v in r.items() if k not in ("status", "card")},
                    cached=True)
        if writes:
            stale = _guarded(guard, writes) or _apply(root, before, writes)
            if stale:
                return _result(name, "refused" if stale.startswith(_REFUSED) else "stale", error=stale)
        return r
    r = _run(card, clean, root, before, inject, inject_facts)
    writes = r.pop("writes", {})
    if r["ok"] and writes:
        stale = _guarded(guard, writes) or _apply(root, before, writes)
        if stale:
            return _result(name, "refused" if stale.startswith(_REFUSED) else "stale", error=stale)
    if r["ok"] and cache and not testing:
        _cache_put(key, r, writes)
    return r


_REFUSED = "the change was checked but not applied: "


def _guarded(guard, writes):
    if guard is None:
        return None
    try:
        why = guard(writes)
    except Exception as e:  # a guard that breaks refuses: a write it couldn't judge never lands
        why = f"the write guard failed ({e!r})"
    return _REFUSED + str(why) if why else None


def enabled():
    """False when SAMPO_CARDS=off (core.mirror_env renames it FOREMAN_CARDS): fm code offers no cards then."""
    return (os.environ.get("SAMPO_CARDS") or os.environ.get("FOREMAN_CARDS") or "on").lower() not in ("off", "0",
                                                                                                   "no")


def harness_enabled():
    """T-3181: fm code's harness-run card step is opt-in, on only for an explicit SAMPO_CARDS=on (the card bench sets it
    per arm), until a pilot proves it: the 30-pair one did not, and it cost 30% more tokens."""
    return (os.environ.get("SAMPO_CARDS") or os.environ.get("FOREMAN_CARDS") or "").lower() in ("on", "1", "yes", "true")


class Session:
    """One model's use of cards in one run, which can't loop: ATTEMPTS failed calls close a card, an identical call
    after a failure closes it without running, a deterministic card closes at its first failed run, and the first card
    to close escalates (once a session) to escalate(result), whose answer comes back as the result's advice."""

    def __init__(self, root, escalate=None, cache=True, guard=None):
        self.root, self.escalate, self.cache, self.guard = root, escalate, cache, guard
        self.fails, self.tried, self.closed, self.escalated = {}, set(), set(), False

    def call(self, name, inputs):
        if name in self.closed:
            return _result(name, "closed", error=f"card {name!r} is closed for this run after failed calls: carry on "
                                                 "with plain tools")
        key = (name, json.dumps(inputs, sort_keys=True, default=str))
        if key in self.tried:
            return self._close(name, _result(name, "repeat", error="this exact call already failed and nothing ran "
                                                                   "again; the card is closed: carry on with plain tools"))
        r = call(name, inputs, self.root, cache=self.cache, guard=self.guard)
        if r["ok"]:
            return r
        self.tried.add(key)
        n = self.fails[name] = self.fails.get(name, 0) + 1
        r["attempt"] = n
        card = cards(self.root)[0].get(name) or {}
        if r["status"] in ("confinement", "environment"):  # the same machine fails the same way: no retry
            self.closed.add(name)
        elif n >= ATTEMPTS or (card.get("deterministic") and r["status"] in RUN_FAILED):
            self._close(name, r)
        return r

    def _close(self, name, r):
        self.closed.add(name)
        if self.escalate and not self.escalated:
            self.escalated = r["escalated"] = True
            try:
                advice = self.escalate(r)
            except Exception:  # an adviser that fails changes nothing: the run carries on with plain tools
                advice = None
            if advice:
                r["advice"] = str(advice)[:4000]
        return r


def text(r):
    """A call's result as a model reads it: 'card NAME: applied …' and the diff, or the facts, cut to TEXT_CAP; a failure
    starts 'error: ' ('refused: ' when the caller's guard said no), with its attempt and any advice."""
    name, st = r["card"], r["status"]
    if st == "facts":
        facts = r["facts"] if isinstance(r["facts"], str) else json.dumps(r["facts"], indent=1)
        out = f"card {name}: facts{' (cached)' if r['cached'] else ''}\n{facts}"
    elif st == "applied":
        out = (f"card {name}: applied{' (cached)' if r['cached'] else ''}, changed " +
               ", ".join(r["changed"] + [f"{x} (new)" for x in r["created"]]) + f"\n{r['diff']}")
    else:
        out = (("refused: " if st == "refused" else "error: ") + f"card {name} {st}: {r['error']}" +
               (f" (attempt {r['attempt']} of {ATTEMPTS})" if r.get("attempt") else ""))
        if r.get("advice"):
            out += f"\nA larger model's advice for doing it with plain actions:\n{r['advice']}"
    return out if len(out) <= TEXT_CAP else out[:TEXT_CAP] + "\n[cut]"


def sign_test(wins, losses):
    """The exact two-sided sign test's p for paired outcomes (ties dropped): 1.0 with no untied pair."""
    n, k = wins + losses, max(wins, losses)
    if not n:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n)


# --- the paired bench --------------------------------------------------------------------------------------------------

ARMS = ("off", "on")  # SAMPO_CARDS in fm code's run: the same model, cards withheld, then offered


def bench_rows(path):
    """The bench's rows in path (jsonl): [] when there is none; a torn or foreign line is skipped."""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    x = json.loads(line)
                except ValueError:
                    continue
                if isinstance(x, dict):
                    rows.append(x)
    except FileNotFoundError:
        pass
    return rows


def bench_summary(rows):
    """Paired rows into per-arm pass rate, mean tokens and seconds over the pairs, the pairs only cards passed (wins) or
    only the plain run passed (losses), the exact sign test's p, and "proven" only at p < 0.05 with more wins. A pair is
    one scenario of one run with both arms on one model; the newest row of an arm counts."""
    by = {}
    for r in rows:
        by.setdefault((r.get("run"), r.get("scenario"), r.get("model")), {})[r.get("arm")] = r
    pairs = [v for v in by.values() if all(a in v for a in ARMS)]
    arms = {}
    for a in ARMS:
        xs, n = [v[a] for v in pairs], len(pairs)
        good = sum(1 for x in xs if x.get("passed") is True)
        arms[a] = {"n": n, "passed": good, "rate": round(good / n, 3) if n else None,
                   "tokens": round(sum(x.get("tokens") or 0 for x in xs) / n) if n else None,
                   "seconds": round(sum(x.get("seconds") or 0 for x in xs) / n, 1) if n else None,
                   "card_calls": sum(x.get("card_calls") or 0 for x in xs)}  # 0 with cards on: never used, not tried
    wins = sum(1 for v in pairs if v["on"].get("passed") is True and v["off"].get("passed") is not True)
    losses = sum(1 for v in pairs if v["off"].get("passed") is True and v["on"].get("passed") is not True)
    p = sign_test(wins, losses)
    return {"pairs": len(pairs), "arms": arms, "wins": wins, "losses": losses, "p": round(p, 4),
            "verdict": "proven" if p < 0.05 and wins > losses else "not proven"}


def _answering(names):
    """The first model profile of names that answers one short call (Jarvis may be down): (name, [why each before it
    failed])."""
    from sampo import models as sm
    why = []
    for n in names:
        try:
            sm.complete(sm.resolve(n), [{"role": "user", "content": "Say ok."}], max_tokens=16, timeout=120)
            return n, why
        except (sm.ModelError, OSError, ValueError) as e:
            why.append(f"{n}: {c.fit(str(e), 160)}")
    raise ValueError("no model answered: " + "; ".join(why))


def _bench_one(base, key, model, arm):
    """One arm of one exam scenario: fm code on model with SAMPO_CARDS=arm in a throwaway copy (louhi's smith arm and
    its isolation); passed is the grader's hidden checks on the tree it leaves, as for an exam's fix."""
    from sampo import louhi
    old, t0 = os.environ.get("SAMPO_CARDS"), time.monotonic()
    os.environ["SAMPO_CARDS"] = arm  # the box keeps SAMPO_ names; the child's core.mirror_env reads it
    try:
        with tempfile.TemporaryDirectory(prefix="sampo-cardbench-") as tmp:
            work, events = os.path.join(tmp, "work"), os.path.join(tmp, "arm.events.jsonl")
            a = louhi._smith_arm(base, key, work, model, None, events)
            secs = time.monotonic() - t0
            real = [d for d in key.get("defects") or [] if (d.get("check") or {}).get("type") not in louhi.GUARD_CHECKS]
            try:
                passed = bool(real) and all(louhi.passes(base, key, d, work) for d in key["defects"])
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                passed = False
            seen = bench_events(bench_rows(events), set(cards()[0]))
    finally:
        if old is None:
            os.environ.pop("SAMPO_CARDS", None)
        else:
            os.environ["SAMPO_CARDS"] = old
    return dict({"passed": passed, "closed": bool(a.get("done")), "tokens": a.get("tokens") or 0,
                 "seconds": round(secs, 1), "error": a.get("error")}, **seen)


def bench_events(events, names):
    """What one fm code run's events say for the bench row: the cards it was offered, its card calls (by the card
    action or a card's own name) and how many came back ok, and its trail of turns and actions (at most 60), the
    material of slice.md's what-failed."""
    calls, ok, trail, offered = 0, 0, [], []
    for e in events:
        ev = e.get("event")
        if ev == "note" and isinstance(e.get("cards"), list):
            offered = e["cards"]  # what fm code itself offered (its request and its plan's title)
        if ev == "verify":
            trail.append(f"verify: exit {e.get('exit')}")
        elif ev in ("plan", "answer", "blocked", "error", "closed"):
            plan = e.get("plan") if isinstance(e.get("plan"), dict) else {}
            trail.append(f"{ev}: {c.fit(str(e.get('reason') or e.get('text') or plan.get('title') or ''), 80)}")
        elif ev == "harness":  # T-3181: the calls the harness made before turn 1 are card calls too
            for hc in e.get("calls") or []:
                calls += 1
                ok += hc.get("status") in OK
        elif ev == "action":
            trail.append(f"{e.get('action')}:{e.get('result')}")
            if e.get("action") == "card" or e.get("action") in names:
                calls += 1
                ok += e.get("result") == "ok"
    return {"offered": offered, "card_calls": calls, "card_ok": ok, "trail": trail[:60]}


def bench(p, run, models, out, limit=10, kinds=("bug",), lang="python", log=print):
    """T-3160: the paired measurement. The first `limit` scenarios of exam run `run` in lang of kinds, each run twice by
    fm code on the first of models that answers, cards off and on (which goes first alternates); a row per arm goes to
    out as it ends, and a rerun skips the arms out already holds, so a long run resumes. Returns the summary."""
    if limit < 1:  # [:0] would pick nothing and blame the run, [:-2] would drop scenarios silently
        raise ValueError(f"--limit must be at least 1, not {limit}")
    from sampo import louhi
    model, down = _answering(models)
    for why in down:
        log(f"skipped {why}")
    every = louhi.load(p, run)["scenarios"]
    picked = [s for s in every if s.get("lang") == lang and s.get("kind") in kinds][:limit]
    if not picked:
        there = {}
        for s in every:
            there[f"{s.get('lang')} {s.get('kind')}"] = there.get(f"{s.get('lang')} {s.get('kind')}", 0) + 1
        raise ValueError(f"no {lang} scenario of kind {', '.join(kinds)} in {run}; it has " + (", ".join(
            f"{k} ×{n}" for k, n in sorted(there.items())) or "none"))
    have = {(r.get("run"), r.get("scenario"), r.get("model"), r.get("arm")) for r in bench_rows(out)}
    log(f"{len(picked)} scenario(s) of {run} on {model}, {len(have)} arm(s) already in {out}")
    for i, s in enumerate(picked):
        key, base = louhi._key(p, run, s["id"]), os.path.join(louhi.root(p), run, s["id"])
        for arm in ARMS if i % 2 == 0 else ARMS[::-1]:
            if (run, s["id"], model, arm) in have:
                continue
            row = dict(run=run, scenario=s["id"], kind=s.get("kind"), model=model, arm=arm,
                       **_bench_one(base, key, model, arm), at=c.now())
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            c.append_line(out, json.dumps(row))
            log(f"{s['id']} {arm}: {'pass' if row['passed'] else 'fail'}, {row['tokens']} tokens, {row['seconds']} s, "
                f"{row['card_calls']} card call(s)" + (f", {row['error']}" if row["error"] else ""))
    return bench_summary([r for r in bench_rows(out) if r.get("run") == run and r.get("model") == model])


# --- ranking ---------------------------------------------------------------------------------------------------------

_STOP = set("a an the of to in on for and or with by from this that it its is are be as at into each all any my me "
            "our so do does please can you".split())


def _tokens(text):
    out = set()
    for w in re.findall(r"[a-z0-9]+", str(text).lower()):
        if len(w) > 1 and w not in _STOP:
            out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def _card_text(card):
    return " ".join([card["name"].replace("-", " "), card.get("purpose", ""), *card.get("examples", [])])


def rank(text, k=5, root=None):
    """[(score, card)] best first: an idf-weighted cosine of the task's words and each card's name, purpose and
    examples. root: the repo the cards would run in (cards(root))."""
    found = cards(root)[0]
    docs = {n: _tokens(_card_text(cd)) for n, cd in found.items()}
    df = {}
    for toks in docs.values():
        for t in toks:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(1 + len(docs) / n) for t, n in df.items()}
    # ponytail: words no card knows don't count against it (a long request names its own files and symbols), so one rare
    # shared word can lift a card; the embedding ranker replaces this where a local embedder answers
    q = _tokens(text) & set(idf)
    qn = math.sqrt(sum(idf[t] ** 2 for t in q)) or 1.0
    out = []
    for n, toks in docs.items():
        dn = math.sqrt(sum(idf[t] ** 2 for t in toks)) or 1.0
        out.append((sum(idf[t] ** 2 for t in q & toks) / (qn * dn), found[n]))
    out.sort(key=lambda x: (-x[0], x[1]["name"]))
    return out[:k]


def requests_path():
    return os.path.join(c.state_dir(), "callcards", "requests.jsonl")


def pick(text, k=5, root=None):
    """The cards that fit a task, best first, or no_card: then the task is filed as a card request (every miss)."""
    ranked = rank(text, k, root)
    fit = [summary(cd, s) for s, cd in ranked if s >= LEXICAL_FLOOR]
    if fit:
        return {"status": "cards", "cards": fit}
    os.makedirs(os.path.dirname(requests_path()), exist_ok=True)
    c.append_line(requests_path(), json.dumps({"at": c.now(), "text": str(text)[:2000],
                                               "top": [[cd["name"], round(s, 3)] for s, cd in ranked[:3]]}))
    return {"status": "no_card", "error": "no card fits: carry on with plain tools (the task is filed as a card "
                                          "request)", "cards": []}


# --- tests and the CLI -------------------------------------------------------------------------------------------------

def run_tests(card):
    """[(case name, [what failed])] for a card's tests.json, each in a fresh folder with the cache off."""
    try:
        with open(os.path.join(card["dir"], "tests.json")) as f:
            cases = json.load(f)
    except (OSError, ValueError) as e:
        return [("tests.json", [f"unreadable: {e}"])]
    out = []
    for i, case in enumerate(cases if isinstance(cases, list) else []):
        root = os.path.realpath(tempfile.mkdtemp(prefix="sampo-cardtest-"))
        try:
            for rel, text in case.get("files", {}).items():
                if _rel_ok(rel):
                    raise ValueError(f"file {rel!r} {_rel_ok(rel)}")
                p = os.path.join(root, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", newline="") as f:  # as written: a CRLF case stays CRLF
                    f.write(text)
            r = call(card["name"], case.get("inputs", {}), root, cache=False, inject=case.get("inject"),
                     inject_facts=case.get("inject_facts"))
            why = []
            if r["status"] != case.get("expect", "applied"):
                why.append(f"status {r['status']} ({(r['error'] or '')[:300]}), expected {case.get('expect', 'applied')}")
            for rel, text in (case.get("after") or {}).items():
                try:
                    with open(os.path.join(root, rel), newline="") as f:
                        got = f.read()
                except OSError:
                    got = None
                if got != text:
                    why.append(f"{rel} is {got!r:.300}, expected {text!r:.300}")
            if "facts" in case and r["facts"] != case["facts"]:
                why.append(f"facts {json.dumps(r['facts'])[:300]}, expected {json.dumps(case['facts'])[:300]}")
            for s in case.get("facts_has", []):
                if s not in json.dumps(r["facts"]):
                    why.append(f"facts lack {s!r}: {json.dumps(r['facts'])[:300]}")
            for s in case.get("facts_lacks", []):
                if s in json.dumps(r["facts"]):
                    why.append(f"facts hold {s!r}: {json.dumps(r['facts'])[:300]}")
            for s in case.get("error_has", []):
                if s not in (r["error"] or ""):
                    why.append(f"the error lacks {s!r}: {(r['error'] or '')[:300]}")
        except (OSError, ValueError, AttributeError, TypeError) as e:
            why = [f"the case is malformed: {e}"]
        finally:
            shutil.rmtree(root, ignore_errors=True)
        out.append((case.get("name", f"case {i + 1}") if isinstance(case, dict) else f"case {i + 1}", why))
    return out


def _coerce(card, pairs):
    """key=value words into typed inputs, by the card's schema."""
    props, out = card["inputs"].get("properties", {}), {}
    for pair in pairs:
        k, eq, v = pair.partition("=")
        if not eq:
            raise ValueError(f"{pair!r}: inputs are key=value")
        t = props.get(k, {}).get("type") if isinstance(props.get(k), dict) else None
        if t in ("integer", "number"):
            try:
                v = int(v) if t == "integer" else float(v)
            except ValueError:
                pass  # the schema check names it
        elif t == "boolean":
            v = v.lower() in ("1", "true", "yes")
        elif t in ("array", "object"):
            try:
                v = json.loads(v)
            except ValueError:
                v = [x for x in v.split(",") if x] if t == "array" else v
        out[k] = v
    return out


def _root(args):
    if getattr(args, "root", None):
        return args.root
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.getcwd()


def cmd_card(args):
    from sampo import cli
    act, js = args.action or "list", getattr(args, "json", False)
    root = _root(args)
    found, broken = cards(root)
    if act == "list":
        if js:
            print(json.dumps({"cards": [summary(cd) for cd in found.values()], "broken": broken}))
            return 0
        for cd in found.values():
            print(f"{cd['name']:<22} {'read ' if cd['read'] else 'write'}  {cd.get('purpose', '')}")
        for n, why in broken.items():
            print(f"! {n}: {why}")
        if not found:
            print("no cards yet")
        return 0
    if act == "rank":
        text = " ".join([args.name or ""] + args.rest).strip()
        if not text:
            raise cli.UsageError("sampo card rank \"<the task in words>\"")
        r = [dict(summary(cd, s), fits=s >= LEXICAL_FLOOR) for s, cd in rank(text, root=root)]  # a look: none filed
        print(json.dumps({"cards": r, "floor": LEXICAL_FLOOR}) if js else "\n".join(
            f"{x['score']:.3f}{'  ' if x['fits'] else '- '} {x['name']}: {x['purpose']}" for x in r) or "no cards yet")
        return 0
    if act == "bench":
        out = args.out or os.path.join(root, "research", "callcards", "bench.jsonl")
        if args.name:
            try:
                s = bench(cli.resolve(args), args.name, [x for x in args.model.split(",") if x], out, args.limit,
                          tuple(x for x in args.kind.split(",") if x), args.lang,
                          log=lambda line: print(line, file=sys.stderr, flush=True))
            except ValueError as e:
                raise cli.UsageError(str(e))
        else:  # no run: the summary of every row in out
            s = bench_summary(bench_rows(out))
        a, b = s["arms"]["off"], s["arms"]["on"]
        print(json.dumps(s) if js else
              f"{s['pairs']} pair(s): cards off {a['passed']}/{a['n']}, {a['tokens']} tokens, {a['seconds']} s; cards on "
              f"{b['passed']}/{b['n']}, {b['tokens']} tokens, {b['seconds']} s (means per scenario), {b['card_calls']} card "
              f"call(s); {s['wins']} won, "
              f"{s['losses']} lost, sign test p={s['p']}: {s['verdict']}")
        return 0
    names = [args.name] if args.name else (list(found) if act == "test" else [])
    if not names:
        raise cli.UsageError(f"sampo card {act} NAME")
    for n in names:
        if n not in found:
            near = _near(n, found) if found else None
            raise cli.UsageError(f"no card is named {n!r}" + (f" ({broken[n]})" if n in broken else "") +
                                 (f": did you mean {near!r}?" if near else ""))
    if act == "show":
        cd = found[names[0]]
        print(json.dumps({k: v for k, v in cd.items() if k != "digest"}, indent=2))
        return 0
    if act == "call":
        cd = found[names[0]]
        try:
            inputs = json.loads(args.inputs) if args.inputs else {}
            inputs.update(_coerce(cd, args.rest))
        except (ValueError, AttributeError) as e:
            raise cli.UsageError(f"bad inputs: {e}")
        r = call(cd["name"], inputs, root, cache=not args.no_cache)
        if js:
            print(json.dumps(r))
        else:
            print(f"{r['status']}{' (cached)' if r['cached'] else ''}" + (f": {r['error']}" if r["error"] else ""))
            if r["facts"] is not None:
                print(json.dumps(r["facts"], indent=2) if not isinstance(r["facts"], str) else r["facts"])
            if r["diff"]:
                print(r["diff"], end="")
        return 0 if r["ok"] else 1
    bad = 0
    for n in names:  # test
        res = run_tests(found[n])
        passed = sum(1 for _, why in res if not why)
        bad += len(res) - passed
        if js:
            print(json.dumps({"card": n, "passed": passed, "cases": [{"name": cn, "failed": why} for cn, why in res]}))
            continue
        print(f"{n}: {passed}/{len(res)}")
        for cn, why in res:
            for w in why:
                print(f"  ✗ {cn}: {w}")
    return 1 if bad else 0
