"""T-1211: fmmodels, the stdlib model client for fm code. Every request goes to an in-process fake server; no test
reaches the real network."""
import http.server
import json
import os
import tempfile
import threading
import unittest

import helpers  # noqa: F401  (puts plugin/lib on sys.path)
from sampo import models as m

KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "FM_TEST_KEY",
        "TYPESAFE_BASE_URL", "TYPESAFE_API_KEY", "TYPESAFE_MODEL", "LLM_API_KEY",  # T-2494: the user's env never leaks in
        "FOREMAN_NO_LOCAL_MODELS")  # T-3229: run.py hides local models; these tests stand in their own fakes
SECRET = "sk-test-SECRET-123"


class Fake:
    """A one-port HTTP server answering from a script: each request pops (status, body, headers); the last one repeats.
    Records (method, path, headers, json body) of every request."""

    def __init__(self, script):
        self.script, self.seen = list(script), []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                fake.seen.append((self.command, self.path, dict(self.headers), json.loads(raw) if raw else None))
                status, body, hdrs = fake.script.pop(0) if len(fake.script) > 1 else fake.script[0]
                data = (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(status)
                for k, v in (hdrs or {}).items():
                    self.send_header(k, v)
                if "Content-Length" not in (hdrs or {}):  # a script may lie about it (a truncated reply)
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, args=(0.02,), daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def oa_reply(text="hi", tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = {k: os.environ.get(k) for k in KEYS + ("FOREMAN_HOME", "SAMPO_SECRETS")}
        for k in KEYS:
            os.environ.pop(k, None)
        os.environ["FOREMAN_HOME"] = self._tmp.name
        os.environ["SAMPO_SECRETS"] = os.path.join(self._tmp.name, "secrets.env")  # T-3036: never the user's file
        self.slept = []
        self._sleep, m._sleep = m._sleep, self.slept.append
        self._local, m.LOCAL = m.LOCAL, []
        m._tags.cache_clear()  # T-3598: kept a minute per server URL, and a closed fake's port may come back
        m._probe.cache_clear()
        self.fakes = []

    def tearDown(self):
        m._sleep, m.LOCAL = self._sleep, self._local
        for f in self.fakes:
            f.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def fake(self, *script):
        f = Fake(script)
        self.fakes.append(f)
        return f

    def profile(self, fake, kind="openai", key=True):
        if key:
            os.environ["FM_TEST_KEY"] = SECRET
        return {"name": "t", "kind": kind, "base_url": fake.url, "model": "m1",
                "key_env": "FM_TEST_KEY" if key else None, "context": 8192, "tools": True}


TOOL = {"type": "function", "function": {"name": "read", "description": "read a file",
                                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}


class OpenAIShape(Base):
    def test_plain_text(self):
        f = self.fake((200, oa_reply("hello"), None))
        r = m.complete(self.profile(f), [{"role": "user", "content": "hi"}], max_tokens=50)
        self.assertEqual(r["text"], "hello")
        self.assertEqual(r["tool_calls"], [])
        self.assertEqual(r["usage"], {"in": 7, "out": 3})
        self.assertEqual(r["stop"], "end")
        method, path, hdrs, body = f.seen[0]
        self.assertEqual((method, path), ("POST", "/v1/chat/completions"))
        self.assertEqual(hdrs["Authorization"], f"Bearer {SECRET}")
        self.assertEqual(body["model"], "m1")
        self.assertEqual(body["max_tokens"], 50)
        self.assertNotIn("tools", body)

    def test_native_tool_call(self):
        call = {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}}
        f = self.fake((200, oa_reply(None, [call], "tool_calls"), None))
        r = m.complete(self.profile(f), [{"role": "user", "content": "read a.py"}], tools=[TOOL])
        self.assertEqual(r["tool_calls"], [{"id": "c1", "name": "read", "args": {"path": "a.py"}}])
        self.assertEqual(r["text"], "")
        self.assertEqual(r["stop"], "tool_calls")
        self.assertEqual(f.seen[0][3]["tools"], [TOOL])

    def test_stop_vocabulary(self):
        call = {"type": "function", "function": {"name": "read", "arguments": "{}"}}
        f = self.fake((200, oa_reply("x", finish="length"), None), (200, oa_reply(None, [call, call], "stop"), None))
        p = self.profile(f)
        self.assertEqual(m.complete(p, [{"role": "user", "content": "hi"}])["stop"], "length")
        r = m.complete(p, [{"role": "user", "content": "hi"}])
        self.assertEqual(r["stop"], "tool_calls")  # a local server that says "stop" beside its tool calls
        ids = [c["id"] for c in r["tool_calls"]]
        self.assertTrue(all(i and i.startswith("call_") for i in ids))
        self.assertEqual(len(set(ids)), 2)

    def test_key_with_control_characters_is_refused_unechoed(self):
        f = self.fake((200, oa_reply(), None))
        p = self.profile(f)
        os.environ["FM_TEST_KEY"] = "sk-abc\r\nX-Evil: 1"
        with self.assertRaises(m.ModelError) as cm:
            m.complete(p, [{"role": "user", "content": "hi"}])
        self.assertNotIn("sk-abc", str(cm.exception))
        self.assertNotIn("sk-abc", repr(cm.exception.__context__ or ""))
        self.assertEqual(f.seen, [])

    def test_key_http_client_cannot_encode_is_a_scrubbed_model_error(self):
        f = self.fake((200, oa_reply(), None))
        p = self.profile(f)
        os.environ["FM_TEST_KEY"] = "sk-€SECRETPART"
        with self.assertRaises(m.ModelError) as cm:
            m.complete(p, [{"role": "user", "content": "hi"}])
        self.assertNotIn("SECRETPART", str(cm.exception))
        self.assertTrue(cm.exception.__suppress_context__)

    def test_key_whitespace_is_stripped(self):
        f = self.fake((200, oa_reply(), None))
        p = self.profile(f)
        os.environ["FM_TEST_KEY"] = f"  {SECRET}\n"
        m.complete(p, [{"role": "user", "content": "hi"}])
        self.assertEqual(f.seen[0][2]["Authorization"], f"Bearer {SECRET}")

    def test_truncated_reply_is_a_model_error(self):
        f = self.fake((200, '{"choices": [', {"Content-Length": "500"}))
        with self.assertRaises(m.ModelError):
            m.complete(self.profile(f), [{"role": "user", "content": "hi"}])

    def test_deeply_nested_reply_is_a_model_error(self):
        f = self.fake((200, "[" * 200000, None))
        with self.assertRaises(m.ModelError):
            m.complete(self.profile(f), [{"role": "user", "content": "hi"}])

    def test_fields_of_the_wrong_type_read_as_missing(self):
        f = self.fake((200, {"choices": [{"message": {"content": "x"}}], "usage": [1]}, None),
                      (200, {"content": "oops", "usage": 5, "stop_reason": "end_turn"}, None))
        self.assertEqual(m.complete(self.profile(f), [{"role": "user", "content": "hi"}])["usage"], {"in": 0, "out": 0})
        r = m.complete(self.profile(f, "anthropic"), [{"role": "user", "content": "hi"}])
        self.assertEqual((r["text"], r["usage"]), ("", {"in": 0, "out": 0}))

    def test_reply_that_is_not_an_object_is_a_model_error(self):
        f = self.fake((200, [1, 2], None))
        for kind in ("openai", "anthropic"):
            with self.assertRaises(m.ModelError):
                m.complete(self.profile(f, kind), [{"role": "user", "content": "hi"}])

    def test_no_choices_error_is_scrubbed(self):
        f = self.fake((200, {"echo": SECRET}, None))
        with self.assertRaises(m.ModelError) as cm:
            m.complete(self.profile(f), [{"role": "user", "content": "hi"}])
        self.assertNotIn(SECRET, str(cm.exception))

    def test_key_never_goes_over_plain_http_to_a_remote_host(self):
        os.environ["FM_TEST_KEY"] = SECRET
        p = {"name": "x", "kind": "openai", "base_url": "http://example.invalid/v1", "model": "m",
             "key_env": "FM_TEST_KEY"}
        with self.assertRaises(m.ModelError) as cm:
            m.complete(p, [{"role": "user", "content": "hi"}])
        self.assertIn("https", str(cm.exception))
        f = self.fake((200, oa_reply(), None))
        m.complete(dict(p, base_url=f.url.replace("127.0.0.1", "localhost")), [{"role": "user", "content": "hi"}])

    def test_userinfo_in_base_url_is_refused_unechoed(self):
        f = self.fake((200, oa_reply(), None))
        p = dict(self.profile(f, key=False), base_url=f.url.replace("http://", "http://bob:hunter2@"))
        with self.assertRaises(m.ModelError) as cm:
            m.complete(p, [{"role": "user", "content": "hi"}])
        self.assertNotIn("hunter2", str(cm.exception))
        self.assertEqual(f.seen, [])

    def test_complete_s_own_tool_calls_go_back_in_wire_shape(self):
        f = self.fake((200, oa_reply("done"), None))
        msgs = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "read", "args": {"p": 1}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "data"}]
        m.complete(self.profile(f), msgs)
        sent = f.seen[0][3]["messages"][1]["tool_calls"][0]
        self.assertEqual(sent["function"]["name"], "read")
        self.assertEqual(json.loads(sent["function"]["arguments"]), {"p": 1})
        self.assertEqual(sent["type"], "function")

    def test_429_then_success_honours_retry_after(self):
        f = self.fake((429, {"error": "slow down"}, {"Retry-After": "3"}), (200, oa_reply("ok"), None))
        r = m.complete(self.profile(f), [{"role": "user", "content": "hi"}])
        self.assertEqual(r["text"], "ok")
        self.assertEqual(self.slept, [3.0])
        self.assertEqual(len(f.seen), 2)

    def test_retry_after_is_capped(self):
        f = self.fake((429, "", {"Retry-After": "9999"}), (200, oa_reply("ok"), None))
        m.complete(self.profile(f), [{"role": "user", "content": "hi"}])
        self.assertEqual(self.slept, [20.0])

    def test_odd_retry_after_falls_back(self):
        f = self.fake((503, "", {"Retry-After": "nan"}), (503, "", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                      (200, oa_reply("ok"), None))
        m.complete(self.profile(f), [{"role": "user", "content": "hi"}])
        self.assertEqual(self.slept, [1.0, 2.0])

    def test_500_three_times_raises_without_the_key(self):
        f = self.fake((500, f"boom, you sent {SECRET}", None))
        with self.assertRaises(m.ModelError) as cm:
            m.complete(self.profile(f), [{"role": "user", "content": "hi"}])
        self.assertEqual(len(f.seen), 3)
        self.assertEqual(cm.exception.status, 500)
        self.assertIn("500", str(cm.exception))
        self.assertIn("boom", str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertEqual(len(self.slept), 2)

    def test_400_is_not_retried(self):
        f = self.fake((400, "bad request", None))
        with self.assertRaises(m.ModelError):
            m.complete(self.profile(f), [{"role": "user", "content": "hi"}])
        self.assertEqual(len(f.seen), 1)

    def test_an_unset_key_env_is_read_from_the_private_secrets_file(self):
        # T-3036: hooks, the night job and sampo-live aren't login shells, so ~/.bashrc never reaches them
        f = self.fake((200, oa_reply(), None))
        p = self.profile(f)
        os.environ.pop("FM_TEST_KEY")
        path = os.environ["SAMPO_SECRETS"]
        with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as fh:
            fh.write("# keys\nOTHER=x\nexport FM_TEST_KEY='%s'\n" % SECRET)
        m.complete(p, [{"role": "user", "content": "hi"}])
        self.assertEqual(f.seen[0][2]["Authorization"], f"Bearer {SECRET}")
        os.chmod(path, 0o644)  # readable by others: not trusted, as ssh does with a loose key
        with self.assertRaises(m.ModelError) as cm:
            m.complete(p, [{"role": "user", "content": "hi"}])
        self.assertIn("FM_TEST_KEY", str(cm.exception))

    def test_a_launch_time_SAMPO_name_and_the_FOREMAN_name_both_reach_secrets_path(self):
        # T-3173: core moves SAMPO_SECRETS to FOREMAN_SECRETS at import; a fixture path only, never the real file
        import subprocess
        import sys
        lib = os.path.dirname(os.path.dirname(m.__file__))
        code = f"import sys; sys.path.insert(0, {lib!r}); from sampo import models as mm; print(mm.secrets_path())"
        for name in ("SAMPO_SECRETS", "FOREMAN_SECRETS"):
            env = {k: v for k, v in os.environ.items() if not k.endswith("_SECRETS")}
            env[name] = "/x/secrets.env"
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "/x/secrets.env", name + r.stderr[-200:])

    def test_the_secrets_file_is_found_under_the_accounts_home_not_HOME(self):
        # T-3080: an exam sets HOME to a throwaway, and the 27B arm then read "LLM_API_KEY is not set"
        import pwd
        os.environ.pop("SAMPO_SECRETS")
        old = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name
        try:
            self.assertEqual(m.secrets_path(),
                             os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".config", "sampo", "secrets.env"))
        finally:
            os.environ["HOME"] = old if old is not None else ""
        os.environ["SAMPO_SECRETS"] = "/x/secrets.env"
        self.assertEqual(m.secrets_path(), "/x/secrets.env")
        os.environ.pop("SAMPO_SECRETS")
        def no_entry(uid):  # a container's uid with no account entry
            raise KeyError(uid)
        real, pwd.getpwuid = pwd.getpwuid, no_entry
        try:
            self.assertEqual(m.secrets_path(), os.path.expanduser("~/.config/sampo/secrets.env"))
        finally:
            pwd.getpwuid = real

    def test_missing_key_env_raises(self):
        f = self.fake((200, oa_reply(), None))
        p = self.profile(f)
        os.environ.pop("FM_TEST_KEY")
        with self.assertRaises(m.ModelError) as cm:
            m.complete(p, [{"role": "user", "content": "hi"}])
        self.assertIn("FM_TEST_KEY", str(cm.exception))
        self.assertEqual(f.seen, [])

    def test_keyless_local_profile_sends_no_auth(self):
        f = self.fake((200, oa_reply(), None))
        m.complete(self.profile(f, key=False), [{"role": "user", "content": "hi"}])
        self.assertNotIn("Authorization", f.seen[0][2])

    def test_redirect_is_not_followed(self):
        f = self.fake((302, "", {"Location": "http://127.0.0.1:9/steal"}))
        with self.assertRaises(m.ModelError) as cm:
            m.complete(self.profile(f), [{"role": "user", "content": "hi"}])
        self.assertEqual(len(f.seen), 1)
        self.assertIn("127.0.0.1:9/steal", str(cm.exception))

    def test_non_json_reply_raises(self):
        f = self.fake((200, "<html>proxy</html>", None))
        with self.assertRaises(m.ModelError):
            m.complete(self.profile(f), [{"role": "user", "content": "hi"}])

    def test_non_http_base_url_refused(self):
        p = {"name": "x", "kind": "openai", "base_url": "file:///etc", "model": "m", "key_env": None}
        with self.assertRaises(m.ModelError):
            m.complete(p, [{"role": "user", "content": "hi"}])


