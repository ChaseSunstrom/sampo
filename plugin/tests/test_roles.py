"""T-0683 (Intelligence — agent types and roles): a reproducer role, what every agent hands back, a seeded-bug bench for
the reviewer, a per-agent scorecard, and an andon a lane can raise."""
import glob
import json
import os
import re
import subprocess

from helpers import ForemanTestCase, read_text

from sampo import core as c

AGENTS = os.path.join(c.PLUGIN_ROOT, "agents")


class Reproducer(ForemanTestCase):
    def test_the_reproducer_writes_the_failure_first_and_hands_back_its_command(self):
        text = read_text(os.path.join(AGENTS, "fm-reproducer.md"))
        self.assertRegex(text, r"(?m)^name: fm-reproducer$")
        self.assertRegex(text, r"(?m)^tools: .*Write")
        self.assertIn("sampo task prove", text)
        self.assertIn("performance", read_text(os.path.join(AGENTS, "fm-reviewer.md")).lower())


class Noticed(ForemanTestCase):
    def test_every_agent_hands_back_what_it_noticed_and_it_is_captured(self):
        for path in glob.glob(os.path.join(AGENTS, "*.md")):
            self.assertIn("Noticed:", read_text(path), os.path.basename(path))
        self.fm("init")
        report = os.path.join(self.tmp, "report.md")
        with open(report, "w") as f:
            f.write("Verdict: approve\nNoticed: the CSV parser drops a BOM line\nNoticed: none\n"
                    "Noticed: no test covers an empty export\nNoticed: None (T-2186 already covers the reach check)\n"
                    "Noticed: nothing - all of it was inside the brief\nNoticed: N/A.\n"
                    "Noticed: None of the lock's callers check its return\nNoticed: None found.\n"
                    "Noticed: Nothing else to report\nNoticed: Nothing validates the port\nNoticed: None observed\n"
                    "Noticed: None at this time\nNoticed: None, but the retry loop never backs off\n")
        out = self.fm("research", "add", "T-review", "--from-agent", report).stdout
        self.assertIn("5 noticed", out)  # T-2190: "None (…)", "None found." are no item; "None of the …" is one
        titles = [b.title for b in c.load_briefs(c.find_project(self.repo)) if b.meta.get("source") == "discovered"]
        for want in ("BOM", "empty export", "lock's callers", "validates the port", "backs off"):
            self.assertTrue(any(want in t for t in titles), (want, titles))
        self.assertEqual(len(titles), 5, titles)  # and no "None …" line among them

    def test_a_noticed_line_close_to_an_open_task_is_logged_onto_it(self):  # T-1726
        self.fm("init")
        tid = self.fm_json("capture", "fm lease take prints every lease in the project", "--type", "FIX")["id"]
        report = os.path.join(self.tmp, "report.md")
        with open(report, "w") as f:
            f.write("Noticed: fm lease take prints every lease in the project, not just its own\n"
                    "Noticed: the CSV parser drops a BOM line\n")
        out = self.fm("research", "add", "T-review", "--from-agent", report).stdout
        self.assertIn("1 noticed item(s) captured", out)
        self.assertIn(f"1 logged onto open tasks: {tid}", out)
        p = c.find_project(self.repo)
        self.assertEqual(sum("lease take" in b.title for b in c.load_briefs(p)), 1, "no second task")
        self.assertIn("not just its own", c.find_brief(p, tid).section("Log"))


REVIEWER = r'''#!/usr/bin/env python3
import re, sys
diff = sys.stdin.read()
path = re.search(r"^\+\+\+ b/(\S+)", diff, re.M).group(1)
line = int(re.search(r"^@@ -\d+(?:,\d+)? \+(\d+)", diff, re.M).group(1)) + 3
print(f"- HIGH — `{path}:{line}` — returns None — restore the value — high")
'''


class SeedReview(ForemanTestCase):
    def test_seeded_bugs_score_a_reviewer_by_whether_it_names_them(self):
        self.fm("init")
        # T-5584: bench seed-review is a gated verb (a --reviewer command may be a Claude CLI), so this fixture project
        # turns its own switch on; the real project's stays off. Both reviewers below are local stubs: no model runs.
        self.fm("claude-bench", "on")
        for n in range(3):
            with open(os.path.join(self.repo, f"m{n}.py"), "w") as f:
                f.write("".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(4)))
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "code"], check=True)
        stub = os.path.join(self.tmp, "reviewer.py")
        with open(stub, "w") as f:
            f.write(REVIEWER)
        out = self.fm("bench", "seed-review", "--cases", "3", "--reviewer", f"python3 {stub}").stdout
        self.assertIn("caught 3/3", out)
        miss = self.fm("bench", "seed-review", "--cases", "2", "--reviewer", "echo looks fine").stdout
        self.assertIn("caught 0/2", miss)


class Scorecard(ForemanTestCase):
    def test_each_agent_type_has_spawns_tokens_and_lanes_kept(self):
        self.fm("init")
        with open(os.path.join(self.home, "state", "spend.jsonl"), "a") as f:
            for _ in range(2):
                f.write(json.dumps({"day": c.now()[:10], "ts": c.now(), "feature": "subagent:sampo:fm-builder",
                                    "runs": 1, "tokens": 50000}) + "\n")
        p = c.find_project(self.repo)
        c.log_event(p, "lane_merge", task="T-0001", data={})
        c.log_event(p, "lane_rm", task="T-0002", data={"branch_kept": False})
        out = self.fm("usage", "--agents").stdout
        line = next(l for l in out.splitlines() if "fm-builder" in l)
        self.assertIn("2 spawns", line)
        self.assertIn("100k tokens", line)
        self.assertRegex(line, r"lanes merged 1, removed 1")


