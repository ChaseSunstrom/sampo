"""T-1432: fm code hardening. An inline --worker template that turns its CLI's approvals off is refused; more cloud metadata
endpoints (and names resolving to one) are refused; a server's odd usage counts don't crash a run; a lean claude planner
that loads more than the guard is stopped; a read from inside a worker never creates a project."""
import json
import os
import socket
import sys
import types
import unittest
from unittest import mock

from helpers import ForemanTestCase, git_repo
from test_code import CodeCase
from sampo import smith
from sampo import models


class InlineWorker(CodeCase):
    def test_an_inline_template_with_an_approval_off_flag_is_refused(self):
        for tpl in ("gemini --yolo {prompt}", "gemini -y -p {prompt}", "claude -p {prompt} --dangerously-skip-permissions"):
            with self.subTest(tpl=tpl):
                r = self.code("make calc.add add", "--worker", tpl, "--verify", "python3 test_calc.py", check=False)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("code.toml", r.stderr)
                self.assertIn("[ack]", r.stderr)
                self.assertIn("a - b", self.calc())
        self.assertEqual(smith.worker_argv("mycli -y {prompt}", "go", which=False), ["mycli", "-y", "go"])
        # T-1432 review: a shell wrapper's script is one word, so the flag inside it hid from the lint
        for tpl in ("sh -c 'gemini --yolo {prompt}'", "bash -lc \"claude -p {prompt} --dangerously-skip-permissions\"",
                    "env sh -c 'codex exec -a never {prompt}'"):
            with self.subTest(tpl=tpl):
                self.assertTrue(smith.dangerous_flags(tpl), tpl)
        self.assertEqual(smith.dangerous_flags("sh -c 'mycli -y {prompt}'"), [])


def model(url):
    return {"name": "x", "kind": "openai", "base_url": url, "model": "m"}


class MetadataEndpoints(unittest.TestCase):
    def setUp(self):
        getattr(models, "_RESOLVED", {}).clear()  # T-1436: each test resolves afresh

    def test_a_host_is_looked_up_once_a_minute_and_a_failed_lookup_isnt_kept(self):
        # T-1436: _check_url runs on every model call; each looked the host up again (up to 2 s on a slow resolver)
        calls = []

        def answer(host, *a, **k):
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
        with mock.patch.object(socket, "getaddrinfo", answer):
            self.assertFalse(models._link_local("models.example.com"))
            self.assertFalse(models._link_local("models.example.com"))
        self.assertEqual(calls, ["models.example.com"])
        with mock.patch.object(socket, "getaddrinfo", side_effect=OSError("no answer")) as failing:
            models._link_local("down.example.com")
            models._link_local("down.example.com")
        self.assertEqual(failing.call_count, 2, "a failed lookup is tried again")

    def addrs(self, *ips):
        return lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]

    def refused(self, url):
        with self.assertRaises(models.ModelError) as e:
            models.complete(model(url), [{"role": "user", "content": "hi"}], timeout=1)
        self.assertIn("metadata", str(e.exception))

    def test_alibaba_and_azure_metadata_addresses_are_refused(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("no lookup for an address")):
            for url in ("http://100.100.100.200/latest", "http://168.63.129.16/", "http://[::ffff:100.100.100.200]/"):
                with self.subTest(url=url):
                    self.refused(url)

    def test_a_hostname_resolving_to_a_metadata_address_is_refused(self):
        with mock.patch.object(socket, "getaddrinfo", self.addrs("10.0.0.5", "169.254.169.254")):
            self.refused("https://metadata.example.com/v1")
        with mock.patch.object(socket, "getaddrinfo", self.addrs("100.100.100.200")):
            self.refused("https://meta.example.com/v1")
        with mock.patch.object(socket, "getaddrinfo", self.addrs("10.0.0.5")):
            models._check_url({"name": "x"}, "https://models.example.com/v1", None)

    def test_a_lookup_that_fails_or_hangs_is_bounded_and_not_refused(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror("no such name")):
            models._check_url({"name": "x"}, "https://nowhere.example.com/v1", None)
        import threading
        gate = threading.Event()
        with mock.patch.object(socket, "getaddrinfo", lambda *a, **k: gate.wait(30) and []), \
                mock.patch.object(models, "RESOLVE_TIMEOUT", 0.2):
            import time
            t0 = time.monotonic()
            models._check_url({"name": "x"}, "https://slow.example.com/v1", None)
            self.assertLess(time.monotonic() - t0, 5)  # wall clock: RESOLVE_TIMEOUT deadline (a thread blocks on the gate)
        gate.set()


class OddUsage(ForemanTestCase):
    def test_string_or_null_counts_count_as_zero(self):
        run = smith.Run(types.SimpleNamespace(), self.repo, self.tmp)
        for usage in ({"in": "12", "out": None}, {"in": None, "out": "lots"}, {"in": True, "out": [1]}, {}):
            run.usage({"usage": usage, "text": ""})
        run.usage({"usage": {"in": 3, "out": 4}, "text": ""})
        self.assertEqual(run.tokens, [3, 4])

    def test_a_cli_result_with_string_usage_reads(self):
        reply = os.path.join(self.tmp, "reply.json")
        with open(reply, "w") as f:
            json.dump({"type": "result", "result": "hi", "usage": {"input_tokens": "10", "cache_read_input_tokens": None,
                                                                   "output_tokens": "2"}}, f)
        got = models._cli({"name": "c", "kind": "cli", "argv": f"cat {reply}"}, [{"role": "user", "content": "x"}], 10)
        self.assertEqual(got["text"], "hi")
        self.assertEqual(got["usage"]["in"], 0)


