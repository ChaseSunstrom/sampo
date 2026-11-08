"""T-3160: calling cards. A card is a verified generator a small model calls by name with typed inputs: the inputs are
checked against its schema before anything runs, the generator and its checker run confined (no network, no reads
outside the card and the scratch copy, no writes outside the scratch copy, no credentials) on a scratch copy of the
files they touch, and the change lands atomically only when the checker passes. A session never loops on a card."""
import hashlib
import json
import os
import resource
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from helpers import SampoTestCase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from sampo import callcards, landlock, louhi  # noqa: E402

# T-3238: a test that runs a card for real needs calls confined; elsewhere (macOS: no Landlock) they refuse, by design
confined = unittest.skipUnless(callcards.confinement()[0], "no confinement on this machine: calls refuse (Unconfined "
                                                          "tests it)")
from sampo import core as c  # noqa: E402

UPPER = '''import json, sys
inp = json.load(sys.stdin)["inputs"]
with open(inp["path"]) as f:
    text = f.read()
with open(inp["path"], "w") as f:
    f.write(text.upper() if inp.get("mode", "upper") == "upper" else text.lower())
'''
CHECK_UPPER = '''import json, sys
inp = json.load(sys.stdin)["inputs"]
text = open(inp["path"]).read()
if inp.get("mode", "upper") == "upper" and text != text.upper():
    print("the output still has lower-case letters"); sys.exit(1)
'''
FAIL = 'import sys\nprint("the output still has lower-case letters")\nsys.exit(1)\n'
FACTS = 'import json, sys\ninp = json.load(sys.stdin)["inputs"]\nprint(json.dumps({"size": len(open(inp["path"]).read())}))\n'
ESCAPE = '''import json, sys
inp = json.load(sys.stdin)["inputs"]
got = []
for p in TARGETS:
    try:
        with open(p, "w") as f:
            f.write("escaped\\n")
        got.append(p)
    except OSError:
        pass
if got:
    open(inp["path"], "w").write("escaped\\n")
else:
    sys.exit(3)
'''
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["path"],
          "properties": {"path": {"type": "string", "format": "path"}, "mode": {"enum": ["upper", "lower"]},
                         "note": {"type": "string", "maxLength": 40}}}


