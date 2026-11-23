"""Fixes from the session-wide review of the self-improvement passes (T-0291): fm export stays in the project (T-0288),
fm second session's captures always wait for the user (T-0289), bench git runs no filter driver (T-0290)."""
import json
import os
import subprocess

from helpers import ForemanTestCase
from test_second import STUB


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


class Export(ForemanTestCase):
    def test_out_stays_inside_the_project(self):
        self.fm("init")
        outside = os.path.join(self.tmp, "outside.md")
        p = self.fm("export", "agents", "--out", outside, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("inside the project", p.stderr)
        self.assertFalse(os.path.exists(outside))
        os.symlink(self.tmp, os.path.join(self.repo, "up"))
        p = self.fm("export", "agents", "--out", os.path.join(self.repo, "up", "x.md"), "--force", check=False)
        self.assertNotEqual(p.returncode, 0)  # a link out of the project is still outside
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x.md")))
        os.makedirs(os.path.join(self.repo, "docs"))
        self.fm("export", "agents", "--out", os.path.join(self.repo, "docs", "AGENTS.md"))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "docs", "AGENTS.md")))
        for inside in (".git/info/x.md", ".claude/x.md"):  # review: config and settings live there
            os.makedirs(os.path.dirname(os.path.join(self.repo, inside)), exist_ok=True)
            self.assertNotEqual(self.fm("export", "agents", "--out", os.path.join(self.repo, inside), check=False)
                                .returncode, 0, inside)


class Confirm(ForemanTestCase):
    def test_a_childs_capture_waits_even_in_full_autonomy(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        cc = self.safe_config_dir()  # T-6020: a config folder under /tmp is refused by the child launcher
        env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": os.path.join(self.tmp, "log"),
               "CLAUDE_CONFIG_DIR": cc, "FOREMAN_SESSION_ID": "new-session"}
        self.fm("init")
        self.fm("autonomy", "full")
        from sampo import core as c
        from sampo import cost
        d = os.path.join(cc, "projects", os.path.basename(cost.transcripts_dir(self.repo)))
        os.makedirs(d)
        with open(os.path.join(d, "old-session.jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "export the report as CSV"}}))
        self.fm("second", "session", env=env)
        p = c.find_project(self.repo)
        b = next(x for x in c.load_briefs(p) if "CSV" in x.title)
        self.assertTrue(c.needs_approval(b, "full"))  # a child's words wait for the user's yes
        # review: none of the session's own moves settles it — approved=, explore=, --allow, batching, fm next
        self.fm("task", "set", b.id, "approved=true")
        self.fm("task", "set", b.id, "explore=false")
        self.assertTrue(c.needs_approval(c.find_brief(p, b.id), "full"))
        self.assertNotEqual(self.fm("task", "set", b.id, "--allow", "confirm", check=False).returncode, 0)
        plain = json.loads(self.fm("capture", "x", "--json").stdout)["id"]
        self.assertFalse(c.needs_approval(c.find_brief(p, plain), "full"))  # nothing else changes
        p2 = self.fm("batch", b.id, plain, check=False)
        self.assertNotEqual(p2.returncode, 0)
        self.assertIn("waits for the user", p2.stderr)
        self.assertNotIn(b.id, self.fm("next").stdout)
        self.assertIn(f"sampo ask {b.id} confirm", " ".join(c.plan_gaps(c.find_brief(p, b.id), "full")))
        self.fm_ask(b.id, "confirm", why="a child found it in an old session")  # the one way: the user's own yes
        self.hook("UserPromptSubmit", {"prompt": "yes"})
        self.assertFalse(c.needs_approval(c.find_brief(p, b.id), "full"))


class Filters(ForemanTestCase):
    def test_bench_git_runs_no_filter_driver(self):
        from sampo import bench
        repo = os.path.join(self.tmp, "r")
        os.makedirs(repo)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        with open(os.path.join(repo, ".gitattributes"), "w") as f:
            f.write("*.txt filter=evil\n")
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("hi\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")  # no driver configured yet: nothing runs
        marker = os.path.join(self.tmp, "ran")
        home = os.path.join(self.tmp, "home")  # review: the user's global config counts too (_git keeps HOME)
        os.makedirs(home)
        cfg = os.path.join(home, ".gitconfig")
        for key in ("clean", "smudge"):  # what a bypass-mode session could leave in a config file
            git(repo, "config", f"filter.evil.{key}", f"touch {marker}; cat")
            git(repo, "config", "--file", cfg, f"filter.other.{key}", f"touch {marker}; cat")
        with open(os.path.join(repo, ".gitattributes"), "a") as f:
            f.write("*.md filter=other\n")
        with open(os.path.join(repo, "b.md"), "w") as f:
            f.write("b\n")
        self.addCleanup(os.environ.__setitem__, "HOME", os.environ["HOME"])
        os.environ["HOME"] = home
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("changed\n")
        bench._safe_git(repo, "add", "-A")
        bench._safe_git(repo, "diff", "--cached", "HEAD")
        bench._safe_git(repo, "checkout", "HEAD", "--", "a.txt")
        with bench._worktree(repo, "HEAD"):
            pass
        self.assertFalse(os.path.exists(marker))


class AtomicWrite(ForemanTestCase):
    def test_a_planted_temp_name_doesnt_redirect_the_write(self):
        from sampo import core as c
        target = os.path.join(self.repo, "notes.md")
        outside = os.path.join(self.tmp, "elsewhere.md")
        os.symlink(outside, os.path.join(self.repo, f".notes.md.{os.getpid()}.tmp"))  # the name the old code used
        c.write_atomic(target, "hello\n")
        self.assertFalse(os.path.exists(outside))
        with open(target) as f:
            self.assertEqual(f.read(), "hello\n")
        os.chmod(target, 0o600)
        c.write_atomic(target, "again\n")
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)  # an existing file keeps its mode
