"""T-5040: sampo.hooksmoke.run(plugin) feeds a tree's own hooks/hook a small payload corpus in a scratch home and says
(ok, why): ok for a sound tree, not ok (with the case and what went wrong) for a tree whose hook entry or library is
broken, within seconds and without touching the caller's state."""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helpers import PLUGIN, SampoTestCase, cpu_contention

from sampo import hooksmoke

BUDGET_S = 5


def budget():
    """The wall-clock budget: BUDGET_S on an idle box, scaled by helpers.cpu_contention on a crowded one."""
    return BUDGET_S * cpu_contention()


def tree_stamp(path):
    """(path, mtime) of every entry under path, so a write deep in a folder that already exists shows as a change."""
    return sorted((os.path.relpath(os.path.join(d, n), path), os.lstat(os.path.join(d, n)).st_mtime_ns)
                  for d, dirs, files in os.walk(path) for n in dirs + files)


def make_tree(root, entry=None, core=None):
    """A copy of the real tree's hook entry beside a lib of symlinks to the real one; `entry` / `core` replace
    hooks/hook / sampo/core.py with that text. Returns the plugin folder."""
    plugin = os.path.join(root, "plugin")
    os.makedirs(os.path.join(plugin, "hooks"))
    hook = os.path.join(plugin, "hooks", "hook")
    shutil.copy2(os.path.join(PLUGIN, "hooks", "hook"), hook)
    if entry is not None:
        with open(hook, "w", encoding="utf-8") as f:
            f.write(entry)
    real = os.path.join(PLUGIN, "lib")
    lib = os.path.join(plugin, "lib")
    os.makedirs(os.path.join(lib, "sampo"))
    for name in os.listdir(real):
        if name not in ("sampo", "__pycache__"):
            os.symlink(os.path.join(real, name), os.path.join(lib, name))
    for name in os.listdir(os.path.join(real, "sampo")):
        if name != "__pycache__" and not (core is not None and name == "core.py"):
            os.symlink(os.path.join(real, "sampo", name), os.path.join(lib, "sampo", name))
    if core is not None:
        with open(os.path.join(lib, "sampo", "core.py"), "w", encoding="utf-8") as f:
            f.write(core)
    return plugin


