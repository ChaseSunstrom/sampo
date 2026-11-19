"""T-4717, T-4764: the disk of a Rust project driven by Sampo kept filling (2026-11-15: seven builder worktrees held 36 GB of
their own target/ folders, the shared cargo cache 35 GB, the project's own target 61 GB; free space fell about 10 GB an
hour). T-4575 gave Sampo's own commands one shared CARGO_TARGET_DIR, but two lanes sharing one folder ran each other's
compiled crates (cargo issue 12516), so T-4764 gave every lane a folder of its own under Sampo's cache
(cache/cargo-lanes/<slug>/<lane>). Now: (1) a lane's cargo folder goes with its lane (a landed lane, `fm lane rm`, tidy
of a finished lane), by exact path, never a pattern, never a link, never while a process holds it or a cargo build holds
its lock; (2) the lane folders have a cap (10% of the disk, at most 40 GB, SAMPO_CARGO_CAP_BYTES overrides) and
`sampo tidy --apply` prunes whole folders oldest first, never one used in the last hour, held by a live process (or by
one working in its lane) or below a held .cargo-lock, never outside the cache; (3) the old shared folder is retired
(tidy removes it); a folder whose lane is gone is removed after an hour; (4) a lane's own marked .cargo/config.toml
(written only where git already ignores it) points a builder's bare cargo at the lane's folder; T-4717's marked file in
the lanes' parent folder is removed; (5) doctor and tidy name these folders with their sizes; (6) SAMPO_CARGO_SHARED=0
turns it off.

Safety: every folder is the test's own temp folder with fake files (no cargo runs, no real Sampo state, no real project);
the machine's CARGO_* variables and ~/.cargo are never read (patched to scratch ones); a prune is only ever pointed at a
scratch cache."""
import fcntl
import json
import os
import subprocess
import time
import unittest.mock

from test_disk_pressure import DiskCase, Usage, git, DAY
from test_lane_cargo_target import alone, write

from sampo import core as c
from sampo import doctor as d
from sampo import lanes

VAR = "CARGO_TARGET_DIR"
CAP = "SAMPO_CARGO_CAP_BYTES"
SHARED = "SAMPO_CARGO_SHARED"
HOUR = 3600
BLOCK = 8192  # fake files are whole blocks, so their sizes add up exactly
TAG = "Signature: 8a477f597d28d172789f06886806bc55\n# This file is a cache directory tag created by cargo.\n"


