"""T-5902: sampo.sinks, the destructive sinks a diff adds or reaches from its tests, and the brief that carries them.
The case is T-5899: a mocked Popen's pid reached os.killpg as 1. Nothing here signals a process: the module only reads text."""
import json
import os

from helpers import ForemanTestCase
from test_hooks import HookCase

from sampo import core as c
from sampo import sinks


def new_file(path, text):
    """The diff that adds a whole file."""
    body = text.rstrip("\n").split("\n")
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(body)} @@\n"
            + "".join(f"+{x}\n" for x in body))


def edit(path, line, text):
    """A -U0 diff that adds one line at `line` of an existing file."""
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{line},0 +{line} @@\n+{text}\n"


def kinds(diff, read=None, files=None):
    return [(h.file, h.line, h.kind) for h in sinks.scan(diff, read, files)]


KILL_GROUP = '''import os
import signal


def _kill_group(proc):
    pid = proc.pid
    os.killpg(pid, signal.SIGKILL)
'''


class T5899Case(ForemanTestCase):
    """The call that killed the session, and the test that fed it a Mock."""

    def test_the_killpg_of_a_computed_pid_is_listed_with_file_and_line(self):
        got = sinks.scan(new_file("plugin/lib/pilotsig.py", KILL_GROUP))
        self.assertEqual([(h.file, h.line, h.kind) for h in got], [("plugin/lib/pilotsig.py", 7, "kill")])
        self.assertIn("`pid` is computed", got[0].why)
        self.assertIn("in _kill_group", got[0].why)

    def test_an_attribute_pid_and_a_negative_pid_are_computed_too(self):
        text = "import os\nos.kill(proc.pid, 9)\nos.killpg(-pid, 9)\nos.kill(os.getpid(), 9)\n"
        self.assertEqual([h.line for h in sinks.scan(new_file("a.py", text))], [2, 3, 4])

    def test_the_global_literals_are_listed_a_plain_constant_is_not(self):
        text = "import os\nos.kill(0, 9)\nos.kill(1, 9)\nos.killpg(-1, 9)\nos.kill(4242, 9)\nos.killpg(4242, 9)\n"
        got = sinks.scan(new_file("a.py", text))
        self.assertEqual([h.line for h in got], [2, 3, 4])
        self.assertTrue(all("kill(-1)" in h.why for h in got))

    def test_a_call_the_diff_did_not_touch_is_not_listed(self):
        text = KILL_GROUP + "\n\ndef other():\n    return 1\n"
        got = sinks.scan(edit("p.py", 11, "    return 2"), read=lambda f: text.replace("return 1", "return 2"))
        self.assertEqual(got, [])

    def test_a_multi_line_call_counts_when_any_of_its_lines_changed(self):
        text = "import shutil\n\n\ndef f(d):\n    shutil.rmtree(\n        d,\n        ignore_errors=True)\n"
        self.assertEqual(kinds(edit("p.py", 7, "        ignore_errors=True)"), read=lambda f: text),
                         [("p.py", 5, "rm")])

    def test_each_sink_a_diff_adds_is_listed_once_and_in_file_order(self):
        text = "import os, shutil\nshutil.rmtree(d)\nos.remove(f)\nshutil.rmtree('build')\nos.kill(p, 1)\n"
        got = sinks.scan(new_file("a.py", text))
        self.assertEqual([(h.line, h.kind) for h in got], [(2, "rm"), (3, "rm"), (5, "kill")])


