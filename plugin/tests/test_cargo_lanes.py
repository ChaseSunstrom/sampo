"""T-4764: the one shared cargo target folder (T-4575, T-4717) let two lanes whose sources differ in one workspace crate
reuse each other's compiled artifact: a lane tested another lane's code. Cause (cargo issue 12516, open, seen on cargo
1.99.x): cargo hashes a path package with the workspace prefix removed, so the same crate in two worktrees is one unit
with one fingerprint slot. A target folder shared between worktrees is unsound by construction, so every lane builds
into its own folder under Sampo's cache, named by the lane.

Safety: every folder is the test's own temp folder; the machine's CARGO_* variables and ~/.cargo are never read (patched
to scratch ones; only RUSTUP_HOME points at the real toolchains, read only). The real-cargo test builds crates with no
dependencies, offline, and skips with a reason when cargo is absent."""
import os
import shutil
import subprocess
import unittest
import unittest.mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes

VAR = "CARGO_TARGET_DIR"


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True)


def cargo_works():
    if not shutil.which("cargo"):
        return "cargo is not on PATH"
    try:
        r = subprocess.run(["cargo", "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return f"cargo does not run: {e}"
    return "" if r.returncode == 0 else f"cargo --version failed: {r.stderr.strip()[:100]}"


WORKSPACE = {
    "Cargo.toml": '[workspace]\nresolver = "2"\nmembers = ["lib1", "app"]\n',
    "lib1/Cargo.toml": '[package]\nname = "lib1"\nversion = "0.1.0"\nedition = "2021"\n',
    "lib1/src/lib.rs": "pub fn value() -> u32 { 1 }\n",
    "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\nlib1 = { path = "../lib1" }\n',
    "app/src/lib.rs": "",
    "app/tests/t.rs": '#[test]\nfn shows() { println!("VALUE={}", lib1::value()); }\n',
}


class RealCargo(ForemanTestCase):
    """Two worktrees of one small workspace that differ in one crate, built in turn with the environment Sampo gives
    lanes: each runs its own code."""

    def setUp(self):
        super().setUp()
        why = cargo_works()
        if why:
            self.skipTest(why)
        real_home = os.environ.get("RUSTUP_HOME") or os.path.join(os.path.expanduser("~"), ".rustup")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("CARGO") and k != "RUSTC_WRAPPER"}
        clean.update(HOME=os.path.join(self.tmp, "home-x"), CARGO_HOME=os.path.join(self.tmp, "cargo-home"),
                     RUSTUP_HOME=real_home)
        self.env = clean
        patch = unittest.mock.patch.dict(os.environ, clean, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        for rel, text in WORKSPACE.items():
            write(os.path.join(self.repo, rel), text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "workspace")
        self.fm("init")
        self.p = c.find_project(self.repo)

    def lane(self, name, value, older=False):
        wt = os.path.join(self.tmp, name)
        git(self.repo, "worktree", "add", "-q", "-b", f"foreman/{name}", wt)
        if value != 1:
            src = os.path.join(wt, "lib1", "src", "lib.rs")
            write(src, f"pub fn value() -> u32 {{ {value} }}\n")
            if older:  # the collision cargo 12516 reports: this lane's source is older than the other lane's build
                os.utime(src, (1_000_000_000, 1_000_000_000))
        return os.path.realpath(wt)

    def run_in(self, wt):
        code, out = c.run_command(wt, "cargo test --offline -q -p app -- --nocapture", timeout=600, env=dict(self.env))
        self.assertEqual(code, 0, out[-1500:])
        return [ln for ln in out.splitlines() if ln.startswith("VALUE=")]

    def test_two_lanes_that_differ_in_one_crate_each_run_their_own_code(self):
        one = self.lane("lane-one", 1)
        two = self.lane("lane-two", 2, older=True)
        self.assertEqual(self.run_in(one), ["VALUE=1"])
        self.assertEqual(self.run_in(two), ["VALUE=2"], "the second lane rebuilds the crate that differs")
        self.assertEqual(self.run_in(one), ["VALUE=1"], "and the first still runs its own after the second built")

    def test_the_folders_they_build_into_are_two(self):
        one, two = self.lane("lane-one", 1), self.lane("lane-two", 2)
        self.assertNotEqual(lanes.lane_env(c.Project(self.p.slug, one, self.p.dir, lane=one))[VAR],
                            lanes.lane_env(c.Project(self.p.slug, two, self.p.dir, lane=two))[VAR])


class RustProject(ForemanTestCase):
    """A registered Rust project with a clean cargo environment (no cargo runs)."""

    def setUp(self):
        super().setUp()
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("CARGO", "SAMPO_CARGO", "FOREMAN_CARGO")) and k != "RUSTC_WRAPPER"}
        clean.update(HOME=os.path.join(self.tmp, "home-x"), CARGO_HOME=os.path.join(self.tmp, "cargo-home"))
        patch = unittest.mock.patch.dict(os.environ, clean, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        write(os.path.join(self.repo, "Cargo.toml"), '[package]\nname = "fixture"\nversion = "0.1.0"\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "rust")
        self.fm("init")
        self.p = c.find_project(self.repo)

    def wt(self, *parts):
        path = os.path.join(self.tmp, *parts)
        self.n = getattr(self, "n", 0) + 1
        git(self.repo, "worktree", "add", "-q", "-b", f"b-{self.n}", path)
        return os.path.realpath(path)

    def folder(self, wt, environ=None):
        return c.cargo_target(wt, self.p.slug, environ)[0]


class TheFolderEachLaneGets(RustProject):
    """The folder a lane builds into is its own, whatever its name, and only Sampo's cache holds it (no cargo runs)."""

    def test_no_two_lanes_are_handed_one_folder(self):
        names = [("x", "T-1"), ("y", "T-1"), ("x", "T-2"), ("a b", "c;d"), ("x", "weird\nname"), ("x", ".hidden")]
        seen = {}
        for parts in names:
            wt = self.wt(*parts)
            folder = self.folder(wt)
            self.assertNotIn(folder, seen, "two lanes, one folder")
            seen[folder] = wt
            self.assertTrue(folder.startswith(c.cargo_lanes_base(self.p.slug) + os.sep))
            self.assertRegex(os.path.basename(folder), lanes.LANE_FOLDER.pattern, "a name the prune and the drop accept")
        self.assertEqual(len(seen), len(names))

    def test_the_same_lane_gets_the_same_folder_every_time(self):
        wt = self.wt("T-9")
        self.assertEqual(self.folder(wt), self.folder(wt))
        self.assertEqual(self.folder(wt), self.folder(os.path.join(wt, ".")))

    def test_a_long_or_hostile_name_stays_one_short_safe_name(self):
        name = c.cargo_lane_name(os.path.join(self.tmp, "a" * 300, "..x/;rm -rf $HOME\n"))
        self.assertLess(len(name), 64)
        self.assertRegex(name, lanes.LANE_FOLDER.pattern)
        self.assertFalse(name.startswith("."))

    def test_the_main_checkout_and_a_project_without_rust_get_none(self):
        self.assertEqual(c.cargo_target(self.repo, self.p.slug), (None, ""))
        wt = self.wt("T-3")
        os.unlink(os.path.join(self.repo, "Cargo.toml"))
        self.assertEqual(c.cargo_target(wt, self.p.slug), (None, ""))
        self.assertEqual(c.cargo_lane_env(wt, slug=self.p.slug), {})

    def test_the_users_choice_stands(self):
        wt = self.wt("T-4")
        for name in (VAR, "CARGO_BUILD_TARGET_DIR"):
            folder, why = c.cargo_target(wt, self.p.slug, {name: "/mine/t"})
            self.assertIsNone(folder)
            self.assertIn("/mine/t", why)
        write(os.path.join(self.repo, ".cargo", "config.toml"), '[build]\ntarget-dir = "/mine/cfg"\n')
        folder, why = c.cargo_target(wt, self.p.slug, {})
        self.assertIsNone(folder)
        self.assertIn("/mine/cfg", why)

    def test_a_stale_value_sampo_handed_out_is_replaced_not_kept(self):
        wt = self.wt("T-5")
        for stale in (c.cargo_shared_dir(self.p.slug), c.cargo_lane_dir(self.p.slug, os.path.join(self.tmp, "other"))):
            self.assertEqual(self.folder(wt, {VAR: stale}), c.cargo_lane_dir(self.p.slug, wt), stale)

    def test_the_off_switch_hands_out_nothing(self):
        wt = self.wt("T-6")
        folder, why = c.cargo_target(wt, self.p.slug, {"SAMPO_CARGO_SHARED": "0"})
        self.assertIsNone(folder)
        self.assertIn("SAMPO_CARGO_SHARED=0", why)

    def test_the_env_names_the_folder_and_marks_it_once(self):
        wt = self.wt("T-7")
        env = c.cargo_lane_env(wt, {"PATH": os.defpath}, self.p.slug)
        self.assertEqual(env, {VAR: c.cargo_lane_dir(self.p.slug, wt)})
        mark = os.path.join(env[VAR], c.CARGO_LANE_MARK)
        with open(mark, encoding="utf-8") as f:
            self.assertEqual(f.read(), wt + "\n")
        os.utime(mark, (1_000_000_000, 1_000_000_000))
        c.cargo_lane_env(wt, {"PATH": os.defpath}, self.p.slug)
        self.assertEqual(os.stat(mark).st_mtime, 1_000_000_000, "written once")

    def test_sccache_on_the_path_is_the_rustc_wrapper_and_a_users_wrapper_wins(self):
        wt = self.wt("T-8")
        fake = os.path.join(self.tmp, "bin", "sccache")
        write(fake, "#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)
        env = {"PATH": os.path.dirname(fake)}
        self.assertEqual(c.cargo_lane_env(wt, env, self.p.slug)["CARGO_BUILD_RUSTC_WRAPPER"], fake)
        self.assertNotIn("CARGO_BUILD_RUSTC_WRAPPER", c.cargo_lane_env(wt, {**env, "RUSTC_WRAPPER": "mine"}, self.p.slug))
        self.assertNotIn("CARGO_BUILD_RUSTC_WRAPPER", c.cargo_lane_env(wt, {"PATH": os.defpath}, self.p.slug))


class CargoNote(RustProject):
    """T-4951 (kith, 2026-11-17: 79 GB of lanes' own ./target): a builder's cargo command that builds into its worktree
    (a bare cargo with no lane config, or CARGO_TARGET_DIR / --target-dir inside the worktree) gets one PreToolUse note
    naming the lane's own folder and the export line. A note, never a refusal: the folder may be the user's choice."""

    def note(self, cmd, root, tool="Bash"):
        from sampo import hooks
        return hooks._cargo_note({"tool_name": tool, "tool_input": {"command": cmd}, "cwd": root}, c.find_project(root))

    def test_a_build_into_the_worktree_names_the_lanes_folder_and_the_export(self):
        wt = self.wt("T-1")
        self.assertTrue(c.find_project(wt).lane, "the fixture is a lane")
        folder = self.folder(wt)
        for cmd in ("cargo build", "cargo test --workspace", "cargo +nightly check", "/usr/bin/cargo clippy",
                    "CARGO_TARGET_DIR=target cargo build", "CARGO_TARGET_DIR=./target cargo test",
                    'CARGO_TARGET_DIR="$PWD/target" cargo test', "CARGO_TARGET_DIR=${PWD}/out cargo b",
                    f"CARGO_TARGET_DIR={wt}/target cargo check", "export CARGO_TARGET_DIR=target; cargo build",
                    "cargo build --target-dir target", "cargo build --target-dir=./tgt", "cd crates && cargo test",
                    "git pull && cargo nextest run", "timeout 600 cargo test 2>&1 | tail -5"):
            note = self.note(cmd, wt)
            self.assertIsNotNone(note, cmd)
            self.assertIn(f"export {VAR}={folder}", note, cmd)
            self.assertNotIn("\n", note, "one line, so the session hears it once")

    def test_the_lanes_own_folder_one_outside_or_no_build_says_nothing(self):
        wt = self.wt("T-2")
        folder = self.folder(wt)
        for cmd in (f"{VAR}={folder} cargo build", f"export {VAR}={folder}; cargo test", f"cargo build --target-dir {folder}",
                    f"cargo test --target-dir={folder}", f"{VAR}=/var/tmp/elsewhere cargo build",
                    "cargo fmt --check", "cargo --version", "cargo metadata --format-version 1", "cargo tree",
                    'echo "cargo build"', "git commit -qm 'run cargo build'", "grep -n cargo Cargo.toml", "ls",
                    f"cd {self.repo} && cargo build"):
            self.assertIsNone(self.note(cmd, wt), cmd)
        self.assertIsNone(self.note("cargo build", wt, tool="Read"))

    def test_a_lane_with_its_config_in_place_says_nothing_for_a_bare_cargo_but_still_for_an_override(self):
        wt = self.wt("T-3")
        folder = self.folder(wt)
        write(os.path.join(wt, ".cargo", "config.toml"), f"{c.CARGO_MARK}\n[build]\ntarget-dir = '{folder}'\n")
        self.assertIsNone(self.note("cargo build", wt))
        self.assertIsNotNone(self.note(f"{VAR}=target cargo build", wt), "the command overrides the config")

    def test_the_main_checkout_a_project_without_rust_and_the_users_choice_say_nothing(self):
        wt = self.wt("T-4")
        self.assertIsNone(self.note("cargo build", self.repo), "the main checkout keeps its own target/")
        write(os.path.join(wt, ".cargo", "config.toml"), "[build]\ntarget-dir = '/var/tmp/mine'\n")
        self.assertIsNone(self.note("cargo build", wt), "the user's own config")
        os.unlink(os.path.join(wt, ".cargo", "config.toml"))
        with unittest.mock.patch.dict(os.environ, {VAR: "/var/tmp/mine"}):
            self.assertIsNone(self.note("cargo build", wt), "the user's own variable")
        with unittest.mock.patch.dict(os.environ, {"SAMPO_CARGO_SHARED": "0"}):
            self.assertIsNone(self.note("cargo build", wt), "the switch is off")
        os.rename(os.path.join(self.repo, "Cargo.toml"), os.path.join(self.repo, "Cargo.toml.off"))
        self.assertIsNone(self.note("cargo build", wt), "no Cargo.toml in the main checkout: no Rust project")

    def test_through_the_hook_it_is_a_note_never_a_refusal_and_is_said_once(self):
        wt = self.wt("T-5")
        folder = self.folder(wt)
        call = {"tool_name": "Bash", "tool_input": {"command": "cargo build"}, "cwd": wt, "session_id": "sess-cargo"}
        first = self.hook("PreToolUse", call)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertNotIn("permissionDecision", first.stdout)
        self.assertIn(folder, first.stdout)
        again = self.hook("PreToolUse", call)
        self.assertNotIn(folder, again.stdout, "once a session")


class TheGatesOwnFolder(RustProject):
    """T-4764 follow-up: a throwaway worktree (the gate's, the train's: a detached checkout outside the lane homes, made
    fresh for every run) builds into ONE folder per project, so each run is a warm build and none leaves a new 8 to 15 GB
    folder behind; a lane never gets it."""

    def setUp(self):
        super().setUp()
        patch = unittest.mock.patch.object(c, "gettempdir", return_value=os.path.realpath(self.tmp))  # the test's own "/tmp"
        patch.start()
        self.addCleanup(patch.stop)

    def throwaway(self, name):
        path = os.path.join(self.tmp, name)
        git(self.repo, "worktree", "add", "-q", "--detach", path)
        return os.path.realpath(path)

    def test_only_a_worktree_sampo_made_for_a_run_is_a_throwaway(self):
        """T-4764 review: a user's own detached worktree is no throwaway: it would share the gate's folder with every run."""
        for name in ("scratch", "my-fm-verify-x", "verify", "sampo-trainer"):
            wt = self.throwaway(name)
            self.assertFalse(c.cargo_throwaway(wt), name)
            self.assertEqual(self.folder(wt), c.cargo_lane_dir(self.p.slug, wt), f"{name} gets a folder of its own, like a lane")
        for prefix in ("sampo-base-", "sampo-probe-", "sampo-train-", "sampo-batch-", "fm-verify-"):
            self.assertTrue(c.cargo_throwaway(self.throwaway(prefix + "abc12")), prefix)

    def test_a_checkout_inside_a_sampo_temp_folder_is_a_throwaway_one_level_down_only(self):
        for parent, leaf, want in (("fm-verify-q1", "wt", True), ("fm-hunks-q2", "wt", True), ("fm-prove-q3", "base", True),
                                   ("tmpab12", "ship", True), ("tmpab13", "rel", True), ("tmpab14", "wt", False),
                                   ("mine", "wt", False), ("fm-verify-q4", os.path.join("deep", "wt"), False)):
            os.makedirs(os.path.join(self.tmp, parent, os.path.dirname(leaf)), exist_ok=True)
            path = os.path.join(self.tmp, parent, leaf)
            git(self.repo, "worktree", "add", "-q", "--detach", path)
            self.assertEqual(c.cargo_throwaway(os.path.realpath(path)), want, os.path.join(parent, leaf))

    def test_a_sampo_named_folder_elsewhere_than_the_temp_folder_is_no_throwaway(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        path = os.path.join(other, "fm-verify-zz")
        git(self.repo, "worktree", "add", "-q", "--detach", path)
        self.assertFalse(c.cargo_throwaway(os.path.realpath(path)))

    def test_two_successive_throwaway_worktrees_of_one_project_get_the_same_folder(self):
        one = self.folder(self.throwaway("fm-verify-aaaa"))
        shutil.rmtree(os.path.join(self.tmp, "fm-verify-aaaa"))
        git(self.repo, "worktree", "prune")
        two = self.folder(self.throwaway("sampo-train-bbbb"))
        self.assertEqual(one, two)
        self.assertEqual(one, c.cargo_gate_dir(self.p.slug))
        self.assertTrue(one.startswith(c.cargo_lanes_base(self.p.slug) + os.sep))

    def test_a_lane_never_gets_the_gates_folder_and_the_gates_folder_is_no_lane_name(self):
        gate = self.folder(self.throwaway("fm-verify-cccc"))
        for wt in (self.wt("T-11"), self.wt("T-12")):
            self.assertNotEqual(self.folder(wt), gate)
        self.assertNotRegex(os.path.basename(gate), lanes.LANE_FOLDER.pattern, "no prune, orphan sweep or drop can take it")

    def test_a_detached_worktree_in_the_lane_homes_is_still_a_lane(self):
        home = os.path.realpath(self.repo) + ".lanes"
        path = os.path.join(home, "T-13")
        git(self.repo, "worktree", "add", "-q", "--detach", path)
        self.assertEqual(self.folder(os.path.realpath(path)), c.cargo_lane_dir(self.p.slug, path))
        self.assertFalse(c.cargo_throwaway(path))

    def test_a_branch_checkout_outside_the_lane_homes_is_no_throwaway(self):
        self.assertFalse(c.cargo_throwaway(self.wt("T-14")))
        self.assertFalse(c.cargo_throwaway(self.repo), "the main checkout")
        self.assertTrue(c.cargo_throwaway(self.throwaway("fm-verify-dddd")))

    def test_the_gates_env_carries_its_folder_and_leaves_no_lane_marker(self):
        wt = self.throwaway("fm-verify-eeee")
        env = c.cargo_lane_env(wt, {"PATH": os.defpath}, self.p.slug)
        self.assertEqual(env, {VAR: c.cargo_gate_dir(self.p.slug)})
        self.assertFalse(os.path.lexists(os.path.join(env[VAR], c.CARGO_LANE_MARK)), "a marker would let the orphan sweep take it")

    def test_nothing_that_removes_a_lane_folder_takes_the_gates(self):
        gate = c.cargo_gate_dir(self.p.slug)
        write(os.path.join(gate, "debug", "deps", "x.rlib"), "x" * 5000)
        os.utime(os.path.join(gate, "debug", "deps", "x.rlib"), (1_000_000_000, 1_000_000_000))
        os.utime(gate, (1_000_000_000, 1_000_000_000))
        wt = self.throwaway("fm-verify-ffff")
        self.assertFalse(lanes.drop_lane_cargo_dir(self.p.slug, wt, held=[]))
        res = lanes.prune_cargo_cache(self.p, cap=1, held=[])
        self.assertNotIn("gate", [n for n, _ in res["removed"]])
        self.assertEqual(lanes.orphan_lane_folders(self.p, held=[]), [])
        self.assertTrue(os.path.isfile(os.path.join(gate, "debug", "deps", "x.rlib")))

    def test_the_guard_lets_a_throwaway_session_write_only_the_gates_folder(self):
        wt = self.throwaway("fm-verify-gggg")
        p = c.Project(self.p.slug, self.repo, self.p.dir, lane=wt)
        self.assertEqual(c.cargo_assigned_dir(p), c.cargo_gate_dir(self.p.slug))
        lane = self.wt("T-15")
        pl = c.Project(self.p.slug, self.repo, self.p.dir, lane=lane)
        self.assertEqual(c.cargo_assigned_dir(pl), c.cargo_lane_dir(self.p.slug, lane))
        self.assertNotEqual(c.cargo_assigned_dir(pl), c.cargo_gate_dir(self.p.slug))


class TheGateRunsDoNotShareAFolder(RustProject):
    """T-4764 re-check: only the train's runs take turns; a post-merge verify and a ship run detached beside it, so two gate
    runs of DIFFERENT code in one shared folder would be the original defect again (a test binary relinked under a run).
    The run that holds the project's gate lock gets the stable folder; one that finds it busy builds into a private folder
    that goes when its command ends."""

    def setUp(self):
        super().setUp()
        patch = unittest.mock.patch.object(c, "gettempdir", return_value=os.path.realpath(self.tmp))
        patch.start()
        self.addCleanup(patch.stop)
        self.gate = c.cargo_gate_dir(self.p.slug)

    def throwaway(self, name):
        path = os.path.join(self.tmp, name)
        git(self.repo, "worktree", "add", "-q", "--detach", path)
        return os.path.realpath(path)

    def test_sequential_runs_reuse_the_stable_folder(self):
        for _ in range(2):
            with c.cargo_gate_slot(self.gate) as folder:
                self.assertEqual(folder, self.gate)

    def test_overlapping_runs_never_share_a_folder_and_the_second_ones_goes_when_it_ends(self):
        with c.cargo_gate_slot(self.gate) as first:
            with c.cargo_gate_slot(self.gate) as second:
                with c.cargo_gate_slot(self.gate) as third:
                    self.assertEqual(first, self.gate)
                    self.assertEqual(len({first, second, third}), 3, "three overlapping runs, three folders")
                    for private in (second, third):
                        self.assertEqual(os.path.dirname(private), os.path.dirname(self.gate))
                        self.assertRegex(os.path.basename(private), c.GATE_PRIVATE.pattern)
                        self.assertNotRegex(os.path.basename(private), lanes.LANE_FOLDER.pattern, "no prune or drop can take it")
                        write(os.path.join(private, "debug", "deps", "x.o"), "x")
                self.assertFalse(os.path.lexists(third), "gone when its command ended")
                self.assertTrue(os.path.isdir(second))
            self.assertFalse(os.path.lexists(second))
        with c.cargo_gate_slot(self.gate) as again:
            self.assertEqual(again, self.gate, "the lock is free again")

    def test_a_private_folder_is_removed_even_when_the_command_raises(self):
        with c.cargo_gate_slot(self.gate):
            with self.assertRaises(RuntimeError):
                with c.cargo_gate_slot(self.gate) as private:
                    write(os.path.join(private, "x"), "x")
                    raise RuntimeError("boom")
            self.assertEqual([n for n in os.listdir(os.path.dirname(self.gate)) if c.GATE_PRIVATE.match(n)], [])

    def test_a_lock_that_cannot_be_taken_means_a_private_folder_never_the_shared_one(self):
        with unittest.mock.patch.object(c.fcntl, "flock", side_effect=OSError("no locks here")):
            with c.cargo_gate_slot(self.gate) as folder:
                self.assertNotEqual(folder, self.gate)
                self.assertRegex(os.path.basename(folder), c.GATE_PRIVATE.pattern)

    def test_the_lock_marks_use_so_an_unused_gate_folder_can_be_told(self):
        with c.cargo_gate_slot(self.gate):
            pass
        lock = os.path.join(os.path.dirname(self.gate), c.GATE_LOCK)
        os.utime(lock, (1_000_000_000, 1_000_000_000))
        with c.cargo_gate_slot(self.gate):
            pass
        self.assertGreater(os.stat(lock).st_mtime, 1_000_000_000 + 1000)

    def test_two_overlapping_run_command_calls_in_throwaway_worktrees_get_different_folders(self):
        import threading
        one, two = self.throwaway("fm-verify-aaaa"), self.throwaway("sampo-train-bbbb")
        started, go = os.path.join(self.tmp, "started"), os.path.join(self.tmp, "go")
        first = {}

        def run_first():
            first["r"] = c.run_command(one, f"touch {started}; while [ ! -f {go} ]; do sleep 0.05; done; "
                                            f"echo DIR=$CARGO_TARGET_DIR; mkdir -p $CARGO_TARGET_DIR; ls -d $CARGO_TARGET_DIR",
                                       timeout=60, env={"PATH": os.environ["PATH"]})

        t = threading.Thread(target=run_first)
        t.start()
        try:
            for _ in range(200):
                if os.path.exists(started):
                    break
                import time
                time.sleep(0.05)
            self.assertTrue(os.path.exists(started))
            code2, out2 = c.run_command(two, "echo DIR=$CARGO_TARGET_DIR; mkdir -p $CARGO_TARGET_DIR", timeout=60,
                                        env={"PATH": os.environ["PATH"]})
        finally:
            write(go)
            t.join(60)
        code1, out1 = first["r"]
        self.assertEqual((code1, code2), (0, 0), (out1, out2))
        d1 = out1.split("DIR=")[1].split()[0]
        d2 = out2.split("DIR=")[1].split()[0]
        self.assertEqual(d1, self.gate, "the run that held the lock first builds in the stable folder")
        self.assertNotEqual(d2, d1)
        self.assertRegex(os.path.basename(d2), c.GATE_PRIVATE.pattern)
        self.assertFalse(os.path.lexists(d2), "the second run's folder is gone with its command")

    def test_a_run_in_a_lane_takes_no_gate_lock(self):
        lane = self.wt("T-21")
        code, out = c.run_command(lane, "echo DIR=$CARGO_TARGET_DIR", timeout=60, env={"PATH": os.environ["PATH"]})
        self.assertEqual(code, 0, out)
        self.assertEqual(out.split("DIR=")[1].split()[0], c.cargo_lane_dir(self.p.slug, lane))
        self.assertFalse(os.path.lexists(os.path.join(os.path.dirname(self.gate), c.GATE_LOCK)))


if __name__ == "__main__":
    unittest.main()
