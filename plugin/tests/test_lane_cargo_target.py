"""T-4575, T-4764: builder lanes of a Rust project each compiled into their own target folder in the lane (2026-11-13: lane
worktrees grew 8.6 GB to 15 GB in 40 minutes and twice took the disk to 99%). A project whose root holds a Cargo.toml now
gives each lane a CARGO_TARGET_DIR of its own outside the repo (one folder shared by two lanes lets a lane run the other's
compiled crate: cargo issue 12516, T-4764): the builder brief says how the lane finds it, the commands Sampo spawns for a
lane carry it, a value the user already set (the environment, or build.target-dir in a .cargo/config.toml) is kept and
said, a project without a Cargo.toml gets neither, and sampo doctor's disk line counts lane build output among what can
be freed.

Safety: fixture projects hold a Cargo.toml file only (no Rust toolchain runs); every folder is the test's own temp
folder; the machine's CARGO_* variables and ~/.cargo are never read (patched to scratch ones)."""
import argparse
import collections
import os
import subprocess
import unittest.mock

from helpers import ForemanTestCase
from test_disk_pressure import DiskCase

from sampo import core as c
from sampo import doctor as d
from sampo import lanes

Usage = collections.namedtuple("Usage", "total used free")
VAR = "CARGO_TARGET_DIR"