class RustCase(DiskCase):
    def setUp(self):
        super().setUp()
        alone(self)
        write(os.path.join(self.repo, "Cargo.toml"), '[package]\nname = "fixture"\n')
        write(os.path.join(self.repo, ".gitignore"), "target/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "rust")
        self.slug = self.p.slug
        self.old = c.cargo_shared_dir(self.slug)  # T-4575's folder, retired by T-4764
        self.base = c.cargo_lanes_base(self.slug)
        self.low = lambda _: Usage(900e9, 893e9, 7e9)
        self.parent_config = os.path.join(self.lane_root, ".cargo", "config.toml")

    def fake(self, path, hours_old=0.0, blocks=1):
        """A fake build file of `blocks` * 8 KB whose newest use was `hours_old` hours ago."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\0" * BLOCK * blocks)
        then = time.time() - hours_old * HOUR
        os.utime(path, (then, then))
        return path

    def cfolder(self, name, hours_old, blocks=1, lane=None, marked=True):
        """The cargo folder of the lane `name` (a lane path that need not exist: `lane` names another), last used
        `hours_old` hours ago; its marker file is as old."""
        lane = lane or os.path.join(self.lane_root, name)
        path = c.cargo_lane_dir(self.slug, lane)
        self.fake(os.path.join(path, "debug", "deps", "x.rlib"), hours_old, blocks)
        if marked:
            mark = os.path.join(path, c.CARGO_LANE_MARK)
            write(mark, os.path.realpath(lane) + "\n")
            then = time.time() - hours_old * HOUR
            os.utime(mark, (then, then))
        return path

    def kept(self, name, hours_old, blocks=1):
        """A cargo folder whose lane is there (a plain folder: nothing here asks git), so no orphan rule takes it."""
        lane = os.path.join(self.lane_root, name)
        os.makedirs(lane, exist_ok=True)
        return self.cfolder(name, hours_old, blocks, lane=lane)

    def old_entry(self, rel="debug/deps/x.rlib", hours_old=50, blocks=2):
        return self.fake(os.path.join(self.old, *rel.split("/")), hours_old, blocks)

    def target(self, lane, blocks=2, tagged=True):
        """A lane's own target/ folder: a fake build file, and cargo's CACHEDIR.TAG when `tagged`."""
        big = self.fake(os.path.join(lane, "target", "debug", "deps", "big.rlib"), 0, blocks)
        if tagged:
            write(os.path.join(lane, "target", "CACHEDIR.TAG"), TAG)
        return big

    def disk(self, folder):
        """What the filesystem allocates below `folder` (not the folder's own block): every file and folder under it,
        read by this test's own walk. A folder costs 0 on tmpfs, 4096 on ext4, more elsewhere (T-4741: sizes a test
        asserts come from here, never from numbers that held on one machine's filesystem)."""
        total = 0
        for top, dirs, files in os.walk(folder):
            total += sum(os.lstat(os.path.join(top, n)).st_blocks * 512 for n in dirs + files)
        return total

    def sizes(self):
        """({folder name: bytes}, total) of the lane cargo folders, as the prune sizes them."""
        infos, total = lanes._lane_infos(lanes._lanes_root(self.slug), time.monotonic() + 30)
        return {os.path.basename(k): v[0] for k, v in infos.items()}, total

    def cap_leaving(self, *gone):
        """A cap that is exactly what the lane folders hold once the folders `gone` (paths) are removed, whatever the
        filesystem allocates for a folder."""
        sizes, total = self.sizes()
        return total - sum(sizes[os.path.basename(g)] for g in gone)

    def cap(self, n):
        patch = unittest.mock.patch.dict(os.environ, {CAP: str(n)})
        patch.start()
        self.addCleanup(patch.stop)

    def prune(self, **kw):
        kw.setdefault("held", [])  # no live process holds a fake folder
        return lanes.prune_cargo_cache(self.p, **kw)

    def removed(self, r):
        return [n for n, _ in r["removed"]]

    def names(self, *paths):
        return [os.path.basename(x) for x in paths]

    def sees_holders(self):
        """Whether this host can say what its processes hold: /proc (Linux) or lsof (macOS). A prune removes nothing
        without that view and prunes with it, so a test that runs a subprocess (where nothing can be mocked) expects
        by this, never by whether /proc is there (T-4792: macOS has lsof, and its prune ran)."""
        return lanes._held_paths() is not None


class TheLaneConfig(RustCase):
    """A builder's own cargo commands reach the lane's folder through the lane's own marked config, where git ignores it."""

    def setUp(self):
        super().setUp()
        self.ignore("target/\n.cargo/config.toml\n")

    def ignore(self, text):
        write(os.path.join(self.repo, ".gitignore"), text)
        git(self.repo, "commit", "-qam", "ignore")

    def cfg(self, lane):
        return os.path.join(os.path.realpath(lane), ".cargo", "config.toml")

    def test_a_lane_gets_one_marked_config_naming_its_own_folder_where_git_ignores_it(self):
        lane = self.lane("T-5001", old=False)
        path = c.lane_cargo_config(lane)
        self.assertEqual(path, self.cfg(lane))
        self.assertEqual(c._cargo_config_dir(path), c.cargo_lane_dir(self.slug, lane))
        self.assertTrue(c.cargo_config_ours(path))
        self.assertEqual(c.lane_cargo_config(lane), path, "a second call finds it in place")
        self.assertTrue(os.path.isfile(os.path.join(c.cargo_lane_dir(self.slug, lane), c.CARGO_LANE_MARK)),
                        "the folder carries its lane's path, so tidy can tell when the lane is gone")

    def test_a_second_run_writes_nothing(self):
        lane = self.lane("T-5011", old=False)
        path = c.lane_cargo_config(lane)
        os.utime(path, (1_000_000_000, 1_000_000_000))
        c.lane_cargo_config(lane)
        self.assertEqual(os.stat(path).st_mtime, 1_000_000_000)

    def test_two_lanes_get_two_files_naming_two_folders(self):
        a, b = self.lane("T-5012", old=False), self.lane("T-5013", old=False)
        pa, pb = c.lane_cargo_config(a), c.lane_cargo_config(b)
        self.assertNotEqual(pa, pb)
        self.assertNotEqual(c._cargo_config_dir(pa), c._cargo_config_dir(pb))

    def test_git_sees_nothing_and_its_exclude_file_is_not_touched(self):
        exclude = os.path.join(self.repo, ".git", "info", "exclude")

        def text():
            if not os.path.exists(exclude):
                return None
            with open(exclude, encoding="utf-8") as f:
                return f.read()
        before = text()
        lane = self.lane("T-5002", old=False)
        self.assertTrue(c.lane_cargo_config(lane))
        self.assertEqual(git(lane, "status", "--porcelain"), "")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(lanes._left_work(lane), [], "Sampo's own file is no work the lane would lose")
        self.assertEqual(text(), before)

    def test_a_users_own_ignored_config_is_still_work_a_lane_would_lose(self):
        lane = self.lane("T-5009", old=False)
        write(os.path.join(lane, ".cargo", "config.toml"), "[term]\nverbose = true\n")
        self.assertEqual(lanes._left_work(lane), ["!! .cargo/config.toml"])

    def test_a_file_git_would_list_is_not_written(self):
        self.ignore("target/\n")
        lane = self.lane("T-5003", old=False)
        self.assertEqual(c.lane_cargo_config(lane), "", "env-only fallback: the focus line's export")
        self.assertFalse(os.path.exists(os.path.join(lane, ".cargo")))
        self.assertEqual(git(lane, "status", "--porcelain"), "")

    def test_a_file_git_would_list_is_named_with_the_exact_gitignore_line_at_focus_and_in_the_finding(self):
        self.ignore("target/\n")
        lane = self.lane("T-5030", old=False)
        hint = c.cargo_ignore_hint(lane)
        self.assertIn("`.cargo/config.toml`", hint)
        self.assertIn(".gitignore", hint)
        self.assertIn("./target", hint)
        self.assertIn(c.cargo_lane_dir(self.slug, lane), hint, "the export that does work meanwhile")
        self.assertIn(hint, c.cargo_focus_line(lane, self.slug))
        self.assertFalse(os.path.exists(os.path.join(lane, ".cargo")), "naming it writes nothing")
        self.target(lane)
        from sampo import tidy
        rows = [f for f in lanes.cargo_cache_findings(self.p, False, [], tidy.finding) if f["kind"] == "lane_target"]
        self.assertEqual(len(rows), 1)
        self.assertIn(hint, rows[0]["detail"])

    def test_no_hint_when_the_file_is_in_place_or_the_cause_is_not_git(self):
        lane = self.lane("T-5031", old=False)
        c.lane_cargo_config(lane)
        self.assertEqual(c.cargo_ignore_hint(lane), "")
        self.assertNotIn(".gitignore", c.cargo_focus_line(lane, self.slug))
        user = self.lane("T-5032", old=False)
        write(os.path.join(user, ".cargo", "config.toml"), "[term]\nverbose = true\n")
        self.assertEqual(c.cargo_ignore_hint(user), "", "the user's own file is another cause")
        self.assertEqual(c.cargo_ignore_hint(self.repo), "", "the main checkout")

    def test_the_lane_still_names_its_folder_with_the_file_in_place(self):
        lane = self.lane("T-5019", old=False)
        c.lane_cargo_config(lane)
        self.assertEqual(c.cargo_target(lane, self.slug), (c.cargo_lane_dir(self.slug, lane), ""),
                         "Sampo's own file is not the user's choice")

    def test_a_lane_without_rust_gets_nothing(self):
        lane = self.lane("T-5004", old=False)
        os.unlink(os.path.join(lane, "Cargo.toml"))
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertFalse(os.path.exists(os.path.join(lane, ".cargo")))

    def test_the_main_checkout_is_never_given_one(self):
        self.assertEqual(c.lane_cargo_config(self.repo), "")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".cargo")))

    def test_a_folder_that_is_not_a_sampo_lane_is_left_alone(self):
        for where in (os.path.join(self.tmp, "stray-worktree"), os.path.join(self.repo, "inside-main")):
            git(self.repo, "worktree", "add", "-q", "-b", os.path.basename(where), where)
            self.assertEqual(c.lane_cargo_config(where), "", "not under <repo>.lanes/ or <repo>/.claude/worktrees/")
            self.assertFalse(os.path.exists(os.path.join(where, ".cargo")))

    def test_the_users_choice_stands_and_nothing_is_written(self):
        lane = self.lane("T-5005", old=False)
        with unittest.mock.patch.dict(os.environ, {VAR: "/mine/t"}):
            self.assertEqual(c.lane_cargo_config(lane), "")
        write(os.path.join(self.repo, ".cargo", "config.toml"), '[build]\ntarget-dir = "/mine/cfg"\n')
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertFalse(os.path.exists(self.cfg(lane)))

    def test_a_choice_in_a_folder_above_the_main_checkout_or_in_the_lanes_parent_stands_too(self):
        lane = self.lane("T-5014", old=False)
        write(os.path.join(self.tmp, ".cargo", "config.toml"), '[build]\ntarget-dir = "/mine/above"\n')
        target, why = c.cargo_target(lane, self.slug)
        self.assertIsNone(target)
        self.assertIn("/mine/above", why)
        self.assertEqual(c.lane_cargo_config(lane), "")
        os.unlink(os.path.join(self.tmp, ".cargo", "config.toml"))
        write(self.parent_config, '[build]\ntarget-dir = "/mine/parent"\n')  # the user's own, no Sampo mark
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertFalse(os.path.exists(self.cfg(lane)))
        self.assertTrue(os.path.exists(self.parent_config), "the user's file is never removed")

    def test_a_choice_made_after_our_file_was_written_removes_the_file(self):
        lane = self.lane("T-5015", old=False)
        c.lane_cargo_config(lane)
        write(os.path.join(self.repo, ".cargo", "config.toml"), '[build]\ntarget-dir = "/mine/later"\n')
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertFalse(os.path.exists(self.cfg(lane)), "Sampo's file must not outrank the user's choice")

    def test_something_of_the_users_in_the_lanes_cargo_folder_is_never_overwritten(self):
        lane = self.lane("T-5006", old=False)
        for name, text in (("config.toml", "[term]\nverbose = true\n"), ("config", "[term]\nquiet = true\n")):
            mine = os.path.join(lane, ".cargo", name)
            write(mine, text)
            self.assertEqual(c.lane_cargo_config(lane), "", "env-only fallback: the focus line's export")
            with open(mine, encoding="utf-8") as f:
                self.assertEqual(f.read(), text)
            os.unlink(mine)

    def test_a_linked_cargo_folder_is_left_alone(self):
        lane = self.lane("T-5016", old=False)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(lane, ".cargo"))
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_path_with_a_quote_or_a_line_break_is_refused_and_a_backslash_is_kept(self):
        lane = self.lane("T-5017", old=False)
        for bad in ("it's", "two\nlines", "cr\rhere"):
            with unittest.mock.patch.object(c, "cargo_target", return_value=(os.path.join(self.tmp, bad), "")):
                self.assertEqual(c.lane_cargo_config(lane), "", repr(bad))
            self.assertFalse(os.path.exists(self.cfg(lane)))
        odd = os.path.join(self.tmp, "back\\slash")
        with unittest.mock.patch.object(c, "cargo_target", return_value=(odd, "")):
            path = c.lane_cargo_config(lane)
        self.assertEqual(c._cargo_config_dir(path), odd, "a TOML literal string takes a backslash as it is")

    def test_the_off_switch_turns_it_off_and_takes_our_file_away(self):
        lane = self.lane("T-5018", old=False)
        c.lane_cargo_config(lane)
        for name in ("FOREMAN_CARGO_SHARED", SHARED):  # mirror_env moves the SAMPO_ name to the FOREMAN_ one at start
            with unittest.mock.patch.dict(os.environ, {name: "0"}):
                self.assertEqual(c.cargo_target(lane, self.slug)[0], None, name)
                self.assertIn("SAMPO_CARGO_SHARED=0", c.cargo_target(lane, self.slug)[1])
                self.assertEqual(c.lane_cargo_config(lane), "", name)
                self.assertFalse(os.path.exists(self.cfg(lane)), name)
            c.lane_cargo_config(lane)  # back on
            self.assertTrue(os.path.exists(self.cfg(lane)))
        with unittest.mock.patch.dict(os.environ, {SHARED: "1"}):
            self.assertEqual(c.cargo_target(lane, self.slug)[0], c.cargo_lane_dir(self.slug, lane), "only 0 turns it off")

    def test_a_lane_under_claude_worktrees_gets_one_too(self):
        home = os.path.join(self.repo, ".claude", "worktrees")
        lane = self.lane("T-5020", old=False, parent=home)
        path = c.lane_cargo_config(lane)
        self.assertEqual(path, self.cfg(lane))
        self.assertEqual(git(lane, "status", "--porcelain"), "")

    def test_sccache_on_the_path_becomes_the_rustc_wrapper_and_a_users_wrapper_wins(self):
        lane = self.lane("T-5021", old=False)
        fake = os.path.join(self.tmp, "bin", "sccache")
        write(fake, "#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)
        env = dict(os.environ, PATH=os.path.dirname(fake) + os.pathsep + os.environ["PATH"])
        path = c.lane_cargo_config(lane, env)
        self.assertEqual(c._cargo_config_build(path).get("rustc-wrapper"), fake)
        c.lane_cargo_config(lane, dict(os.environ))  # sccache is gone from the PATH: the file follows
        self.assertNotIn("rustc-wrapper", c._cargo_config_build(path))
        c.lane_cargo_config(lane, {**env, "RUSTC_WRAPPER": "mine"})
        self.assertNotIn("rustc-wrapper", c._cargo_config_build(path), "the user's wrapper wins")

    def test_t4717s_marked_file_in_the_lanes_parent_folder_goes_and_the_users_stays(self):
        lane = self.lane("T-5022", old=False)
        write(self.parent_config, f"{c.CARGO_MARK_OLD}\n[build]\ntarget-dir = '{self.old}'\n")
        c.lane_cargo_config(lane)
        self.assertFalse(os.path.exists(self.parent_config), "it named the one shared folder")
        self.assertFalse(os.path.exists(os.path.dirname(self.parent_config)), "the empty .cargo folder goes too")
        write(self.parent_config, "[term]\nverbose = true\n")
        c.lane_cargo_config(lane)
        self.assertTrue(os.path.exists(self.parent_config), "an unmarked file is the user's")

    def test_a_builders_bash_calls_write_and_spawn_nothing(self):
        lane = self.lane("T-5008", old=False)
        r = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": lane})
        self.assertEqual(r.returncode, 0, r.stderr)  # (the hook runs in another process: the file check is the proof)
        self.assertFalse(os.path.exists(self.cfg(lane)), "the hook leaves the config to the lane's creation and focus")

    def test_a_lane_made_by_fm_gets_it(self):
        tid = json.loads(self.fm("task", "new", "Rust work", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        self.fm("lane", "new", tid)
        lane = os.path.join(self.lane_root, tid)
        self.assertEqual(c._cargo_config_dir(self.cfg(lane)), c.cargo_lane_dir(self.slug, lane))
        self.assertFalse(os.path.exists(self.parent_config), "none in the lanes' parent folder any more")


class TheCap(RustCase):
    def test_default_is_a_tenth_of_the_disk_and_at_most_forty_gb(self):
        self.assertEqual(c.cargo_cap_bytes(total=100e9, environ={}), 10e9)
        self.assertEqual(c.cargo_cap_bytes(total=900e9, environ={}), 40e9)
        self.assertEqual(c.cargo_cap_bytes(total=400e9, environ={}), 40e9)

    def test_the_environment_overrides_it_and_a_bad_value_does_not(self):
        self.assertEqual(c.cargo_cap_bytes(total=900e9, environ={CAP: "5000000"}), 5000000)
        for bad in ("", "x", "-3", "0"):
            self.assertEqual(c.cargo_cap_bytes(total=900e9, environ={CAP: bad}), 40e9, bad)


class ThePrune(RustCase):
    def test_the_oldest_folders_go_first_until_the_lane_folders_are_under_their_cap(self):
        paths = [self.cfolder(name, age) for name, age in (("a-1", 30), ("b-2", 20), ("c-3", 10), ("d-4", 5))]
        self.cap(self.cap_leaving(paths[0], paths[1]))
        r = self.prune()
        self.assertEqual(sorted(os.listdir(self.base)), sorted(self.names(paths[2], paths[3])), "the two oldest went")
        self.assertEqual(self.removed(r), self.names(paths[0], paths[1]), "oldest first")
        self.assertLessEqual(r["after"], r["cap"])
        self.assertGreater(r["before"], r["after"])

    def test_a_folder_goes_whole_or_not_at_all(self):
        a = self.cfolder("a-1", 30, blocks=3)
        self.fake(os.path.join(a, "debug", "incremental", "x", "s.bin"), 30)
        self.cap(1)
        self.prune()
        self.assertFalse(os.path.lexists(a))

    def test_a_folder_with_no_marker_is_pruned_by_the_cap_like_the_rest(self):
        a = self.cfolder("a-1", 30, marked=False)
        self.cap(1)
        self.assertEqual(self.removed(self.prune()), self.names(a))

    def test_nothing_goes_when_it_is_under_its_cap(self):
        a = self.cfolder("a-1", 30)
        self.cap(100 * BLOCK)
        self.assertEqual(self.prune()["removed"], [])
        self.assertTrue(os.path.isdir(a))

    def test_a_dry_run_names_what_would_go_and_removes_nothing(self):
        a = self.cfolder("a-1", 30)
        self.cap(1)
        r = self.prune(dry=True)
        self.assertEqual(self.removed(r), self.names(a))
        self.assertTrue(os.path.isdir(a))

    def test_a_folder_used_in_the_last_hour_is_never_removed(self):
        old, fresh = self.cfolder("old", 5), self.cfolder("fresh", 0.2)
        self.cap(1)
        r = self.prune()
        self.assertEqual(self.removed(r), self.names(old))
        self.assertTrue(os.path.isdir(fresh))
        self.assertGreater(r["after"], r["cap"], "over its cap, and said so: nothing else was safe to remove")

    def test_reading_the_marker_does_not_make_a_folder_look_used(self):
        a = self.cfolder("read", 50)
        for _ in range(2):  # relatime sets a read file's atime to now: every run and every scan reads the marker
            lanes._lane_marker(a)
            c.cargo_mark_lane(a, os.path.join(self.lane_root, "read"))
        self.cap(1)
        self.assertEqual(self.removed(self.prune()), self.names(a))

    def test_a_new_file_deep_in_an_old_folder_keeps_the_whole_folder(self):
        a = self.cfolder("mixed", 40)
        self.fake(os.path.join(a, "debug", "sub", "new.bin"), 0.1)
        self.cap(1)
        self.assertEqual(self.prune()["removed"], [])
        self.assertTrue(os.path.isdir(a))

    def test_a_folder_a_live_process_holds_is_never_removed(self):
        held, free = self.cfolder("held", 50), self.cfolder("free", 40)
        self.cap(1)
        r = self.prune(held=[os.path.join(held, "debug", "deps", "x.rlib")])
        self.assertEqual(self.removed(r), self.names(free))
        self.assertTrue(os.path.isdir(held))
        self.assertEqual(r["skipped"], [(os.path.basename(held), "a live process holds it")])

    def test_a_process_standing_in_the_folder_holds_it(self):
        a = self.cfolder("cwd", 50)
        self.cap(1)
        self.assertEqual(self.prune(held=[os.path.join(a, "debug")])["removed"], [])

    def test_a_process_working_in_the_lane_holds_its_folder(self):
        lane = os.path.join(self.lane_root, "busy")
        a = self.cfolder("busy", 50)
        self.cap(1)
        for where in (lane, os.path.join(lane, "src", "deep")):
            r = self.prune(held=[where])
            self.assertEqual(r["removed"], [], where)
            self.assertEqual(r["skipped"], [(os.path.basename(a), "a live process works in its lane")])
        self.assertTrue(os.path.isdir(a))

    def lock(self, path):
        """A cargo build's lock: a file some other open description holds an exclusive flock on."""
        self.fake(path, 80)
        fd = os.open(path, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return path

    def test_a_folder_whose_cargo_lock_is_held_is_skipped(self):
        locked, free = self.cfolder("locked", 70), self.cfolder("free", 60)
        self.lock(os.path.join(locked, "debug", ".cargo-lock"))
        self.cap(1)
        r = self.prune()
        self.assertEqual(self.removed(r), self.names(free))
        self.assertTrue(os.path.isfile(os.path.join(locked, "debug", ".cargo-lock")))
        self.assertTrue(any("cargo build" in why for _, why in r["skipped"]), r)

    def test_a_lock_below_a_target_triple_folder_keeps_the_folder(self):
        a = self.cfolder("triple", 70)
        self.lock(os.path.join(a, "x86_64-unknown-linux-gnu", "debug", ".cargo-lock"))
        self.cap(1)
        self.assertEqual(self.prune()["removed"], [])
        self.assertTrue(os.path.isdir(a))

    def test_a_lock_that_cannot_be_read_counts_as_held(self):
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        a = self.cfolder("unreadable", 70)
        os.chmod(self.fake(os.path.join(a, "debug", ".cargo-lock"), 80), 0)
        self.cap(1)
        self.assertEqual(self.prune()["removed"], [])
        self.assertTrue(os.path.isdir(a))

    def test_a_free_lock_file_is_no_reason_to_keep_a_folder(self):
        a = self.cfolder("free", 70)
        self.fake(os.path.join(a, "debug", ".cargo-lock"), 80)
        self.cap(1)
        self.assertTrue(self.prune()["removed"])
        self.assertFalse(os.path.exists(a))

    def test_the_holders_are_read_again_before_each_removal(self):
        a, b = self.cfolder("a", 50), self.cfolder("b", 40)
        calls = []

        def holders():  # a build starts in b after the snapshot taken for the first removal
            calls.append(1)
            return [] if len(calls) <= 1 else [os.path.join(b, "debug")]
        self.cap(1)
        with unittest.mock.patch.object(lanes, "_held_paths", holders):
            r = lanes.prune_cargo_cache(self.p)
        self.assertEqual(len(calls), 2, "one snapshot, one more before the second removal")
        self.assertEqual(self.removed(r), self.names(a))
        self.assertTrue(os.path.isdir(b))

    def test_without_a_view_of_open_files_nothing_is_removed(self):
        a = self.cfolder("a", 50)
        self.cap(1)
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=None):
            r = lanes.prune_cargo_cache(self.p)
        self.assertEqual(r["removed"], [])
        self.assertIn("open files", r["why"])
        self.assertTrue(os.path.isdir(a))

    def test_the_view_vanishing_mid_pass_stops_it(self):
        a, b = self.cfolder("a", 50), self.cfolder("b", 40)
        views = iter([[], None])
        self.cap(1)
        with unittest.mock.patch.object(lanes, "_held_paths", lambda: next(views)):
            r = lanes.prune_cargo_cache(self.p)
        self.assertEqual(self.removed(r), self.names(a))
        self.assertTrue(os.path.isdir(b))
        self.assertIn("open files", r["why"])

    def test_a_link_is_never_followed_and_nothing_outside_the_cache_is_touched(self):
        outside = os.path.join(self.tmp, "precious")
        keep = self.fake(os.path.join(outside, "keep.bin"), 99)
        a = self.cfolder("real", 50)
        os.symlink(outside, os.path.join(a, "debug", "link"))  # a link inside a lane folder
        os.symlink(keep, os.path.join(a, "debug", "deps-link.rlib"))
        os.symlink(outside, os.path.join(self.base, "stray-0123456789"))  # a link where a lane folder would be
        self.cap(1)
        r = self.prune()
        self.assertTrue(os.path.isfile(keep), "a link in the cache points outside: never followed, never removed")
        self.assertEqual(self.removed(r), self.names(a), "the real folder went, links and all")
        self.assertTrue(os.path.islink(os.path.join(self.base, "stray-0123456789")), "the base's link is left as it is")

    def test_only_names_of_the_scheme_in_the_projects_own_folder_are_pruned(self):
        stray = self.fake(os.path.join(self.base, "notours", "deps", "x.rlib"), 90)
        plain = self.fake(os.path.join(self.base, "plain-file"), 90)
        other = self.fake(os.path.join(os.path.dirname(self.base), "other-slug",
                                       c.cargo_lane_name(os.path.join(self.tmp, "o")), "x.rlib"), 90)
        mine = self.cfolder("mine", 50)
        self.cap(1)
        r = self.prune()
        self.assertEqual(self.removed(r), self.names(mine))
        for kept in (stray, plain, other):
            self.assertTrue(os.path.isfile(kept), kept)

    def test_a_projects_own_target_and_a_lanes_files_are_never_touched(self):
        mine = self.fake(os.path.join(self.repo, "target", "debug", "deps", "x.rlib"), 500)
        lane = self.lane("T-5010", old=False)
        own = self.fake(os.path.join(lane, "target", "debug", "deps", "y.rlib"), 500)
        src = self.fake(os.path.join(lane, "src", "main.rs"), 500)
        old = self.old_entry()
        self.cfolder("T-5010", 50, lane=lane)
        self.cap(1)
        self.prune()
        for kept in (mine, own, src, old):
            self.assertTrue(os.path.isfile(kept), kept)

    def test_a_cache_folder_that_is_a_link_is_not_pruned(self):
        target = os.path.join(self.tmp, "elsewhere")
        name = c.cargo_lane_name(os.path.join(self.lane_root, "x"))
        keep = self.fake(os.path.join(target, name, "debug", "x.rlib"), 50)
        os.makedirs(os.path.dirname(self.base), exist_ok=True)
        os.symlink(target, self.base)
        self.cap(1)
        r = self.prune()
        self.assertEqual(r["removed"], [])
        self.assertIn("link", r["why"])
        self.assertTrue(os.path.isfile(keep))

    def test_a_missing_cache_is_nothing_to_do(self):
        self.cap(1)
        r = self.prune()
        self.assertEqual((r["removed"], r["before"]), ([], 0))

    def test_a_second_run_finds_it_done(self):
        paths = [self.cfolder(f"e{i}", 10 + i) for i in range(4)]
        self.cap(self.cap_leaving(paths[3], paths[2]))
        first = self.prune()
        again = self.prune()
        self.assertTrue(first["removed"])
        self.assertEqual(again["removed"], [])

    def test_a_pass_that_runs_out_of_time_removes_nothing_it_did_not_size(self):
        paths = [self.cfolder(f"e{i}", 10 + i) for i in range(3)]
        self.cap(1)
        r = self.prune(budget=-1)
        self.assertEqual(r["removed"], [])
        self.assertTrue(r["why"])
        self.assertTrue(r["unsized"])
        self.assertTrue(all(os.path.isdir(x) for x in paths))


class ThePruneOrder(RustCase):
    """T-4764 follow-up: a lane whose task is still open (waiting on review) is pruned last; an unmarked folder is pruned
    like any other (a failed marker write must not exempt a folder from the cap for good)."""

    def open_tasks(self, *lanes_):
        import types
        briefs = [types.SimpleNamespace(meta={"lane": x}, status="review") for x in lanes_]
        patch = unittest.mock.patch.object(c, "load_briefs", return_value=briefs)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_lane_whose_task_is_open_goes_after_the_closed_ones_whatever_its_age(self):
        waiting = self.kept("waits", 90)  # the oldest, but its task is in review
        done = self.kept("done", 50)
        self.kept("newer", 10)
        self.open_tasks(os.path.join(self.lane_root, "waits"))
        self.cap(self.cap_leaving(done))
        r = self.prune()
        self.assertEqual(self.removed(r), self.names(done))
        self.assertTrue(os.path.isdir(waiting), "the open one stays while closed ones can still go")

    def test_when_nothing_else_is_left_the_open_lanes_folder_goes_too_oldest_first(self):
        waiting, done = self.kept("waits", 90), self.kept("done", 50)
        self.open_tasks(os.path.join(self.lane_root, "waits"))
        self.cap(1)
        self.assertEqual(self.removed(self.prune()), self.names(done, waiting))

    def test_an_unreadable_task_list_changes_nothing(self):
        a, b = self.kept("a", 90), self.kept("b", 50)
        self.cap(self.cap_leaving(a))
        with unittest.mock.patch.object(c, "load_briefs", side_effect=OSError("no")):
            self.assertEqual(self.removed(self.prune()), self.names(a))
        self.assertTrue(os.path.isdir(b))

    def test_an_unmarked_folder_is_pruned_like_any_other_when_nothing_holds_it(self):
        a = self.cfolder("nomark", 50, marked=False)
        self.cap(1)
        self.assertEqual(self.removed(self.prune()), self.names(a))
        self.assertFalse(os.path.exists(a))

    def test_an_unmarked_folder_something_holds_is_still_kept(self):
        a = self.cfolder("nomark2", 50, marked=False)
        self.cap(1)
        r = self.prune(held=[os.path.join(a, "debug")])
        self.assertEqual(r["removed"], [])
        self.assertTrue(os.path.isdir(a))


class TheHoldersWithoutProc(RustCase):
    """T-4764 follow-up: without /proc (macOS) the open files come from lsof, through one helper that _busy_dirs shares;
    a view that can't be had is said, never read as 'nothing is held'."""

    def no_proc(self, out="", code=0, boom=None):
        real = os.path.isdir
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if boom:
                raise boom
            return subprocess.CompletedProcess(argv, code, out, "")
        for patch in (unittest.mock.patch.object(lanes.os.path, "isdir", lambda p: False if p == "/proc/self" else real(p)),
                      unittest.mock.patch.object(lanes.subprocess, "run", run)):
            patch.start()
            self.addCleanup(patch.stop)
        return calls

    def test_the_open_files_come_from_lsof_and_busy_dirs_uses_the_same_helper(self):
        calls = self.no_proc("p1\nn/a/b\nn/c\nf3\nnTCP\n")
        self.assertEqual(lanes._held_paths(), ["/a/b", "/c"])
        self.assertEqual(lanes._busy_dirs(), ["/a/b", "/c"])
        self.assertEqual(calls[0][:2], ["lsof", "-Fn"])
        self.assertEqual(calls[1], ["lsof", "-Fn", "-d", "cwd"])

    def test_no_lsof_or_a_failing_one_is_no_view(self):
        self.no_proc(boom=FileNotFoundError("lsof"))
        self.assertIsNone(lanes._held_paths())
        self.assertEqual(lanes._busy_dirs(), [])

    def test_an_lsof_error_is_no_view(self):
        self.no_proc("n/a\n", code=2)
        self.assertIsNone(lanes._held_paths())

    def test_a_folder_lsof_lists_is_kept_and_a_free_one_goes(self):
        held, free = self.kept("held", 60), self.kept("free", 50)
        self.no_proc(f"n{os.path.join(held, 'debug', 'deps', 'x.rlib')}\n")
        self.cap(self.cap_leaving(free))
        r = lanes.prune_cargo_cache(self.p)
        self.assertEqual(self.removed(r), self.names(free))
        self.assertTrue(os.path.isdir(held))

    def test_the_host_that_lists_holders_through_lsof_prunes_the_same_folders_as_one_with_proc(self):
        """T-4792: macOS CI ran the subprocess tidy test with lsof as its view and got the pruned folders where the test
        expected none (it read 'no /proc' as 'no view'). Mocked as macOS gives it: no /proc, lsof lists processes that
        hold nothing in the cache; the findings, the prune and the doctor name what they name with /proc."""
        from sampo import tidy
        paths = [self.kept(f"e{i}", age) for i, age in enumerate((40, 30, 20, 10))]
        self.cap(self.cap_leaving(paths[0], paths[1]))
        self.no_proc("p1\nn/\nn/usr/lib/dyld\nf3\nn/dev/null\n")
        self.assertTrue(self.sees_holders(), "lsof gives a view, so /proc's absence is no refusal")
        actions = []
        found = lanes.cargo_cache_findings(self.p, True, actions, tidy.finding)
        self.assertEqual([f["kind"] for f in found], ["cargo_cache"])
        self.assertEqual(actions, [("cargo_cache", self.slug)])
        self.assertEqual(sorted(os.listdir(self.base)), sorted(self.names(paths[2], paths[3])), "the same two as with /proc")

    def test_the_old_shared_folder_goes_on_a_host_with_lsof_as_it_does_with_proc(self):
        entry = self.old_entry(blocks=2)
        self.no_proc("p1\nn/\nn/dev/null\n")
        self.assertTrue(lanes.retire_cargo_shared(self.p)["removed"])
        self.assertFalse(os.path.exists(entry))

    def test_without_lsof_nothing_is_removed_and_the_report_says_why(self):
        a = self.kept("a", 60)
        self.no_proc(boom=FileNotFoundError("lsof"))
        self.cap(1)
        r = lanes.prune_cargo_cache(self.p)
        self.assertEqual(r["removed"], [])
        self.assertIn("open files", r["why"])
        self.assertTrue(os.path.isdir(a))

    def test_a_gone_lanes_folder_that_cannot_be_judged_is_named_not_skipped_silently(self):
        from sampo import tidy
        gone = self.cfolder("vanished", 50, lane=os.path.join(self.lane_root, "gone-lane"))
        skipped = []
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=None):
            self.assertEqual(lanes.orphan_lane_folders(self.p, skipped=skipped), [])
            found = lanes.cargo_cache_findings(self.p, False, [], tidy.finding)
        self.assertEqual(skipped, [(os.path.basename(gone), "no view of the open files on this system")])
        kept = [f for f in found if f["kind"] == "cargo_lane_orphan_kept"]
        self.assertEqual(len(kept), 1, found)
        self.assertIn(os.path.basename(gone), kept[0]["detail"])
        self.assertIn("no view of the open files", kept[0]["detail"])
        self.assertTrue(os.path.isdir(gone))


class TheAutoPrune(RustCase):
    """T-4764 follow-up: the cap holds without `sampo tidy`: a lane folder assigned (lane new, focus) or a lane removed
    (lane rm, a landing) prunes with the same rules, bounded in time, and says in one line what went."""

    def test_over_the_cap_it_prunes_oldest_first_and_names_what_went_in_one_line(self):
        a, b = self.kept("a", 60), self.kept("b", 50)
        self.cap(self.cap_leaving(a))
        line = lanes.auto_prune(self.p, held=[])
        self.assertFalse(os.path.exists(a))
        self.assertTrue(os.path.isdir(b))
        self.assertNotIn("\n", line)
        self.assertIn(os.path.basename(a), line)
        self.assertIn("cap", line)

    def test_under_the_cap_or_nothing_there_says_nothing(self):
        self.assertEqual(lanes.auto_prune(self.p, held=[]), "")
        a = self.kept("a", 60)
        self.cap(10**12)
        self.assertEqual(lanes.auto_prune(self.p, held=[]), "")
        self.assertTrue(os.path.isdir(a))

    def test_an_assignment_looks_at_most_once_in_ten_minutes_a_removal_always(self):
        a = self.kept("a", 60)
        self.cap(1)
        self.assertTrue(lanes.auto_prune(self.p, held=[]))
        b = self.kept("b", 60)
        self.assertEqual(lanes.auto_prune(self.p, held=[]), "", "a second look so soon")
        self.assertTrue(os.path.isdir(b))
        self.assertTrue(lanes.auto_prune(self.p, force=True, held=[]))
        self.assertFalse(os.path.exists(b))
        self.assertFalse(os.path.exists(a))

    def test_a_sizing_that_runs_out_removes_nothing_and_does_not_raise(self):
        a = self.kept("a", 60)
        self.cap(1)
        line = lanes.auto_prune(self.p, force=True, held=[], budget=-1)
        self.assertTrue(os.path.isdir(a))
        self.assertNotIn("\n", line)
        self.assertIn("sampo tidy --apply", line, "the check says it ran out and where the rest is done")
        self.assertIn("ran out", line)

    def test_the_stamp_is_no_lane_folder(self):
        self.kept("a", 60)
        self.cap(10**12)
        lanes.auto_prune(self.p, held=[])
        root = lanes._lanes_root(self.slug)
        self.assertEqual([n for n, _ in lanes._lane_folders(root)], [os.path.basename(c.cargo_lane_dir(self.slug, os.path.join(self.lane_root, "a")))])

    def test_a_held_folder_is_kept_and_nothing_else_than_what_it_prunes_is_touched(self):
        held, free = self.kept("held", 60), self.kept("free", 50)
        self.cap(1)
        lanes.auto_prune(self.p, force=True, held=[os.path.join(held, "debug")])
        self.assertTrue(os.path.isdir(held))
        self.assertFalse(os.path.exists(free))

    def test_every_moment_a_lane_folder_comes_or_goes_runs_it(self):
        import inspect
        for fn, name in ((lanes.remove, "lane rm"), (lanes.lane_new_cargo, "lane new")):
            self.assertIn("auto_prune(", inspect.getsource(fn), name)
        from sampo import cli
        self.assertIn("auto_prune(", inspect.getsource(cli.cmd_focus), "focus")
        self.assertNotIn("auto_prune(", inspect.getsource(lanes.drop_landed_target),
                         "a landing runs under the train's lock: no sizing of the cache there")

    def test_a_landing_drops_its_own_folder_and_the_next_look_prunes_the_rest(self):
        lane = self.lane("T-5060", old=False)
        mine, old = self.cfolder("T-5060", 0, lane=lane), self.kept("older", 70)
        self.cap(1)
        lanes.auto_prune(self.p, held=[])  # a look a minute ago: its stamp would hold the next one back
        self.assertFalse(os.path.exists(old))
        old = self.kept("older2", 70)
        b = unittest.mock.Mock(meta={"lane": lane})
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            self.assertTrue(lanes.drop_landed_target(self.p, b, self.repo))
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.isdir(old), "no cap walk at the landing")
        self.assertTrue(lanes.auto_prune(self.p, held=[]), "the landing cleared the stamp: the next look is at once")
        self.assertFalse(os.path.exists(old))

    def test_the_stamp_is_written_whole_by_rename(self):
        self.kept("a", 60)
        self.cap(10**12)
        calls = []
        real = c.write_atomic
        with unittest.mock.patch.object(c, "write_atomic", lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1]):
            lanes.auto_prune(self.p, held=[])
        self.assertEqual([os.path.basename(x) for x in calls], [lanes.PRUNE_STAMP])

    def test_the_budget_covers_the_removals_too_and_the_line_names_what_is_left(self):
        a, b = self.kept("a", 60), self.kept("b", 50)
        self.cap(1)
        real = lanes._drop_lane_folder

        def slow(root, path, held, deadline=None):  # the first removal uses up the whole budget
            ok = real(root, path, held, deadline)
            time.sleep(2.2)
            return ok
        with unittest.mock.patch.object(lanes, "_drop_lane_folder", slow):
            line = lanes.auto_prune(self.p, force=True, held=[], budget=2)
        self.assertFalse(os.path.exists(a))
        self.assertTrue(os.path.isdir(b), "the run stopped at its deadline before the second removal")
        self.assertIn("sampo tidy --apply", line)
        self.assertIn("ran out of its 2 s", line)

    def test_one_reading_of_the_holders_serves_the_whole_prune(self):
        self.kept("a", 60), self.kept("b", 50)
        self.cap(1)
        calls = []
        with unittest.mock.patch.object(lanes, "_held_paths", lambda *a, **k: calls.append(1) or []):
            line = lanes.auto_prune(self.p, force=True)
        self.assertEqual(len(calls), 1, line)
        self.assertIn("pruned 2", line)


