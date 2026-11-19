"""T-4862: a lane check's retry appends to its check/ID.log, so the first try's lines survive a retry. Each try starts with
a header line (try N, the tip, the UTC time); a try 1 start writes a fresh file. The detached start is faked here
(train.subprocess.Popen writes one line into the stdout file it is given), so no real check runs."""
# map-links: plugin/lib/sampo/train.py
import os
import re
from unittest import mock

from test_lane_land import LandCase

from sampo import core as c
from sampo import train

HEADER = r"^sampo: try %d on %s at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"  # the header a try starts its output with


class CheckLog(LandCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("FOREMAN_NO_BACKGROUND", None)  # the spawn is the code under test (SampoTestCase restores it)
        self.p = c.find_project(self.repo)
        self.b = c.find_brief(self.p, self.lane(1, "f1.txt"))
        self.tip = self.git("rev-parse", "HEAD", cwd=os.path.join(self.tmp, "lane1"))
        self.starts = 0
        popen = mock.patch.object(train.subprocess, "Popen", side_effect=self.fake_popen)
        popen.start()
        self.addCleanup(popen.stop)

    def fake_popen(self, argv, **kw):
        """A check whose one output line names its start, written to the log file the spawn gave it."""
        self.starts += 1
        kw["stdout"].write(f"check output {self.starts}\n")
        kw["stdout"].flush()
        return mock.Mock(pid=os.getpid())

    def spawn(self, tries):
        return train._spawn_check(self.p, self.b, self.repo, tip=self.tip, since=None, tries=tries)

    def text(self, log):
        with open(log, encoding="utf-8") as f:
            return f.read()

    def header(self, tries):
        return re.compile(HEADER % (tries, re.escape(self.tip[:9])))

    def test_retry_keeps_the_first_try_and_marks_each_try(self):  # T-4862
        _, log = self.spawn(1)
        _, log2 = self.spawn(2)
        self.assertEqual(log, log2)
        lines = self.text(log).splitlines()
        self.assertEqual(len(lines), 4, lines)
        self.assertRegex(lines[0], self.header(1))
        self.assertEqual(lines[1], "check output 1")
        self.assertRegex(lines[2], self.header(2))
        self.assertEqual(lines[3], "check output 2")

    def test_try_1_starts_the_log_fresh(self):  # T-4862
        _, log = self.spawn(1)
        with open(log, "w", encoding="utf-8") as f:  # what an earlier tip's check left behind
            f.write("stale line from an older tip\n")
        self.spawn(1)
        text = self.text(log)
        self.assertNotIn("stale line", text)
        self.assertRegex(text.splitlines()[0], self.header(1))
        self.assertEqual(text.count("sampo: try "), 1, text)
        self.assertEqual(text.splitlines()[1], "check output 2")
