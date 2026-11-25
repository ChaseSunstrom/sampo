"""T-3571: sampo-fast, the native PreToolUse fast path, against the Python hook. On every payload both give the same
exit code, stdout, stderr and state-file effects (events.jsonl lines compared without ts, ms, load and fast); the
native side decides the calls it can prove Python allows silently and hands every other one to hooks/hook with the
same argv, env and stdin bytes. Without cargo (or SAMPO_FAST_BIN) only the shim's Python fallback runs."""
import atexit
import contextlib
import fcntl
import glob
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from helpers import HOOK, PLUGIN, SampoTestCase, busy_factor

from sampo import budget, core as c, fastbuild, hooks

REAL_TREE_STATE = fastbuild.tree_state  # the Installer tests mock it clean; test_tree_state_reads_git uses this
NATIVE = os.path.join(PLUGIN, "native")
SHIM = os.path.join(PLUGIN, "hooks", "fast")
VOLATILE = ("ts", "ms", "load", "fast", "guard_ms")
OS_ADDED = ("__CF_USER_TEXT_ENCODING",)  # T-4993: macOS puts it in every process's environment at exec (env -i shows it)
REAL_STATE = c.state_dir()  # read before any test points FOREMAN_HOME at its own folder: recorded inputs, read only
TRANSCRIPTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
_BIN = []


def find_cargo():
    return shutil.which("cargo") or next((p for p in [os.path.expanduser("~/.cargo/bin/cargo")]
                                          if os.access(p, os.X_OK)), None)


def target_root():
    return os.environ.get("CARGO_TARGET_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "sampo", "cargo-target")


KEPT_HOURS = 6  # a build folder nobody used for this long goes


def native_key(native):
    """T-5879: what a build of `native` depends on, as one digest: where it is, every source file's bytes, and the size
    and mtime build.rs embeds for each watched file. Cargo calls a path crate fresh by mtime and gives two checkouts
    one slot (cargo issue 12516), so a tree whose files are older than the last build there, or whose files another
    process built over, was run as the other tree's binary: a build folder per key never meets that."""
    h = hashlib.sha1(os.path.realpath(native).encode())
    for dirpath, dirs, files in os.walk(native):  # the manifests and the crates: not the installed binary beside them
        dirs[:] = sorted(d for d in dirs if dirpath != native or d == "crates")
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            if dirpath == native and not name.startswith("Cargo."):
                continue
            with open(path, "rb") as f:
                h.update(f"{os.path.relpath(path, native)} {hashlib.sha1(f.read()).hexdigest()}\n".encode())
    plugin = os.path.dirname(native)
    for rel in WATCHED:
        try:
            st = os.stat(os.path.join(plugin, rel))
            h.update(f"{rel} {st.st_size} {st.st_mtime_ns}\n".encode())
        except OSError:
            h.update(f"{rel} missing\n".encode())
    return h.hexdigest()[:20]


def _profile_dirs(root):
    """Every `lane` profile folder under `root`: the old shared one, and one per key."""
    out = [os.path.join(root, "lane")]
    with contextlib.suppress(OSError):
        out += [os.path.join(root, n, "lane") for n in sorted(os.listdir(root)) if n.startswith("k-")]
    return [d for d in out if os.path.isdir(d)]


def _tidy(root, keep):
    """Under the lock: remove the build folders (the old shared `lane` too) idle for KEPT_HOURS, so a train's
    subset trees leave about 20 MB each for hours, not for good."""
    now = time.time()
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if path == keep or not (name == "lane" or name.startswith("k-")) or not os.path.isdir(path):
            continue
        used = os.path.join(path, ".used")
        with contextlib.suppress(OSError):
            if now - os.stat(used if os.path.exists(used) else path).st_mtime > KEPT_HOURS * 3600:
                shutil.rmtree(path, ignore_errors=True)


def _seed(root, mine):
    """The registry crates another build folder compiled, copied in (never this crate's own artifacts nor an
    incremental cache): a new key pays for the one crate it changes, as the shared folder did."""
    have = os.path.join(mine, "lane")
    if os.path.isdir(have):
        return
    old = [d for d in _profile_dirs(root) if os.path.isdir(os.path.join(d, ".fingerprint"))
           and os.path.dirname(d) != mine]
    if not old:
        return
    try:
        shutil.copytree(max(old, key=os.path.getmtime), have, symlinks=True,
                        ignore=shutil.ignore_patterns("incremental", "sampo-fast*", "sampo_fast*"))
    except (OSError, shutil.Error):
        shutil.rmtree(have, ignore_errors=True)  # cold: slower, the same answer


def build_fast(native, root, cargo):
    """`cargo build --profile lane` of `native` into a build folder of its own under `root` (see native_key), and a
    copy of the binary in a private temp folder. Build and copy run under one lock: the folder is `native`'s alone,
    but root holds the others'."""
    os.makedirs(root, exist_ok=True)
    mine = os.path.join(root, "k-" + native_key(native))
    with open(os.path.join(root, "sampo-fast.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        os.makedirs(mine, exist_ok=True)
        with open(os.path.join(mine, ".used"), "w"):
            pass
        _tidy(root, mine)
        _seed(root, mine)
        r = subprocess.run(["nice", "-n", "10", cargo, "build", "--locked", "--profile", "lane",
                            "--manifest-path", os.path.join(native, "Cargo.toml")],
                           env=dict(os.environ, CARGO_TARGET_DIR=mine, CARGO_INCREMENTAL="0"), capture_output=True,
                           text=True, timeout=900)
        if r.returncode:
            raise AssertionError("cargo build failed:\n" + r.stderr[-4000:])
        tmp = tempfile.mkdtemp(prefix="sampo-fast-")
        atexit.register(shutil.rmtree, tmp, True)
        return shutil.copy2(os.path.join(mine, "lane", "sampo-fast"), tmp)


def fast_bin():
    """SAMPO_FAST_BIN, else this checkout's own build (build_fast; once per process); None without cargo."""
    if not _BIN:
        path = os.environ.get("SAMPO_FAST_BIN") or os.environ.get("FOREMAN_FAST_BIN")  # importing sampo renames it
        cargo = find_cargo()
        if not path and cargo:
            path = build_fast(NATIVE, target_root(), cargo)
        _BIN.append(path)
    return _BIN[0]


STUB = """#!/usr/bin/env python3
import json, os, sys
data = sys.stdin.buffer.read()
with open(os.environ["STUB_OUT"], "w") as f:
    json.dump({"argv": sys.argv, "stdin": data.hex(), "env": dict(os.environ)}, f)
print("stub ran")
sys.exit(3)
"""
# the files whose logic sampo-fast mirrors: a copy with the same size and mtime counts as the one it was built from
WATCHED = ("hooks/hook", "lib/sampo/__init__.py", "lib/sampo/py.py", "lib/sampo/hooks.py", "lib/sampo/core.py",
           "lib/sampo/budget.py", "lib/sampo/spawn.py", "lib/sampo/killsafe.py", "lib/fmguard.py")
# the Python it mirrors, function by function: a change to one fails this test until main.rs is checked against it
MIRRORED = ("hooks.run", "hooks._run", "hooks._pre_tool_use", "hooks._credential_like", "hooks._credential_read",
            "hooks._outline_first", "hooks._outward_denial", "hooks._event", "hooks._rev", "hooks._cwd",
            "budget.check_subagent", "budget.subagent_pause", "budget.rate_limits", "budget._recent_snaps",
            "core.mirror_env", "core.state_dir", "core.foreman_home", "core.state_fallbacks", "core.gettempdir",
            "core._marker_default", "core._private", "core._writable", "core.now", "core.clock", "core.host_load",
            "core.append_line", "core.find_project", "core._project", "core.projects_dir", "core.read_meta")


def mirror_hashes():
    out = {}
    for name in MIRRORED:
        mod, fn = name.split(".")
        f = getattr({"hooks": hooks, "budget": budget, "core": c}[mod], fn, None)
        if f is None:  # gone (renamed, or a revert on the base): named in the diff below, not an AttributeError
            continue
        src = inspect.getsource(f)
        if name == "hooks._pre_tool_use":  # the branches before the guard; the guarded tools are Python's alone
            src = src.split("        if tool not in GUARDED:\n", 1)[0]
        if name == "hooks._run":  # less the other events' branch (handlers and breaker): sampo-fast runs PreToolUse only
            head, rest = src.split("\n    else:\n", 1)
            src = head + rest[rest.index("\n    if event not in UNTIMED:\n"):]
        out[name] = hashlib.sha1(src.encode()).hexdigest()[:12]
    with open(HOOK, encoding="utf-8") as f:  # its event parse and dispatch: the rest (re-run under a supported Python,
        src = f.read()  # the committed-library fallback, the bench's module dump, the regex cache) is the Python
    # process's own mechanics, never part of an answer sampo-fast gives; an edit anywhere still defers (WATCHED)
    out["hooks/hook"] = hashlib.sha1((src[src.index("\nARGV = "):src.index("\nSTDIN = None\n")] +
                                      src[src.index("\n_data = "):]).encode()).hexdigest()[:12]
    return out


_WARM = {}


def warm_index(home):
    """T-4906: the Ward's writer index of this plugin tree in `home`'s state, as a running Sampo has it after its first
    guarded call: built cold, it is a second's work inside the hook, which the two runs' slow-hook lines time apart."""
    import fmguard
    if "text" not in _WARM:
        root = os.path.join(os.path.dirname(os.path.abspath(fmguard.__file__)), "sampo")
        idx = fmguard._writer_index()
        _WARM["name"] = f"ward-index-{fmguard._index_stamp(root, fmguard._index_files(root))}.json"
        _WARM["text"] = json.dumps({k: v for k, v in idx.items() if not k.startswith("_")})
    d = os.path.join(home, "state", "ward-index")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, _WARM["name"]), "w", encoding="utf-8") as f:
        f.write(_WARM["text"])


class Base(SampoTestCase):
    @property
    def bin(self):
        """The binary; skips the test that needs it when there is none (the shim's fallback test never asks)."""
        if not fast_bin():
            self.skipTest("no cargo and no SAMPO_FAST_BIN: this claim needs the sampo-fast binary")
        return fast_bin()

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.home, "state", "sessions"))
        host = os.environ.pop("FOREMAN_HOST", None)  # helpers' fake load is a test knob: sampo-fast leaves it to Python
        if host is not None:
            self.addCleanup(os.environ.__setitem__, "FOREMAN_HOST", host)

    def project(self, **meta):
        d = os.path.join(self.home, "state", "projects", c.slug_for(self.repo))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(dict({"path": self.repo, "schema": 1}, **meta), f)

    def snapshot(self, name="snap", **limits):
        with open(os.path.join(self.home, "state", "sessions", f"{name}.json"), "w") as f:
            json.dump({"rate_limits": limits}, f)

    def tree(self, home):
        """{path: bytes} of every file under home; events.jsonl as rows without the volatile values (their keys and
        key order kept), plus whether a row is marked fast and the last row's load."""
        out, fast, load = {}, False, None
        for d, _, files in os.walk(home):
            for f in files:
                path = os.path.join(d, f)
                with open(path, "rb") as fh:
                    data = fh.read()
                if f == "events.jsonl":
                    rows = [json.loads(x) for x in data.decode().splitlines()]
                    fast = fast or any(r.get("fast") is True for r in rows)
                    load = rows[-1].get("load") if rows else None
                    data = [([k for k in r if k != "fast"], {k: v for k, v in r.items() if k not in VOLATILE})
                            for r in rows]
                elif f.endswith(".last"):  # hooks.log.last: "<epoch seconds> <digest>"; the two runs may straddle a second
                    data = re.sub(rb"^\d+ ", b"{t} ", data)
                elif f == ".lock":  # core.lock's holder line, "<pid> <epoch> <who>": two runs, two pids and moments
                    data = re.sub(rb"^\d+ [\d.]+ ", b"{pid} {t} ", data)
                else:  # a log line's time
                    data = re.sub(rb"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", b"{ts}", data)
                out[os.path.relpath(path, home)] = data
        return out, fast, load

    def pair(self, payload=None, raw=None, env=None, cwd=None):
        """Run the Python hook and sampo-fast on the same bytes, each in its own copy of this test's home ({home} in
        an env value is that copy); assert they agree; True when sampo-fast decided the call itself."""
        if raw is None:
            raw = json.dumps(dict({"session_id": "sess-1", "cwd": self.repo, "hook_event_name": "PreToolUse"},
                                  **payload)).encode()
        runs = []
        warm_index(self.home)
        home = os.path.join(self.tmp, "run")  # one path for both: what they print and store may name it
        for cmd in ([HOOK, "PreToolUse"], [self.bin, "--hook", HOOK, "PreToolUse"]):
            shutil.rmtree(home, True)
            shutil.copytree(self.home, home, symlinks=True)
            e = dict(os.environ, FOREMAN_HOME=home)
            e.update({k: v.format(home=home) for k, v in (env or {}).items()})
            r = subprocess.run(cmd, input=raw, env=e, capture_output=True, cwd=cwd or self.repo,
                               timeout=30 * busy_factor())
            runs.append((r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace"),
                         *self.tree(home)))
        (pcode, pout, perr, ptree, pfast, pload), (rcode, rout, rerr, rtree, rfast, rload) = runs
        self.assertFalse(pfast, "the Python hook never marks a line fast")
        self.assertEqual((pcode, pout, perr), (rcode, rout, rerr), raw[:300])
        self.assertEqual(ptree, rtree, raw[:300])
        if rfast and pload is not None:  # load per CPU, read a moment apart: the same CPU count, not the same instant
            self.assertLess(abs(rload - pload), max(0.06, pload / 3), f"load per CPU {rload} vs Python's {pload}")
        return rfast

    def cases(self, table):
        for name, payload, native, *env in table:
            with self.subTest(name):
                self.assertEqual(self.pair(**payload, env=env[0] if env else None), native,
                                 f"{name}: sampo-fast should {'decide' if native else 'defer'}")