class TheGateFolders(RustCase):
    """T-4764 follow-up: the gate's stable cargo folder and the private ones a busy run builds in leave no litter: the
    stable one goes when unused for 14 days or over the lane cap (never in the last hour, never with a run or a process
    in it), a private one when its run is gone; all by exact path, in a temp cache."""

    def stable(self, hours_old, blocks=1):
        gate = c.cargo_gate_dir(self.slug)
        self.fake(os.path.join(gate, "debug", "deps", "x.rlib"), hours_old, blocks)
        then = time.time() - hours_old * HOUR
        lock = os.path.join(self.base, c.GATE_LOCK)  # the run's lock file, as old as its last run
        write(lock, "")
        for mark in (gate, lock):
            os.utime(mark, (then, then))
        return gate

    def private(self, pid, hours_old, blocks=1, suffix="abcdef"):
        path = os.path.join(self.base, f"gate-p{pid}-{suffix}")
        self.fake(os.path.join(path, "debug", "deps", "y.rlib"), hours_old, blocks)
        then = time.time() - hours_old * HOUR
        os.utime(path, (then, then))
        return path

    def dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def test_a_stable_folder_unused_for_two_weeks_goes_and_a_younger_one_stays(self):
        gate = self.stable(15 * 24)
        self.assertEqual(lanes.gate_cargo_folder(self.p, held=[], dry=True)["stale"], True)
        self.assertTrue(os.path.isdir(gate), "dry: nothing removed")
        r = lanes.gate_cargo_folder(self.p, held=[])
        self.assertTrue(r["removed"], r)
        self.assertFalse(os.path.exists(gate))
        young = self.stable(13 * 24)
        r = lanes.gate_cargo_folder(self.p, held=[])
        self.assertFalse(r["stale"])
        self.assertTrue(os.path.isdir(young))

    def test_over_the_lane_cap_it_goes_unless_it_was_used_in_the_last_hour(self):
        self.cap(1)
        used = self.stable(0.2)
        self.assertFalse(lanes.gate_cargo_folder(self.p, held=[])["stale"], "a run used it a moment ago")
        self.assertTrue(os.path.isdir(used))
        old = self.stable(5)
        self.assertTrue(lanes.gate_cargo_folder(self.p, held=[])["removed"])
        self.assertFalse(os.path.exists(old))

    def test_a_process_in_it_or_a_run_holding_its_slot_keeps_it(self):
        gate = self.stable(15 * 24)
        r = lanes.gate_cargo_folder(self.p, held=[os.path.join(gate, "debug")])
        self.assertFalse(r["removed"])
        self.assertTrue(r["why"], "named, not silent")
        self.assertTrue(os.path.isdir(gate))
        with c.cargo_gate_slot(gate, touch=False) as folder:  # a run in it (the lock is held)
            self.assertEqual(folder, gate)
            r = lanes.gate_cargo_folder(self.p, held=[])
        self.assertFalse(r["removed"])
        self.assertIn("a run holds it", r["why"])
        self.assertTrue(os.path.isdir(gate))

    def test_a_link_where_the_gate_folder_would_be_is_left_alone(self):
        outside = os.path.join(self.tmp, "precious-gate")
        keep = self.fake(os.path.join(outside, "keep.bin"), 99 * 24)
        os.makedirs(self.base, exist_ok=True)
        os.symlink(outside, os.path.join(self.base, c.CARGO_GATE))
        r = lanes.gate_cargo_folder(self.p, held=[])
        self.assertFalse(r["removed"])
        self.assertTrue(os.path.isfile(keep))

    def test_tidy_names_it_and_apply_removes_it_with_the_reason_in_the_line(self):
        from sampo import tidy
        gate = self.stable(15 * 24)
        found = lanes.cargo_cache_findings(self.p, False, [], tidy.finding)
        item = [f for f in found if f["kind"] == "cargo_gate"]
        self.assertEqual(len(item), 1, found)
        self.assertIn("unused for 14 days", item[0]["detail"])
        self.assertTrue(os.path.isdir(gate))
        actions = []
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            lanes.cargo_cache_findings(self.p, True, actions, tidy.finding)
        self.assertFalse(os.path.exists(gate))
        self.assertIn(("cargo_gate", self.slug), actions)

    def test_a_dead_runs_private_folder_is_swept_a_live_runs_and_a_held_one_stay(self):
        dead = self.private(self.dead_pid(), 2)
        live = self.private(os.getpid(), 2, suffix="123456")
        held = self.private(self.dead_pid(), 2, suffix="fedcba")
        skipped = []
        gone = lanes.sweep_gate_leftovers(self.p, held=[os.path.join(held, "debug")], skipped=skipped)
        self.assertEqual([n for n, _ in gone], [os.path.basename(dead)])
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.isdir(live), "its run is alive")
        self.assertTrue(os.path.isdir(held), "a process holds it")
        self.assertEqual([n for n, _ in skipped], [os.path.basename(held)])

    def test_the_sweep_with_no_view_of_the_open_files_stops_after_one_read(self):
        a = self.private(self.dead_pid(), 2, suffix="aaaaaa")
        b = self.private(self.dead_pid(), 2, suffix="bbbbbb")
        calls, skipped = [], []
        with unittest.mock.patch.object(lanes, "_held_paths", lambda *x, **k: calls.append(1)):
            gone = lanes.sweep_gate_leftovers(self.p, skipped=skipped)
        self.assertEqual(gone, [])
        self.assertEqual(len(calls), 1, "no re-read per folder")
        self.assertTrue(os.path.isdir(a) and os.path.isdir(b))
        self.assertEqual(len(skipped), 1, skipped)
        self.assertIn("no view", skipped[0][1])

    def test_a_live_pid_that_left_it_for_a_day_is_a_reused_pid(self):
        stale = self.private(os.getpid(), 30)
        gone = lanes.sweep_gate_leftovers(self.p, held=[])
        self.assertEqual([n for n, _ in gone], [os.path.basename(stale)])

    def test_a_private_folder_only_matching_the_scheme_is_touched(self):
        odd = self.fake(os.path.join(self.base, "gate-pxyz-abcdef", "f"), 99 * 24)
        near = self.fake(os.path.join(self.base, "gate-p12-abcde", "f"), 99 * 24)  # five hex digits: not ours
        self.assertEqual(lanes.sweep_gate_leftovers(self.p, held=[]), [])
        self.assertTrue(os.path.isfile(odd) and os.path.isfile(near))

    def test_tidy_and_the_auto_prune_sweep_leftovers_too(self):
        from sampo import tidy
        a = self.private(self.dead_pid(), 2, suffix="aaaaaa")
        found = lanes.cargo_cache_findings(self.p, False, [], tidy.finding)
        self.assertEqual(len([f for f in found if f["kind"] == "cargo_gate_leftover"]), 1, found)
        self.assertTrue(os.path.isdir(a))
        line = lanes.auto_prune(self.p, force=True, held=[])
        self.assertFalse(os.path.exists(a))
        self.assertIn("private gate folder", line)

    def busy_gate(self):
        """T-4984: a gate folder a run used two hours ago, with what a week of runs at new checkout paths leaves."""
        gate = self.stable(2)
        f = lambda rel, hours: self.fake(os.path.join(gate, *rel.split("/")), hours)
        return gate, {
            "fresh": f("debug/deps/kithd-aaaa.rlib", 2), "mid": f("debug/deps/kithd-cccc", 30),
            "old": f("debug/deps/kithd-bbbb", 72), "fp": f("debug/.fingerprint/kithd-bbbb/bin-kithd", 72),
            "build": f("debug/build/kithd-bbbb/out/gen.rs", 72), "musl": f("x86_64-unknown-linux-musl/debug/deps/t-dddd.rlib", 96),
            "inc": f("debug/incremental/kithd-1xyz/s-abc/dep-graph.bin", 3), "live": f("debug/incremental/kithd-2q/s-q/f", 0.2),
            "tag": f("CACHEDIR.TAG", 500)}

    def test_the_gate_sweep_takes_dead_incremental_state_and_what_no_run_used_for_two_days(self):
        # T-4984: on 2026-11-18 a project's gate folder held 52 GB, 35 of them incremental/ and 215 copies of one binary
        gate, x = self.busy_gate()
        r = lanes.sweep_gate_stale(self.p, held=[], dry=True)
        swept = {"old": "debug/deps/kithd-bbbb", "fp": "debug/.fingerprint/kithd-bbbb", "build": "debug/build/kithd-bbbb",
                 "musl": "x86_64-unknown-linux-musl/debug/deps/t-dddd.rlib", "inc": "debug/incremental/kithd-1xyz"}
        self.assertEqual(sorted(n for n, _ in r["removed"]), sorted(swept.values()))
        self.assertGreater(r["bytes"], 0)
        self.assertTrue(all(os.path.exists(p) for p in x.values()), "dry: nothing removed")
        r = lanes.sweep_gate_stale(self.p, held=[])
        for k, path in x.items():
            self.assertEqual(os.path.exists(path), k not in swept, k)
        self.assertTrue(os.path.isdir(os.path.join(gate, "debug", "incremental")), "only what is in it goes")

    def test_the_gate_sweep_counts_age_from_the_gates_last_run_not_from_now(self):
        gate, x = self.busy_gate()
        later = time.time() + 30 * DAY  # a month with no run: nothing is staler than it was (the 14-day rule takes it whole)
        r = lanes.sweep_gate_stale(self.p, held=[], dry=True, now=later)
        self.assertNotIn("debug/deps/kithd-cccc", [n for n, _ in r["removed"]])
        self.fake(os.path.join(gate, "debug", "build", "odd-eeee", "out", "f"), -30 * 24)  # dated a month ahead
        r = lanes.sweep_gate_stale(self.p, held=[], dry=True)
        self.assertNotIn("debug/deps/kithd-cccc", [n for n, _ in r["removed"]], "a future date makes nothing staler")
        self.assertIn("debug/deps/kithd-bbbb", [n for n, _ in r["removed"]])

    def test_a_run_in_the_gate_or_a_process_holding_it_stops_the_sweep(self):
        gate, x = self.busy_gate()
        with c.cargo_gate_slot(gate, touch=False):
            r = lanes.sweep_gate_stale(self.p, held=[])
        self.assertEqual((r["removed"], r["why"]), ([], "a run holds it"))
        r = lanes.sweep_gate_stale(self.p, held=[os.path.join(gate, "debug")])
        self.assertEqual(r["removed"], [])
        self.assertTrue(r["why"])
        self.assertTrue(all(os.path.exists(p) for p in x.values()))

    def test_on_a_disk_that_keeps_no_access_times_only_the_incremental_state_goes(self):
        gate, x = self.busy_gate()
        with unittest.mock.patch.object(lanes, "_noatime", return_value=True):
            r = lanes.sweep_gate_stale(self.p, held=[])
        self.assertEqual([n for n, _ in r["removed"]], ["debug/incremental/kithd-1xyz"])
        self.assertTrue(os.path.exists(x["old"]), "a crate read every run would look unused there")

    def test_tidy_the_auto_prune_and_doctor_name_and_run_the_gate_sweep(self):
        from sampo import tidy
        gate, x = self.busy_gate()
        found = [f for f in lanes.cargo_cache_findings(self.p, False, [], tidy.finding) if f["kind"] == "cargo_gate_sweep"]
        self.assertEqual(len(found), 1)
        self.assertIn("5 gate build entries", found[0]["detail"])
        self.assertIn("gate build entries", d.check_disk(self.tmp, self.low, [(self.p, {})]).detail.split("Sampo could free")[1])
        self.assertTrue(os.path.exists(x["old"]))
        line = lanes.auto_prune(self.p, force=True, held=[])
        self.assertNotIn("\n", line)
        self.assertIn("swept 5 gate build entries", line)
        self.assertFalse(os.path.exists(x["old"]) or os.path.exists(x["inc"]))
        self.assertTrue(os.path.exists(x["fresh"]))

    def test_doctor_counts_the_private_folders_with_the_stable_one(self):
        gate = self.stable(2, blocks=10)
        priv = self.private(os.getpid(), 1, blocks=10)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertIn(f"gate cargo folder {lanes.human(self.disk(gate) + self.disk(priv))}", r.detail)


