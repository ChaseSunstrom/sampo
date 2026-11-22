"""T-1164, T-5723: fm lane brief lists the defect classes reviewers confirmed in the last 30 days on the brief's own scope
files (a class once, with its count and an example, the class read from the cache of a local-model pass); rejected and older
findings are ignored; with none there is no section. The cached pass itself is test_lanes.DefectClassesCached."""
import json
import os
import unittest

from helpers import ForemanTestCase, read_text

from sampo import core as c
from sampo import lanes


class DefectClasses(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def finding(self, text, verdict="confirmed", file="hooks.py"):
        c.log_event(self.p, "finding", task="T-9000", data={"lens": "edge", "verdict": verdict, "text": text, "file": file})

    def brief(self):
        tid = json.loads(self.fm("task", "new", "X", "--type", "FEATURE", "--tier", "M", "--ac", "ok :: true",
                                 "--step", "s", "--scope", "hooks.py", "--interpretation", "Reading 1: a. Reading 2: b.",
                                 "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a",
                                 "--json").stdout)["id"]
        path = json.loads(self.fm("lane", "brief", tid, "--json", env={"MOONS_LOCAL": ""}).stdout)["path"]
        return read_text(path)

    def test_two_confirmed_of_one_class_print_once_with_count(self):
        a = "The new check runs on every hook call, a cost on the hot path"
        b = "Another scan of the whole ledger per prompt, again a hot path cost"
        self.finding(a)
        self.finding(b)
        self.finding("a hot path read, but rejected by the main thread", "rejected")
        lanes._classes_save(self.p, {lanes._finding_key(a): "cost", lanes._finding_key(b): "cost"}, 0)
        text = self.brief()
        self.assertIn("Defects this project keeps shipping", text)
        lines = [ln for ln in text.splitlines() if ln.startswith("- cost at scale on a hot path: 2")]
        self.assertEqual(len(lines), 1, text)
        self.assertIn("Another scan of the whole ledger", lines[0])  # the newest as the example

    def test_rejected_and_old_findings_make_no_class(self):
        self.finding("a hot path cost on every prompt, rejected", "rejected")
        old = {"ts": "2020-01-01T00:00:00+00:00", "task": "T-9001", "event": "finding",
               "data": {"lens": "edge", "verdict": "confirmed", "file": "hooks.py", "text": "an old hot path cost on every prompt"}}
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a") as f:
            f.write(json.dumps(old) + "\n")
        self.assertEqual(lanes.defect_classes(self.p, ["hooks.py"]), [])

    def test_no_findings_no_section(self):
        self.assertNotIn("keeps shipping", self.brief())


if __name__ == "__main__":
    unittest.main()