class Reads(Base):
    def test_a_session_with_a_read_store_goes_to_python(self):
        """T-6064: _read_dedupe may answer a repeat Read once PostToolUse recorded one for this session and agent (its
        store file exists): sampo-fast leaves every Read of that session and agent, ranged or not, to Python."""
        wide = os.path.join(self.repo, "wide.txt")
        with open(wide, "w") as f:
            f.write(("x" * 100 + "\n") * 100)
        read = lambda **ti: {"payload": {"tool_name": "Read", "tool_input": ti}}
        self.cases([("no store yet", read(file_path=wide), True),
                    ("no store yet, a range", read(file_path=wide, offset=2, limit=5), True)])
        with open(os.path.join(self.home, "state", "sessions", "sess-1.readseen-main.json"), "w") as f:
            json.dump({"n": 0, "r": {}}, f)
        self.cases([("a store", read(file_path=wide), False),
                    ("a store, a range", read(file_path=wide, offset=2, limit=5), False),
                    ("a store of another agent", {"payload": {"tool_name": "Read", "agent_id": "ab12",
                                                              "tool_input": {"file_path": wide}}}, True)])

    def test_read_cases(self):
        small = os.path.join(self.repo, "small.txt")
        with open(small, "w") as f:
            f.write("hello\n")
        big = os.path.join(self.repo, "big.py")
        with open(big, "w") as f:
            f.write("".join(f"def f{i}():\n    return {i}\n\n" for i in range(300)))
        wide = os.path.join(self.repo, "wide.txt")  # past the byte floor, short of the line bar
        with open(wide, "w") as f:
            f.write(("x" * 100 + "\n") * 100)
        os.makedirs(os.path.join(self.tmp, "vault", ".ssh"))
        with open(os.path.join(self.tmp, "vault", ".ssh", "id_x"), "w") as f:
            f.write("not a key\n")
        os.symlink(os.path.join(self.tmp, "vault", ".ssh"), os.path.join(self.repo, "keys"))
        read = lambda **ti: {"payload": {"tool_name": "Read", "tool_input": ti}}
        self.cases([
            ("small file", read(file_path=small), True),
            ("relative small file", read(file_path="small.txt"), True),
            ("no file_path", read(), True),
            ("empty file_path", read(file_path=""), True),
            ("a folder", read(file_path=self.repo), True),
            ("big file: outline due", read(file_path=big), False),
            ("big file with a range", read(file_path=big, offset=1, limit=20), True),
            ("big file, pages null still whole", read(file_path=big, pages=None), False),
            ("wide file under the line bar", read(file_path=wide), True),
            ("missing file", read(file_path=os.path.join(self.repo, "nope.txt")), False),
            ("credential folder", read(file_path=os.path.join(self.tmp, "vault", ".ssh", "id_x")), False),
            ("credential through a link", read(file_path=os.path.join(self.repo, "keys", "id_x")), False),
            ("credential file name", read(file_path=os.path.join(self.repo, ".netrc")), False),
            ("home-relative", read(file_path="~/.bashrc"), False),
            ("file_path not a string", read(file_path=5), False),
            ("null byte", read(file_path=small + "\x00x"), False),
            ("big file, odd session id: Python denies it, so sampo-fast defers", {"payload": {
                "tool_name": "Read", "tool_input": {"file_path": big}, "session_id": "a/b"}}, False),
            ("small file, odd session id", {"payload": {
                "tool_name": "Read", "tool_input": {"file_path": small}, "session_id": "a/b"}}, True),
            ("big file, non-ASCII agent id", {"payload": {"tool_name": "Read", "tool_input": {"file_path": big},
                                                          "agent_id": "ägent"}}, False),
            ("subagent whole read, bypass, over 8 KB and 250 lines: Python rewrites it", {"payload": {
                "tool_name": "Read", "tool_input": {"file_path": big}, "agent_id": "sub-1",
                "permission_mode": "bypassPermissions"}}, False),
            ("subagent whole read over 8 KB, default mode: Python decides", {"payload": {
                "tool_name": "Read", "tool_input": {"file_path": big}, "agent_id": "sub-1",
                "permission_mode": "default"}}, False),
            ("subagent wide file under the line bar", {"payload": {
                "tool_name": "Read", "tool_input": {"file_path": wide}, "agent_id": "sub-1",
                "permission_mode": "bypassPermissions"}}, True),
            ("tool_input a list", {"payload": {"tool_name": "Read", "tool_input": ["x"]}}, False),
            ("cwd not a string", {"payload": {"tool_name": "Read", "tool_input": {"file_path": small}, "cwd": 5}},
             False),
            ("not JSON", {"raw": b"{nope"}, False),
            ("not UTF-8", {"raw": b'{"tool_name": "Read", "tool_input": {"file_path": "\xff"}}'}, False),
            ("a JSON list", {"raw": b"[1]"}, False),
            ("empty stdin", {"raw": b""}, False),
            ("SAMPO_FAST=0", read(file_path=small), False, {"SAMPO_FAST": "0"}),
            ("a pinned clock", read(file_path=small), False, {"FOREMAN_NOW": "2026-01-01T00:00:00Z"}),
            ("a fake host load", read(file_path=small), False, {"SAMPO_HOST": "load=1,cpus=2"}),
            ("a state path that isn't UTF-8", read(file_path=small), False, {"FOREMAN_STATE": "{home}/st\udcff"}),
        ])

    def test_a_headless_pilot_sessions_small_read_goes_to_python(self):
        """T-6533 review round 2: sampo-fast allowed a small Read natively before hooks.py's headless scope could refuse it,
        so a headless pilot session read a small file outside its lane (a .env) untouched. Its marker sends the call to
        Python; with no marker the same read stays native (the control)."""
        outside = os.path.join(self.tmp, "outside.env")
        with open(outside, "w") as f:
            f.write("TOKEN=1\n")
        read = {"tool_name": "Read", "tool_input": {"file_path": outside}}
        pilot = {"FOREMAN_PILOT_HEADLESS": "1", "FOREMAN_PILOT_LANE": self.repo, "FOREMAN_PILOT_MAIN": self.repo}
        self.assertTrue(self.pair(read), "control: a small read with no marker is native")
        self.assertFalse(self.pair(read, env=pilot), "a headless pilot session's read is Python's to judge")

    def test_proc_and_letter_case_reads_go_to_the_guard(self):
        """T-5987: /proc/<pid>/environ|mem|maps|fd/N reports length 0, so a native read let it through; and a credential
        folder or file in another letter case (~/.SSH, .NETRC) matched neither prefilter. Both defer, both agree."""
        vault = os.path.join(self.tmp, "vault")
        paths = [f"/proc/{os.getpid()}/{n}" for n in ("environ", "mem", "maps", "smaps", "fd/0", "fdinfo/0")]
        paths += ["/proc/self/environ", "/PROC/1/environ", "/proc"]
        for d in hooks._CRED_DIRS:
            for variant in (d.upper(), d.swapcase()):
                os.makedirs(os.path.join(vault, variant), exist_ok=True)
                with open(os.path.join(vault, variant, "key"), "w") as f:
                    f.write("small\n")
                paths.append(os.path.join(vault, variant, "key"))
        for n in hooks._CRED_FILES:
            os.makedirs(os.path.dirname(os.path.join(vault, n.upper())), exist_ok=True)
            with open(os.path.join(vault, n.upper()), "w") as f:
                f.write("small\n")
            paths.append(os.path.join(vault, n.upper()))
        for p in paths:
            with self.subTest(p=p):
                self.assertTrue(hooks._credential_like(p), "the Python prefilter lets it by")
                self.assertFalse(self.pair({"tool_name": "Read", "tool_input": {"file_path": p}}),
                                 "sampo-fast decided a read the guard has to judge")

    def test_state_named_by_env(self):
        read = {"tool_name": "Read", "tool_input": {"file_path": HOOK}}
        for var in ("FOREMAN_STATE", "SAMPO_STATE"):  # SAMPO_X is FOREMAN_X's new name (core.mirror_env)
            with self.subTest(var):  # inside the copied home, so the tree comparison sees it
                self.assertTrue(self.pair(read, env={var: "{home}/elsewhere"}))
        self.assertTrue(self.pair(read, env={"FOREMAN_STATE": "{home}/x/../elsewhere/"}), "normalized like abspath")
        self.assertFalse(self.pair(read, env={"FOREMAN_STATE": "~/state-x", "HOME": "{home}"}),
                         "a ~ path is Python's to expand")

    def test_state_fallback_marker_defers(self):
        alt = os.path.join(self.tmp, "xdg", "foreman")
        os.makedirs(alt, mode=0o700)
        self.assertTrue(self.pair({"tool_name": "Read", "tool_input": {"file_path": HOOK}}), "a bare fallback dir")
        with open(os.path.join(alt, ".foreman-state.json"), "w") as f:
            json.dump({"default": "/elsewhere"}, f)
        self.assertFalse(self.pair({"tool_name": "Read", "tool_input": {"file_path": HOOK}}))

    def test_a_marker_in_a_sampo_named_fallback_defers_too(self):
        """T-5060: core.state_fallbacks names sampo's folders first; the binary looks in them as well as the foreman ones."""
        alt = os.path.join(self.tmp, "xdg", "sampo")
        os.makedirs(alt, mode=0o700)
        read = {"tool_name": "Read", "tool_input": {"file_path": HOOK}}
        self.assertTrue(self.pair(read), "a bare fallback dir")
        with open(os.path.join(alt, ".foreman-state.json"), "w") as f:
            json.dump({"default": "/elsewhere"}, f)
        self.assertFalse(self.pair(read))


