"""T-5180: moonheld, the sampled blind re-judge of kept moons. Every model call is a fake moonscale._ask."""
# map-links: plugin/lib/sampo/moonheld.py plugin/lib/sampo/moonscale.py plugin/lib/sampo/moons.py
import json
import os
import re
import subprocess
import sys
from unittest import mock

from helpers import ForemanTestCase
from test_moons_scale import good

CELL = {"problem": "slow tests", "source": "pain", "kind": "bugs", "mechanism": "biology", "scale": "file",
        "surface": "terminal"}


class Fake:
    """A fake _ask for the two judges, routed on the system prompt. `answering` names the models that answer (a profile
    or Haiku); `kill_no` and `tier_low` are titles the judge turns down."""

    def __init__(self, answering=("alpha", "beta", "haiku"), kill_no=(), tier_low=()):
        self.answering, self.kill_no, self.tier_low = set(answering), set(kill_no), set(tier_low)
        self.calls = []

    def __call__(self, p, system, prompt, cheap=True, paid=0, feature=None, writer=None, profile=None, **kw):
        model = profile if writer == "local" else writer
        self.calls.append({"stage": "kill" if system.startswith("You check the kill tests") else "tier",
                           "model": model, "writer": writer, "feature": feature, "prompt": prompt})
        if model not in self.answering:
            raise ValueError(f"{model} is down")
        out = []
        for chunk in prompt.split("<moon n=")[1:]:
            n, title = chunk.split(">")[0], re.search(r"Title: (.*)", chunk)
            title = title.group(1) if title else ""
            if self.calls[-1]["stage"] == "kill":
                out.append(f"{n}: " + ("no | needs a team" if title in self.kill_no else "yes | one person, one day"))
            else:
                out.append(f"{n}: " + ("low | wild 1 | tame" if title in self.tier_low else "high | wild 3 | new"))
        return "\n".join(out), model


