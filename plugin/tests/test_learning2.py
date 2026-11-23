"""T-0752 (learning, paid half): an eval inbox fed by blocks and granted guard blocks, a bench holdout split with a
scorecard, playbook drafts from repeated steps, per-model lesions, smoke fates, retired lessons and reviewer
precision."""
import glob
import json
import os

from helpers import ForemanTestCase, read_text

from sampo import core as c


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def done(self, title, *steps, lesson=None):
        self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                *[a for s in steps or ["s"] for a in ("--step", s)], "--focus")
        tid = c.load_briefs(self.p)[-1].id
        self.fm("task", "finish", tid, "--audit", "self", "--run", "true", *(["--lesson", lesson] if lesson else []))
        return tid


class EvalInbox(Base):
    def test_a_block_becomes_an_eval_case_and_granted_guard_blocks_are_candidates(self):
        self.fm("task", "new", "Flaky deploy", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.fm("task", "block", "T-0001", "the deploy key is missing")
        self.assertTrue(glob.glob(os.path.join(self.p.dir, "evals", "regression-flaky-deploy*")), os.listdir(self.p.dir))
        c.log_event(self.p, "guard_block", task="T-0002", data={"category": "core", "detail": "edit plugin/x.py"})
        c.log_event(self.p, "approval_granted", task="T-0002", data={"allow": ["core"], "via": "prompt"})
        out = self.fm("evals", "inbox").stdout
        self.assertRegex(out, r"T-0001 blocked: the deploy key is missing")
        self.assertNotIn("T-0002", out)  # T-0754: blocked, then the user's yes: the guard worked as designed
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            f.write(json.dumps({"ts": c.now(), "kind": "guard_block", "session_id": "s1", "category": "destructive",
                                "tool": "Bash", "project": self.p.slug, "cmd": "git status --short"}) + "\n")
        from sampo import cost
        from sampo import friction
        from unittest import mock
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:  # no active task then: state, not a rule
            f.write(json.dumps({"ts": c.now(), "kind": "guard_block", "session_id": "s1", "category": "brief",
                                "tool": "Bash", "project": self.p.slug, "cmd": "touch notes.md"}) + "\n")
        with mock.patch.object(friction, "_recheck", lambda events: {i: "today's guard allows it"
                                                                         for i in range(len(events))}):
            rows = cost.eval_inbox(self.p)
        self.assertTrue(any("git status --short" in r["line"] and "allows now" in r["line"] for r in rows), rows)
        self.assertFalse(any("touch notes.md" in r["line"] for r in rows), rows)


class Holdout(Base):
    def test_a_stable_fifth_is_held_out_and_the_scorecard_splits_it(self):
        d = os.path.join(self.p.dir, "bench")
        os.makedirs(os.path.join(d, "results"))
        ids = [f"T-{i:04d}" for i in range(1, 41)]
        with open(os.path.join(d, "cases.json"), "w") as f:
            json.dump([{"id": i, "type": "FIX", "tier": "S"} for i in ids], f)
        hold = json.loads(self.fm("bench", "list", "--split", "holdout", "--json").stdout)["ids"]
        train = json.loads(self.fm("bench", "list", "--split", "train", "--json").stdout)["ids"]
        self.assertEqual(sorted(hold + train), ids)
        self.assertTrue(1 <= len(hold) < len(ids) // 2, hold)  # about one in five, by a stable hash
        self.assertEqual(hold, json.loads(self.fm("bench", "list", "--split", "holdout", "--json").stdout)["ids"])
        with open(os.path.join(d, "results", "r1.json"), "w") as f:
            json.dump({"label": "1.2.26", "cases": [{"id": i, "pass": True} for i in ids]}, f)
        self.assertRegex(self.fm("bench", "scorecard").stdout, rf"1\.2\.26: train {len(train)}/{len(train)} · holdout "
                                                               rf"{len(hold)}/{len(hold)}")
        from sampo import evolve
        with open(evolve.__file__) as f:
            self.assertIn('split="train"', f.read())


class Playbook(Base):
    def test_repeats_drafts_a_playbook_from_steps_three_tasks_share(self):
        for t in ("one", "two", "three"):
            self.done(f"Add the {t} migration", "Write the failing test", "Add the migration", "Run the gate")
        self.fm("repeats", "--draft")
        drafts = glob.glob(os.path.join(self.p.dir, "research", "playbook-draft-*.md"))
        self.assertTrue(drafts)
        text = read_text(drafts[0])
        self.assertIn("Add the migration", text)
        self.assertIn("T-0003", text)

    def test_the_playbook_draft_refuses_an_unreadable_private_term_list(self):
        # T-4817: the draft is meant for the repo, so it is redacted for the user's own terms (strict): a damaged list
        # is one fixed line and no file
        for t in ("one", "two", "three"):
            self.done(f"Add the {t} migration", "Write the failing test", "Add the migration", "Run the gate")
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w") as f:
            f.write("")
        got = self.fm("repeats", "--draft", check=False)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn(c.PRIVATE_UNREADABLE, got.stdout + got.stderr)
        self.assertNotIn("Traceback", got.stderr)
        self.assertEqual(glob.glob(os.path.join(self.p.dir, "research", "playbook-draft-*.md")), [])
        os.remove(os.path.join(c.state_dir(), c.PRIVATE_TERMS))
        self.fm("repeats", "--draft")
        self.assertTrue(glob.glob(os.path.join(self.p.dir, "research", "playbook-draft-*.md")))


class Lesions(Base):
    def test_a_new_model_gets_a_lesion_night_job_until_its_files_are_ablated(self):
        c.log_event(self.p, "task_done", task="T-0009", data={"model": "Opus 9"})
        os.makedirs(os.path.join(self.p.dir, "bench"))
        with open(os.path.join(self.p.dir, "bench", "cases.json"), "w") as f:  # a lesion is gated by the bench
            json.dump([{"id": "T-0001", "type": "FIX", "tier": "S"}], f)
        sessions = os.path.join(c.state_dir(), "sessions")
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "s.json"), "w") as f:
            json.dump({"rate_limits": {"seven_day": {"used_percentage": 10}}}, f)
        self.fm("claude-bench", "on")  # T-5584: a lesion runs a Claude model, so the user's switch comes first
        from sampo import night
        jobs = [(n, a) for n, a, _ in night.jobs(self.p) if n.startswith("model lesion")]
        self.assertTrue(jobs)
        self.assertEqual(jobs[0][1][:2], ["evolve", "--target"])
        self.assertIn("--drop", jobs[0][1])
        c.log_event(self.p, "evolve", data={"target": jobs[0][1][2], "kept": False, "model": "Opus 9"})
        nxt = [(n, a) for n, a, _ in night.jobs(self.p) if n.startswith("model lesion")]
        self.assertNotEqual(nxt[0][1][2], jobs[0][1][2])  # the next file, not the one just ablated


