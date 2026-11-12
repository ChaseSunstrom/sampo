"""T-2665: three over-refusals the friction pass found: git apply's read-only flags, a sys.path literal, a bare .ssh."""
import os

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on the path)
from test_guard import GuardCase, g


class GitApply(GuardCase):
    def test_read_only_flags_allowed_plain_apply_blocked(self):
        for flag in ("--check", "--stat", "--numstat", "--summary", "--check --stat"):
            with self.subTest(flag=flag):
                self.assertIsNone(self.bash(f"git apply {flag} x.diff", cwd=self.fhome))
        for cmd in ("git apply x.diff", "git apply --stat --apply x.diff", "git apply --check --index x.diff",
                    "git apply x.diff --check --reverse"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.bash(cmd, cwd=self.fhome), cmd)


class SysPath(GuardCase):
    LIB = "{fhome}/plugin/lib"

    def test_sys_path_literal_is_not_a_write_target(self):
        for call in ("insert(0, '%s')", "append('%s')"):
            code = "import sys; sys.path." + (call % self.LIB) + "; print(1)"
            with self.subTest(call=call):
                self.assertIsNone(self.bash(f'python3 -c "{code}"', cwd=self.repo))

    def test_heredoc_that_writes_elsewhere_is_not_refused_for_its_sys_path(self):
        # the shape the guard ledger refused 7 times: a heredoc that puts plugin/lib on sys.path and writes a scratch file
        code = ("python3 - <<'EOF'\nimport sys\nsys.path.insert(0, '%s')\nopen('%s/out.txt', 'w').write('x')\nEOF"
                % (self.LIB, "{repo}"))
        self.assertIsNone(self.bash(code, cwd=self.repo))

    def test_same_literal_opened_for_write_still_blocks(self):
        code = "import sys; sys.path.insert(0, '%s'); open('%s/x.py', 'w').write('1')" % (self.LIB, self.LIB)
        self.assertIsNotNone(self.bash(f'python3 -c "{code}"', cwd=self.repo))
        code = "import sys; sys.path.insert(0, '%s'); open('%s', 'w').write('1')" % (self.LIB, self.LIB)
        self.assertIsNotNone(self.bash(f'python3 -c "{code}"', cwd=self.repo))


class HeredocFilterTail(GuardCase):
    """T-2261: a heredoc fed to python with `2>&1 | tail` after it was read as shell text, so a module named in test
    text or a quoted tower path blocked it (28 blocks in 7 days). Read-only filters keep it fed; every other tail doesn't."""
    TEXT = "import json\ns = \"from fmcore import save_brief\\nsave_brief(x)\"\nprint(json.dumps(s))"
    TOWER = "s = open('{fhome}/state/projects/x/notes.txt').read()\nopen('{repo}/out.txt', 'w').write(s)"
    READ_ONLY = ("2>&1", "| tail -5", "2>&1 | tail -20", "2>&1 | head -n 50", "| grep -E 'ok|FAIL' | wc -l",
                 "| cut -d: -f1 | sort -u | uniq -c", "| sort -k2 -n -r", "2>&1 | grep -v \"^#\" | tail -n 3")

    def run_py(self, body, tail="", pre=""):
        return self.bash(f"{pre}python3 - <<'EOF' {tail}\n{body}\nEOF", cwd=self.repo)

    def test_read_only_filter_tail_judged_as_python(self):
        for body in (self.TEXT, self.TOWER):
            self.assertIsNone(self.run_py(body), body)  # the baseline: the same heredoc with nothing after it
            for tail in self.READ_ONLY:
                with self.subTest(body=body[:20], tail=tail):
                    self.assertIsNone(self.run_py(body, tail))

    def test_other_tails_stay_refused(self):
        for tail in ("| sh", "| bash", "| python3", "| tee out.txt", "| xargs echo", "| sed -i s/a/b/ f.txt",
                     "| tail -5 > out.txt", "2>&1 | tail -5 >> out.txt", "; tail -5", "&& tail -5", "|| tail -5",
                     "| grep `echo x`", "| grep $(echo x)", "| grep \"$X\"", "| grep *", "| sort -o out.txt",
                     "| sort -uo out.txt", "| sort --compress-program=sh", "| sort --out=o.txt", "| uniq - out.txt",
                     "| uniq -c out.txt", "| tail -5 # note", "|& tail -5",
                     # review: after `--` uniq's second name is its output file, whatever it starts with
                     "| uniq -- - -d/../.bashrc", "| uniq - -", "| uniq -c --",
                     # review: a following tail never ends
                     "| tail -f", "| tail -F", "| tail -n5 -f", "| tail --follow=name", "| tail -n 3 '-f'",
                     "| tail --fo"):
            with self.subTest(tail=tail):
                self.assertIsNotNone(self.run_py(self.TEXT, tail))

    def test_redefined_filter_or_second_heredoc_stays_refused(self):
        for pre in ("tail() {{ sh; }}\n", "function tail {{ sh; }}\n", "PATH=/tmp/x:$PATH\n", "alias tail=sh\n",
                    "eval 'x'\n", "source ./env.sh\n", ". ./env.sh\n", "hash -p /bin/sh tail\n", "enable -n tail\n",
                    # review: the hash and alias tables by name, and builtins spelled with quotes or backslashes
                    "BASH_CMDS[tail]=/tmp/e.sh\n", "BASH_ALIASES[tail]=sh\n", "e\\val 'tail(){{ sh; }}'\n",
                    "ev\"\"al 'tail(){{ sh; }}'\n", "s\\ource ./env.sh\n", "'.' ./env.sh\n", "\\. ./env.sh\n",
                    "'tail'(){{ sh; }}\n"):  # bash 5 rejects a quoted function name; refused all the same
            with self.subTest(pre=pre):
                self.assertIsNotNone(self.run_py(self.TEXT, "| tail -5", pre))
        two = "python3 - <<'EOF' 2>&1 | grep -f - <<'B'\n" + self.TEXT + "\nEOF\nx\nB"
        self.assertIsNotNone(self.bash(two, cwd=self.repo))

    def test_writer_bodies_stay_refused_behind_a_filter(self):
        for body in ("import fmcore\nopen('{fhome}/plugin/lib/x.py', 'w').write('1')",
                     "from fmcore import save_brief\nsave_brief('x')",
                     "open('{fhome}/state/projects/x/tasks.json', 'w').write('1')"):
            with self.subTest(body=body):
                self.assertIsNotNone(self.run_py(body, "2>&1 | tail -5"))