class Agents(Base):
    def test_budget_cases(self):
        far = 9e9
        # T-5837: only a typed call that names its model is native; the rest meet the spawn rule in Python
        scout_in = {"prompt": "look", "description": "x", "subagent_type": "sampo:fm-scout", "model": "haiku"}  # T-5497: typed
        agent = {"payload": {"tool_name": "Agent", "tool_input": scout_in}}
        task = {"payload": {"tool_name": "Task", "tool_input": {"prompt": "look", "subagent_type": "sampo:scout",
                                                                  "model": "haiku"}}}
        self.cases([("no snapshot", agent, True), ("Task, no snapshot", task, True)])
        untyped = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "description": "x", "model": "haiku"}}}
        general = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "subagent_type": "general-purpose",
                                                                      "model": "haiku"}}}
        on_opus = {"payload": {"tool_name": "Agent", "tool_input": dict(scout_in, model="Opus")}}
        mixed = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "subagent_type": "General-Purpose",
                                                                    "model": "haiku"}}}
        blank = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "subagent_type": "  ",
                                                                    "model": "haiku"}}}
        explore = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "subagent_type": "Explore",
                                                                      "model": "haiku"}}}
        other = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "subagent_type": "other:thing",
                                                                    "model": "haiku"}}}
        self.cases([("T-5497: an untyped launch gets a fit note from Python", untyped, False),
                    ("general-purpose gets one", general, False), ("any case of it too", mixed, False),
                    ("a blank type gets one", blank, False), ("a Sampo type on Opus gets one", on_opus, False),
                    ("a named type that is not Sampo's has no note: native", explore, True),
                    ("another plugin's type is native", other, True)])
        # T-6101: a typed call that names sonnet or opus is Python's (the spawn rule rewrites it), whatever the type
        sonnet_explore = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "subagent_type": "Explore",
                                                                            "model": "sonnet"}}}
        opus_explore = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "subagent_type": "Explore",
                                                                         "model": "claude-opus-4-1"}}}
        self.cases([("T-6101: a sonnet launch of a named type is Python's", sonnet_explore, False),
                    ("T-6101: an opus launch of a named type is Python's", opus_explore, False)])
        fixer = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "fix", "subagent_type": "sampo:fm-fixer",
                                                                   "model": "sonnet"}}}
        scout = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "subagent_type": "sampo:fm-scout",
                                                                   "model": "haiku"}}}
        spawn = lambda **ti: {"payload": {"tool_name": "Agent", "tool_input": dict({"prompt": "look"}, **ti)}}
        self.cases([("T-5774: a fixer launch without isolation is Python's to refuse", fixer, False),
                    ("a scout launch that names its model is still native", scout, True),
                    ("T-5837: no model: the table fills it", spawn(subagent_type="Explore"), False),
                    ("an empty model", spawn(subagent_type="Explore", model=""), False),
                    ("a null model", spawn(subagent_type="Explore", model=None), False),
                    ("a model that is no string", spawn(subagent_type="Explore", model=7), False),
                    ("no subagent_type", spawn(model="haiku"), False),
                    ("an empty subagent_type", spawn(subagent_type="", model="haiku"), False),
                    ("general-purpose: a lookup is refused", spawn(subagent_type="general-purpose", model="haiku"), False),
                    ("a namespaced general-purpose", spawn(subagent_type="sampo:general-purpose", model="haiku"), False),
                    ("a tool_input that is no object", {"payload": {"tool_name": "Agent", "tool_input": "x"}}, False),
                    ("no tool_input", {"payload": {"tool_name": "Task"}}, False)])
        self.snapshot(five_hour={"used_percentage": 40, "resets_at": far},
                      seven_day={"used_percentage": 30.5, "resets_at": far})
        self.cases([("under the pace", agent, True), ("FOREMAN_FAULT_LANE", agent, False,
                                                       {"FOREMAN_FAULT_LANE": os.path.join(self.tmp, "mark")})])
        self.snapshot(five_hour={"used_percentage": 95, "resets_at": far})
        self.cases([("5-hour window full: Python refuses", agent, False)])
        self.snapshot(five_hour={"used_percentage": 95, "resets_at": 1})  # the window reset since
        self.cases([("full window already reset", agent, True)])
        self.snapshot(seven_day={"used_percentage": 92, "resets_at": far})
        self.cases([("week past the bar", agent, False)])
        self.snapshot(seven_day={"used_percentage": 50, "resets_at": True})
        self.cases([("resets_at true is a number to Python", agent, True)])
        with open(os.path.join(self.home, "state", "sessions", "snap.json"), "w") as f:
            f.write('{"rate_limits": [1]}')
        self.cases([("rate_limits a list", agent, True)])
        with open(os.path.join(self.home, "state", "sessions", "snap.json"), "w") as f:
            f.write('{"rate_limits": {"five_hour": {"used_percentage": NaN}}}')
        self.cases([("NaN: Python's JSON only", agent, False)])

    def test_recorded_snapshots(self):
        snaps = sorted(glob.glob(os.path.join(REAL_STATE, "sessions", "*.json")), key=os.path.getmtime)[-6:]
        if not snaps:
            self.skipTest("no recorded statusline snapshots")
        for s in snaps:
            shutil.copy2(s, os.path.join(self.home, "state", "sessions"))
        self.pair({"tool_name": "Agent", "tool_input": {"prompt": "x", "subagent_type": "sampo:fm-scout"}})


class Outward(Base):
    def test_outward_cases(self):
        send = {"payload": {"tool_name": "SendMessage", "tool_input": {"to": "lead", "message": "done"}}}
        push = {"payload": {"tool_name": "PushNotification", "tool_input": {"message": "done"}}}
        art_read = {"payload": {"tool_name": "Artifact", "tool_input": {"action": "read", "url": "u"}}}
        doc_read = {"payload": {"tool_name": "mcp__claude_ai_Claude_Docs__read", "tool_input": {}}}
        doc_write = {"payload": {"tool_name": "mcp__claude_ai_Claude_Docs__update", "tool_input": {}}}
        self.cases([("no project", send, True), ("push, no project", push, True)])
        self.project()
        self.cases([("project not sensitive", send, True), ("docs update", doc_write, True)])
        self.project(sensitive=True)
        self.cases([("sensitive: Python refuses", send, False), ("docs update, sensitive", doc_write, False),
                    ("artifact read, sensitive", art_read, True), ("docs read, sensitive", doc_read, True)])
        self.project(sensitive="")
        doc_other = {"payload": {"tool_name": "mcp__claude_ai_Claude_Docs__zap", "tool_input": {}}}
        self.cases([("sensitive empty string is off", send, True), ("a docs tool it doesn't know", doc_other, False)])
        with open(os.path.join(self.home, "state", "projects", c.slug_for(self.repo), "meta.json"), "w") as f:
            f.write("{broken")
        self.cases([("unreadable meta", send, False)])

    def test_schedulers_and_guarded_tools_defer(self):
        cases = [("cron yes", {"payload": {"tool_name": "CronCreate", "tool_input": {"prompt": "yes do it"}}}, False),
                 ("cron", {"payload": {"tool_name": "CronCreate", "tool_input": {"prompt": "continue"}}}, False),
                 ("remote trigger", {"payload": {"tool_name": "RemoteTrigger", "tool_input": {}}}, False),
                 ("edit", {"payload": {"tool_name": "Edit", "tool_input": {
                     "file_path": os.path.join(self.repo, "a.py"), "old_string": "a", "new_string": "b"}}}, False),
                 # T-3360: Python reads a Monitor command as Bash (the guard's decision); sampo-fast leaves it to Python
                 ("monitor, safe", {"payload": {"tool_name": "Monitor", "tool_input": {"command": "tail -f x.log"}}}, False),
                 ("monitor, rm -rf ~", {"payload": {"tool_name": "Monitor", "tool_input": {"command": "rm -rf ~"}}}, False),
                 ("monitor, no command", {"payload": {"tool_name": "Monitor", "tool_input": {}}}, False)]
        fixtures = os.path.join(PLUGIN, "tests", "fixtures")
        for name in ("guard_cases.jsonl", "guard_allow.jsonl"):
            with open(os.path.join(fixtures, name)) as f:
                rows = [json.loads(x) for x in f if x.strip()]
            for k in rows[:: 4 if name == "guard_allow.jsonl" else 1]:  # every guard case, a quarter of the allows
                cwd = os.path.join(self.repo, k["cwd"]) if not os.path.isabs(k["cwd"]) else k["cwd"]
                cases.append((f"{name} {k['id']}", {"payload": {"tool_name": "Bash", "tool_input": {
                    "command": k["cmd"]}, "cwd": cwd if os.path.isdir(cwd) else self.repo}}, False))
        self.cases(cases)


