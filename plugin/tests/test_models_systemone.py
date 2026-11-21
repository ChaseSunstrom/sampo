"""T-2887: decide() through a systemone backend (Jev's typed-decision API: Ollama /v1/systemone, Jeff, SelfJev). The fake
server answers with the shape Ollama 0.40.11 gave for kev:4b-q8_0; no test reaches a live server."""
import json
import os

import test_models as tm
from helpers import PRIVATE_TERMS
from sampo import core as c
from sampo import models as m

CHOICES = ["FIX", "FEATURE", "CLEAN"]
# the real reply (Ollama 0.40.11, kev:4b-q8_0), the type question: choice, per-option probabilities, confidence
REAL = {"model": "kev:4b-q8_0", "answers": {"type": {
    "type": "choice", "choice": "FIX", "confidence": 0.8699,
    "probabilities": {"FIX": 0.8916, "FEATURE": 0.0143, "CLEAN": 0.0282, "PERFORMANCE": 0.0059, "SECURITY": 0.0173,
                      "RESEARCH": 0.0426}}}, "usage": {"input_tokens": 151, "output_tokens": 0}}


def answer(choice, probs, conf=0.5):
    return {"answers": {"type": {"type": "choice", "choice": choice, "probabilities": probs, "confidence": conf}}}


class Systemone(tm.Base):
    def backend(self, reply, status=200, local=True):
        f = self.fake((status, reply, None))
        return f, {"name": "kev", "local": local, "profile": {
            "name": "kev", "kind": "systemone", "base_url": f.url, "model": "kev:4b-q8_0", "key_env": None, "context": 8192}}

    def decide(self, reply, status=200):
        f, b = self.backend(reply, status)
        return f, m.decide("intake-type", "fix the login crash", CHOICES + ["PERFORMANCE", "SECURITY", "RESEARCH"],
                           backends=[b])

    def test_the_real_reply_answers_and_the_request_is_one_choice_question(self):
        f, r = self.decide(REAL)
        self.assertEqual((r["choice"], r["probability"], r["backend"]), ("FIX", 0.892, "kev"))
        method, path, _, body = f.seen[0]
        self.assertEqual((method, path), ("POST", "/v1/systemone"))
        self.assertEqual(body["model"], "kev:4b-q8_0")
        q = next(iter(body["questions"].values()))
        self.assertEqual((q["type"], list(q["criteria"])), ("choice", CHOICES + ["PERFORMANCE", "SECURITY", "RESEARCH"]))
        self.assertIn("login", body["state"])

    def test_only_listed_choices_and_probabilities_in_unit_range_are_taken(self):
        for bad in (answer("DELETE", {"DELETE": 0.9, "FIX": 0.1}),            # not a listed choice
                    answer("FIX", {"FIX": 1.7}),                               # outside [0, 1]
                    answer("FIX", {"FIX": -0.2}),
                    answer("FIX", {"FIX": True}),                              # a bool is not a probability
                    answer("FIX", {"FEATURE": 0.4}),                           # the choice has no probability
                    {"answers": {"type": "FIX"}}, {"answers": []}, {"answers": {}}, [], "nope"):
            f, r = self.decide(bad)
            self.assertIsNone(r, bad)

    def test_an_error_reply_or_a_server_error_is_a_miss_not_a_crash(self):
        _, r = self.decide({"error": "model 'kev' not found"}, status=404)
        self.assertIsNone(r)
        _, r = self.decide({"error": "criteria must be an array"})
        self.assertIsNone(r)

    def test_a_systemone_profile_loads_from_models_json_and_is_no_chat_model(self):
        with open(os.path.join(self._tmp.name, "models.json"), "w") as f:
            json.dump({"profiles": {"jeff": {"kind": "systemone", "base_url": "http://127.0.0.1:8765", "model": "jeff"}}}, f)
        prof = m.configured()[1]["jeff"]
        self.assertEqual(prof["kind"], "systemone")
        with self.assertRaises(m.ModelError):
            m.complete(prof, [{"role": "user", "content": "hi"}])

    def test_seed_timeout_is_kept_when_a_positive_number_and_refused_otherwise(self):  # T-5377: _gen_smith reads it
        def load(v):
            with open(os.path.join(self._tmp.name, "models.json"), "w") as f:
                json.dump({"profiles": {"slow": {"kind": "openai", "base_url": "http://127.0.0.1:8765", "model": "s",
                                                 "seed_timeout": v}}}, f)
            return m.configured()[1]["slow"]
        self.assertEqual(load(1800)["seed_timeout"], 1800)
        self.assertEqual(load(90.5)["seed_timeout"], 90.5)
        for bad in (0, -5, True, "600", None):
            with self.assertRaisesRegex(m.ModelError, "seed_timeout must be a positive number of seconds", msg=repr(bad)):
                load(bad)

    def test_the_chat_probe_skips_decision_and_embedding_models(self):
        tags = {"models": [{"name": "kev:4b-q8_0", "capabilities": ["decision"]},
                           {"name": "nomic-embed-text:v1.5", "capabilities": ["embedding"]},
                           {"name": "qwen3.5:9b", "capabilities": ["completion"]}]}
        ids = {"data": [{"id": n["name"]} for n in tags["models"]]}
        f = self.fake((200, ids, None), (200, tags, None), (200, {}, None))  # /models, /api/tags, /api/show
        got = m._probe("ollama", f.url)
        self.assertEqual(got["model"], "qwen3.5:9b")
        f = self.fake((200, ids, None), (200, {"models": [{"name": "kev:4b-q8_0"}]}, None), (200, {}, None))
        self.assertEqual(m._probe("ollama", f.url)["model"], "kev:4b-q8_0")  # no capabilities: the old behaviour

    def test_described_choices_reach_the_criteria_and_the_chat_prompt_and_no_endpoint_is_hardcoded(self):
        f, b = self.backend(REAL)
        want = m.shadow_uses()["intake-type"]
        got = m.decide("intake-type", "fix the login crash", want, backends=[b])
        self.assertEqual(got["choice"], "FIX")
        crit = next(iter(f.seen[0][3]["questions"].values()))["criteria"]
        self.assertEqual(crit, want)
        self.assertTrue(all(v != k for k, v in crit.items()))
        chat = self.fake((200, {"choices": [{"message": {"content": '{"choice": "FIX", "probability": 0.9}'}}]}, None))
        prof = {"name": "c", "kind": "openai", "base_url": chat.url, "model": "c", "key_env": None, "context": 8192}
        m.decide("intake-type", "fix it", want, backends=[{"name": "c", "local": True, "profile": prof}])
        self.assertIn(want["FIX"], json.dumps(chat.seen[0][3]["messages"]))
        with open(m.__file__) as fh:
            self.assertFalse(c.has_private(fh.read(), PRIVATE_TERMS), "models.py names a private term")

    def test_ollama_decision_models_are_found_through_their_capabilities(self):
        tags = {"models": [{"name": "kev:4b-q8_0", "capabilities": ["completion", "decision"]},
                           {"name": "bge-m3:567m", "capabilities": ["embedding"]}, {"name": "old:1b"}]}
        f = self.fake((200, tags, None))
        got = m.systemone_profiles(base_url=f.url[:-3])
        self.assertEqual([(x["name"], x["kind"], x["model"]) for x in got], [("kev:4b-q8_0", "systemone", "kev:4b-q8_0")])
        self.assertEqual(got[0]["base_url"], f.url)  # the /v1 root, where /systemone lives
        self.assertEqual(m.systemone_profiles(base_url="http://127.0.0.1:1"), [])  # nothing listening: none, no crash
