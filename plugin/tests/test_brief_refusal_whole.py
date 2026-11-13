"""T-4545: `sampo lane brief` on an M brief names every missing piece in its first refusal (T-4492 landed the empty-section
shape; T-4536 still took four rounds: the Approach rules hid behind one another), and the Approach section is accepted by
its heading as the brief prints it, 'Approach (options → choice → why)', as well as by 'Approach'."""
import json

from helpers import ForemanTestCase

from sampo import core as c

LONG = "Approach (options → choice → why)"
READINGS = "Reading 1: cache the lookups. Reading 2: cache only the slow ones; ruled out, the fast ones gain nothing."
OPTIONS = ("1) memo dict in the module; fails if: the process restarts and the cache is cold\n"
           "2) use what exists: the lru_cache already in lookup.py; likely outcome: a one-line change\n"
           "3) do less: log the slow lookups first; likely outcome: no speed change, just a measure\n"
           "Choice 2 (85%): the existing decorator, it is tested")


class WholeRefusal(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def m_brief(self, ac="lookups are cached", step=None):
        args = ["task", "new", "Cache the lookups", "--type", "FIX", "--tier", "M", "--ac", ac]
        if step:
            args += ["--step", step]
        return json.loads(self.fm(*args, "--json").stdout)["id"]

    def refusal(self, tid):
        r = self.fm("lane", "brief", tid, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        return " ".join((r.stdout + r.stderr).split())

    def gaps(self, tid):
        return c.plan_gaps(c.find_brief(self.p, tid), c.effective_autonomy(self.p))

    def approach_gaps(self, tid):  # the Approach's own gaps, however the gap words them ("Approach names 0 options", …)
        return [g for g in self.gaps(tid) if "Approach" in g]

    def test_an_empty_m_brief_lists_every_missing_piece_in_one_refusal(self):
        tid = self.m_brief()  # no readings, no options, a criterion with no verify command, no step
        gaps = self.gaps(tid)
        self.assertTrue(any(g.startswith("Interpretation") and "2+ readings" in g for g in gaps), gaps)
        self.assertTrue(any(g.startswith(LONG) and "3+ options" in g for g in gaps), gaps)
        self.assertIn("verify command on criterion 1", gaps)
        self.assertIn("step", gaps)
        out = self.refusal(tid)
        for piece in ("2+ readings", "3+ options", "fails if", "likely outcome", "use what exists", "do less",
                      "Choice N (NN%)", "rival read", "verify command on criterion 1", "step"):
            self.assertIn(piece, out)

    def test_a_zero_option_approach_still_names_use_what_exists_and_do_less(self):
        # T-4536's rounds: a choice line with no options named 0 options, then the rules came one refusal at a time
        tid = self.m_brief(ac="lookups are cached :: true", step="cache them")
        self.fm("task", "set", tid, "--section", "Interpretation", "--text", READINGS)
        self.fm("task", "set", tid, "--section", LONG, "--text", "Choice 1 (90%): the memo")
        gaps = self.approach_gaps(tid)
        self.assertEqual(len(gaps), 3, gaps)  # 0 options; use-what-exists; do-less: all three, not one per refusal
        out = self.refusal(tid)
        for piece in ("3+ options", "use-what-exists", "do-less"):
            self.assertIn(piece, out)

    def test_a_fixed_brief_passes_on_the_next_try(self):
        tid = self.m_brief(ac="lookups are cached :: true", step="cache them")
        self.refusal(tid)
        self.fm("task", "set", tid, "--section", "Interpretation", "--text", READINGS)
        self.fm("task", "set", tid, "--section", LONG, "--text", OPTIONS)
        self.assertEqual([g for g in self.gaps(tid) if "Approach" in g or g.startswith("Interpretation")], [], self.gaps(tid))
        r = self.fm("lane", "brief", tid, "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_approach_under_the_short_heading_is_read_by_either_name(self):
        # a brief whose heading is the short 'Approach' (an older template): both names reach the same section, once
        for name in (LONG, "Approach"):
            tid = self.m_brief(ac="lookups are cached :: true", step="cache them")
            path = c.find_brief(self.p, tid).path
            with open(path, encoding="utf-8") as f:
                text = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace(f"## {LONG}\n", "## Approach\n"))
            self.fm("task", "set", tid, "--section", "Interpretation", "--text", READINGS)
            self.fm("task", "set", tid, "--section", name, "--text", OPTIONS)
            b = c.find_brief(self.p, tid)
            self.assertEqual(b.section(LONG).strip(), OPTIONS.strip(), name)
            self.assertEqual(self.approach_gaps(tid), [], (name, self.gaps(tid)))
            heads = [h.strip() for h, _ in b.sections if h.split(" (")[0].strip() == "Approach"]
            self.assertEqual(len(heads), 1, (name, heads))

    def test_the_long_heading_and_the_short_name_set_the_same_section(self):
        by_long = self.m_brief(ac="lookups are cached :: true", step="cache them")
        by_short = self.m_brief(ac="lookups are cached :: true", step="cache them")
        self.fm("task", "set", by_long, "--section", "Interpretation", "--text", READINGS)
        self.fm("task", "set", by_long, "--section", LONG, "--text", OPTIONS)
        self.fm("task", "set", by_short, "--section", "Interpretation", "--text", READINGS)
        self.fm("task", "set", by_short, "--section", "Approach", "--text", OPTIONS)
        long_b, short_b = c.find_brief(self.p, by_long), c.find_brief(self.p, by_short)
        self.assertEqual(long_b.section(LONG).strip(), OPTIONS.strip())
        self.assertEqual(short_b.section(LONG).strip(), OPTIONS.strip())
        self.assertEqual(self.approach_gaps(by_long), [], self.gaps(by_long))
        self.assertEqual(self.approach_gaps(by_short), [], self.gaps(by_short))
