"""T-5599: `task show IDS [--section NAME]` and `task ls IDS` take several ids in one call (T-1 T-3, T-1,T-3..T-5); a
missing id is named and the rest still print (exit 1); bare `task ls` is still the queue; one id prints as it always did."""
import json
import unittest

from helpers import ForemanTestCase


class MultiShow(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "A thing", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--approach", "Pick it: the first option", "--focus")  # T-0001
        self.fm("capture", "Idea bravo in its own words", "--anyway")  # T-0002
        self.fm("capture", "Idea charlie in its own words", "--anyway")  # T-0003

    def test_two_ids_print_each_header_and_its_section_only(self):
        r = self.fm("task", "show", "T-0001", "T-0002", "--section", "Approach")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(r.stdout.index("# T-0001 A thing"), r.stdout.index("# T-0002 Idea bravo"), r.stdout)
        self.assertIn("Pick it: the first option", r.stdout)
        self.assertNotIn("## Acceptance criteria", r.stdout)  # the section, not the whole brief

    def test_comma_and_range_spellings_keep_their_order(self):
        r = self.fm("task", "show", "T-0003,T-0001..T-0002", "--section", "Approach")
        self.assertEqual(r.returncode, 0, r.stderr)
        heads = [r.stdout.index(f"# {t} ") for t in ("T-0003", "T-0001", "T-0002")]
        self.assertEqual(heads, sorted(heads), r.stdout)
        whole = self.fm("task", "show", "T-0001..T-0002")
        self.assertEqual(whole.stdout.count("## Steps"), 2, whole.stdout)  # the whole brief, once per task

    def test_a_missing_id_is_named_and_the_rest_still_print(self):
        r = self.fm("task", "show", "T-0001", "T-0099", "--section", "Approach", check=False)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("sampo: task show T-0099: no task T-0099", r.stderr)
        self.assertIn("Pick it: the first option", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_json_is_one_array_and_a_missing_id_is_named(self):
        rows = json.loads(self.fm("task", "show", "T-0002", "T-0001", "--json").stdout)
        self.assertEqual([x["id"] for x in rows], ["T-0002", "T-0001"])
        self.assertIn("steps_total", rows[0])  # the same keys one id prints
        r = self.fm("task", "show", "T-0001", "T-0099", "--json", check=False)
        self.assertEqual((r.returncode, [x["id"] for x in json.loads(r.stdout)]), (1, ["T-0001"]), r.stderr)
        self.assertIn("T-0099", r.stderr)
        sec = json.loads(self.fm("task", "show", "T-0001", "--section", "Approach", "--json").stdout)
        self.assertEqual(sec, {"id": "T-0001", "section": "Approach", "text": "Pick it: the first option"})

    def test_one_id_is_unchanged_and_a_section_alone_has_no_header(self):
        self.assertTrue(self.fm("task", "show", "T-0001").stdout.startswith("---\n"))
        r = self.fm("task", "show", "T-0001", "--section", "Approach")
        self.assertEqual(r.stdout.strip(), "Pick it: the first option")
        self.assertNotIn("# T-0001", r.stdout)

    def test_an_unknown_section_is_named_and_prints_nothing(self):
        r = self.fm("task", "show", "T-0001", "T-0002", "--section", "Nope", check=False)
        self.assertEqual((r.returncode, r.stdout), (1, ""), r.stdout + r.stderr)
        self.assertIn("sampo: task show T-0001: no section 'Nope'", r.stderr)
        self.assertIn("Approach", r.stderr)  # the sections it has

    def test_a_word_that_is_not_an_id_refuses_before_any_print(self):
        r = self.fm("task", "show", "T-0001", "extra", "--section", "Approach", check=False)
        self.assertEqual((r.returncode != 0, r.stdout, "extra" in r.stderr), (True, "", True), r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_an_option_between_the_ids_still_takes_them_all(self):
        r = self.fm("task", "show", "T-0001", "--section", "Approach", "T-0002")
        self.assertIn("# T-0002 Idea bravo", r.stdout, r.stdout + r.stderr)


class MultiLs(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "A thing", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")  # T-0001
        self.fm("capture", "Idea bravo in its own words", "--anyway")  # T-0002

    def test_one_line_per_task_named(self):
        lines = self.fm("task", "ls", "T-0001", "T-0002").stdout.splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("T-0001 "), lines)
        for want in ("active", "FIX", "S", "A thing", "criteria 0/1", "steps 0/1"):
            self.assertIn(want, lines[0])
        self.assertIn("captured", lines[1])
        self.assertIn("Idea bravo in its own words", lines[1])

    def test_a_range_and_json_and_a_missing_id(self):
        self.assertEqual(len(self.fm("task", "ls", "T-0001..T-0002").stdout.splitlines()), 2)
        rows = json.loads(self.fm("task", "ls", "T-0002", "T-0001", "--json").stdout)
        self.assertEqual([x["id"] for x in rows], ["T-0002", "T-0001"])
        self.assertEqual((rows[1]["criteria_met"], rows[1]["criteria_total"]), (0, 1))
        r = self.fm("task", "ls", "T-0001", "T-0099", check=False)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("sampo: task ls T-0099: no task T-0099", r.stderr)
        self.assertTrue(r.stdout.startswith("T-0001 "), r.stdout)

    def test_bare_ls_is_still_the_queue(self):
        r = self.fm("task", "ls")
        self.assertIn("(ran `fm queue`", r.stderr)
        self.assertEqual(r.stdout, self.fm("queue").stdout)  # the queue's own lines, not one per id


if __name__ == "__main__":
    unittest.main()
