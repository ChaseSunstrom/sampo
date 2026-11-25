"""T-2745: the guard knows Sampo. Every refusal under fm (the guard suites' own string literals, test_guard_diff's FORMS)
must come out the same, in the same category, when the command is spelled with sampo, sampo.* modules, SAMPO_* names and
the new verb names; and what is allowed stays allowed."""
import ast
import glob
import json
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


class StateEnvAsk(ForemanTestCase):
    """T-5379 (slice B of T-5372): a Bash command that moves where Sampo reads its state (FOREMAN_STATE, FOREMAN_HOME,
    SAMPO_HOME) and runs sampo, fm or plugin code asks the user first: a second layer beside the trust file's own
    refusal of a state the agent made itself (T-2282). An ask where a person can answer (a bench or demo may set them on
    purpose); under headless runs or full autonomy nobody answers a prompt, so there it denies, with a way forward."""

    ASKS = (
        # T-5547 round 6 (revised), Rule A: a Sampo state name in an assignment shape asks wherever it stands in the text.
        # A trailing "NAME=" with no value asks too: it cannot be told from "NAME= sampo status", which empties the state.
        "FOREMAN_STATE=/tmp/s sampo status", "FOREMAN_HOME=/tmp/h fm status", "SAMPO_HOME=/tmp/h sampo status",
        "FOREMAN_PILOT_CLAUDE='sh /tmp/x' sampo status", "export FOREMAN_PILOT_VERB=/tmp/x; true",
        "FOREMAN_PILOT_HEADLESS=0 sampo status",  # round 2: the marker that confines a headless main session
        "env FOREMAN_PILOT_HEADLESS= sampo lane list",  # T-5766: a program run detached
        "FOREMAN_PILOT_LANE=/ sampo status", "export FOREMAN_PILOT_LANE=/tmp/x",  # T-5889: the lane the headless run is confined to
        "FOREMAN_DRIVE_TASK=T-1 sampo status", "export FOREMAN_DRIVE_TASK=T-1",  # T-6521: the drive marker a fix session reads its task by
        "SAMPO_HOME=/tmp/x true", "FOREMAN_HOME=/tmp/h ls", "env FOREMAN_HOME=/tmp/h env",
        "FOREMAN_STATE=/x/plugin/state ls", "echo FOREMAN_HOME=/tmp/h sampo status",
        "git add plugin/lib/fmguard.py && echo SAMPO_HOME=1", "printf '%s\\n' FOREMAN_HOME=/tmp/h",
        "cat <<'EOF'\nFOREMAN_HOME=/tmp/h sampo status\nEOF", "# FOREMAN_HOME=/tmp/h sampo status\nsampo status",
        "sampo status --home FOREMAN_HOME=1", "export HOME; sampo status", "XDG_STATE_HOME_X=/x sampo status",
        "FOREMAN_HOME=/tmp/h python3 plugin/bin/sampo prove . --yes-run", "SAMPO_HOME=/tmp/h ./plugin/bin/sampo status",
        "FOREMAN_STATE=/tmp/s FOO=1 sampo status", "A=1 SAMPO_HOME=/tmp/h sampo status",
        "export FOREMAN_STATE=/tmp/s; sampo status", "export -x SAMPO_HOME=/tmp/h && fm status",
        "export FOREMAN_HOME=/tmp/h\nsampo status", "declare -x FOREMAN_HOME=/tmp/h; sampo status",
        "typeset -x SAMPO_HOME=/tmp/h; fm status", "readonly FOREMAN_STATE=/tmp/s; sampo status",
        "env FOREMAN_HOME=/tmp/h sampo status", "env -i FOREMAN_STATE=/tmp/s fm status",
        "env -u X SAMPO_HOME=/tmp/h python3 plugin/bin/fm status", "/usr/bin/env SAMPO_HOME=/tmp/h sampo status",
        "FOREMAN_HOME+=/x sampo status", "FOREMAN_HOME='/tmp/h' sampo status", 'SAMPO_HOME="/tmp/h" sampo status',
        "sudo FOREMAN_HOME=/tmp/h sampo status", "nohup env SAMPO_HOME=/tmp/h sampo status",
        "(FOREMAN_STATE=/tmp/s sampo status)", "if true; then FOREMAN_STATE=/tmp/s sampo status; fi",
        "cd /tmp && FOREMAN_HOME=/tmp/h sampo status | head", "true | SAMPO_HOME=/tmp/h fm status",
        "bash -c 'FOREMAN_HOME=/tmp/h sampo status'", 'sh -c "export SAMPO_HOME=/tmp/h; fm status"',
        "eval 'FOREMAN_STATE=/tmp/s sampo status'", "echo $(SAMPO_HOME=/tmp/h sampo status)",
        "FOREMAN_HOME=/tmp/h python3 -c 'import sampo.core as c; print(c.state_dir())'",
        "FOREMAN_HOME=/tmp/h python3 plugin/hooks/hook PreToolUse", "FOREMAN_HOME=/tmp/h bash plugin/uninstall.sh",
        "FOREMAN_HOME=/tmp/h python3 -m sampo status", "SAMPO_HOME=/tmp/h sampo-fast PreToolUse",
        "FOREMAN_STATE=/tmp/s xargs sampo status", "FOREMAN_STATE=/tmp/s timeout 5 sampo status",
        "FOREMAN_HOME=/tmp/h cat x | fm status", "echo `SAMPO_HOME=/tmp/h sampo status`",
        "bash <<'EOF'\nSAMPO_HOME=/tmp/h sampo status\nEOF", "bash <<EOF\nexport FOREMAN_STATE=/tmp/s\nfm status\nEOF",
        "FOREMAN_HOME=/tmp/h \\\n  sampo status", "FOREMAN_HOME=/tmp/h; export FOREMAN_HOME; sampo status",
        "bash -lc 'SAMPO_HOME=/tmp/h sampo status'", "bash -c \"bash -c 'FOREMAN_HOME=/x fm status'\"",
        "FOREMAN_HOME=/tmp/h python3 -c \"from sampo import core; print(core.state_dir())\"",
        "FOREMAN_STATE=/tmp/s python3 plugin/tests/run.py test_x; sampo status",
        # review 1: a name split by quotes or a backslash, or built from an expansion, is the same name to the shell
        'env FOREMAN_"STATE"=/tmp/s sampo status', 'export FOREMAN_HOM""E=/x; sampo status',
        "export FOREMAN_\\STATE=/x; sampo status", 'FOREMAN_STA"TE"=/x sampo status',
        "export FOREMAN_$X=/x; sampo status", "env 'SAMPO_HOME'=/x fm status",
        # review 2: past the bounds the rest is read as words, never let through
        "eval eval eval eval eval eval 'FOREMAN_STATE=/tmp/s sampo status'",
        "bash -c :; " * 70 + "bash -c 'FOREMAN_STATE=/x sampo status'", "bash -c :; " * 70 + "FOREMAN_HOME=/x sampo status",
        "eval " * 40 + "'FOREMAN_HOME=/tmp/h sampo status'",
        # review 3: the runner exemption is the plugin's own tests/run.py through an interpreter, nothing else
        "FOREMAN_HOME=/tmp/h sampo prove tests/run.py --yes-run", "FOREMAN_HOME=/tmp/h fm prove plugin/tests/run.py",
        "FOREMAN_HOME=/tmp/h python3 /tmp/proj/tests/run.py; sampo status",
        "cat > /tmp/p/tests/run.py <<'EOF'\nimport sampo.core\nEOF\nFOREMAN_HOME=/x python3 /tmp/p/tests/run.py",
        # review 4: python's own code and flag clusters, a here-string, a heredoc fed to an interpreter
        "FOREMAN_STATE=/x python3 -c \"import os; os.environ['FOREMAN_STATE']='/x'; import sampo.core\"",
        "python3 -c \"import os; os.environ['FOREMAN_STATE']='/x'; import sampo.core\"",
        "python3 -Ic \"import os; os.environ['SAMPO_HOME']='/x'; import sampo\"",
        "FOREMAN_HOME=/x python3 -Ic 'import sampo.core'", "FOREMAN_HOME=/x python3 -msampo status",
        "FOREMAN_HOME=/x bash <<< 'sampo status'", "FOREMAN_HOME=/x sh <<<'fm status'",
        "python3 <<EOF\nimport os\nos.environ['FOREMAN_STATE']='/x'\nimport sampo.core\nEOF",
        "FOREMAN_HOME=/x python3 <<'EOF'\nfrom sampo import core\nEOF",
        # review 5: a carrier that is not the program in the lead
        "cd plugin && FOREMAN_STATE=/x python3 bin/sampo status",
        "FOREMAN_HOME=/x ~/.claude/plugins/cache/m/sampo/1.0/bin/sampo status",
        "FOREMAN_STATE=/x find . -exec sampo status \\;", "FOREMAN_STATE=/x make sampo", "FOREMAN_STATE=/x watch sampo status",
        "FOREMAN_STATE=/x parallel sampo ::: a b", "FOREMAN_STATE=/x su -c 'sampo status'",
        "echo 'sampo status' > /tmp/x.sh; FOREMAN_STATE=/x bash /tmp/x.sh",
        "echo 'sampo status' > /tmp/x.sh; FOREMAN_STATE=/x source /tmp/x.sh",
        "echo 'fm status' > /tmp/x.sh; export FOREMAN_STATE=/x; . /tmp/x.sh", "FOREMAN_HOME=/x zsh -c 'sampo status'",
        "FOREMAN_HOME=/x fish -c 'sampo status'", "FOREMAN_HOME=/x python3.12 -c 'import sampo'",
        # review 6: where the state folder is found moves with these too
        "HOME=/tmp/h sampo status", "XDG_STATE_HOME=/tmp/s sampo status", "TMPDIR=/tmp/t sampo status", "TMP=/x fm status",
        "TEMP=/x fm status", "export HOME=/tmp/h; sampo status", "env HOME=/tmp/h sampo status",
        "env -S 'FOREMAN_HOME=/x sampo status'", "su -c 'FOREMAN_HOME=/x sampo status'",
        "FOREMAN_HOME=/x bash -e /tmp/x.sh; echo 'fm status' > /tmp/x.sh",
        # T-5547: the real run is still a run where the program is not the first word: a plugin script under a shell,
        # and a wrapper in front of it. (uv run, make and find -exec are carriers the list does not know: residuals.)
        "HOME=/x bash ./plugin/bin/sampo status", "HOME=/tmp/h npx sampo status", "HOME=/x nohup sampo status",
        # round 3, a name the words do not spell out: a computed or obfuscated one is a set of an unknown name
        "n=FOREMAN_STATE; export $n=/tmp/s; sampo status", "env {FOREMAN,x}_STATE=/tmp/s sampo status",
        "env $'FOREMAN_\\x53TATE=/tmp/s' sampo status", "export $(echo FOREMAN_STATE)=/x; sampo status",
        "export `echo SAMPO_HOME`=/x; sampo status", "declare -x a{b,c}=1; sampo status",
        "n=HOME; env $n=/x sampo status", "n=SAMPO_HOME; export \"$n=/x\"; fm status",
        "bash -c 'n=HOME; export $n=/x; sampo status'",
        # round 3, a script the same command writes holds the set, not only the run
        "printf 'export FOREMAN_STATE=/x\\nsampo status\\n' > /tmp/a.sh; bash /tmp/a.sh",
        "cat > a.sh <<'EOF'\nFOREMAN_STATE=/x sampo status\nEOF\nbash a.sh",
        "echo 'export HOME=/x; sampo status' > /tmp/a.sh; sh /tmp/a.sh",
        "printf 'import os\\nos.environ[\"HOME\"]=\"/x\"\\nimport sampo.core\\n' > /tmp/a.py; python3 /tmp/a.py",
        "cat > /tmp/a.sh <<EOF\nexport SAMPO_HOME=/x\nfm status\nEOF\nsource /tmp/a.sh",
        # round 3, python code that runs Sampo another way, and sets a name another way
        "FOREMAN_STATE=/x python3 -c 'import os, sampo.core'",
        "FOREMAN_STATE=/x python3 -c \"import importlib; importlib.import_module('sampo.core')\"",
        "FOREMAN_STATE=/x python3 -c \"__import__('sampo')\"",
        "FOREMAN_STATE=/x PYTHONPATH=plugin/lib python3 /tmp/shim.py",
        "PYTHONPATH=plugin/lib FOREMAN_HOME=/x python3 -c 'print(1)'",
        "env FOREMAN_STATE=/x PYTHONPATH=/some/plugin/lib python3 /tmp/shim.py",
        "python3 -c \"import os, sampo.core; os.environ.update(HOME='/x')\"",
        "python3 -c \"import os; os.putenv('HOME', '/x'); import sampo\"",
        "python3 -c \"import subprocess; subprocess.run(['sampo','status'], env={'HOME': '/x'})\"",
        "python3 -c \"import subprocess; subprocess.run(['sampo','status'], env=dict(TMPDIR='/x'))\"",
        # round 3: past the bounds the code is read as words too
        "bash -c :; " * 70 + "python3 -c \"import os; os.environ['HOME']='/x'; import sampo.core\"",
        # T-5547 round 5: a set reaches a command that is not a bare text name (through a pipe, a shadowed name)
        "HOME=/x grep -rn sampo plugin/lib | bash", "ls() { sampo status; }; HOME=/x ls",
        # Rule A's state set, Rule B's generic set and a run: the set is in the text, the run is in the same command
        "export FOREMAN_STATE=/tmp/s", "HOME=/x sampo status", "export HOME=/x; echo \"$(sampo state)\"",
        "trap 'sampo state' EXIT; export HOME=/x", "env HOME=/x sampo status",
        "python3 -c 'import os; os.system(\"HOME=/x sampo status\")'", "HOME=/x bash +x plugin/bin/sampo status",
        # T-5547 round 6: the wrappers in _WRAPPER_SPEC, with their options, carry the set to the run
        "HOME=/x sudo -iu u sampo status", "HOME=/x sudo --user u sampo status", "HOME=/x sudo -Eu u sampo status",
        "HOME=/x flock /tmp/l -c 'sampo status'", "HOME=/x sudo sampo status",
        # T-5547 round 6 (security review): (a) the -c string of script, su and runuser, and runuser's command after its options
        "HOME=/x su -c 'sampo status'", "HOME=/x runuser -u u -- sampo status", "HOME=/x runuser -u u -c 'sampo status'",
        "HOME=/x script -c 'sampo status' /dev/null", "HOME=/x script -qc 'sampo status' /dev/null",
        # (b) sudo and doas: the long forms and the options added to the table; an option the guard does not know asks
        "HOME=/x sudo --chdir /tmp sampo status", "HOME=/x sudo --host h sampo status", "HOME=/x sudo --prompt p sampo status",
        "HOME=/x sudo --role r sampo status", "HOME=/x sudo --type t sampo status", "HOME=/x sudo --close-from 5 sampo status",
        "HOME=/x sudo --other-user u sampo status", "HOME=/x sudo -T 5 sampo status", "HOME=/x sudo --command-timeout 5 sampo status",
        "HOME=/x sudo --group g sampo status", "HOME=/x sudo --user=u sampo status", "HOME=/x doas --bogus sampo status",
        "HOME=/x sudo --newopt v sampo status", "HOME=/x doas -Z v sampo status", "HOME=/x sudo --newopt v ls",
        # (c) sudo and doas take NAME=value operands, a computed name too
        "sudo HOME=/x sampo status", "n=HOME; sudo $n=/x sampo status", "sudo FOREMAN_STATE=/x sampo status",
        # (d) an interpreter whose code imports Sampo, or runs one of its modules, is a visible run for a generic name
        "HOME=/x python3 -c 'import sampo'", "HOME=/x python3 -c 'from sampo import core'", "HOME=/x python3 -c 'import fmguard'",
        "HOME=/x python3 -m sampo.cli", "HOME=/x python3 -c'import sampo.core'", "HOME=/x python3 -cimport\\ sampo",
        "HOME=/x python3 -c 'import sampo.core'", "HOME=/x python3 -m timeit 'import sampo.core'",
    )
    PASSES = (
        # T-5547 round 6 (revised), the required passes: a generic name with no visible Sampo run is no run, and a mention is no set
        "HOME=/tmp/h git commit -m 'run sampo status later'", "TMPDIR=/t make", "TMPDIR=/t grep -rn foo . --include=*.py",
        "export TMPDIR=/tmp/t", "git log --grep FOREMAN_HOME", "grep -r SAMPO_STATE plugin/",
        "TMPDIR=/t grep -rn sampo plugin/lib", "HOME=/x ls", "[ -d plugin/lib ] && TMPDIR=/t grep -rn x plugin/lib",
        "sampo status", "fm task list", "python3 plugin/bin/sampo status", "FOO=1 sampo status", "export PATH=/x; sampo status",
        "FOREMAN_HOME_X=1 sampo status", "MY_FOREMAN_HOME=/x sampo status",
        "unset FOREMAN_HOME; sampo status", 'echo "$FOREMAN_HOME"; sampo status',
        # the test runner builds each class's own home; running it is plugin code, but never a state move of the agent's
        "python3 plugin/tests/run.py test_guard_sampo", "python3 plugin/tests/run.py test_guard -k x --deadline 60",
        "FOREMAN_HOME=/tmp/h python3 plugin/tests/run.py test_guard_sampo", "env SAMPO_HOME=/tmp/h python3 plugin/tests/run.py test_docs",
        "TMPDIR=/tmp/t python3 plugin/tests/run.py test_x", "FOREMAN_HOME=/tmp/h python3 -u plugin/tests/run.py test_x", "SAMPO_HOME=/tmp/h python3 -I plugin/tests/run.py test_x",
        "HOMEDIR=/x sampo status", "MY_HOME=/x sampo status", "TMPDIR=/tmp/t ls", "echo HOME=/x sampo status",
        "FOREMAN_HOMES=/x sampo status",
        "python3 -c \"import sampo.core as c; print(c.state_dir())\"",
        # round 3: a computed name, a script or code that never meets Sampo is still no run
        "n=FOREMAN_STATE; echo $n; ls", "echo $(date)=x; ls",
        "PYTHONPATH=plugin/lib python3 -c 'print(1)'", "export A=$(date) B=${C}; sampo status", "FOO=$(sampo status) ls",
        # accepted residuals (T-5547 round 6): a carrier the list does not know, a computed name, a file that is not read here
        "TMPDIR=/t uv run sampo status", "TMPDIR=/t make -f Makefile sampo", "HOME=/x find . -exec sampo status \\;",
        "FOREMAN_${X}=/x sampo status",
        "python3 -c \"from os import environ; environ.setdefault('HOME','/x'); import fmguard\"",
        "HOME=/x; cat plugin/bin/sampo > s.sh; bash s.sh", "HOME=/x wc -l x; alias ls='sampo status'; ls",
        "HOME=/x; . ./plugin/x.sh", "HOME=/x ls; PATH=/tmp/p:$PATH ls",
        # round 6 residuals: a program the list does not name (taskset, chrt, fakeroot, setpriv, prlimit, dbus-run-session,
        # xvfb-run, valgrind, perf), a program or code in a variable, a stdin program, a glob program, code from a variable
        "HOME=/x taskset 1 sampo status", "HOME=/x chrt 1 sampo status", "HOME=/x fakeroot sampo status",
        "HOME=/x setpriv --reuid=1 sampo status", "HOME=/x prlimit --nofile=9 sampo status",
        "HOME=/x dbus-run-session sampo status", "HOME=/x xvfb-run sampo status", "HOME=/x valgrind sampo status",
        "HOME=/x perf stat sampo status", "HOME=/x $S status", "HOME=/x eval \"$CMD\"", "HOME=/x python3 -c \"$CODE\"",
        "HOME=/x echo sampo | xargs", "HOME=/x sam?o status",
        # generic names with no visible Sampo run (Rule B): a git, find, rg or env command is no run
        "TMPDIR=/t find plugin/lib -name '*.py'", "HOME=/x git log --oneline", "HOME=/x rg -n sampo plugin/lib",
        "HOME=/x rg sampo plugin/lib/fmguard.py", "HOME=/tmp/h git commit -m 'fix: sampo, TMPDIR=/x'",
        "TMPDIR=/t git log --grep=sampo", "HOME=/x git diff -- plugin/bin/fm",
        "n=A; export $n=/x; ls", "env {a,b}=1 ls",
        "python3 -c \"import os; os.environ.update(HOME='/x'); print(1)\"",
        "python3 -c \"import subprocess; subprocess.run(['ls'], env={'HOME': '/x'})\"",
        "HOME=/x python3 -m py_compile plugin/lib/fmguard.py",
    )
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(g.__file__))))  # the lane's own root

    def ask(self, cmd, **kw):
        from sampo import hooks
        return hooks._state_env_ask({"tool_name": "Bash", "cwd": self.ROOT, "tool_input": {"command": cmd}}, **kw)

    def test_every_form_that_moves_the_state_and_runs_sampo_asks(self):
        for cmd in self.ASKS:
            with self.subTest(cmd=cmd):
                got = self.ask(cmd)
                self.assertIsNotNone(got, cmd)
                self.assertEqual(got[0], "ask")
                self.assertRegex(got[1], r"STATE|HOME|TMP|TEMP|FOREMAN_|SAMPO_|name|1 MB")
                self.assertTrue(g.state_env_run(cmd, self.ROOT), cmd)

    def test_commands_that_only_mention_it_or_only_run_sampo_pass(self):
        for cmd in self.PASSES:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.ask(cmd), cmd)
                self.assertFalse(g.state_env_run(cmd, self.ROOT), cmd)

    MENTIONS = (
        # T-5547: a command that only names sampo, fm or a plugin path in its words or its quoted text is no run, even
        # with a HOME or TMPDIR assignment in front (the state variables the guard watches)
        "TMPDIR=/tmp/t grep -rn sampo plugin/lib", "HOME=/tmp/h grep -rn 'fm status' plugin/lib",
        "TMPDIR=/tmp/t grep -n 'plugin/bin/sampo' plugin/lib/fmguard.py",
        "TMPDIR=/t cat plugin/bin/sampo", "HOME=/x ls plugin/hooks", "TMP=/x wc -l plugin/lib/sampo/hooks.py",
        "HOME=/tmp/h cat > /tmp/notes.md <<'EOF'\nfm status is fine\nEOF",
        "TMPDIR=/x echo \"sampo\" > /tmp/n.txt",
        "sampo capture 'set HOME=/x then run sampo status'", "sampo task new 'TMPDIR=/tmp/t and sampo' --type T",
        "sampo task new 'set HOME=/x then sampo status' --type T", "fm task note T-1 'export TMPDIR=/x; fm status'",
        "sampo capture \"HOME=/x\" --text 'sampo'",
        # T-5547 final round: a state name inside quoted text is a mention (no assignment, setter or unquoted word)
        "git commit -m 'FOREMAN_STATE=x sampo'", "grep -rn SAMPO_HOME= plugin",
        "sampo task new 'see FOREMAN_STATE=/x in sampo' --type T",
        "echo 'SAMPO_HOME=/tmp/h' | grep sampo", "sampo task new 'set FOREMAN_STATE=/x then sampo status' --type T",
    )

    def test_a_command_that_only_mentions_sampo_in_its_words_or_quoted_text_passes(self):
        for cmd in self.MENTIONS:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.ask(cmd), cmd)
                self.assertFalse(g.state_env_run(cmd, self.ROOT), cmd)

    def test_a_script_path_too_long_to_search_is_read_as_written(self):
        """T-5547: a path past the search bound is taken as written, so the command's text is read and the answer asks."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            long = os.path.join(d, "a" * 1100 + ".sh")
            self.assertIsNotNone(self.ask(f"echo 'sampo status' > {long}; FOREMAN_HOME=/x bash {long}"))
            self.assertIsNone(self.ask(f"bash {long}"))  # no set in the command, and the path is not read: no ask

    # T-5547 review: five bypasses the narrowing opened; each shape must ask, and each test names its bypass
    # T-5547 round 6 (revised): these carriers are not in _WRAPPER_SPEC or the runner list, and git's and gdb's own
    # argument words are not read for Sampo (Rule B asks only for a parsed command word that runs it). Accepted residuals.
    RESIDUAL_CARRIERS = (
        "HOME=/x taskset -c 0 sampo status", "HOME=/x chrt 1 sampo status", "HOME=/x fakeroot sampo status",
        "HOME=/x setpriv --reuid=1 sampo status", "HOME=/x prlimit --nofile=9 sampo status",
        "HOME=/x dbus-run-session sampo status", "HOME=/x xvfb-run sampo status", "HOME=/x valgrind sampo status",
        "HOME=/x perf stat sampo status", "HOME=/x gdb -batch -ex run sampo",
        "HOME=/x git -c alias.x='!sampo status' x", "HOME=/x someunknownrunner sampo status",
        "HOME=/x git rebase -x 'sampo status' HEAD~1", "HOME=/x git submodule foreach 'sampo status'",
        "HOME=/x python3 -X dev {d}/none.py plugin/lib", "HOME=/x python3 -m pytest plugin/lib/fmguard.py -k sampo",
        "HOME=/x bash {d}/none.sh plugin/bin/sampo", "S=sampo; HOME=/x $S status", "P=plugin/bin/sampo; HOME=/x $P status",
        "CMD='sampo status'; HOME=/x eval \"$CMD\"", "CODE='import sampo.core'; HOME=/x python3 -c \"$CODE\"",
        "echo sampo | HOME=/x xargs -I{} sh -c '{}'", "HOME=/x ~/.local/bin/sam?o status",
    )
    # {d} is a fresh tempfile directory (see _asks_all): the shapes name paths there, never a fixed name
    BYPASS_WRITES = (
        "echo 'sampo status' >| {d}/w.sh; HOME=/x bash {d}/w.sh", "echo 'sampo status' &> {d}/w.sh; HOME=/x bash {d}/w.sh",
        "printf 'sampo status\\n' 2> {d}/w.sh; HOME=/x sh {d}/w.sh", "printf 'sampo status\\n' 1>> {d}/w.sh; HOME=/x sh {d}/w.sh",
        "echo 'sampo status' | tee -a {d}/w.sh; HOME=/x bash {d}/w.sh",
        "echo 'sampo status' | tee --append {d}/w.sh; HOME=/x bash {d}/w.sh",
        "echo 'sampo status' > {d}/sub/../w.sh; HOME=/x bash {d}/w.sh", "echo 'sampo status' > '{d}/w.sh'; HOME=/x bash {d}/w.sh",
    )
    BYPASS_OPTIONS = (
        "HOME=/x bash +x plugin/bin/sampo status", "HOME=/x sudo -iu u sampo status", "HOME=/x sudo --user u sampo status",
        "HOME=/x sudo -Eu u sampo status", "HOME=/x doas -u u sampo status", "HOME=/x flock {d}/l -c 'sampo status'",
        "HOME=/x timeout -k 1 5 sampo status",
    )
    BYPASS_ARGS = (
        "HOME=/x sh {d}/none.sh sampo status", "HOME=/x zsh {d}/none.zsh fm status",
    )
    BYPASS_UNWRITTEN = (
        "install {d}/src5 {d}/g5.sh && HOME=/x bash {d}/g5.sh  # sampo status",
        "HOME=/x bash {d}/none.sh; # sampo status",
    )
    # T-5547 round 3: a git alias set by -c in another case, a runner not on any list, a script rewritten by cp,
    # a relative script after cd, written in the same command
    BYPASS_ROUND3 = (
        "cd {d} && printf 'sampo status\\n' > s.sh && HOME=/x bash ./s.sh",
    )
    # T-5547 round 4: stdin and process substitution are programs, a $ in the program's name, interpreter code
    BYPASS_ROUND4 = (
        "echo 'sampo status' | HOME=/x bash", "HOME=/x bash <(echo 'sampo status')",
        "HOME=/x node -p 'require(\"sampo\")'", "HOME=/x php -r 'system(\"sampo status\");'",
    )

    # T-5547 round 8 (the last round): glued -c and long --command forms of the runners, every sudo/doas word, the
    # words after a runner's options, imports read by ast, Rule B scoped to its own simple command, the 1 MB script
    R8_GLUED_RUNNERS = (
        "HOME=/x su -c'sampo status'", "HOME=/x runuser -u u -c'sampo status'", "HOME=/x script -qc'sampo status' /dev/null",
        "HOME=/x su --command='sampo status'", "HOME=/x script --command='sampo status' /dev/null",
        "HOME=/x script --session-command='sampo status' /dev/null", "HOME=/x runuser --comm='sampo status' u",
        "HOME=/x su --session-command=sampo", "HOME=/x runuser -u u -- nice sampo status",
    )
    R8_SUDO_WORDS = (
        "HOME=/x env sudo --chroot /tmp sampo status", "timeout 5 sudo HOME=/x sampo status",
        "HOME=/x timeout 5 sudo sampo status", "HOME=/x sudo -u u --chroot /tmp sampo status",
    )
    R8_IMPORTS = (
        "HOME=/x python3 -c 'import os, sampo'", "HOME=/x python3 -c 'from sampo.core import x'",
        "HOME=/x python3 -c \"__import__('sampo')\"", "HOME=/x python3 -c \"importlib.import_module('sampo.core')\"",
        "HOME=/x python3 -c 'import os\nimport sampo'", "HOME=/x python3 -c 'import sampo as s'",
    )

    R8_UNKNOWN_OPTIONS = (
        "HOME=/x runuser -Q 1 -- sampo status", "HOME=/x su --bogus 1 sampo status", "HOME=/x script -Z sampo status /dev/null",
        "HOME=/x runuser -u u -Q -- sampo status",
    )

    def test_round8_glued_and_long_command_forms_of_the_runners_ask(self):
        self._asks_all(self.R8_GLUED_RUNNERS)

    def test_round8_an_unknown_option_of_su_runuser_or_script_asks(self):
        self._asks_all(self.R8_UNKNOWN_OPTIONS)
        self._passes_all(("HOME=/x runuser -u u -- ls", "HOME=/x su -l u -c 'ls'"))

    def test_round8_every_sudo_or_doas_word_is_checked_not_only_the_head(self):
        self._asks_all(self.R8_SUDO_WORDS)

    def test_round8_imports_are_read_by_ast_and_dunder_import_asks(self):
        self._asks_all(self.R8_IMPORTS)

    def test_round8_rule_b_is_scoped_to_its_own_simple_command_and_rule_a_is_not(self):
        self._passes_all(("TMPDIR=/t make; sampo status", "HOME=/x ls; sampo status"))
        self._asks_all(("TMPDIR=/t sh -c 'sampo status'", "FOREMAN_STATE=/x make; sampo status"))

    def test_round8_a_script_over_1_mb_is_not_asked_on_its_size_alone(self):
        """T-5547 final round: a script past 1 MB asks only for a name the first 1 MB sets (Rule A or B), never for size."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            big = os.path.join(d, "big.sh")
            with open(big, "w") as f:
                f.write("echo ok\n" * 200000)  # about 1.6 MB, no name in it
            self.assertIsNone(self.ask(f"bash {big}"))
            self.assertFalse(g.state_env_run(f"bash {big}", self.ROOT))
            self.assertIsNone(self.ask(f"node {big}"))
            self.assertIsNone(self.ask(f"./configure --prefix=/usr"))

    def test_round8_a_name_inside_the_first_1_mb_of_a_big_script_asks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            big = os.path.join(d, "big.sh")
            with open(big, "w") as f:
                f.write("export FOREMAN_STATE=/x\n" + "echo ok\n" * 200000)  # the name is in the first 1 MB
            got = self.ask(f"bash {big}")
            self.assertIsNotNone(got, big)
            self.assertEqual(got[0], "ask")
            self.assertEqual(g.state_env_run(f"bash {big}", self.ROOT), ["FOREMAN_STATE"])

    def test_round8_a_name_past_the_first_1_mb_of_a_big_script_is_not_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            big = os.path.join(d, "big.sh")
            with open(big, "w") as f:
                f.write("echo ok\n" * 200000 + "export FOREMAN_STATE=/x\n")  # the name is past 1 MB: not read
            self.assertIsNone(self.ask(f"bash {big}"))

    def _captured_whole_walks(self, cmd):
        """The texts each whole-command walk receives for cmd (T-5547 round 9: the process-substitution slices)."""
        seen = []
        real = g._whole_walk
        g._whole_walk = lambda texts, facts, depth: seen.append(list(texts))
        try:
            g.state_env_run(cmd, self.ROOT)
        finally:
            g._whole_walk = real
        return seen

    def test_round9_process_substitution_slices_are_capped_at_8(self):
        seen = self._captured_whole_walks("cat " + "<(true) " * 50)
        self.assertTrue(seen)
        for texts in seen:
            self.assertLessEqual(len(texts), 8)

    def test_round9_each_process_substitution_slice_is_length_bounded(self):
        seen = self._captured_whole_walks("cat <(" + "x" * (1 << 20) + ")")
        self.assertTrue(seen)
        for texts in seen:
            for t in texts:
                self.assertLessEqual(len(t), 1 << 14)

    def test_round9_a_script_is_binary_only_when_its_first_line_holds_a_nul(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            later = os.path.join(d, "later.sh")  # the NUL is on line 2: the script is text, its name is read
            with open(later, "wb") as f:
                f.write(b"echo ok\n\x00\nexport FOREMAN_STATE=/x\n")
            self.assertIsNotNone(self.ask(f"bash {later}"))
            first = os.path.join(d, "first.sh")  # the NUL is on line 1: a binary, no script
            with open(first, "wb") as f:
                f.write(b"\x00export FOREMAN_STATE=/x\n")
            self.assertIsNone(self.ask(f"bash {first}"))

    def test_round9_read_mapfile_and_for_loops_set_their_names(self):
        self._asks_all([
            "read TMPDIR <<< /x; sampo status",
            "mapfile -t HOME < /dev/null; sampo status",
            "for TMPDIR in a b; do :; done; sampo status",
        ])
        self._passes_all(["read -r FOO <<< /x; sampo status", "for i in a b; do :; done; sampo status"])

    def test_round9_the_prefilter_of_a_script_keeps_a_quoted_setter_word(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:  # no = in the script: the setter word, split by quotes, is still read
            s = os.path.join(d, "quoted.sh")
            with open(s, "w") as f:
                f.write('"ex"port HOME\nsampo status\n')
            self.assertIsNotNone(self.ask(f"bash {s}"))

    def _asks_all(self, cmds):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for cmd in cmds:
                cmd = cmd.replace("{d}", d)
                with self.subTest(cmd=cmd):
                    got = self.ask(cmd)
                    self.assertIsNotNone(got, cmd)
                    self.assertEqual(got[0], "ask")
                    self.assertTrue(g.state_env_run(cmd, self.ROOT), cmd)

    def test_bypass_1_round6_residual_carriers_pass_as_documented(self):
        self._passes_all(self.RESIDUAL_CARRIERS)

    def _passes_all(self, cmds):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for cmd in cmds:
                cmd = cmd.replace("{d}", d)
                with self.subTest(cmd=cmd):
                    self.assertIsNone(self.ask(cmd), cmd)
                    self.assertFalse(g.state_env_run(cmd, self.ROOT), cmd)

    def test_bypass_2_every_redirect_form_and_tee_option_that_writes_a_script_asks(self):
        self._asks_all(self.BYPASS_WRITES)

    def test_bypass_3_a_leftover_option_or_a_dropped_sudo_doas_flock_or_timeout_word_asks(self):
        self._asks_all(self.BYPASS_OPTIONS)

    def test_bypass_4_a_shell_or_interpreter_argument_that_names_sampo_asks(self):
        self._asks_all(self.BYPASS_ARGS)

    def test_bypass_5_a_script_not_on_disk_takes_the_whole_command_text_as_written(self):
        self._asks_all(self.BYPASS_UNWRITTEN)

    def test_round3_a_git_alias_by_c_in_any_case_or_an_unknown_runner_asks(self):
        self._asks_all(self.BYPASS_ROUND3)

    def test_round3_a_script_that_cp_rewrites_before_it_runs_is_a_round6_residual(self):
        """T-5547 round 6: the script is read as it is when the guard runs (echo hi here); cp writes sampo status into it
        only inside the command, so the guard cannot see the run. An accepted residual, named in the fmguard docstring."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "src.sh"), "w") as f:
                f.write("sampo status\n")
            with open(os.path.join(d, "s.sh"), "w") as f:
                f.write("echo hi\n")
            cmd = f"cp {d}/src.sh {d}/s.sh; HOME=/x bash {d}/s.sh"
            self.assertIsNone(self.ask(cmd), cmd)
            self.assertFalse(g.state_env_run(cmd, self.ROOT), cmd)

    def test_round4_stdin_process_substitution_indirection_and_interpreter_code_ask(self):
        self._asks_all(self.BYPASS_ROUND4)

    # T-5547 round 4 review (adversary): seven escapes; each shape asks, one BYPASS case per shape
    BYPASS_REVIEW_GIT = (  # HIGH 1: git is text only for its listed subcommands, and not behind a run-making variable
        "FOREMAN_STATE=/x git bisect run sampo status", "FOREMAN_STATE=/x GIT_EXTERNAL_DIFF=sampo git diff",
        "FOREMAN_STATE=/x GIT_SSH_COMMAND=sampo git ls-remote ssh://h/r", "git config alias.s '!sampo status'; FOREMAN_STATE=/x git s",
    )
    BYPASS_REVIEW_S = (  # HIGH 2: -s reads stdin as the script, it is not code
        "echo 'sampo status' | FOREMAN_STATE=/x bash -s", "echo 'sampo status' | HOME=/x sh -s",
    )
    BYPASS_REVIEW_EXPAND = (  # HIGH 3: an expansion in code an eval, a shell or an interpreter runs
        "CMD='sampo status'; FOREMAN_STATE=/x sh -c \"$CMD\"",
    )
    BYPASS_REVIEW_XARGS = (  # HIGH 4: xargs and parallel take their commands from their input
        "echo sampo | FOREMAN_STATE=/x xargs -I{} {} status",
    )
    BYPASS_REVIEW_REG = (  # MEDIUM 5: rg --pre runs its argument
        "FOREMAN_STATE=/x rg --pre sampo x .", "FOREMAN_STATE=/x rg --pre=sampo x .",
    )
    BYPASS_REVIEW_PATH = (  # MEDIUM 6: a program named by its path is read, and a text program is a bare name
        "printf '#!/bin/sh\\nsampo status\\n' > {d}/r; chmod +x {d}/r; HOME=/x {d}/r", "FOREMAN_STATE=/x ./cat sampo status",
    )
    BYPASS_REVIEW_GLOB = (  # MEDIUM 7: brace and glob program words
        "FOREMAN_STATE=/x {sampo,} status",
    )

    def test_review_git_subcommands_and_run_making_variables_ask(self):
        self._asks_all(self.BYPASS_REVIEW_GIT)

    def test_review_s_reads_stdin_not_code_so_it_asks(self):
        self._asks_all(self.BYPASS_REVIEW_S)

    def test_review_an_expansion_in_eval_or_shell_or_interpreter_code_asks(self):
        self._asks_all(self.BYPASS_REVIEW_EXPAND)

    def test_review_xargs_and_parallel_take_their_commands_from_input_and_ask(self):
        self._asks_all(self.BYPASS_REVIEW_XARGS)

    def test_review_rg_pre_runs_its_argument_and_asks(self):
        self._asks_all(self.BYPASS_REVIEW_REG)

    def test_review_a_program_named_by_its_path_is_read_and_a_fake_cat_asks(self):
        self._asks_all(self.BYPASS_REVIEW_PATH)

    def test_review_brace_and_glob_program_words_ask(self):
        self._asks_all(self.BYPASS_REVIEW_GLOB)

    def test_round5_a_script_file_is_read_for_its_sets_up_to_1_mb(self):
        """T-5547 round 5: a script file is read for the state names it sets, up to 1 MB. A set near its end, under the
        bound, is found; past the bound the file is not read, so a set in it is not seen (a judgment call, disclosed)."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            near = os.path.join(d, "near.sh")
            with open(near, "w") as f:
                f.write("sampo status\n" + "true\n" * 100000 + "export HOME=/x\n")  # about 500 KB: read to the end, with a run
            self.assertIsNotNone(self.ask(f"bash {near}"), near)
            far = os.path.join(d, "far.sh")
            with open(far, "w") as f:
                f.write("true\n" * (1 << 19) + "export HOME=/x\n")  # past 1 MB: not read, so no set is seen
            self.assertIsNone(self.ask(f"bash {far}"), far)  # T-5547 final round: past the cap, the file is not read further and its size is no reason to ask

    def test_round6_a_substitution_in_a_word_is_a_run_so_a_set_asks(self):
        """T-5547 round 6 (HIGH): a word holding $( ), a backtick, <( or >( runs code, so a text program with one is no text."""
        for cmd in ('export FOREMAN_STATE=x; echo "$(sampo state)"', "FOREMAN_STATE=/x echo `sampo state`",
                    "export FOREMAN_STATE=/x; echo <(sampo state)", "FOREMAN_HOME=/x ls \"$(sampo status)\"",
                    "export FOREMAN_STATE=/x; cat <<EOF\n$(sampo status)\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.ask(cmd), cmd)

    def test_round6_a_deferred_program_reaches_a_set_before_it(self):
        """T-5547 round 6 (HIGH): a set persists for the whole text, so a program anywhere in it (a trap runs later) is a run."""
        for cmd in ("trap 'sampo state' EXIT; export FOREMAN_STATE=x", "export FOREMAN_STATE=x; trap 'sampo state' EXIT",
                    "export FOREMAN_STATE=x; ls; sampo state"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.ask(cmd), cmd)

    def test_round6_a_script_past_the_size_cap_is_not_asked_and_a_path_program_is_read(self):
        """T-5547 round 6 (MEDIUM), final round: a script over the cap is judged on its first 1 MB, never on its size; a
        program named by a path is read."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            big = os.path.join(d, "big.sh")
            with open(big, "w") as f:
                f.write("true\n" * ((1 << 20) // 5 + 10))  # over the cap, no set in it
            self.assertIsNone(self.ask(f"{big}"), big)
            self.assertIsNone(self.ask(f"bash {big}"), big)
            near = os.path.join(d, "near.sh")
            with open(near, "w") as f:
                f.write("export HOME=/x\nsampo status\n")
            self.assertIsNotNone(self.ask(f"{near}"), near)  # argv[0] is a path: its body is read for the set
            self.assertIsNotNone(self.ask(f"bash {near}"), near)

    def test_round6_text_programs_that_run_code_are_no_text(self):
        """T-5547 round 6 (MEDIUM): [[ and [ run arithmetic subscripts, test and printf -v assign; none is text."""
        for cmd in ("FOREMAN_HOME=/x [[ a[$(sampo status)] ]]", "FOREMAN_HOME=/x [ -d a ] && sampo status",
                    "printf -v FOREMAN_STATE /x; sampo status", "FOREMAN_HOME=/x test -d a && sampo status"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.ask(cmd), cmd)

    def test_a_script_on_disk_that_runs_sampo_is_a_run_and_the_plugins_own_runner_is_not(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            bad, ok = os.path.join(d, "x.sh"), os.path.join(d, "ok.sh")
            for path, body in ((bad, "#!/bin/sh\nsampo status\n"), (ok, "#!/bin/sh\necho hi\n")):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body)
            self.assertIsNotNone(self.ask(f"FOREMAN_HOME=/x bash {bad}"))
            self.assertIsNotNone(self.ask(f"export SAMPO_HOME=/x; source {bad}"))
            self.assertIsNotNone(self.ask(f"FOREMAN_HOME=/x bash {ok}"))  # a set before bash asks, whatever the script holds
            # a tests/run.py of another project, even one that exists, is not the plugin's runner
            os.makedirs(os.path.join(d, "tests"))
            fake = os.path.join(d, "tests", "run.py")
            with open(fake, "w", encoding="utf-8") as f:
                f.write("import sampo.core\n")
            self.assertIsNotNone(self.ask(f"FOREMAN_HOME=/x python3 {fake}"))
        own = os.path.join(self.ROOT, "plugin", "tests", "run.py")
        self.assertIsNone(self.ask(f"FOREMAN_HOME=/x python3 {own} test_docs"))

    def test_a_plugin_shaped_tree_that_is_not_sampos_own_is_no_runner(self):
        """Round 3: plugin/tests/run.py with lib/fmguard.py beside it is a shape anyone can build; the runner is exempt only
        under a root known to be Sampo's (the guard's own plugin, or a worktree of its repository)."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for rel in ("plugin/lib/fmguard.py", "plugin/tests/run.py"):
                path = os.path.join(d, rel)
                os.makedirs(os.path.dirname(path))
                with open(path, "w", encoding="utf-8") as f:
                    f.write("import sampo.core\n")
            fake = os.path.join(d, "plugin", "tests", "run.py")
            self.assertIsNotNone(self.ask(f"FOREMAN_HOME=/x python3 {fake}"))
            self.assertIsNotNone(self.ask(f"cd {d} && SAMPO_HOME=/x python3 plugin/tests/run.py test_x"))
            self.assertIsNotNone(self.ask(f"env -C {d} SAMPO_HOME=/x python3 plugin/tests/run.py test_x"))
            self.assertIsNone(self.ask("SAMPO_HOME=/x python3 plugin/tests/run.py test_x"))  # no cd: the cwd it was given
            # a git worktree of the guard's own repository is the project's own: same common dir
            repo, wt = os.path.join(d, "repo"), os.path.join(d, "wt")
            os.makedirs(os.path.join(repo, ".git", "worktrees", "wt"))
            os.makedirs(wt)
            with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as f:
                f.write(f"gitdir: {os.path.join(repo, '.git', 'worktrees', 'wt')}\n")
            self.assertIsNone(g._git_common(wt))  # git writes the way back too: without it the file is only a claim
            with open(os.path.join(repo, ".git", "worktrees", "wt", "gitdir"), "w", encoding="utf-8") as f:
                f.write(os.path.join(wt, ".git") + "\n")
            self.assertEqual(g._git_common(wt), g._git_common(repo))
            self.assertIsNotNone(g._git_common(repo))
            fake = os.path.join(d, "fake")  # a .git that is a link to the real one is no proof either
            os.makedirs(fake)
            os.symlink(os.path.join(repo, ".git"), os.path.join(fake, ".git"))
            self.assertIsNone(g._git_common(fake))
            self.assertNotEqual(g._git_common(d), g._git_common(repo))
        self.assertIsNotNone(g._git_common(self.ROOT))

    def test_a_huge_word_is_searched_for_the_program_not_returned_whole(self):
        import time
        word = "x" * 5000
        self.assertTrue(g.state_env_run(f'FOREMAN_STATE=/x make "{word} sampo"', self.ROOT))
        self.assertTrue(g.state_env_run(f'FOREMAN_STATE=/x make "{word} plugin/bin/sampo"', self.ROOT))
        self.assertTrue(g.state_env_run(f'FOREMAN_STATE=/x make "{word}"', self.ROOT))  # make is not text: no argument scan
        t = time.monotonic()  # linear in the word, whatever it repeats
        for rep in ("$", "import ", "plugin/", "{", "$("):
            g.state_env_run("export a=1 " + rep * 60000 + "; FOREMAN_STATE=/x ls", self.ROOT)
            g.state_env_run("FOREMAN_STATE=/x make " + rep * 60000 + " sampo", self.ROOT)
        self.assertLess(time.monotonic() - t, 20 * __import__("helpers").busy_factor())

    def test_the_docs_say_where_it_denies(self):
        from sampo import hooks
        for doc in (hooks._state_env_ask.__doc__, self.__class__.__doc__):
            self.assertRegex(doc, r"(?i)deny|denies|refus")
            self.assertRegex(doc, r"(?i)headless")
            self.assertRegex(doc, r"(?i)full autonomy")

    def test_where_nobody_can_answer_a_prompt_it_is_refused_with_a_way_forward(self):
        from unittest import mock
        from sampo import hooks
        cmd = "FOREMAN_STATE=/tmp/s sampo status"
        with mock.patch.object(hooks, "_headless", return_value=True), \
                mock.patch.object(hooks.c, "active_brief_fast", return_value=None):
            got = self.ask(cmd)
        self.assertEqual(got[0], "deny")
        self.assertIn("sampo task block", got[1])
        self.assertIn("state", got[1])
        with mock.patch.object(hooks, "_headless", return_value=False), \
                mock.patch.object(hooks.c, "effective_autonomy", return_value="full"), \
                mock.patch.object(hooks.c, "active_brief_fast", return_value=None):
            self.assertEqual(self.ask(cmd, p=object())[0], "deny")
        with mock.patch.object(hooks, "_headless", return_value=False), \
                mock.patch.object(hooks.c, "effective_autonomy", return_value="ask"):
            self.assertEqual(self.ask(cmd, p=object())[0], "ask")
            self.assertEqual(self.ask(cmd)[0], "ask")

    def test_the_except_branch_reads_the_words_and_stays_bounded(self):
        import time
        from unittest import mock
        with mock.patch.object(g, "_split", side_effect=ValueError("odd")):
            self.assertTrue(g.state_env_run("FOREMAN_HOME=/x sampo status"))
            self.assertTrue(g.state_env_run('env FOREMAN_"HOME"=/x ~/p/bin/fm status'))
            self.assertFalse(g.state_env_run("echo hi; sampo status"))
            self.assertTrue(g.state_env_run("FOREMAN_HOME=/x ls"))  # unparsed: nothing is known to be text, so it asks
            long = "a=1 " * 60000
            t = time.monotonic()
            g.state_env_run(long + "FOREMAN_HOME=" + "x" * 100000)
            g.state_env_run("FOREMAN_" * 30000 + " sampo " * 30000)
            self.assertLess(time.monotonic() - t, 5 * __import__("helpers").busy_factor())

    def test_only_a_bash_command_is_judged(self):
        from sampo import hooks
        cmd = "FOREMAN_HOME=/tmp/h sampo status"
        for tool, ti in (("Write", {"file_path": "/tmp/x", "content": cmd}), ("Read", {"file_path": cmd}),
                         ("Bash", {}), ("Bash", {"command": None}), ("Bash", {"command": 5})):
            self.assertIsNone(hooks._state_env_ask({"tool_name": tool, "tool_input": ti}), (tool, ti))

    def test_a_command_the_parser_cannot_read_asks_when_it_names_both(self):
        for cmd in ("FOREMAN_HOME=/tmp/h sampo status 'unclosed", 'export SAMPO_HOME=/tmp/h; sampo status "x'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.ask(cmd), cmd)

    def test_a_deeply_nested_or_huge_command_is_judged_in_bounded_time(self):
        import time
        nest = "bash -c '" * 3 + "FOREMAN_HOME=/tmp/h sampo status" + "'" * 3
        deep = "eval " * 40 + "'FOREMAN_HOME=/tmp/h sampo status'"
        huge = "echo x; " * 20000 + "FOREMAN_HOME=/tmp/h sampo status"
        t = time.monotonic()
        for cmd in (nest, deep, huge):
            self.assertIsNotNone(self.ask(cmd), cmd[:60])  # past the bounds the rest is read as words: still an ask
        self.assertLess(time.monotonic() - t, 5 * __import__("helpers").busy_factor())

    def test_the_hook_asks_with_the_reason_and_lets_the_plain_call_through(self):
        for cmd in ("FOREMAN_STATE=/tmp/s sampo status", "export SAMPO_HOME=/tmp/h; fm status"):
            with self.subTest(cmd=cmd):
                p = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}})
                self.assertEqual(p.returncode, 0, p.stderr)
                out = json.loads(p.stdout)["hookSpecificOutput"]
                self.assertEqual(out["permissionDecision"], "ask")
                self.assertIn("state", out["permissionDecisionReason"])
        for cmd in ("sampo status", "TMPDIR=/tmp/t ls", "python3 plugin/tests/run.py test_guard_sampo"):
            with self.subTest(cmd=cmd):
                p = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}})
                self.assertNotIn('"ask"', p.stdout, cmd)


if __name__ == "__main__":
    unittest.main()
