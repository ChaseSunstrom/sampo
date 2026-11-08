"""T-1181: a stopped fm-reviewer's findings are logged as `finding` events, one per finding, verdict 'reported'."""
import json
import unittest

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes

MSG = """**Verdict** — changes needed.

## lens: adversary
**Findings**
- HIGH — `plugin/lib/fmhooks.py:2950` — trusts the payload text — cap it — high
- **MEDIUM** — plugin/lib/fmcli.py:12 — a second run double counts a retry loop — dedupe — medium

## lens: edge
1. LOW — `plugin/tests/x.py:3` — a mid-way abort leaves partial state — roll back — low

**Acceptance criteria** — met.
**Noticed:** none
"""


class ReviewerFindings(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = json.loads(self.fm("task", "new", "Fix login", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "step", self.tid, "add", "fix")
        self.fm("task", "ac", self.tid, "add", "works", "--verify", "pytest")
        self.fm("focus", self.tid)

    def stop(self, msg=MSG, agent="sampo:fm-reviewer"):
        self.hook("SubagentStop", {"agent_id": "a1", "agent_type": agent, "last_assistant_message": msg})

    def found(self):
        return [e for e in c.ledger_tail(c.find_project(self.repo), 200) if e["event"] == "finding"]

    def test_one_event_per_finding_with_lens_severity_file(self):
        self.stop()
        got = [(e["data"]["lens"], e["data"]["severity"], e["data"]["file"], e["data"]["verdict"], e["task"])
               for e in self.found()]
        self.assertEqual(got, [("adversary", "HIGH", "plugin/lib/fmhooks.py:2950", "reported", self.tid),
                               ("adversary", "MEDIUM", "plugin/lib/fmcli.py:12", "reported", self.tid),
                               ("edge", "LOW", "plugin/tests/x.py:3", "reported", self.tid)])

    def test_non_reviewer_logs_nothing(self):
        self.stop(agent="sampo:fm-recon")
        self.assertEqual(self.found(), [])

    def test_cap_at_30(self):
        self.stop("\n".join(f"- LOW — a.py:{i} — issue {i} — fix — low" for i in range(50)))
        self.assertEqual(len(self.found()), 30)

    def test_rejected_is_not_a_defect_class_but_reported_is(self):
        self.stop("- HIGH — a.py:1 — a retry abort leaves partial state — fix — high\n"
                  "- HIGH — b.py:2 — wrong signal from stale state — fix — high")
        p = c.find_project(self.repo)
        self.assertEqual(len(lanes.defect_classes(p)), 2)
        self.fm("task", "finding", self.tid, "edge", "rejected", "a retry abort leaves partial state")
        out = "\n".join(lanes.defect_classes(p))
        self.assertNotIn("half-done", out)
        self.assertIn("stale", out)


if __name__ == "__main__":
    unittest.main()
