"""T-2560, T-2579: every CI job runs its share of the suite and no share is left out. Each OS's shard jobs must be exactly
1..N, where N is the count the run line divides that OS's jobs by; the test reads N from the workflow, so the next
re-split (a fourth macOS shard, a third Ubuntu one) edits only the workflow."""
import os
import re
import unittest

WORKFLOW = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        ".github", "workflows", "test.yml")


def problems(text):
    """What breaks the invariant, as plain lines ([] when every OS's shards are 1..N for its run-line N)."""
    m = re.search(r"--shard \$\{\{ matrix\.shard \}\}/\$\{\{ matrix\.experimental && (\d+) \|\| (\d+) \}\}", text)
    if not m:
        return ["the run line doesn't divide by a per-job shard count"]
    n_exp, n_main = int(m.group(1)), int(m.group(2))
    main = re.search(r"^\s+shard: \[([\d, ]+)\]", text, re.M)
    shards = {"main": sorted(int(x) for x in main.group(1).split(",")) if main else [],
              "experimental": sorted(int(x) for x in re.findall(
                  r"- os: [\w.-]+.*?\n(?:\s+\w+: .*\n)*?\s+experimental: true\n(?:\s+\w+: .*\n)*?\s+shard: (\d+)", text))}
    out = []
    for kind, n in (("main", n_main), ("experimental", n_exp)):
        if shards[kind] != list(range(1, n + 1)):
            out.append(f"{kind} jobs run shards {shards[kind]}, but the run line divides them by {n}")
    return out


class CiMatrix(unittest.TestCase):
    def test_every_share_of_the_suite_is_a_job(self):
        with open(WORKFLOW, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(problems(text), [])
        dropped = re.sub(r"(?s)(\n\s+- os: macos-latest[^\n]*\n(?:\s+\w+: [^\n]*\n)*?\s+shard: 3\n)", "\n", text, count=1)
        self.assertNotEqual(problems(dropped), [], "a dropped shard job is caught")
        # T-4633: whatever the divisor is today (3 until the macOS jobs became fifths), one more than the jobs is caught
        miscount, n = re.subn(r"(matrix\.shard \}\}/\$\{\{ matrix\.experimental && )(\d+)",  # T-4687: not the timeout line
                              lambda m: m.group(1) + str(int(m.group(2)) + 1), text)
        self.assertGreater(n, 0, "the run line names the experimental jobs' shard count")
        self.assertNotEqual(problems(miscount), [], "a run line that divides by more shards than jobs is caught")

    def test_the_experimental_job_gets_a_longer_class_watchdog(self):
        # T-2844: run 41716829800's macOS 3.14 shard killed test_exam.LeanKind at 480 s (504 s there, 85 s here)
        with open(WORKFLOW, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"--timeout \$\{\{ matrix\.experimental && (\d+) \|\| (\d+) \}\}", text)
        self.assertIsNotNone(m, "the run line sets --timeout per job kind")
        exp, main = int(m.group(1)), int(m.group(2))
        self.assertEqual(main, 480, "the main jobs keep run.py's default watchdog")
        self.assertGreaterEqual(exp, 900)
        deadline = int(re.search(r"--deadline \$\{\{ matrix\.experimental && (\d+)", text).group(1))
        self.assertLess(exp, deadline, "one class can't eat the whole experimental deadline")


if __name__ == "__main__":
    unittest.main()