class Python(ForemanTestCase):
    def hits(self, body):
        return [(h.line, h.kind, h.why) for h in sinks.scan(new_file("a.py", "import os, subprocess, shutil\n" + body))]

    def test_a_subprocess_argv_with_a_computed_target(self):
        got = self.hits('subprocess.run(["rm", "-rf", d])\nsubprocess.run(["rm", "-rf", "build"])\n'
                        'subprocess.check_call(["git", "-C", wt, "reset", "--hard"])\n'
                        'subprocess.check_call(["git", "reset", "--hard", "HEAD"])\n'
                        'subprocess.run(["kill", "-9", str(pid)])\n')
        self.assertEqual([(n, k) for n, k, _ in got], [(2, "rm"), (4, "git"), (6, "kill")])

    def test_a_shell_string_with_an_fstring_or_a_concatenation(self):
        got = self.hits('os.system(f"rm -rf {d}/x")\nos.system("rm -rf " + d)\nos.system("rm -rf /tmp/x")\n'
                        'subprocess.run(f"pkill -f {name}", shell=True)\nsubprocess.run(["bash", "-c", f"kill {pid}"])\n'
                        'os.system("rm -rf %s" % d)\nos.system("rm -rf {}".format(d))\n')
        self.assertEqual([n for n, _, _ in got], [2, 3, 5, 6, 7, 8])

    def test_pathlib_unlink_takes_its_receiver_as_the_target(self):
        got = self.hits("import pathlib\npathlib.Path(p).unlink()\npathlib.Path('x').unlink()\nq.rmdir()\n")
        self.assertEqual([n for n, _, _ in got], [3, 5])

    def test_a_method_on_an_object_and_a_container_remove_are_no_sinks(self):
        self.assertEqual(self.hits("proc.kill()\nitems.remove(x)\nself.kill(pid)\nd.pop(k)\nos.getpid()\n"), [])

    def test_a_trailing_comment_naming_a_sink_is_no_call_in_the_line_fallback(self):
        text = "def (:\n    if pid <= 1:  # killpg(1) is every process\n        return\n    rm_x = 1  # rm -rf $HOME\n"
        self.assertEqual(sinks.scan(new_file("a.py", text)), [])

    def test_a_syntax_error_falls_back_to_the_added_lines(self):
        got = sinks.scan(new_file("a.py", "def (:\n    os.kill(pid, 9)\n    shutil.rmtree(d)\n"))
        self.assertEqual([(h.line, h.kind) for h in got], [(2, "kill"), (3, "rm")])


class Shell(ForemanTestCase):
    def hits(self, body, path="x.sh"):
        return [(h.line, h.kind) for h in sinks.scan(new_file(path, body))]

    def test_rm_kill_git_and_find_with_a_computed_target(self):
        body = ('rm -rf "$DIR"/x\nrm -rf build\nrm -rf $HOME/.cache\nrm -rf /\nrm -rf ./out/*\nrm file\n'
                'kill -9 $pid\nkill 1234\nkill -9 -1\nkill $(pgrep x)\npkill -f worker\nkillall node\n'
                'git -C "$wt" reset --hard\ngit reset --hard HEAD\ngit clean -fdx "$d"\ngit clean -fdx\n'
                'find "$d" -name x -delete\nfind . -name x -delete\n')
        self.assertEqual(self.hits(body), [
            (1, "rm"), (3, "rm"), (4, "rm"), (5, "rm"), (7, "kill"), (9, "kill"), (10, "kill"), (11, "pkill"),
            (12, "killall"), (13, "git"), (15, "git"), (17, "rm")])

    def test_a_comment_and_prose_are_not_sinks(self):
        self.assertEqual(self.hits("# rm -rf $DIR\necho 'we will kill $pid later'\n// kill $x\n"), [])

    def test_a_command_after_a_pipe_or_xargs_or_in_a_substitution(self):
        self.assertEqual(self.hits("ls | xargs rm -rf\nx=$(rm -rf $d)\nsudo rm -rf $d\nif x; then kill $p; fi\n"),
                         [(1, "rm"), (2, "rm"), (3, "rm"), (4, "kill")][1:])

    def test_a_workflow_and_a_makefile_are_read_as_shell(self):
        self.assertEqual(self.hits("  - run: rm -rf ${{ env.DIR }}\n", ".github/workflows/ci.yml"), [(1, "rm")])
        self.assertEqual(self.hits("clean:\n\trm -rf $(BUILD)\n", "Makefile"), [(2, "rm")])

    def test_markdown_is_not_scanned(self):
        self.assertEqual(self.hits("rm -rf $DIR\n", "README.md"), [])