class AnthropicShape(Base):
    def reply(self, content, stop="end_turn"):
        return {"content": content, "stop_reason": stop, "usage": {"input_tokens": 11, "output_tokens": 4}}

    def test_plain_text(self):
        f = self.fake((200, self.reply([{"type": "text", "text": "hel"}, {"type": "text", "text": "lo"}]), None))
        r = m.complete(self.profile(f, "anthropic"), [{"role": "system", "content": "be brief"},
                                                       {"role": "user", "content": "hi"}])
        self.assertEqual(r, {"text": "hello", "tool_calls": [], "usage": {"in": 11, "out": 4}, "stop": "end"})
        method, path, hdrs, body = f.seen[0]
        self.assertEqual(path, "/v1/messages")
        hdrs = {k.lower(): v for k, v in hdrs.items()}
        self.assertEqual(hdrs["x-api-key"], SECRET)
        self.assertIn("anthropic-version", hdrs)
        self.assertNotIn("authorization", hdrs)
        self.assertEqual(body["system"], "be brief")
        self.assertEqual(body["messages"], [{"role": "user", "content": [{"type": "text", "text": "hi"}]}])

    def test_native_tool_call(self):
        f = self.fake((200, self.reply([{"type": "text", "text": "reading"},
                                        {"type": "tool_use", "id": "tu1", "name": "read", "input": {"path": "a"}}],
                                       "tool_use"), None))
        r = m.complete(self.profile(f, "anthropic"), [{"role": "user", "content": "go"}], tools=[TOOL])
        self.assertEqual(r["tool_calls"], [{"id": "tu1", "name": "read", "args": {"path": "a"}}])
        self.assertEqual(r["text"], "reading")
        self.assertEqual(r["stop"], "tool_calls")
        self.assertEqual(f.seen[0][3]["tools"], [{"name": "read", "description": "read a file",
                                                  "input_schema": TOOL["function"]["parameters"]}])

    def test_message_conversion(self):
        msgs = [{"role": "system", "content": "s1"}, {"role": "system", "content": "s2"},
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "let me look", "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "read", "arguments": '{"path": "x"}'}},
                    {"id": "b", "name": "ls", "args": {}}]},
                {"role": "tool", "tool_call_id": "a", "content": "X"},
                {"role": "tool", "tool_call_id": "b", "content": "Y"},
                {"role": "user", "content": "and?"}]
        system, out = m.to_anthropic(msgs)
        self.assertEqual(system, "s1\n\ns2")
        self.assertEqual(out, [
            {"role": "user", "content": [{"type": "text", "text": "q"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "let me look"},
                                              {"type": "tool_use", "id": "a", "name": "read", "input": {"path": "x"}},
                                              {"type": "tool_use", "id": "b", "name": "ls", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "X"},
                                         {"type": "tool_result", "tool_use_id": "b", "content": "Y"},
                                         {"type": "text", "text": "and?"}]}])

    def test_conversion_leaves_the_callers_messages_alone(self):
        msgs = [{"role": "assistant", "content": [{"type": "text", "text": "a"}],
                 "tool_calls": [{"id": "t", "name": "ls", "args": {}}]},
                {"role": "assistant", "content": [{"type": "text", "text": "b"}]}]
        before = json.dumps(msgs)
        first = m.to_anthropic(msgs)
        self.assertEqual(json.dumps(msgs), before)
        self.assertEqual(m.to_anthropic(msgs), first)
        self.assertEqual(len(first[1][0]["content"]), 3)

    def test_max_tokens_stop_is_length(self):
        f = self.fake((200, self.reply([{"type": "text", "text": "x"}], "max_tokens"), None))
        self.assertEqual(m.complete(self.profile(f, "anthropic"), [{"role": "user", "content": "go"}])["stop"],
                         "length")

    def test_tool_conversion_accepts_bare_specs(self):
        bare = TOOL["function"]
        self.assertEqual(m.to_anthropic_tools([TOOL, bare]),
                         [{"name": "read", "description": "read a file", "input_schema": bare["parameters"]}] * 2)

    def test_500_three_times_raises_without_the_key(self):
        f = self.fake((503, {"error": {"message": f"overloaded {SECRET}"}}, None))
        with self.assertRaises(m.ModelError) as cm:
            m.complete(self.profile(f, "anthropic"), [{"role": "user", "content": "hi"}])
        self.assertEqual(len(f.seen), 3)
        self.assertNotIn(SECRET, str(cm.exception))