class Passthrough(Base):
    """A deferred call reaches hooks/hook exactly as Claude Code would have run it."""

    def plugin(self, fresh=False):
        plug = os.path.join(self.tmp, "plug")
        os.makedirs(os.path.join(plug, "hooks"))
        shutil.copy2(SHIM, os.path.join(plug, "hooks", "fast"))
        real = os.stat(os.path.join(PLUGIN, WATCHED[0]))
        with open(os.path.join(plug, "hooks", "hook"), "w") as f:  # fresh: padded to the real hook's size
            f.write(STUB + ("#" * (real.st_size - len(STUB) - 1) + "\n" if fresh else ""))
        os.chmod(os.path.join(plug, "hooks", "hook"), 0o755)
        if fresh:  # the mirrored sources as built: same size and mtime
            for rel in WATCHED[1:] + (".claude-plugin/plugin.json",):
                os.makedirs(os.path.dirname(os.path.join(plug, rel)), exist_ok=True)
                shutil.copy2(os.path.join(PLUGIN, rel), os.path.join(plug, rel))
            os.utime(os.path.join(plug, "hooks", "hook"), ns=(real.st_atime_ns, real.st_mtime_ns))
        return plug

    def run_stub(self, cmd, data, env=None):
        out = os.path.join(self.tmp, "stub.json")
        if os.path.exists(out):
            os.remove(out)
        e = dict(os.environ, STUB_OUT=out, FOREMAN_STATE=os.path.join(self.tmp, "st"), **(env or {}))
        r = subprocess.run(cmd, input=data, env=e, capture_output=True, timeout=30 * busy_factor(), cwd=self.repo)
        seen = None
        if os.path.exists(out):
            with open(out) as f:
                seen = json.load(f)
            self.assertEqual((r.returncode, r.stdout), (3, b"stub ran\n"))
            self.assertEqual(bytes.fromhex(seen["stdin"]), data, "the hook got the same stdin bytes")
        return r, seen, e

    def test_defer_keeps_argv_env_and_stdin(self):
        plug = self.plugin()
        hook = os.path.join(plug, "hooks", "hook")
        big = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/x", "content": "y" * 300_000}}).encode()
        for name, data in (("small", b'{"tool_name": "Read", "tool_input": {"file_path": "/etc/hostname"}}'),
                           ("over the pipe size", big), ("not UTF-8", b"\xff\xfe{"), ("empty", b"")):
            with self.subTest(name):
                r, seen, e = self.run_stub([self.bin, "--hook", hook, "PreToolUse", "--extra"], data)
                self.assertIsNotNone(seen, r.stderr)
                self.assertEqual(seen["argv"], [hook, "PreToolUse", "--extra"])
                self.assertEqual(seen["env"], {k: v for k, v in e.items()}, "the same environment")

    def test_shim_without_binary_runs_the_hook(self):
        plug = self.plugin()
        data = b'{"tool_name": "Read", "tool_input": {"file_path": "/etc/hostname"}}'
        r, seen, _ = self.run_stub([os.path.join(plug, "hooks", "fast"), "PreToolUse"], data)
        self.assertIsNotNone(seen, r.stderr)
        self.assertEqual(seen["argv"], [os.path.join(plug, "hooks", "hook"), "PreToolUse"])

    def install(self, plug, digest=None):
        """The binary as an installer leaves it: this user's, 0755, its sha256 in sampo-fast.sha256 beside it."""
        os.makedirs(os.path.join(plug, "native"), exist_ok=True)
        path = os.path.join(plug, "native", "sampo-fast")
        shutil.copyfile(self.bin, path)
        os.chmod(path, 0o755)
        with open(self.bin, "rb") as f:
            digest = digest or hashlib.sha256(f.read()).hexdigest()
        with open(path + ".sha256", "w") as f:
            f.write(f"{digest}  sampo-fast\n")
        os.chmod(path + ".sha256", 0o644)
        return path

    def test_shim_runs_only_a_verified_binary(self):
        plug = self.plugin(fresh=True)
        data = json.dumps({"tool_name": "Read", "tool_input": {"file_path": HOOK}, "session_id": "s",
                           "cwd": self.repo}).encode()
        fast = [os.path.join(plug, "hooks", "fast"), "PreToolUse"]
        path = self.install(plug)
        self.assertIsNone(self.run_stub(fast, data)[1], "verified: sampo-fast decides")
        for name, undo in (("no checksum file", lambda: os.remove(path + ".sha256")),
                           ("another sha256", lambda: self.install(plug, digest="0" * 64)),
                           ("group-writable binary", lambda: os.chmod(path, 0o775)),
                           ("other-writable binary", lambda: os.chmod(path, 0o757)),
                           ("group-writable checksum", lambda: os.chmod(path + ".sha256", 0o664)),
                           ("a link to it", lambda: (os.rename(path, path + ".real"), os.symlink(path + ".real", path)))):
            with self.subTest(name):
                self.install(plug)
                undo()
                r, seen, _ = self.run_stub(fast, data)
                self.assertIsNotNone(seen, f"{name}: hooks/hook runs instead ({r.stderr!r})")
                for p in (path, path + ".real"):
                    if os.path.lexists(p):
                        os.remove(p)

    def test_shim_with_binary_decides_and_a_stale_source_defers(self):
        plug = self.plugin(fresh=True)
        self.install(plug)
        data = json.dumps({"tool_name": "Read", "tool_input": {"file_path": HOOK}, "session_id": "s",
                           "cwd": self.repo}).encode()
        fast = [os.path.join(plug, "hooks", "fast"), "PreToolUse"]
        r, seen, e = self.run_stub(fast, data)
        self.assertIsNone(seen, f"sampo-fast decided the read itself: {r.stderr!r}")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        with open(os.path.join(e["FOREMAN_STATE"], "events.jsonl")) as f:
            row = json.loads(f.read().splitlines()[-1])
        self.assertEqual({k: row[k] for k in ("kind", "event", "tool", "fast")},
                         {"kind": "hook_ms", "event": "PreToolUse", "tool": "Read", "fast": True})
        self.assertEqual(list(row)[:6], ["ts", "kind", "event", "ms", "rev", "tool"], "Python's key order")
        for rel in WATCHED:
            with self.subTest(stale=rel):
                path = os.path.join(plug, rel)
                st = os.stat(path)
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
                r, seen, _ = self.run_stub(fast, data)
                self.assertIsNotNone(seen, f"{rel} changed since the build: Python decides")
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    def test_watched_covers_every_file_the_hook_loads(self):
        """Every plugin file the Python hook loads on a call sampo-fast decides is watched: an edit to any of them
        hands calls to Python until a rebuild (the hook's own module dump, T-3139)."""
        big = os.path.join(PLUGIN, "lib", "sampo", "hooks.py")
        loaded = set()
        for pl in ({"tool_name": "Read", "tool_input": {"file_path": SHIM}},
                   {"tool_name": "Read", "tool_input": {"file_path": big, "offset": 1, "limit": 5}},
                   {"tool_name": "Agent", "tool_input": {"prompt": "x", "subagent_type": "sampo:fm-scout", "model": "haiku"}},
                   {"tool_name": "SendMessage", "tool_input": {"to": "a", "message": "x"}},
                   {"tool_name": "mcp__claude_ai_Claude_Docs__read", "tool_input": {}}):
            dump = os.path.join(self.tmp, "modules.jsonl")
            data = json.dumps(dict({"session_id": "s", "cwd": self.repo}, **pl)).encode()
            self.assertTrue(self.pair(pl), pl)
            subprocess.run([HOOK, "PreToolUse"], input=data, capture_output=True, cwd=self.repo, timeout=60,
                           env=dict(os.environ, FOREMAN_HOME=self.home, SAMPO_HOOK_MODULES=dump))
            with open(dump) as f:
                loaded |= {p for line in f for p in json.loads(line)}
        self.assertTrue(loaded, "the hook dumped its modules")
        self.assertEqual(sorted(loaded - {"plugin/" + w for w in WATCHED}), [],
                         "loaded by the Python hook but not in WATCHED (build.rs, and the tuple here)")

    def test_mirrored_python_is_unchanged(self):
        got = json.loads(subprocess.run([self.bin, "--consts"], capture_output=True, text=True, timeout=30).stdout)
        changed = sorted(k for k in set(got["mirrors"]) | set(mirror_hashes())
                         if got["mirrors"].get(k) != mirror_hashes().get(k))
        self.assertEqual(changed, [], "these changed since sampo-fast mirrored them: re-check plugin/native/crates/"
                                      f"sampo-fast/src/main.rs against them, then set its MIRRORS to {mirror_hashes()}")

    def test_mirrored_constants_match_python(self):
        r = subprocess.run([self.bin, "--consts"], capture_output=True, text=True, timeout=30)
        got = json.loads(r.stdout)
        got.pop("mirrors")
        self.assertEqual(got, {
            "cred_dirs": hooks._CRED_DIRS, "cred_files": hooks._CRED_FILES, "outward": sorted(hooks.OUTWARD),
            "schedulers": sorted(hooks.SCHEDULERS), "outward_mcp": hooks.OUTWARD_MCP, "reads": sorted(hooks._READS),
            "outline_lines": hooks.OUTLINE_LINES, "snap_span": budget.SNAP_SPAN, "pace_floor": budget.PACE_FLOOR,
            "untimed": "PreToolUse" in hooks.UNTIMED, "watched": list(WATCHED)})


