"""T-3529: the user's private terms (host globs or plain words) live only in the state folder, outside every repo:
sampo private add|rm|list keeps them, never printing or logging one in clear, and core.redact_local (the .sampo
mirror's last pass) writes <host> or <private> in their place."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest

from helpers import PRIVATE_TERMS, PRIVATE_TERMS_REAL, ForemanTestCase, read_text

from sampo import core as c

HOST = "*.home.example-private.test"  # made-up values: never a real private domain in a test
WORD = "wombatcorp"


class PrivateTerms(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def test_add_list_rm_keep_terms_in_the_state_folder_and_never_print_or_log_them(self):
        said = self.fm("private", "add", HOST).stdout + self.fm("private", "add", "WombatCorp").stdout
        path = os.path.join(c.state_dir(), c.PRIVATE_TERMS)
        self.assertTrue(path.startswith(os.path.join(self.home, "state") + os.sep), path)
        self.assertEqual(c.private_terms(), sorted([HOST, WORD]), "kept lowercased")
        self.assertEqual(os.stat(path).st_mode & 0o077, 0, "only the user reads the file")
        listed = self.fm("private", "list").stdout  # not a terminal: a count and digests, never the terms
        ledger = read_text(os.path.join(c.find_project(self.repo).dir, "ledger.jsonl"))
        for text in (said, listed, ledger):
            self.assertNotIn("example-private", text)
            self.assertNotIn(WORD, text.lower())
        self.assertIn("2 private terms", listed)
        self.assertIn(hashlib.sha256(WORD.encode()).hexdigest()[:8], ledger, "a digest and a count are logged")
        self.assertIn('"count": 2', ledger)
        self.fm("private", "rm", "WOMBATCORP")
        self.assertEqual(c.private_terms(), [HOST])
        self.assertNotEqual(self.fm("private", "rm", "not-there", check=False).returncode, 0)
        for bad in ("*", "*.x", "a\nb"):
            self.assertNotEqual(self.fm("private", "add", bad, check=False).returncode, 0, repr(bad))
        self.assertEqual(c.private_terms(), [HOST])

    def test_redact_local_writes_host_for_a_matching_host_and_private_for_a_word(self):
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w") as f:
            json.dump([HOST, WORD, "*", "ab", "*.x"], f)  # a hand-edited term that would match everything is left out
        self.assertEqual(c.private_terms(), [HOST, WORD])
        cases = {
            "GET http://llm.home.example-private.test:8080/v1 ok": "GET http://<host>:8080/v1 ok",
            "ssh alice@Jarvis.HOME.example-private.test now.": "ssh <host> now.",
            "the apex home.example-private.test too": "the apex <host> too",
            "WombatCorp's box": "<private>'s box",
            "nothome.example-private.testing and wombatcorpx stay": "nothome.example-private.testing and wombatcorpx stay",
        }
        for raw, want in cases.items():
            self.assertEqual(c.redact_local(raw), want, raw)
        self.assertTrue(c.has_private("see llm.home.example-private.test"))
        self.assertFalse(c.has_private("see <host>"))
        self.assertTrue(c.has_private("a wombatcorp here", ["WOMBATCORP"]), "terms passed in, any case")
        os.remove(os.path.join(c.state_dir(), c.PRIVATE_TERMS))
        self.assertEqual(c.redact_local("llm.home.example-private.test"), "llm.home.example-private.test")


DAMAGE = ("dir", "json", "object", "bytes", "mode", "empty", "blank", "mixed", "dangling")  # T-4804: the ways the list
# exists and can't be read as terms (a truncated file is empty or blank; a dangling symlink is a path that is not absent)
STRICT = {"sync.py", "readme.py", "rune.py", "release.py", "train.py", "profile.py", "draft.py"}  # leave the machine, land
# in the repo or another agent's context, or write the list (draft.py: the plan committed to a branch, T-4817)
LENIENT = {"board.py", "watch.py", "hooks.py", "repeats.py", "steermeter.py"}  # local display: they call
# private_terms_lenient / redact_display, never the strict reading
STRICT_IN_LENIENT = {("repeats.py", "file"), ("repeats.py", "draft")}  # forge's and the playbook's drafts go to the repo
FIXED_LINE = "the private-term list exists but cannot be read"
# (file, function) of a strict read under an except that goes on without a `raise`; each reviewed (T-4804):
REVIEWED_CARRY_ON = {
    ("profile.py", "leaks"),            # the unreadable list is its own leak kind, never a clean scan
    ("profile.py", "unreadable"),       # returns True: the callers withhold
    ("readme.py", "_route"),            # records a distinct 'private_list: unreadable' status and writes nothing
    ("readme.py", "check"),             # a check, not a write: flags the file 'not scanned'
    ("release.py", "_private_hits"),    # _err(...) raises the refusal; nothing is pushed
    ("release.py", "private_lines"),    # a local diagnostic (line numbers): {} without the list
    ("train.py", "_run"),               # prints one fixed note; every lane is then held by the merge scan
}


class Unreadable(ForemanTestCase):
    """T-4804: a list that exists and can't be read is not 'no terms': the source says so, what leaves the machine or
    lands in the repository refuses with one fixed line (never a term, a path or the file's content), and what only
    decorates local output says once and goes on."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.path = os.path.join(c.state_dir(), c.PRIVATE_TERMS)

    def damage(self, how):
        if os.path.lexists(self.path):
            os.chmod(self.path, 0o600) if os.path.isfile(self.path) else None
            os.remove(self.path) if not os.path.isdir(self.path) else os.rmdir(self.path)
        if how == "dir":
            os.mkdir(self.path)
        elif how == "dangling":
            os.symlink(os.path.join(self.tmp, "no-such-list.json"), self.path)
        else:
            body = {"json": b'["wombat', "object": b'{"a": 1}', "bytes": b"\xff\xfe\x00 not utf-8", "mode": b'["wombatcorp"]',
                    "empty": b"", "blank": b"  \n", "mixed": b'["wombatcorp", 3]'}[how]
            with open(self.path, "wb") as f:
                f.write(body)
            if how == "mode":
                os.chmod(self.path, 0)
                if os.access(self.path, os.R_OK):  # root reads anything
                    self.skipTest("this user reads a mode-0 file")

    def test_the_source_raises_on_an_unreadable_list_and_returns_none_only_for_absent_or_empty(self):
        for body in (None, "[]", '["ab"]'):  # no terms: a missing path, a literal empty list, only too-short entries
            if os.path.lexists(self.path):
                os.remove(self.path)
            if body is not None:
                with open(self.path, "w") as f:
                    f.write(body)
            self.assertEqual(c.private_terms(), [], repr(body))
        for how in DAMAGE:
            with self.subTest(how=how):
                self.damage(how)
                with self.assertRaises(c.PrivateTermsUnreadable) as e:
                    c.private_terms()
                self.assertIsInstance(e.exception, ValueError, "every boundary that catches ValueError holds it")
                self.assertEqual(str(e.exception), c.PRIVATE_UNREADABLE)
                self.assertIn(FIXED_LINE, c.PRIVATE_UNREADABLE)
                for text in (str(e.exception), repr(e.exception)):
                    self.assertNotIn(self.path, text)
                    self.assertNotIn("wombat", text)

    def test_the_lenient_reading_returns_none_and_says_once(self):
        import contextlib
        import io
        self.damage("json")
        err = io.StringIO()
        c._PRIVATE_SAID.clear()
        with contextlib.redirect_stderr(err):
            self.assertEqual(c.private_terms_lenient(), [])
            self.assertEqual(c.redact_display("a http://10.1.2.3/x"), "a http://<host>/x", "the rest still redacts")
            self.assertEqual(c.private_terms_lenient(), [])
        self.assertEqual(err.getvalue().count("could not be read"), 1, err.getvalue())
        self.assertNotIn("wombat", err.getvalue())
        self.assertNotIn(self.path, err.getvalue())

    def test_a_readable_list_reads_the_same_everywhere(self):
        with open(self.path, "w") as f:
            json.dump([HOST, WORD], f)
        self.assertEqual(c.private_terms(), [HOST, WORD])
        self.assertEqual(c.private_terms_lenient(), [HOST, WORD])
        self.assertEqual(c.redact_display("WombatCorp here"), "<private> here")
        self.assertEqual(c.redact_local("WombatCorp here"), "<private> here")

    def test_redact_local_and_has_private_do_not_pass_text_through_on_an_unreadable_list(self):
        self.damage("json")
        with self.assertRaises(c.PrivateTermsUnreadable):
            c.redact_local("wombatcorp at 10.0.0.1")
        with self.assertRaises(c.PrivateTermsUnreadable):
            c.has_private("wombatcorp")
        self.assertEqual(c.redact_local("wombatcorp", ["wombatcorp"]), "<private>", "terms passed in: no read, no refusal")
        self.assertTrue(c.has_private("wombatcorp", ["wombatcorp"]))

    def test_private_add_and_rm_never_overwrite_a_damaged_list_and_list_says_so(self):
        self.damage("json")
        for args in (("add", "wombatcorp"), ("rm", "wombatcorp"), ("list",)):
            got = self.fm("private", *args, check=False)
            self.assertNotEqual(got.returncode, 0, args)
            self.assertIn(FIXED_LINE, got.stdout + got.stderr, args)
            self.assertNotIn("Traceback", got.stderr)
            with open(self.path, "rb") as f:
                self.assertEqual(f.read(), b'["wombat', "the damaged file is the user's to repair: left as it is")

    def test_every_damage_mode_is_refused_by_the_cli_and_the_file_is_kept(self):
        for how in ("empty", "blank", "mixed", "dangling"):
            with self.subTest(how=how):
                self.damage(how)
                before = os.readlink(self.path) if how == "dangling" else read_text(self.path)
                got = self.fm("private", "add", "newcorp", check=False)
                self.assertNotEqual(got.returncode, 0)
                self.assertIn(FIXED_LINE, got.stdout + got.stderr)
                self.assertEqual(os.readlink(self.path) if how == "dangling" else read_text(self.path), before)

    def test_entries_unusable_are_counted_and_said_not_silently_dropped(self):
        import contextlib
        import io
        from sampo import doctor
        with open(self.path, "w") as f:  # too short, too short, and one with a space (private_ok drops all three)
            json.dump(["ab", "x", "two words here", WORD], f)
        self.assertEqual(c.private_terms(), [WORD], "a term that matches everything never counts")
        self.assertEqual(c.private_short(), 3)
        err = io.StringIO()
        c._PRIVATE_SAID.clear()
        with contextlib.redirect_stderr(err):
            c.private_terms_lenient()
            c.private_terms_lenient()
        self.assertEqual(err.getvalue().count("unusable"), 1, err.getvalue())
        self.assertIn("3 entries in the private-term list are unusable", err.getvalue())
        self.assertNotIn("too short", err.getvalue(), "a spaced entry is not too short")
        self.assertNotIn(WORD, err.getvalue())
        r = doctor.check_private_terms()
        self.assertEqual(r.status, "WARN")
        self.assertIn("3 entries in the private-term list are unusable", r.detail)
        with open(self.path, "w") as f:  # a non-empty list with no usable entry is not 'no list'
            json.dump(["ab"], f)
        r = doctor.check_private_terms()
        self.assertEqual(r.status, "WARN")
        self.assertIn("no usable term", r.detail)
        with open(self.path, "w") as f:
            json.dump([WORD], f)
        self.assertEqual((c.private_short(), doctor.check_private_terms().status), (0, "PASS"))

    def test_a_directory_in_place_of_the_list_is_refused_not_replaced(self):
        self.damage("dir")
        got = self.fm("private", "add", "wombatcorp", check=False)
        self.assertNotEqual(got.returncode, 0)
        self.assertTrue(os.path.isdir(self.path))

    def test_doctor_names_an_unreadable_list_and_passes_a_readable_or_absent_one(self):
        from sampo import doctor
        self.assertEqual(doctor.check_private_terms().status, "PASS", "absent")
        with open(self.path, "w") as f:
            json.dump([WORD], f)
        self.assertEqual(doctor.check_private_terms().status, "PASS", "readable")
        self.damage("object")
        r = doctor.check_private_terms()
        self.assertEqual(r.status, "FAIL")
        self.assertIn("cannot be read", r.detail)
        self.assertNotIn(self.path, r.detail)

    def test_the_genesis_export_refuses_and_writes_nothing(self):
        from sampo import rune
        p = c.find_project(self.repo)
        out = os.path.join(self.tmp, "site-genesis")
        os.makedirs(out)
        self.damage("json")
        with self.assertRaises(c.PrivateTermsUnreadable):
            rune.export(p, out)
        self.assertEqual(os.listdir(out), [])

    def test_local_readers_go_on_with_an_unreadable_list(self):
        self.damage("json")
        self.assertEqual(self.fm("status", check=False).returncode, 0)
        self.assertEqual(c.redact_display("a http://10.1.2.3/x"), "a http://<host>/x")

    def test_the_profile_is_withheld_not_cleared_when_the_list_is_unreadable(self):
        # T-4804 review: the profile reaches SessionStart and SubagentStart context, builder briefs, the MCP tool and the
        # codex/gemini rules files: with a damaged list nothing could be redacted, so it is a leak, never a clean scan
        from unittest import mock
        from sampo import profile
        cc = os.path.join(self.tmp, "cc")
        os.makedirs(cc)
        codex = os.path.join(self.tmp, "codex")  # the rules file lands here, never in the real home
        os.makedirs(codex)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cc, "CODEX_HOME": codex}):
            os.makedirs(profile.folder(), exist_ok=True)
            with open(os.path.join(profile.folder(), "seed.md"), "w") as f:
                f.write("- Keep replies short and quiet.\n")
            self.assertEqual(profile.rebuild()["items"], 1)
            built = read_text(profile.path())
            self.assertIn("Keep replies", profile.digest())
            self.assertEqual(profile.check(), [])
            self.assertEqual(profile.withheld(), "")
            self.damage("json")
            self.assertEqual(profile.leaks("plain text"), [profile.UNREADABLE_KIND])
            self.assertEqual((profile.digest(), profile.digest(short=True), profile.full(), profile.note(short=True)),
                             ("",) * 4)
            self.assertEqual(profile.note(), profile.WITHHELD, "a session start says why, in one fixed line")
            self.assertEqual(profile.check(), [profile.WITHHELD])
            self.assertEqual(profile.withheld(), profile.WITHHELD)
            self.assertNotIn(self.path, profile.WITHHELD)
            with self.assertRaises(c.PrivateTermsUnreadable):
                profile.scrub("plain text")
            os.remove(profile.path())
            with self.assertRaises(c.PrivateTermsUnreadable):
                profile.rebuild()
            self.assertFalse(os.path.exists(profile.path()), "a profile built under a damaged list is never written")
            with open(profile.path(), "w") as f:
                f.write(built)
            with self.assertRaises(c.PolicyError) as e:
                profile.link("codex")
            self.assertIn(profile.WITHHELD, str(e.exception))
            for how in ("dir", "blank"):
                self.damage(how)
                self.assertEqual(profile.full(), "", how)
            os.remove(self.path) if not os.path.isdir(self.path) else os.rmdir(self.path)
            self.assertIn("Keep replies", profile.digest(), "repaired: handed out again")

    def test_a_new_reader_of_the_list_must_be_classified(self):
        # T-4804: the silent [] of an unreadable list failed open in 15 readers; a file that reads the list takes a side.
        # An AST walk over every file under plugin/lib: attribute references, aliases (from … import x as y), getattr
        # by name and any use of the list's file name count, not only a call spelled c.private_terms(
        import ast
        lib = os.path.join(c.PLUGIN_ROOT, "lib")
        trees = {}
        for root, dirs, files in os.walk(lib):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith(".py"):
                    trees[os.path.relpath(os.path.join(root, f), lib).removeprefix("sampo" + os.sep)] = ast.parse(
                        read_text(os.path.join(root, f)))
        readers = {"private_terms", "private_terms_lenient", "redact_local", "redact_display", "has_private"}
        names = readers | {"PRIVATE_TERMS"}

        def touches(tree):
            for n in ast.walk(tree):
                if isinstance(n, ast.Attribute) and n.attr in names or isinstance(n, ast.Name) and n.id in names:
                    return True
                if isinstance(n, ast.ImportFrom) and any(a.name in names for a in n.names):
                    return True
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and (
                        n.value in names or len(n.value) < 60 and "private-terms" in n.value):  # getattr(c, "…"), a path
                    return True
            return False

        seen = {f for f, t in trees.items() if touches(t)}
        known = STRICT | LENIENT | {"core.py", "doctor.py"}  # doctor reports the unreadable list: it catches it by name
        self.assertEqual(sorted(seen - known), [], "classify it: strict (refuse) or lenient (redact_display), then list it here")

        bad, carry = _read_findings(trees, LENIENT, STRICT)
        self.assertEqual(bad, [], "classify each read: a display reader is lenient, a strict one never takes the lenient reading")
        self.assertEqual(sorted(carry - REVIEWED_CARRY_ON), [], "a strict read under an except that carries on: re-raise "
                         "PrivateTermsUnreadable first, or review it into REVIEWED_CARRY_ON (file, function)")
        self.assertEqual(sorted(REVIEWED_CARRY_ON - carry), [], "reviewed, but no longer there: drop it from the list")

    def test_the_tripwire_sees_a_read_through_an_import_alias(self):
        # T-4804 round 3: `from sampo.core import private_terms as pt` then pt() is the same read; the walk resolves the alias
        import ast
        lenient_bad = ast.parse("from sampo.core import private_terms as pt\n\ndef show():\n    return pt()\n")
        bad, _ = _read_findings({"x.py": lenient_bad}, {"x.py"}, set())
        self.assertEqual(len(bad), 1, bad)
        self.assertIn("x.py:4", bad[0])
        explicit = ast.parse("from sampo.core import redact_local as rl\n\ndef show(terms):\n    return rl('t', terms)\n")
        self.assertEqual(_read_findings({"x.py": explicit}, {"x.py"}, set()), ([], set()), "terms given: no read of the list")
        strict_catch = ast.parse(
            "from sampo.core import private_terms as pt\n\ndef export():\n    try:\n        return pt()\n    except ValueError:\n"
            "        return []\n")
        bad, carry = _read_findings({"y.py": strict_catch}, set(), {"y.py"})
        self.assertEqual((bad, carry), ([], {("y.py", "export")}), "a strict read through the alias under a carrying handler")
        lenient_in_strict = ast.parse("from sampo.core import redact_display as rd\n\ndef f(t):\n    return rd(t)\n")
        bad, _ = _read_findings({"z.py": lenient_in_strict}, set(), {"z.py"})
        self.assertEqual(len(bad), 1, bad)
        mixed = ast.parse("from sampo.core import redact_local as rl\n\ndef file(b):\n    return rl(b)\n")
        self.assertEqual(_read_findings({"repeats.py": mixed}, {"repeats.py"}, set(), {("repeats.py", "file")})[0], [],
                         "a reviewed strict function inside a display file")