class Decide(Base):
    """T-2485 (the user's gen-2 model layer, T-2437): decide() tries the caller's rule, then local backends, then cloud,
    stops at the first valid answer whose probability clears the bar, never calls a cloud backend for a sensitive
    project, and books every answer for scoring."""
    CHOICES = ["FIX", "FEATURE", "CLEAN"]

    def project(self, sensitive=False):
        import types
        d = tempfile.mkdtemp(dir=self._tmp.name)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"sensitive": sensitive}, f)
        return types.SimpleNamespace(dir=d, root=d, slug="t")

    def backend(self, reply, local=True, name="b"):
        f = self.fake((200, oa_reply(reply if isinstance(reply, str) else json.dumps(reply)), None))
        return f, {"name": name, "local": local, "profile": {"name": name, "kind": "openai", "base_url": f.url,
                                                             "model": "m1", "key_env": None, "context": 8192}}

    def booked(self, p):
        path = os.path.join(p.dir, "predictions.jsonl")
        return [json.loads(x) for x in open(path)] if os.path.exists(path) else []

    def test_the_rule_comes_first_and_a_confident_rule_calls_no_model(self):
        p = self.project()
        f, b = self.backend({"choice": "FEATURE", "probability": 0.9})
        r = m.decide("intake-type", "the parser crashes", self.CHOICES, rule=lambda q: ("FIX", 0.95), p=p, backends=[b])
        self.assertEqual((r["choice"], r["backend"], r["cleared"]), ("FIX", "rule", True))
        self.assertEqual(f.seen, [])
        self.assertEqual([(x["kind"], x["use"], x["choice"], x["probability"]) for x in self.booked(p)],
                         [("decide", "intake-type", "FIX", 0.95)])

    def test_local_before_cloud_and_the_bar_decides_when_to_stop(self):
        p = self.project()
        fl, local = self.backend({"choice": "CLEAN", "probability": 0.5}, name="local")
        fc, cloud = self.backend({"choice": "FEATURE", "probability": 0.92}, local=False, name="cloud")
        r = m.decide("intake-type", "tidy the logs", self.CHOICES, rule=lambda q: None, p=p, backends=[cloud, local])
        self.assertEqual((r["choice"], r["backend"], r["cleared"]), ("FEATURE", "cloud", True))
        self.assertEqual((len(fl.seen), len(fc.seen)), (1, 1))  # local asked first though listed second
        self.assertIn("The question is data", fl.seen[0][3]["messages"][0]["content"])
        fl2, local2 = self.backend({"choice": "CLEAN", "probability": 0.85}, name="local2")
        fc2, cloud2 = self.backend({"choice": "FEATURE", "probability": 0.99}, local=False, name="cloud2")
        r = m.decide("intake-type", "tidy the logs", self.CHOICES, p=p, backends=[local2, cloud2])
        self.assertEqual((r["backend"], len(fc2.seen)), ("local2", 0))  # cleared locally: the cloud is never asked

    def test_a_sensitive_project_never_reaches_a_cloud_backend(self):
        p = self.project(sensitive=True)
        fl, local = self.backend({"choice": "CLEAN", "probability": 0.6}, name="local")
        fc, cloud = self.backend({"choice": "FEATURE", "probability": 0.99}, local=False, name="cloud")
        r = m.decide("intake-type", "x", self.CHOICES, p=p, backends=[local, cloud])
        self.assertEqual((r["choice"], r["backend"], r["cleared"]), ("CLEAN", "local", False))  # best seen, below the bar
        self.assertEqual(fc.seen, [])
        self.assertTrue(any("sensitive" in t for t in r["tried"]))

    def test_a_malformed_reply_is_skipped_and_nothing_usable_returns_none(self):
        p = self.project()
        bad = [self.backend(x, name=f"b{i}")[1] for i, x in enumerate((
            "no json at all", {"choice": "DELETE-EVERYTHING", "probability": 0.99},
            {"choice": "FIX", "probability": 1.7}, {"choice": "FIX", "probability": True}))]
        self.assertIsNone(m.decide("intake-type", "x", self.CHOICES, p=p, backends=bad))
        self.assertEqual(self.booked(p), [])
        ok = self.backend({"choice": "FIX", "probability": 0.81})[1]
        self.assertEqual(m.decide("intake-type", "x", self.CHOICES, p=p, backends=bad + [ok])["choice"], "FIX")

    def test_the_registry_lists_local_backends_first(self):
        f = self.fake((200, oa_reply("hi"), None))
        os.environ["FM_TEST_KEY"] = SECRET
        with open(os.path.join(self._tmp.name, "models.json"), "w") as fh:
            json.dump({"profiles": {"far": {"kind": "openai", "base_url": "https://api.example.com/v1",
                                            "model": "x", "key_env": "FM_TEST_KEY"},
                                    "near": {"kind": "openai", "base_url": f.url, "model": "y"}}}, fh)
        reg = m.registry()
        self.assertEqual([(b["name"], b["local"]) for b in reg][:2], [("near", True), ("far", False)])