class TheWalksStopOnTheirDeadline(RustCase):
    """T-4764 follow-up: one folder of a million files is no single step: the sizing and the removal look at their
    deadline every WALK_CHECK_EVERY entries, not only per folder."""

    class Ticking:
        """A clock that moves one tick per look, so a deadline passes in the middle of a walk."""
        def __init__(self):
            self.t = 0.0

        def monotonic(self):
            self.t += 1
            return self.t

        def __getattr__(self, name):
            return getattr(time, name)

    def flat(self, n=10):
        folder = os.path.join(self.tmp, "flat")
        for i in range(n):
            self.fake(os.path.join(folder, f"f{i}"), 0)
        return folder

    def test_dir_bytes_gives_up_in_the_middle_of_one_folder(self):
        folder = self.flat()
        with unittest.mock.patch.object(lanes, "WALK_CHECK_EVERY", 2), unittest.mock.patch.object(lanes, "time", self.Ticking()):
            self.assertIsNone(lanes.dir_bytes(folder, 3))
        with unittest.mock.patch.object(lanes, "time", self.Ticking()):
            self.assertIsNotNone(lanes.dir_bytes(folder, 3), "control: the default step is longer than this folder")

    def test_the_cache_sizing_gives_up_in_the_middle_of_one_folder(self):
        folder = self.flat()
        with unittest.mock.patch.object(lanes, "WALK_CHECK_EVERY", 2), unittest.mock.patch.object(lanes, "time", self.Ticking()):
            with self.assertRaises(lanes._SizingOut):
                lanes._cache_tree(folder, 3)
        with unittest.mock.patch.object(lanes, "time", self.Ticking()):
            lanes._cache_tree(folder, 3)

    def test_a_removal_past_its_deadline_stops_and_leaves_the_folder_for_the_next_one(self):
        folder = self.flat()
        self.assertFalse(lanes._rmtree_until(folder, time.monotonic() - 1))
        self.assertTrue(os.path.isdir(folder))
        self.assertTrue(lanes._rmtree_until(folder, time.monotonic() + 60))
        self.assertFalse(os.path.exists(folder))

    def test_a_removal_takes_exactly_its_folder_and_leaves_a_links_target(self):
        outside = os.path.join(self.tmp, "precious-two")
        keep = self.fake(os.path.join(outside, "keep.bin"), 99)
        folder = self.flat(3)
        sibling = self.fake(os.path.join(self.tmp, "flat-sibling", "f"), 0)
        os.symlink(outside, os.path.join(folder, "link"))
        self.assertTrue(lanes._rmtree_until(folder, time.monotonic() + 60))
        self.assertFalse(os.path.lexists(folder))
        self.assertTrue(os.path.isfile(keep) and os.path.isfile(sibling))

    def test_an_lsof_that_lists_nothing_is_no_view_though_the_busy_dirs_are_empty(self):
        real = os.path.isdir
        for patch in (unittest.mock.patch.object(lanes.os.path, "isdir", lambda p: False if p == "/proc/self" else real(p)),
                      unittest.mock.patch.object(lanes.subprocess, "run",
                                                 lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))):
            patch.start()
            self.addCleanup(patch.stop)
        self.assertIsNone(lanes._held_paths(), "a full view of every process is never empty: lsof failed")
        self.assertEqual(lanes._busy_dirs(), [])


