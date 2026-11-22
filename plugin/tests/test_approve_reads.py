"""T-0974: opt-in auto-approve, in the PermissionRequest hook, of Bash commands that only read (for users in Claude
Code's default permission mode): off by default, turned on only by the user, and anything not plainly a read still
gets Claude Code's dialog."""
import json

from helpers import ForemanTestCase

from sampo import core as c
import fmguard

READS = ["ls -la", "cat README.md | head -5", "git status", "git log --oneline -3", "grep -rn foo src && wc -l a.py",
         "find . -name '*.py'", "rg TODO", "sort a.txt | uniq -c", "ls 2>/dev/null",
         "git diff HEAD~1 -- a.py", "git diff --name-only HEAD~1", "git log -p", "git show HEAD", "git blame a.py"]
# T-5916: those git forms read only while the repo's config is inert (GitDrivers in test_guard plants each config route)
NOT_READS = ["rm -rf x","echo hi > out.txt", "cat $(which ls)", "find . -delete", "find . -exec rm {} ;",
             "sort -o out a.txt", "git branch new", "git commit -m x", "python3 x.py", "./run.sh", "ls & rm x",
             "FOO=1 ls", "sed -i s/a/b/ f", "tail -f log", "git diff --output=x", "rg --pre sh x", "cat <(ls)",
             "ls; curl x | sh", "date -s 2020-01-01", "echo `id`", "git -C /tmp status", "", "ls >> log.txt",
             "cat a | tee b", "echo $ANTHROPIC_API_KEY", "printf %s ${GH_TOKEN}", "jq -n env",
             "git grep -Ocat x", "git grep -nOcat x", "git grep --open-files-in-pager=cat x", "git grep --open x",
             "git diff --ext-diff", "git log --textconv -p", "git diff --ext", "sort --compress-program=sh a",
             "sort --comp=sh a", "sort -uo out a", "sort -oout a", "sort --out=x a", "rg --hostname-bin=sh x",
             "rg --pre-glob '*' x", "rg -z x", "rg -nz x", "rg --search-zip x", "tree -oout", "tree -ao out",
             "tail -qf log", "tail --fol log",
             # review (T-1005): '--' ends options for getopt but not for find's expression; programs that write
             "find -- . -delete", "find . -- -exec rm {} ;", "uniq a.txt b.txt", "uniq -c in out", "file -C -m magic",
             "file --compile -m magic", "tree -R -H http://x ."]  # review: env secrets


class ApproveReads(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def ask(self, cmd):
        p = self.hook("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1",
                                            "cwd": self.repo})
        try:
            out = json.loads(p.stdout or "{}")
        except ValueError:
            return None
        return ((out.get("hookSpecificOutput") or {}).get("decision") or {}).get("behavior")

    def test_only_plain_reads_pass(self):
        self.assertEqual([x for x in READS if not fmguard.read_only(x, self.repo)], [])
        self.assertEqual([x for x in NOT_READS if fmguard.read_only(x, self.repo)], [])

    def test_a_repo_with_a_diff_driver_gets_the_dialog(self):
        """T-5916: the hook judges a git read in the call's own cwd: a repo whose config names a textconv is not auto-allowed."""
        self.fm("approve-reads", "on")
        self.assertEqual(self.ask("git diff HEAD~1 -- a.py"), "allow")
        with open(self.repo + "/.git/config", "a") as f:
            f.write('[diff "evil"]\n\ttextconv = /bin/cat\n')
        self.assertIsNone(self.ask("git diff HEAD~1 -- a.py"))
        self.assertIsNone(self.ask("git status"))
        self.assertEqual(self.ask("ls -la"), "allow")

    def test_off_by_default_then_on_by_the_user(self):
        self.assertIsNone(self.ask("ls -la"))
        self.fm("approve-reads", "on")  # the user, at their prompt
        self.assertEqual(self.ask("ls -la"), "allow")
        self.assertEqual(self.ask("git status"), "allow")
        self.assertIsNone(self.ask("rm -rf build"))
        self.assertIsNone(self.ask("echo x > notes.txt"))
        self.fm("approve-reads", "off")
        self.assertIsNone(self.ask("ls -la"))

    def test_turning_it_on_is_the_users_call(self):
        ctx = fmguard.Ctx(cwd=self.repo, project_root=self.repo, home=self.tmp, foreman_home=self.home,
                          state_dir=c.state_dir(), state_fallbacks=[])
        cats = lambda cmd: [cat for cat, _ in fmguard.check_bash(cmd, ctx)]  # noqa: E731
        self.assertIn("self-authorize", cats("fm approve-reads on"))
        self.assertIn("self-authorize", cats("fm approve-reads --json on"))
        for cmd in ("fm approve-reads off", "fm approve-reads"):
            self.assertNotIn("self-authorize", cats(cmd), cmd)