class Shadow(Base):
    """T-2488: each use records the path's own decision inline (no model call); the night's shadow pass runs decide()
    on those rows with local backends only, and spends nothing without one."""
    def project(self):
        import types
        d = tempfile.mkdtemp(dir=self._tmp.name)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({}, f)
        return types.SimpleNamespace(dir=d, root=d, slug="t")

    def rows(self, p, name):
        path = os.path.join(p.dir, name)
        return [json.loads(x) for x in open(path)] if os.path.exists(path) else []

    def test_a_shadow_row_records_the_paths_choice_redacted(self):
        p = self.project()
        tok = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret
        m.shadow(p, "intake-type", "fix the parser; token " + tok, "FIX")
        r = self.rows(p, m.SHADOW)
        self.assertEqual([(x["use"], x["path"]) for x in r], [("intake-type", "FIX")])
        self.assertNotIn(tok, r[0]["input"])

    def test_the_night_pass_answers_rows_once_on_local_backends_only(self):
        p = self.project()
        for t in ("fix the parser", "add an export", "tidy the logs"):
            m.shadow(p, "intake-type", t, "FIX")
        f = self.fake((200, oa_reply(json.dumps({"choice": "FEATURE", "probability": 0.9})), None))
        local = {"name": "near", "local": True, "profile": {"name": "near", "kind": "openai", "base_url": f.url,
                                                           "model": "m", "key_env": None, "context": 8192}}
        cloud = dict(local, name="far", local=False)
        self.assertEqual(m.shadow_pass(p, backends=[cloud])["answered"], 0)  # no local backend: nothing spent
        self.assertEqual(f.seen, [])
        self.assertEqual(m.shadow_pass(p, cap=2, backends=[cloud, local])["answered"], 2)
        self.assertEqual(m.shadow_pass(p, backends=[local])["answered"], 1)  # each row answered once
        self.assertEqual(m.shadow_pass(p, backends=[local])["answered"], 0)
        refs = [x.get("ref") for x in self.rows(p, "predictions.jsonl") if x["kind"] == "decide"]
        self.assertEqual(sorted(refs), sorted(x["id"] for x in self.rows(p, m.SHADOW)))


class ModelRules(unittest.TestCase):
    """T-2486: the user's four model rules (T-2437), each a check that fails when it breaks."""
    LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")

    def source(self, name):
        with open(os.path.join(self.LIB, name), encoding="utf-8") as f:
            return f.read()

    def test_models_advise_and_proof_decides_no_gate_reaches_the_model_layer(self):
        import ast
        for mod in ("fmguard.py", "sampo/core.py"):  # the guard and the evidence core never import it at all
            tree = ast.parse(self.source(mod))
            names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            names |= {f"{n.module}.{a.name}" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
                      for a in n.names}
            self.assertEqual({"fmmodels", "sampo.models"} & names, set(), mod)
        tree = ast.parse(self.source("sampo/cli.py"))
        gates = {"cmd_check", "cmd_gates", "task_finish", "task_done_in"}
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in gates):
            seg = ast.get_source_segment(self.source("sampo/cli.py"), fn)
            self.assertNotRegex(seg, r"fmmodels|sampo\.models|import[^\n]*\bmodels\b|\bdecide\(", fn.name)  # ponytail: direct calls only, not an import walk

    def test_the_model_layer_downloads_and_installs_nothing_itself(self):
        # installs and model downloads go through the install queue (fm ask --later) for the user's yes
        self.assertNotRegex(self.source("sampo/models.py"),
                            r"(?i)urlretrieve|/api/pull|\bollama pull\b|\bpip install\b|snapshot_download|hf_hub")

    def test_a_use_goes_live_only_by_a_decision_row_citing_its_exam(self):
        import types
        d = tempfile.mkdtemp()
        p = types.SimpleNamespace(dir=d, root=d, slug="t")
        rows = lambda *xs: open(os.path.join(d, "decisions.md"), "w").write(  # noqa: E731
            "| Date | Decision | Why | Revisit |\n|---|---|---|---|\n" + "".join(f"| 2026-11-01 | {x} | why |  |\n" for x in xs))
        self.assertFalse(m.live(p, "intake-type"))  # no row: shadow
        rows("model-live: intake-type, no numbers given")
        self.assertFalse(m.live(p, "intake-type"))  # a switch must cite its exam run
        rows("model-live: intake-type: exam s30-001 beat the rule path, 81% against 74% on held-out")
        self.assertTrue(m.live(p, "intake-type"))
        self.assertFalse(m.live(p, "lead-triage"))  # per use
        rows("model-live: intake-type: exam s30-001, 81% against 74%", "model-shadow: intake-type: calibration drifted")
        self.assertFalse(m.live(p, "intake-type"))  # the newest row for the use wins


