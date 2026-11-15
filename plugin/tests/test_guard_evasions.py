"""The guard evasion corpus holds (T-1263).

guard_evasions.jsonl and guard_gaps.jsonl are gen_evasions.py's output. These tests hold the current guard to every
label in the corpus, hold every listed gap to still being a gap, and fail if regenerating would change either file —
so a guard change (a fix or a regression) forces a reviewed regeneration, and a fixed gap has to move into the corpus.
Run: cd plugin/tests && python3 -m unittest test_guard_evasions"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(os.path.dirname(HERE), "lib"), os.path.join(HERE, "fixtures"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from sampo import replay  # noqa: E402
import gen_evasions  # noqa: E402
from test_guard import GuardCase, g  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
CORPUS = os.path.join(FIX, "guard_evasions.jsonl")
GAPS = os.path.join(FIX, "guard_gaps.jsonl")


@unittest.skipIf(gen_evasions.home_in_a_repo(), "the home folder is inside a git tree: the guard judges it a project")
class GuardEvasions(unittest.TestCase):
    def _cases(self, path):
        cases, broken = replay.load_cases(path)
        self.assertEqual(broken, 0, f"a line of {os.path.basename(path)} is not a case")
        return cases

    def test_the_committed_files_are_the_generators_current_output(self):
        # regenerating must not change the committed bytes: a guard change breaks this until someone regenerates
        corpus_text, gaps_text = gen_evasions.build()
        with open(CORPUS, encoding="utf-8") as f:
            self.assertEqual(f.read(), corpus_text, "guard_evasions.jsonl is stale: python3 fixtures/gen_evasions.py")
        with open(GAPS, encoding="utf-8") as f:
            self.assertEqual(f.read(), gaps_text, "guard_gaps.jsonl is stale: python3 fixtures/gen_evasions.py")

    def test_the_guard_holds_every_corpus_label(self):
        cases = self._cases(CORPUS)
        self.assertGreater(len(cases), 0, "no evasion corpus")
        # T-1291: pin the temp folder as the real corpus test does, so a different TMPDIR cannot make a case drift
        with mock.patch.object(tempfile, "tempdir", "/var/tmp"):
            self.assertEqual(replay.drift(cases), [], "the guard disagrees with a labelled evasion")
            self.assertEqual(tempfile.tempdir, "/var/tmp", "verdicts must restore tempfile's folder")

    def test_every_listed_gap_is_still_a_gap(self):
        gaps = self._cases(GAPS)
        self.assertGreater(len(gaps), 0, "no gaps listed")
        with mock.patch.object(tempfile, "tempdir", "/var/tmp"):
            drifted = replay.drift(gaps)
        # every gap must still be one the guard gets wrong; a fix drops it from drift -> this fails until it moves to
        # the corpus. The recorded `got` must be what the guard actually returns now.
        self.assertEqual(len(drifted), len(gaps), "a listed gap no longer drifts: regenerate and move it to the corpus")
        by_id = {g["id"]: g for g in gaps}
        for d in drifted:
            self.assertEqual(d["got"], by_id[d["id"]]["got"], f"stored got is stale for {d['cmd']!r}")
        # a gap is a dangerous command let through or, outside a project (the corpus runs from home), an everyday one
        # refused once obfuscated: the guard can't see which tree a hidden rm or git writes to, so it says no
        self.assertTrue(all(g["got"] != g["should"] for g in gaps))
        known = ("-rf build/", "checkout -b fix/login")
        self.assertEqual([g["cmd"] for g in gaps if g["should"] == "allow" and not any(k in g["cmd"] for k in known)],
                         [], "a new over-broad refusal")


class SnapshotForms(unittest.TestCase):
    """T-1482: the four bench SNAPSHOT commands are allowed in a project because the PreToolUse hook saves the work
    they discard first. The corpus leaves them out (their verdict hangs on the tree), so here each obfuscated form runs
    from a disposable git folder: the hook must save it, or the guard refuse it; one that does neither (\\git reset
    --hard read as no git command) throws the work away unsaved."""
    def setUp(self):
        import subprocess
        base = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache")  # /tmp is scratch to the guard
        self._tmp = tempfile.TemporaryDirectory(dir=base if os.access(base, os.W_OK) else None)
        self.addCleanup(self._tmp.cleanup)
        self.t = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.t, "home")
        os.makedirs(os.path.join(self.home, ".claude", "foreman"))
        self.repo, self.other = os.path.join(self.t, "repo"), os.path.join(self.t, "other")
        for repo in (self.repo, self.other):
            os.makedirs(repo)
            git = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t",
                                             "-c", "commit.gpgSign=false", *a], check=True, capture_output=True)
            git("init", "-q")
            for i, step in enumerate(["0", "1", "2", "3", "4", "5", "6", "stashed", "dirty"]):
                with open(os.path.join(repo, "f.txt"), "w") as f:
                    f.write(step)
                if i < 7:  # HEAD~5 exists
                    git("add", "f.txt")
                    git("commit", "-qm", step)
                elif step == "stashed":  # a stash for stash clear to drop
                    git("stash", "push", "-q")
            # f.txt left "dirty": uncommitted work the commands would discard

    def judge(self, cmd, cwd):
        """(refusal or None, the hook's save note or None), in the hook's order: the guard, then the save."""
        import fmguard as g
        from sampo import hooks
        ctx = g.Ctx(cwd=cwd, project_root=g.project_root_for(cwd, self.home), home=self.home,
                    foreman_home=os.path.join(self.home, ".claude", "foreman"), scratch=["/tmp"], task_id="T-0001")
        refused = g.check("Bash", {"command": cmd}, ctx)
        note = hooks._snapshot_note({"tool_input": {"command": cmd}, "cwd": cwd}, g)
        return refused, note

    @staticmethod
    def saved_in(repo, note):
        """Whether the note says a save was made in repo: its restore line, which a \"couldn't save\" note never has."""
        return f"(restore: git -C {repo} " in (note or "")

    def test_every_obfuscated_snapshot_command_is_saved_or_refused(self):
        ran, unsaved = 0, []
        for cmd in sorted(gen_evasions.SNAPSHOT):
            variants = [(tr.__name__, tr(cmd)) for tr in gen_evasions.EVERY] + gen_evasions.stacked(cmd)
            for name, v in variants:
                if not v:
                    continue
                # in the folder, and from outside it after a cd (the save must land in the repo, not the hook's cwd)
                for full, cwd in ((v, self.repo), (f"cd {self.repo} && {v}", self.t)):
                    ran += 1
                    refused, saved = self.judge(full, cwd)
                    if refused is None and not self.saved_in(self.repo, saved):
                        unsaved.append(f"{name}: {full!r} from {cwd}: {saved}")
        self.assertGreater(ran, len(gen_evasions.SNAPSHOT) * 10, "too few forms: the test would be vacuous")
        self.assertEqual(unsaved, [], "allowed with nothing saved first:\n" + "\n".join(unsaved))

    def test_each_folder_a_command_discards_in_is_saved(self):
        # review: a cd before eval, a second repo of the same kind, a -C through a variable
        for cmd, cwd, repos in [
            (f"cd {self.repo} && eval 'git clean -fdx'", self.t, [self.repo]),
            (f"cd {self.repo}; sh <<< 'git clean -fdx'", self.t, [self.repo]),
            ("git reset --hard; $'git' -C ../other clean -fdx", self.repo, [self.repo, self.other]),
            (f"R={self.repo}; $'git' -C $R clean -fdx", self.t, [self.repo]),
            # second review: a relative cd inside the nested text starts from the outer folder
            (f"cd {self.repo} && eval 'cd sub && git clean -fdx'", self.t, [self.repo]),
            (f"cd {self.repo} && sh -c 'cd sub && g\"\"it clean -fdx'", self.t, [self.repo]),
        ]:
            with self.subTest(cmd=cmd):
                os.makedirs(os.path.join(self.repo, "sub"), exist_ok=True)
                _, saved = self.judge(cmd, cwd)
                for r in repos:
                    self.assertTrue(self.saved_in(r, saved), saved)

    def test_a_folder_it_cannot_follow_is_said_not_guessed(self):
        # second review: after cd $X the command runs who knows where; saving the hook's folder would be a false comfort
        for cmd in ("cd $X && g''it reset --hard", "cd $X && eval 'git clean -fdx'"):
            with self.subTest(cmd=cmd):
                _, note = self.judge(cmd, self.repo)
                self.assertFalse(self.saved_in(self.repo, note), note)
                self.assertIn("couldn't tell where", note or "")

    def test_saves_share_one_deadline(self):
        # second review: each repo's save had its own SNAPSHOT_S; two slow ones could outlast the hook's 5 s
        from sampo import hooks
        given = []

        def slow(top, why, timeout=None, what="tree"):
            given.append(timeout)
            import time
            time.sleep(0.3)
            return ["abc"]
        cmd = "git reset --hard; git -C ../other reset --hard; git -C ../other stash clear"
        with mock.patch.object(hooks, "SNAPSHOT_S", 0.5), mock.patch.object(hooks.c, "snapshot", slow):
            _, note = self.judge(cmd, self.repo)
        self.assertEqual(len(given), 2, note)  # the third had no time left
        self.assertLessEqual(given[1], 0.5 - 0.3 + 0.05)
        self.assertIn("no time left", note)


class CredentialReleaseWalks(GuardCase):
    """T-4666: the scratch and template releases of the credentials guard must not be walkable to a real secret. Every
    fixture (a link, a hardlink, a secret) is built in this test's own temp folder; no real credential file is named."""

    def scratch(self):
        # beside repo and home (one filesystem, so a hardlink works), never above them: a scratch root apart from both
        scr = os.path.join(os.path.dirname(self.repo), "scr")
        os.makedirs(scr, exist_ok=True)
        return scr

    def cctx(self):
        return g.Ctx(cwd=self.repo, project_root=self.repo, home=self.home, foreman_home=self.fhome,
                     scratch=[self.scratch()], allow=set(), task_id="T-0007")

    def test_cred_dotdot_through_a_scratch_link_is_the_real_file(self):
        scr = self.scratch()
        deploy = os.path.join(self.repo, "deploy")
        work = os.path.join(self.home, "work")
        os.makedirs(deploy, exist_ok=True)
        os.makedirs(work, exist_ok=True)
        for name, target in (("lnk-deploy", deploy), ("lnk-work", work)):
            if not os.path.lexists(os.path.join(scr, name)):
                os.symlink(target, os.path.join(scr, name))
        for path in (f"{scr}/lnk-deploy/../.env", f"{scr}/lnk-work/../.ssh/id_rsa", f"{scr}/lnk-deploy/../secrets.yaml"):
            with self.subTest(path=path):
                self.assertBlocked(g.check("Write", {"file_path": path, "content": "x"}, self.cctx()), "credentials")
                self.assertBlocked(g.check("Bash", {"command": f"echo x > {path}"}, self.cctx()), "credentials")
        # a plain folder, or a link that stays in scratch, is still scratch
        os.makedirs(os.path.join(scr, "a", "b"), exist_ok=True)
        if not os.path.lexists(os.path.join(scr, "lnk-in")):
            os.symlink(os.path.join(scr, "a", "b"), os.path.join(scr, "lnk-in"))
        for path in (f"{scr}/a/b/../.env", f"{scr}/lnk-in/../.env", f"{scr}/a/../redchk/.env"):
            with self.subTest(path=path):
                self.assertIsNone(g.check("Write", {"file_path": path, "content": "x"}, self.cctx()))
                self.assertIsNone(g.check("Bash", {"command": f"echo x > {path}"}, self.cctx()))

    def test_cred_hardlink_of_a_secret_to_a_released_name_is_refused(self):
        folder = os.path.join(self.repo, "hl")
        os.makedirs(folder, exist_ok=True)
        secret = os.path.join(folder, ".env")
        with open(secret, "w") as f:
            f.write("K=1\n")
        for cmd in (f"cd {folder} && ln .env .env.example", f"ln {secret} {folder}/.env.example",
                    f"ln -f {secret} {self.scratch()}/fixture.txt", f"link {secret} {folder}/.env.sample",
                    f"cp -l {secret} {folder}/.env.template", f"cp --link {secret} {self.scratch()}/x.txt"):
            with self.subTest(cmd=cmd):
                self.assertBlocked(g.check("Bash", {"command": cmd}, self.cctx()), "credentials")
        # a symlink is no second name for the inode; a hardlink of a template, or a read-only look, is fine
        for cmd in (f"cd {folder} && ln -s .env .env.example", f"ln {folder}/.env.example {folder}/.env.sample",
                    f"cd {folder} && ln -s .env .env.template"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(g.check("Bash", {"command": cmd}, self.cctx()))
        # an existing released name that already shares the secret's inode is that secret
        for name, root in ((".env.example", folder), (".env", self.scratch()), ("secrets.yaml.template", folder)):
            twin = os.path.join(root, name)
            if not os.path.lexists(twin):
                os.link(secret, twin)
            with self.subTest(twin=twin):
                self.assertBlocked(g.check("Write", {"file_path": twin, "content": "x"}, self.cctx()), "credentials")
        fresh = os.path.join(self.scratch(), "fresh", ".env.example")  # one link only: a template, as before
        os.makedirs(os.path.dirname(fresh), exist_ok=True)
        with open(fresh, "w") as f:
            f.write("K=\n")
        self.assertIsNone(g.check("Write", {"file_path": fresh, "content": "x"}, self.cctx()))


class ClimbThroughLinks(GuardCase):
    """T-4667: a path that climbs with '..' after a link is judged where it really goes in every category, not only the
    credentials one (T-4666), and in a read as in a write. Every link and hardlink is built in this test's own temp
    folder; no real credential, core or state file is named or touched."""

    def scratch(self):
        scr = os.path.join(os.path.dirname(self.repo), "scr")
        os.makedirs(scr, exist_ok=True)
        return scr

    def cctx(self, **kw):
        return g.Ctx(cwd=self.repo, project_root=self.repo, home=self.home, foreman_home=self.fhome,
                     scratch=[self.scratch()], allow=set(), task_id="T-0007", **kw)

    def link(self, name, target):
        os.makedirs(target, exist_ok=True)
        path = os.path.join(self.scratch(), name)
        if not os.path.lexists(path):
            os.symlink(target, path)
        return path

    def cats(self, tool, arg, ctx=None):
        key = "command" if tool == "Bash" else "file_path"
        return {c for c, _ in g.findings(tool, {key: arg, "content": "x"}, ctx or self.cctx())}

    def test_core_state_and_system_files_reached_through_a_scratch_link_are_judged_as_themselves(self):
        scr = self.scratch()
        lib = self.link("lnk-lib", os.path.join(self.fhome, "plugin", "lib"))
        proj = self.link("lnk-proj", os.path.join(self.fhome, "state", "projects", "x"))
        usr = self.link("lnk-usr", "/usr/lib")
        for path, want in ((f"{lib}/../rules/foreman.md", "core"), (f"{lib}/../lib/fmguard.py", "core"),
                           (f"{proj}/../x/ledger.jsonl", "state-direct"), (f"{usr}/../share/fixture.txt", "system")):
            for tool, arg in (("Write", path), ("Edit", path), ("Bash", f"echo x > {path}"), ("Bash", f"tee {path}"),
                              ("Bash", f"cp {self.repo}/README.md {path}"),
                              ("Bash", f"python3 -c \"open('{path}', 'w').write('x')\"")):
                with self.subTest(tool=tool, arg=arg):
                    self.assertIn(want, self.cats(tool, arg))
        self.assertTrue(scr)

    def test_a_relative_write_after_a_climbing_cd_through_a_link_is_judged_by_the_real_folder(self):
        # T-4673: `cd link/..` collapses to the link's lexical parent here, but cd -P (or set -P) lands in the real one,
        # where a later relative name is another file. Both places count; every link is in this test's own temp folder
        lib = self.link("lnk-lib", os.path.join(self.fhome, "plugin", "lib"))
        proj = self.link("lnk-proj", os.path.join(self.fhome, "state", "projects", "x"))
        usr = self.link("lnk-usr", "/usr/lib")
        for cd, rel, want in ((f"cd {lib}/..", "rules/foreman.md", "core"),
                              (f"cd -P {lib}/..", "rules/foreman.md", "core"),
                              (f"cd {proj}/..", "x/ledger.jsonl", "state-direct"),
                              (f"cd {usr}/..", "share/fixture.txt", "system"),
                              (f"cd {usr}/../", "share/fixture.txt", "system")):
            for cmd in (f"{cd} && echo x > {rel}", f"{cd} && tee {rel}", f"{cd}; echo x > {rel}",
                        f"{cd} && cp {self.repo}/README.md {rel}", f"{cd} && touch {rel}",
                        f"{cd} && python3 -c \"open('{rel}', 'w').write('x')\"",
                        f"{cd} && python3 - <<'EOF'\nopen('{rel}', 'w').write('x')\nEOF"):
                with self.subTest(cmd=cmd):
                    self.assertIn(want, self.cats("Bash", cmd))

    def test_a_relative_write_after_a_plain_cd_is_judged_in_that_folder_as_before(self):
        # the control for the test above: no link, so one reading, and the interpreter forms see it as they did
        folder = os.path.join(self.fhome, "plugin")
        for cmd in (f"cd {folder} && echo x > rules/foreman.md",
                    f"cd {folder} && python3 -c \"open('rules/foreman.md', 'w').write('x')\"",
                    f"cd {folder} && python3 - <<'EOF'\nopen('rules/foreman.md', 'w').write('x')\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertIn("core", self.cats("Bash", cmd))

    def test_a_cd_that_climbs_without_a_link_or_stays_in_scratch_frees_its_relative_writes(self):
        scr = self.scratch()
        os.makedirs(os.path.join(scr, "a", "b"), exist_ok=True)
        inside = self.link("lnk-in", os.path.join(scr, "a", "b"))
        for cd in (f"cd {scr}/a/b/..", f"cd {inside}/..", f"cd {scr}/a/../a", f"cd {self.repo}/deploy/.."):
            for cmd in (f"{cd} && echo x > fixture.txt", f"{cd} && python3 -c \"open('fixture.txt', 'w').write('x')\""):
                with self.subTest(cmd=cmd):
                    self.assertEqual(self.cats("Bash", cmd, self.cctx(confine=(self.repo, self.repo))), set())

    def test_a_climb_to_a_confined_or_unbriefed_project_file_is_judged_there(self):
        main, lane = self.repo, os.path.join(os.path.dirname(self.repo), "lane-x")
        os.makedirs(lane, exist_ok=True)
        deploy = self.link("lnk-dep", os.path.join(main, "deploy"))
        path = f"{deploy}/../README.md"  # lexically scratch/README.md, really the main checkout's file
        self.assertIn("confine", self.cats("Write", path, self.cctx(confine=(lane, main))))
        self.assertIn("brief", self.cats("Write", path, self.cctx(unbriefed=main)))

    def test_a_climb_that_stays_honest_is_not_refused(self):
        scr = self.scratch()
        os.makedirs(os.path.join(scr, "a", "b"), exist_ok=True)
        inside = self.link("lnk-in", os.path.join(scr, "a", "b"))
        for path in (f"{scr}/a/b/../fixture.txt", f"{inside}/../fixture.txt", f"{scr}/a/../redchk/x.txt",
                     f"{self.repo}/deploy/../notes.txt"):
            for tool, arg in (("Write", path), ("Bash", f"echo x > {path}")):
                with self.subTest(tool=tool, arg=arg):
                    self.assertEqual(self.cats(tool, arg, self.cctx(confine=(self.repo, self.repo))), set())

    def test_a_read_that_climbs_through_a_scratch_link_to_a_credential_is_refused(self):
        work = self.link("lnk-work", os.path.join(self.home, "work"))
        path = f"{work}/../.ssh/id_rsa"
        for tool, arg in (("Read", path), ("Bash", f"cat {path}"), ("Bash", f"cp {path} {self.scratch()}/copy.txt"),
                          ("Bash", f"python3 -c \"print(open('{path}').read())\"")):
            with self.subTest(tool=tool, arg=arg):
                self.assertIn("credentials", self.cats(tool, arg))
        # public parts and a plain read stay free
        pub = f"{work}/../.ssh/id_rsa.pub"
        self.assertEqual(self.cats("Read", pub), set())
        self.assertEqual(self.cats("Read", f"{self.scratch()}/a/../fixture.txt"), set())

    def test_the_real_path_is_read_where_resolve_returns_not_carried_on_the_string(self):
        # T-4667: a string operation on a resolved path (normpath, an f-string, a join) drops anything hung on a str
        # subclass; the verdict must not depend on it
        self.assertIs(type(g._resolve("a/../b", "/x")), str)
        work = self.link("lnk-work", os.path.join(self.home, "work"))
        path = f"{work}/../.ssh/id_rsa"
        self.assertEqual(g._walk(path, self.repo), (f"{self.scratch()}/.ssh/id_rsa",
                                                    os.path.join(self.home, ".ssh", "id_rsa")))
        self.assertEqual(g._walk(f"{self.scratch()}/a/../b", self.repo)[1], "")  # no link: nothing to add

    def test_home_credential_folders_match_in_any_letter_case(self):
        for path in (f"{self.home}/.SSH/id_x", f"{self.home}/.Ssh/known_hosts", f"{self.home}/.AWS/credentials",
                     f"{self.home}/.Config/GH/hosts.yml", f"{self.home}/.NETRC", f"{self.home}/.Git-Credentials",
                     f"{self.home.upper()}/.ssh/notes", f"{self.home}/.CLAUDE/.CREDENTIALS.JSON"):
            with self.subTest(path=path):
                self.assertIn("credentials", self.cats("Write", path))
                self.assertIn("credentials", self.cats("Bash", f"echo x > {path}"))
        for path in (f"{self.home}/.SSH/id_x", f"{self.home}/.NETRC"):
            with self.subTest(read=path):
                self.assertIn("credentials", self.cats("Read", path))
        # a folder that only looks alike is none
        for path in (f"{self.home}/.sshd-notes/x", f"{self.home}/.configs/ghost/x"):
            with self.subTest(path=path):
                self.assertNotIn("credentials", self.cats("Write", path))


class InterpreterHardlinks(GuardCase):
    """T-4667: a hardlink made by interpreter code is the same file under a released-looking name, as `ln` is (T-4666).
    The secret is made in this test's own temp folder."""

    def cctx(self):
        scr = os.path.join(os.path.dirname(self.repo), "scr")
        os.makedirs(scr, exist_ok=True)
        return g.Ctx(cwd=self.repo, project_root=self.repo, home=self.home, foreman_home=self.fhome, scratch=[scr],
                     allow=set(), task_id="T-0007")

    def folder(self):
        folder = os.path.join(self.repo, "ihl")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, ".env"), "w") as f:
            f.write("K=1\n")
        return folder

    def cats(self, cmd):
        return {c for c, _ in g.findings("Bash", {"command": cmd}, self.cctx())}

    def test_interpreter_code_that_hardlinks_a_credential_is_refused(self):
        folder = self.folder()
        for cmd in (f"cd {folder} && python3 -c \"import os; os.link('.env', '.env.example')\"",
                    f"python3 -c \"import os; os.link('{folder}/.env', '{folder}/.env.sample')\"",
                    f"cd {folder} && python3 -c \"from pathlib import Path; Path('.env.template').hardlink_to('.env')\"",
                    f"cd {folder} && node -e \"require('fs').linkSync('.env', '.env.example')\"",
                    f"cd {folder} && perl -e 'link(\".env\", \".env.example\")'",
                    f"cd {folder} && ruby -e 'File.link(\".env\", \".env.example\")'",
                    f"cd {folder} && python3 - <<'EOF'\nimport os\nos.link('.env', '.env.example')\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertIn("credentials", self.cats(cmd))

    def test_interpreter_links_that_make_no_second_name_for_a_secret_are_free(self):
        folder = self.folder()
        for cmd in (f"cd {folder} && python3 -c \"import os; os.link('a.txt', 'b.txt')\"",
                    f"cd {folder} && python3 -c \"import os; os.symlink('.env', '.env.example')\"",  # a symlink: no inode
                    f"cd {folder} && python3 -c \"import os; os.link('.env.example', 'x.txt')\"",
                    "python3 -c \"print('link(a, b)')\""):
            with self.subTest(cmd=cmd):
                self.assertNotIn("credentials", self.cats(cmd))
        # a copy is a new inode: the link scan adds nothing (the write scan may still read the name .env, as it always did)
        self.assertEqual(g._interp_links(f"cd {folder} && python3 -c \"import shutil; shutil.copy('.env', 'copy.txt')\"",
                                         self.cctx()), [])

    def test_a_copy_that_makes_a_new_file_is_no_hardlink_so_it_stays_free(self):
        # cp -a and rsync -H keep the links among the files they copy; neither gives the SOURCE a second name (the new
        # file is its own inode), so neither is judged like ln: a project's own .env backed up stays free. Only cp -l,
        # ln and link share the source's inode (T-4666); rsync --link-dest links a file under its own relative name
        folder = self.folder()
        for cmd in (f"cp -a {folder}/.env {folder}/copy.txt", f"rsync -aH {folder}/ {folder}-copy/"):
            with self.subTest(cmd=cmd):
                self.assertNotIn("credentials", self.cats(cmd))


class InterpreterSystemWrites(GuardCase):
    """T-4673: interpreter code that writes a system path is judged "system", in each language the scanner reads, as
    `echo x > /etc/x` is. Only the classifier is called: no test writes or runs anything against a system path, and the
    paths named are made-up files."""

    def cctx(self):
        scr = os.path.join(os.path.dirname(self.repo), "scr")
        os.makedirs(scr, exist_ok=True)
        return g.Ctx(cwd=self.repo, project_root=self.repo, home=self.home, foreman_home=self.fhome, scratch=[scr],
                     allow=set(), task_id="T-0007")

    def cats(self, cmd):
        return {c for c, _ in g.findings("Bash", {"command": cmd}, self.cctx())}

    def test_interpreter_code_that_writes_a_system_path_is_refused(self):
        for cmd in (
                "python3 -c \"open('/etc/t4673.conf', 'w').write('x')\"",
                "python3 -c \"open('/etc/t4673.conf', mode='a')\"",
                "python3 -c \"import io; io.open('/etc/t4673.conf', 'wb')\"",
                "python3 -c \"from pathlib import Path; Path('/etc/t4673.conf').write_text('x')\"",
                "python3 -c \"from pathlib import Path; Path('/usr/lib/t4673').mkdir()\"",
                "python3 -c \"from pathlib import Path; (Path('/etc') / 't4673.conf').unlink()\"",
                "python3 -c \"import os; os.remove('/usr/lib/t4673')\"",
                "python3 -c \"import os; os.rename('out.txt', '/usr/lib/t4673')\"",
                "python3 -c \"import os; os.chmod('/usr/bin/t4673', 0o777)\"",
                "python3 -c \"import os; os.symlink('out.txt', '/etc/t4673')\"",
                "python3 -c \"import shutil; shutil.copy('out.txt', '/usr/local/bin/t4673')\"",
                "python3 -c \"import shutil; shutil.rmtree('/usr/lib/t4673')\"",
                "python3 -c \"import os; p = '/etc/t4673.conf'; open(p, 'w').write('x')\"",
                "python3 -c \"import os; open(os.path.join('/etc', 't4673.conf'), 'w')\"",
                "python3 -c \"d = '/etc'; open(f'{d}/t4673.conf', 'w')\"",
                "python3 -c \"d = '/etc'; open(d + '/t4673.conf', 'w')\"",
                "python3 - <<'EOF'\nopen('/etc/t4673.conf', 'w').write('x')\nEOF",
                "python3 - <<'EOF'\nimport os\nos.remove('/usr/lib/t4673')\nEOF",
                "python3 - <<EOF\nimport os\nos.unlink('/etc/t4673')\nEOF",
                "python3 -c \"open('/dev/sda', 'wb')\"",
                "node -e \"require('fs').writeFileSync('/etc/t4673.conf', 'x')\"",
                "node -e \"require('fs').appendFileSync('/etc/t4673.conf', 'x')\"",
                "node -e \"require('fs').unlinkSync('/usr/lib/t4673')\"",
                "node -e \"require('fs').createWriteStream('/etc/t4673.conf')\"",
                "node -e \"require('fs').promises.writeFile('/etc/t4673.conf', 'x')\"",
                "node -e \"const f = '/etc/t4673.conf'; require('fs').writeFileSync(f, 'x')\"",
                "node -e \"require('fs').renameSync('out.txt', '/usr/lib/t4673')\"",
                "node -e \"require('fs').copyFileSync('out.txt', '/etc/t4673.conf')\"",
                "node -e \"require('fs').openSync('/etc/t4673.conf', 'w')\"",
                "node - <<'EOF'\nrequire('fs').writeFileSync('/etc/t4673.conf', 'x')\nEOF",
                "perl -e 'open(F, \">/etc/t4673.conf\"); print F 1'",
                "perl -e 'open(my $f, \">\", \"/etc/t4673.conf\"); print $f 1'",
                "perl -e 'open(F, \">>\", \"/etc/t4673.conf\")'",
                "perl -e 'unlink(\"/etc/t4673.conf\")'",
                "perl -e 'rename(\"out.txt\", \"/usr/lib/t4673\")'",
                "perl -e 'mkdir \"/usr/lib/t4673\"'",
                "perl -e 'my $f = \"/etc/t4673.conf\"; open(F, \">$f\")'",
                "ruby -e 'File.write(\"/etc/t4673.conf\", \"x\")'",
                "ruby -e 'File.open(\"/etc/t4673.conf\", \"w\") { |f| f.puts 1 }'",
                "ruby -e 'FileUtils.rm_f(\"/etc/t4673.conf\")'",
                "ruby -e 'File.delete \"/etc/t4673.conf\"'",
                "ruby -e 'File.rename(\"out.txt\", \"/usr/lib/t4673\")'",
                "php -r 'file_put_contents(\"/etc/t4673.conf\", \"x\");'",
                "php -r '$h = fopen(\"/etc/t4673.conf\", \"w\");'",
                "php -r 'unlink(\"/etc/t4673.conf\");'",
                "php -r 'copy(\"out.txt\", \"/etc/t4673.conf\");'",
                "lua -e 'io.open(\"/etc/t4673.conf\", \"w\"):write(\"x\")'",
                "lua -e 'os.remove(\"/etc/t4673.conf\")'",
                "lua -e 'os.rename(\"out.txt\", \"/usr/lib/t4673\")'",
                "deno eval 'await Deno.writeTextFile(\"/etc/t4673.conf\", \"x\")'",
                "deno eval 'Deno.removeSync(\"/etc/t4673.conf\")'",
                "bun -e 'await Bun.write(\"/etc/t4673.conf\", \"x\")'",
                "Rscript -e 'writeLines(\"x\", \"/etc/t4673.conf\")'",
                "Rscript -e 'write.csv(data.frame(a=1), \"/etc/t4673.csv\")'",
                "Rscript -e 'file.remove(\"/etc/t4673.conf\")'",
                "julia -e 'open(\"/etc/t4673.conf\", \"w\") do f; write(f, 1); end'",
                "julia -e 'rm(\"/etc/t4673.conf\")'",
                "tclsh <<'EOF'\nset f [open /etc/t4673.conf w]\nEOF",
                "tclsh <<'EOF'\nfile delete /etc/t4673.conf\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertIn("system", self.cats(cmd))

    def test_interpreter_code_that_only_reads_a_system_path_or_names_one_as_data_is_not_refused(self):
        for cmd in (
                "python3 -c \"print(open('/etc/hostname').read())\"",
                "python3 -c \"print(open('/etc/hostname').read()); open('out.txt', 'w').write('x')\"",
                "python3 -c \"import shutil; shutil.copy('/etc/hosts', 'out.txt')\"",
                "python3 -c \"import shutil; shutil.copytree('/usr/lib/python3', 'out')\"",
                "python3 -c \"import os; os.rename('out.txt', 'kept.txt'); print(os.listdir('/etc'))\"",
                "python3 -c \"p = '/usr/bin/git'; open('out.txt', 'w').write(p)\"",
                "python3 -c \"x = []; x.append('/etc/hosts'); x.remove('/etc/hosts'); open('o.txt', 'w')\"",
                "python3 -c \"s = '/etc/x'.replace('/etc', 'out'); open(s, 'w')\"",
                "python3 -c \"import os; os.path.exists('/etc/x'); os.makedirs('out/d', exist_ok=True)\"",
                "python3 -c \"open('/var/tmp/t4673.txt', 'w')\"",
                "python3 -c \"open('/tmp/t4673.txt', 'w')\"",
                "python3 -c \"from pathlib import Path; Path('out.txt').write_text(Path('/etc/hostname').read_text())\"",
                "python3 - <<'EOF'\nimport os\nos.remove('out.txt')\nprint(open('/etc/hostname').read())\nEOF",
                "node -e \"require('fs').writeFileSync('out.txt', require('fs').readFileSync('/etc/hostname'))\"",
                "node -e \"require('fs').writeFileSync('out.txt', '/etc/hostname')\"",
                "node -e \"require('fs').copyFileSync('/etc/hosts', 'out.txt')\"",
                "node -e \"require('fs').openSync('/etc/hostname', 'r')\"",
                "perl -e 'open(F, \">out.txt\"); print F \"/etc/hostname\"'",
                "perl -e 'open(F, \"/etc/hostname\"); open(G, \">\", \"out.txt\")'",
                "ruby -e 'File.write(\"out.txt\", File.read(\"/etc/hostname\"))'",
                "ruby -e 'File.open(\"/etc/hostname\") { |f| puts f.read }; File.write(\"o.txt\", 1)'",
                "php -r 'file_put_contents(\"out.txt\", file_get_contents(\"/etc/hostname\"));'",
                "php -r 'copy(\"/etc/hosts\", \"out.txt\");'",
                "lua -e 'io.open(\"out.txt\", \"w\"):write(io.open(\"/etc/hostname\"):read(\"a\"))'",
                "deno eval 'await Deno.writeTextFile(\"out.txt\", await Deno.readTextFile(\"/etc/hostname\"))'",
                "Rscript -e 'writeLines(readLines(\"/etc/hostname\"), \"out.txt\")'",
                "julia -e 'write(\"out.txt\", read(\"/etc/hostname\", String))'"):
            with self.subTest(cmd=cmd):
                self.assertNotIn("system", self.cats(cmd))


class InterpreterSystemWritesRoundTwo(GuardCase):
    """T-4673 review: a perl layer, work caps that must fail closed, a regex that must not blow up, and a scan that follows
    the commands in order (the folder a cd leaves, a heredoc that is only text) instead of reading the whole line."""

    cctx = InterpreterSystemWrites.cctx
    cats = InterpreterSystemWrites.cats

    def test_perl_three_argument_open_with_an_io_layer_is_judged(self):
        for cmd in (
                "perl -e 'open(my $f, \">:encoding(UTF-8)\", \"/etc/t4673.conf\")'",
                "perl -e 'open(F, \">>:raw\", \"/usr/lib/t4673\")'",
                "perl -e 'open(my $f, \"+<:utf8\", \"/etc/t4673.conf\")'"):
            with self.subTest(cmd=cmd):
                self.assertIn("system", self.cats(cmd))
        self.assertNotIn("system", self.cats("perl -e 'open(my $f, \"<:encoding(UTF-8)\", \"/etc/hostname\")'"))
        self.assertNotIn("system", self.cats("perl -e 'open(my $f, \">:utf8\", \"out.txt\")'"))

    def test_a_cap_never_hides_a_later_write(self):
        pad = "/* " + "open " * 200 + "*/\n"
        harmless = "".join(f"fs.writeFileSync('out{i}.txt', 'x');\n" for i in range(250))
        for cmd in (
                "node - <<'EOF'\nconst fs = require('fs');\n" + pad + "fs.writeFileSync('/etc/t4673.conf', 'x');\nEOF",
                "node - <<'EOF'\nconst fs = require('fs');\n" + harmless + "fs.writeFileSync('/etc/t4673.conf', 'x');\nEOF",
                "python3 - <<'EOF'\n" + "".join(f"open('o{i}.txt', 'w')\n" for i in range(200))
                + "open('/etc/t4673.conf', 'w')\nEOF",
                "python3 - <<'EOF'\nimport os\n" + "".join(f"p = 'a{i}'\n" for i in range(500))
                + "open('/etc/t4673.conf', 'w')\nEOF",
                "python3 - <<'EOF'\np = 'a'\n" + "p = 'b'\n" * 5 + "p = '/etc/t4673.conf'\nopen(p, 'w')\nEOF"):
            with self.subTest(cmd=cmd[:80]):
                self.assertIn("system", self.cats(cmd))

    def test_quotes_and_backslashes_do_not_make_the_scan_slow(self):
        import time
        for cmd in ("node - <<'EOF'\nfs.writeFileSync(\"" + "\\" * 2000 + "\nEOF",
                    "node - <<'EOF'\nfs.writeFileSync('x' + `" + "\\" * 2000 + "\nEOF",
                    "ruby -e 'File.open(\"out.txt\", \"w" + "\\" * 2000 + "'"):
            with self.subTest(cmd=cmd[:40]):
                t0 = time.monotonic()
                self.cats(cmd)
                self.assertLess(time.monotonic() - t0, 0.5)

    def test_a_cd_is_followed_in_order(self):
        scr = self.cctx().scratch[0]
        for cmd in (
                f"cd /usr/lib; ls; cd {scr}; python3 -c \"open('out.txt', 'w')\"",
                f"(cd /usr/lib && ls); python3 -c \"open('out.txt', 'w')\"",
                f"cd {scr} && python3 -c \"open('out.txt', 'w')\"",
                f"cd /usr/lib; cd {scr}; node -e \"require('fs').writeFileSync('out.txt', 'x')\""):
            with self.subTest(cmd=cmd):
                self.assertNotIn("system", self.cats(cmd))
        for cmd in (
                "cd /usr/lib && python3 -c \"open('t4673', 'a')\"",
                "cd \"/usr/lib\" && python3 -c \"open('t4673', 'a')\"",
                "cd /usr/lib; python3 -c \"open('t4673', 'a')\"",
                f"cd {scr}; cd /usr/lib; python3 -c \"open('t4673', 'a')\"",
                f"cd {scr}; ls; cd /usr/lib && ls && python3 -c \"open('t4673', 'a')\"",
                f"cd /usr/lib || true; python3 -c \"open('t4673', 'a')\"",
                f"if true; then cd /usr/lib; fi; python3 -c \"open('t4673', 'a')\"",
                f"cd /usr/lib; (cd {scr}); python3 -c \"open('t4673', 'a')\""):
            with self.subTest(cmd=cmd):
                self.assertIn("system", self.cats(cmd))

    def test_text_that_only_sits_in_the_line_is_not_code(self):
        for cmd in (
                "cat > doc.md <<'EOF'\nrun os.remove('/etc/t4673') then fs.writeFileSync('/etc/t4673', 1)\nEOF\npython3 -m pytest",
                "cat > notes.txt <<'EOF'\nopen('/etc/t4673', 'w')\nEOF\nnode app.js",
                "git commit -m \"docs: fs.writeFileSync('/etc/t4673') and os.remove('/etc/t4673') in a message\" && node app.js",
                "echo \"os.remove('/etc/t4673')\"; python3 -m pytest -q"):
            with self.subTest(cmd=cmd[:60]):
                self.assertNotIn("system", self.cats(cmd))
        for cmd in (
                "cat > t.py <<'EOF'\nopen('/etc/t4673', 'w')\nEOF\npython3 t.py",
                "cat > t.js <<'EOF'\nrequire('fs').writeFileSync('/etc/t4673', 'x')\nEOF\nnode t.js",
                "echo hi; python3 -c \"open('/etc/t4673', 'w')\"",
                "node -e \"require('fs').writeFileSync('/etc/t4673', 'x')\"; ls",
                "echo \"open('/etc/t4673', 'w')\" > t.py; python3 t.py",
                "python3 - <<'EOF'\nopen('/etc/t4673', 'w')\nEOF\ncat > doc.md <<'EOF'\nnothing\nEOF"):
            with self.subTest(cmd=cmd[:60]):
                self.assertIn("system", self.cats(cmd))

    def test_more_spellings_of_a_write_are_judged(self):
        for cmd in (
                "ruby -e 'File.open(\"/etc/t4673.conf\", \"w:UTF-8\") { |f| f.puts 1 }'",
                "node -e \"const f = require('fs'); f.writeFileSync('/etc/t4673.conf', 'x')\"",
                "node -e \"const { writeFileSync } = require('fs'); writeFileSync('/etc/t4673.conf', 'x')\"",
                "node --input-type=module -e \"import { writeFileSync as w } from 'fs'; w('/etc/t4673.conf', 'x')\""):
            with self.subTest(cmd=cmd):
                self.assertIn("system", self.cats(cmd))
        for cmd in (
                "python3 -c \"import sqlite3; sqlite3.connect(':memory:')\"",
                "python3 -c \"import sqlite3; sqlite3.connect('file:/etc/t4673.db?mode=ro', uri=True)\""):
            with self.subTest(cmd=cmd):
                self.assertNotIn("system", self.cats(cmd))
        self.assertIn("system", self.cats("python3 -c \"import sqlite3; sqlite3.connect('/etc/t4673.db')\""))

    def test_a_chained_climbing_cd_is_followed(self):
        scr = self.cctx().scratch[0]
        os.makedirs(os.path.join(scr, "d"), exist_ok=True)
        os.symlink("/usr/lib", os.path.join(scr, "lnk"))
        cmd = f"cd {scr}; cd -P lnk/..; python3 -c \"open('t4673', 'a')\""
        self.assertIn("system", self.cats(cmd))


class InterpreterSystemWritesRoundThree(GuardCase):
    """T-4673 round 3: code fed through xargs, a folder a wrapper's option sets, every silent cap ending in the unknown,
    code with no space or paren, a chdir inside the code, and a regex that must stay linear."""

    cctx = InterpreterSystemWrites.cctx
    cats = InterpreterSystemWrites.cats

    def judged(self, cmd):
        """Whether the guard named a path the interpreter code writes (the pipe rule's own refusal is another message)."""
        return any("written from interpreter code" in why for _, why in g.findings("Bash", {"command": cmd}, self.cctx()))

    def test_code_xargs_feeds_to_an_interpreter_is_judged(self):
        for cmd in (
                "printf '%s' \"open('/etc/t4673.conf','w')\" | xargs -0 python3 -c",
                "echo \"open('/etc/t4673.conf','w')\" | xargs -I{} python3 -c {}",
                "echo \"require('fs').writeFileSync('/etc/t4673.conf','x')\" | xargs -0 node -e"):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.judged(cmd))
        self.assertFalse(self.judged("echo \"open('out.txt','w')\" | xargs -0 python3 -c"))
        self.assertFalse(self.judged("ls | xargs -n1 python3 -m pytest"))

    def test_a_folder_a_wrapper_sets_is_where_the_code_runs(self):
        scr = self.cctx().scratch[0]
        for cmd in (
                "env -C /usr/lib python3 -c \"open('t4673', 'a')\"",
                "env --chdir=/usr/lib python3 -c \"open('t4673', 'a')\"",
                "env --chdir /usr/lib node -e \"require('fs').writeFileSync('t4673', 'x')\"",
                "sudo -D /usr/lib python3 -c \"open('t4673', 'a')\"",
                f"cd {scr}; env -C /usr/lib python3 -c \"open('t4673', 'a')\"",
                "cd /usr/lib; env -C \"$X\" python3 -c \"open('t4673', 'a')\""):
            with self.subTest(cmd=cmd):
                self.assertIn("system", self.cats(cmd))
        for cmd in (f"cd /usr/lib; env -C {scr} python3 -c \"open('out.txt', 'w')\"",
                    f"env --chdir={scr} python3 -c \"open('out.txt', 'w')\""):
            with self.subTest(cmd=cmd):
                self.assertNotIn("system", self.cats(cmd))

    def test_every_cap_ends_in_the_unknown(self):
        far = "a" * 3100
        many = ",".join(f'"a{i}"' for i in range(10))
        names = ", ".join(f"a{i}" for i in range(450))
        chain = "a0 = '/etc/t4673.conf'\n" + "".join(f"a{i + 1} = a{i}\n" for i in range(10)) + "open(a10, 'w')"
        for label, cmd in (
                ("call window", f"node -e \"require('fs').copyFileSync('{far}', '/etc/t4673.conf')\""),
                ("python depth", "python3 - <<'EOF'\n" + chain + "\nEOF"),
                ("eight picks", f"ruby -e 'File.delete({many}, \"/etc/t4673.conf\")'"),
                ("interpolation", "ruby -e 'x=\"a\"; x=\"b\"; x=\"c\"; x=\"/etc/t4673.conf\"; File.write(\"#{x}\", 1)'"),
                ("join", "ruby -e 'x=\"out\"; x=\"/etc\"; File.write(File.join(x, \"t4673.conf\"), 1)'"),
                ("name hops", "ruby -e 'x1=\"/etc/t4673.conf\"; x2=x1; x3=x2; x4=x3; File.write(x4, 1)'"),
                ("picked names", "node - <<'EOF'\nconst {" + names + ", writeFileSync} = require('fs');\n"
                 "writeFileSync('/etc/t4673.conf', 'x');\nEOF"),
                ("r named", "Rscript -e 'f <- \"a\"; f <- \"b\"; f <- \"c\"; f <- \"/etc/t4673.csv\"; cat(\"hi\", file=f)'"),
                ("perl modes", "perl -e '$m=\"<\"; $m=\"<\"; $m=\"<\"; $m=\">\"; open(F, $m, \"/etc/t4673.conf\")'"),
                ("perl paths", "perl -e '$p=\"a\"; $p=\"b\"; $p=\"c\"; $p=\"/etc/t4673.conf\"; open(F, \">\", $p)'")):
            with self.subTest(cap=label):
                self.assertIn("system", self.cats(cmd))

    def test_code_with_no_space_or_paren_is_judged(self):
        for cmd in ("perl -e 'unlink\"/etc/t4673.conf\"'",
                    "lua -e 'os.remove\"/etc/t4673.conf\"'",
                    "ruby -e 'File.delete\"/etc/t4673.conf\"'"):
            with self.subTest(cmd=cmd):
                self.assertIn("system", self.cats(cmd))
        self.assertNotIn("system", self.cats("perl -e 'unlink\"out.txt\"'"))

    def test_a_chdir_in_the_code_moves_its_relative_writes(self):
        for cmd in (
                "python3 -c \"import os; os.chdir('/usr/lib'); open('t4673', 'w')\"",
                "node -e \"process.chdir('/usr/lib'); require('fs').writeFileSync('t4673', 'x')\"",
                "perl -e 'chdir \"/usr/lib\"; unlink(\"t4673\")'",
                "ruby -e 'Dir.chdir(\"/usr/lib\"); File.write(\"t4673\", \"x\")'",
                "php -r 'chdir(\"/usr/lib\"); file_put_contents(\"t4673\", \"x\");'",
                "python3 -c \"import os, sys; os.chdir(sys.argv[1]); open('t4673', 'w')\" /usr/lib",
                "node -e \"process.chdir(process.argv[1]); require('fs').writeFileSync('t4673', 'x')\" /usr/lib"):
            with self.subTest(cmd=cmd):
                self.assertIn("system", self.cats(cmd))
        for cmd in (
                "python3 -c \"import os; os.chdir('build'); open('out.txt', 'w')\"",
                "python3 -c \"import os, sys; os.chdir(sys.argv[1]); open('/tmp/t4673.txt', 'w')\" /usr/lib",
                "python3 -c \"open('out.txt', 'w')\""):
            with self.subTest(cmd=cmd):
                self.assertNotIn("system", self.cats(cmd))

    def test_the_fs_name_pattern_is_linear(self):
        import time
        for cmd in ("node - <<'EOF'\nfs.writeFileSync('x', 'y'); " + "a" * 30000 + "\nEOF",
                    "node - <<'EOF'\nrequire('fs'); " + "a_" * 15000 + "\nEOF"):
            with self.subTest(cmd=cmd[:40]):
                t0 = time.monotonic()
                self.cats(cmd)
                self.assertLess(time.monotonic() - t0, 0.5)

    def test_more_node_and_ruby_spellings(self):
        for cmd in (
                "ruby -e 'open(\"/etc/t4673.conf\", \"w\")'",
                "node -e \"const { promises: p } = require('fs'); p.writeFile('/etc/t4673.conf', 'x')\"",
                "node -e \"const w = require('fs').writeFileSync; w('/etc/t4673.conf', 'x')\""):
            with self.subTest(cmd=cmd):
                self.assertIn("system", self.cats(cmd))
        self.assertNotIn("system", self.cats("ruby -e 'open(\"/etc/hostname\").read; open(\"out.txt\", \"w\")'"))
        self.assertIn("system", self.cats("Rscript -e 'cat(\"hi\", file=\"/etc/t4673.csv\")'"))  # file= opens the gate


if __name__ == "__main__":
    unittest.main()
