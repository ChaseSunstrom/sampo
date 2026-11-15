"""T-4717: the disk of a Rust project driven by Sampo kept filling (2026-11-15: seven builder worktrees held 36 GB of their
own target/ folders, the shared cargo cache 35 GB, the project's own target 61 GB; free space fell about 10 GB an hour).
T-4575 gave Sampo's own commands the one shared CARGO_TARGET_DIR, but a builder's own `cargo build` is not one of them, so
each lane still built into its own target/. Now: (1) ONE marked .cargo/config.toml in the folder that holds the project's
lanes (<repo>.lanes/, or <repo>/.claude/worktrees/ when git ignores it) names the shared folder, written when a lane is
made or focused (never on a hook's every call, no file in any lane, git's info/exclude untouched); (2) the shared cache
has a cap (10% of the disk, at most 40 GB, SAMPO_CARGO_CAP_BYTES overrides) and `sampo tidy --apply` prunes it oldest
first, never an entry used in the last hour, held by a live process or below a held .cargo-lock, never outside the cache;
(3) a landed lane's own target/ is gone, only when it is proven cargo's output (CACHEDIR.TAG, nothing tracked);
(4) doctor and tidy name these folders with their sizes; (5) SAMPO_CARGO_SHARED=0 turns the sharing off.

Safety: every folder is the test's own temp folder with fake files (no cargo runs, no real Sampo state, no real project);
the machine's CARGO_* variables and ~/.cargo are never read (patched to scratch ones); a prune is only ever pointed at a
scratch cache."""
import fcntl
import json
import os
import subprocess
import time
import unittest.mock

from helpers import ForemanTestCase  # noqa: F401  (the base of DiskCase)
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
        self.shared = c.cargo_shared_dir(self.p.slug)
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

    def entry(self, rel, hours_old, blocks=1, root=None):
        return self.fake(os.path.join(root or self.shared, *rel.split("/")), hours_old, blocks)

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

    def cap_leaving(self, *gone):
        """A cap that is exactly what the cache holds once the entries `gone` (relative paths) are removed, whatever
        the filesystem allocates for a folder."""
        info, total = lanes._cache_tree(self.shared, time.monotonic() + 30)
        return total - sum(info[os.path.join(self.shared, *g.split("/"))][0] for g in gone)

    def cap(self, n):
        patch = unittest.mock.patch.dict(os.environ, {CAP: str(n)})
        patch.start()
        self.addCleanup(patch.stop)

    def prune(self, **kw):
        kw.setdefault("held", [])  # no live process holds a fake folder
        return lanes.prune_cargo_cache(self.p, **kw)