class NoteCallWhere(unittest.TestCase):  # T-3017
    def test_loopback_is_local_and_anything_else_remote(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "calls.jsonl")
            with mock.patch.dict(os.environ, {m.CALLS_ENV: path}):
                for url in ("http://127.0.0.1:11434/v1", "http://localhost:1/v1", "http://[::1]:1/v1", "https://j.example/v1"):
                    m.note_call({"name": "n", "model": "x", "base_url": url})
            rows = [json.loads(line) for line in open(path)]
        self.assertEqual([r["where"] for r in rows], ["local", "local", "local", "remote"])


    def test_a_child_sampo_logs_to_the_parents_call_log(self):  # T-3448
        # the child's core.mirror_env moved the inherited SAMPO_CALLS to FOREMAN_CALLS, then its cli.main set
        # SAMPO_CALLS to its own temp home's log, which note_call read: an exam's Smith logged nowhere the page looks
        import subprocess
        import sys
        lib = os.path.dirname(os.path.dirname(os.path.abspath(m.__file__)))
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); from sampo import cli, models\n"
                "sys.argv = ['sampo', '--help']\ntry:\n    cli.main()\nexcept SystemExit:\n    pass\n"
                "print(os.environ.get(models.CALLS_ENV))")
        with tempfile.TemporaryDirectory() as d:
            env = {k: v for k, v in os.environ.items() if not k.endswith("_CALLS")}
            env.update(SAMPO_CALLS=os.path.join(d, "parent.jsonl"), FOREMAN_HOME=os.path.join(d, "home"))
            out = subprocess.run([sys.executable, "-c", code, lib], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.stdout.strip().splitlines()[-1], os.path.join(d, "parent.jsonl"), out.stderr[-500:])


class ExtractJson(unittest.TestCase):
    def test_fenced(self):
        self.assertEqual(m.extract_json('Sure:\n```json\n{"a": 1}\n```\nthanks'), {"a": 1})

    def test_bare(self):
        self.assertEqual(m.extract_json('the answer is {"a": [1, 2]} ok'), {"a": [1, 2]})

    def test_nested_and_braces_in_strings(self):
        self.assertEqual(m.extract_json('x {"a": {"b": "}{"}, "c": 2} y {"d": 3}'), {"a": {"b": "}{"}, "c": 2})

    def test_broken(self):
        self.assertIsNone(m.extract_json('{"a": 1, '))
        self.assertIsNone(m.extract_json("no json here"))
        self.assertIsNone(m.extract_json(""))
        self.assertIsNone(m.extract_json(None))

    def test_skips_a_broken_one_for_a_later_good_one(self):
        self.assertEqual(m.extract_json('{oops} then {"ok": true}'), {"ok": True})

    def test_hostile_input_is_bounded(self):
        self.assertIsNone(m.extract_json('{"a":' * 50000))
        self.assertIsNone(m.extract_json("[" * 50000))

    def test_an_invalid_escape_keeps_the_object(self):  # T-3530: qwen3.8-27b wrote \' in a done summary 5 turns running
        text = r'{"action": "done", "summary": "find_user_sql: name.replace(\'x\', \'y\') allocates twice"}'
        self.assertEqual(m.extract_json(text), {"action": "done",
                                                "summary": r"find_user_sql: name.replace(\'x\', \'y\') allocates twice"})
        self.assertEqual(m.extract_json(r'run {"action": "run", "cmd": "grep -c \d app.py"}'),
                         {"action": "run", "cmd": r"grep -c \d app.py"})
        self.assertEqual(m.extract_json(r'{"a": "x\\y \"q\""}'), {"a": 'x\\y "q"'})  # valid escapes unchanged
        self.assertEqual(m.extract_json(r'{"path": "C:\users\u00e9"}'), {"path": "C:\\users\u00e9"})

    def test_an_inner_object_never_stands_in_for_the_action(self):  # T-2878 review: state rides every text action
        after = r'{"action": "edit", "file": "a.py", "old": "x", "new": "it\'s", "state": {"goal": "g", "facts": []}}'
        before = r'{"action": "edit", "state": {"goal": "g"}, "file": "a.py", "old": "x", "new": "it\'s"}'
        braces = r'{"action": "edit", "file": "a.rs", "old": "x", "new": "fn f() {}  // it\'s"}'
        for text in (after, before, braces):
            with self.subTest(text=text):
                v = m.extract_json(text)
                self.assertEqual((v or {}).get("action"), "edit")
                self.assertEqual(v["new"][-5:], r"it\'s")


class LocalContext(Base):  # T-1360
    def test_each_local_server_says_the_context_it_serves(self):
        listed = (200, {"data": [{"id": "qwen"}]}, None)
        cases = [
            ("ollama", [listed, (200, {"parameters": "temperature 0.2\nnum_ctx 16384", "model_info": {
                "qwen2.context_length": 32768}}, None)], 16384, ("POST", "/api/show")),
            ("ollama", [listed, (200, {"model_info": {"qwen2.context_length": 32768}}, None)], 4096, ("POST", "/api/show")),
            ("llamacpp", [listed, (200, {"default_generation_settings": {"n_ctx": 12288}}, None)], 12288, ("GET", "/props")),
            ("vllm", [(200, {"data": [{"id": "q", "max_model_len": 65536}]}, None)], 65536, None),
            ("lmstudio", [listed, (200, {"loaded_context_length": 20000, "max_context_length": 131072}, None)], 20000,
             ("GET", "/api/v0/models/qwen")),
            ("llamacpp", [listed, (500, {"error": "no"}, None)], m.LOCAL_CONTEXT, ("GET", "/props")),
            ("ollama", [listed, (200, {"parameters": "num_ctx 9999999999"}, None)], m.LOCAL_CONTEXT, ("POST", "/api/show")),
        ]
        for name, script, want, call in cases:
            f = self.fake(*script)
            m.LOCAL = [(name, f.url)]
            self.assertEqual(m.resolve()["context"], want, (name, script[-1]))
            if call:
                self.assertIn(call, [x[:2] for x in f.seen[1:]], name)  # T-2887: Ollama's /api/tags comes between
        self.assertEqual(f.seen[-1][3], {"model": "qwen"}, "Ollama is asked about the model it listed")


