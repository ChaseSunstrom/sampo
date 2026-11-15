"""T-4686: the shapes T-4673's interpreter-write judging left reading as known-safe. A shell string spawned from node,
perl, ruby, php or lua, a ternary or `||` path, `R -e`, a python function parameter, `__import__`, `extractall`, a
loop or comprehension target, and a chdir hidden in env -S. Only the classifier is called with command text: no test
runs anything, and every path named is a made-up file."""
import os
import unittest

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on the path)
from test_guard import GuardCase, g


class Leftovers(GuardCase):
    def cctx(self):
        scr = os.path.join(os.path.dirname(self.repo), "scr")
        os.makedirs(scr, exist_ok=True)
        return g.Ctx(cwd=self.repo, project_root=self.repo, home=self.home, foreman_home=self.fhome, scratch=[scr],
                     allow=set(), task_id="T-0007")

    def cats(self, cmd):
        return {c for c, _ in g.findings("Bash", {"command": cmd}, self.cctx())}

    def refused(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIn("system", self.cats(cmd), cmd)

    def test_shell_string_spawned_from_node_is_judged(self):
        self.refused((
            "node -e \"require('child_process').execSync('tee /etc/t4686.conf')\"",
            "node -e \"require('child_process').exec('rm /etc/t4686.conf')\"",
            "node -e \"require('child_process').spawnSync('sh', ['-c', 'echo x > /etc/t4686'])\"",
            "node -e \"const cp = require('child_process'); cp.execFileSync('rm', ['/etc/t4686'])\"",
        ))

    def test_shell_string_spawned_from_perl_ruby_php_lua_is_judged(self):
        self.refused((
            "perl -e 'system(\"tee /etc/t4686\")'",
            "perl -e 'system(\"rm\", \"/etc/t4686\")'",
            "perl -e '`rm /etc/t4686`'",
            "perl -e 'qx{rm /etc/t4686}'",
            "ruby -e 'system(\"rm /etc/t4686\")'",
            "ruby -e '`rm /etc/t4686`'",
            "ruby -e '%x(rm /etc/t4686)'",
            "ruby -e 'IO.popen(\"tee /etc/t4686\")'",
            "php -r 'system(\"rm /etc/t4686\");'",
            "php -r 'shell_exec(\"rm /etc/t4686\");'",
            "php -r 'exec(\"rm /etc/t4686\");'",
            "php -r 'passthru(\"rm /etc/t4686\");'",
            "lua -e 'os.execute(\"rm /etc/t4686\")'",
        ))

    def test_ternary_or_default_path_is_judged_on_every_branch(self):
        self.refused((
            "node -e \"require('fs').writeFileSync(1 ? '/etc/t4686' : 'out.txt', 'x')\"",
            "node -e \"require('fs').writeFileSync(process.argv[1] || '/etc/t4686', 'x')\"",
            "node -e \"require('fs').writeFileSync(process.env.X ?? '/etc/t4686', 'x')\"",
            "node -e \"const f = 1 ? 'out.txt' : '/etc/t4686'; require('fs').writeFileSync(f, 'x')\"",
            "node -e \"require('fs').writeFileSync((1 ? '/etc/t4686' : 'out.txt'), 'x')\"",
            "ruby -e 'File.write(ARGV[0] || \"/etc/t4686\", \"x\")'",
            "perl -e 'unlink($ARGV[0] || \"/etc/t4686\")'",
            "php -r 'file_put_contents(1 ? \"/etc/t4686\" : \"out.txt\", \"x\");'",
        ))

    def test_R_inline_writes_are_judged(self):
        self.refused((
            "R -e 'writeLines(\"x\", \"/etc/t4686\")'",
            "R --no-save -e 'writeLines(\"x\", \"/etc/t4686\")'",
            "R --slave -e 'file.remove(\"/etc/t4686\")'",
            "Rscript -e 'writeLines(\"x\", \"/etc/t4686\")'",
        ))

    def test_python_function_parameter_is_bound_from_its_call(self):
        self.refused((
            "python3 -c \"def w(p): open(p, 'w')\nw('/etc/t4686')\"",
            "python3 -c \"w = lambda p: open(p, 'w')\nw('/etc/t4686')\"",
            "python3 -c \"import os\ndef rm(p, q=1): os.remove(p)\nrm('/usr/lib/t4686')\"",
        ))

    def test_python_dynamic_import_is_the_module(self):
        self.refused((
            "python3 -c \"__import__('os').remove('/etc/t4686')\"",
            "python3 -c \"import importlib; importlib.import_module('os').remove('/etc/t4686')\"",
            "python3 -c \"__import__('shutil').rmtree('/usr/lib/t4686')\"",
        ))

    def test_python_dynamic_attribute_is_unknown(self):
        self.refused((
            "python3 -c \"import os; getattr(os, 'rem' + 'ove')('/etc/t4686')\"",
            "python3 -c \"import os; getattr(os, input())('/etc/t4686')\"",
        ))

    def test_python_archive_extraction_is_a_write(self):
        self.refused((
            "python3 -c \"import tarfile; tarfile.open('a.tar').extractall('/etc')\"",
            "python3 -c \"import zipfile; zipfile.ZipFile('a.zip').extractall('/usr/lib/t4686')\"",
            "python3 -c \"import zipfile; zipfile.ZipFile('a.zip').extract('x', '/etc')\"",
            "python3 -c \"import shutil; shutil.unpack_archive('a.tar', '/etc')\"",
        ))

    def test_python_loop_and_comprehension_targets_are_bound(self):
        self.refused((
            "python3 -c \"[open(p, 'w') for p in ['/etc/t4686']]\"",
            "python3 -c \"ps = ['/etc/t4686', 'out.txt']\nfor p in ps: open(p, 'w')\"",
            "python3 -c \"import os\nfor p in sorted(['/etc/t4686']): os.remove(p)\"",
            "python3 -c \"import glob, os\nfor p in glob.glob('/etc/t4686*'): os.remove(p)\"",
            "python3 -c \"import os\nfor n in os.listdir('/etc'): os.remove('/etc/' + n)\"",
            "python3 -c \"import os\nfor n in os.listdir('/etc'): os.remove(n)\"",
        ))

    def test_env_dash_S_hiding_a_chdir_is_judged(self):
        self.refused((
            "env -S '--chdir=/etc' python3 -c \"open('t4686.conf', 'w')\"",
            "env -S '-C /etc' python3 -c \"open('t4686.conf', 'w')\"",
            "env -S '--chdir /usr/lib' node -e \"require('fs').writeFileSync('t4686', 'x')\"",
        ))

    # T-4686 review round: four paths that ended as known-safe where the rule says unknown, and the smaller gaps
    def test_whole_spawn_command_the_code_does_not_spell_out_is_unknown(self):
        self.refused((
            "node -e \"const c=process.argv[1]; require('child_process').execSync(c)\" \"rm -rf /etc/t4686\"",
            "node -e \"require('child_process').exec(process.argv[1])\" \"rm /etc/t4686\"",
            "node -e 'require(\"child_process\").exec(`${process.argv[1]} --x`)' 'rm /etc/t4686'",
            "perl -e 'system($ARGV[0])' 'rm /etc/t4686'",
        ))

    def test_a_cut_combination_list_is_unknown(self):
        self.refused((
            "node -e \"require('child_process').execFileSync('rm', [1 ? 'a' : '/etc/t4686', 1 ? 'b' : 'c', 1 ? 'd' : 'e',"
            " 1 ? 'f' : 'g', 1 ? 'h' : 'i', 1 ? 'j' : 'k'])\"",
        ))

    def test_getattr_on_an_aliased_module_is_judged(self):
        self.refused((
            "python3 -c \"import os as o; getattr(o, 'r'+'emove')('/etc/t4686')\"",
            "python3 -c \"import shutil as s; getattr(s, 'rm' + 'tree')('/etc/t4686')\"",
        ))

    def test_python_parameters_with_no_bound_call_are_unknown(self):
        self.refused((
            "python3 -c \"(lambda p: open(p, 'w'))('/etc/t4686')\"",
            "python3 -c \"class C:\n    def w(self, p): open(p, 'w')\nC().w('/etc/t4686')\"",
            "python3 -c \"class C:\n    def __init__(self, p): open(p, 'w')\nC('/etc/t4686')\"",
            "python3 -c \"def w(p): open(p, 'w')\nw(*['/etc/t4686'])\"",
            "python3 -c \"def w(p): open(p, 'w')\nw(**{'p': '/etc/t4686'})\"",
            "python3 -c \"import functools\ndef w(p): open(p, 'w')\nfunctools.partial(w, '/etc/t4686')()\"",
            "python3 -c \"def w(p='out.txt'): open(p, 'w')\nlist(map(w, ['/etc/t4686']))\"",
            "python3 -c \"import os\ndef f(p): os.remove(p)\nlist(map(f, ['/etc/t4686']))\"",
        ))

    def test_js_destructure_and_function_aliases_are_judged(self):
        self.refused((
            "node -e \"const {execSync: x} = require('child_process'); x('rm /etc/t4686')\"",
            "node -e \"const { exec: run, spawnSync } = require('node:child_process'); run('rm /etc/t4686')\"",
            "node -e \"const run = require('child_process').execSync; run('rm /etc/t4686')\"",
            "node -e \"const cp = require('child_process'); const go = cp.execSync; go('rm /etc/t4686')\"",
            "node --input-type=module -e \"import {execSync as x} from 'child_process'; x('rm /etc/t4686')\"",
        ))

    def test_qx_delimiters_and_nesting(self):
        self.refused((
            "perl -e \"qx'rm /etc/t4686'\"",
            "perl -e 'qx\"rm /etc/t4686\"'",
            "perl -e 'qx#rm /etc/t4686#'",
            "ruby -e '%x(echo $(rm /etc/t4686))'",
            "ruby -e '%x(echo (a) $(rm /etc/t4686))'",
            "perl -e 'qx{echo {a} $(rm /etc/t4686)}'",
            "perl -e 'qx{ls $x'",  # a body the scan can't close is unknown
        ))

    def test_review_round_ordinary_code_stays_allowed(self):
        for cmd in (
                "node -e \"require('child_process').execSync('git ' + process.argv[1])\"",
                "node -e 'const a = process.argv[1]; require(\"child_process\").exec(`git ${a}`)'",
                "node -e \"const {execSync: x} = require('child_process'); x('ls -la')\"",
                "node -e \"const run = require('child_process').execSync; run('git status')\"",
                "python3 -c \"def w(p): open(p, 'w')\nw('out.txt')\"",
                "python3 -c \"class C:\n    def w(self, p): open(p, 'w')\nC().w('out.txt')\"",
                "python3 -c \"(lambda p: open(p, 'w'))('out.txt')\"",
                "python3 -c \"import os\nprint(getattr(os, 'name'))\"",
                "python3 -c \"import argparse\na = argparse.Namespace(x=1)\nprint(getattr(a, 'x'))\"",
                "ruby -e 'puts %x(echo (a) $(date))'",
                "perl -e 'print qx#date#'",
        ):
            with self.subTest(cmd=cmd):
                self.assertNotIn("system", self.cats(cmd), cmd)

    # what is still read as safe (ponytail: each needs following a value through a call or a table the scan doesn't
    # model; the cost of closing it is an over-refusal of every script that passes a function on). Documented in
    # docs/map/ops-3.md under "Interpreter leftovers".
    @unittest.expectedFailure
    def test_left_scandir_entry_path(self):
        self.refused(("python3 -c \"import os\nfor e in os.scandir('/etc'): os.remove(e.path)\"",))

    @unittest.expectedFailure
    def test_left_module_table_lookup(self):
        self.refused(("python3 -c \"import os; vars(os)['remove']('/etc/t4686')\"",))

    @unittest.expectedFailure
    def test_left_command_string_a_function_returns(self):
        self.refused(("node -e \"const c = () => 'rm /etc/t4686'; require('child_process').execSync(c())\"",))

    def test_ordinary_one_liners_stay_allowed(self):
        for cmd in (
                "python3 -c \"import json; print(json.dumps({'a': 1}, indent=2))\"",
                "python3 -c \"import sys; print(sys.version)\"",
                "python3 -c \"print(open('README.md').read())\"",
                "python3 -c \"open('out.txt', 'w').write('x')\"",
                "python3 -c \"def f(p): return open(p).read()\nprint(f('README.md'))\"",
                "python3 -c \"import os\nfor n in os.listdir('.'): print(n)\"",
                "python3 -c \"import tarfile; tarfile.open('a.tar').extractall('out')\"",
                "node -e \"console.log(JSON.stringify({a: 1}, null, 2))\"",
                "node -e \"console.log(process.version)\"",
                "node -e \"require('child_process').execSync('ls -la')\"",
                "node -e \"require('child_process').execSync('git status')\"",
                "node -e \"require('fs').writeFileSync(1 ? 'out.txt' : 'b.txt', 'x')\"",
                "perl -e 'system(\"ls\")'",
                "perl -e 'print \"hi\\n\"'",
                "ruby -e 'puts `date`'",
                "ruby -e 'puts RUBY_VERSION'",
                "php -r 'echo shell_exec(\"whoami\");'",
                "lua -e 'os.execute(\"ls\")'",
                "R -e 'print(R.version.string)'",
                "env -S '--chdir=.' python3 -c \"open('out.txt', 'w')\"",
        ):
            with self.subTest(cmd=cmd):
                self.assertNotIn("system", self.cats(cmd), cmd)