class BareCredName(GuardCase):
    """T-2665: kept fail-closed. Exempting code that calls no listed read API failed open (io.FileIO, linecache and
    fileinput read a file with none of them), so a credential name beside the home folder still blocks, text test or not."""

    def test_any_credential_name_beside_home_still_blocks(self):
        for cmd in ("python3 -c \"import os; print(open(os.path.join('{home}', '.ssh', 'id_rsa')).read())\"",
                    "python3 -c \"print(open('{home}/.ssh/id_rsa').read())\"",
                    "python3 -c \"import os; os.listdir('{home}'); print('.ssh')\"",
                    "python3 -c \"import io; print(io.FileIO('{home}' + '/.ssh/' + 'id_rsa').read())\"",
                    "python3 -c \"import linecache; print(linecache.getlines('{home}' + '/.ssh/' + 'id_rsa'))\"",
                    "python3 -c \"import sys; print('.ssh' in sys.argv[0], '{home}')\""):
            with self.subTest(cmd=cmd):
                self.assertBlocked(self.bash(cmd, cwd=self.repo), "credentials")


class NoTaskCase(GuardCase):
    """T-4138: the write detector with no task active (the project's root is `unbriefed`): shapes mined from 106 blocks
    where text that wasn't a write target was named as one ("<proj>/.json", "<proj>/.join(struct.pack(")."""
    HOME_CACHE = "~/.cache/x/out.jsonl"

    def verdict(self, cmd, cwd=None):
        cwd = cwd or self.repo
        ctx = g.Ctx(cwd=cwd, project_root=g.project_root_for(cwd, self.home), home=self.home, foreman_home=self.fhome,
                    scratch=["/tmp"], task_id=None, unbriefed=self.repo)
        return g.check("Bash", {"command": cmd.replace("<proj>", self.repo).replace("<home>", self.home)}, ctx)

    def refusal(self, cmd, **kw):
        r = self.verdict(cmd, **kw)
        return None if r is None else g.message(r, self.ctx(task=None))

    def heredoc(self, body):
        return "python3 - <<'EOF'\n" + body + "\nEOF"