class Profiles(Base):
    def write_models(self, data):
        with open(os.path.join(self._tmp.name, "models.json"), "w") as f:
            json.dump(data, f)

    def test_named_profile_from_models_json(self):
        self.write_models({"profiles": {"mine": {"kind": "anthropic", "base_url": "http://h/v1", "model": "c",
                                                 "key_env": "FM_TEST_KEY"}}})
        p = m.resolve("mine")
        self.assertEqual(p["name"], "mine")
        self.assertEqual(p["kind"], "anthropic")
        self.assertEqual(p["model"], "c")
        self.assertEqual(set(p), {"name", "kind", "base_url", "model", "key_env", "context", "tools"})

    def test_models_json_default_beats_local_and_keys(self):
        local = self.fake((200, {"data": [{"id": "llama"}]}, None))
        m.LOCAL = [("ollama", local.url)]
        os.environ["OPENAI_API_KEY"] = "k"
        self.write_models({"default": "mine", "profiles": {"mine": {"base_url": "http://h/v1", "model": "x"}}})
        self.assertEqual(m.resolve()["name"], "mine")

    def test_local_beats_keyed_cloud(self):
        local = self.fake((200, {"data": [{"id": "qwen"}, {"id": "other"}]}, None))
        m.LOCAL = [("dead", "http://127.0.0.1:9/v1"), ("lmstudio", local.url)]
        os.environ["OPENAI_API_KEY"] = "k"
        p = m.resolve()
        self.assertEqual((p["name"], p["model"], p["base_url"], p["key_env"]), ("lmstudio", "qwen", local.url, None))
        self.assertEqual(local.seen[0][:2], ("GET", "/v1/models"))

    def test_first_keyed_cloud_without_local(self):
        os.environ["GROQ_API_KEY"] = "k"
        os.environ["GEMINI_API_KEY"] = "k"
        p = m.resolve()
        self.assertEqual((p["name"], p["key_env"]), ("groq", "GROQ_API_KEY"))
        os.environ["ANTHROPIC_API_KEY"] = "k"
        p = m.resolve()
        self.assertEqual((p["name"], p["kind"]), ("anthropic", "anthropic"))

    def test_gemini_goes_through_its_openai_endpoint(self):
        os.environ["GEMINI_API_KEY"] = "k"
        p = m.resolve("gemini")
        self.assertEqual(p["kind"], "openai")
        self.assertIn("generativelanguage.googleapis.com", p["base_url"])
        self.assertTrue(p["base_url"].endswith("/openai"))

    def test_nothing_available_raises(self):
        with self.assertRaises(m.ModelError):
            m.resolve()

    def test_unknown_name_raises(self):
        with self.assertRaises(m.ModelError):
            m.resolve("nope")

    def test_broken_models_json_raises_naming_it(self):
        with open(os.path.join(self._tmp.name, "models.json"), "w") as f:
            f.write("{nope")
        with self.assertRaises(m.ModelError) as cm:
            m.resolve()
        self.assertIn("models.json", str(cm.exception))

    def test_resolve_stops_probing_at_the_first_local_answer(self):
        a = self.fake((200, {"data": [{"id": "one"}]}, None))
        b = self.fake((200, {"data": [{"id": "two"}]}, None))
        m.LOCAL = [("ollama", a.url), ("vllm", b.url)]
        self.assertEqual(m.resolve()["model"], "one")
        self.assertEqual(b.seen, [])

    def test_broken_local_replies_are_skipped(self):
        cut = self.fake((200, '{"data": [', {"Content-Length": "500"}))
        deep = self.fake((200, "[" * 200000, None))
        listy = self.fake((200, [1], None))
        m.LOCAL = [("a", cut.url), ("b", deep.url), ("c", listy.url)]
        os.environ["OPENAI_API_KEY"] = "k"
        self.assertEqual(m.resolve()["name"], "openai")

    def test_bad_profile_fields_name_models_json(self):
        for bad in ({"kind": "cohere"}, {"tools": "yes"}, {"key_env": 5}, {"context": "big"},
                    {"price": 3}, {"price": {"in": -1}}, {"price": {"in": True}}, {"price": {"per": 1}}):  # T-1357
            self.write_models({"profiles": {"p": dict({"base_url": "http://h/v1", "model": "x"}, **bad)}})
            with self.assertRaises(m.ModelError, msg=bad) as cm:
                m.resolve("p")
            self.assertIn("models.json", str(cm.exception))

    def test_anthropic_default_model(self):
        os.environ["ANTHROPIC_API_KEY"] = "k"
        self.assertEqual(m.resolve("anthropic")["model"], "claude-sonnet-5-5")

    def test_local_server_with_no_models_is_skipped(self):
        empty = self.fake((200, {"data": []}, None))
        odd = self.fake((200, {"data": 5}, None))
        m.LOCAL = [("ollama", empty.url), ("vllm", odd.url)]
        os.environ["OPENAI_API_KEY"] = "k"
        self.assertEqual(m.resolve()["name"], "openai")

    def test_profiles_lists_configured_keyed_and_local(self):
        local = self.fake((200, {"data": [{"id": "llama"}]}, None))
        m.LOCAL = [("ollama", local.url)]
        os.environ["OPENAI_API_KEY"] = "k"
        self.write_models({"profiles": {"mine": {"base_url": "http://h/v1", "model": "x"}}})
        self.assertEqual([p["name"] for p in m.profiles()], ["mine", "ollama", "openai"])


if __name__ == "__main__":
    unittest.main()


class OwnServer(Base):
    """T-2494: a server the user owns (models.json "own": true) is asked after loopback and before clouds, and the
    night shadow pass may use it for a plain project, never a sensitive one; TYPESAFE_BASE_URL adds a profile."""
    def project(self, sensitive=False):
        import types
        d = tempfile.mkdtemp(dir=self._tmp.name)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"sensitive": sensitive}, f)
        return types.SimpleNamespace(dir=d, root=d, slug="t")

    def models(self, **profiles):
        with open(os.path.join(self._tmp.name, "models.json"), "w") as f:
            json.dump({"profiles": profiles}, f)

    def test_own_is_validated_and_ordered_between_loopback_and_cloud(self):
        self.models(home={"base_url": "https://home.example/llm/v1", "model": "q", "key_env": "LLM_API_KEY", "own": True})
        os.environ["OPENAI_API_KEY"] = "k"  # pragma: allowlist secret
        reg = m.registry()
        self.assertEqual([(b["name"], b["local"], b["own"]) for b in reg][:2], [("home", False, True), ("openai", False, False)])
        self.models(home={"base_url": "https://home.example/llm/v1", "model": "q", "own": "yes"})
        with self.assertRaises(m.ModelError):
            m.registry()

    def test_the_night_pass_uses_an_own_server_for_a_plain_project_never_a_sensitive_one(self):
        f = self.fake((200, oa_reply(json.dumps({"choice": "FIX", "probability": 0.9})), None))
        own = {"name": "home", "local": False, "own": True,
               "profile": {"name": "home", "kind": "openai", "base_url": f.url, "model": "q", "key_env": None, "context": 8192}}
        secret = self.project(sensitive=True)
        m.shadow(secret, "intake-type", "fix the parser", "FIX")
        self.assertEqual(m.shadow_pass(secret, backends=[own])["answered"], 0)
        self.assertEqual(f.seen, [])  # a sensitive project's text never leaves the machine
        plain = self.project()
        m.shadow(plain, "intake-type", "fix the parser", "FIX")
        self.assertEqual(m.shadow_pass(plain, backends=[own])["answered"], 1)

    def test_typesafe_base_url_adds_a_profile_and_names_its_key_only_when_set(self):
        self.assertNotIn("typesafe", [x["name"] for x in m.profiles()])
        os.environ["TYPESAFE_BASE_URL"] = "https://ts.example/v1/"
        got = next(x for x in m.profiles() if x["name"] == "typesafe")
        self.assertEqual((got["base_url"], got["model"], got["key_env"]), ("https://ts.example/v1", "selfjev-4b", None))
        os.environ.update(TYPESAFE_API_KEY="k", TYPESAFE_MODEL="jeff")  # pragma: allowlist secret
        got = next(x for x in m.profiles() if x["name"] == "typesafe")
        self.assertEqual((got["model"], got["key_env"]), ("jeff", "TYPESAFE_API_KEY"))