class Concurrent(Base):
    """T-5879: sampo-fast built for two checkouts at once."""

    run_stub = Passthrough.run_stub

    def checkout(self, name, spawn_note):
        """A checkout of its own under tmp: the native sources, the stub hook and the mirrored Python, spawn.py ending
        in `spawn_note` (two trees differ there)."""
        plug = os.path.join(self.tmp, name, "plugin")
        shutil.copytree(NATIVE, os.path.join(plug, "native"), ignore=shutil.ignore_patterns("target"))
        os.makedirs(os.path.join(plug, "hooks"))
        shutil.copy2(SHIM, os.path.join(plug, "hooks", "fast"))
        with open(os.path.join(plug, "hooks", "hook"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(plug, "hooks", "hook"), 0o755)
        for rel in WATCHED[1:] + (".claude-plugin/plugin.json",):
            os.makedirs(os.path.dirname(os.path.join(plug, rel)), exist_ok=True)
            shutil.copy2(os.path.join(PLUGIN, rel), os.path.join(plug, rel))
        with open(os.path.join(plug, "lib", "sampo", "spawn.py"), "a") as f:
            f.write(spawn_note)
        return plug

    def age(self, plug, hours):
        """Every file of the tree as old as an rsync -t or a restored copy leaves it: older than any build before."""
        when = time.time() - hours * 3600
        for dirpath, _, files in os.walk(plug):
            for name in files:
                os.utime(os.path.join(dirpath, name), (when, when))

    def decides(self, plug, binary):
        """Whether `binary`, installed in the tree, answers a read itself: it does when its stamps are the tree's."""
        os.makedirs(os.path.join(plug, "native"), exist_ok=True)
        path = os.path.join(plug, "native", "sampo-fast")
        shutil.copyfile(binary, path)
        os.chmod(path, 0o755)
        with open(binary, "rb") as f, open(path + ".sha256", "w") as out:
            out.write(f"{hashlib.sha256(f.read()).hexdigest()}  sampo-fast\n")
        os.chmod(path + ".sha256", 0o644)
        data = json.dumps({"tool_name": "Read", "tool_input": {"file_path": HOOK}, "session_id": "s",
                           "cwd": self.repo}).encode()
        return self.run_stub([os.path.join(plug, "hooks", "fast"), "PreToolUse"], data)[1] is None

    def test_checkouts_each_run_a_binary_of_their_own_sources(self):
        """T-5879: two checkouts whose Python differs build at the same time into the shared cache, and each runs the
        binary stamped from its own files, also when its files are older than the other's build (the 16:06Z cull: a
        stale-source and a mirrored-constants failure no checkout had caused)."""
        cargo = find_cargo()
        if not cargo or os.environ.get("SAMPO_FAST_BIN") or os.environ.get("FOREMAN_FAST_BIN"):
            self.skipTest("this claim builds the binary itself: it needs cargo and no SAMPO_FAST_BIN")
        a, b = self.checkout("a", ""), self.checkout("b", "# only in b\n")
        self.age(b, 5)
        built, errors = {}, []

        def build(name, plug):
            try:
                built[name] = build_fast(os.path.join(plug, "native"), target_root(), cargo)
            except BaseException as e:  # noqa: BLE001: reported by the test thread
                errors.append(e)
        threads = [threading.Thread(target=build, args=(n, p)) for n, p in (("a", a), ("b", b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertNotEqual(native_key(os.path.join(a, "native")), native_key(os.path.join(b, "native")))
        self.assertTrue(self.decides(a, built["a"]), "a runs a binary stamped from a's files")
        self.assertTrue(self.decides(b, built["b"]), "b runs a binary stamped from b's files")
        self.assertFalse(self.decides(b, built["a"]), "a's binary defers on b's files: the stamps differ")
        with open(os.path.join(b, "lib", "sampo", "spawn.py"), "a") as f:  # b's folder, new files, old mtimes
            f.write("# b again, a different size\n")
        self.age(b, 6)
        again = build_fast(os.path.join(b, "native"), target_root(), cargo)
        self.assertTrue(self.decides(b, again), "b rebuilt over its own earlier build: the binary is b's new files'")


    def test_tidy_removes_the_idle_build_folders_and_keeps_the_rest(self):
        root = os.path.join(self.tmp, "root")
        long_ago = time.time() - (KEPT_HOURS + 1) * 3600
        for name, when in (("k-idle", long_ago), ("k-recent", time.time()), ("k-mine", long_ago), ("lane", long_ago),
                           ("other", long_ago)):
            os.makedirs(os.path.join(root, name))
            with open(os.path.join(root, name, ".used"), "w"):
                pass
            os.utime(os.path.join(root, name, ".used"), (when, when))
        _tidy(root, os.path.join(root, "k-mine"))
        self.assertEqual(sorted(os.listdir(root)), ["k-mine", "k-recent", "other"])


class Recorded(Base):
    """Locally only: tool inputs from this machine's recent transcripts (read only; credential paths left out)."""

    def test_recorded_inputs(self):
        files = sorted(glob.glob(os.path.join(TRANSCRIPTS, "**", "*.jsonl"), recursive=True), key=os.path.getmtime)[-40:]
        if not files:
            self.skipTest("no recorded transcripts")
        names = {"Read", "Agent", "Task", "SendMessage", "Artifact", "PushNotification"}
        seen, picked = set(), []
        for path in files:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 2_000_000))
                lines = f.read().splitlines()[1:]
            for line in lines:
                if b'"tool_use"' not in line:
                    continue
                try:
                    e = json.loads(line)
                    blocks = e["message"]["content"]
                except (ValueError, KeyError, TypeError):
                    continue
                for b in blocks if isinstance(blocks, list) else []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use" or b.get("name") not in names:
                        continue
                    ti = b.get("input") or {}
                    fp = ti.get("file_path") if isinstance(ti, dict) else None
                    if isinstance(fp, str) and hooks._credential_like(fp):
                        continue
                    key = json.dumps([b["name"], ti], sort_keys=True)
                    if key not in seen and sum(x["tool_name"] == b["name"] for x, _ in picked) < 15:
                        seen.add(key)
                        cwd = e.get("cwd") if isinstance(e.get("cwd"), str) and os.path.isdir(e["cwd"]) else self.repo
                        picked.append(({"tool_name": b["name"], "tool_input": ti, "cwd": cwd}, cwd))
        if not picked:
            self.skipTest("no recorded inputs of those tools")
        decided = 0
        for payload, cwd in picked:
            with self.subTest(payload["tool_name"]):
                decided += self.pair(payload, cwd=cwd)
        self.assertGreater(decided, 0, "sampo-fast decided none of the recorded calls")


# T-3608: the installer. A fake cargo stands in for the compiler: it records what it was run with and leaves the file the
# case names (the real sampo-fast, or a stand-in that misbehaves) where cargo would.
FAKE_CARGO = """#!/usr/bin/env python3
import json, os, shutil, sys, time
here = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:] == ["--version"]:
    print("cargo 9.9.9 (fake)")
    sys.exit(0)
with open(os.path.join(here, "fake.json")) as f:
    cfg = json.load(f)
listing = []
for d, _, files in os.walk(os.environ.get("CARGO_HOME", "/nonexistent")):
    listing += [os.path.relpath(os.path.join(d, n), os.environ["CARGO_HOME"]) for n in files]
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ),
                        "cargo_home": sorted(listing)}) + "\\n")
if cfg["mode"] == "fail":
    sys.stderr.write("error: no matching package named `serde` found\\n")
    sys.exit(101)
if cfg["mode"] == "hang":
    time.sleep(60)
if cfg["mode"] == "touch":
    with open(cfg["touch"], "a") as f:
        f.write("// edited during the build\\n")
out = os.path.join(sys.argv[sys.argv.index("--target-dir") + 1], "release")
os.makedirs(out, exist_ok=True)
if cfg["mode"] != "nothing":
    shutil.copy(cfg["binary"], os.path.join(out, "sampo-fast"))
"""
FAKE_RUSTC = """#!/bin/sh
if [ "$1" = "-vV" ]; then
  printf 'rustc 9.9.9 (fake)\\nbinary: rustc\\nhost: x86_64-unknown-linux-gnu\\n'
else
  echo "rustc 9.9.9 (fake)"
fi
"""
# stand-ins for a built file: $1 is --hook, $2 the hook, $3 the event
ALLOW_ALL = "#!/bin/sh\nexit 0\n"
DEFER_ALL = '#!/bin/sh\nexec "$2" "$3"\n'
DIFFERS = ('#!/usr/bin/env python3\nimport subprocess, sys\nr = subprocess.run([sys.argv[2], sys.argv[3]], '
           'stdin=sys.stdin, capture_output=True)\nsys.stdout.buffer.write(r.stdout + b"extra")\nsys.exit(r.returncode)\n')


class Installer(Base):
    """sampo-fast's installer: built offline, probed against the Python hook, installed only when every check holds;
    every doubt leaves plugin/hooks/hook in charge."""

    def setUp(self):
        super().setUp()
        self.plug = os.path.join(self.tmp, "plug")
        os.makedirs(os.path.join(self.plug, "hooks"))
        os.makedirs(os.path.join(self.plug, ".claude-plugin"))
        for rel in ("hooks/hook", "hooks/fast", ".claude-plugin/plugin.json"):
            shutil.copy2(os.path.join(PLUGIN, rel), os.path.join(self.plug, rel))  # the stamps the binary was built with
        os.symlink(os.path.join(PLUGIN, "lib"), os.path.join(self.plug, "lib"))
        self.native = os.path.join(self.plug, "native")
        shutil.copytree(NATIVE, self.native, ignore=lambda d, names: [  # what a checkout has: no build output here
            n for n in names if d == NATIVE and (n in ("target", "sampo-fast", "sampo-fast.sha256", "sampo-fast.src")
                                                 or n.startswith(".sampo-fast"))])  # T-4935: nor a live build's temp file
        self.fhome = os.path.join(self.tmp, "user")  # the account's home as the build sees it: its own ~/.cargo, no config
        self.bindir = os.path.join(self.fhome, ".cargo", "bin")
        os.makedirs(self.bindir)
        self.cargo = os.path.join(self.bindir, "cargo")
        self.tools()
        self.cfg = os.path.join(self.bindir, "fake.json")
        self.workdir = os.path.join(self.tmp, "work")
        self.crates = self.registry()
        self.lines = []
        tree = mock.patch.object(fastbuild, "tree_state", lambda plugin=None: ("a" * 40, ""))  # git is its own test below
        tree.start()
        self.addCleanup(tree.stop)

    def tools(self):
        """The account's ~/.cargo/bin: a fake cargo and a fake rustc (the linker is the machine's own cc, root's)."""
        for name, text in (("cargo", FAKE_CARGO), ("rustc", FAKE_RUSTC)):
            path = os.path.join(self.bindir, name)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o755)

    def reset_home(self):
        """A new account home (the last case broke its folders): the fake cargo, its setting and the registry again."""
        cfg = None
        if os.path.exists(self.cfg):
            with open(self.cfg) as f:
                cfg = f.read()
        shutil.rmtree(os.path.join(self.tmp, "w"), ignore_errors=True)
        shutil.rmtree(self.fhome)
        os.makedirs(self.bindir)
        self.tools()
        if cfg is not None:
            with open(self.cfg, "w") as f:
                f.write(cfg)
        self.registry()

    def registry(self):
        """A crates.io cache in fhome with a made-up archive for each registry package Cargo.lock names, the lock
        rewritten with their sums: [(name, version)]. (The real cache is the user's; the real-build test reads that.)"""
        path = os.path.join(self.native, "Cargo.lock")
        with open(path) as f:
            blocks = f.read().split("[[package]]")
        reg = os.path.join(self.fhome, ".cargo", "registry")
        idx = "index.crates.io-1949cf8c6b5b557f"
        out = []
        for i, block in enumerate(blocks):
            m = re.search(r'name = "([^"]+)"\nversion = "([^"]+)"\nsource = "registry[^"]*"\nchecksum = "([0-9a-f]+)"', block)
            if not m:
                continue
            name, version = m.group(1), m.group(2)
            data = f"archive of {name} {version}".encode()
            blocks[i] = block.replace(m.group(3), hashlib.sha256(data).hexdigest())
            for rel, body in ((f"cache/{idx}/{name}-{version}.crate", data),
                              (f"index/{idx}/.cache/{fastbuild.index_path(name)}", b"entry of " + name.encode())):
                os.makedirs(os.path.dirname(os.path.join(reg, rel)), exist_ok=True)
                with open(os.path.join(reg, rel), "wb") as f:
                    f.write(body)
            out.append((name, version))
        with open(os.path.join(reg, "index", idx, "config.json"), "w") as f:
            f.write('{"dl": "x"}')
        with open(path, "w") as f:
            f.write("[[package]]".join(blocks))
        return out

    def fake(self, mode="ok", binary=None, **more):
        with open(self.cfg, "w") as f:
            json.dump(dict({"mode": mode, "binary": binary}, **more), f)

    def stand_in(self, text):
        path = os.path.join(self.tmp, "stand-in")
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)
        return path

    def calls(self):
        path = os.path.join(self.bindir, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(x) for x in f]

    def build(self, **kw):
        kw.setdefault("home", self.fhome)
        kw.setdefault("workdir", self.workdir)
        return fastbuild.build_fast(self.plug, say=self.lines.append, **kw)

    def builds(self):
        """The build folders under the workdir (there are none between builds)."""
        return sorted(os.listdir(self.workdir)) if os.path.isdir(self.workdir) else []

    def files(self):
        """What the installer would have written: name -> (bytes, mode), for the three files in native/."""
        out = {}
        for name in (fastbuild.BIN, fastbuild.SUM, fastbuild.SRC):
            path = os.path.join(self.native, name)
            if os.path.lexists(path):
                with open(path, "rb") as f:
                    out[name] = (f.read(), os.lstat(path).st_mode)
        return out

    def leftovers(self):
        return [n for n in os.listdir(self.native) if n.startswith(".sampo-fast")]

    def assertStays(self, result, before, why=""):
        """Not installed: the line says the Python hook stays, and not a byte of native/ changed."""
        ok, line = result
        self.assertFalse(ok, line)
        self.assertIn("native fast path not installed", line)
        self.assertIn("the Python hook stays", line)
        self.assertIn(why, line)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.leftovers(), [], "no half-written file is left")

    def test_installs_atomically_in_a_scrubbed_environment(self):
        self.fake(binary=self.bin)
        self.assertEqual(fastbuild.status(self.plug, self.cargo)[0], "missing")
        ok, line = self.build()
        self.assertTrue(ok, line)
        self.assertIn("native fast path installed", line)
        (build,) = self.calls()  # one cargo run: no `cargo clean` whose result nobody read
        self.assertEqual(build["cwd"], os.path.realpath(self.native))
        self.assertEqual(build["env"].get("CARGO_NET_OFFLINE"), "true")
        self.assertEqual(build["env"].get("CARGO_INCREMENTAL"), "0")
        self.assertEqual(build["argv"][:2], ["build", "--release"])
        for flag in ("--locked", "--offline"):
            self.assertIn(flag, build["argv"])
        target = build["argv"][build["argv"].index("--target-dir") + 1]
        self.assertTrue(os.path.dirname(os.path.dirname(target)) == self.workdir, target)
        self.assertEqual(build["argv"][build["argv"].index("--manifest-path") + 1], os.path.join(self.native, "Cargo.toml"))
        self.assertEqual(oct(os.stat(self.workdir).st_mode & 0o777), "0o700")
        self.assertEqual(self.builds(), [".sampo-fast.lock"], "the build folder is removed after")
        got = self.files()
        data, mode = got[fastbuild.BIN]
        self.assertEqual(mode & 0o777, 0o755)
        self.assertEqual(got[fastbuild.SUM][0].decode(), f"{hashlib.sha256(data).hexdigest()}  sampo-fast\n")
        self.assertEqual(got[fastbuild.SUM][1] & 0o022, 0)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(fastbuild.status(self.plug, self.cargo)[0], "ok")
        # hooks/fast, the guard, runs it: a read it can prove silent is decided natively and logged fast
        data = json.dumps({"tool_name": "Read", "tool_input": {"file_path": os.path.join(self.plug, "hooks", "hook")},
                           "session_id": "s", "cwd": self.repo}).encode()
        r = subprocess.run([os.path.join(self.plug, "hooks", "fast"), "PreToolUse"], input=data, capture_output=True,
                           cwd=self.repo, timeout=60 * busy_factor(), env=dict(os.environ, FOREMAN_HOME=self.home))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        with open(os.path.join(self.home, "state", "events.jsonl")) as f:
            rows = [json.loads(x) for x in f]
        self.assertTrue(any(row.get("fast") is True for row in rows), rows)

    def test_a_second_run_changes_nothing(self):
        self.fake(binary=self.bin)
        self.assertTrue(self.build()[0])
        before = self.files()
        ok, line = fastbuild.repair(self.plug, cargo=self.cargo, home=self.fhome)
        self.assertTrue(ok, line)
        self.assertIn("ok", line)
        self.assertEqual((self.files(), len(self.calls())), (before, 1), "an ok install is not rebuilt")

    def test_nothing_the_caller_exported_reaches_cargo(self):
        """T-3608 review blocker 1: a session that sets PATH, HOME, RUSTUP_HOME, TMPDIR, CARGO_HOME or RUSTC_WRAPPER must
        not choose the cargo, the toolchain, the registry or the scratch the build runs in. The build's environment is
        built from the account's home, not filtered from the caller's."""
        self.fake(binary=self.bin)
        evil = os.path.join(self.tmp, "evil")
        os.makedirs(evil)
        trojan = os.path.join(evil, "cargo")
        with open(trojan, "w") as f:
            f.write(f"#!/bin/sh\necho ran > {os.path.join(self.tmp, 'trojan-ran')}\nexit 1\n")
        os.chmod(trojan, 0o755)
        poison = {"PATH": evil + os.pathsep + os.environ["PATH"], "HOME": evil, "RUSTUP_HOME": evil, "TMPDIR": evil,
                  "CARGO_HOME": evil, "RUSTUP_TOOLCHAIN": "nightly", "RUSTFLAGS": "-C link-arg=-evil",
                  "RUSTC_WRAPPER": trojan, "RUSTC": trojan, "CARGO_BUILD_TARGET": "x", "LD_PRELOAD": "/nope",
                  "CARGO_NET_OFFLINE": "false", "CARGO_INCREMENTAL": "1", "SAMPO_FAST": "0", "CARGO_TARGET_DIR": evil}
        with mock.patch.dict(os.environ, poison):
            ok, line = self.build()
        self.assertTrue(ok, line)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "trojan-ran")), "the caller's PATH chose a cargo")
        (build,) = self.calls()
        env = build["env"]
        self.assertEqual(env["PATH"], fastbuild.build_path(self.fhome))
        self.assertEqual(env["HOME"], self.fhome)
        for k in ("TMPDIR", "CARGO_HOME"):
            self.assertTrue(env[k].startswith(self.workdir + os.sep), (k, env[k]))
        stray = sorted(k for k in env if k not in ("PATH", "HOME", "USER", "LOGNAME", "LC_ALL", "TMPDIR", "CARGO_HOME",
                                                   "CARGO_NET_OFFLINE", "CARGO_INCREMENTAL", "CARGO_TERM_COLOR",
                                                   "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER", "RUSTC")
                       and k not in OS_ADDED)
        self.assertEqual(stray, [], "nothing else is set: not RUSTUP_HOME, RUSTFLAGS, RUSTC_WRAPPER, LD_PRELOAD, SAMPO_*")
        self.assertEqual(env["RUSTC"], os.path.join(self.bindir, "rustc"), "the checked rustc, not the caller's RUSTC")
        self.assertEqual((env["CARGO_NET_OFFLINE"], env["CARGO_INCREMENTAL"]), ("true", "0"))
        self.assertEqual(fastbuild.find_cargo(self.fhome), self.cargo, "the search never reads the caller's PATH")

    def test_the_linker_is_the_root_owned_system_cc_named_to_cargo(self):
        """T-3608 rounds 3 and 4: rustc runs `cc` to link, and the first cc on a path is a trojan if any folder on it is
        open. The build takes cc from the system folders only (a cc in ~/.cargo/bin is never looked at), requires it to be
        root's, names it to cargo, puts ~/.cargo/bin last on PATH and names the checked rustc as RUSTC."""
        self.fake(binary=self.bin)
        trojan = os.path.join(self.bindir, "cc")  # the first cc on the old PATH: the one a link would have run
        with open(trojan, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(trojan, 0o755)
        ok, line = self.build()
        self.assertTrue(ok, line)
        (build,) = self.calls()
        env = build["env"]
        cc = fastbuild.find_system_cc()
        self.assertTrue(cc and not cc.startswith(self.fhome), cc)
        self.assertEqual(env["CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER"], cc)
        self.assertEqual(os.stat(cc).st_uid, 0, "root's")
        self.assertEqual(env["RUSTC"], os.path.join(self.bindir, "rustc"), "the checked rustc, not a search")
        parts = env["PATH"].split(os.pathsep)
        self.assertEqual((parts[-1], parts[:-1]), (self.bindir, list(fastbuild.SYSTEM_PATH)), "~/.cargo/bin is last")
        self.assertEqual(fastbuild.linker_var("aarch64-apple-darwin"), "CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER")
        for bad in ("", "x", "x86_64 unknown", "a-b\nCARGO_X=1", "A-B"):
            self.assertEqual(fastbuild.linker_var(bad), "", repr(bad))

    def test_a_cc_that_is_not_root_s_or_not_in_the_system_folders_stops_the_build_before_cargo_runs(self):
        self.fake(binary=self.bin)
        sysbin = os.path.join(self.tmp, "sysbin")
        os.makedirs(sysbin)
        cc = os.path.join(sysbin, "cc")
        with open(cc, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(cc, 0o755)  # closed to others, but this user's: a program this user (or a session of theirs) can replace
        with mock.patch.object(fastbuild, "SYSTEM_PATH", (sysbin,)):
            self.assertStays(self.build(), {}, "is not root's")
            self.assertEqual(self.calls(), [], "cargo never ran")
        os.remove(cc)
        trojan = os.path.join(self.bindir, "cc")  # in ~/.cargo/bin only: not a place a linker is taken from
        with open(trojan, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(trojan, 0o755)
        with mock.patch.object(fastbuild, "SYSTEM_PATH", (sysbin,)):
            self.assertStays(self.build(), {}, "no cc (the linker) in the system folders")
            self.assertEqual(self.calls(), [], "cargo never ran")
        self.assertTrue(self.build()[0], "with the machine's own system folders it builds")

    def test_the_rustc_is_pinned_and_a_trojan_in_front_of_it_is_not(self):
        """RUSTC names the checked rustc outright; a poisoned RUSTC from the caller is overwritten, not passed on."""
        self.assertEqual(fastbuild.scrub_env(self.fhome, self.tmp, rustc="/x/rustc")["RUSTC"], "/x/rustc")
        self.assertNotIn("RUSTC", fastbuild.scrub_env(self.fhome, self.tmp))
        self.assertEqual(fastbuild.build_path(self.fhome).split(os.pathsep)[-1], self.bindir)

    def test_no_rustc_or_no_host_triple_leaves_the_python_hook(self):
        self.fake(binary=self.bin)
        rustc = os.path.join(self.bindir, "rustc")
        with open(rustc, "w") as f:
            f.write("#!/bin/sh\necho 'rustc 9.9.9 (fake)'\n")  # prints no host line
        os.chmod(rustc, 0o755)
        self.assertStays(self.build(), {}, "names no host triple")
        self.assertEqual(self.calls(), [])
        os.remove(rustc)
        self.assertStays(self.build(), {}, "no rustc")
        self.assertEqual(self.calls(), [])

    def test_a_config_file_written_during_the_build_is_refused(self):
        """The check before the build is a moment; a file that appears while cargo runs would be in the binary."""
        self.fake("touch", binary=self.bin, touch=os.path.join(self.plug, ".cargo", "config.toml"))
        os.makedirs(os.path.join(self.plug, ".cargo"))
        self.assertStays(self.build(), {}, "appeared during the build")

    def test_no_cargo_leaves_the_python_hook(self):
        os.remove(self.cargo)
        self.assertStays(self.build(), {}, "no cargo")
        self.assertEqual(self.calls(), [])
        ok, line = fastbuild.repair(self.plug, cargo="", home=self.fhome)
        self.assertEqual((ok, self.files()), (False, {}), line)
        self.assertIn("no cargo", line)

    def test_a_cargo_others_can_replace_is_not_run(self):
        """T-3608 review blocker 1: cargo and rustc are trusted only when the file and every folder above it are the
        user's or root's and closed to everyone else."""
        self.fake(binary=self.bin)
        real = self.cargo + ".real"
        for name, do in (("group-writable file", lambda: os.chmod(self.cargo, 0o775)),
                         ("world-writable folder", lambda: os.chmod(self.bindir, 0o777)),
                         ("group-writable folder above", lambda: os.chmod(os.path.dirname(self.bindir), 0o775)),
                         ("a link into a writable folder",
                          lambda: (os.rename(self.cargo, real), os.chmod(self.tmp, 0o700), os.makedirs(os.path.join(self.tmp, "w")),
                                   os.chmod(os.path.join(self.tmp, "w"), 0o777),
                                   os.rename(real, os.path.join(self.tmp, "w", "cargo")),
                                   os.symlink(os.path.join(self.tmp, "w", "cargo"), self.cargo)))):
            with self.subTest(name):
                do()
                self.assertStays(self.build(), {}, "cargo is not trusted")
                self.assertEqual(self.calls(), [], "cargo never ran")
                self.reset_home()
        self.assertTrue(self.build()[0], "closed again, it builds")

    def test_tool_refusal_reads_each_link_of_the_chain(self):
        path = os.path.join(self.tmp, "tool")
        with open(path, "w") as f:
            f.write("x")
        os.chmod(path, 0o755)
        self.assertEqual(fastbuild.tool_refusal(path), "")
        os.chmod(path, 0o757)
        self.assertIn("others can write", fastbuild.tool_refusal(path))
        self.assertIn("cannot be read", fastbuild.tool_refusal(path + ".none"))
        self.assertEqual(fastbuild.tool_refusal("/bin/sh"), "", "root's tools in root's folders are trusted")
        self.assertIn("not a plain file", fastbuild.tool_refusal("/etc"))

    def test_a_config_or_toolchain_file_stops_the_build_before_cargo_runs(self):
        self.fake(binary=self.bin)
        for name, folder in ((".cargo/config.toml", self.tmp), (".cargo/config", self.plug),
                             ("rust-toolchain.toml", self.tmp), ("rust-toolchain", self.native),
                             (".cargo/config.toml", self.native), (".cargo/config.toml", self.fhome)):
            path = os.path.join(folder, name)
            with self.subTest(path=os.path.relpath(path, self.tmp)):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write('[build]\nrustc-wrapper = "/bin/true"\n')
                self.assertStays(self.build(), {}, "would steer cargo")
                self.assertEqual(self.calls(), [], "cargo never ran")
                os.remove(path)
        self.assertTrue(self.build()[0], "with them gone it builds")

    def test_the_home_cargo_config_is_found_without_the_env(self):
        """The home is the account's (here the test's): a config file under it is seen whatever $HOME says."""
        os.makedirs(os.path.join(self.fhome, ".cargo"), exist_ok=True)
        with open(os.path.join(self.fhome, ".cargo", "config.toml"), "w") as f:
            f.write("[net]\noffline = false\n")
        self.fake(binary=self.bin)
        self.assertStays(self.build(), {}, os.path.join(self.fhome, ".cargo"))

    def test_a_failed_build_keeps_the_old_install(self):
        self.fake(binary=self.bin)
        self.assertTrue(self.build()[0])
        before = self.files()
        self.fake("fail")
        self.assertStays(self.build(), before, "the offline build failed")
        self.assertIn("serde", self.build()[1], "cargo's own words are in the line")
        self.assertEqual(self.files(), before)

    def test_a_build_that_makes_no_file_installs_nothing(self):
        self.fake("nothing")
        self.assertStays(self.build(), {}, "no file to install")

    def test_a_hung_build_is_killed(self):
        self.fake("hang")
        with mock.patch.object(fastbuild, "BUILD_SECONDS", 1):
            t0 = time.monotonic()
            self.assertStays(self.build(), {}, "stopped")
        self.assertLess(time.monotonic() - t0, 30)

    def test_a_built_file_that_differs_from_the_python_hook_is_not_installed(self):
        for name, text, why in (("allows everything", ALLOW_ALL, "differs from the Python hook"),
                                ("adds output", DIFFERS, "differs from the Python hook"),
                                ("decides nothing itself", DEFER_ALL, "deferred")):
            with self.subTest(name):
                self.fake(binary=self.stand_in(text))
                self.assertStays(self.build(), {}, why)
        self.assertEqual(fastbuild.status(self.plug, self.cargo)[0], "missing")

    def test_the_refusal_says_what_differed(self):
        """T-5064: macOS CI refused a build on 'shell: built-in 4' and the log named no side of it."""
        self.fake(binary=self.stand_in(DIFFERS))
        ok, line = self.build()
        self.assertFalse(ok)
        self.assertRegex(line, r"differs from the Python hook on the probe '[^']+': stdout at \d+: b'.*' vs b'.*extra'")

    def test_a_refusal_on_state_alone_names_the_file(self):
        """T-5064 review: exit, stdout and stderr equal and only the state different named nothing; the refusal now names
        a file on one side only, and the first differing row of a shared one."""
        self.fake(binary=self.stand_in(
            "#!/usr/bin/env python3\nimport os, subprocess, sys\n"
            f"r = subprocess.run([{self.bin!r}] + sys.argv[1:], stdin=sys.stdin, capture_output=True)\n"
            "open(os.path.join(os.environ['FOREMAN_HOME'], 'stray.txt'), 'w').close()\n"
            "sys.stdout.buffer.write(r.stdout)\nsys.stderr.buffer.write(r.stderr)\nsys.exit(r.returncode)\n"))
        ok, line = self.build()
        self.assertFalse(ok)
        self.assertRegex(line, r"state files on one side only: stray\.txt \(built\)")

    def test_the_excerpt_starts_where_the_two_sides_part(self):
        from sampo import fastbuild
        head = b'{"hookSpecificOutput": {"permissionDecision": "deny", "reason": "' + b"x" * 90
        text = fastbuild._what_differs((2, head + b"one", b"", {}), (2, head + b"two", b"", {}))
        self.assertIn("b'one'", text.replace("x", ""))
        self.assertIn("vs", text)
        self.assertEqual(fastbuild._what_differs((0, b"", b"", {"a": [1, 2]}), (0, b"", b"", {"a": [1, 3]})),
                         "state a row 1: 2 vs 3")

    def test_a_refusal_on_a_different_exit_code_names_both_codes(self):
        """T-5064 review: the exit codes are ints; the first cut sliced them like text and the refusal itself raised."""
        self.fake(binary=self.stand_in(
            "#!/usr/bin/env python3\nimport subprocess, sys\n"
            f"r = subprocess.run([{self.bin!r}] + sys.argv[1:], stdin=sys.stdin, capture_output=True)\n"
            "sys.stdout.buffer.write(r.stdout)\nsys.stderr.buffer.write(r.stderr)\nsys.exit(r.returncode + 3)\n"))
        ok, line = self.build()
        self.assertFalse(ok)
        self.assertRegex(line, r"differs from the Python hook on the probe '[^']+': exit \d+ vs \d+")

    def test_a_first_call_that_differs_once_does_not_refuse_the_build(self):
        """T-5064: a first run can differ from a second (macOS's git shim fills a cache on its first call); the pair is
        run once more, and only a difference that stays refuses the build."""
        flag = os.path.join(self.tmp, "first-call-seen")
        self.fake(binary=self.stand_in(
            "#!/usr/bin/env python3\nimport os, subprocess, sys\n"
            f"r = subprocess.run([{self.bin!r}] + sys.argv[1:], stdin=sys.stdin, capture_output=True)\n"
            f"first = not os.path.exists({flag!r})\nopen({flag!r}, 'a').close()\n"
            "sys.stdout.buffer.write(r.stdout + (b'extra' if first else b''))\nsys.stderr.buffer.write(r.stderr)\n"
            "sys.exit(r.returncode)\n"))
        ok, line = self.build()
        self.assertTrue(ok, line[:300])

    def test_a_built_file_that_answers_shell_calls_itself_is_refused(self):
        """T-3608 review: the first probe held only Read calls; a file that forwards everything but allows every Bash
        call passed it. The corpus holds the guard's own Bash fixtures, each of which the file must hand to Python."""
        stand = self.stand_in(
            "#!/usr/bin/env python3\nimport json, subprocess, sys\ndata = sys.stdin.buffer.read()\n"
            "try:\n    tool = json.loads(data).get('tool_name')\nexcept Exception:\n    tool = None\n"
            "if tool == 'Bash':\n    sys.exit(0)\n"
            f"r = subprocess.run([{self.bin!r}] + sys.argv[1:], input=data, capture_output=True)\n"
            "sys.stdout.buffer.write(r.stdout)\nsys.stderr.buffer.write(r.stderr)\nsys.exit(r.returncode)\n")
        self.fake(binary=stand)
        self.assertStays(self.build(), {}, "differs from the Python hook on the probe 'shell:")

    def test_a_built_file_that_decides_what_the_table_hands_over_is_refused(self):
        """An edit is Python's to decide (the guard reads it); a file that logs it as its own is not the one we tested."""
        stand = self.stand_in(
            "#!/usr/bin/env python3\nimport json, os, subprocess, sys\ndata = sys.stdin.buffer.read()\n"
            "try:\n    tool = json.loads(data).get('tool_name')\nexcept Exception:\n    tool = None\n"
            f"r = subprocess.run([{self.bin!r}] + sys.argv[1:], input=data, capture_output=True)\n"
            "if tool == 'Edit':\n"
            "    path = os.path.join(os.environ['FOREMAN_HOME'], 'state', 'events.jsonl')\n"
            "    rows = [json.loads(x) for x in open(path)]\n"
            "    rows[-1]['fast'] = True\n"
            "    open(path, 'w').write(''.join(json.dumps(x) + '\\n' for x in rows))\n"
            "sys.stdout.buffer.write(r.stdout)\nsys.stderr.buffer.write(r.stderr)\nsys.exit(r.returncode)\n")
        self.fake(binary=stand)
        self.assertStays(self.build(), {}, "decided 'an edit' itself")

    def test_the_corpus_holds_the_guard_fixtures_and_the_read_table(self):
        root = os.path.join(self.tmp, "corpus-root")
        fastbuild._make_root(root)
        rows = fastbuild.corpus(root, PLUGIN)
        names = [n for n, _, _ in rows]
        shell = [n for n in names if n.startswith("shell: guard_cases.jsonl")]
        with open(os.path.join(PLUGIN, "tests", "fixtures", "guard_cases.jsonl")) as f:
            self.assertEqual(len(shell), sum(1 for x in f if x.strip()))
        self.assertGreater(sum(n.startswith("shell: guard_allow.jsonl") for n in names), 5)
        self.assertGreater(sum(native for _, _, native in rows), 8)
        for n, raw, native in rows:
            if n.startswith("shell:"):
                self.assertFalse(native, n)
                self.assertEqual(json.loads(raw)["tool_name"], "Bash")

    def test_a_state_marker_in_a_shared_folder_stops_the_build_before_it_starts(self):
        """The Python hook would follow it into the user's real state, so no probe could run: nothing is compiled either
        (it was 20 minutes of work to learn that at the end)."""
        self.fake(binary=self.bin)
        marker = "/tmp/foreman-state-1/.foreman-state.json"
        with mock.patch.object(fastbuild, "_fixed_marker", lambda: marker):
            self.assertStays(self.build(), {}, "state-fallback marker")
            self.assertEqual(self.calls(), [], "cargo never ran")
            ok, why = fastbuild.probe(self.bin, self.plug)
            self.assertFalse(ok)
            self.assertIn("state-fallback marker", why)

    def test_a_marker_in_either_name_of_a_shared_folder_is_found(self):
        """T-5060: sampo-state-<uid> and the old foreman-state-<uid> in each shared folder; no marker, none found."""
        shared = [os.path.join(self.tmp, d) for d in ("t1", "t2", "t3")]
        for d in shared:
            os.makedirs(d)
        self.assertEqual(fastbuild._fixed_marker(shared), "")
        for name, where in (("sampo", shared[1]), ("foreman", shared[2])):
            folder = os.path.join(where, f"{name}-state-{os.getuid()}")
            os.makedirs(folder)
            with open(os.path.join(folder, ".foreman-state.json"), "w") as f:
                f.write("{}")
            with self.subTest(name=name):
                self.assertEqual(fastbuild._fixed_marker(shared), os.path.join(folder, ".foreman-state.json"))
            os.remove(os.path.join(folder, ".foreman-state.json"))

    def test_the_installer_never_writes_a_state_marker(self):
        """default_workdir reads the marker; core.state_dir would create one when the default state is unwritable."""
        env = {k: v for k, v in os.environ.items() if k != "FOREMAN_STATE"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(c, "state_dir", side_effect=AssertionError("state_dir() may write the marker")), \
                mock.patch.object(c, "_activate_fallback", side_effect=AssertionError("a marker was written")):
            path = fastbuild.default_workdir(PLUGIN)
        self.assertEqual(path.split(os.sep)[-3:-1], ["cache", "sampo-fast-build"])
        self.assertTrue(path.startswith(os.path.join(self.home, "state") + os.sep), path)

    def test_marker_in_effect_reads_every_place_the_binary_looks(self):
        home = os.path.join(self.tmp, "mh")
        env = {"XDG_STATE_HOME": os.path.join(self.tmp, "xs"), "TMPDIR": os.path.join(self.tmp, "mt")}
        for folder in (os.path.join(env["XDG_STATE_HOME"], "foreman"), os.path.join(env["TMPDIR"], f"foreman-state-{os.getuid()}")):
            os.makedirs(folder)
            path = os.path.join(folder, ".foreman-state.json")
            with open(path, "w") as f:
                f.write("{}")
            self.assertEqual(fastbuild.marker_in_effect(home, env), path, "any content counts: the binary lstats it")
            os.remove(path)
        default = os.path.join(home, ".local", "state", "foreman")
        os.makedirs(default)
        with open(os.path.join(default, ".foreman-state.json"), "w") as f:
            f.write("{}")
        self.assertEqual(fastbuild.marker_in_effect(home, {}), os.path.join(default, ".foreman-state.json"))

    def test_the_probe_runs_in_a_home_of_its_own(self):
        seen = []
        real = subprocess.run
        binary = self.bin  # before the patch: the first use builds it (with cargo's own env)
        with mock.patch.object(subprocess, "run", lambda cmd, **kw: (seen.append(kw["env"]), real(cmd, **kw))[1]):
            ok, why = fastbuild.probe(binary, self.plug)
        self.assertTrue(ok, why)
        self.assertGreater(len(seen), 8)
        for env in seen:
            self.assertEqual(sorted(k for k in env if k.startswith(("FOREMAN", "SAMPO", "XDG"))), ["FOREMAN_HOME", "XDG_STATE_HOME"])
            for k in ("FOREMAN_HOME", "HOME", "XDG_STATE_HOME", "TMPDIR"):
                self.assertTrue(env[k].startswith(tempfile.gettempdir()), (k, env[k]))
            self.assertNotEqual(env["HOME"], os.path.expanduser("~"))

    def test_a_bad_build_does_not_replace_a_good_install(self):
        self.fake(binary=self.bin)
        ok, line = self.build()
        if not ok:  # T-4993/T-5064: said first and short, so a log that cuts a long assertion message still shows why
            print(f"BUILD NOT INSTALLED: {line[:600]}", file=sys.stderr, flush=True)
        self.assertTrue(ok, f"{line[:300]} | {self.lines[-3:]}")
        before = self.files()
        self.fake(binary=self.stand_in(ALLOW_ALL))
        self.assertStays(self.build(), before, "differs")
        self.assertEqual(fastbuild.status(self.plug, self.cargo)[0], "ok")

    def test_a_source_changed_during_the_build_is_refused(self):
        self.fake("touch", binary=self.bin, touch=os.path.join(self.native, "crates", "sampo-fast", "src", "main.rs"))
        self.assertStays(self.build(), {}, "changed during the build")

    def test_a_link_in_the_sources_is_refused(self):
        self.fake(binary=self.bin)
        os.symlink("/etc/hostname", os.path.join(self.native, "crates", "sampo-fast", "src", "extra.rs"))
        self.assertStays(self.build(), {}, "not a plain file")
        os.remove(os.path.join(self.native, "crates", "sampo-fast", "src", "extra.rs"))
        os.symlink("/etc", os.path.join(self.native, "crates", "linked"))
        self.assertStays(self.build(), {}, "has a link")
        self.assertEqual(self.calls(), [])

    def test_a_native_folder_that_is_a_link_or_writable_is_refused(self):
        self.fake(binary=self.bin)
        os.chmod(self.native, 0o775)
        self.assertStays(self.build(), {}, "writable by others")
        os.chmod(self.native, 0o755)
        os.rename(self.native, self.native + ".real")
        os.symlink(self.native + ".real", self.native)
        ok, line = self.build()
        self.assertFalse(ok, line)
        self.assertEqual(self.calls(), [])

    def test_a_build_running_elsewhere_is_not_joined(self):
        self.fake(binary=self.bin)
        os.makedirs(self.workdir, mode=0o700)
        with open(os.path.join(self.workdir, ".sampo-fast.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertStays(self.build(), {}, "another build")
        self.assertEqual(self.calls(), [])

    def test_a_build_folder_that_others_can_write_is_refused(self):
        self.fake(binary=self.bin)
        os.makedirs(self.workdir, mode=0o700)
        os.chmod(self.workdir, 0o777)
        self.assertStays(self.build(), {}, "writable by others")
        self.assertEqual(self.calls(), [])

    def test_every_build_starts_in_a_new_empty_folder_and_leaves_none(self):
        """T-3608 review: a persistent target folder let a build link (and `cargo clean -p` forgot) what an earlier run
        compiled; each build is a fresh folder, removed after, and a stopped run's leftovers are swept."""
        self.fake(binary=self.bin)
        os.makedirs(self.workdir, mode=0o700)
        stale = os.path.join(self.workdir, "build-old", "target", "release")
        os.makedirs(stale)
        with open(os.path.join(stale, "sampo-fast"), "w") as f:
            f.write("an earlier run's compiled file")
        os.chmod(os.path.dirname(stale), 0o500)  # read-only, as cargo leaves extracted crates
        with open(os.path.join(native := self.native, ".sampo-fast-new-abc"), "w") as f:
            f.write("half")
        self.assertTrue(self.build()[0])
        (call,) = self.calls()
        target = call["argv"][call["argv"].index("--target-dir") + 1]
        self.assertNotIn("build-old", target)
        self.assertEqual(self.builds(), [".sampo-fast.lock"])
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(os.path.join(native, ".sampo-fast-new-abc")))
        self.fake("fail")
        self.assertFalse(self.build()[0])
        self.assertEqual(self.builds(), [".sampo-fast.lock"], "a failed build leaves no folder either")

    def test_the_build_sees_only_the_archives_cargo_lock_names(self):
        """T-3608 review blocker 2: CARGO_HOME is private. It holds each locked crate's archive (hashed here against
        Cargo.lock's sum and copied from the bytes hashed), its index entry and config.json, and nothing else of the
        user's ~/.cargo: not the extracted src tree, an unlocked archive, a git checkout, a config."""
        reg = os.path.join(self.fhome, ".cargo", "registry")
        idx = "index.crates.io-1949cf8c6b5b557f"
        for rel in (f"cache/{idx}/evil-1.0.0.crate", f"src/{idx}/itoa-1.0.18/build.rs", "../git/checkouts/x/y.rs",
                    "../credentials.toml"):
            os.makedirs(os.path.dirname(os.path.join(reg, rel)), exist_ok=True)
            with open(os.path.join(reg, rel), "w") as f:
                f.write("decoy")
        self.fake(binary=self.bin)
        ok, line = self.build()
        self.assertTrue(ok, line)
        (call,) = self.calls()
        want = sorted([f"registry/cache/{idx}/{n}-{v}.crate" for n, v in self.crates] +
                      [f"registry/index/{idx}/.cache/{fastbuild.index_path(n)}" for n, _ in self.crates] +
                      [f"registry/index/{idx}/config.json"])
        self.assertEqual(call["cargo_home"], want)
        self.assertGreaterEqual(len(self.crates), 5)

    def test_an_archive_that_is_not_the_one_cargo_lock_names_stops_the_build(self):
        self.fake(binary=self.bin)
        idx = "index.crates.io-1949cf8c6b5b557f"
        name, version = self.crates[0]
        crate = os.path.join(self.fhome, ".cargo", "registry", "cache", idx, f"{name}-{version}.crate")
        for what, do, why in (("tampered", lambda: open(crate, "wb").write(b"backdoored"), "sum differs"),
                              ("missing", lambda: os.remove(crate), "not in ~/.cargo/registry/cache"),
                              ("a link", lambda: (os.remove(crate), os.symlink("/etc/hostname", crate)), "plain file of yours")):
            with self.subTest(what):
                do()
                ok, line = self.build()
                self.assertFalse(ok, line)
                self.assertIn(f"{name} {version}", line, "the crate is named")
                self.assertIn(why, line)
                self.assertIn("the Python hook stays", line)
                self.assertEqual((self.calls(), self.files(), self.leftovers()), ([], {}, []))
                self.reset_home()

    def test_a_lock_that_takes_a_crate_from_anywhere_but_the_archives_is_refused(self):
        self.fake(binary=self.bin)
        path = os.path.join(self.native, "Cargo.lock")
        with open(path) as f:
            good = f.read()
        for what, text, why in (("git", good + '\n[[package]]\nname = "x"\nversion = "1.0.0"\nsource = "git+https://example.invalid/x"\n',
                                 "not from crates.io"),
                                ("another registry", good.replace("crates.io-index", "other-index", 1), "not from crates.io"),
                                ("no checksum", re.sub(r'checksum = "[0-9a-f]+"\n', "", good, count=1), "without a plain")):
            with self.subTest(what):
                with open(path, "w") as f:
                    f.write(text)
                self.assertStays(self.build(), {}, why)
                self.assertEqual(self.calls(), [])

    def test_a_tree_that_differs_from_its_commit_is_not_built(self):
        """T-3608 review: what builds is what a commit says. A dirty or untracked file in plugin/native (or no git at
        all) leaves the Python hook; the installed record names the commit built from."""
        self.fake(binary=self.bin)
        with mock.patch.object(fastbuild, "tree_state", lambda plugin=None: ("", "crates/x.rs differ from HEAD: commit or restore them first")):
            self.assertStays(self.build(), {}, "differ from HEAD")
        self.assertEqual(self.calls(), [])
        self.assertTrue(self.build()[0])
        with open(os.path.join(self.native, fastbuild.SRC)) as f:
            rec = json.load(f)
        self.assertEqual((rec["head"], rec["dirty"]), ("a" * 40, False))
        self.assertIn("cargo", rec["tools"])
        self.assertIn("9.9.9", rec["tools"]["cargo"]["version"])
        self.assertEqual(len(rec["crates"]), len(self.crates))

    def test_tree_state_reads_git_without_the_users_config(self):
        """The real thing, in a repo of its own: clean, then a modified, a staged and an untracked file, then no repo."""
        top = os.path.join(self.tmp, "gitrepo")
        plug = os.path.join(top, "plugin")
        os.makedirs(os.path.join(plug, "native", "crates"))
        with open(os.path.join(plug, "native", "Cargo.toml"), "w") as f:
            f.write("[workspace]\n")
        env = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": self.tmp}

        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
                           cwd=top, env=env, check=True, capture_output=True)

        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "x")
        tree_state = REAL_TREE_STATE
        head, why = tree_state(plug)
        self.assertEqual((len(head), why), (40, ""))
        for what, do in (("modified", lambda: open(os.path.join(plug, "native", "Cargo.toml"), "a").write("# x\n")),
                         ("untracked", lambda: open(os.path.join(plug, "native", "crates", "new.rs"), "w").write("x")),
                         ("staged", lambda: git("add", "-A"))):
            with self.subTest(what):
                do()
                head, why = tree_state(plug)
                self.assertEqual(head, "")
                self.assertIn("differ from HEAD", why)
        outside = os.path.join(self.tmp, "nogit")
        os.makedirs(os.path.join(outside, "native"))
        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(top, ".git")}):  # not the caller's, either
            head, why = tree_state(outside)
        self.assertEqual(head, "")
        self.assertTrue(why)

    def test_status_names_each_way_an_install_goes_stale(self):
        self.fake(binary=self.bin)
        self.assertTrue(self.build()[0])
        snap = os.path.join(self.tmp, "snap")
        shutil.copytree(self.plug, snap, symlinks=True)  # copy2 inside: every mtime kept
        path = os.path.join(self.native, fastbuild.BIN)
        main = os.path.join(self.native, "crates", "sampo-fast", "src", "main.rs")
        hook = os.path.join(self.plug, "hooks", "hook")

        def bump(p):
            st = os.stat(p)
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))

        def append(p, data):
            with open(p, "ab") as f:
                f.write(data)

        def record(text):
            with open(os.path.join(self.native, fastbuild.SRC), "w") as f:
                f.write(text)

        for name, do in (("a native source edited", lambda: append(main, b"\n")),
                         ("a mirrored source touched", lambda: bump(hook)),
                         ("a binary that no longer matches its sum", lambda: append(path, b"x")),
                         ("no sum", lambda: os.remove(path + ".sha256")),
                         ("no record", lambda: os.remove(os.path.join(self.native, fastbuild.SRC))),
                         ("a record for another file", lambda: record(json.dumps({"sum": "0" * 64}))),
                         ("a record that is not JSON", lambda: record("{nope")),
                         ("group-writable", lambda: os.chmod(path, 0o775)),
                         ("a link", lambda: (os.rename(path, path + ".real"), os.symlink(path + ".real", path)))):
            with self.subTest(name):
                shutil.rmtree(self.plug)
                shutil.copytree(snap, self.plug, symlinks=True)
                self.assertEqual(fastbuild.status(self.plug, self.cargo)[0], "ok")
                do()
                state, why = fastbuild.status(self.plug, self.cargo)
                self.assertEqual(state, "stale", why)
                self.assertIn("sampo doctor --repair", why)
                ok, line = fastbuild.repair(self.plug, cargo=self.cargo, say=self.lines.append, home=self.fhome,
                                            workdir=self.workdir)  # and a repair fixes it
                if name == "a mirrored source touched":  # only a build of the touched tree decides: this fake is the old one
                    self.assertFalse(ok, line)
                    self.assertIn("deferred", line)
                    self.assertEqual(fastbuild.status(self.plug, self.cargo)[0], "stale")
                    continue
                self.assertTrue(ok, line)
                self.assertEqual(fastbuild.status(self.plug, self.cargo)[0], "ok")

    def test_missing_reads_why(self):
        state, why = fastbuild.status(self.plug, self.cargo)
        self.assertEqual((state, "doctor --repair" in why), ("missing", True))
        state, why = fastbuild.status(self.plug, "")
        self.assertEqual(state, "missing")
        self.assertIn("no cargo", why)
        self.assertIn("Python hook stays", why)

    def test_a_real_offline_locked_build_of_the_committed_tree(self):
        """No fake: the user's cargo builds the tree (offline, from the registry cache, Cargo.lock held to) and the
        installed file is the one hooks/fast accepts. Skipped without cargo."""
        home = fastbuild.account_home()
        if not (fastbuild.find_cargo(home) and fast_bin()):
            self.skipTest("no cargo")
        shutil.copy(os.path.join(NATIVE, "Cargo.lock"), os.path.join(self.native, "Cargo.lock"))  # the real sums
        ok, line = fastbuild.build_fast(self.plug, workdir=self.workdir, home=home, say=self.lines.append)
        if not ok and ("not in ~/.cargo/registry" in line or "no crates.io archive cache" in line or "no index entry" in line):
            self.skipTest(f"this machine's cargo cache lacks the crates: {line}")
        self.assertTrue(ok, line)
        self.assertEqual(fastbuild.status(self.plug)[0], "ok")
        self.assertEqual(oct(os.stat(os.path.join(self.native, fastbuild.BIN)).st_mode & 0o777), "0o755")


if __name__ == "__main__":
    unittest.main()