def _read_findings(trees, lenient, strict, strict_ok=None):
    """T-4804: what an AST walk finds about each file's reads of the private-term list. trees: {file: ast}. Returns
    (the display readers that read it strictly or the strict ones that take the lenient reading, as 'file:line: …'; the
    (file, function) pairs where a strict read sits under an except that goes on without a raise). An import alias
    (from sampo.core import private_terms as pt) is resolved to the name it stands for, so pt() counts as a read.
    strict_ok: (file, function) pairs inside a display file that write to the repo and so read strictly (T-4817)."""
    import ast
    strict_ok = STRICT_IN_LENIENT if strict_ok is None else strict_ok
    direct, lenient_names = ("private_terms", "redact_local", "has_private"), ("redact_display", "private_terms_lenient")
    bad, carry = [], set()
    for f in sorted(set(lenient) | set(strict)):
        tree = trees[f]
        alias = {a.asname: a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names if a.asname}
        parents = {ch: n for n in ast.walk(tree) for ch in ast.iter_child_nodes(n)}

        def called(n):
            fn = n.func
            return fn.attr if isinstance(fn, ast.Attribute) else alias.get(fn.id, fn.id) if isinstance(fn, ast.Name) else ""

        def explicit(n):  # redact_local / has_private given the terms to use: no read of the list
            return called(n) in ("redact_local", "has_private") and (len(n.args) >= 2 or any(k.arg == "terms" for k in n.keywords))

        def enclosing(node):
            while node in parents and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node = parents[node]
            return node if node in parents or isinstance(node, ast.FunctionDef) else None

        def fname(node):
            e = enclosing(node)
            return e.name if e is not None else ""

        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        if f in lenient:
            for n in calls:
                if called(n) in direct and not explicit(n) and (f, fname(n)) not in strict_ok:
                    bad.append(f"{f}:{n.lineno}: a display reader is lenient (redact_display, or terms from "
                               "private_terms_lenient)")
            scope = [n for n in calls if (f, fname(n)) in strict_ok]  # the reviewed strict functions: direct reads only
            strict_fns = {fname(n) for n in scope if called(n) in direct and not explicit(n)}
        else:
            for n in ast.walk(tree):
                if isinstance(n, ast.Attribute) and n.attr in lenient_names or isinstance(n, ast.ImportFrom) and any(
                        a.name in lenient_names for a in n.names):
                    bad.append(f"{f}:{n.lineno}: a strict reader never takes the lenient reading")
            scope = calls
            strict_fns = {fname(n) for n in calls if called(n) in direct and not explicit(n) and fname(n)}
            while True:  # a helper that calls a strict helper is strict too (sync._version reads it for _stamps)
                more = {fname(n) for n in calls if called(n) in strict_fns and fname(n)} - strict_fns
                if not more:
                    break
                strict_fns |= more
        for n in scope:
            if not (called(n) in strict_fns or called(n) in direct and not explicit(n)):
                continue
            child, par = n, parents.get(n)
            while par is not None:  # each try whose body holds this strict read
                if isinstance(par, ast.Try) and any(child is s for s in par.body):
                    named = lambda h: {x.id if isinstance(x, ast.Name) else x.attr for x in ast.walk(h.type)  # noqa: E731
                                       if isinstance(x, (ast.Name, ast.Attribute))} if h.type else {"*"}
                    for i, h in enumerate(par.handlers):
                        if any("PrivateTermsUnreadable" in named(g) and any(isinstance(x, ast.Raise) for x in ast.walk(g))
                               for g in par.handlers[:i]):
                            break  # an earlier handler re-raises it: the broad ones after it never see it
                        if named(h) & {"*", "ValueError", "Exception", "BaseException", "PrivateTermsUnreadable"} and not any(
                                isinstance(x, ast.Raise) for x in ast.walk(h)):
                            carry.add((f, fname(child)))
                child, par = par, parents.get(par)
    return bad, carry


class TrackedTree(unittest.TestCase):
    def test_no_tracked_file_holds_a_private_term(self):
        # T-4096: a test carried the JARVIS login for 9 h until fm ship's scan refused it (T-3931); the tracked tree is
        # scanned here, so a gate run before any merge finds it. The term itself is never printed
        if not PRIVATE_TERMS_REAL:  # T-4100: CI has only the made-up term, which these tests use on purpose
            self.skipTest("no private term is configured here (sampo private add)")
        root = os.path.dirname(c.PLUGIN_ROOT)
        needles = sorted({max(t.removeprefix("*.").split("*"), key=len) for t in PRIVATE_TERMS})
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:  # 0600, so no term shows in ps as argv
            f.write("\n".join(needles) + "\n")
            f.flush()
            r = subprocess.run(["git", "-C", root, "grep", "-l", "-z", "-I", "-i", "-F", "-f", f.name],
                               capture_output=True, text=True, timeout=300)
        self.assertIn(r.returncode, (0, 1), "git grep failed")
        hits = [p for p in r.stdout.split("\0") if p and c.has_private(read_text(os.path.join(root, p)), PRIVATE_TERMS)]
        self.assertEqual(hits, [], "these tracked files name one of the user's private terms (sampo private list)")