class _Held(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        from sampo import core as c
        from sampo import moonheld, moonscale
        self.c, self.ms, self.mh = c, moonscale, moonheld
        self.p = c.find_project(self.repo)

    def keep(self, n, keepers=("alpha",), titles=None):
        """n moons stored and marked kept by the kill, tier and novelty judges named in `keepers`; their ids."""
        objs = [good(title=(titles or {}).get(i, f"Moon number {i} for a flaky suite")) for i in range(n)]
        ids = [i for i, _, _ in self.ms.add(self.p, objs, CELL, "haiku", 1)]
        evs = [{"id": i, "at": "2026-11-20T00:00:00Z", "stage": st, "verdict": "kept", "reason": "", "model": m}
               for i in ids for st in ("kill", "tier", "novelty") for m in keepers]
        self.c.append_line(os.path.join(self.ms._dir(self.p), self.ms.FUNNEL), "\n".join(json.dumps(e) for e in evs))
        return ids

    def run_held(self, fake, n=5, local_only=False, locals_=("alpha", "beta"), **kw):
        profs = [{"name": x} for x in locals_]
        with mock.patch.object(self.ms, "_ask", side_effect=fake), \
                mock.patch.object(self.ms, "_locals", side_effect=lambda s: iter(profs)):
            return self.mh.held(self.p, n, local_only, **kw)

    def rows(self):
        return self.mh._rows(self.p)


class Rejudge(_Held):
    def test_each_sampled_moon_is_asked_both_judges_on_a_model_other_than_its_keeper(self):
        ids = self.keep(4, keepers=("alpha",))
        fake = Fake()
        r = self.run_held(fake, n=4)
        self.assertEqual((r["sample"], r["held"], r["rate"]), (4, 4, 1.0))
        self.assertEqual({x["stage"] for x in fake.calls}, {"kill", "tier"}, "the kill and the tier judge both asked")
        self.assertNotIn("alpha", {x["model"] for x in fake.calls}, "never the keeper")
        self.assertEqual({x["feature"] for x in fake.calls}, {"moons-held"})
        rows = self.rows()
        self.assertEqual(sorted(x["id"] for x in rows), sorted(ids))
        self.assertTrue(all(x["kill_model"] == "beta" and x["tier_model"] == "beta" and x["held"] for x in rows), rows)

    def test_a_moon_the_judges_turn_down_does_not_hold_and_the_rate_says_so(self):
        self.keep(4, titles={0: "Weak kill", 1: "Weak tier"})
        r = self.run_held(Fake(kill_no={"Weak kill"}, tier_low={"Weak tier"}), n=4)
        self.assertEqual((r["sample"], r["held"], r["rate"]), (4, 2, 0.5))
        by = {x["id"]: x["held"] for x in self.rows()}
        self.assertEqual(sorted(by.values()), [False, False, True, True])

    def test_a_moon_whose_keeper_is_the_only_model_that_answers_stays_unsampled(self):
        ids = self.keep(2, keepers=("alpha",)) + self.keep(2, keepers=("beta",))
        fake = Fake(answering=("alpha",))
        r = self.run_held(fake, n=4, local_only=True)
        got = {x["id"] for x in self.rows()}
        self.assertEqual(got, set(ids[2:]), "the two beta-kept moons were re-judged by alpha; alpha's own were not")
        self.assertEqual({x["kill_model"] for x in self.rows()} | {x["tier_model"] for x in self.rows()}, {"alpha"})
        self.assertTrue(all("alpha" not in x["keepers"] for x in self.rows()))
        self.assertEqual(r["sample"], 2)
        self.assertEqual(len(self.rows()), 2)

    def test_a_keeper_that_answers_for_another_model_is_thrown_away(self):  # _ask falls back to any local on a failed profile
        self.keep(2, keepers=("alpha",))

        def sneaky(p, system, prompt, **kw):
            return Fake()(p, system, prompt, writer="local", profile="alpha")
        r = self.run_held(sneaky, n=2)
        self.assertEqual((r["sample"], len(self.rows())), (0, 0))

    def test_local_only_never_goes_to_haiku_and_sensitive_neither(self):
        self.keep(2, keepers=("alpha",))
        fake = Fake()
        self.run_held(fake, n=2, local_only=True, locals_=("alpha",))
        self.assertEqual(fake.calls, [], "only the keeper answers locally: nothing is asked, nothing paid")
        r = self.run_held(fake, n=2, locals_=("alpha",))
        self.assertEqual({x["writer"] for x in fake.calls}, {"haiku"}, "without --local-only Haiku is the other model")
        self.assertEqual(r["sample"], 2)

    def test_a_budget_stop_ends_the_run_with_what_it_has(self):
        from sampo import budget
        self.keep(2)

        def broke(*a, **kw):
            raise budget.BudgetError("budget: no")
        r = self.run_held(broke, n=2)
        self.assertEqual((r["sample"], r["rate"]), (0, None))
        self.assertIn("budget", r["error"])
        self.assertIsNone(self.mh.latest(self.p))


class Sampling(_Held):
    def test_no_moon_is_sampled_twice_until_every_kept_one_was(self):
        ids = self.keep(5)
        seen = []
        for _ in range(3):
            self.run_held(Fake(), n=2)
            seen.append([x["id"] for x in self.rows() if x["round"] == 1])
        self.assertEqual([len(s) for s in seen], [2, 4, 5], "2, 2 more, then the last one only")
        self.assertEqual(sorted(seen[-1]), sorted(ids))
        self.assertEqual(len(set(seen[-1])), 5)
        r = self.run_held(Fake(), n=2)  # every one was: a new round
        self.assertEqual(r["round"], 2)
        self.assertEqual({x["round"] for x in self.rows()}, {1, 2})

    def test_the_draw_is_seeded(self):
        self.keep(12)
        a = self.run_held(Fake(), n=3, seed=7)
        first = [x["id"] for x in self.rows()]
        b = self.run_held(Fake(), n=3, seed=7)
        again = [x["id"] for x in self.rows()][3:]
        self.assertTrue(set(first).isdisjoint(again), "the same seed still skips what was sampled")
        self.assertEqual((a["sample"], b["sample"]), (3, 3))

    def test_an_unkept_moon_is_never_sampled_and_no_kept_moon_is_no_run(self):
        self.ms.add(self.p, [good()], CELL, "haiku", 1)  # kept at write only
        fake = Fake()
        r = self.run_held(fake, n=3)
        self.assertEqual((r["sample"], r["rate"], fake.calls), (0, None, []))
        self.assertFalse(os.path.exists(self.mh._path(self.p)))

    def test_a_bad_n_is_refused(self):
        for bad in (-1, "3", None, 2.5):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.mh.held(self.p, bad)


class Ledger(_Held):
    def test_the_event_and_rows_carry_the_rate_and_latest_returns_the_newest_or_none(self):
        self.assertIsNone(self.mh.latest(self.p))
        self.keep(4, titles={0: "Weak kill"})
        self.run_held(Fake(kill_no={"Weak kill"}), n=2, seed=0)
        self.run_held(Fake(kill_no={"Weak kill"}), n=2, seed=0)
        evs = [json.loads(x) for x in open(os.path.join(self.p.dir, "ledger.jsonl")) if '"moons_held"' in x]
        self.assertEqual(len(evs), 2)
        d = evs[-1]["data"]
        self.assertEqual(set(d), {"sample", "held", "rate"})
        self.assertEqual(self.mh.latest(self.p), d["rate"])
        rows = self.rows()
        self.assertEqual(len({x["sample"] for x in rows}), 1)
        self.assertEqual(rows[-1]["rate"], d["rate"])
        with open(self.mh._path(self.p), "a") as f:  # a torn or foreign line changes nothing
            f.write("{not json\n[1]\n")
        self.assertEqual(self.mh.latest(self.p), d["rate"])

    def test_it_runs_while_another_process_holds_the_store_lock(self):
        from sampo import moonscale
        self.keep(3)
        code = ("import fcntl,os,sys\nfd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR)\nfcntl.flock(fd,fcntl.LOCK_EX)\n"
                "print('held',flush=True)\nsys.stdin.read()\n")
        os.makedirs(self.ms._dir(self.p), exist_ok=True)
        proc = subprocess.Popen([sys.executable, "-I", "-c", code, os.path.join(self.ms._dir(self.p), ".funnel.lock")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.stdin.close)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        with self.assertRaises(moonscale.Busy):
            with moonscale.store_lock(self.p):
                pass
        r = self.run_held(Fake(), n=3)
        self.assertEqual(r["sample"], 3)
        self.assertEqual(self.mh.latest(self.p), 1.0)

    def test_the_line_reads_for_a_person(self):
        self.keep(2)
        self.assertIn("2 of 2 kept moons passed", self.mh.held_line(self.run_held(Fake(), n=2)))
        self.assertIn("no kept moon could be re-judged", self.mh.held_line(self.run_held(Fake(answering=()), n=2)))