class Languages(ForemanTestCase):
    """The languages Sampo maps (core.CODE): js ts go rs rb java kt c cs swift php lua zig."""

    def test_each_language_lists_a_computed_target_and_leaves_a_constant_one(self):
        cases = {
            "a.js": ("fs.rmSync(dir, { recursive: true })\nprocess.kill(pid)\nfs.rmSync('dist')\nexecSync('rm -rf ' + dir)\n", [1, 2, 4]),
            "a.ts": ("await fs.rm(path, { recursive: true })\nprocess.kill(-child.pid)\nrimraf(`${root}/x`)\n", [1, 2, 3]),
            "a.go": ('os.RemoveAll(dir)\nsyscall.Kill(-pid, 9)\nexec.Command("rm", "-rf", dir)\nos.RemoveAll("/tmp/x")\n', [1, 2, 3]),
            "a.rs": ('fs::remove_dir_all(path)?;\nCommand::new("rm").args(["-rf", dir])\nstd::fs::remove_dir_all("target")\n', [1, 2]),
            "a.rb": ("FileUtils.rm_rf(dir)\nFileUtils.rm_rf('tmp')\nsystem(\"rm -rf #{dir}\")\nProcess.kill(9, pid)\n", [1, 3]),
            "a.java": ('FileUtils.deleteDirectory(dir);\nRuntime.getRuntime().exec("rm -rf " + dir);\nFiles.delete(path);\n', [2, 3]),
            "a.kt": ("dir.deleteRecursively()\nFiles.deleteIfExists(path)\nFiles.deleteIfExists(\"x\")\n", [1, 2]),
            "a.c": ('kill(pid, SIGKILL);\nkill(getpid(), 9);\nsystem("rm -rf /tmp/x");\nunlink(path);\n', [1, 2]),
            "a.cs": ("Directory.Delete(path, true);\nProcess.GetProcessById(id).Kill();\n", []),
            "a.php": ('system("rm -rf " . $dir);\nunlink($file);\nposix_kill($pid, 9);\n', [1, 3]),
            "a.lua": ('os.execute("rm -rf " .. dir)\nos.remove(file)\nos.remove("x")\n', [1, 2]),
            "a.zig": ('std.fs.cwd().deleteTree(path) catch {};\n', []),
        }
        for path, (body, want) in cases.items():
            with self.subTest(path=path):
                self.assertEqual([h.line for h in sinks.scan(new_file(path, body))], want)

    def test_a_definition_a_method_on_an_object_and_a_container_are_not_sinks(self):
        body = "func kill(pid int) {}\nfunction kill(pid) {}\nproc.kill()\nlist.remove(x)\nthis.kill(pid)\nfn kill(pid: i32) {}\n"
        self.assertEqual(sinks.scan(new_file("a.go", body)), [])

    def test_a_non_code_file_is_not_scanned(self):
        self.assertEqual(sinks.scan(new_file("notes.txt", "rm -rf $DIR\nkill $pid\n")), [])
        self.assertEqual(sinks.scan(new_file("a.json", '{"x": "rm -rf $DIR"}\n')), [])


STUB_TEST = '''import unittest
from unittest import mock

import pilotsig


class Verb(unittest.TestCase):
    def test_no_plugin_to_load_means_no_run(self):
        with mock.patch.object(pilotsig.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            pilotsig._verb(None, "T-1")
'''