class HookSmoke(SampoTestCase):
    def test_sound_tree_is_ok_and_quick(self):
        t0 = time.monotonic()
        ok, why = hooksmoke.run(PLUGIN)
        self.assertEqual((ok, why), (True, ""))
        self.assertLess(time.monotonic() - t0, budget())

    def test_default_is_the_running_tree(self):
        self.assertEqual(hooksmoke.run(), (True, ""))

    def test_broken_entry_is_named(self):
        plugin = make_tree(self.tmp, entry="#!/usr/bin/env python3\nraise SystemExit(1)\n")
        t0 = time.monotonic()
        ok, why = hooksmoke.run(plugin)
        self.assertFalse(ok)
        self.assertIn("exit", why)
        self.assertLess(time.monotonic() - t0, budget())

    def test_broken_lib_is_named(self):
        plugin = make_tree(self.tmp, core="raise RuntimeError('smoke: a broken lib')\n")
        t0 = time.monotonic()
        ok, why = hooksmoke.run(plugin)
        self.assertFalse(ok, why)
        self.assertIn("allow case", why)  # SessionStart fails open (exit 0); the guard's fail-closed refusal of `ls` shows it
        self.assertLess(time.monotonic() - t0, budget())

    def test_broken_working_copy_in_a_git_checkout_still_fails(self):
        import subprocess
        plugin = os.path.join(self.tmp, "plugin")
        shutil.copytree(PLUGIN, plugin, ignore=shutil.ignore_patterns("__pycache__", "tests"), symlinks=True)
        git = ["git", "-C", self.tmp, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True, capture_output=True)
        subprocess.run(git + ["commit", "-q", "-m", "good"], check=True, capture_output=True)
        with open(os.path.join(plugin, "lib", "sampo", "core.py"), "w", encoding="utf-8") as f:
            f.write("raise RuntimeError('smoke: a broken lib')\n")
        ok, why = hooksmoke.run(plugin)
        self.assertFalse(ok, why)
        self.assertIn("failed to import", why)

    def test_unwritable_temp_is_a_result_not_a_raise(self):
        with mock.patch("tempfile.mkdtemp", side_effect=OSError("disk full")):
            ok, why = hooksmoke.run(PLUGIN)
        self.assertFalse(ok)
        self.assertIn("could not run", why)

    def test_a_guard_that_allows_the_block_case_fails(self):
        plugin = make_tree(self.tmp, entry="#!/usr/bin/env python3\nraise SystemExit(0)\n")
        ok, why = hooksmoke.run(plugin)
        self.assertFalse(ok)
        self.assertIn("block", why)

    def test_a_guard_that_refuses_the_allow_case_fails(self):
        plugin = make_tree(self.tmp, entry="#!/usr/bin/env python3\nimport sys\nsys.stderr.write('no')\nraise SystemExit(2)\n")
        ok, why = hooksmoke.run(plugin)
        self.assertFalse(ok)
        self.assertIn("exit 2", why)

    def test_output_that_is_not_json_fails(self):
        plugin = make_tree(self.tmp, entry="#!/usr/bin/env python3\nimport sys\nprint('{nope')\n"
                           "raise SystemExit(2 if 'PreToolUse' in sys.argv and 'curl' in sys.stdin.read() else 0)\n")
        ok, why = hooksmoke.run(plugin)
        self.assertFalse(ok)
        self.assertIn("JSON", why)

    def test_a_hung_hook_is_cut_off(self):
        plugin = make_tree(self.tmp, entry="#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
        t0 = time.monotonic()
        with mock.patch.object(hooksmoke, "CALL_SECONDS", 1):
            ok, why = hooksmoke.run(plugin)
        self.assertFalse(ok)
        self.assertIn("timed out", why)
        self.assertLess(time.monotonic() - t0, budget())

    def test_missing_entry_is_a_result_not_a_raise(self):
        ok, why = hooksmoke.run(os.path.join(self.tmp, "nowhere"))
        self.assertFalse(ok)
        self.assertIn("hooks/hook", why)

    def test_the_callers_state_is_untouched(self):
        os.makedirs(os.path.join(self.home, "state", "logs"))  # an existing folder: a write inside it must show
        before = tree_stamp(self.home)
        hooksmoke.run(PLUGIN)
        self.assertEqual(tree_stamp(self.home), before)

    def test_the_state_check_sees_a_write_deep_in_a_folder(self):
        os.makedirs(os.path.join(self.home, "state", "logs"))
        before = tree_stamp(self.home)
        with open(os.path.join(self.home, "state", "logs", "hooks.log"), "w", encoding="utf-8") as f:
            f.write("x\n")
        self.assertNotEqual(tree_stamp(self.home), before)

    def test_budget_grows_on_a_crowded_box(self):
        with mock.patch("os.getloadavg", return_value=(0.5, 0.5, 0.5)), mock.patch("os.cpu_count", return_value=16):
            self.assertEqual(budget(), BUDGET_S)
        with mock.patch("os.getloadavg", return_value=(40.0, 40.0, 40.0)), mock.patch("os.cpu_count", return_value=16):
            self.assertGreater(budget(), BUDGET_S)

    def test_scratch_is_removed(self):
        mine = tempfile.gettempdir()
        before = {n for n in os.listdir(mine) if n.startswith("sampo-hooksmoke-")}
        hooksmoke.run(PLUGIN)
        self.assertEqual({n for n in os.listdir(mine) if n.startswith("sampo-hooksmoke-")}, before)


if __name__ == "__main__":
    unittest.main()