class TheLaneOwnFolders(RustCase):
    def grow(self, lane, blocks=2, tagged=True):
        return self.target(lane, blocks, tagged)

    def test_a_removed_lane_leaves_no_target_folder(self):
        lane = self.lane("T-5020", old=False)
        self.grow(lane)
        r = subprocess.run(["git", "-C", self.repo, "worktree", "remove", lane], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(lane, "target")))

    def test_dropping_a_landed_lanes_target_leaves_the_lane_and_the_main_target(self):
        lane = self.lane("T-5021", old=False, merged=True)
        grown = self.grow(lane)
        mine = self.fake(os.path.join(self.repo, "target", "debug", "deps", "x.rlib"), 500)
        src = self.fake(os.path.join(lane, "src", "main.rs"), 1)
        self.assertTrue(lanes.drop_lane_target(lane, self.repo, held=[]))  # no /proc on macOS: nothing is read, nothing removed
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(os.path.dirname(grown)))))
        self.assertTrue(os.path.isfile(src))
        self.assertTrue(os.path.isfile(mine), "the project's own target folder is never touched")

    def test_a_tracked_target_folder_with_a_source_file_survives_even_with_the_tag(self):
        lane = self.lane("T-5024", old=False, merged=True)
        src = os.path.join(lane, "target", "lib.rs")
        write(src, "fn main() {}\n")
        write(os.path.join(lane, "target", "CACHEDIR.TAG"), TAG)
        git(lane, "add", "-f", "target/lib.rs")
        git(lane, "commit", "-qm", "a source folder called target")
        self.assertFalse(lanes.drop_lane_target(lane, self.repo))
        self.assertTrue(os.path.isfile(src))
        self.assertIsNone(lanes.lane_build_output(self.p, budget=5), "not build output: not named either")

    def test_a_tracked_target_folder_without_the_tag_survives(self):
        lane = self.lane("T-5025", old=False, merged=True)
        src = os.path.join(lane, "target", "lib.rs")
        write(src, "fn main() {}\n")
        git(lane, "add", "-f", "target/lib.rs")
        git(lane, "commit", "-qm", "tracked")
        self.assertFalse(lanes.drop_lane_target(lane, self.repo))
        self.assertTrue(os.path.isfile(src))

    def test_an_untracked_folder_without_the_tag_survives(self):
        lane = self.lane("T-5026", old=False, merged=True)
        big = self.grow(lane, tagged=False)
        self.assertFalse(lanes.drop_lane_target(lane, self.repo))
        self.assertTrue(os.path.isfile(big), "made by hand, no cargo signature: not ours to remove")
        self.assertIsNone(lanes.lane_build_output(self.p, budget=5))

    def test_a_tag_that_is_not_cargos_signature_is_not_proof(self):
        lane = self.lane("T-5027", old=False, merged=True)
        big = self.grow(lane, tagged=False)
        write(os.path.join(lane, "target", "CACHEDIR.TAG"), "Signature: 00000000000000000000000000000000\n")
        self.assertFalse(lanes.drop_lane_target(lane, self.repo))
        self.assertTrue(os.path.isfile(big))

    def test_a_link_called_target_is_not_followed(self):
        lane = self.lane("T-5022", old=False)
        outside = os.path.join(self.tmp, "precious")
        keep = self.fake(os.path.join(outside, "keep.bin"), 1)
        write(os.path.join(outside, "CACHEDIR.TAG"), TAG)
        os.symlink(outside, os.path.join(lane, "target"))
        self.assertFalse(lanes.drop_lane_target(lane, self.repo))
        self.assertTrue(os.path.isfile(keep))

    def test_only_a_sampo_lane_of_the_project_is_touched(self):
        for stray in (self.repo, self.tmp):
            kept = self.fake(os.path.join(stray, "target", "x.bin"), 1)
            write(os.path.join(stray, "target", "CACHEDIR.TAG"), TAG)
            self.assertFalse(lanes.drop_lane_target(stray, self.repo), stray)
            self.assertTrue(os.path.isfile(kept))

    def test_a_target_a_process_stands_in_is_left(self):
        lane = self.lane("T-5023", old=False)
        big = self.grow(lane)
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[os.path.dirname(big)]):
            self.assertFalse(lanes.drop_lane_target(lane, self.repo))
        self.assertTrue(os.path.isfile(big))

    def test_a_target_with_a_held_cargo_lock_is_left(self):
        lane = self.lane("T-5028", old=False)
        big = self.grow(lane)
        fd = os.open(self.fake(os.path.join(lane, "target", "debug", ".cargo-lock")), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertFalse(lanes.drop_lane_target(lane, self.repo, held=[]))
        self.assertTrue(os.path.isfile(big))

    # ---- the lane's cargo folder under Sampo's cache goes with the lane (T-4764)

    def test_the_lanes_cargo_folder_goes_by_its_exact_path_and_the_others_stay(self):
        mine, other = os.path.join(self.lane_root, "T-5040"), os.path.join(self.lane_root, "T-5041")
        a, b = self.cfolder("T-5040", 0, lane=mine), self.cfolder("T-5041", 0, lane=other)
        self.assertTrue(lanes.drop_lane_cargo_dir(self.slug, mine, held=[]))
        self.assertFalse(os.path.lexists(a))
        self.assertTrue(os.path.isdir(b), "another lane's folder is never touched")
        self.assertTrue(os.path.isdir(self.base))
        self.assertFalse(lanes.drop_lane_cargo_dir(self.slug, mine, held=[]), "a second call finds nothing to do")

    def test_two_lanes_with_one_folder_name_have_two_folders(self):
        one = os.path.join(self.tmp, "x", "T-5042")
        two = os.path.join(self.tmp, "y", "T-5042")
        a, b = self.cfolder("one", 0, lane=one), self.cfolder("two", 0, lane=two)
        self.assertNotEqual(a, b)
        self.assertTrue(lanes.drop_lane_cargo_dir(self.slug, one, held=[]))
        self.assertTrue(os.path.isdir(b))

    def test_a_folder_a_process_holds_stays(self):
        lane = os.path.join(self.lane_root, "T-5043")
        a = self.cfolder("T-5043", 0, lane=lane)
        self.assertFalse(lanes.drop_lane_cargo_dir(self.slug, lane, held=[os.path.join(a, "debug")]))
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=None):
            self.assertFalse(lanes.drop_lane_cargo_dir(self.slug, lane), "no view of the open files: nothing removed")
        with unittest.mock.patch.object(lanes, "_held_paths", side_effect=OSError("gone")):
            self.assertFalse(lanes.drop_lane_cargo_dir(self.slug, lane), "never raises")
        self.assertTrue(os.path.isdir(a))

    def test_a_folder_with_a_held_cargo_lock_stays(self):
        lane = os.path.join(self.lane_root, "T-5044")
        a = self.cfolder("T-5044", 0, lane=lane)
        fd = os.open(self.fake(os.path.join(a, "debug", ".cargo-lock")), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertFalse(lanes.drop_lane_cargo_dir(self.slug, lane, held=[]))
        self.assertTrue(os.path.isdir(a))

    def test_a_link_in_the_place_of_the_folder_is_never_followed(self):
        lane = os.path.join(self.lane_root, "T-5045")
        outside = os.path.join(self.tmp, "precious")
        keep = self.fake(os.path.join(outside, "keep.bin"), 1)
        os.makedirs(self.base)
        os.symlink(outside, c.cargo_lane_dir(self.slug, lane))
        self.assertFalse(lanes.drop_lane_cargo_dir(self.slug, lane, held=[]))
        self.assertTrue(os.path.isfile(keep))

    def test_a_link_inside_the_folder_goes_as_a_link(self):
        lane = os.path.join(self.lane_root, "T-5046")
        a = self.cfolder("T-5046", 0, lane=lane)
        outside = os.path.join(self.tmp, "precious")
        keep = self.fake(os.path.join(outside, "keep.bin"), 1)
        os.symlink(outside, os.path.join(a, "debug", "link"))
        self.assertTrue(lanes.drop_lane_cargo_dir(self.slug, lane, held=[]))
        self.assertTrue(os.path.isfile(keep), "its target stays")

    def test_a_name_outside_the_scheme_or_a_slug_that_climbs_is_never_removed(self):
        lane = os.path.join(self.lane_root, "T-5047")
        stray = self.fake(os.path.join(self.base, "precious", "x.rlib"), 1)
        with unittest.mock.patch.object(c, "cargo_lane_name", return_value="precious"):
            self.assertFalse(lanes.drop_lane_cargo_dir(self.slug, lane, held=[]))
        self.assertTrue(os.path.isfile(stray))
        elsewhere = self.fake(os.path.join(os.path.dirname(os.path.dirname(self.base)), "other", c.cargo_lane_name(lane), "x"), 1)
        self.assertFalse(lanes.drop_lane_cargo_dir("../other", lane, held=[]))
        self.assertTrue(os.path.isfile(elsewhere))

    def test_the_blocker_names_why(self):
        a = self.cfolder("T-5048", 0)
        root = lanes._lanes_root(self.slug)
        real = os.path.join(root, os.path.basename(a))
        self.assertEqual(lanes._lane_folder_blocker(root, real, []), "")
        self.assertIn("no view", lanes._lane_folder_blocker(root, real, None))
        self.assertIn("holds it", lanes._lane_folder_blocker(root, real, [real]))
        for bad in (os.path.join(root, "plain"), os.path.dirname(root), os.path.join(root, "a", os.path.basename(a))):
            self.assertIn("not one of Sampo's", lanes._lane_folder_blocker(root, bad, []), bad)

    def test_the_landing_removes_both_after_the_record_and_without_sizing_them(self):
        lane = self.lane("T-5029", old=False)
        big = self.grow(lane)
        folder = self.cfolder("T-5029", 0, lane=lane)
        b = unittest.mock.Mock(meta={"lane": lane})
        with unittest.mock.patch.object(lanes, "dir_bytes", side_effect=AssertionError("sized the folder first")), \
                unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            self.assertTrue(lanes.drop_landed_target(self.p, b, self.repo))
        self.assertFalse(os.path.exists(big))
        self.assertFalse(os.path.exists(folder))

    def test_the_landing_removes_the_cargo_folder_alone_when_the_lane_has_no_target(self):
        lane = self.lane("T-5049", old=False)
        folder = self.cfolder("T-5049", 0, lane=lane)
        b = unittest.mock.Mock(meta={"lane": lane})
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            self.assertTrue(lanes.drop_landed_target(self.p, b, self.repo))
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.isdir(lane))

    def test_the_landing_reads_the_holders_once_for_both_removals(self):
        lane = self.lane("T-5054", old=False)
        big = self.grow(lane)
        folder = self.cfolder("T-5054", 0, lane=lane)
        b = unittest.mock.Mock(meta={"lane": lane})
        calls = []
        with unittest.mock.patch.object(lanes, "_held_paths", lambda *a, **k: calls.append(1) or []):
            self.assertTrue(lanes.drop_landed_target(self.p, b, self.repo))
        self.assertEqual(len(calls), 1, "one read under the train's lock, not one per folder")
        self.assertFalse(os.path.exists(big) or os.path.exists(folder))

    def test_the_landing_with_no_view_of_the_holders_removes_nothing_and_reads_once(self):
        lane = self.lane("T-5055", old=False)
        big = self.grow(lane)
        folder = self.cfolder("T-5055", 0, lane=lane)
        b = unittest.mock.Mock(meta={"lane": lane})
        calls = []
        with unittest.mock.patch.object(lanes, "_held_paths", lambda *a, **k: calls.append(1)):
            self.assertFalse(lanes.drop_landed_target(self.p, b, self.repo))
        self.assertEqual(len(calls), 1)
        self.assertTrue(os.path.exists(big) and os.path.isdir(folder), "nothing removed without a view")

    def test_a_target_drop_with_a_deadline_reads_the_holders_under_it(self):
        lane = self.lane("T-5056", old=False)
        self.grow(lane)
        seen = []
        with unittest.mock.patch.object(lanes, "_read_held", lambda deadline=None: seen.append(deadline) or []):
            lanes.drop_lane_target(lane, self.repo, deadline=time.monotonic() + 5)
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0], "the read is bounded by the caller's deadline")

    def test_the_merge_drops_the_target_after_it_records_the_merge(self):
        import inspect
        src = inspect.getsource(lanes.merge)
        self.assertLess(src.index("pm_record(p, c.git_head(main)"), src.index("drop_landed_target(p, b, main)"),
                        "a slow removal never holds the record back")

    def test_removing_a_lane_takes_its_cargo_folder_along(self):
        import inspect
        self.assertIn("drop_lane_cargo_dir(", inspect.getsource(lanes.remove), "fm lane rm")
        lane = self.lane("T-5050", size=1000, merged=True)
        mine, other = self.cfolder("T-5050", 0, lane=lane), self.cfolder("T-5051", 0)
        row = self.finished()["T-5050"]
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            self.assertEqual(lanes._remove_finished(self.repo, row, self.slug), "")
        self.assertFalse(os.path.exists(lane))
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.isdir(other), "another lane's stays")

    def test_tidy_of_a_finished_lane_takes_its_cargo_folder_along(self):
        lane = self.lane("T-5052", size=1000, merged=True)
        mine, other = self.cfolder("T-5052", 0, lane=lane), self.cfolder("T-5053", 0)
        self.tidy("--apply")
        self.assertNotIn(os.path.realpath(lane), self.registered(), "the finished lane was removed")
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.isdir(other))


