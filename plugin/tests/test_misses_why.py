"""T-3931: sampo misses --why: every miss gets a cause class from local evidence, a causal chain of 3+ links ending at a
structural root, roots clustered with counts, one captured detector per root, remote projects merged. Fixtures are the
user's seven asks of 2026-11-08."""
import contextlib
import json

from helpers import ForemanTestCase

from sampo import core as c

ASKS = [  # (text, miss class)
    ("can you make that safe state save a feature of sampo if it isnt already?", "unbuilt"),
    ("maybe also allow better recovery in general if I don't say anything", "unbuilt"),
    ("also has the skills work you ever done actually been used and stuff?", "unused"),
    ("are you also measuring local models performance and whatever too, to keep improving sampo to have them match "
     "your capabilities", "unmeasured"),
    ("also with JARVIS, can you have it fix a few UI issues with lines coming from jarvis not matching up with UI "
     "elements", "unfiled"),
    ("also for Sampo, would it be good to have an orchestration architecture that allows an orchestrator to manage "
     "other orchestrators", "unbuilt"),
    ("also figure out a way to come up with why things were missed", "other"),
]
OLD, ASK_TS = "2026-11-01T10:00:00Z", "2026-11-08T10:00:00Z"


class Why(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo, create=False)
        from sampo import selfaware, which
        self.sa = selfaware
        self.rank = which.rank
        which.rank = lambda text, found=None: {"commands": [{"name": "skills", "score": 3.0}]} \
            if "skills" in text else {"commands": []}
        self.addCleanup(setattr, which, "rank", self.rank)

    def _event(self, event, ts, data):
        with open(f"{self.p.dir}/ledger.jsonl", "a") as f:
            f.write(json.dumps({"ts": ts, "session_id": "s", "project": self.p.slug, "task": None, "event": event,
                                "data": data}) + "\n")

    def _ask(self, i):
        self._event("miss", ASK_TS, {"cls": ASKS[i][1], "text": ASKS[i][0]})

    def _old_brief(self, line):
        self.fm("capture", line)
        b = [x for x in c.load_briefs(self.p) if x.meta.get("created") != OLD][0]
        b.meta["created"] = OLD
        c.save_brief(self.p, b, touch=False)
        return b

    def test_all_seven_asks_are_detected(self):
        for text, cls in ASKS:
            self.assertEqual(self.sa.classify(text), cls, text)

    def test_each_cause_class_gets_evidence_a_chain_and_a_detector(self):
        self._old_brief("FEATURE: safe state save and recovery after a silent reboot")
        self._event("failure", OLD, {"text": "local models performance measuring slow against qwen benchmark"})
        for i in (0, 3, 2, 6):
            self._ask(i)
        got = {r["text"][:20]: r for r in self.sa.why(self.p)["records"]}
        causes = [got[a[:20]]["cause"] for a in (ASKS[0][0], ASKS[3][0], ASKS[2][0], ASKS[6][0])]
        self.assertEqual(causes, ["known", "signal", "built", "blind-spot"])
        for r in got.values():
            self.assertGreaterEqual(len(r["chain"]), 3)
            self.assertTrue(self.sa.valid_chain(r), r)
            self.assertTrue(r["detector"] and r["catcher"] in self.sa.catchers(), r)
        self.assertIn("safe state", got[ASKS[0][0][:20]]["evidence"])
        self.assertIn("measuring", got[ASKS[3][0][:20]]["evidence"])
        self.assertIn("skills", got[ASKS[2][0][:20]]["evidence"])
        self.assertIn("lens", got[ASKS[6][0][:20]]["evidence"])

    def test_a_surface_only_why_is_rejected(self):
        ok = {"cause": "signal", "evidence": "x", "root": "missing feedback loop", "catcher": "verb:next",
              "chain": ["evidence: x", "catcher: verb:next", "root: missing feedback loop"]}
        self.assertTrue(self.sa.valid_chain(ok))
        self.assertFalse(self.sa.valid_chain(dict(ok, chain=ok["chain"][:2])))
        self.assertFalse(self.sa.valid_chain(dict(ok, root="unmeasured", chain=ok["chain"][:2] + ["root: unmeasured"])))
        self.assertFalse(self.sa.valid_chain(dict(ok, catcher="verb:nonesuch")))  # a catcher the code lacks
        self.assertTrue(self.sa.valid_chain(dict(ok, root="a goal nobody scores",
                                                 chain=ok["chain"][:2] + ["root: a goal nobody scores"])))  # grows

    def test_roots_cluster_with_counts_one_detector_each_linked_and_a_second_run_adds_none(self):
        for i in range(7):
            self._ask(i)
        r = self.sa.why(self.p, capture=True)
        self.assertEqual(sum(r["roots"].values()), 7)
        self.assertLessEqual(len(r["roots"]), 4)
        tasks = {v["task"] for v in r["detectors"].values()}
        self.assertEqual(len(tasks), len(r["roots"]))
        for root, d in r["detectors"].items():
            b = c.find_brief(self.p, d["task"])
            self.assertIn(root, b.title + b.section("Raw request"))
            self.assertIn(d["misses"][0][:20], b.section("Raw request") + b.title)
        n = len(c.load_briefs(self.p))
        self.sa.why(self.p, capture=True)
        self.assertEqual(len(c.load_briefs(self.p)), n)

    def _status(self, line, key, status):
        b = self._old_brief(line)
        b.meta["status"] = status
        c.save_brief(self.p, b, touch=False)
        return b

    def test_two_known_misses_get_different_chains_from_their_own_evidence(self):
        a = self._old_brief("FEATURE: orchestration architecture that allows an orchestrator to manage other orchestrators")
        self._status("FIX: jarvis ui lines not matching up with ui elements", "j", "dropped")
        self._ask(5)
        self._ask(4)
        recs = {r["text"][:25]: r for r in self.sa.why(self.p)["records"]}
        open_, dropped = recs[ASKS[5][0][:25]], recs[ASKS[4][0][:25]]
        self.assertEqual((open_["cause"], dropped["cause"]), ("known", "known"))
        self.assertIn(a.id, open_["chain"][1])
        self.assertIn("7 days", open_["chain"][1])  # its age at the ask: OLD to ASK_TS
        self.assertIn("dropped", dropped["chain"][1])
        self.assertNotEqual(open_["chain"][2], dropped["chain"][2])
        self.assertNotEqual(open_["root"], dropped["root"])  # held by an open capture vs closed

    def test_two_misses_sharing_a_mechanism_cluster_on_one_root_with_their_own_chains(self):
        self._event("failure", OLD, {"text": "local models performance measuring slow against qwen benchmark"})
        self._event("failure", OLD, {"text": "jarvis ui lines elements not matching up on the screen"})
        self._ask(3)
        self._ask(4)
        r = self.sa.why(self.p)
        self.assertEqual([x["cause"] for x in r["records"]], ["signal", "signal"])
        self.assertEqual(r["roots"], {r["records"][0]["root"]: 2})
        self.assertNotEqual(r["records"][0]["chain"][0], r["records"][1]["chain"][0])
        self.assertIn("failure", r["records"][0]["chain"][2])  # the event kind and whether a consumer reads it

    def test_a_populated_project_with_only_loose_overlap_is_still_a_blind_spot(self):
        self._old_brief("FEATURE: figure the build graph layout")
        self._event("failure", OLD, {"text": "figure skating results parsed"})
        self._ask(6)  # shares one word ("figure") with each
        rec = self.sa.why(self.p)["records"][0]
        self.assertEqual(rec["cause"], "blind-spot")
        self.assertIn("content words", rec["evidence"])

    def _cause_of(self, text):
        self._event("miss", ASK_TS, {"cls": "other", "text": text})
        return self.sa.why(self.p)["records"][-1]

    def test_filler_words_never_make_a_match_the_real_bad_ones_are_blind_spots(self):
        # real mismatches of the first run: why/how/can/you/make/way/need matched unrelated briefs
        self._old_brief("FEATURE: Genesis gen 1: Missions 2-8, how can we make it so you can do more, you and your way")
        self._old_brief("FEATURE: foreman-ui catch-up: budget, why you need a way to batch the cards")
        self._old_brief("FEATURE: Super recursive brainstorm: why do you need to figure out which one")
        for text in ("how can we make it so what youre doing doesnt eat up the entire CPU",
                     "why dont you implement a way to batch spawn agents",
                     "why do you need to rerun tests to figure out which one failed"):
            self.assertEqual(self._cause_of(text)["cause"], "blind-spot", text)

    def test_a_cpu_ask_finds_the_cpu_brief_and_a_closed_one_gets_the_reopen_catcher(self):
        self._status("FEATURE: CPU gate: limit how much CPU usage a lane uses at once", "c", "done")
        self._status("FEATURE: batch spawn agents in one call", "b", "captured")
        cpu = self._cause_of("why cant you limit how much CPU usage this uses")
        self.assertEqual((cpu["cause"], cpu["catcher"]), ("known", "verb:outcomes"))
        batch = self._cause_of("why dont you implement a way to batch spawn agents")
        self.assertEqual((batch["cause"], batch["catcher"]), ("known", "verb:next"))  # open: the inbox ranking

    def test_the_catcher_follows_the_evidence_not_a_fixed_name(self):
        self._event("failure", OLD, {"text": "local models performance measuring slow against qwen benchmark"})
        sig = self._cause_of(ASKS[3][0])
        self.assertEqual(sig["cause"], "signal")
        self.assertNotEqual(sig["catcher"], "night:scout")
        self.assertIn("failure", sig["chain"][2] + sig["chain"][1])

    def test_a_prompt_event_without_a_miss_event_is_still_an_ask(self):
        self._event("prompt", ASK_TS, {"text": ASKS[0][0]})
        got = self.sa.why(self.p)["records"]
        self.assertEqual([r["text"][:20] for r in got], [ASKS[0][0][:20]])

    def _model(self, reply):
        """Stub the local model: moonscale._local gives a profile, models.complete answers `reply` and counts calls."""
        from sampo import models, moonscale
        calls = []
        self.addCleanup(setattr, moonscale, "_embedder", moonscale._embedder)
        moonscale._embedder = getattr(self, "_emb", lambda p: None)  # none unless a test turns one on (_embedder)
        self.addCleanup(setattr, moonscale, "_local", moonscale._local)
        self.addCleanup(setattr, models, "complete", models.complete)
        moonscale._local = lambda sensitive: {"name": "stub"} if reply is not None else None

        def complete(prof, messages, **kw):
            calls.append(messages[-1]["content"])
            if isinstance(reply, Exception):
                raise reply
            out = reply
            if isinstance(reply, dict) and reply.get("pick") == 1 and getattr(self, "_emb", None):  # row order moves with the recall: point at the row that holds the quote
                for line in messages[-1]["content"].splitlines():
                    if "silent reboot" in line and line[:1].isdigit():
                        out = dict(reply, pick=int(line.split(".")[0]))
                        break
            return {"text": out if isinstance(out, str) else json.dumps(out)}
        models.complete = complete
        return calls

    GOOD = {"pick": 1, "link2": 'the row says "silent reboot" was a known gap', "root": "missing feedback loop",
            "link3": 'nothing read "safe state" against the ask'}

    def _judged(self, reply):
        """The record of ask 0 (one brief holds its words) under a stub model, the cache cleared first."""
        import os
        if not c.load_briefs(self.p):
            self._old_brief("FEATURE: safe state save and recovery after a silent reboot")
            self._ask(0)
        with contextlib.suppress(OSError):
            os.remove(f"{self.p.dir}/misses_why_cache.json")
        self.calls = self._model(reply)
        return self.sa.why(self.p, judge=True)["records"][0]

    def test_a_valid_pick_is_judged_its_chain_quotes_the_row_and_a_rerun_is_cached(self):
        r = self._judged(self.GOOD)
        self.assertEqual((r["path"], r["cause"], r["catcher"]), ("judged", "known", "verb:next"))
        self.assertIn("silent reboot", r["chain"][1])
        self.assertIn("safe state", r["chain"][2])
        self.assertTrue(self.sa.valid_chain(r))
        self.assertEqual(len(self.calls), 1)
        self.sa.why(self.p, judge=True)
        self.assertEqual(len(self.calls), 1)  # answered from the cache

    def test_a_quote_the_row_does_not_hold_is_rejected_and_the_rules_decide(self):
        self.assertEqual(self._judged(dict(self.GOOD, link2='it says "invented phrase here"'))["path"], "rules")

    def test_an_out_of_range_pick_or_a_bad_root_or_junk_is_rejected(self):
        for bad in (dict(self.GOOD, pick=9), dict(self.GOOD, pick=True),
                    dict(self.GOOD, root="x y"), dict(self.GOOD, link3="no quote at all in this one"), "not json"):
            self.assertEqual(self._judged(bad)["path"], "rules", bad)

    def _embedder(self, on=True):
        """Stub the embedder: a 32-dim word-hash vector per text (shared words mean a high cosine); counts its texts."""
        import zlib
        from sampo import models, moonscale
        self.addCleanup(setattr, moonscale, "_embedder", moonscale._embedder)
        self.addCleanup(setattr, models, "embed", models.embed)
        self._emb = moonscale._embedder = lambda p: {"name": "e", "model": "m", "kind": "openai"} if on else None
        seen = []

        def embed(prof, texts, timeout=120):
            seen.extend(texts)
            out = []
            for t in texts:
                v = [0.0] * 32
                for w in t.lower().split():
                    v[zlib.crc32(w.encode()) % 32] += 1.0
                out.append(v)
            return out
        models.embed = embed
        return seen

    def test_the_record_says_whether_recall_was_stem_only_or_fused_with_an_embedder(self):
        self._embedder(on=False)
        r = self._judged(self.GOOD)
        self.assertEqual((r["path"], r["recall"], r["cos"]), ("judged", "stem", None))
        seen = self._embedder()
        r = self._judged(self.GOOD)
        self.assertEqual((r["path"], r["recall"]), ("judged", "stem+embed"))
        self.assertIsInstance(r["cos"], float)
        n = len(seen)
        self.assertGreater(n, 1)
        self.sa.why(self.p, judge=True)
        self.assertEqual(len(seen), n)  # every vector, the ask's too, came from the cache file

    def test_a_judge_that_finds_no_catcher_says_so_as_a_blind_spot_with_a_chain(self):
        self._embedder()
        r = self._judged(dict(self.GOOD, pick=None))
        self.assertEqual((r["path"], r["cause"]), ("judged", "blind-spot"))
        self.assertIn("no catcher existed", r["evidence"])
        self.assertTrue(self.sa.valid_chain(r))

    def test_a_pick_below_the_cosine_gate_becomes_no_catcher_existed(self):
        self._embedder()
        self.addCleanup(setattr, self.sa, "COS_MIN", self.sa.COS_MIN)
        self.sa.COS_MIN = 1.1
        r = self._judged(self.GOOD)
        self.assertEqual((r["path"], r["cause"]), ("judged", "blind-spot"))
        self.assertIn("no catcher existed", r["evidence"])

    def test_no_model_up_or_a_failing_one_means_the_rules(self):
        for reply in (None, OSError("down")):
            self.assertEqual(self._judged(reply)["path"], "rules")

    def test_an_unrelated_ask_is_not_built_with_the_real_ranker(self):
        from sampo import which
        which.rank = self.rank
        self._event("miss", ASK_TS, {"cls": "other", "text": "paint the garden fence green before winter"})
        self.assertNotEqual(self.sa.why(self.p)["records"][0]["cause"], "built")

    def test_a_remote_row_with_a_non_string_field_is_dropped_not_a_crash(self):
        from sampo import bus
        good = {"text": "t", "cls": "other", "ts": ASK_TS, "cause": "blind-spot", "evidence": "e", "catcher": "verb:next",
                "detector": "d", "root": "nobody in the user's seat",
                "chain": ["evidence: e", "catcher: verb:next", "why: w", "root: nobody in the user's seat"]}
        rows = [dict(good, text=["a", "b"]), dict(good, cls={"x": 1}), dict(good, root="a root made up remotely"),
                dict(good, chain=["evidence: e", "x", "y", "root: a root made up remotely"], root="a root made up remotely"),
                good]
        self.addCleanup(setattr, bus, "_ssh", bus._ssh)
        self.addCleanup(setattr, bus, "remotes", bus.remotes)
        bus.remotes = lambda: {"jarvis": {"target": "u@h", "fm": "fm"}}
        bus._ssh = lambda r, command, timeout=20: type(
            "R", (), {"returncode": 0, "stdout": json.dumps({"records": rows}), "stderr": ""})()
        got = self.sa.why(self.p)["records"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["detector"].split(":")[0], "verb")  # recomputed here, not the remote's string

    def test_the_idempotence_key_ends_in_a_delimiter_and_files_under_its_own_trigger(self):
        self.fm("capture", "FIX: why-root: nobody in the user's seat and more words about something else")
        self._ask(6)
        r = self.sa.why(self.p, capture=True)
        root = "nobody in the user's seat"
        b = c.find_brief(self.p, r["detectors"][root]["task"])
        self.assertIn(f"why-root: {root} ::", b.section("Raw request"))
        self.assertEqual(b.meta.get("trigger"), "why-root")

    def test_the_cli_shows_misses_vs_catches_by_cause_over_time(self):
        for i in (3, 6):
            self._ask(i)
        self._event("predicted_catch", ASK_TS, {"text": "x"})
        out = self.fm("misses", "--why", "--days", "365", "--no-capture").stdout
        self.assertIn("2026-11-08", out)
        self.assertIn("root:", out)
        data = json.loads(self.fm("misses", "--why", "--days", "365", "--no-capture", "--json").stdout)
        self.assertEqual(data["trend"]["2026-11-08"]["catch"], 1)
        row = data["trend"]["2026-11-08"]
        self.assertEqual(sum(v for k, v in row.items() if k != "catch"), 2)  # two misses, whatever their causes

    def test_a_remote_project_is_merged_through_conductor_remote(self):
        from sampo import bus
        rec = {"text": "can you fix the lines from jarvis", "cls": "unfiled", "cause": "blind-spot", "ts": ASK_TS,
               "evidence": "none; lens x", "catcher": "verb:next", "detector": "d", "root": "nobody in the user's seat",
               "chain": ["evidence: none", "catcher: verb:next", "why: none", "root: nobody in the user's seat"]}
        calls = []
        self.addCleanup(setattr, bus, "_ssh", bus._ssh)
        self.addCleanup(setattr, bus, "remotes", bus.remotes)
        bus.remotes = lambda: {"jarvis": {"target": "dev@host", "fm": "fm"}}  # made-up login (T-4096)
        bus._ssh = lambda r, command, timeout=20: calls.append(command) or type(
            "R", (), {"returncode": 0, "stdout": json.dumps({"records": [rec]}), "stderr": ""})()
        self._ask(6)
        r = self.sa.why(self.p)
        self.assertEqual(len(calls), 1)
        self.assertIn("--local", calls[0])
        self.assertEqual({x.get("remote") for x in r["records"]}, {None, "jarvis"})
        self.assertEqual(r["roots"]["nobody in the user's seat"], 2)
        bus._ssh = lambda r, command, timeout=20: (_ for _ in ()).throw(OSError("no route"))
        self.assertEqual(self.sa.why(self.p)["remotes_failed"], ["jarvis"])