def alone(case):
    """The machine's own Cargo settings are not a fixture's: no CARGO_* variable, a scratch home for ~/.cargo."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CARGO") and not k.startswith("SAMPO_CARGO")
             and not k.startswith("FOREMAN_CARGO")}
    clean.update(HOME=os.path.join(case.tmp, "home-x"), CARGO_HOME=os.path.join(case.tmp, "cargo-home"))
    patch = unittest.mock.patch.dict(os.environ, clean, clear=True)
    patch.start()
    case.addCleanup(patch.stop)


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Base(ForemanTestCase):
    cargo = True

    def setUp(self):
        super().setUp()
        alone(self)
        if self.cargo:
            write(os.path.join(self.repo, "Cargo.toml"), '[package]\nname = "fixture"\nversion = "0.1.0"\n')
            subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", self.repo, "commit", "-qm", "rust"], check=True, capture_output=True)
        self.fm("init")
        self.p = c.find_project(self.repo)

    def brief(self):
        tid = self.task()
        lanes.builder_brief(self.p, c.find_brief(self.p, tid), argparse.Namespace(model=None, json=False))
        with open(os.path.join(self.p.dir, "audits", f"{tid}.builder.md"), encoding="utf-8") as f:
            return f.read()

    def task(self):
        import json
        tid = json.loads(self.fm("task", "new", "Build it", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        self.fm("task", "set", tid, "approved=true")
        return tid

    def lane(self, name="T-9"):
        wt = os.path.join(self.tmp, "lanes", name)
        lanes._git(self.repo, "worktree", "add", "-q", "-b", f"foreman/{name}", wt)
        return os.path.realpath(wt)

    def lane_p(self, wt):
        return c.Project(self.p.slug, wt, self.p.dir, lane=wt)

    def folder(self, wt):
        return c.cargo_lane_dir(self.p.slug, wt)


class TheLaneFolder(Base):
    def test_a_lane_of_a_rust_project_has_a_folder_outside_the_repo_and_its_lanes(self):
        wt = self.lane()
        folder = lanes.lane_env(self.lane_p(wt))[VAR]
        self.assertTrue(os.path.isabs(folder))
        self.assertEqual(folder, self.folder(wt))
        for inside in (self.repo, self.repo + ".lanes", self.tmp + os.sep + "lanes"):
            self.assertFalse(folder.startswith(inside + os.sep), f"{folder} is inside {inside}")
        self.assertTrue(folder.startswith(c.state_dir() + os.sep), "under Sampo's state, which sync never copies whole")
        self.assertEqual(folder, lanes.lane_env(self.lane_p(wt))[VAR], "the same folder on every call")

    def test_two_lanes_are_handed_two_folders(self):
        one, two = self.lane("T-1"), self.lane("T-2")
        self.assertNotEqual(lanes.lane_env(self.lane_p(one))[VAR], lanes.lane_env(self.lane_p(two))[VAR])

    def test_the_brief_says_where_the_lane_finds_its_folder(self):
        text = self.brief()
        self.assertIn(f"export {VAR}=", text, "names the line `sampo focus` prints")
        self.assertIn("sampo focus", text)
        self.assertIn("own", text)
        self.assertLess(text.index(VAR), text.index("## Your contract"), "named before the contract, with the rules")

    def test_lane_commands_sampo_spawns_carry_it(self):
        wt = self.lane()
        for env in (lanes.cpu_env(self.lane_p(wt)), lanes.cpu_env(self.lane_p(wt), gate=False), lanes.lane_env(self.lane_p(wt))):
            self.assertEqual(env[VAR], self.folder(wt))

    def test_the_main_checkouts_project_adds_nothing(self):
        self.assertNotIn(VAR, lanes.lane_env(self.p))
        self.assertNotIn(VAR, lanes.cpu_env(self.p))

    def out(self, root, env=None):
        code, text = c.run_command(root, 'echo "[${CARGO_TARGET_DIR}]"', timeout=60, env=env)
        self.assertEqual(code, 0, text)
        return text.strip()

    def test_a_command_run_in_a_lane_carries_it_and_one_in_the_main_checkout_does_not(self):
        wt = self.lane()
        self.assertEqual(self.out(wt), f"[{self.folder(wt)}]", "the evidence run and the criterion re-run of a lane")
        self.assertEqual(self.out(self.repo), "[]", "the main checkout builds where the user always built")
        self.assertEqual(self.out(wt, env={"PATH": os.environ["PATH"]}), f"[{self.folder(wt)}]", "an explicit env too")
        self.assertEqual(self.out(wt, env={"PATH": os.environ["PATH"], VAR: "/mine/t"}), "[/mine/t]", "a value is kept")

    def test_a_stale_value_from_an_older_run_is_replaced(self):
        wt = self.lane()
        for stale in (c.cargo_shared_dir(self.p.slug), c.cargo_lane_dir(self.p.slug, os.path.join(self.tmp, "gone"))):
            env = {"PATH": os.environ["PATH"], VAR: stale}
            self.assertEqual(self.out(wt, env=env), f"[{self.folder(wt)}]", f"{stale} was ours, not the user's")

    def test_the_focus_line_exports_the_lanes_own_folder(self):
        wt = self.lane()
        line = c.cargo_focus_line(wt, self.p.slug)
        self.assertIn(f"export {VAR}=", line)
        self.assertIn(self.folder(wt), line)
        self.assertEqual(c.cargo_focus_line(self.repo, self.p.slug), "", "the main checkout has none")

    def test_the_folder_is_marked_with_its_lane_once(self):
        wt = self.lane()
        lanes.lane_env(self.lane_p(wt))
        mark = os.path.join(self.folder(wt), c.CARGO_LANE_MARK)
        with open(mark, encoding="utf-8") as f:
            self.assertEqual(f.read(), wt + "\n")
        os.utime(mark, (1_000_000_000, 1_000_000_000))
        lanes.lane_env(self.lane_p(wt))
        self.assertEqual(os.stat(mark).st_mtime, 1_000_000_000, "not rewritten on a second run")


class WhatTheUserSet(Base):
    def target(self, wt, env=None):
        return c.cargo_target(wt, self.p.slug, env)[0]

    def test_a_value_in_the_environment_is_kept_and_said(self):
        wt = self.lane()
        with unittest.mock.patch.dict(os.environ, {VAR: "/mine/target"}):
            self.assertEqual(self.target(wt), None)
            self.assertEqual(lanes.cpu_env(self.lane_p(wt))[VAR], "/mine/target")
            text = self.brief()
        self.assertIn("/mine/target", text)
        self.assertIn("keep", text.lower())
        self.assertNotIn(f"export {VAR}=", text)

    def test_the_other_name_cargo_reads_is_kept_too(self):
        wt = self.lane()
        with unittest.mock.patch.dict(os.environ, {"CARGO_BUILD_TARGET_DIR": "/mine/other"}):
            self.assertIsNone(self.target(wt))
            self.assertNotIn(VAR, lanes.cpu_env(self.lane_p(wt)))

    def test_a_target_dir_in_the_projects_cargo_config_is_kept_and_said(self):
        wt = self.lane()
        write(os.path.join(self.repo, ".cargo", "config.toml"), '[build]\ntarget-dir = "/mine/cfg"\n')
        self.assertIsNone(self.target(wt))
        self.assertNotIn(VAR, lanes.cpu_env(self.lane_p(wt)))
        text = self.brief()
        self.assertIn(".cargo", text)
        self.assertNotIn(f"export {VAR}=", text)

    def test_the_older_config_name_and_the_users_own_config_count_too(self):
        wt = self.lane()
        write(os.path.join(self.repo, ".cargo", "config"), "[build]\ntarget-dir = 'x'\n")
        self.assertIsNone(self.target(wt))
        os.unlink(os.path.join(self.repo, ".cargo", "config"))
        self.assertIsNotNone(self.target(wt))
        write(os.path.join(os.environ["CARGO_HOME"], "config.toml"), '[build]\ntarget-dir = "/mine/user"\n')
        self.assertIsNone(self.target(wt))

    def test_a_cargo_config_without_a_target_dir_changes_nothing(self):
        wt = self.lane()
        write(os.path.join(self.repo, ".cargo", "config.toml"), '[build]\njobs = 4\n[alias]\nb = "build"\n')
        self.assertEqual(self.target(wt), self.folder(wt))

    def test_a_torn_cargo_config_is_not_a_crash(self):
        wt = self.lane()
        write(os.path.join(self.repo, ".cargo", "config.toml"), "[build\ntarget-dir = \n")
        self.assertIsNotNone(self.target(wt))
        self.assertIn(VAR, self.brief())

    def test_an_empty_variable_is_not_a_value(self):
        wt = self.lane()
        with unittest.mock.patch.dict(os.environ, {VAR: ""}):
            self.assertIsNotNone(self.target(wt))
            self.assertEqual(lanes.cpu_env(self.lane_p(wt))[VAR], self.folder(wt))

    def test_the_switch_off_hands_out_nothing_and_says_so(self):
        wt = self.lane()
        with unittest.mock.patch.dict(os.environ, {"SAMPO_CARGO_SHARED": "0"}):
            dir_, note = c.cargo_target(wt, self.p.slug)
            self.assertIsNone(dir_)
            self.assertIn("SAMPO_CARGO_SHARED=0", note)
            self.assertNotIn(VAR, lanes.cpu_env(self.lane_p(wt)))
            self.assertIn("SAMPO_CARGO_SHARED=0", self.brief())

    def test_a_wrapper_comes_from_sccache_only_when_the_user_set_none(self):
        wt = self.lane()
        bindir = os.path.join(self.tmp, "bin")
        fake = os.path.join(bindir, "sccache")
        write(fake, "#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)
        env = {"PATH": bindir + os.pathsep + os.environ["PATH"]}
        self.assertEqual(c.cargo_lane_env(wt, env, self.p.slug)["CARGO_BUILD_RUSTC_WRAPPER"], fake)
        self.assertNotIn("CARGO_BUILD_RUSTC_WRAPPER", c.cargo_lane_env(wt, {"PATH": os.environ["PATH"]}, self.p.slug) or {})
        for name in ("RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
            self.assertNotIn("CARGO_BUILD_RUSTC_WRAPPER", c.cargo_lane_env(wt, {**env, name: "mine"}, self.p.slug) or {}, name)
        write(os.path.join(self.repo, ".cargo", "config.toml"), "[build]\nrustc-wrapper = 'mine'\n")
        self.assertNotIn("CARGO_BUILD_RUSTC_WRAPPER", c.cargo_lane_env(wt, env, self.p.slug))


class AProjectWithoutCargo(Base):
    cargo = False

    def test_it_gets_neither_the_brief_line_nor_the_variable(self):
        wt = self.lane()
        self.assertEqual(c.cargo_target(wt, self.p.slug), (None, ""))
        self.assertNotIn(VAR, self.brief())
        self.assertNotIn(VAR, lanes.cpu_env(self.lane_p(wt)))
        self.assertNotIn(VAR, lanes.lane_env(self.lane_p(wt)))
        self.assertEqual(c.cargo_focus_line(wt, self.p.slug), "")

    def test_a_cargo_toml_only_in_a_subfolder_is_not_the_projects(self):
        wt = self.lane()
        write(os.path.join(self.repo, "tools", "Cargo.toml"), "[package]\n")
        self.assertIsNone(c.cargo_target(wt, self.p.slug)[0])


class DoctorCountsLaneBuildOutput(DiskCase):
    def setUp(self):
        super().setUp()
        alone(self)
        write(os.path.join(self.repo, "Cargo.toml"), '[package]\nname = "fixture"\n')
        write(os.path.join(self.repo, ".gitignore"), "target/\n")  # a Rust project ignores its build output
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "rust"], check=True, capture_output=True)
        self.low = lambda _: Usage(900e9, 893e9, 7e9)

    def grow(self, lane, size):
        path = os.path.join(lane, "target", "debug", "deps", "big.rlib")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        write(os.path.join(lane, "target", "CACHEDIR.TAG"), "Signature: 8a477f597d28d172789f06886806bc55\n")  # T-4717: cargo's mark

    def grow_cargo(self, lane, size):
        folder = c.cargo_lane_dir(self.p.slug, lane)
        path = os.path.join(folder, "debug", "deps", "x.rlib")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        c.cargo_mark_lane(folder, lane)
        return folder

    def test_target_folders_in_unfinished_lanes_are_named_with_their_bytes(self):
        a = self.lane("T-4001", size=100, old=False)
        b = self.lane("agent-b", size=100, old=False)
        self.grow(a, 1_500_000)
        self.grow(b, 700_000)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertEqual(r.status, "WARN")
        self.assertRegex(r.detail, r"lane build output \(2 target folders\) 2\.[1-3] MB")

    def test_a_lanes_cargo_folder_counts_among_them_by_lane(self):
        a = self.lane("T-4005", size=100, old=False)
        self.grow_cargo(a, 900_000)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertRegex(r.detail, r"lane build output \(1 cargo folder\) 9\d\d KB")
        self.assertRegex(r.detail, r"lane T-4005 cargo folder 9\d\d KB")

    def test_the_old_shared_folder_counts_among_them(self):
        path = os.path.join(c.cargo_shared_dir(self.p.slug), "debug", "deps", "x.rlib")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(os.urandom(900_000))
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertRegex(r.detail, r"lane build output \(old shared\) 9\d\d KB")
        self.assertIn("sampo tidy --apply", r.detail)

    def test_a_finished_lane_is_counted_once_as_the_lane(self):
        done = self.lane("T-4002", size=100, merged=True)
        self.grow(done, 1_200_000)
        self.grow_cargo(done, 800_000)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertRegex(r.detail, r"(done|no-task) lane T-4002 1\.[2-3] MB")
        self.assertNotIn("lane build output", r.detail, "tidy removes the whole lane, target and cargo folder included")

    def test_a_project_without_a_cargo_toml_names_none(self):
        other = self.lane("T-4003", size=100, old=False)
        write(os.path.join(other, "target", "x.o"), "x" * 2000)
        os.unlink(os.path.join(other, "Cargo.toml"))  # this lane has no Cargo.toml: its target is not Rust's
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertNotIn("lane build output", r.detail)

    def test_nothing_is_measured_on_a_roomy_disk_and_nothing_is_deleted(self):
        a = self.lane("T-4004", size=100, old=False)
        self.grow(a, 400_000)
        folder = self.grow_cargo(a, 400_000)
        with unittest.mock.patch.object(lanes, "lane_build_output", side_effect=AssertionError("sized on a roomy disk")):
            roomy = d.check_disk(self.tmp, lambda _: Usage(900e9, 300e9, 600e9), [(self.p, {})])
        self.assertEqual(roomy.status, "PASS")
        d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertTrue(os.path.isfile(os.path.join(a, "target", "debug", "deps", "big.rlib")), "doctor only names it")
        self.assertTrue(os.path.isfile(os.path.join(folder, "debug", "deps", "x.rlib")), "doctor only names it")

    def test_a_scan_that_fails_costs_the_check_nothing(self):
        with unittest.mock.patch.object(lanes, "lane_build_output", side_effect=OSError("gone")):
            r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertEqual(r.status, "WARN")


if __name__ == "__main__":
    unittest.main()