class DoctorAndTidyNameThem(RustCase):
    def test_doctor_names_each_lane_target_folder_each_cargo_folder_and_the_cap(self):
        a = self.lane("T-5030", old=False)
        b = self.lane("agent-b", old=False)
        self.target(a, 150)  # 1.2 MB of files
        self.target(b, 60)  # 0.5 MB of files
        mine = self.cfolder("T-5030", 5, blocks=30, lane=a)  # 245 KB of files
        self.cap(2_000_000)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertEqual(r.status, "WARN")
        for text in (f"T-5030 target {lanes.human(self.disk(os.path.join(a, 'target')))}",
                     f"agent-b target {lanes.human(self.disk(os.path.join(b, 'target')))}",
                     f"lane T-5030 cargo folder {lanes.human(self.disk(mine))}",
                     f"cargo lane folders {lanes.human(self.disk(mine))} of 2.0 MB cap per project"):
            self.assertIn(text, r.detail)
        self.assertGreaterEqual(self.disk(os.path.join(b, "target")), BLOCK * 60, "at least the bytes the test wrote")
        self.assertIn("lane build output (2 target folders, 1 cargo folder)", r.detail)

    def test_doctor_names_the_gate_folder_outside_the_cap(self):
        gate = c.cargo_gate_dir(self.slug)
        self.fake(os.path.join(gate, "debug", "deps", "x.rlib"), 2, blocks=40)
        self.cap(1)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertIn("lane build output (gate)", r.detail)
        self.assertIn(f"gate cargo folder {lanes.human(self.disk(gate))} (one gate or train run at a time builds in it, "
                      "a run beside it in a private folder; outside the lane cap; sampo tidy --apply removes it when unused for 14 days "
                      "or over ", r.detail)
        self.assertNotIn("cargo lane folders", r.detail)
        self.assertEqual(lanes.prune_cargo_cache(self.p, held=[])["removed"], [], "the lane cap never takes the gate's folder")

    def test_doctor_names_the_old_shared_folder_and_how_it_goes(self):
        self.old_entry(blocks=30)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertIn("lane build output (old shared)", r.detail)
        self.assertIn(f"old shared cargo folder {lanes.human(self.disk(self.old))} (sampo tidy --apply removes it", r.detail)

    def test_doctor_does_not_count_the_parent_config_folder_as_a_lane(self):
        home = os.path.join(self.repo, ".claude", "worktrees")
        write(os.path.join(home, ".cargo", "config.toml"), "x")
        a = self.lane("T-5033", old=False, parent=home)
        self.target(a, 3)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertRegex(r.detail, r"app 1\b", "one lane, not two")

    def test_a_target_folder_that_is_not_cargos_output_is_not_named(self):
        a = self.lane("T-5034", old=False)
        self.target(a, 20, tagged=False)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertNotIn("T-5034 target", r.detail)

    def test_a_sizing_that_runs_out_says_so_instead_of_showing_nothing(self):
        a = self.lane("T-5035", old=False)
        self.target(a, 20)
        out = lanes.lane_build_output(self.p, budget=-1)
        self.assertIsNotNone(out)
        self.assertIn("could not size", out[0])
        self.assertIn("could not size", " ".join(out[2]))

    def test_a_roomy_disk_measures_none_of_it(self):
        a = self.lane("T-5031", old=False)
        self.target(a, 3)
        with unittest.mock.patch.object(lanes, "lane_build_output", side_effect=AssertionError("sized")):
            r = d.check_disk(self.tmp, lambda _: Usage(900e9, 300e9, 600e9), [(self.p, {})])
        self.assertEqual(r.status, "PASS")

    def test_the_findings_in_process(self):
        from sampo import tidy
        for i, age in enumerate((40, 30, 20, 10)):
            self.kept(f"e{i}", age)
        self.cap(BLOCK * 2)
        found = lanes.cargo_cache_findings(self.p, False, [], tidy.finding)
        self.assertEqual([f["kind"] for f in found], ["cargo_cache"])

    def test_tidy_says_it_could_not_size_the_folders_when_the_sizing_runs_out(self):
        from sampo import tidy
        self.kept("e0", 40)
        with unittest.mock.patch.object(lanes, "TIDY_SCAN_S", -1):
            found = lanes.cargo_cache_findings(self.p, False, [], tidy.finding)
        self.assertEqual([f["kind"] for f in found], ["cargo_cache"])
        self.assertIn("could not size", found[0]["detail"])

    def test_tidy_lists_folders_over_their_cap_and_apply_prunes_them_oldest_first(self):
        paths = [self.kept(f"e{i}", age) for i, age in enumerate((40, 30, 20, 10))]
        cap = self.cap_leaving(paths[0], paths[1])
        env = {"HOME": self.fake_home, CAP: str(cap)}
        report = json.loads(self.fm("tidy", "--json", env=env).stdout)  # a process mirrors SAMPO_X to FOREMAN_X at start
        found = [f for f in report["findings"] if f["kind"] == "cargo_cache"]
        self.assertEqual(len(found), 1, report)
        self.assertGreater(found[0]["bytes"], cap)
        self.assertTrue(found[0]["auto"])
        self.assertTrue(os.path.isdir(paths[0]), "a report removes nothing")
        self.fm("tidy", "--json", "--apply", env=env)
        left = sorted(os.listdir(self.base))
        # a host that cannot say what a process holds (no /proc and no lsof) prunes nothing (its own test:
        # test_without_a_view_of_open_files_nothing_is_removed); one that can (Linux's /proc, macOS's lsof: T-4792) prunes
        # the two oldest. The in-process tests below pin both with the view mocked.
        want = self.names(paths[2], paths[3]) if self.sees_holders() else self.names(*paths)
        self.assertEqual(left, sorted(want), f"view of open files here: {self.sees_holders()}; tidy said: {report}")

    def test_tidy_apply_prunes_oldest_first_with_the_open_files_given(self):
        """The same prune on every host: the holders are given (nothing is held), so /proc is not needed."""
        from sampo import tidy
        paths = [self.kept(f"e{i}", age) for i, age in enumerate((40, 30, 20, 10))]
        self.cap(self.cap_leaving(paths[0], paths[1]))
        actions = []
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            found = lanes.cargo_cache_findings(self.p, True, actions, tidy.finding)
        self.assertEqual([f["kind"] for f in found], ["cargo_cache"])
        self.assertEqual(actions, [("cargo_cache", self.slug)])
        self.assertEqual(sorted(os.listdir(self.base)), sorted(self.names(paths[2], paths[3])))

    def test_tidy_names_a_kept_lanes_target_folder_with_its_size_and_removes_nothing(self):
        a = self.lane("T-5032", old=False)
        big = self.target(a, 20)
        report = json.loads(self.fm("tidy", "--json", "--apply", env={"HOME": self.fake_home}).stdout)
        found = [f for f in report["findings"] if f["kind"] == "lane_target"]
        self.assertEqual(len(found), 1, report)
        self.assertIn("T-5032", found[0]["detail"])
        self.assertIn(f"target {lanes.human(self.disk(os.path.join(a, 'target')))} ", found[0]["detail"])
        self.assertGreaterEqual(found[0]["bytes"], BLOCK * 20, "at least the bytes the test wrote")
        self.assertTrue(os.path.isfile(big), "an unfinished lane's build output is its builder's")

    def test_a_project_without_rust_gets_none_of_the_lines(self):
        os.unlink(os.path.join(self.repo, "Cargo.toml"))
        git(self.repo, "commit", "-qam", "no rust")
        self.old_entry()
        self.cfolder("e0", 40, lane=os.path.join(self.tmp, "gone"))
        report = json.loads(self.fm("tidy", "--json", env={"HOME": self.fake_home}).stdout)
        kinds = ("cargo_cache", "lane_target", "cargo_shared", "cargo_lane_orphan")
        self.assertEqual([f for f in report["findings"] if f["kind"] in kinds], [])


