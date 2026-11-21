"""T-3881: sampo update fetches origin, refuses what it can't update cleanly (a dirty tree, a branch that isn't the
release one, a diverged history, no origin) with one line each, fast-forwards, runs the plugin and home migrations only
when their dry runs find work, re-checks sampo-fast, and --undo puts the old commit (and what it migrated) back. Every
test runs on a fixture origin and a clone of it: the real checkout is never touched."""
import json
import os
import subprocess
from unittest import mock

from helpers import FM, ForemanTestCase

from sampo import fastbuild, homemove, pluginmove, update

ID = ["-c", "user.email=t@t", "-c", "user.name=t"]


def git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *ID, *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def put(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def manifest(version):
    return json.dumps({"name": "sampo", "version": version})


class Fixture(ForemanTestCase):
    """origin (bare), dev (a clone the 'author' commits in) and app (the install under test), all on main."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.dev = os.path.join(self.tmp, "dev")
        self.app = os.path.join(self.tmp, "install")  # not "app": the base case already made tmp/app
        clone = lambda src, dst: git(self.tmp, "clone", "-q", src, dst)  # noqa: E731
        git(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        clone(self.origin, self.dev)
        git(self.dev, "checkout", "-q", "-B", "main")
        put(self.dev, "plugin/.claude-plugin/plugin.json", manifest("1.0.0"))
        put(self.dev, "plugin/lib/sampo/core.py", "N = 1\n")
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-qm", "first release")
        git(self.dev, "push", "-q", "origin", "main")
        clone(self.origin, self.app)
        self.acct = os.path.join(self.tmp, "h")  # the account's home: no migration reads the real one
        self.cfg = os.path.join(self.acct, ".claude-config")
        os.makedirs(self.cfg)
        p = mock.patch.dict(os.environ, {"HOME": self.acct, "CLAUDE_CONFIG_DIR": self.cfg,
                                         "FOREMAN_HOME": os.path.join(self.tmp, "state-home")})
        p.start()
        self.addCleanup(p.stop)
        self.first = git(self.app, "rev-parse", "HEAD")

    def release(self, version="1.1.0", rel="plugin/lib/sampo/core.py", text="N = 2\n", msg="second release"):
        put(self.dev, rel, text)
        put(self.dev, "plugin/.claude-plugin/plugin.json", manifest(version))
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-qm", msg)
        git(self.dev, "push", "-q", "origin", "main")
        return git(self.dev, "rev-parse", "HEAD")

    def head(self):
        return git(self.app, "rev-parse", "HEAD")

    def quiet(self, plugins=None, repair=(True, "Native fast path: ok."), real_home=False):
        """Migrations with nothing to do, sampo-fast ok, unless a test says otherwise (real_home: the home move runs
        for real); each patch is stopped at cleanup."""
        pm = mock.patch.object(pluginmove, "migrate", side_effect=plugins or (lambda undo=False, dry=False: {
            "status": "done", "changed": False, "config": self.cfg}))
        hm = mock.patch.object(homemove, "migrate", side_effect=lambda undo=False, dry=False: {
            "status": "done", "manifest": "m", "changed": False})
        fb = mock.patch.object(fastbuild, "repair", return_value=repair)
        self.pm, self.fb = pm.start(), fb.start()
        self.hm = None if real_home else hm.start()
        for m in (pm, fb) if real_home else (pm, hm, fb):
            self.addCleanup(m.stop)


class Refusals(Fixture):
    def refused(self, **kw):
        r = update.run(self.app, **kw)
        self.assertEqual(r["status"], "refused", r)
        self.assertNotIn("\n", r["why"])  # one line, saying why
        return r["why"]

    def test_a_dirty_tree_is_refused_and_nothing_moves(self):
        self.release()
        put(self.app, "plugin/lib/sampo/core.py", "N = 99\n")
        self.assertIn("uncommitted", self.refused(check=True))
        self.assertIn("uncommitted", self.refused())
        self.assertEqual(self.head(), self.first)

    def test_an_untracked_file_is_not_dirt(self):
        self.release()
        self.quiet()
        put(self.app, "notes.txt", "mine\n")
        self.assertEqual(update.run(self.app, check=True)["status"], "plan")

    def test_a_branch_that_is_not_the_release_one_is_refused_as_a_dev_checkout(self):
        self.release()
        git(self.app, "checkout", "-q", "-b", "build")
        why = self.refused(check=True)
        self.assertIn("build", why)
        self.assertIn("merges", why)  # a dev checkout updates by its own merges
        git(self.app, "checkout", "-q", "--detach")
        self.assertIn("detached", self.refused())

    def test_a_diverged_history_is_refused(self):
        self.release()
        put(self.app, "local.txt", "x\n")
        git(self.app, "add", "-A")
        git(self.app, "commit", "-qm", "a local commit")
        mine = self.head()
        self.assertIn("diverged", self.refused(check=True))
        self.assertIn("diverged", self.refused())
        self.assertEqual(self.head(), mine)

    def test_an_origin_that_cannot_be_reached_is_refused(self):
        git(self.app, "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"))
        self.assertIn("origin", self.refused())

    def test_a_checkout_without_an_origin_or_git_is_refused(self):
        git(self.app, "remote", "remove", "origin")
        self.assertIn("origin", self.refused())
        r = update.run(self.tmp)  # a folder that is no checkout
        self.assertEqual(r["status"], "refused")
        self.assertIn("git", r["why"])

    def test_a_second_update_running_is_refused(self):
        self.release()
        import fcntl
        gitdir = git(self.app, "rev-parse", "--absolute-git-dir")
        with open(os.path.join(gitdir, "sampo-update.lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertIn("already running", self.refused())


class Check(Fixture):
    def test_check_names_the_commits_the_versions_and_the_migrations_and_changes_nothing(self):
        self.quiet(plugins=lambda undo=False, dry=False: {"status": "plan", "config": self.cfg, "moves": {"a": "b"},
                                                         "folder": "f", "dropped": [], "files": []})
        self.release(msg="the new thing")
        r = update.run(self.app, check=True)
        self.assertEqual(r["status"], "plan")
        self.assertEqual((r["version"]["from"], r["version"]["to"]), ("1.0.0", "1.1.0"))
        self.assertEqual([x["subject"] for x in r["commits"]], ["the new thing"])
        self.assertEqual(r["migrations"]["plugins"]["state"], "pending")
        self.assertEqual(r["migrations"]["home"]["state"], "none")
        self.assertEqual(self.head(), self.first)  # fetched, not merged
        self.assertEqual([c for c in self.pm.call_args_list if not c.kwargs.get("dry")], [])
        text = update.render(r)
        self.assertIn("the new thing", text)
        self.assertIn("1.0.0", text)
        self.assertIn("plugins", text)
        self.assertLessEqual(len(text.splitlines()), 8)  # a few lines, not a wall

    def test_dry_run_is_check(self):
        self.quiet()
        self.release()
        self.assertEqual(update.run(self.app, dry=True)["status"], "plan")
        self.assertEqual(self.head(), self.first)

    def test_up_to_date_says_so(self):
        self.quiet()
        r = update.run(self.app, check=True)
        self.assertEqual(r["status"], "current")
        self.assertIn("up to date", update.render(r))

    def test_ahead_of_origin_is_not_an_update(self):
        self.quiet()
        put(self.app, "local.txt", "x\n")
        git(self.app, "add", "-A")
        git(self.app, "commit", "-qm", "unpushed")
        r = update.run(self.app)
        self.assertEqual(r["status"], "current")
        self.assertIn("ahead", update.render(r))


class Apply(Fixture):
    def test_it_fast_forwards_and_runs_no_migration_whose_dry_run_finds_nothing(self):
        self.quiet()
        new = self.release()
        r = update.run(self.app)
        self.assertEqual(r["status"], "updated", r)
        self.assertEqual(self.head(), new)
        self.assertEqual(r["to"], new)
        self.assertEqual([c for c in self.pm.call_args_list if not c.kwargs.get("dry")], [])
        self.assertEqual([c for c in self.hm.call_args_list if not c.kwargs.get("dry")], [])
        self.assertEqual(update.run(self.app)["status"], "current")  # a second run has nothing to do

    def test_it_runs_the_plugin_migration_when_its_dry_run_finds_work(self):
        def plugins(undo=False, dry=False):
            return {"status": "plan" if dry else "done", "changed": not dry, "config": self.cfg, "moves": {"a": "b"},
                    "folder": "f", "dropped": [], "files": ["settings.json"], "backups": "bk"}
        self.quiet(plugins=plugins)
        self.release()
        r = update.run(self.app)
        self.assertEqual(r["migrations"]["plugins"]["state"], "ran")
        self.assertEqual(r["migrations"]["home"]["state"], "none")
        self.assertEqual([bool(c.kwargs.get("dry")) for c in self.pm.call_args_list], [True, False])
        self.assertIn("restart", update.render(r).lower())

    def test_it_moves_the_home_for_real_when_it_is_still_named_foreman(self):
        self.quiet(real_home=True)
        claude = os.path.join(self.acct, ".claude")
        old = os.path.join(claude, "foreman")
        os.makedirs(os.path.join(old, "state"))
        put(old, "state/x.txt", "kept\n")
        with mock.patch.dict(os.environ, {"FOREMAN_HOME": old}):
            self.release()
            r = update.run(self.app)
            self.assertEqual(r["migrations"]["home"]["state"], "ran", r)
            self.assertTrue(os.path.islink(old))
            self.assertTrue(os.path.isfile(os.path.join(claude, "sampo", "state", "x.txt")))
            u = update.run(self.app, undo=True)
            self.assertEqual(u["status"], "undone", u)
            self.assertTrue(os.path.isdir(old) and not os.path.islink(old))
            self.assertTrue(os.path.isfile(os.path.join(old, "state", "x.txt")))
        self.assertEqual(self.head(), self.first)

    def test_a_migration_that_cannot_run_here_is_told_not_fatal(self):
        def plugins(undo=False, dry=False):
            raise ValueError("the foreman marketplace isn't a folder on this machine")
        self.quiet(plugins=plugins)
        new = self.release()
        r = update.run(self.app)
        self.assertEqual(r["status"], "updated")
        self.assertEqual(self.head(), new)
        self.assertEqual(r["migrations"]["plugins"]["state"], "skipped")
        self.assertIn("isn't a folder", update.render(r))

    def test_sampo_fast_is_checked_after_the_merge_and_its_state_reported(self):
        self.quiet(repair=(True, "native fast path installed (900 KiB, built from abc; sum verified); hooks/fast runs it"))
        self.release()
        r = update.run(self.app)
        self.fb.assert_called_once()
        self.assertEqual(os.path.realpath(self.fb.call_args.args[0] if self.fb.call_args.args else
                                          self.fb.call_args.kwargs["plugin"]),
                         os.path.realpath(os.path.join(self.app, "plugin")))
        self.assertTrue(r["fast"]["ok"])
        self.assertIn("sum verified", update.render(r))

    def test_a_fast_path_that_cannot_be_rebuilt_says_the_python_hook_stays_and_the_update_stands(self):
        self.quiet(repair=(False, "native fast path not installed: no cargo in ~/.cargo/bin; the Python hook stays"))
        new = self.release()
        r = update.run(self.app)
        self.assertEqual(r["status"], "updated")
        self.assertEqual(self.head(), new)
        self.assertFalse(r["fast"]["ok"])
        self.assertIn("Python hook stays", update.render(r))

    def test_a_running_session_is_told_what_to_do_only_when_plugin_files_changed(self):
        self.quiet()
        self.release(rel="plugin/lib/sampo/core.py", text="N = 3\n", msg="library change")
        self.assertIn("restart", update.run(self.app)["restart"].lower())
        put(self.dev, "README.md", "y\n")
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-qm", "readme only")
        git(self.dev, "push", "-q", "origin", "main")
        r = update.run(self.app)
        self.assertEqual(r["status"], "updated")
        self.assertEqual(r["restart"], "")


class Undo(Fixture):
    def test_undo_puts_the_old_commit_back(self):
        self.quiet()
        self.release()
        update.run(self.app)
        r = update.run(self.app, undo=True)
        self.assertEqual(r["status"], "undone", r)
        self.assertEqual(self.head(), self.first)
        self.assertIn(self.first[:9], update.render(r))

    def test_undo_without_an_update_has_nothing_to_undo(self):
        r = update.run(self.app, undo=True)
        self.assertEqual(r["status"], "refused")
        self.assertIn("nothing to undo", r["why"])

    def test_undo_refuses_once_the_branch_moved_on_or_the_tree_is_dirty(self):
        self.quiet()
        self.release()
        update.run(self.app)
        put(self.app, "plugin/lib/sampo/core.py", "N = 5\n")
        self.assertIn("uncommitted", update.run(self.app, undo=True)["why"])
        git(self.app, "checkout", "-q", "--", ".")
        put(self.app, "local.txt", "x\n")
        git(self.app, "add", "-A")
        git(self.app, "commit", "-qm", "work on top of the update")
        r = update.run(self.app, undo=True)
        self.assertEqual(r["status"], "refused")
        self.assertIn("moved on", r["why"])

    def test_undo_runs_the_undo_of_each_migration_the_update_ran(self):
        def plugins(undo=False, dry=False):
            if undo:
                return {"status": "undone", "config": self.cfg, "files": ["settings.json"]}
            return {"status": "plan" if dry else "done", "changed": not dry, "config": self.cfg, "moves": {"a": "b"},
                    "folder": "f", "dropped": [], "files": ["settings.json"], "backups": "bk"}
        self.quiet(plugins=plugins)
        self.release()
        update.run(self.app)
        r = update.run(self.app, undo=True)
        self.assertEqual(r["status"], "undone")
        self.assertEqual([bool(c.kwargs.get("undo")) for c in self.pm.call_args_list][-1], True)
        self.assertEqual([c for c in self.hm.call_args_list if c.kwargs.get("undo")], [])  # home wasn't moved: not undone


class Verb(Fixture):
    def run_fm(self, *args):
        return subprocess.run([FM, "update", "--repo", self.app, *args], capture_output=True, text=True,
                              env=dict(os.environ))

    def test_the_verb_checks_as_json_and_refuses_with_exit_1(self):
        self.release()
        r = self.run_fm("--check", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "plan")
        self.assertEqual(self.head(), self.first)
        put(self.app, "plugin/lib/sampo/core.py", "N = 99\n")
        r = self.run_fm("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("uncommitted", r.stdout + r.stderr)

    def test_output_carries_no_escape_codes_when_piped(self):
        self.release()
        self.assertNotIn("\x1b", self.run_fm("--check").stdout)


class ReviewFixes(Fixture):
    """T-3881 review: a landed merge is finished on the next run, a migration that can't import is told, origin text is
    plain, a failed update keeps the earlier undo handle, only Sampo's own checkout is moved."""

    def rec(self, gitdir):
        with open(os.path.join(gitdir, "sampo-update.json")) as f:
            return json.load(f)

    def test_a_landed_merge_whose_migration_never_finished_is_retried_on_the_next_run(self):
        calls = []

        def plugins(undo=False, dry=False):
            calls.append(dry)
            return {"status": "plan" if dry else "done", "changed": not dry, "config": self.cfg, "moves": {"a": "b"}}
        self.quiet(plugins=plugins)
        self.release()
        git(self.app, "fetch", "-q", "origin")
        git(self.app, "merge", "-q", "--ff-only", "origin/main")  # the merge landed, then the session died
        gitdir = git(self.app, "rev-parse", "--absolute-git-dir")
        with open(os.path.join(gitdir, "sampo-update.json"), "w") as f:
            json.dump({"from": self.first, "to": self.head(), "status": "updating", "migrations": {}}, f)
        r = update.run(self.app)
        self.assertEqual(r["migrations"]["plugins"]["state"], "ran", r)
        self.assertEqual(calls, [True, False])
        self.fb.assert_called_once()
        self.assertIn("plugins ran", update.render(r))
        self.assertEqual(self.rec(gitdir)["status"], "applied")

    def test_a_migration_that_cannot_even_import_is_skipped_not_a_traceback(self):
        def plugins(undo=False, dry=False):
            raise ImportError("cannot import name 'newer_api' from 'sampo.core'")
        self.quiet(plugins=plugins)
        new = self.release()
        r = update.run(self.app)
        self.assertEqual(r["status"], "updated")
        self.assertEqual(self.head(), new)
        self.assertEqual(r["migrations"]["plugins"]["state"], "skipped")
        self.assertIn("newer_api", r["migrations"]["plugins"]["detail"])
        self.fb.assert_called_once()  # the fast check still ran
        with mock.patch.object(update, "_module", side_effect=ImportError("no module named homemove")):
            self.assertEqual(update._probe("home")["state"], "skipped")
            self.assertEqual(update._run_migration("home")["state"], "skipped")

    def test_a_commit_subject_from_origin_carries_no_escape_codes(self):
        self.quiet()
        self.release(msg="fix \x1b]0;pwned\x07 the \x1b[31mthing")
        r = update.run(self.app, check=True)
        text = update.render(r)
        for bad in ("\x1b", "\x07"):
            self.assertNotIn(bad, text)
            self.assertNotIn(bad, r["commits"][0]["subject"])
        self.assertIn("thing", text)

    def test_a_failed_update_keeps_the_earlier_updates_undo_handle(self):
        self.quiet()
        self.release()
        update.run(self.app)
        gitdir = git(self.app, "rev-parse", "--absolute-git-dir")
        before = self.rec(gitdir)
        self.release(version="1.2.0", text="N = 3\n", msg="third")
        real = update._git
        wrapped = lambda root, *a, **k: (1, "", "no room") if a[:1] == ("merge",) else real(root, *a, **k)  # noqa: E731
        with mock.patch.object(update, "_git", side_effect=wrapped):
            self.assertEqual(update.run(self.app)["status"], "refused")
        after = self.rec(gitdir)
        self.assertEqual(after, before)
        self.assertEqual(after["status"], "applied")

    def test_only_sampos_own_checkout_is_moved(self):
        self.quiet()
        self.release()
        other = os.path.join(self.tmp, "other")
        git(self.tmp, "clone", "-q", self.origin, other)
        git(other, "rm", "-q", "-r", "plugin")
        git(other, "commit", "-qm", "not sampo")
        why = update.run(other)["why"]
        self.assertIn("isn't Sampo", why)
        git(other, "reset", "-q", "--hard", "origin/main")
        put(other, "plugin/.claude-plugin/plugin.json", json.dumps({"name": "something-else"}))
        git(other, "add", "-A")
        git(other, "commit", "-qm", "other plugin")
        self.assertIn("isn't Sampo", update.run(other, check=True)["why"])
        self.assertIn("isn't Sampo", update.run(other, undo=True)["why"])
        # the default root: an enclosing repo is not the install itself
        inner = os.path.join(self.app, "plugin", "lib")
        self.assertIn("inside another repo", update.run(inner, own=inner)["why"])
        self.assertEqual(update.run(self.app, check=True, own=self.app)["status"], "plan")

    def test_a_long_line_from_origin_is_cut_with_a_visible_mark(self):
        """T-3881 repo-shape: the 300-character cut of origin's words ends in an ellipsis, a short line is whole."""
        cut = update._line("word " * 200)
        self.assertEqual((len(cut), cut[-1]), (300, "…"))
        self.assertEqual(update._line("a\n  b\tc"), "a b c")
