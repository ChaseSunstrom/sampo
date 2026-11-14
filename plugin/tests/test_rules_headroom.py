"""T-4627: the always-on rules text keeps headroom under doctor's character cap, so a lane that adds a rule sentence isn't
left out of its train (T-4612 was, at 6399 of 6400). The Plain words line maps user phrases to verbs; what `sampo which`
already finds for a phrase doesn't need a line of its own in every session, so each removed pair is checked here
through the lookup `sampo which` ranks with (which.rank: skills, playbooks and fm commands, the shelves that need no
project state). The lane brief tells builders the rules have no room.
reruns: the rules file and the lane brief, read as they are; no state kept between runs."""
import json
import os
import re
import unittest

from helpers import ForemanTestCase

from sampo import doctor, which

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(PLUGIN, "rules", "foreman.md")
TARGET = 6100  # chars by doctor's count: 300 under the 6400 cap

# the phrase a user says -> a regex the `load` column of a which row must match (the verb the rules line named).
# which.rank reads the rules' plain words into each verb's text (recall._verb_docs), so a phrase still in the rules
# resolves through itself: this runs on the rules as cut, where the phrases are gone (the three that fed only from
# the rules line are in recall._PLAIN_MOVED)
REMOVED = {
    "status": r"sampo:status",
    "full auto": r"\bfm autonomy\b",
    "clean up": r"sampo:tidy|\bfm tidy\b",
    "is foreman ok?": r"\bfm doctor\b",
    "improve foreman": r"sampo:improve",
    "serve this repo": r"\bfm serve\b",
    "repeated manual work": r"\bfm repeats\b",
    "keep improving yourself": r"\bfm friction\b",
    "improve forever": r"\bfm loop\b",
    "ping me when done": r"\bfm notify\b",
    "what did it cost": r"\bfm cost\b",
}
# measured misses, kept in the rules line: the lookup doesn't return the verb for these phrases (2026-11-14)
MISSES = ("keep tasks with the repo", "work the queue headless", "missing capability")
# what the always-on text keeps: safety routes, and the pointer that makes the removed ones findable
KEPT = MISSES + ("stop everything now", "stop auto-continuing", "/fm-trust on", "don't ask to edit Sampo",
                 "sensitive repo", "the machine restarted", 'sampo which "<X>"')


def loads(phrase):
    return [r["load"] for rows in which.rank(phrase).values() for r in rows]


def rules_chars():
    return doctor.footprint(RULES)["rules chars"]


class RulesHeadroom(unittest.TestCase):
    def test_the_rules_text_is_at_most_6100_chars_by_doctors_count(self):
        self.assertLessEqual(rules_chars(), TARGET)
        self.assertLessEqual(TARGET + 300, doctor.RULES_CHARS_MAX)

    def test_every_removed_pair_is_found_by_the_which_lookup(self):
        for phrase, verb in REMOVED.items():
            with self.subTest(phrase=phrase):
                found = loads(phrase)
                self.assertTrue(any(re.search(verb, x) for x in found), f"{phrase!r}: {verb} not in {found}")

    def test_a_removed_phrase_is_gone_from_the_rules_and_the_safety_routes_stay(self):
        with open(RULES, encoding="utf-8") as f:
            text = f.read()
        for phrase in REMOVED:
            self.assertNotIn(f'"{phrase}"', text, phrase)
        for keep in KEPT:
            self.assertIn(keep, text, keep)

    def test_the_cap_the_brief_names_is_doctors(self):
        from sampo import lanes
        self.assertTrue(any(str(doctor.RULES_CHARS_MAX) in line and "skill" in line and "hook" in line
                            for line in lanes.STANDING), lanes.STANDING)


class BriefNamesTheCap(ForemanTestCase):
    def test_the_lane_brief_says_the_rules_text_has_no_room(self):
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "x", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)
        tid = (tid[0] if isinstance(tid, list) else tid)["id"]
        with open(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"]) as f:
            text = f.read()
        self.assertIn("rules/foreman.md", text)
        self.assertIn(str(doctor.RULES_CHARS_MAX), text)


if __name__ == "__main__":
    unittest.main()
