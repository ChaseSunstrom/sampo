"""T-4575: builder lanes of a Rust project each compiled into their own target folder (2026-11-13: lane worktrees grew
8.6 GB to 15 GB in 40 minutes and twice took the disk to 99%). A project whose root holds a Cargo.toml now gives its
lanes one shared CARGO_TARGET_DIR outside the repo: the builder brief names it, the commands Sampo spawns for a lane
carry it, a value the user already set (the environment, or build.target-dir in a .cargo/config.toml) is kept and said,
a project without a Cargo.toml gets neither, and sampo doctor's disk line counts lane build output among what can be
freed.

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
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CARGO")}
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

    def shared(self):
        return lanes.cargo_target(self.p)[0]


class TheSharedFolder(Base):
    def test_a_rust_project_has_one_folder_outside_the_repo_and_its_lanes(self):
        folder = self.shared()
        self.assertTrue(os.path.isabs(folder))
        for inside in (self.repo, self.repo + ".lanes"):
            self.assertFalse(folder.startswith(inside + os.sep), f"{folder} is inside {inside}")
        self.assertTrue(folder.startswith(c.state_dir() + os.sep), "under Sampo's state, which sync never copies whole")
        self.assertEqual(folder, lanes.cargo_target(self.p)[0], "the same folder on every call")

    def test_a_lane_of_the_project_names_the_same_folder(self):
        lane = os.path.join(self.tmp, "lane")
        lanes._git(self.repo, "worktree", "add", "-q", "-b", "foreman/T-9", lane)
        lane_p = c.Project(self.p.slug, lane, self.p.dir, lane=lane)
        self.assertEqual(lanes.cargo_target(lane_p)[0], self.shared())

    def test_the_brief_names_it_as_the_line_to_export(self):
        text = self.brief()
        self.assertIn(f"export {VAR}={self.shared()}", text)
        self.assertIn("lock", text, "it says a build may wait on cargo's lock")
        self.assertLess(text.index(VAR), text.index("## Your contract"), "named before the contract, with the rules")

    def test_lane_commands_sampo_spawns_carry_it(self):
        self.assertEqual(lanes.cpu_env(self.p)[VAR], self.shared())
        self.assertEqual(lanes.cpu_env(self.p, gate=False)[VAR], self.shared())
        self.assertEqual(lanes.lane_env(self.p)[VAR], self.shared())

    def out(self, root, env=None):
        code, text = c.run_command(root, 'echo "[${CARGO_TARGET_DIR}]"', timeout=60, env=env)
        self.assertEqual(code, 0, text)
        return text.strip()

    def test_a_command_run_in_a_lane_carries_it_and_one_in_the_main_checkout_does_not(self):
        wt = os.path.join(self.tmp, "lane-wt")
        lanes._git(self.repo, "worktree", "add", "-q", "-b", "foreman/T-9", wt)
        self.assertEqual(self.out(wt), f"[{self.shared()}]", "the evidence run and the criterion re-run of a lane")
        self.assertEqual(self.out(self.repo), "[]", "the main checkout builds where the user always built")
        self.assertEqual(self.out(wt, env={"PATH": os.environ["PATH"]}), f"[{self.shared()}]", "an explicit env too")
        self.assertEqual(self.out(wt, env={"PATH": os.environ["PATH"], VAR: "/mine/t"}), "[/mine/t]", "a value is kept")


class WhatTheUserSet(Base):
    def test_a_value_in_the_environment_is_kept_and_said(self):
        with unittest.mock.patch.dict(os.environ, {VAR: "/mine/target"}):
            self.assertEqual(lanes.cargo_target(self.p)[0], None)
            self.assertEqual(lanes.cpu_env(self.p)[VAR], "/mine/target")
            text = self.brief()
        self.assertIn("/mine/target", text)
        self.assertIn("keep", text.lower())
        self.assertNotIn(f"export {VAR}=", text)

    def test_the_other_name_cargo_reads_is_kept_too(self):
        with unittest.mock.patch.dict(os.environ, {"CARGO_BUILD_TARGET_DIR": "/mine/other"}):
            self.assertIsNone(lanes.cargo_target(self.p)[0])
            self.assertNotIn(VAR, lanes.cpu_env(self.p))

    def test_a_target_dir_in_the_projects_cargo_config_is_kept_and_said(self):
        write(os.path.join(self.repo, ".cargo", "config.toml"), '[build]\ntarget-dir = "/mine/cfg"\n')
        self.assertIsNone(lanes.cargo_target(self.p)[0])
        self.assertNotIn(VAR, lanes.cpu_env(self.p))
        text = self.brief()
        self.assertIn(".cargo", text)
        self.assertNotIn(f"export {VAR}=", text)

    def test_the_older_config_name_and_the_users_own_config_count_too(self):
        write(os.path.join(self.repo, ".cargo", "config"), "[build]\ntarget-dir = 'x'\n")
        self.assertIsNone(lanes.cargo_target(self.p)[0])
        os.unlink(os.path.join(self.repo, ".cargo", "config"))
        self.assertIsNotNone(lanes.cargo_target(self.p)[0])
        write(os.path.join(os.environ["CARGO_HOME"], "config.toml"), '[build]\ntarget-dir = "/mine/user"\n')
        self.assertIsNone(lanes.cargo_target(self.p)[0])

    def test_a_cargo_config_without_a_target_dir_changes_nothing(self):
        write(os.path.join(self.repo, ".cargo", "config.toml"), '[build]\njobs = 4\n[alias]\nb = "build"\n')
        self.assertEqual(lanes.cargo_target(self.p)[0], self.shared())
        self.assertIsNotNone(self.shared())

    def test_a_torn_cargo_config_is_not_a_crash(self):
        write(os.path.join(self.repo, ".cargo", "config.toml"), "[build\ntarget-dir = \n")
        self.assertIsNotNone(lanes.cargo_target(self.p)[0])
        self.assertIn(VAR, self.brief())

    def test_an_empty_variable_is_not_a_value(self):
        with unittest.mock.patch.dict(os.environ, {VAR: ""}):
            self.assertIsNotNone(lanes.cargo_target(self.p)[0])
            self.assertEqual(lanes.cpu_env(self.p)[VAR], self.shared())


class AProjectWithoutCargo(Base):
    cargo = False

    def test_it_gets_neither_the_brief_line_nor_the_variable(self):
        self.assertEqual(lanes.cargo_target(self.p), (None, ""))
        self.assertNotIn(VAR, self.brief())
        self.assertNotIn(VAR, lanes.cpu_env(self.p))
        self.assertNotIn(VAR, lanes.lane_env(self.p))

    def test_a_cargo_toml_only_in_a_subfolder_is_not_the_projects(self):
        write(os.path.join(self.repo, "tools", "Cargo.toml"), "[package]\n")
        self.assertIsNone(lanes.cargo_target(self.p)[0])


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

    def test_target_folders_in_unfinished_lanes_are_named_with_their_bytes(self):
        a = self.lane("T-4001", size=100, old=False)
        b = self.lane("agent-b", size=100, old=False)
        self.grow(a, 1_500_000)
        self.grow(b, 700_000)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertEqual(r.status, "WARN")
        self.assertRegex(r.detail, r"lane build output \(2 target folders\) 2\.[1-3] MB")

    def test_the_shared_folder_counts_among_them(self):
        path = os.path.join(lanes.cargo_target(self.p)[0], "debug", "deps", "x.rlib")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(os.urandom(900_000))
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertRegex(r.detail, r"lane build output \(shared\) 9\d\d KB")

    def test_a_finished_lane_is_counted_once_as_the_lane(self):
        done = self.lane("T-4002", size=100, merged=True)
        self.grow(done, 1_200_000)
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertRegex(r.detail, r"(done|no-task) lane T-4002 1\.[2-3] MB")
        self.assertNotIn("lane build output", r.detail, "tidy removes the whole lane, target included")

    def test_a_project_without_a_cargo_toml_names_none(self):
        other = self.lane("T-4003", size=100, old=False)
        write(os.path.join(other, "target", "x.o"), "x" * 2000)
        os.unlink(os.path.join(other, "Cargo.toml"))  # this lane has no Cargo.toml: its target is not Rust's
        r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertNotIn("lane build output", r.detail)

    def test_nothing_is_measured_on_a_roomy_disk_and_nothing_is_deleted(self):
        a = self.lane("T-4004", size=100, old=False)
        self.grow(a, 400_000)
        with unittest.mock.patch.object(lanes, "lane_build_output", side_effect=AssertionError("sized on a roomy disk")):
            roomy = d.check_disk(self.tmp, lambda _: Usage(900e9, 300e9, 600e9), [(self.p, {})])
        self.assertEqual(roomy.status, "PASS")
        d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertTrue(os.path.isfile(os.path.join(a, "target", "debug", "deps", "big.rlib")), "doctor only names it")

    def test_a_scan_that_fails_costs_the_check_nothing(self):
        with unittest.mock.patch.object(lanes, "lane_build_output", side_effect=OSError("gone")):
            r = d.check_disk(self.tmp, self.low, [(self.p, {})])
        self.assertEqual(r.status, "WARN")


if __name__ == "__main__":
    unittest.main()
