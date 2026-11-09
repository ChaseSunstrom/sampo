"""T-0682: verification and product checking — first versions: vacuous checks, behaviour locks, regression bisect,
gate pass rates, not-verified files at finish, and non-web product checks."""
import http.server
import json
import os
import subprocess
import threading

from helpers import ForemanTestCase

from sampo import core as c


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def commit(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)


class Vacuous(Base):
    def test_a_check_that_already_passed_at_the_start_is_named(self):
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.commit("calc")
        self.fm("task", "new", "Add mul", "--type", "FEATURE", "--tier", "S",
                "--ac", "add works :: python3 -c 'import calc; assert calc.add(1, 2) == 3'",
                "--ac", "mul works :: python3 -c 'import calc; assert calc.mul(2, 3) == 6'", "--step", "s", "--focus")
        self.write("calc.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        r = self.fm("task", "prove", "T-0001", "--vacuous", check=False)
        self.assertIn("criterion 1", r.stdout)
        self.assertNotIn("criterion 2", r.stdout)
        self.assertNotEqual(r.returncode, 0)


class SameBehaviour(Base):
    def test_a_refactor_keeps_its_output_and_a_change_shows_the_difference(self):
        self.write("greet.py", "print('hi')\n")
        self.commit("greet")
        self.fm("task", "new", "Tidy greet", "--type", "CLEAN", "--tier", "S", "--ac", "same :: true", "--step", "s",
                "--focus")
        self.write("greet.py", "MSG = 'hi'\nprint(MSG)\n")
        self.fm("task", "prove", "T-0001", "--same", "python3 greet.py")
        self.assertIn("behaviour lock", self.fm("task", "show", "T-0001").stdout)
        self.write("greet.py", "print('hello')\n")
        r = self.fm("task", "prove", "T-0001", "--same", "python3 greet.py", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hello", r.stdout + r.stderr)


class Bisect(Base):
    def test_the_commit_that_broke_a_recheck_is_captured(self):
        self.fm("task", "new", "Ok file", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: grep -qx ok ok.txt",
                "--step", "s", "--focus")
        self.write("ok.txt", "ok\n")
        self.fm("task", "finish", "T-0001", "--audit", "self check", "--run", "true", "--commit", "Add ok")
        for i, text in enumerate(["ok\n", "broken\n", "broken\n"]):
            self.write("ok.txt", text)
            self.write(f"n{i}.txt", "x\n")
            self.commit(["unrelated", "break the ok file", "later work"][i])
        r = self.fm("sentinel", "--bisect", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("break the ok file", r.stdout)
        self.assertTrue([b for b in c.load_briefs(self.p) if b.title.startswith("Regression")])


class Repeat(Base):
    def test_each_gate_reports_its_pass_rate(self):
        self.fm("check", "add", "true")
        self.fm("check", "add", "sh -c 'if [ -f t ]; then rm t; exit 1; else touch t; fi'")
        r = self.fm("check", "--repeat", "4", check=False)
        self.assertIn("4/4", r.stdout)
        self.assertIn("2/4", r.stdout)
        self.assertNotEqual(r.returncode, 0)


class NotVerified(Base):
    def test_finish_names_changed_source_no_linked_test_covered(self):
        self.write("lib/a.py", "A = 1\n")
        self.write("lib/b.py", "B = 1\n")
        self.write("tests/test_a.py", "import unittest\n")
        self.commit("base")
        self.fm("task", "new", "Change a and b", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        self.write("lib/a.py", "A = 2\n")
        self.write("lib/b.py", "B = 2\n")
        r = self.fm("task", "finish", "T-0001", "--audit", "self check", "--run", "python3 tests/test_a.py")
        out = r.stdout + r.stderr
        self.assertIn("lib/b.py", out)
        self.assertNotIn("lib/a.py", out.split("sampo: warning: not verified")[1] if "sampo: warning: not verified" in out else "")

    def test_a_passing_gate_run_covers_the_files_its_paths_match(self):
        # T-0736: the full suite is a gate, so a passing run of it verifies every file it covers
        self.write("lib/a.py", "A = 1\n")
        self.write("tools/t.py", "T = 1\n")
        self.commit("base")
        self.fm("check", "add", "python3 -c 'print(1)'")
        self.fm("check", "paths", "1", "lib/**")
        self.fm("task", "new", "Change a and t", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        self.write("lib/a.py", "A = 2\n")
        self.write("tools/t.py", "T = 2\n")
        r = self.fm("task", "finish", "T-0001", "--audit", "self check", "--run", "python3 -c 'print(1)'")
        out = r.stdout + r.stderr  # T-0956: past the verdict line, which names the kind too
        warned = out.split("sampo: warning: not verified")[1] if "sampo: warning: not verified" in out else ""
        self.assertNotIn("lib/a.py", warned)
        self.assertIn("tools/t.py", warned)


class SmokeCmd(Base):
    def test_a_command_and_an_http_check_run_as_the_product_check(self):
        self.fm("smoke", "set", "cmd", "python3 -c 'print(42)'", "--expect", "42")
        self.assertIn("ok", self.fm("smoke").stdout)
        with open(os.path.join(self.tmp, "health.json"), "w") as f:
            json.dump({"ok": True}, f)
        tmp = self.tmp

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=tmp, **k)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/health.json"
            self.fm("smoke", "set", "http", url, "--json-key", "ok")
            self.assertIn("ok", self.fm("smoke").stdout)
            self.fm("smoke", "set", "http", url, "--json-key", "missing")
            r = self.fm("smoke", check=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("missing", r.stdout + r.stderr)
        finally:
            srv.shutdown()
