"""T-5868: the lane pilot's headless resolver edits its own lane's conflicted files. Claude Code refuses an Edit or Write
of a file under a `--plugin-dir` root as "a sensitive file" (the plugin's own code runs as hooks and commands), and with
`-p` nobody is there to approve it: a resolver started by a pilot that runs from a lane's plugin got that plugin as its
`--plugin-dir`, so every file under the lane's plugin/ (and a test file there) was refused, and its `sampo` commands
imported the lane's own conflicted modules (py.lane_reexec hands a bare `sampo` in a lane over to the lane's plugin).
The pilot now loads the main checkout's plugin, and sets SAMPO_LANE_CHILD while the lane has unmerged paths; the guard
still confines the session to the lane."""
import os
import subprocess
from unittest import mock

from helpers import ForemanTestCase

from sampo import pilotsig


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


def put(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class ConflictEdit(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        put(self.repo, "plugin/.claude-plugin/plugin.json", '{"name": "sampo"}\n')
        put(self.repo, "plugin/lib/sampo/pilotsig.py", "x = 1\n")
        put(self.repo, "plugin/tests/test_claims.py", "a = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "plugin")
        self.base = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        self.lane = os.path.join(self.repo, ".claude", "worktrees", "agent-a1b2c3d4")
        git(self.repo, "worktree", "add", "-q", "-b", "worktree-agent-a1b2c3d4", self.lane)
        self.main_plugin = os.path.join(self.repo, "plugin")
        self.lane_plugin = os.path.join(self.lane, "plugin")
        self.own = os.path.join(self.lane_plugin, "lib", "sampo", "pilotsig.py")  # the pilot, run from the lane's plugin

    def conflict(self):
        """The lane merges trunk and stops on a conflict in plugin/tests/test_claims.py."""
        put(self.lane, "plugin/tests/test_claims.py", "a = 2\n")
        git(self.lane, "commit", "-qam", "lane side")
        put(self.repo, "plugin/tests/test_claims.py", "a = 3\n")
        git(self.repo, "commit", "-qam", "trunk side")
        merge = subprocess.run(["git", "-C", self.lane, "merge", self.base], capture_output=True, text=True)
        self.assertNotEqual(merge.returncode, 0, merge.stdout)
        with open(os.path.join(self.lane, "plugin/tests/test_claims.py"), encoding="utf-8") as f:
            self.assertIn("<<<<<<<", f.read())

    def test_a_pilot_run_from_a_lane_loads_the_main_checkout_s_plugin(self):
        self.assertEqual(os.path.realpath(pilotsig._plugin_dir(self.lane_plugin)), os.path.realpath(self.main_plugin),
                         "the lane's own plugin dir is the --plugin-dir: its files are 'sensitive' to the resolver")
        self.assertEqual(pilotsig._plugin_dir(self.main_plugin), self.main_plugin,
                         "the main checkout's pilot loads its own plugin")

    def test_no_main_plugin_to_load_keeps_the_pilot_s_own(self):
        os.remove(os.path.join(self.main_plugin, ".claude-plugin", "plugin.json"))
        self.assertEqual(pilotsig._plugin_dir(self.lane_plugin), self.lane_plugin)

    def test_a_lane_with_unmerged_paths_runs_the_main_install_s_sampo(self):
        build = "sampo:fm-builder"
        self.assertNotIn("SAMPO_LANE_CHILD", pilotsig._claude_env(self.lane, build),
                         "a clean lane keeps T-2843's hand-over to the lane's own plugin")
        self.assertEqual(pilotsig._claude_env(self.lane, "sampo:fm-resolver").get("SAMPO_LANE_CHILD"), "1",
                         "the resolver's own `git merge` makes the conflict after it starts")
        self.conflict()
        env = pilotsig._claude_env(self.lane, build)
        self.assertEqual(env.get("SAMPO_LANE_CHILD"), "1", "a bare sampo would import the lane's conflicted modules")
        self.assertEqual(env.get("FOREMAN_PILOT_HEADLESS"), "1", "still confined to the lane")

    def test_the_headless_run_gets_the_main_plugin_and_the_main_install(self):
        self.conflict()
        real = subprocess.Popen
        proc = mock.Mock(pid=4242)
        proc.wait.return_value = 0
        with mock.patch.object(pilotsig, "_note_claude", return_value=True), \
                mock.patch.object(pilotsig, "_kill_group"), \
                mock.patch.object(pilotsig, "__file__", self.own), \
                mock.patch.object(pilotsig.subprocess, "Popen",  # the claude is a stub; the git calls are real
                                  side_effect=lambda argv, **kw: proc if "--agent" in argv else real(argv, **kw)) as popen:
            pilotsig._headless(None, "T-0001", self.lane, "sampo:fm-resolver", "go", 30.0)
        run = next(x for x in popen.call_args_list if "--agent" in x[0][0])
        argv = run[0][0]
        self.assertEqual(os.path.realpath(argv[argv.index("--plugin-dir") + 1]), os.path.realpath(self.main_plugin))
        self.assertEqual(run[1]["env"].get("SAMPO_LANE_CHILD"), "1")

    def test_the_resolver_edits_its_own_lane_and_nothing_outside_it(self):
        main_file = os.path.join(self.repo, "x.py")
        lane_file = os.path.join(self.lane, "plugin", "tests", "test_claims.py")

        def said(path):
            return self.hook("PreToolUse", {"tool_name": "Edit", "cwd": self.lane,
                                            "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"}},
                             env={"FOREMAN_PILOT_HEADLESS": "1"}).stdout
        self.conflict()
        self.assertNotIn("blocked", said(lane_file), "the resolver's own lane's conflicted file")
        self.assertIn("blocked confine", said(main_file), "the same edit outside the lane")
