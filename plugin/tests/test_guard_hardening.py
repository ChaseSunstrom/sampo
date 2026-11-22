"""T-1553 and its siblings: tools that test the guard against what the shell really does."""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "..", "lib"), os.path.join(HERE, "fixtures")):
    if p not in sys.path:
        sys.path.insert(0, p)

import fmguard  # noqa: E402
from sampo import oracle as soracle  # noqa: E402
from sampo import replay  # noqa: E402
import gen_evasions  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
SANDBOX = soracle.sandbox() is not None


@unittest.skipUnless(SANDBOX, "no bwrap: the oracle only runs commands inside a sandbox")
class Oracle(unittest.TestCase):
    """T-1553: run a line in a throwaway sandbox whose PATH holds nothing, so every program the shell would start is
    recorded with its real argv instead of run; each recorded command, judged alone, must pass where the line did."""

    def test_the_shell_runs_what_the_line_says(self):
        ran = soracle.ran("A=hi; echo $A | tr a-z A-Z; X=$(printf ls); $X -la")
        self.assertIn(["tr", "a-z", "A-Z"], ran)
        self.assertIn(["ls", "-la"], ran, "a computed command name is recorded as the shell expanded it")

    def test_nothing_real_runs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ran = soracle.ran("rm -rf ~; echo done > out.txt", d)
            self.assertEqual(ran[0], ["rm", "-rf", os.path.expanduser("~")], "recorded with the real ~, not run")
            self.assertFalse(os.path.exists(os.path.join(d, "out.txt")), "the line's folder is read-only")
            home_probe = os.path.join(os.path.expanduser("~"), "fm-oracle-probe-T1553")
            soracle.ran(f"/usr/bin/touch {d}/abs.txt {home_probe} /etc/fm-oracle-probe-T1553", d)
            for p in (os.path.join(d, "abs.txt"), home_probe, "/etc/fm-oracle-probe-T1553"):
                self.assertFalse(os.path.exists(p), f"a program run by its absolute path wrote {p}")

    def test_a_sandbox_that_fails_to_start_is_unable_not_agreement(self):
        with mock.patch.object(soracle, "sandbox", return_value="/bin/false"):
            self.assertIsNone(soracle.ran("ls"))
            bad = soracle.disagreements([("ls", "")])
        self.assertEqual([d["verdict"].split(":")[0] for d in bad], ["unable"])

    def test_both_branches_of_a_status_test_are_seen(self):
        self.assertIn(["rm", "-rf", "build"], soracle.ran("grep -q x f || rm -rf build"))
        self.assertIn(["touch", "y"], soracle.ran("if grep -q x f; then echo hi; else touch y; fi"))

    def test_the_line_cannot_switch_the_recorder_off(self):
        for off in ("command_not_found_handle() { :; }", "unset -f command_not_found_handle"):
            self.assertIn(["rm", "-rf", "build"], soracle.ran(f"{off}; rm -rf build"), off)

    def test_a_folder_above_home_or_tmp_does_not_uncover_them(self):
        import tempfile
        fd, marker = tempfile.mkstemp(dir="/tmp", prefix="fm-oracle-probe-")
        os.close(fd)
        try:
            self.assertEqual(soracle.ran("[ -e /proc/self ] && seen", "/"), [["seen"]], "the probe itself works")
            for where in ("/", "/tmp"):
                self.assertEqual(soracle.ran(f"[ -e {marker} ] && seen", where), [], where)
        finally:
            os.unlink(marker)

    def test_limits_and_tampered_records(self):
        ran = soracle.ran('probe "$(ulimit -v)" "$(ulimit -f)"; mkdir -p "$ORACLE_LOG/zz"; : > "$ORACLE_LOG/junk"; ls')
        self.assertEqual(ran[0][0], "probe")
        self.assertNotIn("unlimited", ran[0][1:], "memory and file size are capped")
        self.assertIn(["ls"], ran, "a stray file in the record folder is skipped, not a crash")

    def test_each_command_is_judged_in_the_folder_the_line_moved_to(self):
        home = os.path.expanduser("~")
        got = soracle.ran_in('cd ~/projects/x && git status; cd sub; ls; (cd /srv && make); cd ""; cd ../..; id', home)
        self.assertEqual(got, [(f"{home}/projects/x", ["git", "status"]), (f"{home}/projects/x/sub", ["ls"]),
                               ("/srv", ["make"]), (f"{home}/projects", ["id"])])
        tmp = soracle.ran_in('d=$(mktemp -d) && cd "$d" && git init -q', home)
        self.assertTrue(tmp[-1][0].startswith("/tmp/"), "mktemp ran (in the sandbox), so cd reached its folder")
        self.assertEqual(tmp[0][1][0], "mktemp", "and it was recorded")
        seen = []
        import types
        spy = types.SimpleNamespace(check=lambda tool, ti, ctx: seen.append((ti["command"], ctx.cwd)))
        with mock.patch.object(soracle, "_guard", return_value=spy):
            soracle.disagreements([("cd /srv && make", "")])
        self.assertIn(("make", "/srv"), seen)

    def test_a_guard_that_raises_is_unable(self):
        boom = mock.Mock()
        boom.check = mock.Mock(side_effect=RuntimeError("parser broke"))
        bad = soracle.disagreements([("ls", "")], guard=boom)
        self.assertEqual([d["verdict"].split(":")[0] for d in bad], ["unable"])

    def test_a_guard_that_misreads_a_line_is_caught(self):
        naive = mock.Mock()  # a guard that reads only the first word: the oracle sees what really runs
        naive.check = lambda tool, ti, ctx: None if ti["command"].startswith("true") else fmguard.check(tool, ti, ctx)
        bad = soracle.disagreements([("true; rm -rf ~", "")], guard=naive)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["ran"][:2], ["rm", "-rf"])

    def test_the_allow_corpus_agrees(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_GUARD_CASES", None)
            cases, _ = replay.load_cases()
        labelled = [(k["cmd"], k.get("cwd") or "") for k in cases]  # the refused ones run nothing: skipped inside
        self.assertGreater(len(labelled), 100)
        self.assertEqual(soracle.disagreements(labelled), [], "nothing misread and nothing unable")

    def test_fm_replay_oracle_reports_a_misread_line(self):
        import contextlib
        import io
        import json
        import types
        args = types.SimpleNamespace(oracle=True, cases=False, candidate=None, cwd=None, cmd=None, json=True)
        planted = [{"id": "x", "cmd": "true; rm -rf ~", "cwd": "", "should": "allow"}]
        naive = lambda tool, ti, ctx: None if ti["command"].startswith("true") else fmguard.check(tool, ti, ctx)
        buf = io.StringIO()
        with mock.patch.object(replay, "load_cases", return_value=(planted, 0)), \
                mock.patch.object(soracle, "_guard", return_value=types.SimpleNamespace(check=naive)), \
                contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as e:
            replay.cmd_replay(args)
        self.assertEqual(e.exception.code, 1)
        res = json.loads(buf.getvalue())
        self.assertEqual((res["cases"], len(res["disagreements"])), (1, 1))
        self.assertEqual(res["disagreements"][0]["ran"][:2], ["rm", "-rf"])


    def test_fm_replay_oracle_takes_one_command(self):
        import contextlib
        import io
        import json
        import types
        args = types.SimpleNamespace(oracle=True, cases=False, candidate=None, cwd=None, cmd="true; rm -rf ~", json=True)
        naive = lambda tool, ti, ctx: None if ti["command"].startswith("true") else fmguard.check(tool, ti, ctx)
        buf = io.StringIO()
        with mock.patch.object(soracle, "_guard", return_value=types.SimpleNamespace(check=naive)), \
                contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            replay.cmd_replay(args)
        res = json.loads(buf.getvalue())
        self.assertEqual((res["cases"], res["disagreements"][0]["ran"][:2]), (1, ["rm", "-rf"]))


class NoSandbox(unittest.TestCase):
    def test_without_bwrap_nothing_runs_and_replay_says_why(self):
        import contextlib
        import io
        import types
        args = types.SimpleNamespace(oracle=True, cases=False, candidate=None, cwd=None, cmd=None, json=False)
        buf = io.StringIO()
        with mock.patch.object(shutil, "which", return_value=None), contextlib.redirect_stdout(buf):
            self.assertIsNone(soracle.ran("rm -rf ~"))
            replay.cmd_replay(args)
        self.assertIn("bwrap", buf.getvalue())


_TO_SH = {"here_string", "script_file", "base64_sh"}


@unittest.skipIf(gen_evasions.home_in_a_repo(), "the home folder is inside a git tree: the guard judges it a project")
class Mutation(unittest.TestCase):
    """T-1605: a guard rule (category) is caught when dropping its findings alone, so the next finding decides as it
    would with the rule gone, turns the labelled corpus red; the rules no case catches are written down and may only
    shrink; seeded stacked obfuscations join the evasion corpus, and none may reach a dangerous command."""

    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_GUARD_CASES", None)
            cases, _ = replay.load_cases()
        cls.cases = cases + replay.load_cases(os.path.join(FIX, "guard_evasions.jsonl"))[0]
        cls.green = replay.drift(cls.cases)
        real = fmguard.check
        cls.caught = []
        for cat in fmguard.CATEGORIES:
            def mutant(tool, ti, ctx, found=None, cat=cat):
                found = fmguard.findings(tool, ti, ctx) if found is None else found
                return real(tool, ti, ctx, found=[f for f in found if f[0] != cat])
            with mock.patch.object(fmguard, "check", mutant):
                if any(d["should"] == "block" for d in replay.drift(cls.cases)):
                    cls.caught.append(cat)

    def test_the_corpus_is_green_before_any_mutant(self):
        self.assertEqual(self.green, [], "a mutant can only turn a green corpus red")

    def test_dropping_a_caught_rule_turns_the_corpus_red(self):
        self.assertGreaterEqual(len(self.caught), 7, "the corpora catch fewer guard rules than they did")

    def test_the_untested_rules_are_written_down_and_only_shrink(self):
        path = os.path.join(FIX, "guard_untested.json")
        with open(path, encoding="utf-8") as f:
            listed = json.load(f)["untested"]
        untested = [c for c in fmguard.CATEGORIES if c not in self.caught]
        self.assertEqual(untested, listed, "guard_untested.json is stale: a rule lost its last case (add one) or "
                                           "gained one (take it off the list)")
        try:  # in a git checkout, against the last commit: the list may lose names, never gain one
            head = subprocess.run(["git", "show", "HEAD:plugin/tests/fixtures/guard_untested.json"], cwd=HERE,
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            head = None
        if head and head.returncode == 0:
            self.assertEqual([c for c in listed if c not in json.loads(head.stdout)["untested"]], [],
                             "a guard rule joined the untested list: give it a labelled case instead")

    def test_no_evasion_reaches_a_dangerous_command(self):
        gaps, _ = replay.load_cases(os.path.join(FIX, "guard_gaps.jsonl"))
        self.assertEqual([g["cmd"] for g in gaps if g["should"] == "block"], [],
                         "an obfuscated dangerous command passes the guard: tighten fmguard.py, never list it")

    def test_seeded_stacks_join_the_corpus(self):
        stacked = [r for r in gen_evasions._rows() if "+" in r[0]]
        per = {}
        for _, orig, _, _ in stacked:
            per[orig] = per.get(orig, 0) + 1
        dangerous = {orig for _, orig, _, label in gen_evasions._rows() if label == "block"}
        self.assertEqual({o: n for o, n in per.items() if n < gen_evasions.STACKS}, {}, "a command got fewer stacks")
        self.assertEqual(set(per), dangerous, "every dangerous command gets stacks")
        with open(os.path.join(FIX, "guard_evasions.jsonl"), encoding="utf-8") as f:
            committed = {json.loads(ln)["cmd"] for ln in f if ln.strip()}
        self.assertTrue({v for _, _, v, _ in stacked} <= committed, "regenerate: python3 fixtures/gen_evasions.py")

    def test_a_stack_inside_bash_is_refused_for_what_it_hides(self):
        # an opaque shape (text fed to sh, eval of computed text) may be refused as opaque; a stack the shell reads in
        # place must still trip a rule the plain command trips, or the guard refused only its shape (a plain command
        # that is itself refused only as opaque, eval "$(curl …)", has nothing more to keep)
        import re
        opaque = re.compile(r"computed text|downloaded script")
        ctx = replay._ctx(replay.resolve_cwd(""))
        payload = lambda cmd: {k for k, d in fmguard.findings("Bash", {"command": cmd}, ctx) if not opaque.search(d)}
        off = [(t, o) for t, o, v, _ in gen_evasions._rows()
               if "+" in t and not set(t.split("+")) & _TO_SH and payload(o) and not payload(o) & payload(v)]
        self.assertEqual(off, [])

    @unittest.skipUnless(SANDBOX, "no bwrap: the oracle only runs commands inside a sandbox")
    def test_every_stack_runs_the_original(self):
        # the shell is watched running each stack; a stack that hands text to sh is checked one layer down (sh is a
        # recorded program too), by running what sh would be given; a plain command of builtins records nothing
        fns = {f.__name__: f for f in gen_evasions.EVERY}
        for tname, orig, v, _ in gen_evasions._rows():
            if "+" not in tname:
                continue
            want = {tuple(a) for a in soracle.ran(orig) or []}
            if not want:
                continue
            inner, outer = tname.split("+")
            if inner in _TO_SH:
                continue  # the original runs inside sh, which is itself a recorded program
            watched = fns[inner](orig) if outer in _TO_SH else v
            got = {tuple(a) for a in soracle.ran(watched) or []}
            self.assertTrue(want <= got, f"{tname} of {orig!r} doesn't run it: {watched!r}")


from helpers import ForemanTestCase  # noqa: E402


class Dojo(ForemanTestCase):  # T-1608
    def test_the_drill_mixes_refused_and_allowed_commands_with_the_guards_live_verdict(self):
        drills = json.loads(self.fm("dojo", "--rounds", "6", "--seed", "1", "--json").stdout)
        self.assertEqual(len(drills), 6)
        self.assertEqual(sorted(d["should"] for d in drills), ["allow"] * 3 + ["block"] * 3)
        for d in drills:
            self.assertTrue(d["why"], d)
            self.assertEqual(d["category"] == "allow", d["should"] == "allow", d)  # the corpus is green: they agree
        sheet = self.fm("dojo", "--rounds", "2", "--seed", "1").stdout
        self.assertIn("the guard refuses it", sheet)

    def test_a_quiz_scores_the_answers(self):
        drills = json.loads(self.fm("dojo", "--rounds", "4", "--seed", "2", "--json").stdout)
        right = "".join("r\n" if d["should"] == "block" else "a\n" for d in drills)
        out = self.fm("dojo", "--rounds", "4", "--seed", "2", "--quiz", input=right).stdout
        self.assertIn("4/4 right.", out)
        self.assertIn("fm guard try", out)
        out = self.fm("dojo", "--rounds", "4", "--seed", "2", "--quiz", input="a\n").stdout  # input ends: it stops
        self.assertRegex(out, r"[01]/1 right\.")
        self.assertNotEqual(self.fm("dojo", "--rounds", "0", check=False).returncode, 0)


class Trust(ForemanTestCase):
    """T-1606: a secret pasted into a prompt gets one note that never repeats it; the same kind of approval asked again
    and again in one session gets a note to batch; fm doctor --wrong files a false alarm as a labelled case for review."""

    KEY = "sk-ant-api03-" + "Q7f2Lm9xWv4Rt8Yp3Kd6Hs1Jc5Nb0Ga"  # key-shaped, made up

    def ctx(self, prompt):
        out = json.loads(self.hook("UserPromptSubmit", {"prompt": prompt}).stdout or "{}")
        return (out.get("hookSpecificOutput") or {}).get("additionalContext", "")

    def test_a_pasted_secret_gets_one_note_and_is_never_echoed(self):
        self.fm("init")
        note = self.ctx(f"use this key for the deploy: {self.KEY} thanks")
        self.assertIn("secret", note.lower())
        self.assertIn("rotat", note)
        self.assertNotIn(self.KEY[:20], note, "the note never repeats the secret")
        self.assertNotIn("secret", self.ctx("now fix the login bug").lower(), "only a message that holds one")
        for root, _, files in os.walk(os.path.join(self.home, "state")):
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    self.assertNotIn(self.KEY[:20].encode(), f.read(), f"{name} keeps the key; Foreman's state never does")

    def test_the_same_ask_again_and_again_gets_a_batching_note(self):
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "Ship it", "--type", "FEATURE", "--tier", "S", "--ac",
                                 "done :: true", "--step", "do", "--json").stdout)["id"]
        self.fm("focus", tid)
        first = self.fm_ask(tid, "publish").stdout
        second = self.fm_ask(tid, "publish").stdout
        third = self.fm_ask(tid, "publish").stdout
        self.assertNotIn("times this session", first + second)
        self.assertIn("publish asked 3 times this session", third)
        self.assertIn("batch", third)
        other = self.fm_ask(tid, "publish", session="sess-2").stdout
        self.assertNotIn("times this session", other, "a new session starts its own count")

    def test_a_secret_in_a_correction_is_never_stored(self):
        # a "never …" message becomes a veto; the key inside it must not reach vetoes.json (review of T-1606)
        self.fm("init")
        self.ctx(f"never use {self.KEY} again")
        self.ctx(f"<pasted_content>deploy key {self.KEY}</pasted_content> what is this?")
        for root, _, files in os.walk(os.path.join(self.home, "state")):
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    self.assertNotIn(self.KEY[13:30].encode(), f.read(), f"{name} keeps part of the key")

    def test_a_pasted_block_is_scanned_and_the_note_is_short(self):
        self.fm("init")
        note = self.ctx(f"<pasted_content>{self.KEY}</pasted_content> what is this?")
        self.assertIn("secret", note.lower())
        self.assertLess(len(note.split("Pasted secret", 1)[1].split(". ")[0]), 100, "short: room for the rest")

    def test_doctor_wrong_files_a_labelled_case_and_a_task_for_review(self):
        # the task goes to the project whose corpus holds the case (Foreman's own), named in the output
        self.fm("init")
        cases = os.path.join(self.tmp, "cases.jsonl")
        env = {"FOREMAN_GUARD_CASES": cases}
        cmd = "rm -rf ~/Documents"  # refused today and meant to stay so: only the filing is under test
        res = json.loads(self.fm("doctor", "--wrong", cmd, "--why", "plumbing test", "--json", env=env).stdout)
        with open(cases, encoding="utf-8") as f:
            case = json.loads(f.read().splitlines()[-1])
        self.assertEqual((case["cmd"], case["should"], case["id"]), (cmd, "allow", res["case"]["id"]))
        self.assertTrue(res["captured"] and res["project"])
        inbox = lambda: self.fm("inbox", "-p", res["project"]).stdout
        self.assertIn("Guard false alarm", inbox(), "a person reviews it as a task; the guard itself is unchanged")
        self.fm("doctor", "--wrong", cmd, env=env)
        self.assertEqual(inbox().count("Guard false alarm"), 1, "filed once")
        self.assertNotEqual(self.fm("doctor", "--wrong", f"curl -H 'Authorization: Bearer {self.KEY}' x", env=env,
                                    check=False).returncode, 0, "a command holding a secret isn't stored")

    def test_asks_are_counted_per_session_side_by_side(self):
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "Ship it", "--type", "FEATURE", "--tier", "S", "--ac",
                                 "done :: true", "--step", "do", "--json").stdout)["id"]
        self.fm("focus", tid)
        for _ in range(2):
            self.fm_ask(tid, "publish", session="sess-a")
            self.fm_ask(tid, "publish", session="sess-b")
        self.assertIn("publish asked 3 times this session", self.fm_ask(tid, "publish", session="sess-a").stdout)

