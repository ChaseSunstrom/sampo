"""T-5601: a big skill keeps a lean SKILL.md stub (the part a compaction re-attaches whole) and its full steps in
references/procedure.md, read on demand; doctor caps each stub so the body cannot grow back."""
import os
import re
import shutil
import tempfile
import unittest

from helpers import PLUGIN, read_text  # puts plugin/lib on sys.path
from sampo import doctor as d

# the frontmatter as it shipped before the split: a stub keeps it unchanged
FRONTMATTER = {
    "brainstorm": '---\nname: brainstorm\ndescription: Use whenever a request is open-ended ("super improve it", '
                  '"what next?"): scored ideas become tasks.\n---\n',
    "directions": "---\nname: directions\ndescription: Use whenever a new UI needs a look: six style directions, "
                  "the pick saved as DESIGN.md.\n---\n",
}
STEPS = {"brainstorm": 8, "directions": 7}
# words from the full steps that must survive in the procedure (the stub only points at them)
FULL_STEP_WORDS = {
    "brainstorm": ("sampo research add brainstorm-<date>", "Super mode", "sampo task audit ID self"),
    "directions": ("sampo design diverge pick LOOK", "Full autonomy", "grep -cE"),
}


def numbered(text):
    return re.findall(r"(?m)^(\d+)\. ", text)


class LeanStubs(unittest.TestCase):
    def skill(self, name):
        return read_text(os.path.join(PLUGIN, "skills", name, "SKILL.md"))

    def procedure(self, name):
        return read_text(os.path.join(PLUGIN, "skills", name, "references", "procedure.md"))

    def test_each_stub_is_within_its_cap_and_keeps_the_frontmatter(self):
        self.assertEqual(set(d.LEAN_SKILLS), set(FRONTMATTER))
        for name, cap in d.LEAN_SKILLS.items():
            with self.subTest(skill=name):
                text = self.skill(name)
                self.assertLessEqual(len(text), cap)
                self.assertTrue(text.startswith(FRONTMATTER[name]), "the frontmatter is unchanged")

    def test_each_stub_names_the_procedure_and_says_to_read_it_again_after_a_compaction(self):
        for name in FRONTMATTER:
            with self.subTest(skill=name):
                text = self.skill(name)
                self.assertIn("`references/procedure.md`", text)
                self.assertIn("read it again after a compaction", text)

    def test_each_stub_keeps_one_line_per_numbered_step(self):
        for name, n in STEPS.items():
            with self.subTest(skill=name):
                self.assertEqual(numbered(self.skill(name)), [str(i) for i in range(1, n + 1)])

    def test_each_stub_step_names_a_heading_the_procedure_has_at_the_same_number(self):
        # a step's heading is its bold title, else the text before its first ":", "(", "," or ";" or sentence end
        for name in STEPS:
            with self.subTest(skill=name):
                proc_steps = dict(re.findall(r"(?m)^(\d+)\. (.*)$", self.procedure(name).replace("*", "")))
                for num, text in re.findall(r"(?m)^(\d+)\. (.*)$", self.skill(name)):
                    bold = re.match(r"\*\*(.+?)\*\*", text)
                    title = bold.group(1).strip() if bold else re.split(r"[:(,;]|\.\s", text, maxsplit=1)[0].strip()
                    self.assertTrue(title, f"step {num} has no heading")
                    self.assertIn(title, proc_steps.get(num, ""), f"step {num} of the stub")

    def test_the_procedure_exists_and_holds_every_numbered_step_and_its_full_wording(self):
        for name, n in STEPS.items():
            with self.subTest(skill=name):
                proc = self.procedure(name)
                self.assertIn("Paths in this file are relative to the skill folder.", proc)
                self.assertEqual(numbered(proc), [str(i) for i in range(1, n + 1)])
                for word in FULL_STEP_WORDS[name]:
                    self.assertIn(word, proc)
                self.assertNotIn("---\nname:", proc, "the procedure is the body, not a second skill")

    def test_the_procedure_is_longer_than_its_stub(self):
        for name in STEPS:
            with self.subTest(skill=name):
                self.assertGreater(len(self.procedure(name)), 2 * len(self.skill(name)))


class LeanCap(unittest.TestCase):
    """doctor's footprint check: a lean stub over its cap fails; an ordinary skill keeps the wide SKILL_MAX."""

    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)
        self.md = os.path.join(self.t, "none.md")

    def write_skill(self, folder, size):
        path = os.path.join(self.t, "skills", folder, "SKILL.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\nname: {folder}\ndescription: does one thing well\n---\n" + "x" * size)

    def footprint(self):
        return d.check_footprint(self.md, self.md, plugin=self.t)

    def test_a_lean_stub_over_its_cap_fails_and_one_at_the_cap_passes(self):
        cap = d.LEAN_SKILLS["brainstorm"]
        self.write_skill("brainstorm", cap - 200)  # the frontmatter adds ~90 chars, so this sits just under the cap
        self.assertEqual(self.footprint().status, "PASS")
        self.write_skill("brainstorm", cap + 200)
        r = self.footprint()
        self.assertEqual(r.status, "FAIL")
        self.assertIn("skills/brainstorm/SKILL.md", r.detail)
        self.assertIn(f"> {cap}", r.detail)

    def test_the_directions_stub_has_its_own_cap(self):
        self.write_skill("directions", d.LEAN_SKILLS["directions"] + 200)
        r = self.footprint()
        self.assertEqual((r.status, f"> {d.LEAN_SKILLS['directions']}" in r.detail), ("FAIL", True), r.detail)

    def test_an_ordinary_skill_keeps_the_wide_cap(self):
        self.write_skill("intake", 5000)  # over a lean cap, under SKILL_MAX: not a stub, so it passes
        self.assertEqual(self.footprint().status, "PASS")
        self.write_skill("intake", d.SKILL_MAX + 200)
        self.assertEqual(self.footprint().status, "FAIL")


if __name__ == "__main__":
    unittest.main()