class Extras(Base):  # T-2893: a profile's body extras, its reasoning switch, and a reply the reasoning ate
    def setUp(self):
        super().setUp()
        m.UNSUPPORTED.clear()  # T-3006: refusals are remembered per profile name, process-wide
        self.addCleanup(m.UNSUPPORTED.clear)

    def test_a_refused_extra_is_not_sent_again_and_only_the_sent_ones_are_blamed(self):  # T-3006
        f = self.fake((400, {"error": "bad"}, None), (200, oa_reply("ok"), None), (200, oa_reply("ok"), None))
        prof = {**self.profile(f), "ruminate_bias": {"1": -1}}
        ask = [{"role": "user", "content": "hi"}]
        m.complete(prof, ask, schema={"type": "object"})
        self.assertEqual(list(m.support(prof)), ["schema"], "bias was never sent, so never blamed")
        m.complete(prof, ask, schema={"type": "object"}, bias=True)
        self.assertEqual(len(f.seen), 3, "one refused call, one retry, then one call: no refuse-and-resend")
        self.assertNotIn("response_format", f.seen[2][3])
        self.assertIn("logit_bias", f.seen[2][3])

    def test_a_thinking_budget_bounds_max_tokens_even_when_the_server_ignores_it(self):  # T-3006
        f = self.fake((200, oa_reply("ok"), None))
        m.complete(self.profile(f), [{"role": "user", "content": "hi"}], max_tokens=16384, think=True, think_budget=512)
        self.assertEqual(f.seen[0][3]["max_tokens"], 512 + m.ANSWER_ALLOWANCE)
    def test_the_test_switch_hides_local_embedders_too(self):  # T-2880: a server on the box is no model in tests
        from unittest import mock
        with mock.patch.dict(os.environ, {"FOREMAN_NO_LOCAL_MODELS": "1"}), \
                mock.patch.object(m, "_tags", side_effect=AssertionError("probed")):
            self.assertEqual(m.embedding_profiles(), [])

    def sent(self, prof, **kw):
        f = self.fake((200, oa_reply("ok"), None))
        prof = {**self.profile(f), **prof}
        m.complete(prof, [{"role": "user", "content": "hi"}], **kw)
        return f.seen[0][3]

    def test_body_extras_and_think_reach_the_request(self):
        body = self.sent({"body": {"top_k": 20, "model": "evil"}, "think": False})
        self.assertEqual(body["top_k"], 20)
        self.assertEqual(body["model"], "m1", "an extra never replaces the request's own keys")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        body = self.sent({"body": {"chat_template_kwargs": {"enable_thinking": True, "x": 1}}, "think": False})
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": True, "x": 1}, "body wins over think")
        self.assertNotIn("chat_template_kwargs", self.sent({}))
        self.assertTrue(self.sent({"think": False}, think=True)["chat_template_kwargs"]["enable_thinking"],
                        "the call's think overrides the profile's")

    def test_reason_arms_reach_the_request_and_unsupported_ones_are_reported(self):  # T-3006
        body = self.sent({"ruminate_bias": {"151667": -5}}, think=True, think_budget=512, bias=True,
                         schema={"type": "object"})
        self.assertEqual(body["thinking_budget_tokens"], 512)
        self.assertEqual(body["logit_bias"], {"151667": -5})
        self.assertEqual(body["response_format"]["json_schema"]["schema"], {"type": "object"})
        m.UNSUPPORTED.clear()
        body = self.sent({}, bias=True)
        self.assertNotIn("logit_bias", body)
        self.assertIn("logit_bias", m.support({"name": "t"}), "no bias set: said, per profile")

    def test_a_server_refusing_the_extras_is_asked_again_without_them_and_remembered(self):  # T-3006
        m.UNSUPPORTED.clear()
        f = self.fake((400, {"error": "unknown field response_format"}, None), (200, oa_reply("ok"), None))
        r = m.complete(self.profile(f), [{"role": "user", "content": "hi"}], schema={"type": "object"})
        self.assertEqual(r["text"], "ok")
        self.assertNotIn("response_format", f.seen[1][3])
        self.assertIn("schema", m.support({"name": "t"}))

    def test_a_budget_spent_on_thinking_closes_it_and_asks_again_with_thinking_off(self):  # T-3006
        eaten = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {}}
        f = self.fake((200, eaten, None), (200, oa_reply("answer"), None))
        r = m.complete(self.profile(f), [{"role": "user", "content": "hi"}], think=True, think_budget=64)
        self.assertEqual(r["text"], "answer")
        self.assertIs(f.seen[1][3]["chat_template_kwargs"]["enable_thinking"], False)

    def test_think_false_reaches_ollama_too(self):
        # T-2928: Ollama's OpenAI API ignores chat_template_kwargs and think; only reasoning_effort none turns it off
        self.assertEqual(self.sent({"think": False})["reasoning_effort"], "none")
        self.assertNotIn("reasoning_effort", self.sent({"think": True}), "reasoning on leaves the server's default")
        self.assertNotIn("reasoning_effort", self.sent({}))
        self.assertEqual(self.sent({"think": False, "body": {"reasoning_effort": "low"}})["reasoning_effort"], "low",
                         "a body that sets it wins")

    def test_an_empty_reply_cut_by_length_names_the_reasoning(self):
        f = self.fake((200, oa_reply("", finish="length"), None))
        with self.assertRaises(m.ModelError) as e:
            m.complete(self.profile(f), [{"role": "user", "content": "hi"}])
        self.assertIn("reasoning used the whole budget", str(e.exception))
        f = self.fake((200, oa_reply("half a sent", finish="length"), None))
        self.assertEqual(m.complete(self.profile(f), [{"role": "user", "content": "hi"}])["text"], "half a sent")

    def test_the_new_profile_keys_are_checked(self):
        for k, v in (("body", []), ("think", "no"), ("slots", 0), ("max_out", True), ("slots", 1.5)):
            with self.subTest(k=k), self.assertRaises(m.ModelError):
                m._profile("x", {"model": "a", k: v}, "t")
        p = m._profile("x", {"model": "a", "body": {"a": 1}, "think": False, "slots": 2, "max_out": 9000}, "t")
        self.assertEqual((p["body"], p["think"], p["slots"], p["max_out"]), ({"a": 1}, False, 2, 9000))

    def test_a_profile_keeps_a_boolean_small_and_refuses_any_other(self):  # T-3974: smith reads profile["small"]
        self.assertIs(m._profile("x", {"model": "a", "small": True}, "t")["small"], True)
        self.assertNotIn("small", m._profile("x", {"model": "a"}, "t"))
        with self.assertRaises(m.ModelError) as e:
            m._profile("x", {"model": "a", "small": "yes"}, "t")
        self.assertIn("small must be true or false", str(e.exception))


class Purposes(unittest.TestCase):  # T-3744: a job goes to a model made for it before scores are compared
    def test_purposes_are_checked(self):
        for bad in ("tools", [], [""], ["a", 5]):
            with self.subTest(bad=bad), self.assertRaises(m.ModelError):
                m._profile("x", {"model": "a", "purposes": bad}, "t")
        self.assertEqual(m._profile("x", {"model": "a", "purposes": ["tools", "structured"]}, "t")["purposes"],
                         ["tools", "structured"])
        self.assertNotIn("purposes", m._profile("x", {"model": "a"}, "t"))

    def test_a_job_only_goes_to_a_model_whose_purposes_include_it(self):
        general, lfm, scenes = {"name": "g"}, {"name": "l", "purposes": ["tools", "structured"]}, \
            {"name": "s", "purposes": ["3d-scenes"]}
        order = [general, scenes, lfm]
        self.assertEqual(m.for_purpose(order, "structured"), [lfm])
        self.assertEqual(m.for_purpose(order, "3d-scenes"), [scenes])
        self.assertEqual(m.for_purpose(order, "agent"), [general], "none matches: today's (general) choice")
        self.assertEqual(m.for_purpose(order, None), [general], "an unlabelled job never lands on a specialist")
        self.assertEqual(m.for_purpose([scenes, lfm], "agent"), [scenes, lfm], "only specialists: nothing to drop")


