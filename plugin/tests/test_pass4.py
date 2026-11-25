"""Self-improvement pass 4 (T-0286): the guard parses cd-prefixed python heredocs (T-0282), tests can't reach the real
claude (T-0283), the adversary lens follows untrusted text to its sinks (T-0284), escapes are attributed to the task
that closed them (T-0285), and hunk proof reaches new files (T-0279)."""
import json
import os
import subprocess

from helpers import ForemanTestCase, read_text
from test_guard import GuardCase


class GuardHeredoc(GuardCase):
    def driving(self, cmd):
        return "driving Sampo's modules" in str(self.bash(cmd) or "")

    def test_a_cd_prefix_is_parsed_like_a_bare_heredoc(self):
        edit = "s = open('fmx.py').read()\ns = s.replace('a', '''    import fmcli\\n    p = fmcli.resolve(args)\\n''')\n" \
               "open('fmx.py', 'w').write(s)\n"
        for pre in ("", "cd /tmp/x && ", "cd /tmp/x && cd sub && ", "x=1 && "):  # T-1887: name=value && too
            self.assertFalse(self.driving(f"{pre}python3 - <<'PY'\n{edit}PY"), pre)
        drive = "import fmhooks\nfmhooks.save_brief(x)\n"
        for pre in ("cd /tmp/x && ", "cd /tmp/x; ", "cd $(mktemp -d) && ", "cd /tmp/x | ", "true && "):
            self.assertTrue(self.driving(f"{pre}python3 - <<'PY'\n{drive}PY"), pre)  # a real drive still blocks
        for pre in ("cd /tmp/x; ", "cd `pwd` && ", "x=$(id) && "):  # anything but a plain cd chain keeps the text match
            self.assertTrue(self.driving(f"{pre}python3 - <<'PY'\n{edit}PY"), pre)


    def test_what_bash_runs_is_what_the_guard_parses(self):
        # review (T-0286): mismatches between the guard's heredoc parse and bash's let a body hide a write
        settings = "{home}/.claude/settings.json"
        for pre in ("", "cd /tmp && "):
            cont = f"{pre}python3 - <<'X' \\\n1>{settings}\nsettings=1\nopen('/tmp/a','w').write('1')\nX"
            self.assertIsNotNone(self.bash(cont), "a continued starter line")
            two = f"{pre}python3 - <<'X' <<\\E\nprint(1)\nX\nopen('{settings}','w').write('1')\nE"
            self.assertIsNotNone(self.bash(two), "a second heredoc bash reads instead")
            space = f"{pre}python3 - <<'X'\nX=1\nX \nopen('{settings}','w').write('1')\nX"
            self.assertIsNotNone(self.bash(space), "a delimiter line with a trailing space doesn't end the body")

    def test_every_splitter_ends_a_body_where_bash_does(self):
        # data after a `X ` line is still body to bash: no false block — and a substitution there still runs
        self.assertIsNone(self.bash("cat <<'X' > /tmp/notes.txt\nhello\nX \nrm -rf ~/Documents\nX"))
        self.assertIsNone(self.bash("cat <<'X' > /tmp/a.py\nX \npython3 -c \"open('{fhome}/plugin/lib/fmguard.py', "
                                    "'w')\"\nX"))
        self.assertIsNotNone(self.bash("cat <<X > /tmp/notes.txt\nX \n$(rm -rf ~/Documents)\nX"))

    def test_a_module_in_the_cd_target_shadows_the_import(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "json.py"), "w") as f:
                f.write("")
            core = "{fhome}/plugin/lib/fmguard.py"
            body = f"import json\nx = '{core}'\nopen('/tmp/ok.txt', 'w').write(json.dumps(1))\n"
            self.assertIsNone(self.bash(f"cd /tmp && python3 - <<'PY'\n{body}PY"))  # no json.py there: proved
            self.assertIsNotNone(self.bash(f"cd {d} && python3 - <<'PY'\n{body}PY"))  # json.py there runs instead
            drive = "import json\ns = 'import fmcore\\nfmcore.write_meta(p, {{}})'\nprint(json.dumps(s))\n"
            self.assertFalse(self.driving(f"cd /tmp && python3 - <<'PY'\n{drive}PY"))
            self.assertTrue(self.driving(f"cd {d} && python3 - <<'PY'\n{drive}PY"))


