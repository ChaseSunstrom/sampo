"""T-4693: foresight reads whether a dropped task was a mistake from a field of its brief (dropped_as: mistake), set by
`sampo task drop --mistake` and by a restore, never from words in its Log. A one-time fill gives the field to the drops
the old wording matched, so no count moves. T-4680's six false 'breakage ahead' shapes replay to the verdicts it meant.

The six fixtures below are RECONSTRUCTIONS from T-4680's brief (which names five shapes: a test-only commit flagged for
leaving its adapter alone, a change flagged for leaving a claims register alone that says nothing about it, three canned
test dates never compared with the clock) plus one more shape of the same family (a date in a data file read only by
code that never reads the clock). The real rows the 2026-11-14 session dropped were not read."""
import datetime
import json
import os
import subprocess
import unittest

from helpers import ForemanTestCase
from sampo import core as c
from sampo import eco
from sampo import foresight as fs

API = "def fetch(i):\n    return i\n\ndef other(i):\n    return -i\n"
OLD_WORDS = "captured by mistake: it read a fixed date"


class DropField(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.today = datetime.date.today()
        fs.fill_dropped_as(self.p)  # the one-time fill ran already: what follows is a drop made after it

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

    def brief(self, tid):
        return c.find_brief(c.find_project(self.repo), tid)

    def capture(self, n=1, trigger="foresight-date"):
        return [eco.capture_once(self.p, f"Breakage ahead: sample {trigger} {i} {os.urandom(3).hex()}", "FIX",
                                 "foresight", key=f"k-{trigger}-{i}-{os.urandom(3).hex()}", trigger=trigger)[0].id
                for i in range(n)]

    def old_drop(self, tid, reason):  # a drop as it was written before the field: status and Log words only
        b = self.brief(tid)
        b.meta["status"] = "dropped"
        b.meta.pop("dropped_as", None)
        b.append_log(f"dropped: {reason}")
        c.save_brief(c.find_project(self.repo), b)

    def drops(self, wkind="date"):
        return fs._rate_cap(c.load_briefs(c.find_project(self.repo), include_archive=True), wkind, self.today)[1]

    def user_task(self, title):
        return json.loads(self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                                  "--step", "s", "--json").stdout)["id"]

    # --- the field: set by the drop and the restore, read by the count
    def test_a_flagged_drop_is_not_counted_whatever_its_reason_says(self):
        a, b, d = self.capture(3)
        self.fm("task", "drop", a, "--mistake")
        self.fm("task", "drop", b, "wrong task, the date is canned", "--mistake")
        self.fm("task", "drop", d, "a real breakage but too late", "--mistake")
        self.assertEqual([self.brief(t).meta.get("dropped_as") for t in (a, b, d)], ["mistake"] * 3)
        self.assertEqual(self.drops(), 0)

    def test_an_unflagged_drop_counts_even_when_its_reason_holds_the_old_words(self):
        a, b = self.capture(2)
        self.fm("task", "drop", a, "captured by mistake: wrong guess", "--force")
        self.fm("task", "drop", b, "not a real breakage", "--force")
        self.assertIsNone(self.brief(a).meta.get("dropped_as"))
        self.assertEqual(self.drops(), 2)

    def test_a_restore_says_the_drop_was_a_mistake_and_a_plain_drop_after_it_starts_over(self):
        (a,) = self.capture()
        self.fm("task", "drop", a, "not a real breakage")
        self.assertEqual(self.drops(), 1)
        self.fm("task", "set", a, "status=planned")
        self.assertEqual(self.brief(a).meta.get("dropped_as"), "mistake")
        self.assertIn("the drop was a mistake", self.brief(a).section("Log"))
        self.assertEqual(self.drops(), 0)  # planned again: open, not a drop
        self.fm("task", "drop", a, "no, it was wrong after all")
        self.assertIsNone(self.brief(a).meta.get("dropped_as"), "a fresh decision doesn't carry the old one")
        self.assertEqual(self.drops(), 1)

    def test_a_restore_of_a_flagged_drop_and_a_flagged_re_drop_keep_the_flag_honest(self):
        (a,) = self.capture()
        self.fm("task", "drop", a, "--mistake")
        self.fm("task", "set", a, "status=captured")
        self.assertEqual(self.brief(a).meta.get("dropped_as"), "mistake")
        self.fm("task", "drop", a, "--mistake")
        self.assertEqual((self.brief(a).status, self.brief(a).meta.get("dropped_as")), ("dropped", "mistake"))
        self.assertEqual(self.drops(), 0)

    def test_drop_mistake_on_an_already_dropped_task_sets_the_field(self):
        (a,) = self.capture()
        self.fm("task", "drop", a, "not a real breakage")
        self.assertEqual(self.drops(), 1)
        r = self.fm("task", "drop", a, "not a real breakage", "--mistake")
        self.assertNotIn("already", r.stdout)
        self.assertEqual(self.brief(a).meta.get("dropped_as"), "mistake")
        self.assertEqual(self.drops(), 0)
        self.assertIn("already", self.fm("task", "drop", a, "not a real breakage", "--mistake").stdout, "a repeat is a no-op")

    def test_mistake_and_done_in_go_together_never_and_a_drop_still_needs_a_reason_or_the_flag(self):
        a, b = self.capture(2)
        r = self.fm("task", "drop", a, "--mistake", "--done-in", b, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--mistake", r.stderr + r.stdout)
        r = self.fm("task", "drop", a, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--mistake", r.stderr + r.stdout, "the refusal names the flag")

    def test_only_the_mistaken_ones_among_many_drops_are_left_out_of_the_count(self):
        a, b, d, e = self.capture(4)
        self.fm("task", "drop", a, "--mistake")
        self.fm("task", "drop", b, "x", "--force")
        self.fm("task", "drop", d, "y", "--mistake")
        self.fm("task", "drop", e, "z", "--force")
        self.assertEqual(self.drops(), 2)

    # --- the user model's requests read the same field
    def test_requests_leave_out_flagged_drops_and_keep_an_unflagged_one_that_says_by_mistake(self):
        flagged, worded, plain = self.user_task("fix the parser"), self.user_task("fix the lexer"), self.user_task("fix the io")
        self.fm("task", "drop", flagged, "a sample line", "--mistake")
        self.fm("task", "drop", worded, "dropped by mistake? no: the user changed their mind")
        ids = [b.id for b, _, _ in fs._requests(self.p)]
        self.assertNotIn(flagged, ids)
        self.assertIn(worded, ids, "words in a reason are not a verdict")
        self.assertIn(plain, ids)

    # --- the one-time fill
    def fresh(self):  # the project as it was before the field: no fill done yet
        c.update_meta(self.p, dropped_as_filled=None)

    def test_the_fill_gives_the_field_to_the_old_wording_in_live_and_archived_briefs_and_nothing_else(self):
        a, b, d, e = self.capture(4)
        for tid, why in ((a, OLD_WORDS), (b, "dropped in error, my bad"), (d, "not a real breakage")):
            self.old_drop(tid, why.replace("dropped in error", "was raised in error") if tid == b else why)
        keep = self.brief(e)  # an open one is left alone, whatever its Log says
        keep.append_log("noted: raised by mistake, fixing")
        c.save_brief(self.p, keep)
        old = self.brief(a)
        month = os.path.join(self.p.dir, "archive", "2026-10")
        os.makedirs(month, exist_ok=True)
        moved = os.path.join(month, os.path.basename(old.path))
        os.rename(old.path, moved)
        self.fresh()
        before = fs._rate_cap(c.load_briefs(self.p, include_archive=True), "date", self.today)[1]
        self.assertEqual(before, 3, "with no field yet every drop reads as counted")
        done = fs.fill_dropped_as(self.p)
        self.assertEqual(sorted(done), sorted([a, b]))
        briefs = {x.id: x for x in c.load_briefs(self.p, include_archive=True)}
        self.assertEqual({t: briefs[t].meta.get("dropped_as") for t in (a, b, d, e)},
                         {a: "mistake", b: "mistake", d: None, e: None})
        self.assertEqual(briefs[a].path, moved, "an archived brief is filled where it lies")
        self.assertEqual(fs._rate_cap(list(briefs.values()), "date", self.today)[1], 1)  # the old reading: the d one only

    def test_the_fill_counts_match_the_old_reading_for_a_mixed_history(self):
        ids = self.capture(5)
        reasons = [OLD_WORDS, "x", "failed in error", "by accident, sorry", "y"]
        for tid, why in zip(ids, reasons):
            self.old_drop(tid, why)
        self.fresh()
        old = sum(not fs._OLD_SLIP.search(b.section("Log")) for b in c.load_briefs(self.p, include_archive=True)
                  if b.status == "dropped")
        fs.fill_dropped_as(self.p)
        self.assertEqual(self.drops(), old)

    def test_the_fill_is_one_time_and_a_later_old_wording_is_not_read(self):
        (a,) = self.capture()
        self.old_drop(a, OLD_WORDS)
        self.fresh()
        self.assertEqual(fs.fill_dropped_as(self.p), [a])
        self.assertTrue(c.read_meta(self.p).get("dropped_as_filled"))
        (b,) = self.capture()
        self.old_drop(b, OLD_WORDS)
        self.assertEqual(fs.fill_dropped_as(self.p), [], "marked: it does not read words again")
        self.assertIsNone(self.brief(b).meta.get("dropped_as"))
        self.assertEqual(self.drops(), 1)

    def test_the_fill_runs_from_the_tick_and_from_the_requests_before_either_counts(self):
        (a,) = self.capture()
        self.old_drop(a, OLD_WORDS)
        u = self.user_task("fix the parser")
        self.old_drop(u, "captured by mistake: a sample line")
        self.fresh()
        self.assertNotIn(u, [b.id for b, _, _ in fs._requests(self.p)])
        self.assertEqual(self.brief(u).meta.get("dropped_as"), "mistake")
        self.fresh()
        self.tick()
        self.assertEqual(self.brief(a).meta.get("dropped_as"), "mistake")
        self.assertTrue(c.read_meta(self.p).get("dropped_as_filled"))

    def test_no_word_list_is_on_the_read_path(self):
        import inspect
        for fn in (fs._mistaken, fs._rate_cap, fs._requests):
            self.assertNotIn("_OLD_SLIP", inspect.getsource(fn))
        self.assertEqual(sorted(n for n in dir(fs) if n == "_SLIP"), [], "the old name is gone")

    # --- the rate, end to end: dropped as a mistake, the rule keeps its cap
    def lics(self, n):
        self.commit({f"lic{i}.py": f"import datetime\nE = '{self.day(3 + i)}'\nOK = datetime.date.today() < datetime.date.fromisoformat(E)\n"
                     for i in range(n)})

    def captures(self):
        return [b for b in c.load_briefs(c.find_project(self.repo)) if b.meta.get("trigger") == "foresight-date"]

    def test_mistake_drops_leave_the_cap_alone_and_plain_ones_lower_it(self):
        self.lics(12)
        self.tick()
        first = self.captures()
        self.assertEqual(len(first), fs.WARN_MAX)
        for b in first[:2]:
            self.fm("task", "drop", b.id, "the date is canned", "--mistake")
        self.tick(1)
        second = len(self.captures()) - len(first)
        self.assertEqual(second, fs.WARN_MAX, "two mistaken drops: nothing lowered")
        for b in self.captures()[-2:]:
            self.fm("task", "drop", b.id, "not a real breakage", "--force")
        before = len(self.captures())
        self.tick(2)
        self.assertEqual(len(self.captures()) - before, fs.WARN_MAX - 2)

    # --- the six reconstructed false alarms: each is booked a warning-skip and files nothing
    def after_pairs(self, a, b, alone):
        self.tick()
        for i in range(fs.PAIR_MIN):
            self.commit({a[0]: a[1] + f"# {i}\n", b[0]: b[1] + f"# {i}\n"}, f"pair {i}")
        self.tick(1)
        self.commit(alone, "alone")
        self.tick(2)

    def assertNoAlarm(self, targets):
        self.assertEqual(self.book("warning"), [])
        self.assertEqual(self.captures(), [])
        skipped = self.book("warning-skip")
        self.assertEqual(sorted(r["target"] for r in skipped), sorted(targets))
        self.assertTrue(all(r["why"] for r in skipped))

    def test_fixture_1_a_test_only_commit_is_not_flagged_for_leaving_its_adapter_alone(self):
        tests = ("test_api.py", "def test_one():\n    assert True\n")
        self.after_pairs(tests, ("adapter.py", "def send(x):\n    return x\n"),
                         {"test_api.py": tests[1] + "\ndef test_two():\n    assert 1\n"})
        self.assertNoAlarm(["adapter.py"])

    def test_fixture_2_a_change_a_claims_register_says_nothing_about_is_not_flagged(self):
        register = ("claims.json", json.dumps({"claims": ["the report totals are rounded to cents"]}))
        self.after_pairs(("api.py", API), register, {"api.py": API.replace("return i\n", "return i + 1\n", 1)})
        self.assertNoAlarm(["claims.json"])

    def test_fixture_3_a_canned_date_compared_only_with_a_fixed_date_is_not_flagged(self):
        self.commit({"test_span.py": "import datetime\n\ndef test_span():\n"
                     f"    a, b = datetime.date.fromisoformat('{self.day(5)}'), datetime.date(2020, 1, 1)\n    assert a > b\n"})
        self.tick()
        self.assertNoAlarm(["test_span.py"])

    def test_fixture_4_a_canned_date_under_a_frozen_clock_is_not_flagged(self):
        self.commit({"test_freeze.py": "from freezegun import freeze_time\n\n@freeze_time('2020-01-01')\n"
                     f"def test_t():\n    import datetime\n    assert datetime.date.today() < datetime.date.fromisoformat('{self.day(6)}')\n"})
        self.tick()
        self.assertNoAlarm(["test_freeze.py"])

    def test_fixture_5_a_canned_date_only_parsed_and_formatted_is_not_flagged(self):
        self.commit({"test_fmt.py": "import datetime\n\ndef test_fmt():\n"
                     f"    d = datetime.date.fromisoformat('{self.day(7)}')\n    assert d.strftime('%Y') == '{self.day(7)[:4]}'\n"})
        self.tick()
        self.assertNoAlarm(["test_fmt.py"])

    def test_fixture_6_a_date_in_a_data_file_read_by_code_that_never_reads_the_clock_is_not_flagged(self):
        self.commit({"fixtures/plan.json": json.dumps({"ends": self.day(4)}),
                     "reader.py": "import json\n\ndef load():\n    return json.load(open('fixtures/plan.json'))\n"})
        self.tick()
        self.assertNoAlarm(["fixtures/plan.json"])


if __name__ == "__main__":
    unittest.main()
