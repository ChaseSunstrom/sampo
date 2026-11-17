"""T-2745: the guard knows Sampo. Every refusal under fm (the guard suites' own string literals, test_guard_diff's FORMS)
must come out the same, in the same category, when the command is spelled with sampo, sampo.* modules, SAMPO_* names and
the new verb names; and what is allowed stays allowed."""
import ast
import glob
import os
import re
import unittest

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on the path)
from test_guard import GuardCase
from test_guard_diff import FORMS
import fmguard as g

KNOWS_SAMPO = True  # the CLI may honour the sampo names: the guard refuses under them what it refuses under fm

HERE = os.path.dirname(os.path.abspath(__file__))
_PROG = re.compile(r"(?<![\w./-])fm(?=\s|$)")
_BIN = re.compile(r"/bin/fm\b")
_MOD = re.compile(r"(?<![\w./-])fm([a-z]+)\b(?!\.py)")
_ENV = re.compile(r"\bFOREMAN_(STATE|HOME|CONFINE)\b")


def sampo_spelling(s):
    imports = re.search(r"\b(?:import|from)\s+fm[a-z]", s)
    s = _ENV.sub(r"SAMPO_\1", _BIN.sub("/bin/sampo", _PROG.sub("sampo", s)))
    # T-4906: an fm module's real twin (fmguard is sampo.ward): the writer index refuses a module it doesn't know
    return _MOD.sub(lambda m: g._sampo_name("fm" + m.group(1)), s) if imports else s


