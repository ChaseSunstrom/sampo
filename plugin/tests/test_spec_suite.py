"""Spec suite round 3 (T-1377): tasks no change can do prove themselves impossible, every attempt failing."""
import os
import subprocess
import sys
import unittest

MAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bench", "spec-suite", "make.py")


class ImpossibleRound(unittest.TestCase):
    def test_every_attempt_at_every_task_fails(self):
        r = subprocess.run([sys.executable, MAKE, "--check", "--suite", "impossible"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-1000:])
        self.assertIn("spec suite (impossible): 3 tasks, each one no attempt passes", r.stdout)
        for name in ("caseslug", "rates", "vault", "base tests", "reproducible"):
            self.assertRegex(r.stdout, rf"(?m)^ok\s+{name}\b")


class QueueRound(unittest.TestCase):
    """T-3340: the benchmark queue: 12 tasks, hidden tests outside the arm's tree, naive variants caught."""

    def test_check_proves_twelve_tasks_and_their_naive_variants(self):
        r = subprocess.run([sys.executable, MAKE, "--check", "--suite", "queue"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-1000:])
        self.assertIn("spec suite (queue): 12 of 12 tasks fail-to-pass with their naive variants caught", r.stdout)
        self.assertRegex(r.stdout, r"(?m)^ok\s+reproducible\b")
        self.assertRegex(r.stdout, r"(?m)^ok\s+answers apart\b")

    def test_the_build_keeps_hidden_tests_out_of_the_repo_and_is_byte_identical(self):
        import filecmp
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("a", "b"):
                r = subprocess.run([sys.executable, MAKE, "--suite", "queue", os.path.join(tmp, n), "--hidden",
                                    os.path.join(tmp, n + "v", "hidden")],
                                   capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            tracked = subprocess.run(["git", "ls-files"], cwd=os.path.join(tmp, "a"), capture_output=True,
                                     text=True).stdout
            self.assertNotIn("test_q_", tracked)
            self.assertTrue(os.listdir(os.path.join(tmp, "av", "hidden")))
            for n in ("av",):
                cmp = filecmp.dircmp(os.path.join(tmp, n, "hidden"), os.path.join(tmp, "bv", "hidden"))
                self.assertEqual(cmp.diff_files + cmp.left_only + cmp.right_only, [])
            head = lambda n: subprocess.run(["git", "rev-parse", "HEAD"], cwd=os.path.join(tmp, n),
                                            capture_output=True, text=True).stdout
            self.assertEqual(head("a"), head("b"))


if __name__ == "__main__":
    unittest.main()
