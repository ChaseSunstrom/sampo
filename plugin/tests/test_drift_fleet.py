"""T-3334 (Fleet part 2): every claim carries a drift fingerprint (mandate, root seal, tower gen-0s, steers, standing
intent, plugin version); a machine whose fingerprint differs is refused tower claims, naming the part; fleet lists the
claims with whether they match; the mandate and gen-0 hashes travel in the sync mirror."""
import os
import time
import unittest

from helpers import ForemanTestCase

from sampo import claims, core as c, eco, mandate, sync
from test_claims_remote import ClaimsCase, T0, git


class Drift(ClaimsCase):
    def clone(self, name):
        d = os.path.join(self.tmp.name, name)
        os.makedirs(d)
        git(d, "init")
        return claims.Claims(d, slug="proj-abc123", remote=self.url, session=name,
                             parts={"mandate": "m1", "seal": "s1", "plugin": "1.0"})

    def test_a_different_mandate_is_listed_and_refused_a_tower_claim(self):
        self.assertEqual(self.a.take("T-1", now=T0)["result"], "taken")
        self.b.parts = {"mandate": "m2", "seal": "s1", "plugin": "1.0"}
        r = self.b.take("tower:alpha", now=T0)
        self.assertEqual((r["result"], r["differs"]), ("drift", ["mandate"]))
        self.assertEqual(self.a.take("tower:alpha", now=T0)["result"], "taken")
        self.assertEqual(self.b.take("T-2", now=T0)["result"], "taken")  # task claims are not refused
        rows = {x["key"]: x for x in self.b.fleet(now=T0)}
        self.assertEqual(rows["T-1"]["differs"], ["mandate"])
        self.assertFalse(rows["T-1"]["match"])
        self.assertTrue(rows["T-1"]["until"])

    def other(self, name, **parts):
        d = os.path.join(self.tmp.name, name)
        os.makedirs(d)
        git(d, "init")
        return claims.Claims(d, slug="proj-abc123", remote=self.url, session=name,
                             parts=dict({"mandate": "m1", "seal": "s1"}, **parts))

    def test_one_vote_per_machine_not_per_claim(self):
        stale = self.other("stale", mandate="old")
        for n in range(5):
            stale.take(f"T-{n}", now=T0)
        c1, c2 = self.other("c1"), self.other("c2")
        c1.take("T-9", now=T0)
        me = self.other("me", mandate="m3")
        # others: stale (5 claims) and c1 (1) tie one machine each; by claims stale would win
        self.assertEqual(c2.take("tower:x", now=T0)["result"], "taken")  # c2 is m1, on top of the tie
        self.assertEqual(me.take("tower:y", now=T0)["result"], "drift")
        self.assertEqual(stale.take("tower:z", now=T0)["differs"], ["mandate"])

    def test_a_tie_that_includes_our_fingerprint_is_not_drift(self):
        self.a.take("T-1", now=T0)  # m1
        odd = self.other("odd", mandate="m9")
        odd.take("T-2", now=T0)
        self.assertEqual(self.b.take("tower:alpha", now=T0)["result"], "taken")  # b is m1: 1-1 among others

    def test_plugin_and_intent_are_advisory(self):
        self.a.take("T-1", now=T0)
        up = self.other("up", plugin="2.0", intent="new")
        self.assertEqual(up.take("tower:alpha", now=T0)["result"], "taken")
        row = {x["key"]: x for x in up.fleet(now=T0)}["T-1"]
        self.assertTrue(row["match"])
        self.assertEqual(row["advisory"], ["intent", "plugin"])

    def test_matching_fingerprints_take_towers(self):
        self.a.take("T-1", now=T0)
        self.assertEqual(self.b.take("tower:alpha", now=T0)["result"], "taken")
        self.assertTrue(all(x["match"] for x in self.b.fleet(now=T0)))


class Parts(ForemanTestCase):
    def test_parts_change_with_the_mandate_and_travel_in_the_mirror(self):
        self.fm("init")
        p = c.find_project(self.repo)
        before = mandate.parts(p)
        self.assertEqual(set(before), {"mandate", "seal", "towers", "steers", "intent", "plugin"})
        mandate.put(p, "Sweep it all", time.time() + 3600)
        after = mandate.parts(p)
        self.assertEqual([k for k in before if before[k] != after[k]], ["mandate"])
        sync.export(p)
        d = os.path.join(sync.mirror(p), "fleet")
        self.assertEqual(os.listdir(d), [eco.machine()["id"] + ".md"])  # one file per machine: pulls never conflict
        self.assertIn(after["mandate"], open(os.path.join(d, os.listdir(d)[0])).read())

    def test_the_root_seal_ignores_a_tower_process_and_the_mandate_ignores_the_time_zone(self):
        self.fm("init")
        p = c.find_project(self.repo)
        mandate.put(p, "Sweep it all", time.time() + 3600)
        a = mandate.parts(p)
        meta = c.read_meta(p)
        meta["mandate"]["until"] = "2031-01-01T05:00:00Z"
        c.write_meta(p, meta)
        self.assertEqual(mandate.parts(p)["mandate"], a["mandate"])
        from sampo import rune
        os.makedirs(rune.root(p), exist_ok=True)
        with open(os.path.join(rune.root(p), "gen-0.sha256"), "w") as f:
            f.write("ab12cd34ef56ab12cd34  gen-0.md\n")
        rune._TOWER = "t1"
        self.addCleanup(setattr, rune, "_TOWER", None)
        self.assertEqual(mandate.parts(p)["seal"], "ab12cd34ef56")


if __name__ == "__main__":
    unittest.main()