class Stub(ForemanTestCase):
    def hits(self, text, path="plugin/tests/test_x.py"):
        return [(h.line, h.call) for h in sinks.scan(new_file(path, text)) if h.kind == "stub"]

    def test_a_popen_stub_with_no_int_pid_is_flagged(self):  # the T-5899 shape
        got = sinks.scan(new_file("plugin/tests/test_pilotsig.py", STUB_TEST))
        self.assertEqual([(h.line, h.call, h.kind) for h in got], [(8, "def test_no_plugin_to_load_means_no_run", "stub")])
        self.assertIn("killpg(1", got[0].why)

    def test_an_int_pid_on_the_stub_clears_it(self):
        self.assertEqual(self.hits(STUB_TEST.replace('"Popen") as popen:', '"Popen") as popen:\n            popen.return_value.pid = 4242')), [])
        self.assertEqual(self.hits(STUB_TEST.replace('"Popen") as popen:', '"Popen", return_value=mock.Mock(pid=4242)) as popen:')), [])

    def test_a_pid_of_1_or_0_is_not_an_int_pid(self):
        self.assertEqual(len(self.hits(STUB_TEST.replace('"Popen") as popen:', '"Popen", return_value=mock.Mock(pid=1)) as popen:'))), 1)
        self.assertEqual(len(self.hits(STUB_TEST.replace('"Popen") as popen:', '"Popen", return_value=mock.Mock(pid=0)) as popen:'))), 1)

    def test_patching_both_os_kill_and_os_killpg_clears_it_patching_one_does_not(self):
        both = STUB_TEST.replace('        with mock', '        with mock.patch("os.kill"), mock.patch.object(pilotsig.os, "killpg"), mock', 1) \
                        .replace("mock.patch.object(pilotsig.subprocess", "mock.patch.object(pilotsig.subprocess", 1)
        self.assertEqual(self.hits(both), [])
        one = STUB_TEST.replace('        with mock', '        with mock.patch("os.kill"), mock', 1)
        self.assertEqual(len(self.hits(one)), 1)

    def test_a_setup_that_stubs_popen_flags_the_class_once(self):
        text = ("import unittest\nfrom unittest import mock\n\n\nclass A(unittest.TestCase):\n    def setUp(self):\n"
                "        mock.patch('subprocess.Popen').start()\n\n    def test_a(self):\n        pass\n\n    def test_b(self):\n        pass\n")
        self.assertEqual(self.hits(text), [(5, "class A")])

    def test_a_decorator_stub_flags_the_test(self):
        text = ("from unittest import mock\n\n\n@mock.patch('subprocess.Popen')\ndef test_a(popen):\n    pass\n")
        self.assertEqual(self.hits(text), [(5, "def test_a")])

    def test_a_test_file_that_stubs_nothing_is_clean_and_a_source_file_is_not_checked(self):
        self.assertEqual(self.hits("def test_a():\n    assert 1\n"), [])
        self.assertEqual(self.hits(STUB_TEST, path="plugin/lib/other.py"), [])

    def test_a_test_function_that_was_not_touched_is_not_flagged(self):
        read = lambda f: STUB_TEST + "\n    def test_other(self):\n        self.assertTrue(1)\n"
        self.assertEqual([h for h in sinks.scan(edit("plugin/tests/test_x.py", 12, "        self.assertTrue(1)"), read=read)
                          if h.kind == "stub"], [])

    def test_the_check_runs_on_the_added_lines_when_the_file_cannot_be_read(self):
        diff = edit("plugin/tests/test_x.py", 40, '        with mock.patch.object(pilotsig.subprocess, "Popen") as p:')
        self.assertEqual(kinds(diff, read=lambda f: None), [("plugin/tests/test_x.py", 40, "stub")])


class Reached(ForemanTestCase):
    def test_a_sink_a_test_calls_is_listed_though_the_diff_never_touched_it(self):
        test = ("import unittest\nfrom unittest import mock\n\nfrom sampo import pilotsig\n\n\nclass K(unittest.TestCase):\n"
                "    def test_k(self):\n        with mock.patch.object(pilotsig.os, 'killpg'):\n            pilotsig._kill_group(mock.Mock(pid=4242))\n")
        mods = {"plugin/lib/sampo/pilotsig.py": KILL_GROUP + "\n\ndef _unused(p):\n    os.kill(p, 9)\n"}
        got = sinks.scan(new_file("plugin/tests/test_pilotsig.py", test), read=lambda f: mods.get(f),
                         files=["plugin/lib/sampo/pilotsig.py", "plugin/tests/test_pilotsig.py"])
        self.assertEqual([(h.file, h.line, h.kind) for h in got], [
            ("plugin/lib/sampo/pilotsig.py", 7, "reached"), ("plugin/tests/test_pilotsig.py", 10, "kill")])  # + the helper call
        self.assertIn("called from plugin/tests/test_pilotsig.py", got[0].why)

    def test_a_sink_in_a_changed_source_file_is_listed_once_not_also_as_reached(self):
        test = "from sampo import pilotsig\n\n\ndef test_k():\n    pilotsig._kill_group(None)\n"
        diff = new_file("plugin/tests/test_pilotsig.py", test) + new_file("plugin/lib/sampo/pilotsig.py", KILL_GROUP)
        got = sinks.scan(diff, read=lambda f: KILL_GROUP if f.endswith("pilotsig.py") and "sampo" in f else None,
                         files=["plugin/lib/sampo/pilotsig.py"])
        self.assertEqual([(h.file, h.kind) for h in got], [
            ("plugin/lib/sampo/pilotsig.py", "kill"), ("plugin/tests/test_pilotsig.py", "kill")])  # the helper call is its own site

    def test_no_file_list_means_no_reach(self):
        test = "from sampo import pilotsig\n\n\ndef test_k():\n    pilotsig.other(None)\n"
        self.assertEqual(sinks.scan(new_file("plugin/tests/test_pilotsig.py", test)), [])


