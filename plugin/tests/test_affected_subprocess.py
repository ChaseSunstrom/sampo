# map-links: plugin/lib/sampo/map.py
"""T-4554: a test that runs the hook or the CLI in a child process the run recorder can't follow (a record holding "*",
the whole tree, and no function keys) is selected by the files it read whole. The 12-lane train's gate passed T-4511
(merge b58282bb5: hooks.py's refusal text) although test_sampo_spelling.Spelling, which reads that text through the
hook, was red with it: its record names no function, so ran_tests took it for a pre-T-3603 record and added nothing."""
import os
import subprocess
from unittest import mock

from helpers import ForemanTestCase

HOOKS = "plugin/lib/sampo/hooks.py"
SPELLING = "plugin/tests/test_sampo_spelling.py"
MERGE = "b58282bb5"


class AffectedSubprocess(ForemanTestCase):
    def top(self):
        top = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if subprocess.run(["git", "-C", top, "cat-file", "-e", MERGE + "^{commit}"], capture_output=True).returncode:
            self.skipTest(f"merge {MERGE} isn't in this clone")
        return top

    def selected(self, top, records):
        """What ran_tests picks for merge b58282bb5's files with a run cache holding `records` (class: its keys)."""
        from sampo import map as smap
        import run
        common = subprocess.run(["git", "-C", top, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        files = subprocess.run(["git", "-C", top, "diff", "--name-only", MERGE + "^1", MERGE], capture_output=True,
                               text=True, check=True).stdout.split()
        self.assertIn(HOOKS, files)
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.tmp, "real")}):
            run.save_cache(run.cache_path(os.path.realpath(common)),
                           {cid: ["d", 2, keys] for cid, keys in records.items()}, 0)
            return smap.ran_tests(top, MERGE + "^1", MERGE, files)

    def test_merge_b58282bb5_selects_the_spelling_test_that_ran_the_hook_in_a_child(self):
        top = self.top()
        got = self.selected(top, {
            # what a run of Spelling leaves: the whole tree ("*", a child it couldn't follow), files read whole, no function
            "test_sampo_spelling.Spelling": ["\0git", "*", HOOKS, SPELLING, "plugin/hooks/hook", "plugin/bin/sampo"],
            # a class that ran another hook function: still left out
            "test_firewall.OutlineFirst": [HOOKS + "::", HOOKS + "::_outline_first", "plugin/tests/test_firewall.py"]})
        self.assertIn(SPELLING, got)
        self.assertNotIn("plugin/tests/test_firewall.py", got)

    def test_a_record_with_no_function_and_no_unfollowed_child_still_adds_nothing(self):
        """T-3711's guard: a pre-T-3603 record (every imported module read whole, no '*') is no signal."""
        top = self.top()
        got = self.selected(top, {"test_sampo_spelling.Spelling": [HOOKS, SPELLING],
                                  "test_firewall.OutlineFirst": [HOOKS + "::", HOOKS + "::_gate_pipe_deny",
                                                                 "plugin/tests/test_firewall.py"]})
        self.assertNotIn(SPELLING, got)

    def test_an_unfollowed_child_that_read_nothing_changed_adds_nothing(self):
        top = self.top()
        got = self.selected(top, {"test_sampo_spelling.Spelling": ["*", SPELLING, "plugin/lib/sampo/core.py"]})
        self.assertNotIn(SPELLING, got)