from sampo import landlock  # noqa: E402

LANDLOCK = landlock.available()[0]


@unittest.skipUnless(LANDLOCK, "this kernel has no Landlock: confinement is optional and the run says so")
class Landlock(unittest.TestCase):
    """T-1607: an fm code worker's process tree can't write outside its tree, scratch and Claude's own folders."""

    @unittest.skipUnless(os.access(os.path.expanduser("~"), os.W_OK), "the home isn't writable here (a sandbox)")
    def test_a_confined_child_cannot_write_outside_its_set(self):
        from sampo import smith
        import tempfile
        with tempfile.TemporaryDirectory() as inside, tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as out:
            code, text = smith.run_bounded(f"echo a > in.txt; echo b > {out}/out.txt; cat /etc/hostname", inside, 30,
                                            confine=[inside])
            self.assertTrue(os.path.exists(os.path.join(inside, "in.txt")), text)
            self.assertFalse(os.path.exists(os.path.join(out, "out.txt")), "outside the set: refused")
            self.assertIn("Permission denied", text)
            self.assertTrue(text.strip().splitlines()[-1], "reads still work")

    def test_the_writable_set_covers_the_tree_its_git_and_claudes_folders(self):
        import tempfile
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "repo")
            subprocess.run(["git", "init", "-q", repo], check=True)
            paths = landlock.writable_for(repo)
            real = [os.path.realpath(p) for p in paths]
            for want in (repo, os.path.join(repo, ".git"), "/tmp", os.path.join(home, ".claude", "projects"),
                         os.path.join(home, ".claude.json"), os.path.join(home, ".cache")):
                if os.path.lexists(want):
                    self.assertIn(os.path.realpath(want), real, want)
            for never in (home, os.path.join(home, ".claude")):
                self.assertNotIn(os.path.realpath(never), real, "never the whole home or all of ~/.claude")