class Render(ForemanTestCase):
    def test_lines_are_file_line_kind_call_and_why_with_a_cap_and_a_count_of_the_rest(self):
        body = "import os\n" + "".join(f"os.kill(p{i}, 9)\n" for i in range(30))
        hits = sinks.scan(new_file("a.py", body))
        lines = sinks.render(hits, cap=5)
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[0].startswith("a.py:2: kill: os.kill(p0, 9) (the target `p0` is computed"), lines[0])
        self.assertEqual(lines[-1], "+25 more in a.py")

    def test_the_block_names_what_the_adversary_must_clear_and_is_empty_for_no_hits(self):
        self.assertEqual(sinks.block([]), "")
        text = sinks.block(sinks.scan(new_file("a.py", "import os\nos.kill(p, 9)\n")))
        for word in ("a stub or Mock", "None", "0, 1, -1", "an empty string", "/", "a finding", "- a.py:2: kill:"):
            self.assertIn(word, text)

    def test_stubs_and_reached_sinks_come_first_then_source_then_tests(self):
        diff = (new_file("plugin/tests/test_a.py", "import os\nos.kill(p, 9)\n") + new_file("b.py", "import os\nos.kill(q, 9)\n")
                + new_file("plugin/tests/test_verb.py", STUB_TEST))
        self.assertEqual([(h.file, h.kind) for h in sinks.scan(diff)], [
            ("plugin/tests/test_verb.py", "stub"), ("b.py", "kill"), ("plugin/tests/test_a.py", "kill")])

    def test_repo_text_is_redacted_and_cut(self):
        body = "import os\nos.kill(p, 9)  # sk-ant-api03-" + "a" * 40 + "\nrm -rf $X" + "y" * 300 + "\n"
        out = "\n".join(sinks.render(sinks.scan(new_file("a.sh", body))))
        self.assertNotIn("a" * 30, out)


class Prep(HookCase):
    """`sampo audit prep` puts the list in the brief and the adversary section holds the lens to it."""

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def prep(self):
        self.fm("init")
        self.tid = self.task(tier="M")

    def test_the_brief_lists_each_sink_and_the_adversary_section_must_clear_them(self):
        self.prep()
        self.write("lib/pilot.py", KILL_GROUP)
        self.write("scripts/clean.sh", 'rm -rf "$BUILD_DIR"/out\n')
        self.write("lib/safe.py", "import os\nos.kill(4242, 9)\n")
        data = json.loads(self.fm("audit", "prep", self.tid, "--json", "--lens", "adversary").stdout)
        with open(data["brief"]) as f:
            brief = f.read()
        self.assertIn("Destructive sinks (mechanical", brief)
        self.assertIn("- lib/pilot.py:7: kill: os.killpg(pid, signal.SIGKILL)", brief)
        self.assertIn("- scripts/clean.sh:1: rm:", brief)
        self.assertNotIn("lib/safe.py", brief.split("Destructive sinks")[1].split("\n\n")[0])
        adversary = brief.split("## adversary")[1]
        self.assertIn("Destructive sinks", adversary)
        self.assertIn("is yours to clear", adversary)
        self.assertEqual([x.split(":")[:2] for x in data["sinks"]], [["lib/pilot.py", "7"], ["scripts/clean.sh", "1"]])

    def test_a_diff_with_no_sink_adds_nothing(self):
        self.prep()
        self.write("lib/ok.py", "import os\nos.kill(4242, 9)\n")
        data = json.loads(self.fm("audit", "prep", self.tid, "--json").stdout)
        with open(data["brief"]) as f:
            self.assertNotIn("Destructive sinks (mechanical", f.read())
        self.assertEqual(data["sinks"], [])

    def test_a_test_that_stubs_popen_with_no_int_pid_reaches_the_brief(self):
        self.prep()
        self.write("tests/test_verb.py", STUB_TEST)
        data = json.loads(self.fm("audit", "prep", self.tid, "--json").stdout)
        self.assertEqual([x.split(":")[:2] for x in data["sinks"]], [["tests/test_verb.py", "8"]])
        with open(data["brief"]) as f:
            self.assertIn("test stub: def test_no_plugin_to_load_means_no_run", f.read())

    def test_the_json_carries_an_empty_sinks_error_when_the_scan_ran(self):
        self.prep()
        self.write("lib/ok.py", "import os\nos.kill(4242, 9)\n")
        self.assertEqual(json.loads(self.fm("audit", "prep", self.tid, "--json").stdout)["sinks_error"], "")

    def test_a_failed_scan_says_so_in_the_brief_text_and_in_the_error_key(self):
        from unittest import mock

        from sampo import cli
        with mock.patch.object(cli, "_without_secrets", side_effect=lambda root, d: d), mock.patch.object(
                sinks, "scan_pairs", side_effect=ValueError("boom")):
            hits, text, err = cli._sinks(type("P", (), {"root": self.repo})(), ["diff"], ["tree"])
        self.assertEqual((hits, err), ([], "ValueError"))
        self.assertIn("the scan failed (ValueError)", text)

    def test_the_file_list_is_read_for_a_test_file_by_the_testish_rule_not_by_a_substring(self):
        from unittest import mock

        from sampo import cli
        calls = []

        def git(root, *args, **kw):
            calls.append(args[0])
            return ""
        root = type("P", (), {"root": self.repo})()
        with mock.patch.object(cli, "_without_secrets", side_effect=lambda r, d: d), mock.patch.object(
                c, "_git", side_effect=git), mock.patch.object(sinks, "scan_pairs", return_value=[]):
            cli._sinks(root, [new_file("lib/contest.py", "x = 1\n")], ["t"])
            self.assertNotIn("ls-files", calls)
            cli._sinks(root, [new_file("spec/a_spec.py", "x = 1\n")], ["t"])
            self.assertIn("ls-files", calls)

    def test_the_adversary_template_names_the_sink_list(self):
        with open(os.path.join(c.PLUGIN_ROOT, "skills", "intake", "references", "audit.md")) as f:
            text = f.read()
        adversary = text.split("**adversary**")[1].split("**edge**")[0]
        self.assertIn("Destructive sinks", adversary)
        self.assertIn("a stub or Mock, None, 0, 1, -1, an empty string, /", adversary)