class InterpreterText(NoTaskCase):
    def test_interp_read_only_open_with_a_computed_name_passes(self):  # shape a
        self.assertIsNone(self.refusal(self.heredoc(
            "import json\nfor n in ['a','b']:\n    d=json.load(open(n+'.json'))\n    print(d)")))
        self.assertIsNone(self.refusal(self.heredoc(
            "import json\nfor n in ['a','b']:\n    d=json.load(open(f'{n}.json', 'r'))\n    print(d)")))

    def test_interp_string_method_argument_is_not_a_path(self):  # shape b: the only write is under the home cache
        body = ("import os, sys\nfor f in os.listdir('.'):\n    if not f.endswith('.jsonl'): continue\n"
                f"    open(os.path.expanduser('{self.HOME_CACHE}'), 'w').write(f)")
        self.assertIsNone(self.refusal(self.heredoc(body)))
        body = body.replace("os.path.expanduser('%s')" % self.HOME_CACHE, "'<home>/.cache/x/out.jsonl'")
        self.assertIsNone(self.refusal(self.heredoc(body)))
        for call in ("f.startswith('.git')", "f.split('.py')", "f.strip('/.')", "f.removesuffix('.jsonl')"):
            with self.subTest(call=call):
                self.assertIsNone(self.refusal(self.heredoc(
                    f"import sys\nf = sys.argv[1]\nprint({call})\nopen('/tmp/o.txt', 'w').write(f)")))

    def test_interp_code_fragment_is_never_named(self):  # shape c
        code = "import struct\nxs=[1]\nopen('/tmp/o.bin','wb').write(b''.join(struct.pack('<I', x) for x in xs))"
        self.assertIsNone(self.refusal(self.heredoc(code)))
        r = self.refusal("python3 -c \"import struct; xs=[1]; print(b''.join(struct.pack('<I', x) for x in xs))\"")
        self.assertIsNone(r)
        # unparsed code (a shell expansion in it) is scanned as text: a fragment between two literals is no path either
        r = self.refusal("python3 -c \"import struct; open('/tmp/o.bin','wb').write(b''.join(struct.pack('<I', x) "
                         "for x in $XS))\"")
        self.assertTrue(r is None or ".join(" not in r, r)

    def test_interp_computed_write_target_is_one_generic_refusal(self):  # shape g, last
        for body in ("open(n+'.json', 'w').write('x')", "open(f'{n}.json', 'w').write('x')",
                     "import pathlib\npathlib.Path(n+'.json').write_text('x')"):
            with self.subTest(body=body):
                r = self.refusal(self.heredoc("n = input()\n" + body))
                self.assertIsNotNone(r)
                self.assertIn("can't read", r)
                self.assertNotIn(".json", r)
                self.assertNotIn(self.repo, r)

    def test_interp_real_project_writes_stay_refused(self):  # shape g
        proj = "<proj>/a.py"
        for body in (f"p='{proj}'; s=open(p).read(); open(p,'w').write(s)", f"open('{proj}','a')",
                     f"import pathlib\npathlib.Path('{proj}').write_text('x')",
                     f"import pathlib\npathlib.Path('{proj}').write_bytes(b'x')",
                     f"open('{proj}','w+')", f"open('{proj}',mode='w')", f"open('{proj}','x')",
                     f"import shutil\nshutil.copy('/tmp/a','{proj}')",
                     f"import os\nos.remove('{proj}')",
                     # a read of the same literal on another line doesn't excuse the write
                     f"s=open('{proj}').read()\nopen('{proj}','w').write(s)"):
            with self.subTest(body=body):
                r = self.refusal(self.heredoc(body))
                self.assertIsNotNone(r)
                self.assertIn("no active task", r)
                self.assertIn(self.repo + "/a.py", r)
        pre = "sampo focus T-1 >/dev/null; "  # the same command focusing a task first: no task is active before it runs
        self.assertIsNotNone(self.refusal(pre + self.heredoc(f"open('{proj}','w').write('x')")))

    def test_interp_read_of_a_literal_is_not_a_write_target(self):
        self.assertIsNone(self.refusal(self.heredoc(
            "s=open('<proj>/a.py').read()\nopen('/tmp/o.txt','w').write(s)")))
        # a rebound open is not the builtin: it stays a target
        self.assertIsNotNone(self.refusal(self.heredoc(
            "from os import replace as open\nopen('<proj>/a.py')\nopen('/tmp/o.txt','w').write('x')")))

    def test_interp_string_methods_do_not_hide_real_writers(self):
        # replace/rename keep their arguments as targets: an aliased os.replace looks like str.replace to a text match
        self.assertIsNotNone(self.refusal(self.heredoc("import os\no = os\no.replace('/tmp/a', '<proj>/b.py')")))
        self.assertIsNotNone(self.refusal(self.heredoc(
            "import pathlib\npathlib.Path('/tmp/a').rename('<proj>/b.py')")))
        self.assertIsNotNone(self.refusal(self.heredoc(
            "import os\nos.path.join('<proj>', 'b.py')\nopen('<proj>/b.py', 'w')")))

    def test_interp_a_bare_name_opened_for_write_stays_a_target(self):
        # it was named only by the old scan's garbage pairing; behind an env assignment the code isn't parsed
        body = "print(' | '.join(open('x.py','w').write('') or y for y in z))"
        self.assertIsNotNone(self.refusal("HOME=/tmp/h; python3 - <<'EOF'\n" + body + "\nEOF"))
        self.assertIsNotNone(self.refusal(self.heredoc(body)))
        self.assertIsNone(self.refusal(self.heredoc("print(' | '.join(open('x.py').read() for y in z))")))


