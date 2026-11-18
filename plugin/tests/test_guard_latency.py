"""fmguard.check stays fast on pathological commands (T-2668).

Each case builds one hostile command and times g.check in CPU seconds (process_time, so a loaded machine doesn't
flake). A case over its bar fails. The bars are set from a measured baseline with >= 5x headroom; the worst measured
time is printed. test_planted_quadratic_fails wires a catastrophic regex into the same harness to prove it can fail."""
import re
import time
import unittest
from unittest import mock

from test_guard import GuardCase

import fmguard as g

BAR = 4.0  # CPU seconds per case: >= 5x the slowest measured (0.68s) here; measured worst is printed below
BARS = {"10k ; commands": 15.0}  # linear, ~0.23ms a command: measured 2.3s, bar 6x that
INDEX_BAR = 20.0  # T-4906: the Ward's writer index, built once per Sampo tree and kept on disk: measured 4.3s, ~5x
# (T-4930: the hook never pays it: a cold tree starts one detached build; this bar holds that build and guard try's)


def cases():
    return {
        "50k-char line": "echo " + "a" * 50000,
        "2000 alternating quotes": "echo " + "a\"b'" * 1000,
        "500 nested $(": "echo " + "$(" * 500 + "x" + ")" * 500,
        "8 nested $(": "echo " + "$(" * 8 + "x" + ")" * 8,  # T-2668: 500 deep hung (n^4); past 8 it's refused at once
        "200 heredocs": "".join(f"cat <<E{i}\nx\nE{i}\n" for i in range(200)),
        "python heredoc 5000 lines": "python3 - <<'EOF'\n" + "x = 1\n" * 5000 + "EOF\n",
        "backslash runs": "echo " + "\\" * 20000 + "x",
        "backslash-quote runs": "echo \"" + "\\\"" * 10000 + "\"",
        "10k ; commands": ";".join(["echo a"] * 10000),
        "many cd x &&": "cd x && " * 2000 + "ls",
        # T-4893: an import list read by a regex from each `from` to its end was quadratic (6.9 s for these)
        "3000 open from-import lists": "python3 -c '" + "from sampo import (" * 3000 + "'",
        "3000 from-imports on one line": "python3 -c '" + "from sampo.core import a, " * 3000 + "'",
    }


class Latency(GuardCase):
    def cpu(self, cmd, fn=None):
        fn = fn or g.check
        t = time.process_time()
        fn("Bash", {"command": cmd}, self.ctx())
        return time.process_time() - t

    def test_every_case_under_bar(self):
        # T-4906: the first command naming Sampo builds the writer index (once per tree, then read from its cache):
        # built here and held to its own bar, so a case's time is the gate's alone, not whichever case came first
        g._INDEX_MEMO.clear()
        t = time.process_time()
        g._writer_index()
        build = time.process_time() - t
        self.assertLess(build, INDEX_BAR, f"the writer index took {build:.2f}s CPU to build")
        times = {name: self.cpu(cmd) for name, cmd in cases().items()}
        worst = max(times, key=times.get)
        print(f"\nguard latency worst: {worst} {times[worst]:.3f}s (bar {BAR}s); index build {build:.3f}s; all: "
              + ", ".join(f"{k}={v:.3f}" for k, v in times.items()))
        for name, t in times.items():
            with self.subTest(case=name):
                self.assertLess(t, BARS.get(name, BAR), f"{name} took {t:.2f}s CPU")

    def test_planted_quadratic_fails(self):
        real, base = g.check, max(self.cpu("echo hi"), 0.001)

        def slow(tool, inp, ctx):
            re.compile(r"(a+)+b").search("a" * 22)  # exponential backtracking, as a bad guard regex would be
            return real(tool, inp, ctx)

        # the bar scales with this machine's own check time, so the margin doesn't depend on its speed (review: a
        # fixed 4 s bar against "a" * 26 was 1.5x over here and could pass on a faster machine)
        with self.assertRaises(AssertionError, msg="the harness did not see the planted slow pattern"):
            self.assertLess(self.cpu("echo hi", slow), base * 20)


if __name__ == "__main__":
    unittest.main()
