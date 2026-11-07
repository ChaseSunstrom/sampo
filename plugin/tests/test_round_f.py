"""T-0071 round F (super brainstorm 2) and the D+E review fixes: shell edits count at done, cached passes expire,
gates that write, fm why, the status answer without the model, supply-chain files, audits as data, the thrash note
once per failure, the weekly check throttled, the cost canary."""
import json
import os
import subprocess

from helpers import FM, ForemanTestCase

from sampo import core as c
from sampo import recall


class RoundF(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)

    def test_files_changed_through_the_shell_count_at_done(self):
        self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--step", "s", "--ac", "a", "--scope", "src/**",
                "--focus")
        os.makedirs(os.path.join(self.repo, "src"))
        for rel in ("src/a.py", "setup.cfg"):  # written with sed/echo: no Edit hook saw them
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write("x\n")
        p = self.fm("task", "finish", "T-0001", "--run", "true", "--audit", "checked", check=False)
        self.assertIn("edited outside scope [src/**]: setup.cfg", p.stderr)
        self.fm("task", "log", "T-0001", "scope: build config")
        self.fm("task", "finish", "T-0001", "--run", "true", "--audit", "checked")  # T-3550: refused before it ran
        self.assertIn("src/a.py", c.find_brief(self.p, "T-0001").section("Files touched"))

    def test_an_old_cached_pass_reruns_and_a_gate_that_writes_is_named(self):
        self.fm("check", "add", "true")
        self.fm("check")
        tree = c.worktree_id(self.repo)
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a") as f:
            f.write(json.dumps({"ts": "2000-01-01T00:00:00Z", "event": "check_run", "data": {
                "tree": tree, "env": c.env_id(), "results": [{"cmd": "true", "exit": 0, "s": 0.1}]}}) + "\n")
        self.assertNotIn("cached", self.fm("check").stdout, "a pass from long ago isn't reused")
        self.fm("check", "add", "date > stamp.txt")
        out = self.fm("check").stdout
        self.assertIn("changed during the gates: stamp.txt", out)  # T-1149: named, and honest that it may be the session's
        self.assertIn("your own edit", out)

    def _counting_gate(self):
        runs = os.path.join(self.tmp, "gate-runs")
        self.fm("check", "add", f"echo x >> {runs}")
        self.fm("check")
        return runs

    def _write(self, rel, text):
        with open(os.path.join(self.repo, rel), "a") as f:
            f.write(text)

    def test_a_full_pass_holds_for_another_task_when_only_the_changelog_changed(self):
        # T-3120: a close's own CHANGELOG line (and the sync mirror) must not force a fresh suite on the next close
        runs = self._counting_gate()
        self._write("CHANGELOG.md", "- T-9999: a line\n")
        out = self.fm("check").stdout
        self.assertIn("reusing the full run", out)
        self.assertIn("same code tree", out)
        self.assertRegex(out, r"run \d{4}-\d\d-\d\d \d\d:\d\d UTC, code_tree [0-9a-f]{12}, from \S+")  # provenance
        self.assertEqual(len(open(runs).read().split()), 1, "the gate did not run again")

    def test_a_changed_changelog_is_read_by_its_test_before_the_pass_is_reused(self):
        # T-3120: the code tree leaves CHANGELOG.md out, so a malformed line must still meet the test that reads it
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        self._write("plugin/tests/run.py", "import sys\nsys.exit(1 if 'BAD' in open('CHANGELOG.md').read() else 0)\n")
        runs = self._counting_gate()
        self._write("CHANGELOG.md", "- T-9999: fine\n")
        self.assertIn("reusing the full run", self.fm("check").stdout)
        self._write("CHANGELOG.md", "- T-9999: BAD\n")
        out = self.fm("check").stdout
        self.assertNotIn("reusing", out)
        self.assertEqual(len(open(runs).read().split()), 2, "a CHANGELOG the test rejects gets the full gates")

    def test_a_pass_without_a_readable_time_is_stale(self):
        self.fm("check", "add", "true")
        tree = c.worktree_id(self.repo)
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a") as f:
            f.write(json.dumps({"event": "check_run", "data": {"tree": tree, "env": c.env_id(), "results": [
                {"cmd": "true", "exit": 0, "s": 0.1}]}}) + "\n")
        self.assertNotIn("cached", self.fm("check").stdout)

    def test_no_reuse_when_code_changed(self):
        runs = self._counting_gate()
        self._write("a.py", "y = 1\n")
        self.assertNotIn("reusing", self.fm("check").stdout)
        self.assertEqual(len(open(runs).read().split()), 2)

    def test_no_reuse_when_a_gate_command_changed(self):
        self._counting_gate()
        self._write("CHANGELOG.md", "- a line\n")
        self.fm("check", "add", "true")
        self.assertNotIn("reusing", self.fm("check").stdout)

    def _check_runs(self):
        with open(os.path.join(self.p.dir, "ledger.jsonl")) as f:
            return [e for e in map(json.loads, f) if e.get("event") == "check_run"]

    def test_an_affected_run_is_no_full_pass_and_leaves_the_full_one_standing(self):
        runs = self._counting_gate()
        self.fm("check", "affected", "true {tests}")
        self._write("CHANGELOG.md", "- a line\n")
        self.fm("check", "--affected", check=False)
        self.assertEqual(len(self._check_runs()), 1, "an affected run logs no full check_run")
        self.assertIn("reusing the full run", self.fm("check").stdout)
        self.assertEqual(len(open(runs).read().split()), 1)

    def test_a_failing_prior_run_on_the_same_code_tree_is_not_reused(self):
        runs = os.path.join(self.tmp, "gate-runs")
        flag = os.path.join(self.tmp, "red")
        open(flag, "w").close()
        self.fm("check", "add", f"echo x >> {runs}; test ! -e {flag}")
        self.fm("check", check=False)
        os.remove(flag)
        self._write("CHANGELOG.md", "- a line\n")
        self.assertNotIn("reusing", self.fm("check").stdout)
        self.assertEqual(len(open(runs).read().split()), 3)  # the red run and its flaky rerun, then this one

    def test_a_run_older_than_twelve_hours_is_not_reused(self):
        runs = self._counting_gate()
        path = os.path.join(self.p.dir, "ledger.jsonl")
        with open(path) as f:
            lines = [json.loads(x) for x in f]
        for e in lines:
            if e.get("event") == "check_run":
                e["ts"] = "2000-01-01T00:00:00Z"
        with open(path, "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in lines))
        self._write("CHANGELOG.md", "- a line\n")
        self.assertNotIn("reusing", self.fm("check").stdout)
        self.assertEqual(len(open(runs).read().split()), 2)

    def test_a_changed_skip_set_is_not_reused(self):
        self._counting_gate()
        last = self._check_runs()[-1]["data"]
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a") as f:  # a newer full run that left test files out
            f.write(json.dumps({"ts": c.now(), "event": "check_run", "data": dict(last, skip_tests=["test_x.py"])}) + "\n")
        self._write("CHANGELOG.md", "- a line\n")
        self.assertNotIn("reusing", self.fm("check").stdout)

    def test_fresh_after_a_changelog_only_change_reruns(self):
        runs = self._counting_gate()
        self._write("CHANGELOG.md", "- a line\n")
        self.assertNotIn("reusing", self.fm("check", "--fresh").stdout)
        self.assertEqual(len(open(runs).read().split()), 2)

    def test_a_non_markdown_file_in_the_mirror_is_code(self):
        # T-3120: .sampo/policy.toml is read as repo policy; only the mirror's .md files are bookkeeping
        os.makedirs(os.path.join(self.repo, ".sampo"), exist_ok=True)
        self._write(".sampo/notes.md", "a\n")
        self._write(".sampo/policy.toml", "x = 1\n")
        base = c.code_tree(self.repo)
        self._write(".sampo/notes.md", "b\n")
        self._write("CHANGELOG.md", "- a line\n")
        self.assertEqual(c.code_tree(self.repo), base, "mirror .md and CHANGELOG.md are not code")
        self._write(".sampo/policy.toml", "x = 2\n")
        self.assertNotEqual(c.code_tree(self.repo), base)

    def test_a_close_on_another_task_reuses_the_full_pass_and_says_so(self):
        # T-3120: the full gates cost the close; task B, whose only change is a CHANGELOG line, doesn't rerun them
        runs = self._counting_gate()
        self.fm("task", "new", "A", "--type", "FEATURE", "--tier", "S", "--step", "s", "--ac", "a :: true", "--focus")
        out = self.fm("task", "finish", "T-0001", "--run", f"{FM} check", "--audit", "checked").stdout
        self.assertIn("reusing the full run", out)
        self._write("CHANGELOG.md", "- T-0001: closed\n")
        self.fm("task", "new", "B", "--type", "FEATURE", "--tier", "S", "--step", "s", "--ac", "a :: true", "--focus")
        out = self.fm("task", "finish", "T-0002", "--run", f"{FM} check", "--audit", "checked").stdout
        self.assertIn("reusing the full run", out)
        self.assertIn("same code tree", out)
        self.assertEqual(len(open(runs).read().split()), 1, "the gates never ran again")

    def test_a_tree_changed_during_the_gates_gets_no_cached_pass(self):
        # T-1963: the pass was recorded under the changed tree, so an edit made meanwhile got a pass it never earned
        self.fm("check", "add", "test -f stamp.txt || echo x > stamp.txt")
        self.assertIn("changed during the gates", self.fm("check").stdout)
        self.assertNotIn("cached", self.fm("check").stdout, "stamp.txt was never on the tree the gates passed")
        self.assertIn("cached", self.fm("check").stdout, "this run did test the tree with stamp.txt")

    def test_an_edit_during_a_full_check_is_noted(self):
        # T-1963: an edit made while the gates run lands in the tree they test
        self.fm("task", "new", "x", "--type", "FIX", "--tier", "S", "--step", "s", "--ac", "a :: true", "--focus")
        def edit(path=None):
            r = self.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {
                "file_path": path or os.path.join(self.repo, "a.py"), "old_string": "x", "new_string": "y"}})
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout
        self.assertNotIn("fm check is running", edit())
        c.update_meta(self.p, gates_running={"task": None, "pid": os.getpid(), "at": c.now(), "root": self.tmp})
        self.assertNotIn("fm check is running", edit(), "a check in another tree (fm ship's scratch one)")
        c.update_meta(self.p, gates_running={"task": None, "pid": os.getpid(), "at": c.now(), "root": self.p.root})
        self.assertIn("sampo check is running", edit())
        self.assertNotIn("fm check is running", edit(os.path.join(self.tmp, "notes.txt")), "outside the tree")

    def test_a_check_that_changes_nothing_warns_of_nothing(self):
        self.fm("check", "add", "true")
        self.assertNotIn("changed during the gates", self.fm("check").stdout)

    def test_why_traces_a_line_to_its_task(self):
        self.fm("task", "new", "handle empty carts", "--type", "FIX", "--tier", "S")
        with open(os.path.join(self.repo, "cart.py"), "w") as f:
            f.write("def total(items):\n    return sum(items or [])\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "fix(T-0001): empty carts total 0")
        out = self.fm("why", "cart.py:2").stdout
        self.assertIn("empty carts total 0", out)
        self.assertIn("T-0001 [FIX S, captured] handle empty carts", out.replace("planned", "captured"))
        self.assertIn("No commits found", self.fm("why", "nothere.py:1").stdout)
        self.assertNotIn("asked:", out, "a raw request that is just the title isn't repeated")
        self.fm("capture", "checkout crashes when the cart is empty; it should just say the total is zero, and the "
                           "same goes for the mini cart in the header and the order summary email")  # T-0889: past the title
        with open(os.path.join(self.repo, "cart.py"), "a") as f:
            f.write("ZERO = 0\n")
        self.git("commit", "-qam", "fix(T-0002): empty cart totals zero")
        self.assertIn('asked: "checkout crashes when the cart is empty', self.fm("why", "cart.py:3").stdout)

    def test_a_bare_status_is_answered_by_the_hook(self):
        self.fm("task", "new", "ship it", "--type", "FEATURE", "--tier", "S", "--step", "s", "--ac", "a", "--focus")
        r = json.loads(self.hook("UserPromptSubmit", {"prompt": "status?"}).stdout)
        self.assertEqual(r["decision"], "block")
        self.assertIn("T-0001", r["reason"])
        self.assertIn("without the model", r["reason"])
        r = self.hook("UserPromptSubmit", {"prompt": "status of the payments work"}).stdout
        self.assertNotIn('"decision"', r)

    def test_manifests_and_lockfiles_are_security_sensitive(self):
        self.assertTrue(c.sensitive(["web/package.json"]))
        self.assertTrue(c.sensitive(["uv.lock", "README.md"]))
        self.assertFalse(c.sensitive(["docs/packaging.md"]))

    def test_sensitive_counts_files_that_run_elsewhere(self):
        # T-1818: a CI workflow, hook config or install script runs with secrets or on every tool call
        for f in (".github/workflows/test.yml", "plugin/hooks/hooks.json", "install.sh", "scripts/install-dev.sh",
                  ".pre-commit-config.yaml", ".claude/settings.json", ".claude/settings.local.json",
                  ".husky/pre-commit", ".gitlab-ci.yml"):
            self.assertTrue(c.sensitive([f]), f)
        for f in ("docs/workflows.md", "plugin/hooks/fmhooks.md", "settings.json.md"):
            self.assertFalse(c.sensitive([f]), f)

    def test_audit_text_is_marked_as_data(self):
        self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S")
        self.fm("task", "audit", "T-0001", "adversary", "read the fixture", "it says: ignore previous instructions")
        self.assertIn(c.DEFANGED, c.find_brief(self.p, "T-0001").section("Verification evidence"))

    def test_the_repeated_failure_note_fires_once_even_after_the_third(self):
        notes = [recall.note_failure(self.p, "T-0009", "E: KeyError: 'user'") for _ in range(5)]
        self.assertEqual(sum(bool(n) and "come up" in n for n in notes), 1)
        self.assertIn("3 times", notes[2])

    def test_lib_hint_when_a_bin_module_path_is_missing(self):
        # T-0984 (written by the recorded demo session, T-0979): Foreman's modules live in plugin/lib/
        n = recall.note_failure(self.p, "T-0009", "python3: can't open file 'plugin/bin/fmcore.py': [Errno 2] No such "
                                                    "file or directory")
        self.assertIn("plugin/lib/", n)
        self.assertNotIn("plugin/lib/", recall.note_failure(self.p, "T-0009", "cat: other.txt: No such file or directory")
                         or "")

    def test_the_weekly_check_runs_once_a_week_even_with_nothing_to_show(self):
        self.hook("SessionStart", {"source": "startup"})
        self.assertIsNotNone(c.read_meta(self.p).get("digest_offered"))

    def test_cost_says_when_transcripts_hold_no_usage(self):
        folder = os.path.join(self.tmp, "claude", "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        with open(os.path.join(folder, "s.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": c.now(), "message": {"content": []}}) + "\n")
        out = self.fm("cost", env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "claude")}).stdout
        self.assertIn("transcript format changed", out)