MODULE = '''import os


def _kill_group(proc):
    os.killpg(proc.pid, 9)


def _verb(proc):
    _kill_group(proc)


def run_verb(x):
    _verb(x)


def unrelated(p):
    os.kill(p, 9)
'''
VERB_TEST = ("from unittest import mock\nfrom sampo import verbs\n\n\ndef test_it():\n    mock.patch('subprocess.Popen')\n"
             "    verbs.run_verb(1)\n")
MOD = "plugin/lib/sampo/verbs.py"
TEST = "plugin/tests/test_verbs.py"


class Helpers(ForemanTestCase):
    """Round 2: a git reset or clean through a helper, and a kill helper, are sinks at the call site."""

    def test_git_reset_hard_and_clean_through_a_git_helper(self):
        src = ('def f(wt, sha):\n    _git(wt, "reset", "--hard", sha)\n    c._git(wt, "clean", "-fdq")\n    _git(wt, "status")\n'
               '    _git(wt, "reset", "--soft", sha)\n    git(wt, "clean", "-n")\n')
        self.assertEqual([(h.line, h.kind) for h in sinks.scan(new_file("a.py", src))], [(2, "git"), (3, "git")])

    def test_the_same_in_a_language_that_is_read_as_lines(self):
        src = 'def f(wt, sha)\n  _git(wt, "reset", "--hard", sha)\n  _git(wt, "status")\nend\n'
        self.assertEqual([(h.line, h.kind) for h in sinks.scan(new_file("a.rb", src))], [(2, "git")])

    def test_a_call_to_a_kill_helper_is_a_kill_at_the_call_site_and_its_definition_is_not(self):
        src = "def _kill_group(proc):\n    pass\n\n\ndef f(proc):\n    _kill_group(proc)\n    kill_group(proc, 9)\n    killsafe(proc.pid)\n    pilotsig._kill_group(proc)\n"
        self.assertEqual([(h.line, h.kind) for h in sinks.scan(new_file("a.py", src))],
                         [(6, "kill"), (7, "kill"), (8, "kill"), (9, "kill")])

    def test_a_dead_pid_probe_is_no_sink_in_ast_and_in_lines(self):
        src = "import os\nimport signal\n\n\ndef f(pid, sig):\n    os.kill(pid, 0)\n    os.kill(pid, signal.SIGKILL)\n    os.kill(pid, sig)\n"
        self.assertEqual([h.line for h in sinks.scan(new_file("a.py", src))], [7, 8])
        broken = [h.line for h in sinks.scan(new_file("a.py", src), read=lambda f: "def (:\n")]
        self.assertEqual(broken, [7, 8])

    def test_a_global_literal_is_listed_for_a_remove_call_too(self):
        src = 'import shutil\nshutil.rmtree("/")\nshutil.rmtree("build")\n'
        self.assertEqual([h.line for h in sinks.scan(new_file("a.py", src))], [2])