class LaneOwnTarget(RustCase):
    """T-4951 (kith, 2026-11-17: finished lanes held 79 GB of their own ./target while each had its cargo folder under
    Sampo's cache): a finished lane waiting on its merge that has its cargo folder loses its ./target to tidy --apply
    (an APPLY item with its size); a lane still building, one without its folder, one a process holds and the main
    checkout's own target/ stay. Doctor's disk line counts it as freeable, not as kept build output."""

    def setUp(self):
        super().setUp()
        patch = unittest.mock.patch.dict(os.environ, {"FM_STALL_CACHE_S": "0"})
        patch.start()
        self.addCleanup(patch.stop)

    def waiting(self, name, finished=True, folder=True):
        """A lane whose committed work waits on its merge (accepted after its last commit when `finished`), with its
        own ./target of 20 blocks and, when `folder`, its cargo folder under Sampo's cache."""
        lane = self.lane(name, size=100, old=False)
        out = self.fm("task", "new", f"Work {name}", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                      "--json").stdout
        b = c.find_brief(self.p, json.JSONDecoder().raw_decode(out)[0]["id"])
        b.meta["lane"] = os.path.realpath(lane)
        if finished:
            b.meta["accepted_at"] = str(time.time() + 60)
        c.save_brief(self.p, b)
        big = self.target(lane, 20)
        if folder:
            self.cfolder(name, 0, lane=lane)
        return os.path.realpath(lane), big

    def rows(self, apply, held=()):
        from sampo import tidy
        actions = []
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=list(held)):
            found = lanes.cargo_cache_findings(self.p, apply, actions, tidy.finding)
        return {os.path.basename(f["lane"]): f for f in found if f["kind"] == "lane_target"}, actions

    def test_a_finished_lanes_target_is_an_apply_item_with_its_size_and_goes_on_apply(self):
        lane, big = self.waiting("T-6001")
        own = self.target(self.repo, 5)  # the main checkout's own target/: never
        rows, _ = self.rows(False)
        self.assertEqual(rows["T-6001"]["severity"], "action", rows)
        self.assertTrue(rows["T-6001"]["auto"])
        self.assertGreaterEqual(rows["T-6001"]["bytes"], BLOCK * 20)
        self.assertIn(lanes.human(self.disk(os.path.join(lane, "target"))), rows["T-6001"]["detail"])
        self.assertTrue(os.path.isfile(big), "a dry run removes nothing")
        rows, actions = self.rows(True)
        self.assertFalse(os.path.exists(os.path.join(lane, "target")))
        self.assertIn(("lane_target", lane), actions)
        self.assertIn("removed", rows["T-6001"]["detail"])
        self.assertIn(lane, self.registered(), "the lane, its sources and branch stay")
        self.assertTrue(os.path.isdir(c.cargo_lane_dir(self.slug, lane)), "its cargo folder stays")
        self.assertTrue(os.path.isfile(own), "the main checkout's own target/")

    def test_a_lane_still_building_or_without_its_cargo_folder_keeps_it(self):
        building, a = self.waiting("T-6002", finished=False)
        bare, b = self.waiting("T-6003", folder=False)
        rows, actions = self.rows(True)
        for name in ("T-6002", "T-6003"):
            self.assertEqual(rows[name]["severity"], "info", name)
        self.assertTrue(os.path.isfile(a), "a lane still building may be building in it")
        self.assertTrue(os.path.isfile(b), "a lane with no folder of its own builds only here")
        self.assertEqual([x for x in actions if x[0] == "lane_target"], [])

    def test_one_a_process_holds_is_kept_and_named_with_why(self):
        lane, big = self.waiting("T-6004")
        rows, actions = self.rows(True, held=[os.path.join(lane, "target", "debug", "deps", "big.rlib")])
        self.assertTrue(os.path.isfile(big))
        self.assertEqual(rows["T-6004"]["severity"], "warn")
        self.assertIn("process", rows["T-6004"]["fix"])
        self.assertNotIn(("lane_target", lane), actions)

    def test_a_lane_some_process_stands_in_is_kept_and_named_with_why(self):
        # review: between builds a session in the lane holds nothing in target/, so only its working folder tells
        lane, big = self.waiting("T-6006")
        with unittest.mock.patch.object(lanes, "_busy_dirs", return_value=[os.path.join(lane, "crates")]):
            rows, actions = self.rows(True)
        self.assertTrue(os.path.isfile(big))
        self.assertEqual(rows["T-6006"]["severity"], "warn")
        self.assertIn("working in the lane", rows["T-6006"]["fix"])
        self.assertNotIn(("lane_target", lane), actions)

    def test_a_lane_an_open_task_still_names_is_not_finished(self):
        # review: focusing a task in a lane names it again, so a finished brief's lane can be another task's live one
        lane, big = self.waiting("T-6007")
        out = self.fm("task", "new", "Second work", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                      "--json").stdout
        b = c.find_brief(self.p, json.JSONDecoder().raw_decode(out)[0]["id"])
        b.meta["lane"] = lane
        c.save_brief(self.p, b)
        rows, actions = self.rows(True)
        self.assertEqual(rows["T-6007"]["severity"], "info")
        self.assertTrue(os.path.isfile(big))

    def test_an_unreadable_view_of_open_files_is_read_once_and_removes_nothing(self):
        a, x = self.waiting("T-6008")
        b, y = self.waiting("T-6009")
        from sampo import tidy
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=None) as read:
            found = lanes.cargo_cache_findings(self.p, True, [], tidy.finding)
        rows = {os.path.basename(f["lane"]): f for f in found if f["kind"] == "lane_target"}
        self.assertEqual(read.call_count, 1, "one read for the run, not one per lane")
        for name in ("T-6008", "T-6009"):
            self.assertEqual(rows[name]["severity"], "warn")
            self.assertIn("could not be read", rows[name]["fix"])
        self.assertTrue(os.path.isfile(x) and os.path.isfile(y))

    def test_doctor_counts_it_as_freeable_not_as_kept(self):
        lane, _ = self.waiting("T-6005")
        freed, kept = d.disk_reclaimables([(self.p, None)])
        mine = [n for label, n in freed if "T-6005" in label]
        self.assertEqual(len(mine), 1, freed)
        self.assertGreaterEqual(mine[0], BLOCK * 20)
        self.assertLess(sum(n for _, n in kept), BLOCK * 20, f"not counted twice: {kept}")


