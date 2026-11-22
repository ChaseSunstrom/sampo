"""T-5527: `sampo lane land` and `sampo lane train` ran at the caller's nice 0, so each close's criteria re-run (sampo task
finish, ~65 s of test_guard/test_docs/test_run) competed with builders and the load gate, which counts only nice-0 load,
refused new lanes while a land closed (2026-11-21: 30.5 of 24 allowed). Both verbs now lower their own niceness to 10 at
the dispatch, before any merge or close, so every child inherits it; a process already at a higher value keeps it
(never raised). Each case runs in a forked child, so the test process itself is never niced; the child reports the
niceness the stubbed train entry saw."""
import argparse
import os
import unittest
from unittest import mock

import helpers  # noqa: F401 (puts plugin/lib on the import path)
from sampo import core as c
from sampo import lanes


def mine():
    return os.getpriority(os.PRIO_PROCESS, 0)


def in_child(fn):
    """fn() run in a forked child; its int result comes back through a pipe (a child that fails reports -1)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            os.write(w, str(fn()).encode())
        except BaseException:  # noqa: BLE001 (a forked child must never fall back into the test runner)
            code = 1
        finally:
            os._exit(code)
    os.close(w)
    with os.fdopen(r) as f:
        got = f.read()
    os.waitpid(pid, 0)
    return int(got) if got else -1


class LandNice(unittest.TestCase):
    def dispatch(self, action, start, env=None):
        """The niceness the stubbed train entry sees when `sampo lane ACTION` starts at nice `start`."""
        def child():
            from sampo import cli, train
            if start > mine():  # a process can only raise its nice value: the runner may already sit above 0
                os.nice(start - mine())
            seen = []
            p = argparse.Namespace(lane=None, root="/nowhere")
            stub = lambda *a, **k: seen.append(mine()) or 0  # noqa: E731
            with mock.patch.object(cli, "resolve", return_value=p), \
                    mock.patch.object(c, "main_worktree", return_value="/nowhere"), \
                    mock.patch.object(train, "cmd", stub), mock.patch.object(train, "cmd_land", stub), \
                    mock.patch.dict(os.environ, env or {}):
                lanes.cmd_lane(argparse.Namespace(action=action, id=None))
            return seen[0]
        return in_child(child)

    BASE = mine()  # the runner's own niceness: 0 for an operator, 10 when the check itself runs niced

    def test_lane_land_runs_its_train_at_nice_10(self):
        self.assertEqual(self.dispatch("land", 0), max(self.BASE, 10))

    def test_lane_train_runs_at_nice_10(self):
        self.assertEqual(self.dispatch("train", 0), max(self.BASE, 10))

    def test_a_process_already_at_15_stays_at_15(self):
        self.assertEqual(self.dispatch("land", 15), max(self.BASE, 15))
        self.assertEqual(self.dispatch("train", 15), max(self.BASE, 15))

    def test_the_operator_knob_sets_another_level_and_a_bad_one_is_ignored(self):
        level = min(19, self.BASE + 3)  # above whatever the runner sits at, so the lowering is visible
        self.assertEqual(self.dispatch("land", 0, {"FOREMAN_LAND_NICE": str(level)}), level)
        self.assertEqual(self.dispatch("land", 0, {"FOREMAN_LAND_NICE": "junk"}), max(self.BASE, 10))
        self.assertEqual(self.dispatch("land", 0, {"FOREMAN_LAND_NICE": "99"}), 19)  # capped at the kernel's 19

    def test_the_other_lane_verbs_keep_their_priority(self):
        def child():
            from sampo import cli
            seen = []
            p = argparse.Namespace(lane=None, root="/nowhere")
            with mock.patch.object(cli, "resolve", return_value=p), \
                    mock.patch.object(c, "main_worktree", return_value="/nowhere"), \
                    mock.patch.object(lanes, "start", lambda *a, **k: seen.append(mine()) or 0):
                lanes.cmd_lane(argparse.Namespace(action="start", id=None))
            return seen[0]
        self.assertEqual(in_child(child), self.BASE)


if __name__ == "__main__":
    unittest.main()