class Argv(ForemanTestCase):
    def test_a_path_to_rm_and_an_env_prefix_are_the_command(self):
        src = ('import subprocess\n\n\ndef f(d):\n    subprocess.run(["/bin/rm", "-rf", d])\n'
               '    subprocess.run(["env", "FOO=1", "rm", "-rf", d])\n    subprocess.run(["/usr/bin/env", "rm", "-rf", d])\n'
               '    subprocess.run(["rm", "-rf", "build"])\n    subprocess.run(["ls", d])\n')
        self.assertEqual([h.line for h in sinks.scan(new_file("a.py", src))], [5, 6, 7])

    def test_an_assignment_prefix_in_a_shell_string_is_seen_through(self):
        self.assertEqual([h.line for h in sinks.scan(new_file("a.sh", "LC_ALL=C rm -rf $DIR\nFOO=1 BAR=2 rm -rf \"$X\"\n"))], [1, 2])


class Continuation(ForemanTestCase):
    """The line fallback runs where the file is not parsed (cli.py is over 400 KB): a call may span lines."""
    read = staticmethod(lambda f: "def (:\n")

    def lines(self, text):
        return [(h.line, h.kind, h.why) for h in sinks.scan(new_file("a.py", text), read=self.read)]

    def test_a_call_whose_argument_is_on_the_next_line_is_read_with_it(self):
        got = self.lines("import os\nos.kill(\n    pid,\n    9)\nos.kill(\n    4242,\n    9)\n")
        self.assertEqual([(n, k) for n, k, _ in got], [(2, "kill")])

    def test_a_probe_on_continuation_lines_is_skipped(self):
        self.assertEqual(self.lines("import os\nos.kill(\n    pid,\n    0)\n"), [])

    def test_a_call_whose_argument_is_not_in_the_diff_is_listed_not_dropped(self):
        got = sinks.scan(edit("a.py", 5, "    os.kill("), read=self.read)
        self.assertEqual([(h.line, h.kind) for h in got], [(5, "kill")])
        self.assertIn("not on this line", got[0].why)


class Cut(ForemanTestCase):
    def test_test_cleanups_collapse_to_one_line_and_a_kill_in_the_same_file_stays(self):
        body = ("import os\nimport shutil\n\n\ndef test_a(a, b, c, pid):\n    shutil.rmtree(a)\n    os.remove(b)\n    os.unlink(c)\n"
                "    os.kill(pid, 9)\n    shutil.rmtree('/')\n")
        lines = sinks.render(sinks.scan(new_file("plugin/tests/test_a.py", body)))
        self.assertEqual(len(lines), 3, lines)
        self.assertTrue(lines[0].startswith("plugin/tests/test_a.py:9: kill [test file]: "), lines[0])
        self.assertTrue(lines[1].startswith("plugin/tests/test_a.py:10: rm [test file]: "), lines[1])
        self.assertTrue(lines[2].startswith("+3 test cleanups"), lines[2])

    def test_a_cleanup_in_a_source_file_is_listed_on_its_own(self):
        lines = sinks.render(sinks.scan(new_file("b.py", "import shutil\nshutil.rmtree(a)\n")))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("b.py:2: rm:"), lines[0])

    def test_a_stub_is_shown_before_thirty_source_sinks_and_the_cut_names_its_kind(self):
        diff = new_file("b.py", "import os\n" + "".join(f"os.kill(p{i}, 9)\n" for i in range(30))) + new_file(TEST, STUB_TEST)
        lines = sinks.render(sinks.scan(diff), cap=5)
        self.assertIn("test stub", lines[0])
        self.assertEqual(lines[-1], "+25 more in b.py")

    def test_stubs_are_capped_on_their_own_and_the_cut_is_named(self):
        body = "from unittest import mock\n\n\n" + "".join(
            f"def test_{i}():\n    mock.patch('subprocess.Popen')\n\n\n" for i in range(14))
        lines = sinks.render(sinks.scan(new_file(TEST, body)))
        self.assertEqual(len(lines), 11, lines)
        self.assertEqual(lines[-1], f"+4 more test stub hits in {TEST}")


