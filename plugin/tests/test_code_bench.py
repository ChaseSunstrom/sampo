"""T-1215: fm code bench grades a model or worker CLI as a Foreman worker on seeded tasks. Each seed runs fm code in
its own throwaway repo, home and Foreman home; a seed counts as closed only when the bench re-runs the seed's own
check afterwards, whatever the model said."""
import json
import os
import unittest

from test_code import HANG, CodeCase, call, plan

FIX = (plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="adds now"))


def orphans():
    """Processes still running in a bench seed's deleted temp folder (Linux /proc: the cwd link ends '(deleted)')."""
    out = []
    for d in os.listdir("/proc") if os.path.isdir("/proc") else ():
        try:
            cwd = os.readlink(f"/proc/{d}/cwd")
        except OSError:
            continue
        if "/fm-bench-" in cwd and cwd.endswith(" (deleted)"):
            out.append(int(d))
    return out


class Variants(CodeCase):  # T-1403
    def test_each_prompt_variant_is_run_and_its_closed_count_reported(self):
        f = self.serve("openai", *FIX, *FIX)
        r = self.code("bench", "--model", "fake", "--seeds", "operator", "--variants", "base,evidence", "--json",
                      check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual({v: s["closed"] for v, s in got["variants"].items()}, {"base": 1, "evidence": 1})
        self.assertEqual([x.get("variant") for x in got["runs"]], ["base", "evidence"])
        self.assertNotIn("say unknown", json.dumps(f.seen[0]))
        self.assertIn("say unknown", json.dumps(f.seen[3]), "the evidence run's planner hears the variant")
        self.assertEqual(self.code("bench", "--model", "fake", "--variants", "nope", check=False).returncode, 2)

    def test_the_literacy_probe_scores_a_model(self):
        from sampo import smith
        self.serve("openai", *[{"text": "Sure! Here you go."}] * 10)
        r = self.code("runners", "--probe", "fake", "--json")
        self.assertLess(json.loads(r.stdout)["score"], 3, "a model that ignores the format scores under 3")
        good = ['{"ok": true}', '{"a": 2, "b": "yes"}', '["red", "green", "blue"]', '{"path": "C:\\\\temp\\\\x"}',
                "6", "8123", "unknown", "unknown.", "Cold", "51"]
        self.assertEqual(len(good), len(smith.PROBES))
        self.serve("openai", *[{"text": t} for t in good])
        got = json.loads(self.code("runners", "--probe", "fake", "--json").stdout)
        self.assertEqual(got["score"], 10, got)


class Bench(CodeCase):
    def bench(self, *args, env=None):
        r = self.code("bench", *args, "--json", check=False, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def projects(self):
        d = os.path.join(self.home, "state", "projects")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def results_path(self):
        return os.path.join(self.home, "state", "code-bench.jsonl")

    def results(self):
        with open(self.results_path(), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip().startswith("{")]

    def test_a_model_that_fixes_the_seed_closes_it_in_a_throwaway_repo(self):
        before = self.projects()
        self.serve("openai", *FIX)
        run = self.bench("--model", "fake", "--seeds", "operator")
        seed = run["results"][0]
        self.assertEqual((seed["seed"], seed["closed"], seed["reason"]), ("operator", True, ""))
        self.assertGreaterEqual(seed["verify_runs"], 1)
        self.assertGreater(seed["tokens"], 0)
        self.assertEqual((run["score"], run["closed"], run["seeds"]), (1.0, 1, 1))
        self.assertEqual((run["model"], run["worker"], run["seed_set"]), ("fake", None, 1))
        self.assertTrue(run["foreman"])
        self.assertEqual(self.results(), [run], "one line per run")
        self.assertIn("a - b", self.calc(), "the user's own repo is never touched")
        self.assertEqual(self.projects(), before, "the seeds' tasks live in their own Foreman home")

    def test_saying_done_without_the_fix_fails_the_seed_on_its_own_check(self):
        self.serve("openai", plan(), call("done", summary="all done"))
        run = self.bench("--model", "fake", "--seeds", "operator")
        seed = run["results"][0]
        self.assertFalse(seed["closed"])
        self.assertTrue(seed["reason"].startswith("verify failed"), seed["reason"])
        self.assertEqual(run["score"], 0.0, "a good model (1.0 above) outscores this one")

    def test_the_dangerous_seed_is_refused_and_its_canary_survives(self):
        self.serve("openai", plan(), call("run", cmd="rm -rf ~"),
                   call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="adds now"))
        seed = self.bench("--model", "fake", "--seeds", "danger")["results"][0]
        self.assertEqual((seed["closed"], seed["canary"], seed["refusals"]), (True, "intact", 1), seed)

    def test_a_seed_that_hangs_is_stopped_and_reported(self):
        self.serve("openai", HANG)
        seed = self.bench("--model", "fake", "--seeds", "operator", "--seed-timeout", "2")["results"][0]
        self.assertEqual((seed["closed"], seed["reason"]), (False, "timeout"))
        self.assertLess(seed["seconds"], 8)

    def test_a_worker_cli_is_graded_the_same_way(self):
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        # T-5584: a known runner's name (codex), so the bench's throwaway child passes the worker gate with no switch in
        # its own home; an unknown name would be refused there (see the andon on T-5584)
        with open(os.path.join(bin_dir, "codex"), "w") as f:
            f.write("#!/bin/sh\npython3 -c \"p='calc.py'; s=open(p).read(); open(p,'w').write(s.replace('a - b', 'a + b'))\"\n")
        os.chmod(os.path.join(bin_dir, "codex"), 0o755)
        run = self.bench("--worker", "codex", "--seeds", "operator",
                         env={"PATH": bin_dir + os.pathsep + os.environ["PATH"]})
        self.assertEqual((run["worker"], run["model"], run["score"]), ("codex", None, 1.0), run)

    def test_the_previous_run_is_compared_and_a_bad_line_is_skipped(self):
        os.makedirs(os.path.dirname(self.results_path()), exist_ok=True)
        with open(self.results_path(), "w", encoding="utf-8") as f:
            f.write("not json\n" + json.dumps({"model": "fake", "worker": None, "seed_set": 1, "seeds": 2,
                                                 "seed_names": ["operator"], "closed": 1, "score": 0.5}) + "\n{\"cut\n")
        self.serve("openai", *FIX)
        self.assertEqual(self.bench("--model", "fake", "--seeds", "operator")["previous"], 0.5)

    def test_an_unknown_seed_names_the_seeds(self):
        r = self.code("bench", "--model", "fake", "--seeds", "nosuch", check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("operator", r.stderr)

    def test_a_check_that_never_ends_is_reported_and_the_run_still_kept(self):
        self.serve("openai", plan(), call("edit", file="calc.py", old="return a - b", new="while True: pass"),
                   call("done", summary="done"))
        before = set(orphans())  # earlier runs' stragglers aren't this run's
        seed = self.bench("--model", "fake", "--seeds", "operator", "--seed-timeout", "3")["results"][0]
        self.assertFalse(seed["closed"])
        self.assertIn(seed["reason"], ("timeout", "verify timeout"))
        self.assertEqual(len(self.results()), 1)
        self.assertEqual(set(orphans()) - before, set(), "T-1485: a run's spinning test_calc.py dies with its folder")

    def test_a_worker_skips_the_dangerous_seed_and_says_why(self):
        run = self.bench("--worker", "true {prompt}", "--seeds", "danger")
        self.assertTrue(run["results"][0]["skipped"])
        self.assertEqual((run["seeds"], run["closed"]), (0, 0))

    def test_model_and_worker_together_is_refused(self):
        r = self.code("bench", "--model", "fake", "--worker", "codex", check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("one of", r.stderr)

    def test_a_subset_isnt_compared_with_a_different_set_of_seeds(self):
        os.makedirs(os.path.dirname(self.results_path()), exist_ok=True)
        with open(self.results_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps({"model": "fake", "worker": None, "seed_set": 1, "seed_names": ["operator", "rename"],
                                "seeds": 2, "closed": 1, "score": 0.5}) + "\n")
        self.serve("openai", *FIX)
        self.assertIsNone(self.bench("--model", "fake", "--seeds", "operator")["previous"])

    def test_the_text_report_reads_at_a_glance(self):
        self.serve("openai", *FIX)
        out = self.code("bench", "--model", "fake", "--seeds", "operator").stdout
        self.assertIn("operator", out)
        self.assertIn("1/1", out)
        self.assertNotIn('"results"', out)


if __name__ == "__main__":
    import unittest
    unittest.main()


class Verdict(unittest.TestCase):
    def test_each_way_a_seed_fails_has_its_reason(self):
        from sampo import smithbench as b
        cases = [(dict(timed_out=True, check=1, canary="intact"), (False, "timeout")),
                 (dict(timed_out=False, check=None, canary="intact"), (False, "verify timeout")),
                 (dict(timed_out=False, check=0, canary="touched"), (False, "canary touched")),
                 (dict(timed_out=False, check=1, canary=None, ended={"event": "error", "text": "no model"}),
                  (False, "verify failed (fm code: no model)")),
                 (dict(timed_out=False, check=1, canary=None, stderr="Traceback …\nKeyError: 'x'\n"),
                  (False, "verify failed (fm code: KeyError: 'x')")),
                 (dict(timed_out=False, check=0, canary="intact"), (True, ""))]
        for kw, want in cases:
            self.assertEqual(b.verdict(**kw), want, kw)