@unittest.skipUnless(LANDLOCK, "this kernel has no Landlock: confinement is optional and the run says so")
class LandlockBounds(unittest.TestCase):
    """T-1607 review: the set is narrow (never Claude Code's settings or Foreman's code), the ruleset is built in the
    parent, and a confinement that can't be made stops the run."""

    def test_claude_settings_hooks_and_foremans_code_stay_unwritable(self):
        import tempfile
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory() as d:
            real = [os.path.realpath(p) for p in landlock.writable_for(d)]
        for never in (os.path.join(home, ".claude"), os.path.join(home, ".claude", "settings.json"),
                      os.path.join(home, ".claude", "foreman"), os.path.join(home, ".claude", "foreman", "plugin"),
                      os.path.join(home, ".claude", "hooks"), os.path.join(home, ".claude", "plugins"), "/dev"):
            self.assertFalse(any(os.path.realpath(never) == p or os.path.realpath(never).startswith(p + "/")
                                 for p in real), never)
        projects = os.path.join(home, ".claude", "projects")
        if os.path.isdir(projects):
            self.assertIn(os.path.realpath(projects), real, "Claude Code's transcripts stay writable")

    def test_a_worktree_gets_objects_refs_and_its_own_git_dir_never_hooks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            main, wt = os.path.join(d, "main"), os.path.join(d, "wt")
            for argv in (["git", "init", "-q", main], ["git", "-C", main, "commit", "-q", "--allow-empty", "-m", "x"],
                         ["git", "-C", main, "worktree", "add", "-q", wt]):
                subprocess.run(argv, check=True, capture_output=True,
                               env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                        GIT_COMMITTER_EMAIL="t@t"))
            real = [os.path.realpath(p) for p in landlock.writable_for(wt)]
            common = os.path.realpath(os.path.join(main, ".git"))
            self.assertIn(os.path.join(common, "objects"), real)  # T-5909: objects/ until T-5924
            for never in ("refs", os.path.join("refs", "heads"), "logs", os.path.join("logs", "refs", "heads"),
                          "packed-refs"):  # a branch HEAD names gets no ref folder: only a fmw lane ref's own folder does
                self.assertNotIn(os.path.join(common, never), real, never)
            self.assertNotIn(common, real, "never the whole common dir: hooks and config live there")
            self.assertTrue(any(p.startswith(os.path.join(common, "worktrees")) for p in real))

    @unittest.skipUnless(os.access(os.path.expanduser("~"), os.W_OK), "the home isn't writable here (a sandbox)")
    def test_renames_truncation_and_file_rules_behave(self):
        from sampo import smith
        import tempfile
        with tempfile.TemporaryDirectory() as inside, tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as other:
            one = os.path.join(other, "one.json")
            open(one, "w").close()
            code, text = smith.run_bounded(
                f"mkdir a b && echo x > a/f && mv a/f b/f && : > b/f && echo ok-tree; echo y >> {one} && echo ok-file; "
                f"echo z > {other}/sibling.json || echo refused-sibling", inside, 30, confine=[inside, one])
            self.assertIn("ok-tree", text)
            self.assertIn("ok-file", text)
            self.assertIn("refused-sibling", text)
            self.assertFalse(os.path.exists(os.path.join(other, "sibling.json")))

    def test_a_confinement_that_cannot_be_made_stops_the_command(self):
        from sampo import smith
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(landlock, "prepare", side_effect=OSError("no ruleset")):
            with self.assertRaises(smith.ConfineError):
                smith.run_bounded("touch ran", d, 30, confine=[d])
            self.assertFalse(os.path.exists(os.path.join(d, "ran")), "never run unconfined instead")

    def test_verify_and_run_commands_are_confined_too(self):
        import inspect
        from sampo import smith
        # the worker's turn, its run commands, the plan check and the criteria's verify (through fm task evidence)
        for fn in (smith.Run.work, smith.Run.run_cmd, smith._plan_problem):
            calls = re.findall(r"run_bounded\((?:[^()]|\([^()]*\))*\)", inspect.getsource(fn))
            self.assertTrue(calls, fn.__name__)
            self.assertEqual([c for c in calls if "confine=" not in c or "confine=None" in c], [], fn.__name__)
        verify = inspect.getsource(smith.Run.verify)
        self.assertRegex(verify, r'self\.fm\("task", "evidence"(?:[^()]|\([^()]*\))*confine=', "verify runs confined")


