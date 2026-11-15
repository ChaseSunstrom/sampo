"""T-4680: a 'breakage ahead' alarm (the co-change and the canned-date detectors of foresight.breakage_tick) becomes a
task only when its own probe shows the link in the code, and a rule whose captures were dropped raises fewer. Each
false shape a session dropped by hand on 2026-11-14 is built as a small scratch repo; each true shape still files."""
import datetime
import json
import os
import subprocess
import unittest

from helpers import ForemanTestCase
from sampo import core as c
from sampo import foresight as fs

API = "def fetch(i):\n    return i\n\ndef other(i):\n    return -i\n"
CLIENT = "from api import fetch\n\ndef call(i):\n    return fetch(i)\n"


class BreakageProbe(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.today = datetime.date.today()

    def commit(self, files, msg="c"):
        for name, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.repo, name)), exist_ok=True)
            with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
                f.write(text)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", msg], check=True)

    def tick(self, days=0):
        return fs.breakage_tick(c.find_project(self.repo), today=self.today + datetime.timedelta(days=days),
                                budget=10 ** 12)

    def book(self, kind):
        try:
            with open(os.path.join(self.p.dir, fs.PREDICTIONS), encoding="utf-8") as f:
                return [r for r in map(json.loads, f) if r.get("kind") == kind]
        except OSError:
            return []

    def day(self, n):
        return (self.today + datetime.timedelta(days=n)).isoformat()

    def pairs(self, a, b, n=None):  # the two files change together n times, the way a module and its partner do
        for i in range(n or fs.PAIR_MIN):
            self.commit({a[0]: a[1] + f"# {i}\n", b[0]: b[1] + f"# {i}\n"}, f"pair {i}")

    def after_pairs(self, a, b, alone):
        self.tick()
        self.pairs(a, b)
        self.tick(1)
        self.commit(alone, "alone")
        self.tick(2)

    # --- co-change: the link is a name the diff changed that the untouched file refers to
    def test_a_test_only_commit_is_not_flagged_for_leaving_its_adapter_alone(self):
        tests = ("test_api.py", "def test_one():\n    assert True\n")
        adapter = ("adapter.py", "def send(x):\n    return x\n")
        self.after_pairs(tests, adapter, {"test_api.py": tests[1] + "# 4\n\ndef test_two():\n    assert 1\n"})
        self.assertEqual(self.book("warning"), [])
        skipped = self.book("warning-skip")
        self.assertEqual([(r["wkind"], r["target"]) for r in skipped], [("cochange", "adapter.py")])
        self.assertTrue(skipped[0]["why"])

    def test_a_change_the_untouched_claims_register_says_nothing_about_is_not_flagged(self):
        register = ("claims.json", json.dumps({"claims": ["the report totals are rounded to cents"]}))
        self.after_pairs(("api.py", API), register, {"api.py": API.replace("return i\n", "return i + 1\n", 1)})
        self.assertEqual(self.book("warning"), [])
        self.assertEqual(len(self.book("warning-skip")), 1)

    def test_a_register_with_a_claim_about_the_changed_name_is_flagged(self):
        register = ("claims.json", json.dumps({"claims": ["fetch returns its argument unchanged"]}))
        self.after_pairs(("api.py", API), register, {"api.py": API.replace("return i\n", "return i + 1\n", 1)})
        self.assertEqual([(r["wkind"], r["target"]) for r in self.book("warning")], [("cochange", "claims.json")])

    def test_an_untouched_caller_of_a_renamed_function_is_flagged(self):
        self.after_pairs(("api.py", API), ("client.py", CLIENT), {"api.py": API.replace("fetch", "get")})
        self.assertEqual([(r["wkind"], r["target"]) for r in self.book("warning")], [("cochange", "client.py")])
        self.assertEqual(self.book("warning-skip"), [])

    def test_a_changed_function_the_partner_never_names_is_not_flagged(self):
        self.after_pairs(("api.py", API), ("client.py", CLIENT),
                         {"api.py": API.replace("return -i", "return -i - 1")})  # other() changed; the client calls fetch()
        self.assertEqual(self.book("warning"), [])

    # --- canned dates: the link is a read of the clock that the date reaches
    def test_a_canned_date_only_compared_with_fixed_data_is_not_flagged(self):
        d = self.day(5)
        self.commit({"test_fixed.py": "import datetime\n\ndef test_span():\n"
                     f"    a, b = datetime.date.fromisoformat('{d}'), datetime.date(2020, 1, 1)\n    assert a > b\n",
                     "test_freeze.py": "from freezegun import freeze_time\n\n@freeze_time('2020-01-01')\n"
                     f"def test_t():\n    import datetime\n    assert datetime.date.today() < datetime.date.fromisoformat('{self.day(6)}')\n"})
        self.tick()
        self.assertEqual(self.book("warning"), [])
        self.assertEqual(sorted(r["target"] for r in self.book("warning-skip")), ["test_fixed.py", "test_freeze.py"])

    def test_a_date_compared_with_the_clock_is_flagged_in_the_file_or_through_code_it_imports(self):
        self.commit({"lic.py": f"import datetime\nEXPIRES = '{self.day(5)}'\n\n"
                     "def ok():\n    return datetime.date.today() < datetime.date.fromisoformat(EXPIRES)\n",
                     "clock.py": "import time\n\ndef late(t):\n    return time.time() > t\n",
                     "test_clock.py": f"from clock import late\n\ndef test_it():\n    assert not late('{self.day(6)}')\n"})
        self.tick()
        self.assertEqual(sorted(r["target"] for r in self.book("warning")), ["lic.py", "test_clock.py"])

    def test_a_date_in_a_data_file_read_only_by_code_that_never_reads_the_clock_is_not_flagged(self):
        self.commit({"fixtures/plan.json": json.dumps({"ends": self.day(4)}),
                     "reader.py": "import json\n\ndef load():\n    return json.load(open('fixtures/plan.json'))\n"})
        self.tick()
        self.assertEqual(self.book("warning"), [])
        self.assertEqual([r["target"] for r in self.book("warning-skip")], ["fixtures/plan.json"])

    # --- the rate: a rule whose captures were dropped raises fewer until one is confirmed
    def lics(self, n):
        self.commit({f"lic{i}.py": f"import datetime\nE = '{self.day(3 + i)}'\nOK = datetime.date.today() < datetime.date.fromisoformat(E)\n"
                     for i in range(n)})

    def captures(self):
        return [b for b in c.load_briefs(c.find_project(self.repo)) if b.meta.get("trigger") == "foresight-date"]

    def test_two_dropped_captures_lower_what_the_rule_raises_next_and_a_confirmed_one_restores_it(self):
        self.lics(9)
        self.tick()
        first = self.captures()
        self.assertEqual(len(first), fs.WARN_MAX)
        for b in first[:2]:
            self.fm("task", "drop", b.id, "not a real breakage", "--force")
        self.tick(1)
        self.assertEqual(len(self.captures()) - len(first), fs.WARN_MAX - 2)  # two dropped: one raised
        self.assertTrue([r for r in self.book("warning-skip") if "dropped" in r["why"]])
        p = c.find_project(self.repo)
        keep = c.find_brief(p, first[2].id)
        keep.meta["status"] = "done"  # the third was acted on: confirmed
        c.save_brief(p, keep)
        before = len(self.captures())
        self.tick(2)
        self.assertEqual(len(self.captures()) - before, fs.WARN_MAX)

    def test_a_rule_with_many_drops_is_log_only_but_for_one_probation_capture_a_week(self):
        self.lics(12)
        for day in range(3):
            self.tick(day)
            for b in self.captures():
                if b.status not in c.CLOSED:
                    self.fm("task", "drop", b.id, "not a real breakage", "--force")
        n = len(self.captures())
        self.assertEqual(n, fs.WARN_MAX)  # three raised the first day and all dropped: nothing more raised after
        self.tick(3)
        self.assertEqual(len(self.captures()), n)  # log-only
        self.tick(10)
        self.assertEqual(len(self.captures()), n + 1)  # a week on: one probation capture


if __name__ == "__main__":
    unittest.main()