def tree(root):
    """Every file under root (.git too) by its bytes: the 'byte for byte unchanged' the criteria name. Not .git/objects
    or a lock file: the setup commit's detached `git maintenance run --auto` writes those while a test runs."""
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if os.path.join(d, x) != os.path.join(root, ".git", "objects")]
        for n in [x for x in files if not x.endswith(".lock")]:
            p = os.path.join(d, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


class Base(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.cards_dir = os.path.join(self.tmp, "cards")
        os.makedirs(self.cards_dir)
        env = mock.patch.dict(os.environ, {"SAMPO_CALLCARDS": self.cards_dir, "FOREMAN_NO_LOCAL_MODELS": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.write("a.txt", "hello world\n")

    def write(self, rel, text):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def read(self, rel):
        with open(os.path.join(self.repo, rel)) as f:
            return f.read()

    def card(self, name, gen=UPPER, check=CHECK_UPPER, tests=None, **spec):
        d = os.path.join(self.cards_dir, name)
        os.makedirs(d)
        spec = {"name": name, "purpose": f"{name}: uppercase or lowercase one text file",
                "examples": ["shout the readme"], "kind": "py", "inputs": SCHEMA, "files": ["$path"],
                "deterministic": False, **spec}
        with open(os.path.join(d, "card.json"), "w") as f:
            json.dump(spec, f)
        for fn, text in (("gen.py", gen), ("check.py", check)):
            with open(os.path.join(d, fn), "w") as f:
                f.write(text)
        if tests is not None:
            with open(os.path.join(d, "tests.json"), "w") as f:
                json.dump(tests, f)
        return name

    def spy(self, after=None):
        """Count the confined runs (generators and checkers); after(n) runs once the nth one returns."""
        real, calls = callcards._spawn, []

        def run(*a, **k):
            r = real(*a, **k)
            calls.append(a)
            if after:
                after(len(calls))
            return r
        p = mock.patch.object(callcards, "_spawn", side_effect=run)
        p.start()
        self.addCleanup(p.stop)
        return calls


class Inputs(Base):
    """AC1: bad input is refused before anything runs, naming the nearest valid value."""

    def test_bad_enum_is_refused_naming_the_nearest_value_before_anything_runs(self):
        self.card("shout")
        runs = self.spy()
        r = callcards.call("shout", {"path": "a.txt", "mode": "uper"}, self.repo)
        self.assertEqual(r["status"], "bad_input", r)
        self.assertIn("'upper'", r["error"])
        self.assertEqual(runs, [], "nothing runs on bad input")

    def test_unknown_field_names_the_field_it_meant_and_missing_required_is_named(self):
        self.card("shout")
        r = callcards.call("shout", {"pth": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "bad_input")
        self.assertIn("'path'", r["error"])
        self.assertIn("required", r["error"])

    def test_a_missing_path_names_the_nearest_file_and_paths_never_leave_the_root(self):
        self.card("shout")
        r = callcards.call("shout", {"path": "a.tx"}, self.repo)
        self.assertEqual(r["status"], "bad_input")
        self.assertIn("a.txt", r["error"])
        for bad in ("../outside.txt", "/etc/hostname", ".git/config", "a.txt\x00x"):
            self.assertEqual(callcards.call("shout", {"path": bad}, self.repo)["status"], "bad_input", bad)

    def test_wrong_type_and_too_long_are_refused(self):
        self.card("shout")
        self.assertEqual(callcards.call("shout", {"path": 3}, self.repo)["status"], "bad_input")
        self.assertEqual(callcards.call("shout", {"path": "a.txt", "note": "x" * 41}, self.repo)["status"], "bad_input")

    def test_an_unknown_card_names_the_nearest_card(self):
        self.card("shout")
        r = callcards.call("shuot", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "no_card")
        self.assertIn("shout", r["error"])

    def test_credential_files_never_enter_a_cards_scope(self):
        """The filter Smith.hidden uses (fmguard._is_credential), not a narrower list of its own."""
        secrets = ("secrets.yaml", "config/credentials.ini", "token.json", "certs/x.p12", "x.pfx", "keys/x.jks")
        for rel in secrets:
            self.write(rel, "s3cret\n")
        self.card("measure", gen=FACTS, check="", read=True, files=["**/*"])
        scope = callcards._scope(callcards.cards()[0]["measure"], {"path": "a.txt"}, self.repo)
        self.assertIn("a.txt", scope)
        self.assertFalse(set(secrets) & set(scope), sorted(scope))
        for rel in secrets:
            r = callcards.call("measure", {"path": rel}, self.repo)
            self.assertEqual(r["status"], "bad_input", rel)
            self.assertIn("credential", r["error"], rel)

    def test_an_input_is_a_literal_name_unless_the_card_declares_it_a_glob(self):
        self.write("a[b].txt", "x\n")
        self.write("ab.txt", "y\n")
        self.card("measure", gen=FACTS, check="", read=True)
        scope = callcards._scope(callcards.cards()[0]["measure"], {"path": "a[b].txt"}, self.repo)
        self.assertEqual(list(scope), ["a[b].txt"], "a path input is never a pattern")
        schema = dict(SCHEMA, properties=dict(SCHEMA["properties"], include={
            "type": "array", "maxItems": 5, "items": {"type": "string", "format": "glob"}}))
        self.card("wide", gen=FACTS, check="", read=True, inputs=schema, files=["$path", "$include"])
        scope = callcards._scope(callcards.cards()[0]["wide"], {"path": "a.txt", "include": ["a*.txt"]}, self.repo)
        self.assertEqual(sorted(scope), ["a.txt", "a[b].txt", "ab.txt"], "a declared glob widens the scope")
        for bad in ("../*", ".git/*", "/etc/*"):
            r = callcards.call("wide", {"path": "a.txt", "include": [bad]}, self.repo)
            self.assertEqual(r["status"], "bad_input", (bad, r))

    @confined
    def test_a_card_folder_must_be_absolute_and_outside_the_repo(self):
        self.card("shout")
        inside = os.path.join(self.repo, "mycards")
        shutil.copytree(self.cards_dir, inside)
        for d in (os.path.relpath(self.cards_dir), inside):
            with mock.patch.dict(os.environ, {"SAMPO_CALLCARDS": d}):
                self.assertEqual(callcards.call("shout", {"path": "a.txt"}, self.repo)["status"], "no_card", d)
                self.assertNotIn("shout", callcards.cards(self.repo)[0], d)
        self.assertEqual(callcards.call("shout", {"path": "a.txt"}, self.repo)["status"], "applied")

    def test_cards_are_read_again_only_when_a_card_folder_changes(self):
        self.card("shout")
        first = callcards.cards()[0]["shout"]["digest"]
        with mock.patch.object(callcards, "_digest", wraps=callcards._digest) as d:
            callcards.cards()
            self.assertEqual(d.call_count, 0, "an unchanged folder isn't hashed again")
            with open(os.path.join(self.cards_dir, "shout", "check.py"), "a") as f:
                f.write("# changed\n")
            self.assertNotEqual(callcards.cards()[0]["shout"]["digest"], first)
            self.assertGreater(d.call_count, 0)

    def test_no_seed_card_takes_an_input_named_action(self):
        """fm code's dispatcher pops 'action' from a JSON call: a card input of that name would never arrive."""
        for name, cd in callcards.cards()[0].items():
            self.assertNotIn("action", cd["inputs"].get("properties", {}), name)


@confined
class Apply(Base):
    """AC1: the generator runs on a scratch copy, the checker there, and only a pass changes the worktree."""

    def test_a_pass_applies_the_change(self):
        self.card("shout")
        r = callcards.call("shout", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "applied", r)
        self.assertTrue(r["ok"])
        self.assertEqual(r["changed"], ["a.txt"])
        self.assertEqual(self.read("a.txt"), "HELLO WORLD\n")

    def test_a_checker_failure_is_typed_and_leaves_the_tree_byte_for_byte(self):
        self.card("shout", check=FAIL)
        before = tree(self.repo)
        r = callcards.call("shout", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "checker_failed", r)
        self.assertFalse(r["ok"])
        self.assertIn("lower-case letters", r["error"])
        self.assertEqual(tree(self.repo), before)

    def test_a_generator_crash_is_typed_and_changes_nothing(self):
        self.card("shout", gen='raise SystemExit("cannot parse it")\n')
        before = tree(self.repo)
        r = callcards.call("shout", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "generator_failed", r)
        self.assertIn("cannot parse it", r["error"])
        self.assertEqual(tree(self.repo), before)

    def test_writes_outside_the_cards_scope_are_refused(self):
        self.card("shout", gen=UPPER + 'open("README.md", "w").write("x")\n')
        before = tree(self.repo)
        self.assertEqual(callcards.call("shout", {"path": "a.txt"}, self.repo)["status"], "scope")
        self.card("eraser", gen='import os\nos.remove("a.txt")\n')
        self.assertEqual(callcards.call("eraser", {"path": "a.txt"}, self.repo)["status"], "scope")
        self.card("linker", gen='import os\nos.remove("a.txt")\nos.symlink("/etc/hostname", "a.txt")\n')
        self.assertEqual(callcards.call("linker", {"path": "a.txt"}, self.repo)["status"], "scope")
        self.assertEqual(tree(self.repo), before)

    def test_a_symlinked_input_is_never_followed(self):
        os.symlink("/etc/hostname", os.path.join(self.repo, "link.txt"))
        self.card("shout")
        self.assertEqual(callcards.call("shout", {"path": "link.txt"}, self.repo)["status"], "bad_input")

    def test_a_file_edited_during_the_call_is_stale_and_keeps_its_edit(self):
        self.card("shout")
        self.spy(after=lambda n: n == 2 and self.write("a.txt", "someone else\n"))
        r = callcards.call("shout", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "stale", r)
        self.assertEqual(self.read("a.txt"), "someone else\n")

    def test_a_read_card_returns_facts_and_changes_nothing(self):
        self.card("measure", gen=FACTS, check="", read=True)
        before = tree(self.repo)
        r = callcards.call("measure", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "facts", r)
        self.assertEqual(r["facts"], {"size": 12})
        self.assertEqual(tree(self.repo), before)

    def test_a_runaway_generator_times_out_and_a_flood_is_capped(self):
        self.card("sleeper", gen="import time\ntime.sleep(60)\n")
        with mock.patch.object(callcards, "TIMEOUT", 2):
            t = time.monotonic()
            self.assertEqual(callcards.call("sleeper", {"path": "a.txt"}, self.repo)["status"], "timeout")
            self.assertLess(time.monotonic() - t, 20)
        self.card("flood", gen=FACTS.replace("print(", "print('x' * 20_000_000)\nprint("), check="", read=True)
        r = callcards.call("flood", {"path": "a.txt"}, self.repo)
        self.assertLess(len(json.dumps(r)), 200_000)
        self.card("bloat", gen='open("big.bin", "wb").write(b"0" * 64_000_000)\n', creates=["big.bin"])
        self.assertEqual(callcards.call("bloat", {"path": "a.txt"}, self.repo)["status"], "generator_failed")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "big.bin")))

    def test_a_missing_dependency_is_the_environment_not_the_cards_failure(self):
        self.card("needy", gen="import no_such_module_xyz\n")
        r = callcards.call("needy", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "environment", r)
        self.assertIn("missing no_such_module_xyz", r["error"])
        self.card("says", gen='import sys\nsys.stderr.write("environment: missing pytest\\n")\nsys.exit(1)\n')
        r = callcards.call("says", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "environment", r)
        self.assertIn("missing pytest", r["error"])
        s = callcards.Session(self.repo)
        self.assertEqual(s.call("needy", {"path": "a.txt"})["status"], "environment")
        self.assertEqual(s.call("needy", {"path": "a.txt", "note": "again"})["status"], "closed")
        self.assertEqual(self.read("a.txt"), "hello world\n")

    def test_the_interpreter_is_part_of_the_cache_key(self):
        self.card("measure", gen=FACTS, check="", read=True)
        self.assertEqual(callcards.call("measure", {"path": "a.txt"}, self.repo)["status"], "facts")
        runs = self.spy()
        with mock.patch.object(callcards, "_env", return_value=["/other/python", "3.0", None]):
            r = callcards.call("measure", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "facts", r)
        self.assertFalse(r.get("cached"), r)
        self.assertTrue(runs)

    def test_a_failed_apply_rolls_back_every_file_and_folder_it_made(self):
        before, real, n = tree(self.repo), c.write_atomic, []

        def flaky(path, text):
            n.append(path)
            if len(n) == 2:
                raise RuntimeError("disk on fire")
            return real(path, text)
        with mock.patch.object(callcards.c, "write_atomic", side_effect=flaky):
            msg = callcards._apply(self.repo, {"a.txt": b"hello world\n"},
                                   {"new/deep/x.txt": b"x", "a.txt": b"A"})
        self.assertIn("disk on fire", msg or "")
        self.assertEqual(tree(self.repo), before)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "new")))