class TheOldSharedFolder(RustCase):
    """T-4575's one folder per project is retired: tidy removes that exact folder, and nothing around it."""

    def folders(self):
        return os.path.dirname(self.old)

    def test_the_exact_folder_goes_and_the_empty_parent_with_the_last_one(self):
        entry = self.old_entry()
        r = lanes.retire_cargo_shared(self.p, held=[])
        self.assertTrue(r["present"] and r["removed"], r)
        self.assertFalse(os.path.exists(entry))
        self.assertFalse(os.path.exists(self.folders()), "the empty cargo-target folder goes after the last project's")

    def test_another_projects_folder_stays_and_so_does_the_parent(self):
        self.old_entry()
        other = self.fake(os.path.join(self.folders(), "other-slug", "debug", "x.rlib"), 90)
        r = lanes.retire_cargo_shared(self.p, held=[])
        self.assertTrue(r["removed"])
        self.assertTrue(os.path.isfile(other))

    def test_a_report_removes_nothing_but_sizes_it(self):
        entry = self.old_entry(blocks=4)
        r = lanes.retire_cargo_shared(self.p, dry=True)
        self.assertTrue(r["present"])
        self.assertFalse(r["removed"])
        self.assertGreaterEqual(r["bytes"], BLOCK * 4)
        self.assertTrue(os.path.isfile(entry))

    def test_a_folder_a_process_holds_or_a_build_locks_stays(self):
        entry = self.old_entry()
        r = lanes.retire_cargo_shared(self.p, held=[os.path.join(self.old, "debug")])
        self.assertFalse(r["removed"])
        self.assertIn("holds it", r["why"])
        fd = os.open(self.fake(os.path.join(self.old, "debug", ".cargo-lock")), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        r = lanes.retire_cargo_shared(self.p, held=[])
        self.assertFalse(r["removed"])
        self.assertIn("lock", r["why"])
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=None):
            self.assertFalse(lanes.retire_cargo_shared(self.p)["removed"])
        self.assertTrue(os.path.isfile(entry))

    def test_a_link_is_left_alone(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        keep = self.fake(os.path.join(elsewhere, "debug", "x.rlib"), 90)
        os.makedirs(self.folders())
        os.symlink(elsewhere, self.old)
        r = lanes.retire_cargo_shared(self.p, held=[])
        self.assertFalse(r["removed"])
        self.assertTrue(os.path.isfile(keep))
        self.assertTrue(os.path.islink(self.old))

    def test_nothing_there_is_nothing_to_do(self):
        self.assertEqual(lanes.retire_cargo_shared(self.p, held=[])["present"], False)

    def test_tidy_lists_it_and_apply_removes_it(self):
        entry = self.old_entry(blocks=4)
        report = json.loads(self.fm("tidy", "--json", env={"HOME": self.fake_home}).stdout)
        found = [f for f in report["findings"] if f["kind"] == "cargo_shared"]
        self.assertEqual(len(found), 1, report)
        self.assertTrue(found[0]["auto"])
        self.assertTrue(os.path.isfile(entry), "a report removes nothing")
        self.fm("tidy", "--json", "--apply", env={"HOME": self.fake_home})
        # T-4820: where /proc is missing (macOS) the open files come from lsof (T-4764), so apply removes the folder
        # there too; it keeps it only where neither view can be had (the old rule, written before lsof)
        self.assertEqual(os.path.exists(entry), lanes._held_paths() is None)

    def test_without_proc_the_open_files_come_from_lsof_and_the_folder_goes_or_stays_by_what_it_says(self):
        # T-4820: the macOS path on any host: no /proc/self, an lsof that lists what is open (not this folder)
        entry = self.old_entry()
        real = os.path.isdir
        no_proc = unittest.mock.patch.object(lanes.os.path, "isdir", lambda p: False if p == "/proc/self" else real(p))
        with no_proc, unittest.mock.patch.object(lanes, "_lsof_paths", return_value=["/usr/lib/x", "/elsewhere/y"]):
            r = lanes.retire_cargo_shared(self.p)
        self.assertTrue(r["removed"], r)
        self.assertFalse(os.path.exists(entry))
        entry = self.old_entry()
        with no_proc, unittest.mock.patch.object(lanes, "_lsof_paths", return_value=[os.path.join(self.old, "debug", "x")]):
            r = lanes.retire_cargo_shared(self.p)
        self.assertFalse(r["removed"], "a file open inside it holds it")
        with no_proc, unittest.mock.patch.object(lanes, "_lsof_paths", return_value=None):
            self.assertFalse(lanes.retire_cargo_shared(self.p)["removed"], "no view: nothing is removed")
        self.assertTrue(os.path.isfile(entry))

    def test_apply_in_process_with_the_open_files_given(self):
        from sampo import tidy
        entry = self.old_entry()
        actions = []
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            found = lanes.cargo_cache_findings(self.p, True, actions, tidy.finding)
        self.assertEqual([f["kind"] for f in found], ["cargo_shared"])
        self.assertEqual(actions, [("cargo_shared", self.slug)])
        self.assertFalse(os.path.exists(entry))

    def test_a_held_one_is_said_not_hidden(self):
        from sampo import tidy
        self.old_entry()
        actions = []
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[os.path.join(self.old, "debug")]):
            found = lanes.cargo_cache_findings(self.p, True, actions, tidy.finding)
        self.assertEqual(actions, [])
        self.assertEqual(found[0]["severity"], "warn")
        self.assertIn("holds it", found[0]["fix"])


class TheOrphans(RustCase):
    """A lane cargo folder whose lane is gone (a throwaway gate worktree, a lane removed by hand) is removed after an hour."""

    def gone(self, name="gone"):
        return os.path.join(self.tmp, name)

    def test_a_folder_whose_lane_is_gone_and_unused_for_an_hour_goes(self):
        a = self.cfolder("g", 3, lane=self.gone())
        r = lanes.orphan_lane_folders(self.p, held=[])
        self.assertEqual([n for n, _ in r], self.names(a))
        self.assertFalse(os.path.exists(a))

    def test_a_dry_run_names_it_and_removes_nothing(self):
        a = self.cfolder("g", 3, lane=self.gone())
        self.assertEqual(len(lanes.orphan_lane_folders(self.p, held=[], dry=True)), 1)
        self.assertTrue(os.path.isdir(a))

    def test_a_folder_whose_lane_is_there_stays(self):
        lane = self.lane("T-5060", old=False)
        a = self.cfolder("T-5060", 30, lane=lane)
        self.assertEqual(lanes.orphan_lane_folders(self.p, held=[]), [])
        self.assertTrue(os.path.isdir(a))

    def test_a_fresh_one_stays(self):
        a = self.cfolder("g", 0.2, lane=self.gone())
        self.assertEqual(lanes.orphan_lane_folders(self.p, held=[]), [])
        self.assertTrue(os.path.isdir(a))

    def test_one_with_no_marker_is_left_to_the_cap(self):
        a = self.cfolder("g", 30, lane=self.gone(), marked=False)
        self.assertEqual(lanes.orphan_lane_folders(self.p, held=[]), [])
        self.assertTrue(os.path.isdir(a))

    def test_a_held_one_stays(self):
        a = self.cfolder("g", 30, lane=self.gone())
        self.assertEqual(lanes.orphan_lane_folders(self.p, held=[os.path.join(a, "debug")]), [])
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=None):
            self.assertEqual(lanes.orphan_lane_folders(self.p), [])
        self.assertTrue(os.path.isdir(a))

    def test_a_marker_that_is_not_a_path_is_no_reason_to_remove(self):
        a = self.cfolder("g", 30, lane=self.gone())
        mark = os.path.join(a, c.CARGO_LANE_MARK)
        for text in ("relative/path\n", "/two\nlines\n", ""):
            write(mark, text)
            os.utime(mark, (1_000_000_000, 1_000_000_000))
            self.assertEqual(lanes.orphan_lane_folders(self.p, held=[]), [], repr(text))
        self.assertTrue(os.path.isdir(a))

    def test_tidy_lists_them_and_apply_removes_them(self):
        from sampo import tidy
        a = self.cfolder("g", 3, lane=self.gone())
        found = lanes.cargo_cache_findings(self.p, False, [], tidy.finding)
        self.assertEqual([f["kind"] for f in found], ["cargo_lane_orphan"])
        self.assertTrue(os.path.isdir(a), "a report removes nothing")
        actions = []
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            lanes.cargo_cache_findings(self.p, True, actions, tidy.finding)
        self.assertEqual(actions, [("cargo_lane_orphan", 1)])
        self.assertFalse(os.path.exists(a))

    def test_tidy_apply_takes_t4717s_parent_config_away_and_leaves_the_users(self):
        from sampo import tidy
        write(self.parent_config, f"{c.CARGO_MARK_OLD}\n[build]\ntarget-dir = '{self.old}'\n")
        actions = []
        lanes.cargo_cache_findings(self.p, False, actions, tidy.finding)
        self.assertTrue(os.path.exists(self.parent_config), "a report removes nothing")
        lanes.cargo_cache_findings(self.p, True, actions, tidy.finding)
        self.assertEqual(actions, [("cargo_config", os.path.realpath(self.lane_root))])
        self.assertFalse(os.path.exists(self.parent_config))
        write(self.parent_config, "[term]\nverbose = true\n")
        lanes.cargo_cache_findings(self.p, True, [], tidy.finding)
        self.assertTrue(os.path.exists(self.parent_config))
