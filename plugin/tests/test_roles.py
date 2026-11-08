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