@confined
class Confinement(Base):
    """AC2: a generator (or checker) that reaches the network, writes outside its scratch copy or reads a secret fails,
    and it never sees a credential."""

    def test_no_network_connection_tcp_udp_or_unix(self):
        tcp, udp, unix = socket.socket(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_UNIX)
        for s in (tcp, udp, unix):
            self.addCleanup(s.close)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        tcp.settimeout(0.5)
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(0.5)
        upath = os.path.join(self.tmp, "u.sock")
        unix.bind(upath)
        unix.listen(1)
        unix.settimeout(0.5)
        gen = f'''import json, socket, sys
inp = json.load(sys.stdin)["inputs"]
got = []
for fam, addr in ((socket.AF_INET, ("127.0.0.1", {tcp.getsockname()[1]})), (socket.AF_UNIX, {upath!r})):
    try:
        s = socket.socket(fam)
        s.settimeout(2)
        s.connect(addr)
        s.sendall(b"hi")
        got.append(str(addr))
    except OSError:
        pass
try:
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"hi", ("127.0.0.1", {udp.getsockname()[1]}))
    got.append("udp")
except OSError:
    pass
if not got:
    sys.exit(3)
open(inp["path"], "w").write("connected " + " ".join(got))
'''
        self.card("netter", gen=gen, check="")
        r = callcards.call("netter", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "generator_failed", r)
        for s in (tcp, unix):
            with self.assertRaises(OSError):
                s.accept()
        with self.assertRaises(OSError):
            udp.recv(10)
        self.assertEqual(self.read("a.txt"), "hello world\n")

    def test_no_write_outside_the_scratch_copy(self):
        outside = [os.path.join(self.repo, "pwned"), os.path.join(self.tmp, "outside.txt"),
                   os.path.join(self.cards_dir, "planted.py"), "../orig/a.txt"]
        self.card("escaper", gen=ESCAPE.replace("TARGETS", repr(outside)), check="")
        r = callcards.call("escaper", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "generator_failed", r)
        for p in outside[:3]:
            self.assertFalse(os.path.exists(p), p)
        self.card("checker-escape", gen=UPPER, check=ESCAPE.replace("TARGETS", repr(outside)))
        self.assertEqual(callcards.call("checker-escape", {"path": "a.txt"}, self.repo)["status"], "checker_failed")
        for p in outside[:3]:
            self.assertFalse(os.path.exists(p), p)
        self.assertEqual(self.read("a.txt"), "hello world\n")

    def test_no_secret_is_readable(self):
        fake = os.path.join(self.tmp, "cfg", "sampo", "secrets.env")
        os.makedirs(os.path.dirname(fake))
        with open(fake, "w") as f:
            f.write("LLM_API_KEY=sk-test-not-real\n")
        real = os.path.expanduser("~/.config/sampo/secrets.env")
        gen = f'''import json, sys
inp = json.load(sys.stdin)["inputs"]
got = []
for p in ({fake!r}, {real!r}, {os.path.join(self.repo, "a.txt")!r}):
    try:
        open(p).read(1)
        got.append(p)
    except OSError:
        pass
if not got:
    sys.exit(3)
open(inp["path"], "w").write("LEAK %d" % len(got))
'''
        self.card("reader", gen=gen, check="")
        r = callcards.call("reader", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "generator_failed", r)
        self.assertNotIn("LEAK", self.read("a.txt"))

    def test_credentials_are_stripped_from_the_env(self):
        names = ("GITHUB_TOKEN", "LLM_API_KEY", "ANTHROPIC_API_KEY", "FAKE_API_KEY", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK")
        self.card("envy", gen='import json, os, sys\nprint(json.dumps({"env": dict(os.environ)}))\n', check="", read=True)
        with mock.patch.dict(os.environ, {n: "secret-value" for n in names}):
            r = callcards.call("envy", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "facts", r)
        env = r["facts"]["env"]
        self.assertFalse(set(names) & set(env), env)
        self.assertNotIn("secret-value", json.dumps(env))
        self.assertNotEqual(env.get("HOME"), os.path.expanduser("~"))

    def test_no_signal_or_ptrace_reaches_another_process_even_without_landlock_scoping(self):
        """Landlock scopes signals only from ABI 6: below it seccomp refuses kill and its kin (a faked ABI 5 here)."""
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(victim.wait)
        self.addCleanup(victim.kill)
        gen = f'''import ctypes, json, os, signal, sys
inp = json.load(sys.stdin)["inputs"]
got = []
try:
    os.kill({victim.pid}, signal.SIGTERM)
    got.append("kill")
except OSError:
    pass
libc = ctypes.CDLL(None, use_errno=True)
libc.ptrace.restype = ctypes.c_long
if libc.ptrace(16, {victim.pid}, None, None) == 0:
    got.append("ptrace")
if not got:
    sys.exit(3)
open(inp["path"], "w").write("reached " + " ".join(got))
'''
        self.card("signaller", gen=gen, check="")
        for v in (landlock.abi(), 5):
            with mock.patch.object(landlock, "abi", return_value=v):
                r = callcards.call("signaller", {"path": "a.txt"}, self.repo, cache=False)
            self.assertEqual(r["status"], "generator_failed", (v, r))
            self.assertIsNone(victim.poll(), v)
        with mock.patch.object(landlock, "abi", return_value=5):
            self.assertIn("signals refused by seccomp", callcards.confinement()[1])
        self.assertEqual(self.read("a.txt"), "hello world\n")

    def test_no_limit_priority_or_sigio_owner_reaches_another_process(self):
        """Another process's RLIMIT_CPU kills it as surely as a signal, at any Landlock ABI; below ABI 6, F_SETOWN aims
        SIGIO (any signal, with F_SETSIG) at it. The run's own limits still work."""
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(victim.wait)
        self.addCleanup(victim.kill)
        limit = resource.prlimit(victim.pid, resource.RLIMIT_CPU)
        gen = f'''import fcntl, json, os, resource, sys
inp = json.load(sys.stdin)["inputs"]
resource.setrlimit(resource.RLIMIT_NOFILE, resource.getrlimit(resource.RLIMIT_NOFILE))
got = []
for name, f in (("prlimit", lambda: resource.prlimit({victim.pid}, resource.RLIMIT_CPU, (1, 1))),
                ("setpriority", lambda: os.setpriority(os.PRIO_PROCESS, {victim.pid}, 19)),
                ("F_SETOWN", lambda: fcntl.fcntl(os.pipe()[0], fcntl.F_SETOWN, {victim.pid}))):
    try:
        f()
        got.append(name)
    except OSError:
        pass
open(inp["path"], "w").write(json.dumps(got))
'''
        self.card("limiter", gen=gen, check="")
        for v, allowed in ((landlock.abi(), ["F_SETOWN"]), (5, [])):
            with mock.patch.object(landlock, "abi", return_value=v):
                r = callcards.call("limiter", {"path": "a.txt"}, self.repo, cache=False)
            self.assertEqual(r["status"], "applied", (v, r))
            self.assertEqual([x for x in json.loads(self.read("a.txt")) if x not in allowed], [], v)
        self.assertEqual(resource.prlimit(victim.pid, resource.RLIMIT_CPU), limit)
        self.assertEqual(os.getpriority(os.PRIO_PROCESS, victim.pid), os.getpriority(os.PRIO_PROCESS, os.getpid()))

    def test_a_process_left_behind_cant_change_the_checked_output(self):
        gen = UPPER + '''import os, time
if os.fork() == 0:
    try:
        os.setsid()
    except OSError:
        pass
    time.sleep(1.5)
    open(inp["path"], "w").write("TAMPERED\\n")
    os._exit(0)
'''
        self.card("leaver", gen=gen, check="import time\ntime.sleep(3)\n")
        r = callcards.call("leaver", {"path": "a.txt"}, self.repo, cache=False)
        self.assertEqual(r["status"], "applied", r)
        self.assertEqual(self.read("a.txt"), "HELLO WORLD\n")

    def test_a_fork_loop_is_capped(self):
        gen = '''import os, sys, time
n = 0
try:
    for n in range(200):
        if os.fork() == 0:
            time.sleep(5)
            os._exit(0)
except OSError:
    print("fork refused after %d" % n)
    sys.exit(3)
'''
        self.card("forker", gen=gen, check="")
        with mock.patch.object(callcards, "NPROC", 16):
            r = callcards.call("forker", {"path": "a.txt"}, self.repo, cache=False)
        self.assertEqual(r["status"], "generator_failed", r)
        self.assertIn("fork refused", r["error"])

    def test_a_run_filling_its_scratch_is_stopped(self):
        gen = 'import time\nfor i in range(20):\n    open("big%d.bin" % i, "wb").write(b"0" * (4 << 20))\ntime.sleep(30)\n'
        self.card("filler", gen=gen, check="", creates=["big*.bin"])
        t = time.monotonic()
        with mock.patch.object(callcards, "SCRATCH_CAP", 8 << 20), mock.patch.object(callcards, "TIMEOUT", 10):
            r = callcards.call("filler", {"path": "a.txt"}, self.repo, cache=False)
        self.assertEqual(r["status"], "generator_failed", r)
        self.assertIn("MB in its scratch", r["error"])
        self.assertLess(time.monotonic() - t, 8)
        self.assertFalse([n for n in os.listdir(self.repo) if n.startswith("big")])


class Unconfined(Base):
    def test_without_confinement_nothing_runs(self):
        self.card("shout")
        runs = self.spy()
        with mock.patch.object(callcards, "confinement", return_value=(False, "no Landlock here")):
            r = callcards.call("shout", {"path": "a.txt"}, self.repo)
        self.assertEqual(r["status"], "confinement", r)
        self.assertIn("no Landlock here", r["error"])
        self.assertEqual(runs, [])
        self.assertEqual(self.read("a.txt"), "hello world\n")


@confined
class Calls(Base):
    """AC6: a call never loops."""

    def test_three_attempts_then_one_escalation_then_closed(self):
        self.card("shout", check=FAIL)
        asked = []
        s = callcards.Session(self.repo, escalate=asked.append)
        rs = [s.call("shout", {"path": "a.txt", "note": f"try {i}"}) for i in range(4)]
        self.assertEqual([r["status"] for r in rs], ["checker_failed"] * 3 + ["closed"])
        self.assertEqual([r.get("attempt") for r in rs[:3]], [1, 2, 3])
        self.assertIn("lower-case letters", rs[0]["error"], "the precise error goes back to the model")
        self.assertEqual(len(asked), 1, "escalates once")
        self.assertTrue(rs[2]["escalated"])
        self.assertEqual(asked[0]["status"], "checker_failed")

    def test_bad_inputs_count_as_attempts(self):
        self.card("shout")
        s = callcards.Session(self.repo)
        rs = [s.call("shout", {"path": "a.txt", "mode": m}) for m in ("x", "y", "z", "upper")]
        self.assertEqual([r["status"] for r in rs], ["bad_input"] * 3 + ["closed"])

    def test_identical_inputs_twice_stop_without_running(self):
        self.card("shout", check=FAIL)
        runs = self.spy()
        s = callcards.Session(self.repo)
        first = s.call("shout", {"path": "a.txt"})
        n = len(runs)
        second = s.call("shout", {"path": "a.txt"})
        self.assertEqual((first["status"], second["status"]), ("checker_failed", "repeat"))
        self.assertEqual(len(runs), n)
        self.assertEqual(s.call("shout", {"path": "a.txt", "note": "other"})["status"], "closed")

    def test_a_deterministic_card_never_retries(self):
        self.card("shout", check=FAIL, deterministic=True)
        asked = []
        s = callcards.Session(self.repo, escalate=asked.append)
        self.assertEqual(s.call("shout", {"path": "a.txt"})["status"], "checker_failed")
        self.assertEqual(s.call("shout", {"path": "a.txt", "note": "again"})["status"], "closed")
        self.assertEqual(len(asked), 1)

    def test_a_pass_resets_nothing_and_other_cards_are_untouched(self):
        self.card("shout", check=FAIL)
        self.card("measure", gen=FACTS, check="", read=True)
        s = callcards.Session(self.repo)
        for i in range(3):
            s.call("shout", {"path": "a.txt", "note": str(i)})
        self.assertEqual(s.call("measure", {"path": "a.txt"})["status"], "facts")

    def test_an_identical_call_on_unchanged_files_returns_the_cached_result(self):
        self.card("measure", gen=FACTS, check="", read=True)
        runs = self.spy()
        first = callcards.call("measure", {"path": "a.txt"}, self.repo)
        n = len(runs)
        again = callcards.call("measure", {"path": "a.txt"}, self.repo)
        self.assertEqual(len(runs), n, "no run on a cache hit")
        self.assertTrue(again["cached"])
        self.assertEqual(again["facts"], first["facts"])
        self.write("a.txt", "longer text now\n")
        self.assertFalse(callcards.call("measure", {"path": "a.txt"}, self.repo).get("cached"))

    def test_a_cached_write_replays_on_the_same_files(self):
        self.card("shout")
        callcards.call("shout", {"path": "a.txt"}, self.repo)
        self.write("a.txt", "hello world\n")
        runs = self.spy()
        r = callcards.call("shout", {"path": "a.txt"}, self.repo)
        self.assertEqual((r["status"], r["cached"], runs), ("applied", True, []))
        self.assertEqual(self.read("a.txt"), "HELLO WORLD\n")

    def test_no_card_fits_is_said_and_filed_as_a_card_request(self):
        self.card("shout")
        r = callcards.pick("compose a sonnet about oolong tea for the release notes")
        self.assertEqual(r["status"], "no_card", r)
        self.assertEqual(r["cards"], [])
        with open(callcards.requests_path()) as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual(rows[-1]["text"], "compose a sonnet about oolong tea for the release notes")
        callcards.pick("compose a sonnet about oolong tea for the release notes")
        with open(callcards.requests_path()) as f:
            self.assertEqual(len(f.readlines()), 2, "every miss is filed")

    def test_a_close_task_ranks_the_card(self):
        self.card("shout")
        r = callcards.pick("uppercase the text file a.txt")
        self.assertEqual(r["status"], "cards")
        self.assertEqual(r["cards"][0]["name"], "shout")


@confined
class Guard(Base):
    """The caller's own write rules (fm code's guard) judge a checked change before it lands."""

    def test_a_guard_refusal_says_why_and_keeps_the_tree(self):
        self.card("shout")
        seen, before = [], tree(self.repo)
        r = callcards.call("shout", {"path": "a.txt"}, self.repo, guard=lambda w: seen.append(w) or "Ward: no")
        self.assertEqual(r["status"], "refused", r)
        self.assertIn("Ward: no", r["error"])
        self.assertEqual(seen, [{"a.txt": b"HELLO WORLD\n"}], "the guard sees the checked bytes")
        self.assertEqual(tree(self.repo), before)

    def test_a_guard_that_breaks_refuses(self):
        self.card("shout")

        def boom(writes):
            raise RuntimeError("the hook died")
        r = callcards.Session(self.repo, guard=boom).call("shout", {"path": "a.txt"})
        self.assertEqual(r["status"], "refused", r)
        self.assertIn("the hook died", r["error"])
        self.assertEqual(self.read("a.txt"), "hello world\n")

    def test_cards_are_off_when_the_env_says_so(self):
        for v, want in (("off", False), ("0", False), ("on", True), (None, True)):
            env = {"SAMPO_CARDS": v} if v else {}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("FOREMAN_CARDS", None)
                if v is None:
                    os.environ.pop("SAMPO_CARDS", None)
                self.assertEqual(callcards.enabled(), want, v)


@confined
class Text(Base):
    """What a model reads back from a call."""

    def test_an_applied_call_shows_its_diff_and_a_failure_its_attempt(self):
        self.card("shout")
        ok = callcards.text(callcards.call("shout", {"path": "a.txt"}, self.repo))
        self.assertTrue(ok.startswith("card shout: applied"), ok)
        self.assertIn("+HELLO WORLD", ok)
        r = callcards._result("shout", "checker_failed", error="still lower-case", attempt=2)
        bad = callcards.text(r)
        self.assertTrue(bad.startswith("error: "), bad)
        self.assertIn("attempt 2 of 3", bad)
        self.assertIn("still lower-case", bad)
        self.assertTrue(callcards.text(callcards._result("x", "refused", error="no")).startswith("refused: "))

    def test_facts_are_cut_to_a_bound(self):
        r = callcards._result("measure", "facts", facts={"lines": ["x" * 100] * 500})
        out = callcards.text(r)
        self.assertLessEqual(len(out), callcards.TEXT_CAP + 200)
        self.assertIn("[cut]", out)


class Bench(unittest.TestCase):
    def test_the_sign_test_is_exact_and_two_sided(self):
        self.assertAlmostEqual(callcards.sign_test(9, 1), 22 / 1024)
        self.assertAlmostEqual(callcards.sign_test(1, 9), 22 / 1024)
        self.assertEqual(callcards.sign_test(0, 0), 1.0)
        self.assertEqual(callcards.sign_test(5, 5), 1.0)
        self.assertLess(callcards.sign_test(15, 3), 0.05)
        self.assertGreater(callcards.sign_test(6, 2), 0.05)

    def test_the_summary_pairs_arms_on_one_model_and_says_proven_only_at_p_under_005(self):
        def row(sid, arm, passed, model="m", tokens=100, seconds=10.0):
            return {"run": "s1", "scenario": sid, "arm": arm, "passed": passed, "model": model, "tokens": tokens,
                    "seconds": seconds}
        rows = [row("a", "off", False), row("a", "on", True, tokens=300), row("b", "off", True), row("b", "on", True),
                row("c", "off", False),  # no on arm yet: not a pair
                row("d", "off", False, model="x"), row("d", "on", True, model="y")]  # two models: not a pair
        s = callcards.bench_summary(rows)
        self.assertEqual(s["pairs"], 2)
        self.assertEqual((s["wins"], s["losses"]), (1, 0))
        self.assertEqual(s["arms"]["on"], {"n": 2, "passed": 2, "rate": 1.0, "tokens": 200, "seconds": 10.0,
                                           "card_calls": 0})
        self.assertEqual(s["arms"]["off"]["passed"], 1)
        self.assertEqual(s["verdict"], "not proven")
        many = [row(str(i), "off", False) for i in range(9)] + [row(str(i), "on", True) for i in range(9)]
        self.assertEqual(callcards.bench_summary(many)["verdict"], "proven")
        worse = [row(str(i), "off", True) for i in range(9)] + [row(str(i), "on", False) for i in range(9)]
        self.assertEqual(callcards.bench_summary(worse)["verdict"], "not proven", "a significant loss proves nothing")
        self.assertEqual(callcards.bench_summary([])["pairs"], 0)

    def test_a_runs_events_give_what_it_was_offered_its_card_calls_and_its_trail(self):
        events = [{"event": "note", "text": "calling cards offered: shout", "cards": ["shout"]},
                  {"event": "plan", "plan": {"title": "Fix the parser"}},
                  {"event": "action", "action": "read", "result": "ok"},
                  {"event": "action", "action": "card", "result": "error"},
                  {"event": "action", "action": "shout", "result": "ok"},  # a card called by its own name
                  {"event": "verify", "exit": 0}, {"event": "say", "text": "noise"},
                  {"event": "blocked", "reason": "the criteria still fail"}]
        seen = callcards.bench_events(events, {"shout"})
        self.assertEqual(seen["offered"], ["shout"])
        self.assertEqual((seen["card_calls"], seen["card_ok"]), (2, 1))
        self.assertEqual(seen["trail"], ["plan: Fix the parser", "read:ok", "card:error", "shout:ok", "verify: exit 0",
                                         "blocked: the criteria still fail"])
        self.assertEqual(callcards.bench_events([{"event": "action", "action": "read"}] * 99, set())["trail"],
                         ["read:None"] * 60)


@confined
class Cli(Base):
    def test_list_show_call_and_test(self):
        tests = [{"name": "shouts", "files": {"t.txt": "hi\n"}, "inputs": {"path": "t.txt"}, "expect": "applied",
                  "after": {"t.txt": "HI\n"}},
                 {"name": "bad mode", "files": {"t.txt": "hi\n"}, "inputs": {"path": "t.txt", "mode": "loud"},
                  "expect": "bad_input"},
                 {"name": "plausible but wrong", "files": {"t.txt": "hi\n"}, "inputs": {"path": "t.txt"},
                  "inject": {"t.txt": "Hi\n"}, "expect": "checker_failed"}]
        self.card("shout", tests=tests)
        self.assertIn("shout", self.fm("card", "list").stdout)
        self.assertIn("upper", self.fm("card", "show", "shout").stdout)
        p = self.fm("card", "call", "shout", "path=a.txt", "mode=uper", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("'upper'", p.stdout + p.stderr)
        out = json.loads(self.fm("card", "call", "shout", "path=a.txt", "--json").stdout)
        self.assertEqual(out["status"], "applied")
        self.assertEqual(self.read("a.txt"), "HELLO WORLD\n")
        p = self.fm("card", "test", "shout")
        self.assertIn("3/3", p.stdout)

    def test_bench_without_a_run_summarises_its_rows(self):
        rows = os.path.join(self.tmp, "rows.jsonl")
        with open(rows, "w") as f:
            for arm, passed in (("off", False), ("on", True)):
                f.write(json.dumps({"run": "s1", "scenario": "a", "model": "m", "arm": arm, "passed": passed,
                                    "tokens": 10, "seconds": 1.0, "card_calls": int(passed)}) + "\n")
            f.write("not json\n")
        out = self.fm("card", "bench", "--out", rows).stdout
        self.assertIn("1 pair(s): cards off 0/1", out)
        self.assertIn("cards on 1/1", out)
        self.assertIn("1 card call(s); 1 won, 0 lost, sign test p=1.0: not proven", out)

    def test_bench_refuses_a_limit_under_one_before_it_calls_a_model(self):
        for n in ("0", "-2"):
            p = self.fm("card", "bench", "s1", "--limit", n, "--model", "no-such-model", check=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("--limit must be at least 1", p.stdout + p.stderr)


class Misses(SampoTestCase):
    """T-3488: a missed scenario files a card request naming its failure class, once per scenario and class; a bench row
    says whether a card applied a change in its run."""

    def test_an_applied_card_call_marks_the_run_a_fix_and_a_read_facts_or_refusal_does_not(self):
        applied = {"event": "action", "action": "card", "result": "ok", "detail": "card shout: applied, changed a.txt\n+X"}
        self.assertTrue(callcards.bench_events([applied], {"shout"})["fix_card_applied"])
        self.assertFalse(callcards.bench_events([dict(applied, detail="card measure: facts\n{}")], {"measure"})[
            "fix_card_applied"])
        self.assertFalse(callcards.bench_events([dict(applied, result="refused", detail="refused: card shout")], {"shout"})[
            "fix_card_applied"])
        self.assertFalse(callcards.bench_events([dict(applied, action="read")], {"shout"})["fix_card_applied"])

    def test_a_missed_run_is_named_by_the_class_its_stream_ended_in(self):
        self.assertIsNone(callcards._miss_class([{"event": "closed"}], True))
        self.assertEqual(callcards._miss_class([{"event": "error", "text": "used all 40 turns"}], False), "loop")
        self.assertEqual(callcards._miss_class([{"event": "timeout"}], False), "timeout")
        self.assertEqual(callcards._miss_class([{"event": "closed", "text": "done"}], False), "false_done")
        self.assertEqual(callcards._miss_class([{"event": "action", "action": "edit", "result": "refused"}], False), "refused")
        self.assertEqual(callcards._miss_class([{"event": "error", "text": "boom"}], False), "other")

    def test_bench_one_names_the_class_and_whether_a_card_applied(self):
        def smith(base, key, work, model, request, events):
            with open(events, "w") as f:
                f.write(json.dumps({"event": "action", "action": "card", "result": "ok",
                                    "detail": "card shout: applied, changed a.txt\n"}) + "\n")
                f.write(json.dumps({"event": "error", "text": "used all 40 turns"}) + "\n")
            return {"done": False, "tokens": 9, "error": None}
        with mock.patch.object(louhi, "_smith_arm", side_effect=smith):
            r = callcards._bench_one(self.tmp, {"id": "s1", "request": "fix it", "defects": []}, "m", "on")
        self.assertFalse(r["passed"])
        self.assertEqual((r["failure_class"], r["fix_card_applied"]), ("loop", True))

    def bench(self, out, verdicts):
        """bench over two scenarios with the arm faked: verdicts maps (scenario, arm) to its failure class, None if it
        passed. The fake's fix flag is set on the passing on arm only: this tests the row's plumbing, not the events."""
        def one(base, key, model, arm):
            cls = verdicts[(key["id"], arm)]
            return {"passed": cls is None, "failure_class": cls, "fix_card_applied": arm == "on" and cls is None,
                    "closed": False, "tokens": 5, "seconds": 1.0, "error": None, "offered": [], "card_calls": 0,
                    "card_ok": 0, "trail": []}
        scen = [{"id": "s1", "lang": "python", "kind": "bug"}, {"id": "s2", "lang": "python", "kind": "bug"}]
        with mock.patch.object(callcards, "_answering", return_value=("m", [])), \
                mock.patch.object(louhi, "load", return_value={"scenarios": scen}), \
                mock.patch.object(louhi, "_key", side_effect=lambda p, run, sid: {"id": sid, "request": f"fix {sid}",
                                                                                   "defects": []}), \
                mock.patch.object(louhi, "root", return_value=self.tmp), \
                mock.patch.object(callcards, "_bench_one", side_effect=one):
            return callcards.bench("p", "r1", ["m"], out, log=lambda line: None)

    def test_a_missed_scenario_files_one_request_per_class_and_bench_rows_carry_the_fix_flag(self):
        out = os.path.join(self.tmp, "bench.jsonl")
        self.bench(out, {("s1", "off"): "loop", ("s1", "on"): "loop", ("s2", "off"): None, ("s2", "on"): None})
        rows = callcards.bench_rows(out)
        self.assertEqual(len(rows), 4)
        self.assertEqual(sorted((r["scenario"], r["arm"], r["fix_card_applied"]) for r in rows),
                         [("s1", "off", False), ("s1", "on", False), ("s2", "off", False), ("s2", "on", True)])
        asks = callcards.bench_rows(callcards.requests_path())
        self.assertEqual([(a["scenario"], a["failure_class"], a["text"]) for a in asks], [("s1", "loop", "fix s1")])
        # a second run of the same miss files nothing new; the same scenario failing another way files its own row
        self.bench(os.path.join(self.tmp, "again.jsonl"),
                   {("s1", "off"): "loop", ("s1", "on"): "timeout", ("s2", "off"): None, ("s2", "on"): None})
        asks = callcards.bench_rows(callcards.requests_path())
        self.assertEqual([(a["scenario"], a["failure_class"]) for a in asks], [("s1", "loop"), ("s1", "timeout")])


if __name__ == "__main__":
    unittest.main()