class TheLaneConfig(RustCase):
    """A builder's own cargo commands reach the shared folder through ONE config in the lanes' parent folder."""

    def test_the_lanes_parent_folder_gets_one_marked_config_naming_the_shared_folder(self):
        lane = self.lane("T-5001", old=False)
        path = c.lane_cargo_config(lane)
        self.assertEqual(path, self.parent_config)
        self.assertEqual(c._cargo_config_dir(path), self.shared)
        self.assertTrue(c.cargo_config_ours(path))
        self.assertEqual(c.lane_cargo_config(lane), path, "a second call finds it in place")
        self.assertFalse(os.path.exists(os.path.join(lane, ".cargo")), "no file in the lane")

    def test_every_lane_shares_the_one_file(self):
        a, b = self.lane("T-5011", old=False), self.lane("T-5012", old=False)
        self.assertEqual(c.lane_cargo_config(a), c.lane_cargo_config(b))
        self.assertEqual(os.listdir(os.path.dirname(self.parent_config)), ["config.toml"])

    def test_git_sees_nothing_and_its_exclude_file_is_not_touched(self):
        exclude = os.path.join(self.repo, ".git", "info", "exclude")
        before = open(exclude, encoding="utf-8").read() if os.path.exists(exclude) else None
        lane = self.lane("T-5002", old=False)
        c.lane_cargo_config(lane)
        self.assertEqual(git(lane, "status", "--porcelain"), "")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(lanes._left_work(lane), [], "nothing the lane would lose")
        self.assertEqual(open(exclude, encoding="utf-8").read() if os.path.exists(exclude) else None, before)

    def test_the_lane_still_names_the_shared_folder_with_the_file_in_place(self):
        lane = self.lane("T-5003", old=False)
        c.lane_cargo_config(lane)
        self.assertEqual(c.cargo_target(lane, self.p.slug), (self.shared, ""), "Sampo's own file is not the user's choice")

    def test_a_lane_without_rust_gets_nothing(self):
        lane = self.lane("T-5004", old=False)
        os.unlink(os.path.join(self.repo, "Cargo.toml"))
        git(self.repo, "commit", "-qam", "no rust")
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertFalse(os.path.exists(self.parent_config))

    def test_the_main_checkout_is_never_given_one(self):
        self.assertEqual(c.lane_cargo_config(self.repo), "")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".cargo")))

    def test_a_folder_that_is_not_a_sampo_lane_is_left_alone(self):
        for where in (os.path.join(self.tmp, "stray-worktree"), os.path.join(self.repo, "inside-main")):
            git(self.repo, "worktree", "add", "-q", "-b", os.path.basename(where), where)
            self.assertEqual(c.lane_cargo_config(where), "", "not under <repo>.lanes/ or <repo>/.claude/worktrees/")
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(where), ".cargo", "config.toml")))

    def test_the_users_choice_stands_and_nothing_is_written(self):
        lane = self.lane("T-5005", old=False)
        with unittest.mock.patch.dict(os.environ, {VAR: "/mine/t"}):
            self.assertEqual(c.lane_cargo_config(lane), "")
        write(os.path.join(self.repo, ".cargo", "config.toml"), '[build]\ntarget-dir = "/mine/cfg"\n')
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertFalse(os.path.exists(self.parent_config))

    def test_a_choice_in_a_folder_above_the_main_checkout_stands_too(self):
        lane = self.lane("T-5013", old=False)
        write(os.path.join(self.tmp, ".cargo", "config.toml"), '[build]\ntarget-dir = "/mine/above"\n')
        shared, why = c.cargo_target(lane, self.p.slug)
        self.assertIsNone(shared)
        self.assertIn("/mine/above", why)
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertFalse(os.path.exists(self.parent_config))

    def test_a_choice_made_after_our_file_was_written_removes_the_file(self):
        lane = self.lane("T-5014", old=False)
        c.lane_cargo_config(lane)
        write(os.path.join(self.repo, ".cargo", "config.toml"), '[build]\ntarget-dir = "/mine/later"\n')
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertFalse(os.path.exists(self.parent_config), "Sampo's file must not outrank the user's choice")

    def test_something_of_the_users_in_the_parent_folder_is_never_overwritten(self):
        lane = self.lane("T-5006", old=False)
        for name, text in (("config.toml", "[term]\nverbose = true\n"), ("config", "[term]\nquiet = true\n")):
            mine = os.path.join(self.lane_root, ".cargo", name)
            write(mine, text)
            self.assertEqual(c.lane_cargo_config(lane), "", "env-only fallback: the builder brief's export")
            with open(mine, encoding="utf-8") as f:
                self.assertEqual(f.read(), text)
            os.unlink(mine)

    def test_a_linked_cargo_folder_is_left_alone(self):
        lane = self.lane("T-5015", old=False)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.lane_root, ".cargo"))
        self.assertEqual(c.lane_cargo_config(lane), "")
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_path_with_a_quote_or_a_line_break_is_refused_and_a_backslash_is_kept(self):
        lane = self.lane("T-5016", old=False)
        for bad in ("it's", "two\nlines", "cr\rhere"):
            with unittest.mock.patch.object(c, "cargo_shared_dir", return_value=os.path.join(self.tmp, bad)):
                self.assertEqual(c.lane_cargo_config(lane), "", repr(bad))
            self.assertFalse(os.path.exists(self.parent_config))
        odd = os.path.join(self.tmp, "back\\slash")
        with unittest.mock.patch.object(c, "cargo_shared_dir", return_value=odd):
            path = c.lane_cargo_config(lane)
        self.assertEqual(c._cargo_config_dir(path), odd, "a TOML literal string takes a backslash as it is")

    def test_the_off_switch_turns_sharing_off_and_takes_our_file_away(self):
        lane = self.lane("T-5017", old=False)
        c.lane_cargo_config(lane)
        for name in ("FOREMAN_CARGO_SHARED", SHARED):  # mirror_env moves the SAMPO_ name to the FOREMAN_ one at start
            with unittest.mock.patch.dict(os.environ, {name: "0"}):
                self.assertEqual(c.cargo_target(lane, self.p.slug)[0], None, name)
                self.assertIn("SAMPO_CARGO_SHARED=0", c.cargo_target(lane, self.p.slug)[1])
                self.assertEqual(c.lane_cargo_config(lane), "", name)
                self.assertFalse(os.path.exists(self.parent_config), name)
            c.lane_cargo_config(lane)  # back on
            self.assertTrue(os.path.exists(self.parent_config))
        with unittest.mock.patch.dict(os.environ, {SHARED: "1"}):
            self.assertEqual(c.cargo_target(lane, self.p.slug)[0], self.shared, "only 0 turns it off")

    def test_a_lane_under_claude_worktrees_gets_one_only_where_git_ignores_it(self):
        home = os.path.join(self.repo, ".claude", "worktrees")
        lane = self.lane("T-5018", old=False, parent=home)
        self.assertEqual(c.lane_cargo_config(lane), "", "git would list it: env-only fallback, the main checkout stays clean")
        self.assertFalse(os.path.exists(os.path.join(home, ".cargo")))
        write(os.path.join(self.repo, ".gitignore"), "target/\n.claude/worktrees/\n")
        git(self.repo, "commit", "-qam", "ignore the lanes")
        path = c.lane_cargo_config(lane)
        self.assertEqual(path, os.path.join(home, ".cargo", "config.toml"))
        self.assertNotIn(".claude/worktrees", git(self.repo, "status", "--porcelain"))

    def test_a_builders_bash_calls_write_and_spawn_nothing(self):
        lane = self.lane("T-5008", old=False)
        with unittest.mock.patch.object(c, "lane_cargo_config", side_effect=AssertionError("per-call work")):
            r = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": lane})
        self.assertEqual(r.returncode, 0, r.stderr)  # (the hook runs in another process: the file check is the proof)
        self.assertFalse(os.path.exists(self.parent_config), "the hook leaves the config to the lane's creation and focus")

    def test_a_lane_made_by_fm_gets_it(self):
        tid = json.loads(self.fm("task", "new", "Rust work", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        self.fm("lane", "new", tid)
        self.assertEqual(c._cargo_config_dir(self.parent_config), self.shared)
        self.assertFalse(os.path.exists(os.path.join(self.lane_root, tid, ".cargo")))


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
    def test_the_oldest_entries_go_first_until_the_cache_is_under_its_cap(self):
        for name, age in (("a-1", 30), ("b-2", 20), ("c-3", 10), ("d-4", 5)):
            self.entry(f"debug/incremental/{name}/s.bin", age)
        self.cap(self.cap_leaving("debug/incremental/a-1", "debug/incremental/b-2"))
        r = self.prune()
        left = sorted(os.listdir(os.path.join(self.shared, "debug", "incremental")))
        self.assertEqual(left, ["c-3", "d-4"], "the two oldest went, the rest fit the cap")
        self.assertEqual([x[0] for x in r["removed"]], ["debug/incremental/a-1", "debug/incremental/b-2"])
        self.assertLessEqual(r["after"], r["cap"])
        self.assertGreater(r["before"], r["after"])

    def test_nothing_goes_when_it_is_under_its_cap(self):
        self.entry("debug/incremental/a-1/s.bin", 30)
        self.cap(10 * BLOCK)
        self.assertEqual(self.prune()["removed"], [])
        self.assertTrue(os.path.isfile(os.path.join(self.shared, "debug", "incremental", "a-1", "s.bin")))

    def test_an_entry_used_in_the_last_hour_is_never_removed(self):
        self.entry("debug/incremental/old/s.bin", 5)
        self.entry("debug/incremental/fresh/s.bin", 0.2)
        self.entry("debug/deps/fresh.rlib", 0.5)
        self.cap(1)
        r = self.prune()
        self.assertEqual([x[0] for x in r["removed"]], ["debug/incremental/old"])
        self.assertTrue(os.path.isfile(os.path.join(self.shared, "debug", "incremental", "fresh", "s.bin")))
        self.assertTrue(os.path.isfile(os.path.join(self.shared, "debug", "deps", "fresh.rlib")))
        self.assertGreater(r["after"], r["cap"], "over its cap, and said so: nothing else was safe to remove")

    def test_a_new_file_deep_in_an_old_entry_keeps_the_whole_entry(self):
        self.entry("debug/incremental/mixed/old.bin", 40)
        self.entry("debug/incremental/mixed/sub/new.bin", 0.1)
        self.cap(1)
        self.assertEqual(self.prune()["removed"], [])
        self.assertTrue(os.path.isfile(os.path.join(self.shared, "debug", "incremental", "mixed", "old.bin")))

    def test_an_entry_a_live_process_holds_is_never_removed(self):
        held = self.entry("debug/incremental/held/s.bin", 50)
        self.entry("debug/incremental/free/s.bin", 40)
        self.cap(1)
        r = self.prune(held=[held])
        self.assertEqual([x[0] for x in r["removed"]], ["debug/incremental/free"])
        self.assertTrue(os.path.isfile(held))

    def test_a_process_standing_in_an_entry_holds_it(self):
        inside = os.path.join(self.shared, "debug", "incremental", "cwd")
        self.entry("debug/incremental/cwd/s.bin", 50)
        self.cap(1)
        self.assertEqual(self.prune(held=[inside])["removed"], [])

    def lock(self, rel):
        """A cargo build's lock: a file some other open description holds an exclusive flock on."""
        path = self.entry(rel, 80)
        fd = os.open(path, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return path

    def test_a_profile_whose_cargo_lock_is_held_is_skipped_in_every_stage(self):
        self.lock("debug/.cargo-lock")
        self.entry("debug/incremental/a/s.bin", 50)
        self.entry("debug/deps/x.rlib", 50)
        self.entry("release/deps/y.rlib", 60)
        self.cap(1)
        r = self.prune()
        for kept in ("debug/incremental/a/s.bin", "debug/deps/x.rlib", "debug/.cargo-lock"):
            self.assertTrue(os.path.isfile(os.path.join(self.shared, *kept.split("/"))), kept)
        self.assertFalse(os.path.exists(os.path.join(self.shared, "release", "deps", "y.rlib")), "the idle profile goes")
        self.assertTrue(any("cargo build" in why for _, why in r["skipped"]), r)
        self.assertFalse(any(rel.startswith("debug") for rel, _ in r["removed"]), r)

    def test_a_lock_below_a_target_triple_folder_keeps_the_whole_triple(self):
        self.lock("x86_64-unknown-linux-gnu/debug/.cargo-lock")
        self.entry("x86_64-unknown-linux-gnu/debug/deps/x.rlib", 70, blocks=4)
        self.cap(1)
        r = self.prune()
        self.assertTrue(os.path.isfile(os.path.join(self.shared, "x86_64-unknown-linux-gnu", "debug", "deps", "x.rlib")), r)

    def test_a_lock_that_cannot_be_read_counts_as_held(self):
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        path = self.entry("debug/.cargo-lock", 80)
        os.chmod(path, 0)
        self.entry("debug/deps/x.rlib", 50)
        self.cap(1)
        self.assertEqual(self.prune()["removed"], [])
        self.assertTrue(os.path.isfile(os.path.join(self.shared, "debug", "deps", "x.rlib")))

    def test_a_free_lock_file_is_no_reason_to_keep_a_profile(self):
        self.entry("debug/.cargo-lock", 80)
        self.entry("debug/deps/x.rlib", 50)
        self.cap(1)
        self.assertTrue(self.prune()["removed"])
        self.assertFalse(os.path.exists(os.path.join(self.shared, "debug", "deps", "x.rlib")))

    def test_the_holders_are_read_again_before_each_stage(self):
        self.entry("debug/incremental/a/s.bin", 50)
        deps = self.entry("debug/deps/x.rlib", 50)
        calls = []

        def holders():  # a build starts in the deps folder after the incremental stage
            calls.append(1)
            return [] if len(calls) <= 2 else [deps]
        self.cap(1)
        with unittest.mock.patch.object(lanes, "_held_paths", holders):
            r = lanes.prune_cargo_cache(self.p)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual([x[0] for x in r["removed"]], ["debug/incremental/a"])
        self.assertTrue(os.path.isfile(deps))

    def test_without_a_view_of_open_files_nothing_is_removed(self):
        self.entry("debug/incremental/a/s.bin", 50)
        self.cap(1)
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=None):
            r = lanes.prune_cargo_cache(self.p)
        self.assertEqual(r["removed"], [])
        self.assertIn("open files", r["why"])
        self.assertTrue(os.path.isfile(os.path.join(self.shared, "debug", "incremental", "a", "s.bin")))

    def test_old_build_files_go_oldest_first_after_the_incremental_entries(self):
        self.entry("debug/incremental/a/s.bin", 3)
        self.entry("debug/deps/x-1.rlib", 90)
        self.entry("debug/deps/x-2.rlib", 60)
        self.entry("debug/deps/x-3.rlib", 30)
        info, total = lanes._cache_tree(self.shared, time.monotonic() + 30)  # a folder's own blocks count in a size too
        gone = info[os.path.join(self.shared, "debug", "incremental", "a")][0] + info[os.path.join(self.shared, "debug", "deps", "x-1.rlib")][0]
        self.cap(total - gone)
        r = self.prune()
        self.assertEqual([x[0] for x in r["removed"]], ["debug/incremental/a", "debug/deps/x-1.rlib"],
                         f"four blocks to two: the incremental entry, then the oldest build file {r}")
        self.assertEqual(sorted(os.listdir(os.path.join(self.shared, "debug", "deps"))), ["x-2.rlib", "x-3.rlib"])

    def test_a_whole_old_profile_folder_goes_last(self):
        self.entry("release/build/x/out.bin", 70, blocks=4)
        self.entry("debug/deps/x.rlib", 0.1)
        self.cap(BLOCK * 2)
        r = self.prune()
        self.assertEqual([x[0] for x in r["removed"]], ["release"])
        self.assertTrue(os.path.isfile(os.path.join(self.shared, "debug", "deps", "x.rlib")))

    def test_a_link_is_never_followed_and_nothing_outside_the_cache_is_touched(self):
        outside = os.path.join(self.tmp, "precious")
        keep = self.fake(os.path.join(outside, "keep.bin"), 99)
        self.entry("debug/incremental/real/s.bin", 50)
        os.symlink(outside, os.path.join(self.shared, "debug", "incremental", "link"))
        os.symlink(outside, os.path.join(self.shared, "release"))
        os.symlink(keep, os.path.join(self.shared, "debug", "deps-link.rlib"))
        self.cap(1)
        self.prune()
        self.assertTrue(os.path.isfile(keep), "a link in the cache points outside: never followed, never removed")

    def test_a_projects_own_target_and_a_lanes_files_are_never_touched(self):
        mine = self.fake(os.path.join(self.repo, "target", "debug", "deps", "x.rlib"), 500)
        lane = self.lane("T-5010", old=False)
        own = self.fake(os.path.join(lane, "target", "debug", "deps", "y.rlib"), 500)
        src = self.fake(os.path.join(lane, "src", "main.rs"), 500)
        self.entry("debug/incremental/a/s.bin", 50)
        self.cap(1)
        self.prune()
        for kept in (mine, own, src):
            self.assertTrue(os.path.isfile(kept), kept)

    def test_only_a_registered_projects_folder_is_pruned(self):
        other = self.entry("debug/incremental/a/s.bin", 50, root=os.path.join(os.path.dirname(self.shared), "not-registered"))
        self.entry("debug/incremental/a/s.bin", 50)
        self.cap(1)
        self.prune()
        self.assertTrue(os.path.isfile(other))
        self.assertFalse(os.path.exists(os.path.join(self.shared, "debug", "incremental", "a")))

    def test_a_cache_folder_that_is_a_link_is_not_pruned(self):
        target = os.path.join(self.tmp, "elsewhere")
        keep = self.fake(os.path.join(target, "debug", "incremental", "a", "s.bin"), 50)
        os.makedirs(os.path.dirname(self.shared), exist_ok=True)
        os.symlink(target, self.shared)
        self.cap(1)
        self.assertEqual(self.prune()["removed"], [])
        self.assertTrue(os.path.isfile(keep))

    def test_a_missing_cache_is_nothing_to_do(self):
        self.cap(1)
        r = self.prune()
        self.assertEqual((r["removed"], r["before"]), ([], 0))

    def test_a_second_run_finds_it_done(self):
        for i in range(4):
            self.entry(f"debug/incremental/e{i}/s.bin", 10 + i)
        self.cap(BLOCK * 2)
        first = self.prune()
        again = self.prune()
        self.assertTrue(first["removed"])
        self.assertEqual(again["removed"], [])

    def test_a_pass_that_runs_out_of_time_removes_nothing_it_did_not_size(self):
        for i in range(3):
            self.entry(f"debug/incremental/e{i}/s.bin", 10 + i)
        self.cap(1)
        r = self.prune(budget=-1)
        self.assertEqual(r["removed"], [])
        self.assertTrue(r["why"])
        self.assertTrue(r["unsized"])
        self.assertEqual(len(os.listdir(os.path.join(self.shared, "debug", "incremental"))), 3)


class TheLaneOwnTarget(RustCase):
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

    def test_the_landing_removes_it_after_the_record_and_without_sizing_it(self):
        lane = self.lane("T-5029", old=False)
        big = self.grow(lane)
        b = unittest.mock.Mock(meta={"lane": lane})
        with unittest.mock.patch.object(lanes, "dir_bytes", side_effect=AssertionError("sized the folder first")), \
                unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            self.assertTrue(lanes.drop_landed_target(self.p, b, self.repo))
        self.assertFalse(os.path.exists(big))

    def test_the_merge_drops_the_target_after_it_records_the_merge(self):
        import inspect
        src = inspect.getsource(lanes.merge)
        self.assertLess(src.index("pm_record(p, c.git_head(main)"), src.index("drop_landed_target(p, b, main)"),
                        "a slow removal never holds the record back")


class DoctorAndTidyNameThem(RustCase):
    def test_doctor_names_each_lane_target_folder_and_the_cache_against_its_cap(self):
        a = self.lane("T-5030", old=False)
        b = self.lane("agent-b", old=False)
        self.target(a, 150)  # 1.2 MB of files
        self.target(b, 60)  # 0.5 MB of files
        self.entry("debug/deps/s.rlib", 5, blocks=30)  # 245 KB of files
        self.cap(2_000_000)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertEqual(r.status, "WARN")
        for text in (f"T-5030 target {lanes.human(self.disk(os.path.join(a, 'target')))}",
                     f"agent-b target {lanes.human(self.disk(os.path.join(b, 'target')))}",
                     f"shared cargo cache {lanes.human(self.disk(self.shared))} of 2.0 MB"):
            self.assertIn(text, r.detail)
        self.assertGreaterEqual(self.disk(os.path.join(b, "target")), BLOCK * 60, "at least the bytes the test wrote")
        self.assertIn("lane build output (2 target folders, shared)", r.detail, "the existing label stands")

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
            self.entry(f"debug/incremental/e{i}/s.bin", age)
        self.cap(BLOCK * 2)
        found = lanes.cargo_cache_findings(self.p, False, [], tidy.finding)
        self.assertEqual([f["kind"] for f in found], ["cargo_cache"])

    def test_tidy_says_it_could_not_size_the_cache_when_the_sizing_runs_out(self):
        from sampo import tidy
        self.entry("debug/incremental/e0/s.bin", 40)
        with unittest.mock.patch.object(lanes, "TIDY_SCAN_S", -1):
            found = lanes.cargo_cache_findings(self.p, False, [], tidy.finding)
        self.assertEqual([f["kind"] for f in found], ["cargo_cache"])
        self.assertIn("could not size", found[0]["detail"])

    def test_tidy_lists_a_cache_over_its_cap_and_apply_prunes_it_oldest_first(self):
        for i, age in enumerate((40, 30, 20, 10)):
            self.entry(f"debug/incremental/e{i}/s.bin", age)
        cap = self.cap_leaving("debug/incremental/e0", "debug/incremental/e1")
        env = {"HOME": self.fake_home, CAP: str(cap)}
        report = json.loads(self.fm("tidy", "--json", env=env).stdout)  # a process mirrors SAMPO_X to FOREMAN_X at start
        found = [f for f in report["findings"] if f["kind"] == "cargo_cache"]
        self.assertEqual(len(found), 1, report)
        self.assertGreater(found[0]["bytes"], cap)
        self.assertTrue(found[0]["auto"])
        self.assertTrue(os.path.isdir(os.path.join(self.shared, "debug", "incremental", "e0")), "a report removes nothing")
        self.fm("tidy", "--json", "--apply", env=env)
        left = sorted(os.listdir(os.path.join(self.shared, "debug", "incremental")))
        # a host without /proc (macOS) cannot say what a process holds: the prune removes nothing there (its own test:
        # test_without_a_view_of_open_files_nothing_is_removed); the in-process test below prunes with a view given
        self.assertEqual(left, ["e2", "e3"] if os.path.isdir("/proc/self") else ["e0", "e1", "e2", "e3"])

    def test_tidy_apply_prunes_oldest_first_with_the_open_files_given(self):
        """The same prune on every host: the holders are given (nothing is held), so /proc is not needed."""
        from sampo import tidy
        for i, age in enumerate((40, 30, 20, 10)):
            self.entry(f"debug/incremental/e{i}/s.bin", age)
        self.cap(self.cap_leaving("debug/incremental/e0", "debug/incremental/e1"))
        actions = []
        with unittest.mock.patch.object(lanes, "_held_paths", return_value=[]):
            found = lanes.cargo_cache_findings(self.p, True, actions, tidy.finding)
        self.assertEqual([f["kind"] for f in found], ["cargo_cache"])
        self.assertEqual(actions, [("cargo_cache", self.p.slug)])
        self.assertEqual(sorted(os.listdir(os.path.join(self.shared, "debug", "incremental"))), ["e2", "e3"])

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

    def test_a_project_without_rust_gets_neither_line(self):
        os.unlink(os.path.join(self.repo, "Cargo.toml"))
        git(self.repo, "commit", "-qam", "no rust")
        report = json.loads(self.fm("tidy", "--json", env={"HOME": self.fake_home}).stdout)
        self.assertEqual([f for f in report["findings"] if f["kind"] in ("cargo_cache", "lane_target")], [])
