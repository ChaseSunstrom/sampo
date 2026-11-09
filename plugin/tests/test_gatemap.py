"""T-3278: sampo gatemap FILES forecasts which self-change gates a change will hit, from past refusals, with one smallest
fix each. learn() turns refusals into per-gate records of trigger (file, rule or command) and count; predict() names the
gates a file list will likely hit, with confidence h/(h+1) from the history it matched."""
import json
import os
import subprocess

from helpers import ForemanTestCase
from sampo import core as c

first = lambda text: json.JSONDecoder().raw_decode(text)[0]


class Gatemap(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.friction = "plugin/lib/sampo/friction.py"
        self.commit_file(self.friction, "def classes():\n    return []\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit_file(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.git("add", rel)
        self.git("commit", "-qm", f"touch {rel}")
        return self.git("rev-parse", "HEAD")

    def refuse_self_change(self, sha, rows):
        c.log_event(self.p, "self_change_refused", task="T-0001", data={"sha": sha, "rows": rows})

    def seed_history(self):
        """One self-change hold on friction.py (footprint, verbs, timing, guard case), a merge refusal, a guard block,
        and a failed close: each a refusal the forecast should learn from."""
        sha = self.git("rev-parse", "HEAD")
        self.refuse_self_change(sha, [
            "footprint plugin/lib/sampo/friction.py 1 -> 2 (+100%) with no always-on cut to pay for it",
            "verbs 3 -> 4 (+1: friction) with none removed",
            "hook friction 40 -> 90 ms",
            "guard: npm test: base 1, branch 0",
        ])
        c.log_event(self.p, "enforce", task="T-0002", data={"kind": "merge_refused", "reason": "self-change gate"})
        c.log_event(self.p, "first_brief_close_failed", task="T-0003", data={"cmd": "npm test", "tail": "1 failed"})
        events = os.path.join(c.state_dir(), "events.jsonl")
        with open(events, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": c.now(), "kind": "guard_block", "category": "publish", "project": self.p.slug,
                                "cmd": "npm publish"}) + "\n")

    def test_learn_turns_past_refusals_into_per_gate_triggers_and_counts(self):
        from sampo import gatemap
        self.seed_history()
        by_gate = {r["gate"]: r for r in gatemap.learn(self.p)}
        for gate in ("footprint", "verbs", "timing", "guard case", "merge refused", "close failed", "guard block"):
            self.assertIn(gate, by_gate, gate)
            self.assertEqual(by_gate[gate]["count"], 1, gate)
        self.assertIn({"kind": "file", "value": self.friction, "count": 1}, by_gate["footprint"]["triggers"])
        self.assertIn({"kind": "command", "value": "npm test", "count": 1}, by_gate["close failed"]["triggers"])
        self.assertIn({"kind": "command", "value": "npm publish", "count": 1}, by_gate["guard block"]["triggers"])

    def test_predict_names_the_gates_a_change_will_hit_with_confidence_and_one_fix_each(self):
        from sampo import gatemap
        self.seed_history()
        rows = {r["gate"]: r for r in gatemap.predict([self.friction], p=self.p)}
        self.assertEqual(rows["footprint"]["confidence"], 0.5)  # one matching refusal: h/(h+1)
        for gate in ("footprint", "verbs", "timing", "guard case"):
            self.assertTrue(rows[gate]["fix"], gate)
        self.assertEqual(rows["tests"]["confidence"], 1.0)  # a rule, not history
        self.assertIn("test_friction.py", rows["tests"]["fix"])
        self.assertEqual(rows["changelog"]["confidence"], 1.0)
        self.assertNotIn("guard block", rows)  # the history is about npm publish, not this file
        for r in rows.values():
            self.assertTrue(0 <= r["confidence"] <= 1, r["gate"])

    def test_a_new_module_owes_its_shim_map_line_and_master_bullet(self):
        from sampo import gatemap
        rows = {r["gate"]: r for r in gatemap.predict(["plugin/lib/sampo/brandnew.py"], p=self.p)}
        self.assertIn("docs", rows)
        self.assertIn("MASTER.md", rows["docs"]["fix"])
        self.assertNotIn("footprint", rows)  # no history for this file, so no forecast of a hold

    def test_the_verb_prints_gates_and_fixes_in_under_15_lines_and_json_on_request(self):
        self.seed_history()
        out = self.fm("gatemap", self.friction).stdout
        self.assertIn("footprint", out)
        self.assertLess(len(out.strip().splitlines()), 15)
        data = first(self.fm("gatemap", self.friction, "--json").stdout)
        self.assertIn("gates", data)
        self.assertIn("footprint", [g["gate"] for g in data["gates"]])

    def test_a_cached_hook_map_decides_the_timing_bench_and_a_bad_path_is_refused(self):
        from sampo import gatemap
        with open(os.path.join(self.p.dir, "hook-modules.json"), "w", encoding="utf-8") as f:
            json.dump({"sha": "x", "map": {"fixture.json": ["plugin/lib/sampo/friction.py"]}}, f)
        self.seed_history()
        rows = {r["gate"]: r for r in gatemap.predict([self.friction], p=self.p)}
        self.assertEqual(rows["timing"]["confidence"], 1.0)  # a hook imports this module: the bench runs
        other = {r["gate"] for r in gatemap.predict(["plugin/lib/sampo/other.py"], p=self.p)}
        self.assertNotIn("timing", other)  # no hook imports it, so the bench can't slow one
        self.assertNotEqual(self.fm("gatemap", "../outside.py", check=False).returncode, 0)  # no escape from the repo

    def test_no_files_means_the_git_diff_of_the_worktree(self):
        self.seed_history()
        with open(os.path.join(self.repo, self.friction), "w", encoding="utf-8") as f:
            f.write("def classes():\n    return [1]\n")
        out = self.fm("gatemap").stdout
        self.assertIn(self.friction, out)
        self.assertIn("footprint", out)

    def test_learn_reads_the_newest_refused_shas_past_the_cap(self):
        from sampo import gatemap
        shas = [self.commit_file(f"plugin/lib/sampo/mod{i}.py", "x = 1\n") for i in range(gatemap.SHAS + 5)]
        for sha in shas:
            self.refuse_self_change(sha, ["footprint plugin/lib/sampo/x.py 1 -> 2 (+100%) with no always-on cut to pay for it"])
        files = {t["value"] for r in gatemap.learn(self.p) if r["gate"] == "footprint" for t in r["triggers"]}
        self.assertIn(f"plugin/lib/sampo/mod{len(shas) - 1}.py", files)  # the newest refusal's file is learned
        self.assertNotIn("plugin/lib/sampo/mod0.py", files)  # the oldest falls past the cap

    def test_a_module_committed_on_the_branch_still_owes_its_docs(self):
        from sampo import gatemap
        self.git("update-ref", "refs/heads/main", "HEAD")  # main is the base; the change is committed on a lane branch
        self.git("checkout", "-qb", "lane")
        self.commit_file("plugin/lib/sampo/brandnew2.py", "x = 1\n")
        rows = {r["gate"] for r in gatemap.predict(["plugin/lib/sampo/brandnew2.py"], p=self.p)}
        self.assertIn("docs", rows)
        self.assertNotIn("docs", {r["gate"] for r in gatemap.predict([self.friction], p=self.p)})  # on main already

    def test_a_stale_local_main_does_not_make_an_existing_module_new(self):
        # T-4060: ship pushes the work branch to origin/main and the local main stays where it was cloned, before
        # plugin/lib/sampo/ existed; every module there then owed a new module's shim, MASTER bullet and map line
        from sampo import gatemap
        old = self.git("rev-list", "--max-parents=0", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")  # what ship pushed
        self.git("checkout", "-qb", "work")
        self.git("update-ref", "refs/heads/main", old)  # the local main: before friction.py was added
        self.commit_file("plugin/lib/sampo/brandnew3.py", "x = 1\n")
        self.assertNotIn("docs", {r["gate"] for r in gatemap.predict([self.friction], p=self.p)})
        self.assertIn("docs", {r["gate"] for r in gatemap.predict(["plugin/lib/sampo/brandnew3.py"], p=self.p)})

    def test_a_shim_owes_no_test_file_and_a_deleted_file_owes_nothing(self):
        from sampo import gatemap
        self.commit_file("plugin/lib/fmshim.py", "import sys\nfrom sampo import gatemap\nsys.modules[__name__] = gatemap\n")
        self.commit_file("plugin/lib/fmreal.py", "def run():\n    return 1\n")
        self.assertNotIn("tests", {r["gate"] for r in gatemap.predict(["plugin/lib/fmshim.py"], p=self.p)})
        self.assertIn("tests", {r["gate"] for r in gatemap.predict(["plugin/lib/fmreal.py"], p=self.p)})  # not a shim
        os.remove(os.path.join(self.repo, self.friction))
        self.assertNotIn(self.friction, gatemap._worktree_files(self.p))

    def test_two_rows_of_one_refusal_count_once_and_learn_can_skip_the_other_sources(self):
        from sampo import gatemap
        sha = self.git("rev-parse", "HEAD")
        self.refuse_self_change(sha, [
            "footprint plugin/lib/sampo/friction.py 1 -> 2 (+100%) with no always-on cut to pay for it",
            "footprint plugin/lib/sampo/friction.py 2 -> 3 (+50%) with no always-on cut to pay for it",
        ])
        self.seed_history()
        rows = {r["gate"]: r for r in gatemap.predict([self.friction], p=self.p)}
        self.assertEqual(rows["footprint"]["confidence"], 0.67)  # two refusals (one per seeded refusal), not two rows
        self.assertNotIn("guard block", {r["gate"] for r in gatemap.learn(self.p, gates=gatemap.SELF_GATES)})
