"""T-0077: FOREMAN_QUIET=1 marks a session another tool drives (an orchestrator): Foreman adds no context, nudges or
brief requirement there, and the guard still blocks what it always blocks."""
import os
import re
import time
import unittest

from helpers import ForemanTestCase

QUIET = {"FOREMAN_QUIET": "1"}


class Quiet(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def test_no_context_nudges_or_brief_requirement(self):
        for event, payload in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "fix the bug"}),
                               ("Stop", {})):
            p = self.hook(event, payload, env=QUIET)
            self.assertEqual((p.returncode, p.stdout.strip()), (0, ""), event)
        edit = {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.repo, "app.py"), "content": "x"}}
        self.assertEqual(self.hook("PreToolUse", edit, env=QUIET).returncode, 0)
        self.assertEqual(self.hook("PreToolUse", edit).returncode, 2, "without it the brief requirement holds")

    def test_the_guard_still_blocks(self):
        p = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf ~"}}, env=QUIET)
        self.assertEqual(p.returncode, 2)


class QuietKeepsTheFullRun(ForemanTestCase):
    """T-4825 (rtk's tee/recall): `sampo quiet` prints the tail of a failed run, and keeps the whole run in a file under
    Sampo's state, named on the ✗ line; a pass or a run the tail already shows keeps nothing; the folder stays bounded."""

    def kept(self, slug=None):
        from sampo import core as c
        folder = os.path.join(c.state_dir(), "outputs", "quiet", slug or c.find_project(self.repo).slug)
        return folder, sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def keep(self, text):
        """_keep_quiet as run from the test repo."""
        from sampo import cli
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            return cli._keep_quiet(text)
        finally:
            os.chdir(old)

    def test_a_cut_failed_run_keeps_its_full_output_and_names_it(self):
        self.fm("init")
        r = self.fm("quiet", "--tail", "3", "--", "sh", "-c", "seq 1 10; exit 3", check=False)
        self.assertEqual(r.returncode, 3)
        last = r.stdout.strip().splitlines()[-1]
        m = re.fullmatch(r"✗ exit 3 \([\d.]+ s; last 3 of 10 lines; full output: (\S+)\)", last)
        self.assertTrue(m, last)  # recall.failure_signature still reads the line as quiet's tally
        path = m.group(1)
        folder, names = self.kept()
        self.assertEqual(os.path.dirname(path), folder)
        self.assertNotIn(os.path.realpath(self.repo), os.path.realpath(path), "state, not the repo tree")
        with open(path) as f:
            self.assertEqual(f.read().split(), [str(i) for i in range(1, 11)], "all of it, not the tail")
        self.assertEqual(os.stat(path).st_mode & 0o077, 0, "outputs can hold secrets: the user's eyes only")
        self.assertNotIn("1\n2\n", r.stdout.split("✗")[0], "the screen still shows only the tail")

    def test_a_pass_or_an_uncut_failure_keeps_nothing(self):
        self.fm("init")
        r = self.fm("quiet", "--", "sh", "-c", "seq 1 10", check=False)
        self.assertEqual((r.returncode, "full output" in r.stdout), (0, False))
        r = self.fm("quiet", "--tail", "40", "--", "sh", "-c", "seq 1 5; exit 1", check=False)
        self.assertEqual((r.returncode, "full output" in r.stdout), (1, False), "the tail held all five lines")
        self.assertEqual(self.kept()[1], [])

    def test_the_folder_keeps_the_last_twenty_and_each_is_capped(self):
        from unittest import mock
        from sampo import cli
        self.fm("init")
        old = time.time() - 7200  # past the hour a file is always kept
        with mock.patch.object(cli, "QUIET_KEEP_BYTES", 1000):
            for n in range(23):
                path = self.keep(f"run {n}\n" + "x" * 5000)
                os.utime(path, (old + n, old + n))  # a clock that can't tie two runs
        folder, names = self.kept()
        self.assertEqual(len(names), cli.QUIET_KEEP_FILES)
        self.assertEqual(cli.QUIET_KEEP_FILES, 20)
        sizes = [os.path.getsize(os.path.join(folder, n)) for n in names]
        self.assertTrue(all(s < 1500 for s in sizes), sizes)
        with open(os.path.join(folder, names[-1])) as f:
            self.assertIn("cut from the middle", f.read())

    def test_the_cap_counts_bytes_not_characters(self):
        from unittest import mock
        from sampo import cli
        self.fm("init")
        with mock.patch.object(cli, "QUIET_KEEP_BYTES", 1000):
            path = self.keep("é" * 5000 + "\n" + "日" * 5000)  # two bytes and three bytes a character
        self.assertLess(os.path.getsize(path), 1200, "1,000 bytes, the cut marker and a newline")
        with open(path, encoding="utf-8") as f:
            self.assertIn("cut from the middle", f.read())

    def test_each_project_has_its_own_twenty_and_a_young_file_is_never_pruned(self):
        from sampo import cli
        self.fm("init")
        other = os.path.join(self.kept("another-project-abc123")[0])
        os.makedirs(other)
        for n in range(30):  # another project's failing runs, all old
            with open(os.path.join(other, f"2020{n:04d}.txt"), "w") as f:
                f.write("theirs")
        mine = [self.keep(f"run {n}") for n in range(25)]  # all younger than an hour
        self.assertEqual(len(self.kept()[1]), 25, "a file under an hour old is never pruned, even past the limit")
        self.assertEqual(len(os.listdir(other)), 30, "one project's runs never evict another's")
        past = time.time() - 7200
        for n, path in enumerate(mine):
            os.utime(path, (past + n, past + n))
        self.keep("one more")  # now the old ones can go, down to the limit
        self.assertEqual(len(self.kept()[1]), cli.QUIET_KEEP_FILES)
        self.assertTrue(os.path.exists(mine[-1]) and not os.path.exists(mine[0]))

    def test_the_folder_is_private_and_a_planted_file_is_not_overwritten(self):
        from unittest import mock
        self.fm("init")
        path = self.keep("first")
        folder = os.path.dirname(path)
        os.chmod(folder, 0o755)  # made by something looser
        with mock.patch("time.time_ns", return_value=42), mock.patch("time.strftime", return_value="20260101-000000"):
            first = self.keep("a")
            second = self.keep("b")  # the same name: O_EXCL refuses, and the run is still kept under another
        self.assertEqual(os.stat(folder).st_mode & 0o077, 0, "the folder is 0700 whatever made it")
        self.assertTrue(first and second and first != second)
        with open(first) as f:
            self.assertEqual(f.read(), "a\n")


if __name__ == "__main__":
    unittest.main()
