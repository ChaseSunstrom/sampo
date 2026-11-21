"""T-3592: sampo cpu cap sets a CPU quota on the session's systemd scope, the same change a typed
systemctl --user set-property makes, so it needs the same yes (category system); the meter and the goal don't."""
import sys
import unittest
from unittest import mock

from helpers import LIB
from test_guard import GuardCase


class CpuCap(GuardCase):
    def test_the_cap_needs_the_system_grant_and_the_meter_does_not(self):
        self.run_table([
            ("fm cpu cap 10", "system"),
            ("fm cpu cap off", "system"),
            ("fm --json cpu cap 25", "system"),
            ("fm -p app-1a2b3c cpu cap 25", "system"),
            ("python3 /x/plugin/bin/fm cpu cap 25", "system"),
            ("fm cpu -p x cap 10", "system"),  # options after the verb don't hide the action
            ("fm cpu --project x cap 10", "system"),
            ("fm cpu --project=x cap 10", "system"),
            ("fm cpu --win 5 cap 10", "system"),
            ("fm cpu --window 5 cap 10", "system"),
            ("fm cpu -- cap 10", "system"),
            ("X=cap; fm cpu $X 10", "system"),
            ('fm cpu "ca"p 10', "system"),
            ("fm cpu $ACTION 10", "system"),  # unknown at check time
            ("fm cpu $(echo cap) 10", "system"),
            ("fm cpu idle on", "system"),  # T-4988: the same set-property on the same scope
            ("fm cpu idle off", "system"),
            ("fm cpu --window 5 idle on", "system"),
            ("fm cpu", None),
            ("fm cpu --window 3 --json", None),
            ("fm cpu goal 10", None),
            ("systemctl --user set-property --runtime kitty-1-0.scope CPUQuota=160%", "system"),  # unchanged
        ], self.bash)
        self.assertIsNone(self.bash("fm cpu cap 10", allow=["system"]))


class SystemWriteGate(GuardCase):
    """T-4673 review: interpreter code with no write-call word pays one pattern search and nothing more: `os`, `dir`,
    `set` and `file` alone (words of nearly every script) don't open the scan, a write call does."""

    def walks(self, cmd):
        from test_guard import g as fmguard
        seen = []
        real = fmguard._interp_walk
        with mock.patch.object(fmguard, "_interp_walk", lambda *a: seen.append(1) or real(*a)):
            fmguard._interp_system_writes(cmd, self.ctx())
        return len(seen)

    def test_code_without_a_write_call_is_not_scanned(self):
        for cmd in ("python3 -c \"import os; print(dir(), set(), file)\"", "python3 -m pytest -q tests",
                    "node -e \"console.log(Object.keys(process.env).length)\"",
                    "ruby -e 'puts [1, 2].sort.inspect'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.walks(cmd), 0)
        for cmd in ("python3 -c \"open('x', 'w')\"", "python3 -c \"import os; os.remove('x')\"",
                    "Rscript -e 'dir.create(\"x\")'", "php -r 'file_put_contents(\"x\", 1);'",
                    "tclsh <<'EOF'\nfile delete x\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.walks(cmd), 1)


class ColdWriteTargets(unittest.TestCase):
    """T-3692: a hook reads a plain command's write targets in a fresh process without compiling a regex (it compiled
    six: the operator, redirect, assignment and perl/ruby tests ran re.fullmatch per token); the answers as before."""
    PROBE = ("import re, sys\nsys.path.insert(0, sys.argv[1])\nimport fmguard\nmade = []\nreal = re._compiler.compile\n"
             "re._compiler.compile = lambda p, f: made.append(p) or real(p, f)\n"
             "for cmd in sys.argv[2:]:\n    print(fmguard.write_targets(cmd))\nprint(made)\n")
    CMDS = ("rm -rf build/out", "python3 plugin/tests/run.py -p test_core.py > log.txt", "cp a.txt b.txt",
            "FOO=1 BAR+=x sed -i s/a/b/ notes.md", "cd plugin && make build; ls | tee -a out.log",
            "perl5.36 -pi -e s/a/b/ conf.ini", "ruby -i -pe 1 notes.rb", "cat <in.txt >|out.txt 2>&1 &>both.log",
            "(cd sub; touch made.txt) || echo x >> err.log", 'echo "a > b; c" > out.md', "echo \"$(date)\" '|' >when.txt")

    def test_plain_commands_compile_no_regex(self):
        import subprocess
        out = subprocess.run([sys.executable, "-c", self.PROBE, LIB, *self.CMDS], capture_output=True, text=True,
                             check=True).stdout.splitlines()
        self.assertEqual(out[-1], "[]")
        self.assertEqual(out[:-1], [str(x) for x in (
            ["build/out"], ["log.txt"], ["b.txt"], ["notes.md"], ["out.log"], ["conf.ini"], ["notes.rb"],
            ["out.txt", "both.log"], ["made.txt", "err.log"], ["out.md"], ["when.txt"])])