class Lockout(ForemanTestCase):
    def test_a_stub_that_cant_run_never_reaches_the_real_claude(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write("import sys\nprint('stub')\n")  # no shebang: exec fails and PATH lookup moves on
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        p = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                           env=dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"]))
        self.assertEqual(p.returncode, 97, p.stdout + p.stderr)
        self.assertIn("real claude", p.stderr)


class Sinks(ForemanTestCase):
    def test_network_code_needs_the_adversary_lens_and_its_sinks(self):
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "fetch it", "--type", "FEATURE", "--tier", "M", "--ac", "x :: true",
                                 "--step", "x", "--interpretation", "Reading 1: a. Reading 2: b.", "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a", "--json").stdout)["id"]
        self.fm("focus", tid)
        with open(os.path.join(self.repo, "get.py"), "w") as f:
            f.write("import urllib.request\n\n\ndef get(url):\n    return urllib.request.urlopen(url).read()\n")
        out = self.fm("audit", "prep", tid).stdout
        self.assertIn("urlopen", out.split("Pre-audit: security-sensitive:", 1)[1].splitlines()[0])
        brief = read_text(os.path.join(self.state_dir(), "audits", f"{tid}.review.md"))
        for sink in ("terminal", "model prompt", "URL", "shell", "file path", "git config"):
            self.assertIn(sink, brief)

    def state_dir(self):
        from sampo import core as c
        return c.find_project(self.repo).dir


class Escapes(ForemanTestCase):
    def finished(self, title):
        tid = json.loads(self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "x",
                                 "--step", "x", "--json").stdout)["id"]
        self.fm("focus", tid)
        self.fm("task", "step", tid, "done", "1", "--evidence", "x", "ok")
        self.fm("task", "ac", tid, "check", "1", "--evidence", "x", "ok")
        self.fm("task", "audit", tid, "self", "x", "ok")
        self.fm("task", "audit", tid, "adversary", "x", "ok")
        self.fm("task", "done", tid)
        return tid

    def test_a_fix_naming_a_done_task_is_an_escape(self):
        self.fm("init")
        done = self.finished("fm deps")
        self.fm("capture", f"SECURITY: fm deps ({done}) prints registry text unescaped", "--type", "SECURITY")
        self.fm("capture", f"FEATURE: more like {done}", "--type", "FEATURE")  # not a fix: not an escape
        self.fm("task", "new", f"Fix the deps listing from {done}", "--type", "FIX", "--tier", "S")
        out = self.fm("friction").stdout
        section = out.split("escapes", 1)[1]
        self.assertIn(done, section)
        self.assertIn("adversary", section)  # the lens that passed it
        self.assertIn("prints registry text", section)
        self.assertNotIn("more like", section)
        self.assertIn("Fix the deps listing", section)  # fm task new counts too


class NewFiles(ForemanTestCase):
    def test_each_definition_of_a_new_file_is_removed_alone(self):
        self.fm("init")
        with open(os.path.join(self.repo, "base.py"), "w") as f:
            f.write("X = 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "base"], check=True)
        tid = json.loads(self.fm("task", "new", "extra", "--type", "FEATURE", "--tier", "S", "--ac", "x :: true",
                                 "--step", "x", "--json").stdout)["id"]
        self.fm("focus", tid)
        with open(os.path.join(self.repo, "extra.py"), "w") as f:
            f.write('"""Extra."""\nimport os\n\n\ndef used():\n    return 1\n\n\n@staticmethod\ndef unused():\n    return 2\n')
        res = json.loads(self.fm("task", "prove", tid, "--hunks", "--run",
                                 "python3 -c 'import extra; assert extra.used() == 1'", "--json", check=False).stdout)
        self.assertEqual((res["total"], res["proven"]), (2, 1))  # the docstring and the import aren't mutated
        self.assertIn("def unused", res["unproven"][0]["text"])
        self.assertIn("@staticmethod", read_text(os.path.join(self.repo, "extra.py")))  # the real file is untouched

    def test_each_hunk_is_reverted_where_it_is(self):
        # found proving this batch: git apply -R --unidiff-zero put a hunk back at the nearest identical line counted
        # from the old file's position, so a shifted hunk mutated the wrong line and its verdict was someone else's
        self.fm("init")
        with open(os.path.join(self.repo, "calc.py"), "w") as f:
            f.write("def f():\n    return 1\n\n\ndef g():\n    return 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "calc"], check=True)
        tid = json.loads(self.fm("task", "new", "two", "--type", "FIX", "--tier", "S", "--ac", "x :: true",
                                 "--step", "x", "--json").stdout)["id"]
        self.fm("focus", tid)
        with open(os.path.join(self.repo, "calc.py"), "w") as f:
            f.write("def f():\n    return 2\n\n\n" + "".join(f"# note {i}\n" for i in range(10))
                    + "def g():\n    return 2\n")
        res = json.loads(self.fm("task", "prove", tid, "--hunks", "--run",
                                 "python3 -c 'import calc; assert calc.g() == 2'", "--json", check=False).stdout)
        self.assertEqual((res["total"], res["proven"]), (3, 1))  # g's hunk is the one the check tests
        self.assertNotIn("+16", " ".join(u["at"] for u in res["unproven"]))


class EditTool(GuardCase):
    def test_the_driving_refusal_names_the_edit_tool(self):
        # T-0740: 9 refusals in one day from edit scripts whose text names fm modules; the way out is the Edit tool
        said = str(self.bash("python3 - <<'PY'\nimport fmcli\nfmcli.main()\nPY") or "")
        self.assertIn("driving Sampo's modules", said)
        self.assertIn("Edit or Write tool", said)