class LandlockOptional(unittest.TestCase):
    def test_the_guard_refuses_turning_confinement_off(self):
        ctx = replay._ctx(os.path.expanduser("~"))
        for cmd in ("FOREMAN_CONFINE=0 fm code run T-1", "export FOREMAN_CONFINE=0; fm code run T-1",
                    "env FOREMAN_CONFINE=0 fm code run T-1"):
            b = fmguard.check("Bash", {"command": cmd}, ctx)
            self.assertEqual(getattr(b, "category", None), "self-authorize", cmd)
        self.assertIsNone(fmguard.check("Bash", {"command": "echo FOREMAN_CONFINE is documented"}, ctx))

    def test_without_landlock_workers_run_unconfined_and_say_why(self):
        from sampo import smith
        with mock.patch.object(landlock, "abi", return_value=0):
            ok, why = landlock.available()
            paths, note = smith.worker_confinement("/tmp")
        self.assertFalse(ok)
        self.assertIsNone(paths)
        self.assertIn("unconfined", note)
        self.assertIn(why, note)

    def test_fm_doctor_says_whether_workers_are_confined(self):
        from sampo import doctor
        with mock.patch.object(landlock, "abi", return_value=0):
            r = doctor.check_landlock()
        self.assertEqual(r.status, "PASS", "optional: never a failure")
        self.assertIn("unconfined", r.detail if hasattr(r, "detail") else str(r))

    def test_it_can_be_turned_off(self):
        from sampo import smith
        with mock.patch.dict(os.environ, {"FOREMAN_CONFINE": "0"}):
            paths, note = smith.worker_confinement("/tmp")
        self.assertIsNone(paths)
        self.assertIn("FOREMAN_CONFINE=0", note)


if __name__ == "__main__":
    unittest.main()
