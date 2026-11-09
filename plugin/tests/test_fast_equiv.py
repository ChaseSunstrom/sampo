"""T-3571: sampo-fast, the native PreToolUse fast path, against the Python hook. On every payload both give the same
exit code, stdout, stderr and state-file effects (events.jsonl lines compared without ts, ms, load and fast); the
native side decides the calls it can prove Python allows silently and hands every other one to hooks/hook with the
same argv, env and stdin bytes. Without cargo (or SAMPO_FAST_BIN) only the shim's Python fallback runs."""
import atexit
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
import unittest

from helpers import HOOK, PLUGIN, SampoTestCase, busy_factor

from sampo import budget, core as c, hooks

NATIVE = os.path.join(PLUGIN, "native")
SHIM = os.path.join(PLUGIN, "hooks", "fast")
VOLATILE = ("ts", "ms", "load", "fast", "guard_ms")
REAL_STATE = c.state_dir()  # read before any test points FOREMAN_HOME at its own folder: recorded inputs, read only
TRANSCRIPTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
_BIN = []


def fast_bin():
    """SAMPO_FAST_BIN, else `cargo build --profile lane` into the shared target dir (once per process); None
    without cargo."""
    if not _BIN:
        path = os.environ.get("SAMPO_FAST_BIN") or os.environ.get("FOREMAN_FAST_BIN")  # importing sampo renames it
        cargo = shutil.which("cargo") or next((p for p in [os.path.expanduser("~/.cargo/bin/cargo")]
                                               if os.access(p, os.X_OK)), None)
        if not path and cargo:
            target = os.environ.get("CARGO_TARGET_DIR") or os.path.join(
                os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "sampo", "cargo-target")
            os.makedirs(target, exist_ok=True)
            # every checkout builds to the one target/lane/sampo-fast, stamped with its own sources: build and copy
            # under one lock, or a train's tree can run a lane's binary (which defers every call)
            with open(os.path.join(target, "sampo-fast.lock"), "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                r = subprocess.run(["nice", "-n", "10", cargo, "build", "--locked", "--profile", "lane",
                                    "--manifest-path", os.path.join(NATIVE, "Cargo.toml")],
                                   env=dict(os.environ, CARGO_TARGET_DIR=target), capture_output=True, text=True,
                                   timeout=900)
                if r.returncode:
                    raise AssertionError("cargo build failed:\n" + r.stderr[-4000:])
                tmp = tempfile.mkdtemp(prefix="sampo-fast-")
                atexit.register(shutil.rmtree, tmp, True)
                path = shutil.copy2(os.path.join(target, "lane", "sampo-fast"), tmp)
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
           "lib/sampo/budget.py", "lib/fmguard.py")
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
            ("big file, odd session id", {"payload": {"tool_name": "Read", "tool_input": {"file_path": big},
                                                      "session_id": "a/b"}}, True),
            ("big file, non-ASCII agent id", {"payload": {"tool_name": "Read", "tool_input": {"file_path": big},
                                                          "agent_id": "ägent"}}, False),
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


class Agents(Base):
    def test_budget_cases(self):
        far = 9e9
        agent = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "description": "x"}}}
        task = {"payload": {"tool_name": "Task", "tool_input": {"prompt": "look"}}}
        self.cases([("no snapshot", agent, True), ("Task, no snapshot", task, True)])
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
        self.pair({"tool_name": "Agent", "tool_input": {"prompt": "x"}})


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
                     "file_path": os.path.join(self.repo, "a.py"), "old_string": "a", "new_string": "b"}}}, False)]
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
                   {"tool_name": "Agent", "tool_input": {"prompt": "x"}},
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


if __name__ == "__main__":
    unittest.main()
