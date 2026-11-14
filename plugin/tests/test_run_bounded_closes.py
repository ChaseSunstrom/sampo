"""T-4636: smith.run_bounded closes the pipes it opened. It started its command with stdout (and stdin, when given text)
as pipes and returned without closing them on any path: each call left a file open until the collector ran, a
ResourceWarning under -W error or -X dev and descriptors held longer than the call. The pipes are now closed on the
normal end, on a timeout, when a callback raises (the reader swallows it: `core._safe`) and when stdin text was given,
after the reader thread ended (a descriptor a thread still reads is never closed under it); a close that fails
(a broken pipe on flush) never reaches the caller. Return values, the kill and reap order and the callbacks are as
before (the keep-check modules and test_code's run_bounded cases guard them).
reruns: the whole module, nothing else: each case is its own run_bounded call over a throwaway directory."""
import gc
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "..", "lib")):
    if p not in sys.path:
        sys.path.insert(0, p)

from helpers import ForemanTestCase  # noqa: E402

from sampo import smith  # noqa: E402

HAS_PROC = os.path.isdir("/proc/self/fd")
# a child that outlives the call in a session of its own: os.setsid in python3, since macOS has no setsid command
OUTLIVING = f"({shlex.quote(sys.executable)} -c 'import os, time; os.setsid(); time.sleep(2)' &)"


def open_fds():
    return len(os.listdir("/proc/self/fd"))


class Closes(ForemanTestCase):
    """Each case runs run_bounded with warnings recorded (the `-W error` of the task, kept as records so a warning
    raised in a finaliser is not lost as an unraisable) and the collector forced."""

    def call(self, cmd, timeout=30, **kw):
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            result = smith.run_bounded(cmd, d, timeout, **kw)
            gc.collect()
        leaked = [str(w.message) for w in seen if issubclass(w.category, ResourceWarning)]
        return result, leaked

    def test_the_normal_end_leaves_no_resource_warning(self):
        (code, out), leaked = self.call("echo hello")
        self.assertEqual((code, out), (0, "hello"))
        self.assertEqual(leaked, [])

    def test_a_timeout_leaves_no_resource_warning(self):
        (code, out), leaked = self.call("echo before; sleep 30", timeout=1)
        self.assertIsNone(code)
        self.assertEqual(out, "before")
        self.assertEqual(leaked, [])

    def test_a_callback_that_raises_leaves_no_resource_warning(self):
        seen = []

        def boom(line):
            seen.append(line)
            raise RuntimeError("the callback fails")
        (code, out), leaked = self.call("echo one; echo two", on_line=boom)
        self.assertEqual((code, out), (0, "one\ntwo"))
        self.assertEqual(seen, ["one", "two"])
        self.assertEqual(leaked, [])

    def test_stdin_text_leaves_no_resource_warning(self):
        (code, out), leaked = self.call("cat", stdin="from the caller")
        self.assertEqual((code, out), (0, "from the caller"))
        self.assertEqual(leaked, [])

    def test_a_command_that_never_reads_its_stdin_leaves_no_resource_warning(self):
        (code, out), leaked = self.call("exit 3", stdin="x" * 1000)
        self.assertEqual(code, 3)
        self.assertEqual(leaked, [])

    @unittest.skipUnless(HAS_PROC, "/proc/self/fd is not here: no descriptor count")
    def test_the_descriptor_count_is_flat_over_50_calls(self):
        self.call("echo warm")  # imports and lazy state first
        for kw in ({}, {"stdin": "text"}, {"on_line": lambda line: True}):
            before = open_fds()
            for _ in range(50):
                self.call("echo x", **kw)
            self.assertEqual(open_fds(), before, f"descriptors grew over 50 calls with {sorted(kw)}")
        before = open_fds()
        for _ in range(50):
            self.call("sleep 30", timeout=0.05)
        self.assertEqual(open_fds(), before, "descriptors grew over 50 timeouts")

    def test_a_leftover_grandchild_holding_the_write_end_neither_blocks_nor_raises(self):
        # the reader's join is bounded (5 s); here it gives up at once, as when a setsid child outlives the call: the
        # reader still reads, so the descriptor stays open under it until it ends, then it is closed
        real = threading.Thread.join
        with mock.patch.object(threading.Thread, "join", lambda self, timeout=None: real(self, 0.2)):
            started = time.monotonic()
            (code, out), leaked = self.call(f"echo parent; {OUTLIVING} ; true")
            took = time.monotonic() - started
        self.assertEqual((code, out), (0, "parent"))
        self.assertLess(took, 1.5, "the close waited for the grandchild")
        self.assertEqual(leaked, [])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(t.name != "MainThread" and t.is_alive() and t.daemon
                                                   for t in threading.enumerate()):
            time.sleep(0.1)
        gc.collect()

    def test_a_close_that_fails_never_reaches_the_caller(self):
        real = smith._close_quietly

        class Broken:
            def close(self):
                raise BrokenPipeError("flush failed")
        self.assertIsNone(real(Broken()))
        self.assertIsNone(real(None))


if __name__ == "__main__":
    unittest.main()