class Reach(ForemanTestCase):
    FILES = [MOD, TEST]

    def scan(self, mod=MODULE, test=VERB_TEST):
        return sinks.scan(new_file(TEST, test), read=lambda f: mod if f == MOD else None, files=self.FILES)

    def test_a_sink_two_calls_down_is_reached_and_the_stub_links_to_it(self):
        got = self.scan()
        self.assertEqual([(h.file, h.line, h.kind) for h in got], [
            (TEST, 5, "stub"), (MOD, 5, "reached"), (MOD, 9, "reached")])
        self.assertIn(f"{MOD}:5", got[0].why)
        self.assertIn(f"stubbed at {TEST}:5", got[1].why)
        self.assertIn(f"stubbed at {TEST}:5", got[2].why)

    def test_the_helper_call_in_the_middle_is_a_kill_at_that_call_site(self):
        self.assertIn("_kill_group(proc)", [h.call for h in self.scan() if h.line == 9][0])

    def test_a_function_the_test_never_reaches_is_not_listed(self):
        self.assertNotIn(17,[h.line for h in self.scan()])

    def test_a_cycle_ends_and_a_chain_stops_after_three_hops(self):
        cyc = "import os\n\n\ndef a(x):\n    b(x)\n\n\ndef b(x):\n    a(x)\n    os.kill(x, 9)\n"
        self.assertEqual([h.line for h in self.scan(cyc, VERB_TEST.replace("run_verb", "a")) if h.kind == "reached"], [10])
        chain = "import os\n" + "".join(f"\n\ndef f{i}(x):\n    f{i + 1}(x)\n" for i in range(6)) + "\n\ndef f6(x):\n    os.kill(x, 9)\n"
        self.assertEqual([h for h in self.scan(chain, VERB_TEST.replace("run_verb", "f0")) if h.kind == "reached"], [])
        near = "import os\n" + "".join(f"\n\ndef f{i}(x):\n    f{i + 1}(x)\n" for i in range(2)) + "\n\ndef f2(x):\n    os.kill(x, 9)\n"
        self.assertEqual(len([h for h in self.scan(near, VERB_TEST.replace("run_verb", "f0")) if h.kind == "reached"]), 1)


class Pairs(ForemanTestCase):
    """Round 2: each diff pair is scanned on its own, and a hit from an earlier pair follows its line to the final tree."""

    def test_a_hit_from_an_earlier_pair_moves_with_its_line(self):
        first = "import os\n\n\ndef f(p):\n    os.kill(p, 9)\n"
        final = "import os\nimport sys\n\n\ndef f(p):\n    os.kill(p, 9)\n"
        got = sinks.scan_pairs([(edit("a.py", 5, "    os.kill(p, 9)"), lambda f: first), (edit("a.py", 2, "import sys"), lambda f: final)])
        self.assertEqual([(h.file, h.line) for h in got], [("a.py", 6)])

    def test_a_hit_the_final_tree_no_longer_holds_is_dropped(self):
        first = "import os\n\n\ndef f(p):\n    os.kill(p, 9)\n"
        final = "import os\n\n\ndef f(p):\n    pass\n"
        got = sinks.scan_pairs([(edit("a.py", 5, "    os.kill(p, 9)"), lambda f: first), (edit("a.py", 5, "    pass"), lambda f: final)])
        self.assertEqual(got, [])

    def test_a_sink_changed_in_both_pairs_is_listed_once(self):
        text = "import os\n\n\ndef f(p):\n    os.kill(p, 9)\n"
        got = sinks.scan_pairs([(edit("a.py", 5, "    os.kill(p, 9)"), lambda f: text), (edit("a.py", 5, "    os.kill(p, 9)"), lambda f: text)])
        self.assertEqual([(h.file, h.line) for h in got], [("a.py", 5)])

    def test_one_pair_is_the_scan_itself(self):
        text = "import os\n\n\ndef f(p):\n    os.kill(p, 9)\n"
        diff = edit("a.py", 5, "    os.kill(p, 9)")
        self.assertEqual(sinks.scan_pairs([(diff, lambda f: text)]), sinks.scan(diff, lambda f: text))