class Andon(ForemanTestCase):
    def test_a_lane_that_stops_and_asks_shows_in_lane_list_and_next(self):
        self.fm("init")
        lane = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/T-0001", lane],
                       check=True, capture_output=True)
        self.fm("task", "new", "Parallel bit", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        p = c.find_project(self.repo)
        with c.lock(p.dir):
            b = c.find_brief(p, "T-0001")
            b.meta.update(lane=os.path.realpath(lane), status="active")
            c.save_brief(p, b)
        with open(os.path.join(lane, "ANDON.md"), "w") as f:
            f.write("Two specs disagree on the date format; assuming ISO 8601 until told otherwise.\n")
        self.assertIn("ANDON", self.fm("lane", "list").stdout)
        self.assertIn("date format", self.fm("next").stdout)
        self.assertTrue(re.search(r"(?i)andon", read_text(os.path.join(AGENTS, "fm-builder.md"))))


class LensPass(ForemanTestCase):
    # T-5724: before its report the builder writes five lines about its own diff, as a LENSES close line the reviewer reads
    EXTRA = len(" and LENSES".encode()) - 1  # the one word the cap rule's list gained

    def test_lens_pass_is_in_the_contract_and_adds_at_most_450_bytes(self):
        text = read_text(os.path.join(AGENTS, "fm-builder.md"))
        bullet = re.search(r"(?ms)^- Lens pass:.*?(?=^- )", text).group(0)  # the size of the pass itself, not of the file
        grown = len(bullet.encode()) + self.EXTRA
        self.assertLessEqual(grown, 450, grown)
        for part in ("LENSES:", "cost:", "error:", "races:", "test:", "input:", "fails closed", "~100 agents"):
            self.assertIn(part, text, part)
        from sampo import lanes
        self.assertIn("LENSES:", lanes.contract("T-0001", "abc123", "/main"), "the brief's contract carries it")

    def test_lens_pass_line_is_a_close_line_the_report_may_end_with(self):
        from sampo import lanes
        report = ("Done.\n\nCHANGELOG: Does a thing (T-0007).\nTITLE: Does a thing (T-0007)\nLESSON: a rule.\n"
                  "NUMBERS: none: x\nDOCS: none: y\nLENSES: cost: one read per stop; error: a bad line is dropped, fail closed; "
                  "races: none, one writer; test: test_x.test_y; input: a defanged line, no path\n\nNoticed: none\n")
        self.assertEqual(lanes.close_fields(report, tail=True).get("id"), "T-0007")
        self.assertIn("test_x.test_y", lanes.close_fields(report, tail=True).get("lenses", ""))


class Lean(ForemanTestCase):
    # T-5681: a general-purpose launch carries the whole skill listing and tool text every turn (~56k chars); the lean
    # fixer and resolver types take only their own short contract, and delegate.md routes the two jobs to them
    def test_the_fixer_and_the_resolver_are_small_editing_agents_with_their_own_model(self):
        for name, model in (("fm-fixer", "haiku"), ("fm-resolver", "haiku")):  # T-6101: haiku by default, sonnet only on escalation
            text = read_text(os.path.join(AGENTS, name + ".md"))
            self.assertLess(len(text.encode()), 3000, name)
            self.assertRegex(text, rf"(?m)^name: {name}$")
            self.assertRegex(text, r"(?m)^tools: .*Edit")
            self.assertRegex(text, r"(?m)^omitClaudeMd: true$")
            self.assertRegex(text, r"(?m)^maxTurns: [1-9][0-9]*$")
            self.assertRegex(text, rf"(?m)^model: {model}$")
            self.assertIn("Noticed:", text)  # the closing line the Noticed class in this file reads back
            self.assertIn("1500 characters", text)  # T-5598: the report cap replaced the old 400 words

    def test_delegate_routes_review_fixes_and_merge_conflicts_to_them(self):
        delegate = read_text(os.path.join(c.PLUGIN_ROOT, "skills", "intake", "references", "delegate.md"))
        rows = delegate.splitlines()
        fixer = [l for l in rows if "sampo:fm-fixer" in l]
        resolver = [l for l in rows if "sampo:fm-resolver" in l]
        self.assertTrue(any("finding" in l for l in fixer), fixer)
        self.assertTrue(any("conflict" in l for l in resolver), resolver)

    def test_the_editing_set_is_one_constant_and_each_of_its_agents_can_edit(self):
        # T-5681 review: the confine, the wide check, the start and stop marks and the close fields all key on this set
        self.assertEqual(set(c.EDITING_AGENTS), {"fm-builder", "fm-fixer", "fm-resolver"})
        for name in c.EDITING_AGENTS:
            text = read_text(os.path.join(AGENTS, name + ".md"))
            self.assertRegex(text, r"(?m)^tools: .*Edit")
            self.assertRegex(text, r"(?m)^tools: .*Write")

    def test_the_lean_contracts_commit_as_they_go_and_say_what_is_left(self):
        for name in ("fm-fixer", "fm-resolver"):
            text = read_text(os.path.join(AGENTS, name + ".md"))
            self.assertIn("Commit as you go", text, name)
            self.assertIn("what is left", text, name)
