"""T-4923: `sampo outline PATH --grep PATTERN` keeps only the definitions whose label matches (a regex, case-sensitive),
before the 400-row cap, so a definition past row 400 is still found; no match exits 0 with a note."""
import json
import os
import subprocess
import sys
import unittest

from helpers import FM, ForemanTestCase


class OutlineGrep(ForemanTestCase):
    def talk(self, *messages):
        lines = [json.dumps(m) for m in messages]
        p = subprocess.run([sys.executable, FM, "mcp"], input="\n".join(lines) + "\n", capture_output=True, text=True,
                           cwd=self.repo, timeout=120, env=dict(os.environ, FOREMAN_HOME=self.home))
        return {m["id"]: m for m in (json.loads(x) for x in p.stdout.splitlines() if x.strip())}

    @staticmethod
    def call(i, tool, **arguments):
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def three(self):
        return self.write("three.py", "def alpha_one():\n    return 1\n\n\ndef beta_two():\n    return 2\n\n\n"
                                      "def gamma_three():\n    return 3\n")

    def big(self):
        # 450 definitions, then one more named target_last: past the 400-row cap
        text = "".join(f"def filler_{i}():\n    return {i}\n\n\n" for i in range(450))
        return self.write("big.py", text + "def target_last():\n    return 0\n")

    def test_grep_keeps_only_the_named_definition(self):
        p = self.fm("outline", self.three(), "--grep", "beta")
        self.assertEqual(p.returncode, 0)
        self.assertIn("beta_two", p.stdout)
        self.assertNotIn("alpha_one", p.stdout)
        self.assertNotIn("gamma_three", p.stdout)

    def test_grep_is_a_case_sensitive_regex(self):
        path = self.three()
        out = self.fm("outline", path, "--grep", "^def (alpha|gamma)_").stdout
        self.assertIn("alpha_one", out)
        self.assertIn("gamma_three", out)
        self.assertNotIn("beta_two", out)
        self.assertIn("no definition matches", self.fm("outline", path, "--grep", "Beta").stdout)
        self.assertIn("beta_two", self.fm("outline", path, "--grep", "beta").stdout)

    def test_the_filter_runs_before_the_row_cap(self):
        path = self.big()
        capped = self.fm("outline", path).stdout
        self.assertNotIn("target_last", capped, "without --grep the 400-row cap hides the last definition")
        self.assertIn("…", capped)
        p = self.fm("outline", path, "--grep", "target_last")
        self.assertIn("target_last", p.stdout)
        self.assertNotIn("filler_0", p.stdout)
        self.assertNotIn("…", p.stdout, "one match is under the cap: no truncation mark")

    def test_no_match_exits_zero_with_a_note(self):
        p = self.fm("outline", self.three(), "--grep", "nothing_like_this")
        self.assertEqual(p.returncode, 0)
        self.assertIn("no definition matches", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_json_defs_are_the_matched_ones(self):
        data = self.fm_json("outline", self.three(), "--grep", "gamma")
        self.assertEqual([d["name"] for d in data["defs"]], ["def gamma_three"])

    def test_the_json_names_the_pattern_and_null_when_unfiltered(self):
        path = self.three()
        self.assertEqual(self.fm_json("outline", path, "--grep", "beta")["grep"], "beta")
        self.assertIn("grep", self.fm_json("outline", path))
        self.assertIsNone(self.fm_json("outline", path)["grep"])

    def test_the_mcp_outline_tool_takes_grep_and_keeps_only_the_matches(self):
        with open(os.path.join(self.repo, "three.py"), "w") as f:  # the MCP path resolves under the project root
            f.write("def alpha_one():\n    return 1\n\n\ndef beta_two():\n    return 2\n\n\ndef gamma_three():\n    return 3\n")
        self.fm("init")
        got = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        self.call(2, "outline", path="three.py", grep="beta"),
                        self.call(3, "outline", path="three.py", grep="-x"))
        tools = {t["name"]: t for t in got[1]["result"]["tools"]}
        self.assertEqual(tools["outline"]["inputSchema"]["properties"]["grep"]["type"], "string")
        text = got[2]["result"]["content"][0]["text"]
        self.assertFalse(got[2]["result"]["isError"], text)
        self.assertIn("beta_two", text)
        self.assertNotIn("alpha_one", text)
        self.assertNotIn("gamma_three", text)
        self.assertTrue(got[3]["result"]["isError"], "a grep that starts with '-' is refused, as every value is")

    def test_a_bad_pattern_is_a_usage_error_not_a_traceback(self):
        p = self.fm("outline", self.three(), "--grep", "(", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("--grep", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
