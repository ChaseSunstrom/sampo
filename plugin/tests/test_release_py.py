"""T-0678 (T-1188): release.py is a thin wrapper over `fm release`: versions, changelog, commit, push, canary."""
import json
import os
import subprocess
import sys

from helpers import ForemanTestCase, read_text, stub_corpus_replay

from sampo import core as c
from test_release import stub_env

RELEASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "release.py")


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fx = os.path.join(self.tmp, "fx")
        os.makedirs(os.path.join(self.fx, "plugin", ".claude-plugin"))
        os.makedirs(os.path.join(self.fx, ".claude-plugin"))
        self.put("plugin/.claude-plugin/plugin.json", json.dumps({"name": "foreman", "version": "1.0.0"}, indent=2))
        self.put(".claude-plugin/marketplace.json", json.dumps(
            {"name": "foreman", "metadata": {"version": "1.0.0"},
             "plugins": [{"name": "foreman", "version": "1.0.0"}, {"name": "other", "version": "0.6.1"}]}, indent=2))
        self.put("CHANGELOG.md", "# Changelog\n\n## Unreleased\n- a change\n\n## 1.0.0\n- first\n")
        for args in (["init", "-q", "-b", "build"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                     ["add", "-A"], ["commit", "-qm", "base"]):
            git(self.fx, *args)
        self.stub = stub_env(self.tmp, self.fx)

    def put(self, rel, text):
        with open(os.path.join(self.fx, rel), "w") as f:
            f.write(text)

    def release(self, *args, check=True, gates=True):
        env = dict(os.environ, FOREMAN_HOME=self.home, **(self.stub if gates else {}))
        p = subprocess.run([sys.executable, RELEASE, *args, "--root", self.fx], capture_output=True, text=True, env=env)
        if check:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def grant(self):
        t = json.loads(self.fm("task", "new", "ship", "--type", "FEATURE", "--tier", "S", "--json", cwd=self.fx).stdout)["id"]
        self.fm("task", "step", t, "add", "ship it", cwd=self.fx)
        self.fm("task", "ac", t, "add", "shipped", "--verify", "true", cwd=self.fx)
        self.fm("focus", t, cwd=self.fx)
        p = c.find_project(self.fx)
        b = c.find_brief(p, t)
        b.meta["allow"] = ["publish"]
        c.save_brief(p, b)
        stub_corpus_replay(self.fx)  # T-1401: --push needs a fresh green guard corpus replay


class Release(Base):
    def test_one_command_bumps_heads_and_commits(self):
        self.release("1.0.1", "--no-check")
        self.assertEqual(json.loads(read_text(os.path.join(self.fx, "plugin/.claude-plugin/plugin.json")))["version"],
                         "1.0.1")
        market = read_text(os.path.join(self.fx, ".claude-plugin/marketplace.json"))
        self.assertEqual(market.count('"1.0.1"'), 2)
        self.assertIn('"0.6.1"', market, "another plugin's version is left alone")
        log = read_text(os.path.join(self.fx, "CHANGELOG.md"))
        self.assertTrue(log.startswith("# Changelog\n\n## Unreleased\n\n## 1.0.1 — "), log)
        self.assertIn("- a change", log.split("## 1.0.0")[0])
        self.assertEqual(git(self.fx, "log", "-1", "--format=%s").strip(), "Foreman 1.0.1")
        self.assertEqual(git(self.fx, "status", "--porcelain").strip(), "")

    def test_an_entry_not_at_the_old_version_is_left_alone(self):
        m = json.loads(read_text(os.path.join(self.fx, ".claude-plugin/marketplace.json")))
        m["metadata"]["version"] = "0.9.0"
        self.put(".claude-plugin/marketplace.json", json.dumps(m, indent=2))
        git(self.fx, "commit", "-qam", "odd")
        self.release("1.0.1", "--no-check")
        market = read_text(os.path.join(self.fx, ".claude-plugin/marketplace.json"))
        self.assertIn('"0.9.0"', market)
        self.assertEqual(market.count('"1.0.1"'), 1)

    def test_it_refuses_a_dirty_tree_or_an_older_version(self):
        self.put("stray.txt", "x\n")
        self.assertNotEqual(self.release("1.0.1", "--no-check", check=False).returncode, 0)
        os.remove(os.path.join(self.fx, "stray.txt"))
        p = self.release("0.9.0", "--no-check", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("newer", p.stderr)

    def test_push_merges_into_the_remotes_main(self):
        remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True)
        git(self.fx, "push", "-q", remote, "build:main")
        self.grant()
        self.release("1.0.1", "--push", remote)
        self.assertIn("Foreman 1.0.1", git(remote, "log", "-3", "--format=%s", "main"))

    def remote(self):
        remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True)
        git(self.fx, "push", "-q", remote, "build:main")
        return remote

    TERM = "zorblat-private.example"  # invented: never a real term

    def test_push_refuses_a_commit_that_carries_a_private_term_and_pushes_nothing(self):  # T-4785 (c): already held, now proved
        self.fm("private", "add", self.TERM)
        remote = self.remote()
        self.put("remote.py", f'A = 1\nHOST = "llm.{self.TERM}"\n')
        git(self.fx, "add", "remote.py")
        git(self.fx, "commit", "-qm", "work")
        self.grant()
        p = self.release("1.0.1", "--push", remote, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("remote.py", p.stderr)
        self.assertNotIn("zorblat", p.stdout + p.stderr)
        self.assertEqual(git(remote, "log", "-1", "--format=%s", "main").strip(), "base", "nothing was pushed")

    def test_push_refuses_when_the_term_list_cannot_be_read_and_pushes_nothing(self):  # T-4785 (d)
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w") as f:
            f.write("{not json")
        remote = self.remote()
        self.grant()
        p = self.release("1.0.1", "--push", remote, check=False)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertIn("term list", p.stderr)
        self.assertNotIn(c.PRIVATE_TERMS, p.stdout + p.stderr, "no path in the line")
        self.assertEqual(git(remote, "log", "-1", "--format=%s", "main").strip(), "base", "nothing was pushed")

    def test_a_failing_git_grep_is_named_by_its_exit_status_never_by_git_output(self):  # T-4785 review
        # the scan once quoted 200 characters of git's stderr: a path, a pathspec or a line of the blob (a term, a token)
        from unittest import mock
        from sampo import cli, release
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w") as f:
            json.dump([self.TERM], f)
        real = c._git

        def fake(root, *a, **k):
            if a and a[0] == "grep":
                return subprocess.CompletedProcess(["git", *a], 128, "", f"fatal: bad pattern /secret/invented/path {self.TERM}")
            return real(root, *a, **k)
        with mock.patch.object(c, "_git", fake), self.assertRaises(cli.UsageError) as e:
            release._private_hits(self.fx, [], "HEAD")
        self.assertIn("128", str(e.exception))
        self.assertIn("nothing was pushed", str(e.exception))
        for leak in ("zorblat", "invented", "bad pattern", "fatal"):
            self.assertNotIn(leak, str(e.exception))

    def test_a_list_with_no_usable_term_is_said_apart_from_no_list_and_is_still_only_a_notice(self):  # T-4785 review
        import contextlib
        import io
        from sampo import release
        path = os.path.join(c.state_dir(), c.PRIVATE_TERMS)
        os.makedirs(c.state_dir(), exist_ok=True)

        def said():
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                release.say_no_list("ship")
            return err.getvalue()
        self.assertIn("had no term list", said())
        for text in ("[]", '["ab"]'):
            with open(path, "w") as f:
                f.write(text)
            out = said()
            self.assertIn("no usable term", out, text)
            self.assertNotIn("had no term list", out, text)
            self.assertNotIn(c.PRIVATE_TERMS, out)
        with open(path, "w") as f:
            json.dump([self.TERM], f)
        self.assertEqual(said(), "")
        with open(path, "w") as f:
            f.write("[]")
        remote = self.remote()
        self.grant()
        p = self.release("1.0.1", "--push", remote)  # a notice, not a hold: the push goes on
        self.assertIn("no usable term", p.stdout + p.stderr)
        self.assertIn("Foreman 1.0.1", git(remote, "log", "-3", "--format=%s", "main"))

    def test_push_with_no_term_list_says_so_and_still_pushes(self):  # T-4785 (d)
        remote = self.remote()
        self.grant()
        p = self.release("1.0.1", "--push", remote)
        self.assertIn("the private-term check had no term list", p.stdout + p.stderr)
        self.assertIn("Foreman 1.0.1", git(remote, "log", "-3", "--format=%s", "main"))

    def test_push_is_refused_without_the_publish_grant(self):
        remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True)
        git(self.fx, "push", "-q", remote, "build:main")
        p = self.release("1.0.1", "--push", remote, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("publish", p.stderr)
        self.assertEqual(git(self.fx, "log", "-1", "--format=%s").strip(), "base")


class UnreadableList(Base):
    def test_an_unreadable_private_list_stops_the_scan_a_push_relies_on(self):
        # T-4804: 'no terms' from a list that exists and can't be read would let a push through unscanned: the scan refuses
        # with the fixed line (no term, no path) and says nothing was pushed; the diagnostic that only names line numbers
        # (private_lines) goes on, locally, with none
        from sampo import cli, release
        path = os.path.join(c.state_dir(), c.PRIVATE_TERMS)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('["wombat')
        tip = git(self.fx, "rev-parse", "HEAD").strip()
        with self.assertRaises(cli.UsageError) as e:
            release._private_hits(self.fx, [], tip)
        self.assertIn("the private-term list exists but cannot be read", str(e.exception))
        self.assertIn("nothing was pushed", str(e.exception))
        for text in (str(e.exception),):
            self.assertNotIn(path, text)
            self.assertNotIn("wombat", text)
        self.assertEqual(release.private_lines(self.fx, tip, ["CHANGELOG.md"]), {})
        with open(path, "w") as f:  # readable again: the scan runs as before
            f.write("[]")
        self.assertEqual(release._private_hits(self.fx, [], tip), ([], []))


class Canary(Base):
    def test_canary_runs_each_local_projects_sentinel(self):
        p = self.release("1.0.1", "--canary")
        self.assertIn("canary", p.stdout)
        self.assertIn("sentinel", p.stdout)