class SmokeFate(Base):
    def test_a_failing_smoke_run_after_a_close_marks_the_task(self):
        tid = self.done("Restyle the header")
        c.log_event(self.p, "smoke", data={"url": "http://localhost:3000", "views": 2, "defects": 3})
        self.assertRegex(self.fm("outcomes").stdout, rf"{tid} smoke failed after")


class Retire(Base):
    def test_a_retired_lesson_leaves_recall_and_tripwires(self):
        tid = self.done("Quote shell paths", lesson="quote every path")
        self.fm("recall", "--lessons", "--retire", f"{tid}.1", "--why", "the guard enforces it now")
        self.assertIn("retired", self.fm("recall", "--lessons").stdout)
        from sampo import recall
        recall.write_tripwires(self.p)
        self.assertNotIn("quote every path", read_text(os.path.join(self.p.dir, "tripwires.json")))


class Precision(Base):
    def test_finding_verdicts_give_each_lens_a_precision(self):
        self.fm("task", "new", "Parser", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.fm("task", "finding", "T-0001", "edge", "confirmed", "empty input crashes")
        self.fm("task", "finding", "T-0001", "adversary", "rejected", "eval injection (it's literal_eval)")
        out = self.fm("usage", "--agents").stdout
        self.assertRegex(out, r"Reviewer precision.*edge: 1 of 1 confirmed")
        self.assertRegex(out, r"adversary: 0 of 1 confirmed")