class VisibleVariables(NoTaskCase):
    SCRATCH = "/tmp/claude-1000/x/scratchpad"

    def test_visible_var_assignment_resolves_to_scratch(self):  # shape d
        cmd = (f'S={self.SCRATCH}; $S/gl.sh "pipelines/553" > $S/p553.json; jq -r .status $S/p553.json')
        self.assertIsNone(self.refusal(cmd))

    def test_visible_var_survives_a_substitution_tail(self):  # shape d with a $(…) before the redirect
        cmd = (f'S={self.SCRATCH}; cd $(git rev-parse --show-toplevel)/fm; $S/gl.sh "pipelines/553" > $S/p553.json')
        self.assertIsNone(self.refusal(cmd))
        # a name set again later, or to a project folder, is not carried into the tail as the scratch one
        for cmd in (f"S={self.SCRATCH}; cd $(pwd)/fm; echo x > $S/p.json; S=<proj>/out",
                    "D=<proj>/out; cd $(pwd)/fm; echo x > $D/p.json"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.refusal(cmd))

    def test_visible_var_assignment_to_a_project_path_is_named_resolved(self):
        r = self.refusal("D=<proj>/out; echo x > $D/p.json")
        self.assertIsNotNone(r)
        self.assertIn(self.repo + "/out/p.json", r)
        self.assertNotIn("not known before it runs", r)

    def test_visible_var_a_later_or_unknown_assignment_stays_a_guess(self):
        for cmd in ("echo x > $S/p.json; S=/tmp/x", "S=$(pwd); echo x > $S/p.json",
                    "S=/tmp/x; S=$1; echo x > $S/p.json", "read S; echo x > $S/p.json",
                    "S=/tmp/x; for S in a b; do echo x > $S/p.json; done"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.refusal(cmd))

    def test_visible_var_cd_then_a_relative_redirect(self):  # shape e
        scratch = self.SCRATCH
        self.assertIsNone(self.refusal(
            f"cd {scratch}/wt && (make test > ../gate-test.log 2>&1; echo done)"))
        self.assertIsNone(self.refusal(f"cd {scratch}/wt && make test > ../gate-test.log 2>&1"))
        r = self.refusal(f"cd <proj> && (make test > ../{os.path.basename(self.repo)}/gate.log; echo done)")
        self.assertIsNotNone(r)
        self.assertIn(self.repo + "/gate.log", r)

    def test_visible_var_loop_variable_over_literal_words_is_resolved(self):  # shape f
        code = ("import shutil;shutil.rmtree('<proj>/.claude/worktrees/$w/__pycache__',ignore_errors=True)")
        r = self.refusal(f'for w in agent-a agent-b; do python3 -c "{code}"; done')
        self.assertIsNotNone(r)  # a project path with no task: a real write
        self.assertNotIn("$w", r)
        self.assertIn("worktrees/agent-a/__pycache__", r)
        # a loop over words it can't read stays a guess, never a literal "$w" folder
        r = self.refusal(f'for w in $(ls); do python3 -c "{code}"; done')
        self.assertIsNotNone(r)
        self.assertNotIn("$w", r)
        # scratch words pass
        scratch = "import shutil;shutil.rmtree('/tmp/claude-1000/x/$w/__pycache__',ignore_errors=True)"
        self.assertIsNone(self.refusal(f'for w in agent-a agent-b; do python3 -c "{scratch}"; done'))


class PrintfFirstWord(GuardCase):
    """T-3223: printf sets a variable only through -v as its first word; a later -v (command -v inside a substitution
    among its arguments) beside an unquoted $t was refused as 'IFS may be changed' though nothing names IFS."""

    def test_a_later_dash_v_in_printfs_arguments_passes(self):
        for cmd in ('for t in podman docker bwrap; do printf "%s=%s " $t "$(command -v $t || echo -)"; done; echo',
                    "printf '%s\\n' $x \"$(command -v $x)\"", 'printf "%s -v %s" $a $b'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.bash(cmd, cwd=self.repo))

    def test_a_computed_or_hidden_dash_v_stays_refused(self):
        name = '"$(echo SFI | rev)"'
        for cmd in (f"printf -v {name} /; ls $x", f'printf "-v" {name} /; ls $x', f"printf '-v' {name} /; ls $x",
                    f"X=-v; printf $X {name} /; ls $x", f"printf {{{{-v,{name}}}}} /; ls $x",  # the helper formats {…}
                    f"printf `echo -v` {name} /; ls $x", f"true; printf -v{name} /; ls $x"):
            with self.subTest(cmd=cmd):
                self.assertBlocked(self.bash(cmd, cwd=self.repo), "system")
