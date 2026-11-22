"""T-1058: fm backtest counts where a changed rule, known fix or card would have fired on recorded sessions."""
import json
import os
import unittest

from helpers import ForemanTestCase


class Backtest(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.uhome = os.path.join(self.tmp, "uhome")
        self.log = os.path.join(self.uhome, ".claude", "projects", "-work-app", "s1.jsonl")
        os.makedirs(os.path.dirname(self.log))
        self.cards_dir = os.path.join(self.tmp, "cards")  # SAMPO_CALLCARDS: calling cards (T-5612)

    def write(self, calls):
        """calls: (command, error text or None); each becomes a tool_use plus its tool_result."""
        with open(self.log, "w") as f:
            for i, (cmd, err) in enumerate(calls):
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": cmd}}]}}) + "\n")
                f.write(json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": err is not None,
                     "content": err or "ok"}]}}) + "\n")

    def bt(self, *args, check=True):
        return self.fm("backtest", *args, env={"HOME": self.uhome}, check=check)

    def test_rule_counts_fires_and_errors(self):
        self.write([("npm install left-pad", "npm ERR! 404"), ("npm install lodash", None), ("ls", None)])
        r = self.bt("--rule", "npm install")
        self.assertIn("would have fired 2 times in 1 session(s)", r.stdout)
        self.assertIn("1 followed by an error", r.stdout)

    def test_json_keys_for_card(self):
        self.write([("cat react-hook-form.md", None), ("ls", None)])
        d = json.loads(self.bt("--card", "react-hook-form", "--json").stdout)
        self.assertEqual((d["fires"], d["sessions"], d["errored"]), (1, 1, 0))

    def test_fix_matches_error_text(self):
        self.write([("cargo build", "cargo: command not found"), ("ls", None)])
        d = json.loads(self.bt("--fix", "cargo: command not found", "--json").stdout)
        self.assertEqual((d["fires"], d["errored"]), (1, 1))

    def test_empty_corpus_and_bad_input(self):
        self.assertIn("no recent session", self.bt("--rule", "x").stdout)
        self.write([("ls", None)])
        self.assertNotEqual(self.bt("--rule", "(", check=False).returncode, 0)
        self.assertNotEqual(self.bt("--card", "no-such-card-zz", check=False).returncode, 0)

    def write_calls(self, calls):
        """T-5612: calls: (tool name, input, result, errored); each becomes a tool_use plus its tool_result."""
        with open(self.log, "w") as f:
            for i, (name, inp, result, err) in enumerate(calls):
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}) + "\n")
                f.write(json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": err, "content": result}]}}) + "\n")

    def calling_card(self, name="grep-runs", **spec):  # not a shipped name: plugin/cards/py-callers shadows it
        """A calling card folder in SAMPO_CALLCARDS, shaped as test_callcards.py writes one; spec adds its fields."""
        d = os.path.join(self.cards_dir, name)
        os.makedirs(d, exist_ok=True)
        spec = {"name": name, "purpose": f"{name}: find the callers of a function", "examples": ["who calls f"],
                "kind": "py", "inputs": {"type": "object", "properties": {}}, "files": [], "deterministic": False,
                **spec}
        with open(os.path.join(d, "card.json"), "w") as f:
            json.dump(spec, f)
        for fn in ("gen.py", "check.py"):
            with open(os.path.join(d, fn), "w") as f:
                f.write("")
        return name

    def cards_bt(self, *args, check=True):
        return self.fm("backtest", *args, env={"HOME": self.uhome, "SAMPO_CALLCARDS": self.cards_dir}, check=check)

    def test_calling_card_counts_the_hand_runs_it_replaces(self):
        self.calling_card(hand=[r"grep -rn \w+ \."])
        self.write_calls([("Bash", {"command": "grep -rn foo ."}, "a" * 100, False),
                          ("Bash", {"command": "grep -rn bar ."}, "b" * 60, False),
                          ("Bash", {"command": "grep -rn baz ."}, "e" * 40, True),
                          ("Bash", {"command": "ls"}, "c" * 1000, False)])
        d = json.loads(self.cards_bt("--card", "grep-runs", "--json").stdout)
        self.assertEqual((d["fires"], d["sessions"], d["errored"], d["calls"]), (3, 1, 1, 4))
        self.assertEqual((d["result_bytes"], d["tokens_read_est"]), (200, 50))
        self.assertNotIn("tokens_saved_est", d)
        text = self.cards_bt("--card", "grep-runs").stdout
        self.assertIn("reading 200 bytes (about 50 tokens, gross)", text)
        self.assertIn("errored or not", text)
        self.assertIn("no once-per-session limit", text)

    def test_calling_card_ignores_a_read_of_the_same_words(self):
        self.calling_card(hand=[r"grep -rn \w+ \."])
        self.write_calls([("Read", {"file_path": "grep -rn foo ."}, "r" * 500, False)])
        d = json.loads(self.cards_bt("--card", "grep-runs", "--json").stdout)
        self.assertEqual((d["fires"], d["result_bytes"], d["tokens_read_est"]), (0, 0, 0))

    def test_calling_card_matches_the_command_not_its_description(self):
        self.calling_card(hand=[r"grep -rn \w+ \."])
        self.write_calls([("Bash", {"command": "ls", "description": "grep -rn foo ."}, "x" * 100, False),
                          ("Bash", {"command": "grep -rn foo .", "description": "list"}, "y" * 100, False)])
        d = json.loads(self.cards_bt("--card", "grep-runs", "--json").stdout)
        self.assertEqual((d["fires"], d["result_bytes"]), (1, 100))

    def test_calling_card_reads_only_the_first_2000_characters_of_a_command(self):
        self.calling_card(hand=[r"grep -rn foo \."])
        self.write_calls([("Bash", {"command": "grep -rn foo ." + " " * 2500}, "ok", False)])
        early = json.loads(self.cards_bt("--card", "grep-runs", "--json").stdout)
        self.write_calls([("Bash", {"command": "x" * 2100 + " grep -rn foo ."}, "ok", False)])
        late = json.loads(self.cards_bt("--card", "grep-runs", "--json").stdout)
        self.assertEqual((early["fires"], late["fires"]), (1, 0))

    def test_calling_card_counts_result_bytes_in_utf8(self):
        self.calling_card(hand=[r"grep -rn \w+ \."])
        self.write_calls([("Bash", {"command": "grep -rn foo ."}, "é" * 50, False)])
        d = json.loads(self.cards_bt("--card", "grep-runs", "--json").stdout)
        self.assertEqual((d["result_bytes"], d["tokens_read_est"]), (100, 25))

    def test_calling_card_without_hand_is_refused_naming_the_field(self):
        self.calling_card("bare-card")
        self.calling_card("empty-hand", hand=[])
        self.write_calls([("Bash", {"command": "ls"}, "ok", False)])
        for name in ("bare-card", "empty-hand"):
            r = self.cards_bt("--card", name, check=False)
            self.assertNotEqual(r.returncode, 0, name)
            self.assertIn('"hand"', r.stdout + r.stderr, name)

    def test_calling_card_bad_hand_pattern_is_refused(self):
        self.calling_card(hand=["grep ("])
        self.write_calls([("Bash", {"command": "ls"}, "ok", False)])
        r = self.cards_bt("--card", "grep-runs", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is not a regex", r.stdout + r.stderr)

    def test_stack_card_wins_over_a_calling_card_of_the_same_name(self):
        self.calling_card("react-hook-form", hand=[r"cat react"])
        self.write_calls([("Bash", {"command": "cat react-hook-form.md"}, "ok", False),
                          ("Bash", {"command": "ls"}, "ok", False)])
        d = json.loads(self.cards_bt("--card", "react-hook-form", "--json").stdout)
        self.assertEqual((d["fires"], d["sessions"], d["errored"]), (1, 1, 0))
        self.assertNotIn("result_bytes", d)

    def test_unknown_card_name_is_refused_naming_both_shelves(self):
        self.calling_card(hand=[r"x"])
        r = self.cards_bt("--card", "no-such-card-zz", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sampo card list", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
