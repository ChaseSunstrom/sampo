"""T-2659: the brainstorm skill's grounding step holds a grounding pass to what found built work it missed: three of four
slate items brainstorm-20261102's pass marked new or partial were already built (fm task prove --hunks, survival by
close grade, decision --sunset)."""
import os
import unittest

from helpers import ForemanTestCase

# T-5601: the full steps live in references/procedure.md; SKILL.md is the stub a compaction re-attaches
SKILL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "brainstorm", "references",
                     "procedure.md")


class Grounding(unittest.TestCase):
    def test_step_4_names_master_three_phrasings_help_strings_and_a_spot_check(self):
        with open(SKILL, encoding="utf-8") as f:
            step = f.read().split("4. **Merge, cluster, ground", 1)[1].split("\n5. ", 1)[0]
        for want in ("MASTER.md", "three phrasings", "help strings", "spot-check"):
            self.assertIn(want, step)

    def test_step_3_names_the_grid_lens_for_exhaustive_requests(self):  # T-2672
        with open(SKILL, encoding="utf-8") as f:
            step = f.read().split("3. **Fan out", 1)[1].split("\n4. ", 1)[0]
        self.assertIn("--lens grid", step)



class Canary(unittest.TestCase):
    """T-2674: an empty search proves absence only if the same search finds something known to be there."""

    def test_step_4_runs_a_canary_before_any_not_found(self):
        with open(SKILL, encoding="utf-8") as f:
            step = f.read().split("4. **Merge, cluster, ground", 1)[1].split("\n5. ", 1)[0]
        self.assertIn("canary", step)
        self.assertIn("same tool and scope", step)
        self.assertIn("canary:", step)  # the verdict records it, so a catalogue can count the "new" verdicts that carry one
        self.assertIn("near the idea", step)  # T-2686: Mission 7's catalogue met the rule with 'def main' 156 times

    def test_one_canary_term_across_most_verdicts_is_flagged(self):  # T-2686
        from sampo import ideas
        same = "".join(f"- [new] idea {i}: grep 'x{i}': none; canary: def main found\n" for i in range(9))
        varied = "".join(f"- [new] idea {i}: none; canary: fm{i}.thing found (fm{i}.py:3)\n" for i in range(12))
        self.assertEqual(ideas.canary_spread(same + "- [new] y: canary: fmcost.burden found\n"), ("def main", 9, 10))
        self.assertIsNone(ideas.canary_spread(varied))
        self.assertIsNone(ideas.canary_spread(same))  # under 10 canaries: too few to judge


class CanaryWarning(ForemanTestCase):
    def test_research_add_warns_on_a_generic_canary(self):  # T-2686
        self.fm("init")
        path = os.path.join(self.tmp, "cat.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(f"- [new] idea {i}: none; canary: def main found\n" for i in range(12)))
        out = self.fm("research", "add", "brainstorm-x", "--file", path).stdout
        self.assertIn("12 of 12 canaries are 'def main'", out)



if __name__ == "__main__":
    unittest.main()
