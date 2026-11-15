"""T-4736 (T-4724, T-4716): an inline python that only reads files is not a write at a computed path, a function passed
to map over literal paths is bound like a loop, a parameter the guard could not bind has its own reason, and a spawn of
a command taken from argv gets one verdict in node and in python. Only the classifier is called with command text: no
test runs anything, and every path named is a made-up file."""
import os

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on the path)
from test_guard import GuardCase, g
from test_guard_overrefusal import NoTaskCase


def py(body):
    """A python3 heredoc command holding the code."""
    return "python3 - <<'EOF'\n" + body + "\nEOF"


LOOP = "import os\nfor p in os.listdir('/proc'):\n    if p.isdigit():\n"


class ReadModes(NoTaskCase):
    """T-4724: with no task active, a computed path is a write target only when a write may happen through it."""

    def refused(self, cmds, msg="no active task"):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                r = self.refusal(cmd)
                self.assertIsNotNone(r, cmd)
                self.assertIn(msg, r)

    def allowed(self, cmds):
        """Each command alone, and with a shutil call after it: the text gate that reads a script as a writer."""
        for cmd in cmds:
            for tail in ("", "\nimport shutil\nx = shutil.which('git')"):
                cmd2 = cmd.replace("\nEOF", tail + "\nEOF") if tail else cmd
                with self.subTest(cmd=cmd2):
                    self.assertIsNone(self.refusal(cmd2), cmd2)

    def test_the_refused_command_of_2026_11_15_pinned(self):
        self.allowed((
            py(LOOP + "        try: print(p, os.readlink(f'/proc/{p}/cwd'))\n        except OSError: pass"),
            py(LOOP + "        try: print(p, open(f'/proc/{p}/cmdline').read())\n        except OSError: pass"),
            # a .replace( or shutil call elsewhere in the code made the regex gate read the whole script as a writer
            py(LOOP + "        try: print(p, os.readlink(f'/proc/{p}/cwd').replace('x', '~'))\n        except OSError: pass"),
            py(LOOP + "        print(p, open(f'/proc/{p}/cmdline', 'rb').read().replace(b'\\0', b' '))"),
        ))

    def test_computed_path_reads_are_reads(self):
        self.allowed((
            py("n = input()\nprint(open(n + '.json').read())"),
            py("n = input()\nprint(open(f'{n}.json', 'r').read())"),
            py("n = input()\nprint(open(f'{n}.json', 'rb').read())"),
            py("n = input()\nprint(open(f'{n}.json', mode='rt').read())"),
            py("n = input()\nprint(open(file=f'{n}.json').read())"),
            py("import os\nn = input()\nprint(os.readlink(f'{n}.lnk'))"),
            py("import os\nn = input()\nprint(os.stat(f'{n}.json').st_size, os.lstat(n + '.json'))"),
            py("import os\nn = input()\nprint(os.listdir(f'{n}.d'), list(os.scandir(n + '.d')))"),
            py("import os\nn = input()\nprint(os.access(f'{n}.json', os.R_OK))"),
            py("import pathlib\nn = input()\nprint(pathlib.Path(f'{n}.json').read_text())"),
            py("import pathlib\nn = input()\nprint(pathlib.Path(f'{n}.json').resolve(), pathlib.Path(n + '.d').iterdir())"),
            py("import pathlib\nn = input()\nprint(pathlib.Path(f'{n}.json').open().read())"),
            py("import pathlib\nn = input()\nprint(pathlib.Path(f'{n}.json').open('r').read())"),
            py("import io\nn = input()\nprint(io.open(f'{n}.json').read())"),
            py("import io\nn = input()\nprint(io.open(f'{n}.json', 'r').read())"),
        ))

    def test_computed_path_writes_stay_refused(self):
        modes = ("'w'", "'a'", "'x'", "'r+'", "'w+'", "'rb+'", "'wb'", "'ab'", "'xb'", "mode='w'", "mode='a+'", "mode='x'",
                 "mode='r+'", "m", "mode=m", "'r' + 'w'", "'rw'[1]", "chr(119)", "mode='w' if n else 'r'", "*a", "**k",
                 "b'r'", "m or 'r'")
        cmds = []
        for mode in modes:
            cmds += [py(f"n = input()\nm = input()\na = ['r']\nk = {{}}\nopen(f'{{n}}.json', {mode}).write('x')"),
                     py(f"n = input()\nm = input()\na = ['r']\nk = {{}}\nopen(n + '.json', {mode}).write('x')")]
            cmds += [py(f"import io\nn = input()\nm = input()\na = ['r']\nk = {{}}\nio.open(f'{{n}}.json', {mode}).write('x')")]
            cmds += [py(f"import pathlib\nn = input()\nm = input()\na = ['r']\nk = {{}}\n"
                        f"pathlib.Path(f'{{n}}.json').open({mode}).write('x')")]
        self.refused(cmds)

    def test_a_mode_built_at_run_time_or_passed_by_position_stays_refused(self):
        self.refused((
            py("n = input()\nm = ''.join(['w'])\nopen(f'{n}.json', m)"),
            py("n = input()\nm = input()\nopen(f'{n}.json', mode=m)"),
            py("n = input()\nm = input()\nopen(f'{n}.json', m, 1)"),
            py("n = input()\nopen(f'{n}.json', 'r', 1, None, None, '\\n', True, None) if 0 else open(f'{n}.json', 'w', 1)"),
            py("n = input()\nopen(f'{n}.json', 'r' if n else 'w')"),
            py("n = input()\nmode = 'w'\nopen(f'{n}.json', mode)"),
            py("n = input()\nopen(*[f'{n}.json', 'w'])"),
            py("n = input()\nopen(**{'file': f'{n}.json', 'mode': 'w'})"),
        ))

    def test_other_computed_writes_stay_refused(self):
        self.refused((
            py("import os\nn = input()\nfd = os.open(f'{n}.json', os.O_WRONLY | os.O_CREAT)"),
            py("import os\nn = input()\nfd = os.open(n + '.json', os.O_RDWR)"),
            py("import os\nn = input()\nfd = os.open(f'{n}.json', 65)"),
            py("import os\nn = input()\nos.remove(f'{n}.json')"),
            py("import os\nn = input()\nos.unlink(n + '.json')"),
            py("import os\nn = input()\nos.rename(f'{n}.json', 'x')"),
            py("import os\nn = input()\nos.symlink('x', f'{n}.json')"),
            py("import os\nn = input()\nos.truncate(f'{n}.json', 0)"),
            py("import os\nn = input()\nos.chmod(f'{n}.json', 0o777)"),
            py("import os\nn = input()\nos.mkdir(f'{n}.d')"),
            py("import shutil\nn = input()\nshutil.rmtree(f'{n}.d')"),
            py("import shutil\nn = input()\nshutil.copy('a', f'{n}.d')"),
            py("import pathlib\nn = input()\npathlib.Path(f'{n}.json').write_text('x')"),
            py("import pathlib\nn = input()\npathlib.Path(n + '.json').write_bytes(b'x')"),
            py("import pathlib\nn = input()\npathlib.Path(f'{n}.json').unlink()"),
            py("import pathlib\nn = input()\npathlib.Path(f'{n}.json').touch()"),
            py("import pathlib\nn = input()\np = pathlib.Path(f'{n}.json')\np.open('w').write('x')"),
        ))

    def test_a_read_that_is_sent_or_written_on_is_not_excused(self):
        self.refused((
            py("n = input()\nopen('/tmp/o.txt', 'w').write(open(f'{n}.json').read())\nopen(n + '.bak', 'w').write('x')"),
            py("n = input()\nopen(f'{n}.bak', 'w').write(open(f'{n}.json').read())"),
            py("n = input()\np = n + '.bak'\nopen(p, 'w').write(open(f'{n}.json').read())"),
        ))

    def test_a_name_bound_to_a_computed_path_is_followed(self):
        self.refused((
            py("import pathlib\nn = input()\np = pathlib.Path(f'{n}.json')\np.open('w').write('x')"),
            py("n = input()\npath = n + '.json'\nopen(path, 'w').write('x')"),
            py("import os\nn = input()\npath = f'{n}.json'\nos.unlink(path)"),
        ))
        self.allowed((
            py("n = input()\npath = n + '.json'\nprint(open(path).read())"),
            py("n = input()\npath = f'{n}.json'\nprint(open(path, 'rb').read())"),
        ))

    def test_a_function_mapped_over_literal_paths_is_bound_like_a_loop(self):
        w = "def w(p): open(p, 'w').write('x')\n"
        self.allowed((
            py(w + "list(map(w, ['a.txt', 'b.txt']))"),
            py(w + "for _ in map(w, ('a.txt', 'b.txt')): pass"),
            py("list(map(lambda p: open(p, 'w').write('x'), ['a.txt', 'b.txt']))"),
            py("from multiprocessing import Pool\n" + w + "with Pool(2) as pool: pool.map(w, ['a.txt', 'b.txt'])"),
            py("from concurrent.futures import ThreadPoolExecutor as E\n" + w + "with E() as ex: list(ex.map(w, ['a.txt']))"),
        ))

    def test_a_mapped_function_that_reaches_a_system_path_is_refused(self):
        w = "def w(p): open(p, 'w').write('x')\n"
        self.refused((
            py(w + "list(map(w, ['/etc/t4736']))"),
            py(w + "list(map(w, ['a.txt', '/etc/t4736']))"),
            py(w + "list(map(w, ['a.txt', '/etc/t4736']))\nlist(map(w, ['b.txt']))"),
            py("from multiprocessing import Pool\n" + w + "with Pool(2) as pool: pool.map(w, ['/etc/t4736'])"),
            py("list(map(lambda p: open(p, 'w').write('x'), ['/etc/t4736']))"),
        ), msg="blocked system")

    def test_a_parameter_the_guard_cannot_bind_has_its_own_reason(self):
        w = "def w(p): open(p, 'w').write('x')\n"
        for body in (w + "import sys\nlist(map(w, sys.argv[1:]))",
                     w + "list(map(w, input().split()))",
                     w + "import functools\nfunctools.partial(w, 'a')()",
                     w + "list(map(w, open('names.txt').read().split()))"):
            with self.subTest(body=body):
                r = self.refusal(py(body))
                self.assertIsNotNone(r)
                self.assertIn("can't bind", r)
                self.assertNotIn("too many", r)

    def test_map_that_the_code_defines_itself_is_not_read_as_the_builtin(self):
        self.refused((
            py("def w(p): open(p, 'w').write('x')\ndef map(f, xs): return [f('/etc/t4736')]\nlist(map(w, ['a.txt']))"),
            py("def w(p): open(p, 'w').write('x')\nmap = lambda f, xs: f('/etc/t4736')\nmap(w, ['a.txt'])"),
        ), msg="blocked system")

    def test_a_spawn_of_a_command_from_argv_gets_one_verdict_in_node_and_python(self):
        """The rule: the outer command line's own words are the command. A command that reads as allowed there is allowed,
        one refused is refused, one the guard can't read (no words, a word the shell expands) is refused."""
        node = "node -e \"require('child_process').%s\" "
        fn = "execSync(process.argv[1])"
        spawn = "spawnSync(process.argv[1], process.argv.slice(2))"
        py_run = "python3 -c \"import subprocess,sys; subprocess.run(sys.argv[1:])\" "
        py_sh = "python3 -c \"import os,sys; os.system(sys.argv[1])\" "
        for cmd in (node % fn + "'git status'", node % spawn + "git status", py_run + "git status", py_sh + "'git status'",
                    "python3 - git status <<'EOF'\nimport subprocess,sys\nsubprocess.run(sys.argv[1:])\nEOF",
                    "python3 -c \"import subprocess,sys; c = sys.argv[1]; subprocess.run(['sh', '-c', c])\" 'git status'"):
            with self.subTest(allowed=cmd):
                self.assertIsNone(self.refusal(cmd), cmd)
        for cmd in (node % fn + "'rm /etc/t4736'", node % spawn + "rm -rf /etc/t4736", py_run + "rm -rf /etc/t4736",
                    py_sh + "'rm /etc/t4736'", py_sh + "'git status; rm /etc/t4736'",
                    "python3 - rm /etc/t4736 <<'EOF'\nimport subprocess,sys\nsubprocess.run(sys.argv[1:])\nEOF",
                    "python3 -c \"import subprocess,sys; c = sys.argv[1]; subprocess.run(['sh', '-c', c])\" 'rm /etc/t4736'",
                    "python3 -c \"import subprocess,sys; subprocess.run(['sh', '-c', sys.argv[1]])\" 'rm /etc/t4736'"):
            with self.subTest(refused=cmd):
                r = self.refusal(cmd)
                self.assertIsNotNone(r, cmd)
                self.assertIn("/etc/t4736", r)
        for cmd in (node % fn.replace("[1]", "[2]") + "'git status'", node % fn, node % fn + "$(echo rm) /etc/t4736",
                    node % fn + "'$X'", node % fn + "`echo rm`", node % fn + "'*'",
                    py_run.rstrip(), py_run + "$(echo rm) /etc/t4736", py_run + "'$X'", py_sh + "'~/x'", py_sh + "'*'",
                    "echo a | xargs python3 -c \"import subprocess,sys; subprocess.run(sys.argv[1:])\"",
                    "echo a | xargs node -e \"require('child_process').execSync(process.argv[1])\"",
                    "python3 -c \"import subprocess,sys; subprocess.run(sys.argv[1:][0:])\" git status",
                    "python3 -c \"import subprocess,sys; subprocess.run(sys.argv[1].upper())\" git status",
                    "python3 -c \"import subprocess,sys; a = sys.argv; subprocess.run(a[1:])\" git status",
                    "python3 -c \"import subprocess,sys; subprocess.run(sys.argv[3])\" git status"):
            with self.subTest(unreadable=cmd):
                r = self.refusal(cmd)
                self.assertIsNotNone(r, cmd)
                self.assertIn("can't read", r)

    def test_argv_words_a_second_command_adds_do_not_pass_for_the_first(self):
        """Two inline commands: node's words are known only when exactly one `node -e` holds them."""
        two = ("node -e \"require('child_process').execSync(process.argv[1])\" 'git status'; "
               "node -e \"require('child_process').execSync(process.argv[1])\" 'rm /etc/t4736'")
        r = self.refusal(two)
        self.assertIsNotNone(r)

    def test_argv_words_bind_to_a_heredoc_only_in_the_one_unambiguous_case(self):
        """One heredoc, one python reader, the heredoc on that command, nothing else on its stdin: its words are argv.
        Any other layout leaves them unknown (a positional pairing took another command's words for this one's)."""
        run = "import subprocess,sys\nsubprocess.run(sys.argv[1:])\n"
        git = "python3 - git status <<'EOF'\n" + run + "EOF"
        self.assertIsNone(self.refusal(git), git)
        self.assertIsNone(self.refusal("python3 - git status <<'EOF'\nprint(1)\nEOF"))
        rm = "python3 - rm /etc/t4736 <<'EOF'\n" + run + "EOF"
        safe = "python3 - git status <<'EOF'\nprint(1)\nEOF"
        evil_first = "python3 - git status <<'A'\nprint(1)\nA\npython3 - rm /etc/t4736 <<'B'\n" + run + "B"
        for cmd in (rm,
                    # two heredocs: the dangerous one is refused whichever order, the words swapped either way
                    "python3 - rm /etc/t4736 <<'A'\nprint(1)\nA\npython3 - git status <<'B'\n" + run + "B",
                    "python3 - git status <<'A'\n" + run + "A\npython3 - rm /etc/t4736 <<'B'\nprint(1)\nB",
                    evil_first,
                    # a heredoc another command reads while python reads a pipe, a here-string, a file
                    "cat <<'A'\n" + run + "A\necho 'x' | python3 - rm /etc/t4736",
                    "cat <<'A'\n" + run + "A\npython3 - rm /etc/t4736 <<< 'x'",
                    "python3 - rm /etc/t4736 < /etc/t4736/in <<'A'\n" + run + "A",
                    "python3 - rm /etc/t4736 < /etc/t4736/in\ncat <<'A'\n" + run + "A",
                    "echo x | python3 - rm /etc/t4736 <<'A'\n" + run + "A",
                    "tee /dev/null <<'A' | python3 - rm /etc/t4736\n" + run + "A"):
            with self.subTest(refused=cmd):
                self.assertIsNotNone(self.refusal(cmd), cmd)
        # the same layouts with words that read as allowed: the code spawns argv, which now is unknown, so refused too
        for cmd in ("python3 - git status <<'A'\n" + run + "A\npython3 - git log <<'B'\nprint(1)\nB",
                    "cat <<'A'\n" + run + "A\necho x | python3 - git status",
                    "python3 - git status <<< 'x'\ncat <<'A'\n" + run + "A"):
            with self.subTest(unreadable=cmd):
                r = self.refusal(cmd)
                self.assertIsNotNone(r, cmd)
                self.assertIn("can't read", r)

    def test_a_name_from_input_or_argv_is_a_computed_path(self):
        """With no task active, open(n, 'w'), os.remove(n) and Path(n).write_text(…) on a name the script gets when it
        runs are writes at a path the guard can't read, in a heredoc and in `-c` code alike."""
        self.refused((
            py("n = input()\nopen(n, 'w').write('x')"),
            py("import os\nn = input()\nos.remove(n)"),
            py("import pathlib\nn = input()\npathlib.Path(n).write_text('x')"),
            py("import sys\nn = sys.argv[1]\nopen(n, 'w').write('x')"),
            py("import sys, os\nos.unlink(sys.argv[1])"),
            py("import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_bytes(b'x')"),
            py("import os\nn = os.environ['T4736']\nopen(n, 'a').write('x')"),
            py("import sys\nfor n in sys.stdin:\n    open(n.strip(), 'w').write('x')"),
            py("n = open('/etc/t4736/names').read().strip()\nopen(n, 'w').write('x')"),
            "python3 -c \"n = input(); open(n, 'w').write('x')\"",
            "python3 -c \"import os; os.remove(input())\"",
            "python3 -c \"n = input(); open(f'{n}.json', 'w').write('x')\"",
            "python3 -c \"import pathlib; n = input(); pathlib.Path(f'{n}.json').write_text('x')\"",
        ), "a file the script writes at a computed path")
        self.allowed((
            py("n = input()\nprint(open(n).read())"),
            py("import sys\nprint(open(sys.argv[1], 'r').read())"),
            py("import os\nn = input()\nprint(os.stat(n).st_size)"),
        ))
        self.assertIsNone(self.refusal("python3 -c \"print(open(input() + '.json').read())\""))

    def test_an_opener_hides_the_flags_so_it_is_not_a_read(self):
        self.refused((
            py("open('x.txt', 'r', opener=lambda p, f: 0)"),
            py("open('x.txt', 'r', opener=op)"),
            py("open('x.txt', 'r', -1, None, None, None, True, op)"),
            py("n = input()\nopen(n, mode='r', opener=op)"),
            py("import os\nn = input()\nopen(f'{n}.json', 'r', opener=lambda p, f: os.open(p, os.O_WRONLY))"),
            "python3 -c \"open('x.txt', 'r', opener=op)\"",
        ), "")  # (a lambda opener is refused by the parameter rule, with its own reason)

    def test_a_writer_passed_to_map_names_the_paths_it_writes(self):
        self.refused((
            py("import os\nlist(map(os.remove, ['a.json', 'b.json']))"),
            py("import os\nfor _ in map(os.unlink, ['a.json']): pass"),
            py("import shutil\nlist(map(shutil.rmtree, ['a.d']))"),
            py("import os\nn = input()\nlist(map(os.remove, [n + '.json']))"),
            py("import os\nn = input()\nlist(map(os.remove, [n]))"),
            py("import pathlib\nlist(map(pathlib.Path.unlink, [pathlib.Path('a.json')]))"),
            py("from multiprocessing import Pool\nimport os\nPool().map(os.remove, ['a.json'])"),
            py("import os\nn = input()\nrm = os.remove\nrm(n + '.json')"),
            py("list(map(open, ['a.json'], ['w']))"),
            "python3 -c \"import os; list(map(os.remove, ['a.json']))\"",
        ))
        self.allowed((
            py("import os\nprint(list(map(os.stat, ['/proc/1'])))"),
            py("import os\nprint(list(map(os.readlink, ['/proc/1/cwd'])))"),
            py("print(list(map(str.replace, ['a'], ['a'], ['b'])))"),
        ))

    def test_a_spawn_of_a_computed_command_is_unreadable_wherever_it_sits(self):
        """T-4736 review: a command the code computes (os.system(f'rm {n}'), a computed program) is refused as unreadable,
        as node's is, with or without another call that opens the write scan."""
        calls = (
            "os.system(f'rm {n}')", "os.system('rm ' + n)", "os.system('rm %s' % n)", "os.system('rm {}'.format(n))",
            "os.system(n)", "os.popen(f'rm {n}')", "os.popen(n).read()",
            "subprocess.run(f'rm {n}', shell=True)", "subprocess.run('rm ' + n, shell=True)",
            "subprocess.run(n, shell=True)", "subprocess.check_output(f'cat {n}', shell=True)",
            "subprocess.Popen(f'rm {n}', shell=True)", "subprocess.call(f'rm {n}', shell=True)",
            "subprocess.run(['sh', '-c', f'rm {n}'])", "subprocess.run([n, 'x'])", "subprocess.run([f'{n}/payload'])",
            "subprocess.run(n.split())", "subprocess.run(args=f'rm {n}', shell=True)", "subprocess.run(['env', n])",
            "subprocess.run(['xargs', 'rm', n])", "subprocess.run(['python3', '-c', f'import os; os.remove({n!r})'])", "subprocess.run(['bash', '-c', n])", "subprocess.run([*n.split()])",
            "from subprocess import run\nrun(f'rm {n}', shell=True)", "import subprocess as sp\nsp.run(f'rm {n}', shell=True)",
            "os.execv(f'{n}/payload', ['payload'])", "os.execv(n, ['x'])",
            "os.execvp(n, [n])", "os.execve(f'{n}/payload', ['payload'], os.environ)",
            "os.execl(f'{n}/payload', 'payload')", "os.execlp(n, n)", "os.spawnv(os.P_WAIT, f'{n}/p', ['p'])",
            "os.spawnlp(os.P_WAIT, n, n)", "os.spawnvp(os.P_NOWAIT, n, [n])", "os.posix_spawn(f'{n}/p', ['p'], os.environ)",
            "import pty\npty.spawn(f'{n}/p')")
        for call in calls:
            for pre in ("import os, shutil, subprocess\nshutil.which('x')\nn = input()\n",
                        "import os, subprocess\nn = input()\n", "import os, subprocess\nimport sys\nn = sys.argv[1]\n"):
                for cmd in (py(pre + call), "python3 -c \"" + (pre + call).replace("\n", "; ").replace("; import pty", "") + "\""):
                    if "pty" in cmd and "-c" in cmd:
                        continue
                    with self.subTest(cmd=cmd):
                        r = self.refusal(cmd)
                        self.assertIsNotNone(r, cmd)
        self.refused((py("import os, subprocess\nn = input()\nsubprocess.run(f'rm {n}', shell=True)"),
                      py("import os\nn = input()\nos.system(f'rm {n}')")), "can't read")
        self.allowed((  # a literal program with a computed argument, no shell: the program is what the guard reads
            py("import subprocess\nn = input()\nsubprocess.run(['git', 'log', n])"),
            py("import os\nn = input()\nos.execv('/bin/ls', ['ls', n])"),
            py("import subprocess, sys\nsubprocess.run([sys.executable, 'x.py'])"),
            py("import subprocess\nsubprocess.run('git status', shell=True)"),
        ))

    def test_only_the_node_command_owns_node_argv(self):
        """T-4736 review: one `node -e` command's words are node's alone: bun's and deno's process.argv, and any line with
        two js interpreter commands, are unreadable, in both orders."""
        js = "require('child_process').execSync(process.argv[1])"
        for cmd in (
                f"node -e \"{js}\" 'git status'; bun -e \"{js}\" 'rm -rf /etc/t4736'",
                f"bun -e \"{js}\" 'rm -rf /etc/t4736'; node -e \"{js}\" 'git status'",
                f"node -e 0 'git status'; bun -e \"{js}\" 'rm -rf /etc/t4736'",
                f"bun -e \"{js}\" 'rm -rf /etc/t4736'; node -e 0 'git status'",
                f"node -e 0 'git status'; deno eval \"{js}\" 'rm -rf /etc/t4736'",
                f"deno eval \"{js}\" 'rm -rf /etc/t4736'; node -e 0 'git status'",
                f"node -e 0 'git status' && bun -e \"{js}\" 'rm -rf /etc/t4736'",
                f"bun -e \"{js}\" 'git status'",
                f"deno eval \"{js}\" 'git status'",
                f"node -e \"{js}\" 'git status' | bun -e \"{js}\" 'git status'",
        ):
            with self.subTest(cmd=cmd):
                r = self.refusal(cmd)
                self.assertIsNotNone(r, cmd)
                self.assertTrue("can't read" in r or "/etc/t4736" in r, r)
        self.assertIsNone(self.refusal(f"node -e \"{js}\" 'git status'"))  # the one node command keeps its words
        self.assertIsNotNone(self.refusal(f"node -e \"{js}\" 'rm /etc/t4736'"))

    def test_proc_environ_mem_maps_and_fd_are_credential_reads_cwd_and_cmdline_are_not(self):
        """T-4736 review: /proc/<pid>/environ holds other processes' keys (mem, maps and fd reach their memory and open
        files); cwd, cmdline, status, stat and exe are the readable ones. Literal or computed, any pid, self too."""
        for leaf in ("environ", "mem", "maps", "fd/3", "smaps", "smaps_rollup"):
            for path in ((f"'/proc/self/{leaf}'",) * (leaf == "environ") + (
                    f"'/proc/1234/{leaf}'", f"f'/proc/{{p}}/{leaf}'", f"'/proc/' + p + '/{leaf}'", f"'/proc/%s/{leaf}' % p",
                    f"os.path.join('/proc', p, '{leaf}')", f"'/proc/{{}}/{leaf}'.format(p)")):
                for body in (f"print(open({path}, 'rb').read())", f"print(open({path}).read())",
                             f"import pathlib\nprint(pathlib.Path({path}).read_bytes())"):
                    cmd = py("p = input()\n" + body)
                    with self.subTest(cmd=cmd):
                        self.assertIsNotNone(self.refusal(cmd), cmd)
        for cmd in ("cat /proc/self/environ", "cat /proc/1234/environ", "cat /proc/1234/mem"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.refusal(cmd), cmd)
        for leaf in ("cwd", "cmdline", "status", "stat", "exe"):
            cmd = py(LOOP + f"        try: print(p, open(f'/proc/{{p}}/{leaf}').read())\n        except OSError: pass")
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.refusal(cmd), cmd)

    def test_a_read_of_a_credential_path_stays_refused(self):
        home = "<home>"
        for cmd in (
                py(f"print(open('{home}/.ssh/id_rsa').read())"),
                py(f"print(open('{home}/.ssh/id_ed25519', 'r').read())"),
                py(f"import os\nprint(open(os.path.join('{home}', '.aws', 'credentials')).read())"),
                py(f"import pathlib\nprint(pathlib.Path('{home}/.netrc').read_text())"),
                py(f"import os\nprint(open(os.path.expanduser('~/.ssh/id_rsa')).read())"),
                py(f"import os\nprint(os.readlink('{home}/.ssh/id_rsa'))"),
                py(f"import urllib.request\nurllib.request.urlopen('http://127.0.0.1/', open('{home}/.ssh/id_rsa').read().encode())"),
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.refusal(cmd), cmd)
