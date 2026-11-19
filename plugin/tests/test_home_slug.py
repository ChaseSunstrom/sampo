"""T-5060 (slice of T-3854): the home is ~/.claude/sampo when it exists, else ~/.claude/foreman (FOREMAN_HOME still wins),
and a project whose root moved keeps its slug through an alias record, so its tasks, state and lanes stay."""
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c


class HomeNames(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.user = os.path.join(self.tmp, "user")
        os.makedirs(self.user)
        env = mock.patch.dict(os.environ, {"HOME": self.user})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FOREMAN_HOME", None)
        os.environ.pop("SAMPO_HOME", None)

    def folder(self, name):
        path = os.path.join(self.user, ".claude", name)
        os.makedirs(path)
        return path

    def test_neither_folder_means_the_foreman_one_as_before(self):
        self.assertEqual(c.foreman_home(), os.path.join(self.user, ".claude", "foreman"))

    def test_only_the_old_folder_is_the_home(self):
        old = self.folder("foreman")
        self.assertEqual(c.foreman_home(), old)

    def test_a_stray_sampo_folder_beside_a_real_foreman_one_is_no_home(self):
        """A mkdir of ~/.claude/sampo must not hide the real home (its state and its core code)."""
        old = self.folder("foreman")
        self.folder("sampo")
        self.assertEqual(c.foreman_home(), old)
        self.assertEqual(c.state_dir(), os.path.join(old, "state"))

    def test_the_old_name_a_link_to_the_sampo_folder_is_the_sampo_home(self):
        """What the move leaves behind: sampo is the folder, foreman a link to it."""
        new = self.folder("sampo")
        os.symlink(new, os.path.join(self.user, ".claude", "foreman"))
        self.assertEqual(c.foreman_home(), new)
        self.assertEqual(c.state_dir(), os.path.join(new, "state"))

    def test_a_dangling_old_link_beside_the_sampo_folder_is_the_sampo_home(self):
        new = self.folder("sampo")
        os.symlink(os.path.join(self.tmp, "gone"), os.path.join(self.user, ".claude", "foreman"))
        self.assertEqual(c.foreman_home(), new)

    def test_the_sampo_folder_alone_is_the_home_once_the_old_name_is_gone(self):
        new = self.folder("sampo")
        self.assertEqual(c.foreman_home(), new)

    def test_a_file_named_sampo_is_no_home(self):
        old = self.folder("foreman")
        with open(os.path.join(self.user, ".claude", "sampo"), "w") as f:
            f.write("x")
        self.assertEqual(c.foreman_home(), old)

    def test_the_hooks_and_the_saved_interpreter_resolve_the_home_as_core_does(self):
        """compact and statusline are stdlib-only (no core import) and py runs before core: each mirrors the rule, and
        this pins every mirror equal to core.foreman_home over each folder combination and the env."""
        import importlib.util
        from importlib.machinery import SourceFileLoader
        from sampo import py
        hooks = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")
        mods = []
        for name in ("compact", "statusline"):
            loader = SourceFileLoader(f"hook_{name}", os.path.join(hooks, name))
            mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
            loader.exec_module(mod)
            mods.append(mod)
        claude = os.path.join(self.user, ".claude")

        def check(label):
            want = c.foreman_home()
            for mod in mods:
                self.assertEqual(mod.home_dir(), want, f"{label}: {mod.__name__}")
            self.assertEqual(py.saved_path(), os.path.join(want, "state", "python"), label)
            return want

        self.assertEqual(check("neither"), os.path.join(claude, "foreman"))
        os.makedirs(os.path.join(claude, "foreman"))
        os.makedirs(os.path.join(claude, "sampo"))
        self.assertEqual(check("both real"), os.path.join(claude, "foreman"))
        shutil.rmtree(os.path.join(claude, "foreman"))
        self.assertEqual(check("sampo alone"), os.path.join(claude, "sampo"))
        os.symlink(os.path.join(claude, "sampo"), os.path.join(claude, "foreman"))
        self.assertEqual(check("foreman a link to sampo"), os.path.join(claude, "sampo"))
        elsewhere = os.path.join(self.tmp, "elsewhere")
        with mock.patch.dict(os.environ, {"FOREMAN_HOME": elsewhere}):
            self.assertEqual(check("env"), elsewhere)

    def test_the_env_names_still_win_over_both_folders(self):
        self.folder("foreman")
        self.folder("sampo")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        with mock.patch.dict(os.environ, {"FOREMAN_HOME": elsewhere}):
            self.assertEqual(c.foreman_home(), elsewhere)

    def test_state_fallbacks_name_sampo_first_and_keep_the_foreman_names(self):
        xdg = os.path.join(self.tmp, "xdg")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": xdg}):
            names = c.state_fallbacks()
        uid = os.getuid()
        self.assertEqual([os.path.basename(n) for n in names[:2]], ["sampo", "foreman"])
        self.assertEqual(os.path.dirname(names[0]), xdg)
        tmp = [os.path.basename(n) for n in names[2:]]
        self.assertEqual(tmp, [f"sampo-state-{uid}", f"foreman-state-{uid}"])

    def test_a_restore_removes_the_marker_of_every_fallback_that_names_the_default(self):
        """sampo's and the old name's fallbacks may both carry a marker: leaving one sends the next call back out."""
        old = self.folder("foreman")
        default = os.path.join(old, "state")
        xdg = os.path.join(self.tmp, "xdg")
        alts = [os.path.join(xdg, "sampo"), os.path.join(xdg, "foreman")]
        for alt in alts:
            os.makedirs(alt, mode=0o700)
            with open(os.path.join(alt, ".foreman-state.json"), "w") as f:
                json.dump({"default": default}, f)
            os.chmod(os.path.join(alt, ".foreman-state.json"), 0o600)
        with open(os.path.join(alts[0], "carried.json"), "w") as f:
            f.write("{}")
        env = {k: v for k, v in os.environ.items() if k != "FOREMAN_STATE"}
        with mock.patch.dict(os.environ, {**env, "XDG_STATE_HOME": xdg}, clear=True):
            self.assertEqual(c.state_dir(), alts[0])
            self.assertEqual(c.restore_default_state(), default)
            self.assertEqual(c.state_dir(), default)
        for alt in alts:
            self.assertFalse(os.path.exists(os.path.join(alt, ".foreman-state.json")), alt)
        self.assertTrue(os.path.exists(os.path.join(default, "carried.json")))

    def test_a_marker_in_the_old_fallback_still_carries_the_old_state(self):
        """An install whose state moved to ~/.local/state/foreman keeps following it."""
        old = self.folder("foreman")
        default = os.path.join(old, "state")
        alt = os.path.join(self.user, ".local", "state", "foreman")
        os.makedirs(alt, mode=0o700)
        with open(os.path.join(alt, ".foreman-state.json"), "w") as f:
            json.dump({"default": default}, f)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": ""}):
            self.assertEqual(c.state_dir(), alt)


class SlugPinned(ForemanTestCase):
    def moved(self):
        """A second checkout of the project, at another path."""
        new = os.path.join(self.tmp, "elsewhere", "app")
        os.makedirs(os.path.dirname(new))
        shutil.copytree(self.repo, new, symlinks=True)
        return os.path.realpath(new)

    def test_a_slug_is_the_name_and_hash_it_always_was(self):
        self.assertEqual(c.slug_for("/work/My App/"), "my-app-" + c.sha1(b"/work/My App/").hexdigest()[:6])
        self.assertEqual(c.slug_for("/x"), "x-" + c.sha1(b"/x").hexdigest()[:6])
        self.assertFalse(os.path.exists(os.path.join(self.home, c.SLUG_ALIASES)), "reading writes nothing")

    def test_a_moved_root_keeps_its_slug_and_its_state(self):
        p = c.init_project(self.repo)
        with open(os.path.join(p.dir, "marker.txt"), "w") as f:
            f.write("state")
        new = self.moved()
        self.assertNotEqual(c.slug_for(new), p.slug, "unpinned, a new path is a new project")
        self.assertEqual(c.pin_slug(self.repo, new), p.slug)
        self.assertEqual(c.slug_for(new), p.slug)
        q = c.find_project(new)
        self.assertEqual((q.slug, q.dir), (p.slug, p.dir))
        self.assertTrue(os.path.exists(os.path.join(q.dir, "marker.txt")))
        self.assertEqual(c.slug_for(self.repo), p.slug, "the old path still answers as it did")

    def test_the_record_lives_under_the_home_and_names_the_new_root(self):
        p = c.init_project(self.repo)
        new = self.moved()
        c.pin_slug(self.repo, new)
        with open(os.path.join(c.foreman_home(), c.SLUG_ALIASES)) as f:
            self.assertEqual(json.load(f), {new: p.slug})

    def test_init_at_the_new_root_updates_the_path_not_the_project(self):
        p = c.init_project(self.repo)
        new = self.moved()
        c.pin_slug(self.repo, new)
        q = c.init_project(new)
        self.assertEqual(q.slug, p.slug)
        self.assertEqual(c.read_meta(q)["path"], new)
        self.assertEqual([x.slug for x, _ in c.all_projects()], [p.slug])

    def test_a_pin_follows_through_a_second_move(self):
        p = c.init_project(self.repo)
        new = self.moved()
        c.pin_slug(self.repo, new)
        third = os.path.join(self.tmp, "third", "app")
        os.makedirs(third)
        c.pin_slug(new, os.path.realpath(third))
        self.assertEqual(c.slug_for(os.path.realpath(third)), p.slug)

    def test_a_move_back_drops_the_record(self):
        p = c.init_project(self.repo)
        new = self.moved()
        c.pin_slug(self.repo, new)
        c.pin_slug(new, self.repo)
        self.assertEqual(c.slug_for(self.repo), p.slug)
        with open(os.path.join(c.foreman_home(), c.SLUG_ALIASES)) as f:
            self.assertEqual(json.load(f), {new: p.slug}, "the original root needs none; the one left is not dropped")

    def test_a_pin_never_hides_a_project_already_at_the_new_root(self):
        c.init_project(self.repo)
        new = self.moved()
        theirs = c.init_project(new)
        with self.assertRaises(ValueError):
            c.pin_slug(self.repo, new)
        self.assertEqual(c.slug_for(new), theirs.slug)

    def test_a_pin_needs_a_project_to_keep(self):
        with self.assertRaises(ValueError):
            c.pin_slug(self.repo, self.moved())

    def test_a_bad_record_is_ignored_and_never_names_a_path_outside_projects(self):
        new = self.moved()
        natural = c.slug_for(new)
        path = os.path.join(c.foreman_home(), c.SLUG_ALIASES)
        for text in ("{nope", "[1]", json.dumps({new: "../../etc"}), json.dumps({new: ""}), json.dumps({new: 5}),
                     json.dumps({new: "a/b"}), json.dumps({new: "x" * 300})):
            with self.subTest(text):
                with open(path, "w") as f:
                    f.write(text)
                self.assertEqual(c.slug_for(new), natural)

    def test_the_lanes_cargo_base_follows_the_pinned_slug(self):
        p = c.init_project(self.repo)
        new = self.moved()
        c.pin_slug(self.repo, new)
        self.assertEqual(c.cargo_lanes_base(c.slug_for(new)), c.cargo_lanes_base(p.slug))

    def test_a_linked_worktree_of_a_moved_checkout_finds_the_projects_state(self):
        p = c.init_project(self.repo)
        new = self.moved()
        c.pin_slug(self.repo, new)
        lane = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", new, "worktree", "add", "-q", "-b", "lane", lane], check=True, capture_output=True)
        q = c.find_project(lane)
        self.assertEqual((q.slug, q.dir), (p.slug, p.dir))


if __name__ == "__main__":
    unittest.main()
