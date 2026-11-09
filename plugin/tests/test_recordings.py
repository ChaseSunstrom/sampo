"""T-3989: a recording (a cast, an event log, an SVG, a video or a screenshot) never changes along with the code it
shows, so nothing said when demo.svg still typed `fm demo` three weeks after the rename. doctor.check_recordings reads
each one's text for the names Sampo retired and flags one older than STALE_DAYS whose depicted code changed since;
scout files its FAIL into the inbox, so sampo next works it."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from helpers import PLUGIN, git_repo

from sampo import doctor as d

ROOT = os.path.dirname(PLUGIN)
CAST = json.dumps({"version": 2, "width": 80, "height": 24}) + "\n"


def commit(root, rel, text, days_ago=0):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    at = f"@{int(time.time() - days_ago * 86400)} +0000"
    env = dict(os.environ, GIT_AUTHOR_DATE=at, GIT_COMMITTER_DATE=at)
    subprocess.run(["git", "-C", root, "add", rel], check=True)
    subprocess.run(["git", "-C", root, "commit", "-qm", rel], check=True, env=env)


def cast(*lines):
    return CAST + "".join(json.dumps([0.1 * i, "o", s]) + "\n" for i, s in enumerate(lines))


class Recordings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fm-recordings-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = git_repo(self.tmp)
        commit(self.root, "plugin/lib/sampo/demo.py", "# the demo\n", days_ago=40)

    def test_a_recording_that_types_the_old_cli_fails_naming_it_and_its_remake(self):
        commit(self.root, "demo/demo.cast", cast("$ ", "f", "m", " demo --pace 1\r\n", "\x1b[1mSampo demo\x1b[0m\r\n"))
        r = d.check_recordings(self.root)
        self.assertEqual(r.status, "FAIL", r.detail)
        self.assertIn("demo/demo.cast", r.detail)
        self.assertIn("`fm demo` (now `sampo demo`)", r.detail)
        self.assertIn("python3 demo/record.py", r.detail)

    def test_a_renamed_verb_and_the_old_product_name_fail_in_an_event_log_and_an_svg(self):
        commit(self.root, "demo/code-tui.jsonl", json.dumps({"event": "say", "text": "run sampo code tui"}) + "\n")
        commit(self.root, "demo/code-tui-tour.svg", '<svg><text x="1">Foreman</text><text x="9">guard</text></svg>')
        r = d.check_recordings(self.root)
        self.assertEqual(r.status, "FAIL", r.detail)
        self.assertIn("demo/code-tui.jsonl shows `sampo code` (now `sampo smith`)", r.detail)
        self.assertIn("demo/code-tui-tour.svg shows `Foreman` (now `Sampo`)", r.detail)

    def test_svg_text_split_across_elements_still_reads_as_words(self):
        commit(self.root, "demo/demo.svg", '<svg><text x="0">$</text><text x="2">fm</text><text x="5">demo</text></svg>')
        self.assertIn("`fm demo`", d.check_recordings(self.root).detail)

    def test_a_recording_older_than_its_depicted_code_warns_and_a_fresh_one_passes(self):
        # a WARN, not a FAIL: sampo check runs the doctor, and a gate must not go red with the calendar
        commit(self.root, "demo/demo.cast", cast("$ sampo demo\r\n"), days_ago=30)
        commit(self.root, "plugin/lib/sampo/demo.py", "# the demo, changed\n", days_ago=10)
        r = d.check_recordings(self.root)
        self.assertEqual(r.status, "WARN", r.detail)
        self.assertIn("demo/demo.cast (recorded ", r.detail)
        # the same words each day it stays stale, so scout files it once (its key hashes the text)
        self.assertIn(") is over 21 days old and plugin/lib/sampo/demo.py changed since: python3 demo/record.py", r.detail)
        self.assertEqual(r.detail, d.check_recordings(self.root).detail)
        with open(os.path.join(self.root, "demo", "demo.cast"), "a", encoding="utf-8") as f:
            f.write(json.dumps([9, "o", "re-recorded, not committed yet"]) + "\n")
        self.assertEqual(d.check_recordings(self.root).status, "PASS")  # a re-record waiting for its commit
        commit(self.root, "demo/demo.cast", cast("$ sampo demo --pace 1\r\n"))
        self.assertEqual(d.check_recordings(self.root).status, "PASS")

    def test_an_old_recording_whose_code_never_changed_passes(self):
        commit(self.root, "demo/demo.cast", cast("$ sampo demo\r\n"), days_ago=30)
        self.assertEqual(d.check_recordings(self.root).status, "PASS")

    def test_a_recording_no_entry_maps_fails_so_a_new_one_says_what_it_shows(self):
        commit(self.root, "demo/new-thing.gif", "GIF89a")
        r = d.check_recordings(self.root)
        self.assertEqual(r.status, "FAIL", r.detail)
        self.assertIn("demo/new-thing.gif: no doctor.RECORDINGS entry says what it shows", r.detail)

    def test_outside_a_sampo_checkout_it_passes_without_reading(self):
        self.assertEqual(d.check_recordings(self.tmp).status, "PASS")
        plain = git_repo(self.tmp, "plain")
        commit(plain, "demo/demo.cast", cast("$ fm demo\r\n"))
        self.assertEqual(d.check_recordings(plain).status, "PASS")

    def test_doctor_runs_it(self):
        src = open(d.__file__, encoding="utf-8").read()
        self.assertIn("results.append(check_recordings(", src)


class ThisRepo(unittest.TestCase):
    def test_every_recording_here_is_mapped_and_free_of_retired_names(self):
        # the text half only: an age check here would turn red with the calendar on a lane that changed nothing
        self.assertEqual(d.recording_problems(ROOT, age=False), ([], []))


if __name__ == "__main__":
    unittest.main()
