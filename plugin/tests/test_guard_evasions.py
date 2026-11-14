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


if __name__ == "__main__":
    unittest.main()
