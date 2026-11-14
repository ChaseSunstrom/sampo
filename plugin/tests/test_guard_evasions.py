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
                if want == "system" and arg.startswith("python3"):
                    continue  # interpreter code's writes are never judged "system", at any path (a separate gap)
                with self.subTest(tool=tool, arg=arg):
                    self.assertIn(want, self.cats(tool, arg))
        self.assertTrue(scr)

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


if __name__ == "__main__":
    unittest.main()