def ollama_reply(text="ok", tool_calls=None, done="stop"):
    msg = dict({"role": "assistant", "content": text}, **({"tool_calls": tool_calls} if tool_calls else {}))
    return {"message": msg, "done_reason": done, "prompt_eval_count": 7, "eval_count": 3}


class OllamaThreads(Base):  # T-3588: llama.cpp's CPU threads busy-poll while the GPU works; /v1 drops num_thread
    VRAM = {"name": "m1", "model": "m1", "size": 100, "size_vram": 100}

    def setUp(self):
        super().setUp()
        m._VRAM.clear()  # /api/ps verdicts are kept per (base_url, model), process-wide; a fake's port may come back
        self.addCleanup(m._VRAM.clear)

    def ollama(self, *rows, reply=None):
        f = self.fake((200, {"models": list(rows)}, None), (200, reply or ollama_reply(), None))
        m.LOCAL = [("ollama", f.url)]  # the port Sampo knows Ollama by
        return f

    def test_a_model_wholly_in_vram_is_asked_natively_with_one_thread(self):
        call = {"id": "call_9", "function": {"index": 0, "name": "read", "arguments": {"path": "a.txt"}}}
        f = self.ollama(self.VRAM, reply=ollama_reply("", [call]))
        msgs = [{"role": "user", "content": "read b"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read",
                                                                     "args": {"path": "b"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "B"}]
        prof = {**self.profile(f, key=False), "think": False, "body": {"temperature": 0.2}}
        r = m.complete(prof, msgs, tools=[TOOL], max_tokens=99, schema={"type": "object"})
        self.assertEqual([s[:2] for s in f.seen], [("GET", "/api/ps"), ("POST", "/api/chat")])
        body = f.seen[1][3]
        self.assertEqual(body["options"], {"num_predict": 99, "num_thread": m.GPU_THREADS, "temperature": 0.2,
                                           "num_ctx": prof["context"]})  # T-3753: the profile's context goes along
        self.assertEqual((body["model"], body["stream"], body["think"], body["format"]),
                         ("m1", False, False, {"type": "object"}))
        self.assertEqual(body["tools"], [TOOL])
        self.assertEqual(body["messages"][1]["tool_calls"],
                         [{"id": "c1", "function": {"name": "read", "arguments": {"path": "b"}}}],
                         "native arguments are an object, not a JSON string")
        self.assertEqual(body["messages"][2], dict(msgs[2], tool_name="read"), "a tool result named by its call")
        self.assertNotIn("tool_name", msgs[2], "the caller's messages are never changed")
        self.assertEqual(r, {"text": "", "tool_calls": [{"id": "call_9", "name": "read", "args": {"path": "a.txt"}}],
                             "usage": {"in": 7, "out": 3}, "stop": "tool_calls"})
        f = self.ollama(self.VRAM)
        prof = {**self.profile(f, key=False), "body": {"options": {"num_thread": 3}}}
        m.complete(prof, msgs[:1], think=True)
        self.assertEqual(f.seen[1][3]["options"]["num_thread"], 3, "a profile's body options win: the calibration knob")
        self.assertNotIn("think", f.seen[1][3], "thinking on is the model's default: think true 400s without thinking")
        m.complete(prof, msgs[:1])
        self.assertEqual([s[:2] for s in f.seen[2:]], [("POST", "/api/chat")], "the ps verdict is reused for PS_TTL")

    def test_a_profile_context_is_sent_as_num_ctx(self):
        # T-3753: with no num_ctx Ollama serves its 2048/4096 default and cuts the prompt (a SWE-bench run's 8.6k-token
        # first prompt on gpu-qwen9, 2026-11-07); the profile's declared context goes with every native call
        msgs = [{"role": "user", "content": "hi"}]
        f = self.ollama(self.VRAM)
        m.complete({**self.profile(f, key=False), "context": 32768}, msgs)
        self.assertEqual(f.seen[1][3]["options"]["num_ctx"], 32768)
        f = self.ollama(self.VRAM)
        m.complete({**self.profile(f, key=False), "context": 32768, "body": {"options": {"num_ctx": 8192}}}, msgs)
        self.assertEqual(f.seen[1][3]["options"]["num_ctx"], 8192, "a profile body's own num_ctx wins")
        for ctx in (None, 0, "32k", True):
            f = self.ollama(self.VRAM)
            m.complete({**self.profile(f, key=False), "context": ctx}, msgs)
            self.assertNotIn("num_ctx", f.seen[1][3]["options"], ctx)

    def test_a_model_seen_wholly_in_vram_stays_native_after_an_eviction(self):
        from unittest import mock
        f = self.fake((200, {"models": [self.VRAM]}, None), (200, ollama_reply(), None),
                      (200, {"models": []}, None), (200, ollama_reply(), None))
        m.LOCAL = [("ollama", f.url)]
        with mock.patch.object(m, "PS_TTL", 0):
            for _ in range(2):
                m.complete(self.profile(f, key=False), [{"role": "user", "content": "hi"}])
        self.assertEqual([s[1] for s in f.seen], ["/api/ps", "/api/chat", "/api/ps", "/api/chat"],
                         "Ollama evicts others before it splits a model that fit: no second reload at default threads")

    def test_a_reply_cut_at_max_tokens_or_without_a_message_reads_as_the_openai_one(self):
        f = self.ollama(self.VRAM, reply=ollama_reply("par", done="length"))
        self.assertEqual(m.complete(self.profile(f, key=False), [{"role": "user", "content": "hi"}])["stop"], "length")
        f = self.ollama(self.VRAM, reply={"error": "boom"})
        with self.assertRaisesRegex(m.ModelError, "no usable choices"):
            m.complete(self.profile(f, key=False), [{"role": "user", "content": "hi"}])

    def test_anything_else_keeps_the_openai_path_without_num_thread(self):
        ask = [{"role": "user", "content": "hi"}]
        for why, rows in (("partly in system RAM", [dict(self.VRAM, size_vram=60)]), ("nothing loaded", []),
                          ("not loaded, though another sits wholly in VRAM: a big one would load partly in RAM",
                           [dict(self.VRAM, name="x", model="x")])):
            with self.subTest(why):
                f = self.fake((200, {"models": rows}, None), (200, oa_reply("ok"), None))
                m.LOCAL = [("ollama", f.url)]
                self.assertEqual(m.complete(self.profile(f, key=False), ask)["text"], "ok")
                self.assertEqual(f.seen[1][:2], ("POST", "/v1/chat/completions"))
                self.assertNotIn("options", f.seen[1][3])
        f = self.fake((404, {}, None), (200, oa_reply("ok"), None))
        m.LOCAL = [("ollama", f.url)]
        self.assertEqual(m.complete(self.profile(f, key=False), ask)["text"], "ok", "no /api/ps: not Ollama")
        self.assertEqual(f.seen[1][1], "/v1/chat/completions")
        f = self.fake((200, oa_reply("ok"), None))
        m.LOCAL = [("ollama", "http://127.0.0.1:11434/v1")]
        m.complete(self.profile(f, key=False), ask)
        self.assertEqual([s[:2] for s in f.seen], [("POST", "/v1/chat/completions")], "another port is never probed")
        self.assertIsNone(m._gpu_ollama({**self.profile(f), "base_url": "https://gpu.example.test:11434/v1"}), "remote")
        f = self.fake((200, oa_reply("ok"), None))
        m.LOCAL = [("ollama", f.url)]
        m.complete(self.profile(f, key=False), [{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        self.assertEqual([s[:2] for s in f.seen], [("POST", "/v1/chat/completions")], "list content: /v1, unprobed")
