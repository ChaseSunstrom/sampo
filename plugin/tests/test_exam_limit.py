"""T-4258: how many scenarios an exam arm answers when no --limit is given. The default of 3 was a spend guard for a
paid Claude session; applied to a local model (free) it made a paired A/B grade 3 of 24 scenarios, and the run said
nothing. A local agent now answers every picked scenario; a session keeps its 3 and says so in one line."""
import unittest

import helpers  # noqa: F401 (puts plugin/lib on sys.path)

from sampo import louhi


class ExamLimit(unittest.TestCase):
    def test_a_local_agent_answers_every_picked_scenario(self):
        for agent in louhi.LOCAL:
            self.assertEqual(louhi.default_limit(agent, None, 24), (None, None), agent)

    def test_a_session_keeps_three_and_says_so(self):
        for agent in louhi.SESSIONS:
            n, line = louhi.default_limit(agent, None, 24)
            self.assertEqual(n, 3, agent)
            self.assertIn("3 of 24", line)
            self.assertIn("--limit", line)

    def test_a_session_with_three_or_fewer_says_nothing(self):
        self.assertEqual(louhi.default_limit(louhi.SESSIONS[0], None, 3), (3, None))

    def test_a_given_limit_rules_and_is_silent(self):
        for agent in louhi.SESSIONS + louhi.LOCAL + ("oracle",):
            self.assertEqual(louhi.default_limit(agent, 5, 24), (5, None), agent)

    def test_any_other_agent_answers_all(self):
        self.assertEqual(louhi.default_limit("oracle", None, 24), (None, None))


if __name__ == "__main__":
    unittest.main()