def corpus():
    """Every string literal in the guard suites that names fm, an fm module or FOREMAN_STATE/HOME/CONFINE."""
    seen = set()
    for path in sorted(glob.glob(os.path.join(HERE, "test_guard*.py"))):
        if path.endswith("test_guard_sampo.py"):
            continue
        with open(path) as f:
            tree = ast.parse(f.read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and 4 < len(n.value) < 600 and (
                    _PROG.search(n.value) or re.search(r"\b(?:import|from)\s+fm[a-z]", n.value) or _ENV.search(n.value)):
                seen.add(n.value)
    return sorted(seen)


class SameAsFm(GuardCase):
    def cat(self, cmd, cwd):
        try:
            r = self.bash(cmd, cwd=cwd)
        except (KeyError, IndexError, ValueError):  # a literal that is no format string
            return "skip"
        return r.category if r else None

    def test_every_corpus_form_is_judged_the_same_with_sampo(self):
        refused, diffs = 0, []
        for s in corpus():
            for cwd in (self.repo, self.fhome):
                a, b = self.cat(s, cwd), self.cat(sampo_spelling(s), cwd)
                refused += a not in (None, "skip")
                if a != b:
                    diffs.append(f"{s!r}: fm {a}, sampo {b}")
        self.assertGreater(refused, 40, "the corpus found too few refusals: the test would be vacuous")
        self.assertEqual(diffs, [], "\n" + "\n".join(diffs[:30]))

    def test_every_shell_form_refuses_a_user_only_sampo_call(self):
        for payload in ("sampo prove . --yes-run", "sampo task set T-1 --allow core", "sampo ask T-1 core --standing",
                        "sampo task set T-1 --allow=remote", "python3 plugin/bin/sampo posture lite"):
            for form in FORMS:
                cmd = form.replace("{p}", payload)
                with self.subTest(cmd=cmd):
                    twin = form.replace("{p}", payload.replace("sampo", "fm").replace("plugin/bin/fm", "plugin/bin/fm"))
                    self.assertEqual(self.cat(cmd, self.repo), self.cat(twin, self.repo))

    def test_named_refusals(self):
        for cmd in ("sampo prove . --yes-run", "sampo task set T-1 --allow core", "sampo posture lite",
                    "SAMPO_CONFINE=0 sampo code run", "export SAMPO_CONFINE=0", "env SAMPO_CONFINE=0 true",
                    "python3 -c 'import sampo.core; sampo.core.write_meta(1)'",
                    "python3 -c 'from sampo.hooks import save_brief'"):
            with self.subTest(cmd=cmd):
                twin = re.sub(r"\bsampo\b(?!\.)", "fm", cmd.replace("SAMPO_", "FOREMAN_"))
                twin = twin.replace("sampo.core", "fmcore").replace("sampo.hooks", "fmhooks")
                got = self.cat(cmd, self.repo)
                self.assertIsNotNone(got, cmd)
                self.assertEqual(got, self.cat(twin, self.repo))

    def test_allowed_stays_allowed(self):
        for cmd in ("sampo status", "sampo task new 'x' --type T", "python3 -c 'import re, json; print(1)'",
                    "SAMPO_HOME=/tmp/x true", "echo sampo prove --yes-run-not", "sampo --help"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.cat(cmd, self.repo), self.cat(re.sub(r"\bsampo\b", "fm", cmd.replace("SAMPO_", "FOREMAN_")), self.repo))

    def test_read_only_from_import_is_judged_like_a_plain_import(self):
        """T-4893: a from-import of a Sampo module that only reads passed nowhere (the bare package read as an entry
        module, and any from-import was refused once a writer word appeared), while the same read as a plain import
        passed. Now both forms get one rule, and a writer named without a call counts for both."""
        read = "python3 -c 'from sampo import landlock; print(landlock.available())'"
        plain = "python3 -c 'import sampo.landlock; print(sampo.landlock.available())'"
        self.assertIsNone(self.cat(read, self.repo))
        # "$(( 1 )) $(…)" left out: there the word sampo reads as a sampo command the shell fills in (an over-refusal of
        # its own, captured beside T-4893)
        for form in (f for f in FORMS if "'" not in f and "$((" not in f):
            with self.subTest(form=form):
                self.assertEqual(self.cat(form.replace("{p}", read), self.repo), self.cat(form.replace("{p}", plain), self.repo))
        for cmd in ("python3 -c 'from sampo import core as c; print(c.read_meta(p))'",
                    "python3 -c 'from sampo.landlock import available; print(available())'",
                    "python3 -c 'from sampo import core; print(core.read_meta(p))'; grep -c 'write_meta(' x.py",
                    "python3 -c 'from sampo import (\nlandlock,\ncore)\nprint(core.read_meta(p))'"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.cat(cmd, self.repo))
        for cmd in ("python3 -c 'from sampo import core; core.write_meta(p, 1)'",
                    "python3 -c 'from sampo import cli; cli.main([1])'", "python3 -c 'from sampo import landlock, hooks'",
                    "python3 -c 'from sampo import (\nlandlock,\ncli)'", "python3 -c 'from sampo import main'",
                    "python3 -c 'from sampo import *'", "python3 -c 'from sampo.core import *; print(1)'",
                    "python3 -c 'from fmcore import *; print(1)'",
                    "python3 -c 'from sampo.core import write_meta as w; w(p, 1)'",
                    "python3 -c 'from sampo.core import (\nread_meta,\nwrite_meta)\nf = write_meta\nf(p, 1)'",
                    "python3 -c 'from sampo import core as c; f = c.write_meta; f(p, 1)'",
                    "python3 -c 'import sampo.core as c; f = c.write_meta; f(p, 1)'",
                    "python3 -c 'import fmcore as c; f = [c.write_meta][0]; f(p, 1)'",
                    "python3 -c 'import sampo.core as c; f = c.task_set; f(p, 1)'",
                    # its review: import( with no space, a `)` in a comment, a fullwidth letter python reads as plain
                    "python3 -c 'from sampo import(cli); cli.main([1])'",
                    "python3 -c 'from sampo.core import(write_meta as w); w(p, 1)'",
                    "python3 -c 'from sampo import (landlock,  # )\ncli)\ncli.main([1])'",
                    "if true; then python3 -c 'from sampo import (landlock,  # )\ncli)\ncli.main([1])'; fi",
                    "python3 -c 'from sampo import core; core.ｗrite_meta(p, 1)'",
                    "python3 -c 'import sampo.core as c; c.ｗrite_meta(p, 1)'",
                    "python3 -c 'from ｓampo import core; core.write_meta(p, 1)'",
                    "if true; then python3 -c 'import ｓampo.core as c; c.write_meta(p, 1)'; fi"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.cat(cmd, self.repo))

    def test_state_write_via_sampo_home_is_refused_like_foreman_home(self):
        for name in ("FOREMAN_HOME", "SAMPO_HOME"):
            with self.subTest(name=name):
                # the literal scan (what runs when a script can't be parsed): a write to a state path named by the variable
                for var in ("$%s", "${%s}"):
                    hits = g._literal_scan("echo x > %s/state/projects/x/meta.json" % (var % name), self.ctx(cwd=self.repo))
                    self.assertTrue(hits, var % name)

    def test_verb_twins_read_as_their_fm_verb(self):
        for twin, (sub, rest) in {"rune": ("genesis", []), "louhi": ("exam", []), "ward": ("guard", []),
                                  "smith": ("code", []), "moons": ("genesis", ["moonshot"]),
                                  "underworld": ("genesis", [])}.items():
            with self.subTest(twin=twin):
                self.assertEqual(g._fm_subcommand([twin, "x"]), (sub, rest + ["x"]))
        self.assertEqual(g._fm_subcommand(["prove", "x"]), ("prove", ["x"]))  # a name that is no twin passes through

    def test_renamed_modules_are_judged_like_their_fm_module(self):
        pairs = {"fmguard": "sampo.ward", "fmcli": "sampo.cli", "fmgenesis": "sampo.rune", "fmexam": "sampo.louhi",
                 "fmcode": "sampo.smith", "fmcodetui": "sampo.smithtui", "fmmoonshot": "sampo.moons",
                 "fmhooks": "sampo.hooks", "fmserve": "sampo.serve", "fmcore": "sampo.core", "fmdocs": "sampo.docs"}
        self.assertEqual({m: g._sampo_name(m) for m in pairs}, pairs)
        for fm, sampo in pairs.items():
            for tpl in ("python3 -c 'import {m}; {m}.write_meta(p, 1)'", "python3 -c 'import {m}; {m}.run([1])'",
                        "python3 -c 'import {m}; print({m}.read_meta(p))'",
                        "python3 -c \"from {m} import main; main(['task'])\"",
                        "python3 -c 'from {m} import write_meta as w; w(p, 1)'"):
                with self.subTest(fm=fm, tpl=tpl):
                    self.assertEqual(self.cat(tpl.format(m=sampo), self.repo), self.cat(tpl.format(m=fm), self.repo))
        self.assertIsNotNone(self.cat("python3 -c 'import sampo.ward; sampo.ward.write_meta(p, 1)'", self.repo))
        self.assertIsNotNone(self.cat("python3 -c 'import sampo.cli; sampo.cli.main([1])'", self.repo))
        self.assertIsNotNone(self.cat("python3 -c 'import sampo.rune; sampo.rune.write_meta(p, 1)'", self.repo))
        self.assertIsNotNone(self.cat("python3 -c 'import sampo; sampo.main([1])'", self.repo))  # the bare package: an entry

    def test_parity_of_the_small_tables(self):
        self.assertTrue(g._PRE_DENY.match("SAMPO_HOME") and g._PRE_DENY.match("FOREMAN_HOME"))
        ctx = self.ctx(cwd=self.repo)
        for root in ("foreman", "sampo"):
            self.assertTrue(g._literal_scan("echo x > ~/.claude/%s/state/projects/x/meta.json" % root, ctx), root)
        for guard in g.GUARD_FILES:  # a standing core yes never covers the guard, whichever path it lives at
            self.assertFalse(g._standing_covers(os.path.join(self.fhome, "plugin", "lib", guard), ctx), guard)
        self.assertEqual(g.GUARD_FILES, ("fmguard.py", g._sampo_name("fmguard").replace(".", "/") + ".py"))
        self.assertTrue(g._DRAFT_USE.fullmatch("sampo edit < d.txt") and g._DRAFT_USE.fullmatch("fm edit < d.txt"))
        for d in (".foreman", ".sampo"):
            r = g.check("Write", {"file_path": os.path.join(ctx.project_root, d, "tasks.json"), "content": "x"}, ctx)
            self.assertIsNotNone(r, d)
            self.assertEqual(r.category, "state-direct", d)

    def test_knows_sampo(self):
        self.assertTrue(KNOWS_SAMPO)
        self.assertTrue(g.KNOWS_SAMPO)


if __name__ == "__main__":
    unittest.main()