class LeanPlanner(CodeCase):
    def test_a_lean_claude_planner_whose_init_lists_an_mcp_server_is_stopped(self):
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        init = {"type": "system", "subtype": "init", "mcp_servers": [{"name": "evil", "status": "connected"}],
                "tools": ["Read"], "plugins": []}
        plan = {"type": "FIX", "tier": "S", "title": "t", "criteria": [{"text": "adds", "verify": "test -f calc.py"}], "steps": ["s"]}
        result = {"type": "result", "is_error": False, "result": json.dumps(plan), "usage": {}}
        with open(os.path.join(bin_, "claude"), "w") as f:
            f.write(f"#!/bin/sh\necho '{json.dumps(init)}'\nsleep 20\necho '{json.dumps(result)}'\n")
        os.chmod(os.path.join(bin_, "claude"), 0o755)
        import time
        t0 = time.monotonic()
        r = self.code("make calc.add add", "--worker", "claude", check=False,
                      env={"PATH": bin_ + os.pathsep + os.environ["PATH"]})
        self.assertLess(time.monotonic() - t0, 15, "stopped at its init line, not after the sleep")  # wall clock: kill deadline of a child worker
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("MCP server evil", r.stderr)
        self.assertIn("a - b", self.calc())


class WorkerReadCreatesNothing(ForemanTestCase):
    def test_fm_state_in_an_unregistered_folder_creates_no_project(self):
        from sampo import core
        folder = git_repo(self.tmp, "other")  # a git repo fm never saw
        self.assertIsNone(core.find_project(folder, create=False))
        self.fm("state", cwd=folder, check=False, env={"FOREMAN_WORKER_OF": "fm-code-T-0001"})
        self.assertIsNone(core.find_project(folder, create=False))
        self.fm("state", cwd=folder, check=False)  # outside a worker, a read still sets the folder up as before
        self.assertIsNotNone(core.find_project(folder, create=False))


class ReviewNits(ForemanTestCase):
    """T-1438: the T-1432 review's nits."""

    def test_a_stop_kills_the_group_once(self):
        from sampo import core
        on_line = mock.Mock(return_value=models.STOP)
        prof = {"name": "c", "kind": "cli", "stream": "claude", "on_line": on_line, "argv": "printf 'a\\nb\\nc\\n' {prompt}"}
        with mock.patch.object(core, "kill_group", wraps=core.kill_group) as kill:
            with self.assertRaises(models.ModelError):
                models._cli(prof, [{"role": "user", "content": "x"}], 10)
        self.assertEqual(kill.call_count, 1)
        self.assertEqual(on_line.call_count, 1, "no line after the STOP reaches the caller's check")

    def test_the_local_probe_refuses_a_metadata_address(self):
        opener = mock.Mock(open=mock.Mock(side_effect=AssertionError("probed")))
        with mock.patch.object(models, "_LOCAL_OPENER", opener), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("no lookup for an address")):
            for url in ("http://169.254.169.254/v1", "http://100.100.100.200/v1", "http://[fe80::1]:80/v1"):
                with self.subTest(url=url):
                    self.assertIsNone(models._probe("x", url))
        opener.open.assert_not_called()

    def test_more_approval_off_flags(self):
        for tpl in ("aider --yes-always --message {prompt}", "q chat --trust-all-tools {prompt}",
                    "cursor-agent --force -p {prompt}", "/usr/bin/cursor-agent -p {prompt} --force",
                    "codex exec -anever {prompt}", "sh -c 'aider --yes-always -m {prompt}'"):
            with self.subTest(tpl=tpl):
                self.assertTrue(smith.dangerous_flags(tpl), tpl)
        for tpl in ("mycli --force {prompt}", "aider --yes {prompt}", "q chat {prompt}", "codex exec -a on-request {prompt}"):
            with self.subTest(tpl=tpl):
                self.assertEqual(smith.dangerous_flags(tpl), [], tpl)

    def test_allow_all_flags(self):
        # T-1443: Copilot CLI's allow-all, Factory droid's skip-permissions-unsafe, Claude's allow-dangerously-…
        for tpl in ("copilot -p {prompt} --allow-all-tools", "copilot --allow-all -p {prompt}",
                    "droid exec --skip-permissions-unsafe {prompt}", "claude -p {prompt} --allow-dangerously-skip-permissions"):
            with self.subTest(tpl=tpl):
                self.assertTrue(smith.dangerous_flags(tpl), tpl)
        self.assertEqual(smith.dangerous_flags("copilot -p {prompt} --allow-tool 'shell(git status)'"), [])

    def test_hooks_inside_a_worker_create_no_project(self):
        from sampo import core
        folder = git_repo(self.tmp, "other")
        worker = {"FOREMAN_WORKER_OF": "fm-code-T-0001"}
        self.hook("SessionStart", {"cwd": folder, "source": "startup"}, env=worker)
        self.hook("UserPromptSubmit", {"cwd": folder, "prompt": "hi"}, env=worker)
        self.assertIsNone(core.find_project(folder, create=False))
        self.hook("UserPromptSubmit", {"cwd": folder, "prompt": "hi"})  # outside a worker, as before
        self.assertIsNotNone(core.find_project(folder, create=False))

    def test_resolve_inside_a_worker_says_it_cant_run_fm_init(self):
        folder = git_repo(self.tmp, "other")
        r = self.fm("state", cwd=folder, check=False, env={"FOREMAN_WORKER_OF": "fm-code-T-0001"})
        self.assertIn("worker of a sampo code run can't run `sampo init`", r.stderr)
        self.assertNotIn("run `fm init` here", r.stderr)


if __name__ == "__main__":
    unittest.main()
