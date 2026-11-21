"""T-1188: fm release VERSION: the release ritual as named steps with a checkpoint after each. (release.py, the thin
wrapper over it, is tested in test_release_py.py.)"""
import json
import os
import re
import subprocess
import sys
import time

from helpers import ForemanTestCase, read_json, read_text, stub_corpus_replay

from sampo import core as c

STUB = '''import json, os, sys, time
verb = sys.argv[1]
once = os.path.join(os.environ.get("STUB_FAIL_ONCE", "/nonexistent"), verb)
if os.path.exists(once):  # this gate fails once, then passes
    os.remove(once)
    sys.exit(1)
if verb == "projects":
    n = int(os.environ.get("STUB_N", "1"))
    print(json.dumps({"projects": [{"project": "app" + (str(i) if n > 1 else ""), "root": os.environ["STUB_ROOT"],
                                    "exists": True} for i in range(n)]}))
elif verb == "sentinel":
    log = os.environ.get("STUB_LOG")
    if log:
        with open(log, "a") as f:
            f.write("start\\n")
        time.sleep(0.5)
        with open(log, "a") as f:
            f.write("end\\n")
    time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
    print("1 proof fails now")
    sys.exit(1 if os.environ.get("STUB_FAIL") else 0)
'''


def put(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def sh(root, *a):
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=True).stdout


def stub_env(tmp, root):
    """A stub fm for the gates (check, release-check) and the canary (projects, sentinel): every gate passes."""
    path = os.path.join(tmp, "fmstub.py")
    put(tmp, "fmstub.py", STUB)
    return {"FOREMAN_RELEASE_FM": path, "STUB_ROOT": root}


class ReleaseCase(ForemanTestCase):
    replayed = True  # T-1401: a fresh green guard corpus replay on record, so --push isn't refused
    UI = "mods/sampo-ui"  # T-5059: the mod's folder; a checkout from before the move holds mods/foreman-ui (ReleaseOldLayout)

    def setUp(self):
        super().setUp()
        put(self.repo, "plugin/.claude-plugin/plugin.json", '{\n  "name": "foreman",\n  "version": "1.0.0"\n}\n')
        put(self.repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n- a thing\n\n## 1.0.0 — 2026-01-01\n- first\n")
        put(self.repo, self.UI + "/.claude-plugin/plugin.json", '{\n  "name": "sampo-ui",\n  "version": "0.1.0"\n}\n')
        put(self.repo, self.UI + "/hooks/a.tsx", "a\n")
        put(self.repo, "plugins/foreman-design/.claude-plugin/plugin.json",
            '{\n  "name": "foreman-design",\n  "version": "1.0.0"\n}\n')
        self.market = {"name": "foreman", "metadata": {"version": "1.0.0"}, "plugins": [
            {"name": "foreman", "version": "1.0.0"}, {"name": "foreman-ui", "version": "0.1.0"},
            {"name": "foreman-design", "version": "1.0.0"}]}
        put(self.repo, ".claude-plugin/marketplace.json", json.dumps(self.market))
        self.fm("init")
        if self.replayed:
            stub_corpus_replay(self.repo)
        sh(self.repo, "add", ".")
        sh(self.repo, "commit", "-qm", "Foreman 1.0.0")
        self.env = stub_env(self.tmp, self.repo)

    def rel(self, *a, check=True, env=True):
        return self.fm("release", *a, check=check, env=self.env if env else None)

    def tid(self, grant=False):
        t = json.loads(self.fm("task", "new", "ship", "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "step", t, "add", "ship it")
        self.fm("task", "ac", t, "add", "shipped", "--verify", "true")
        self.fm("focus", t)
        if grant:
            self.grant(t)
        return t

    def grant(self, t):
        p = c.find_project(self.repo)
        b = c.find_brief(p, t)
        b.meta["allow"] = ["publish"]
        c.save_brief(p, b)

    def origin(self):
        origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        sh(self.repo, "remote", "add", "origin", origin)
        sh(self.repo, "push", "-q", "origin", "main")
        sh(self.repo, "checkout", "-q", "-b", "work")
        return origin

    def subject(self):
        return sh(self.repo, "log", "-1", "--format=%s").strip()


class ReleaseOldLayout(ReleaseCase):
    """T-5059: a checkout from before the move still holds mods/foreman-ui as the real folder: the UI change is seen
    and the bump and the commit write through that name."""
    UI = "mods/foreman-ui"

    def test_a_ui_change_in_the_old_folder_needs_a_ui_version_and_is_bumped_there(self):
        put(self.repo, self.UI + "/hooks/a.tsx", "b\n")
        sh(self.repo, "commit", "-qam", "ui change")
        r = self.rel("9.9.9", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--ui", r.stderr + r.stdout)
        self.rel("9.9.9", "--resume", "--ui", "0.2.0")
        self.assertEqual(read_json(os.path.join(self.repo, self.UI, ".claude-plugin/plugin.json"))["version"], "0.2.0")
        self.assertEqual(self.subject(), "Foreman 9.9.9")
        self.assertEqual(sh(self.repo, "status", "--porcelain"), "")


class Release(ReleaseCase):
    def test_dry_run_lists_every_step_and_changes_nothing(self):
        out = self.rel("9.9.9", "--dry-run").stdout
        for name in ("tree", "check", "bump", "changelog", "commit", "release-check", "canary", "push", "round"):
            self.assertIn(name, out)
        # T-1199: the canary is never fatal, so it never holds the push: it runs last
        self.assertLess(out.index("push:"), out.index("canary:"))
        self.assertLess(out.index("round:"), out.index("canary:"))
        self.assertEqual(sh(self.repo, "status", "--porcelain"), "")
        self.assertEqual(read_json(os.path.join(self.repo, "plugin/.claude-plugin/plugin.json"))["version"], "1.0.0")

    def test_a_run_stopped_after_step_two_resumes_at_step_three(self):
        mk = os.path.join(self.repo, ".claude-plugin/marketplace.json")
        put(self.repo, ".claude-plugin/marketplace.json", "{broken")  # step 3 can't bump it
        sh(self.repo, "commit", "-qam", "break it")
        r = self.rel("9.9.9", "--no-check", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bump", r.stderr + r.stdout)
        self.assertEqual(self.subject(), "break it")
        put(self.repo, ".claude-plugin/marketplace.json", json.dumps(self.market))
        sh(self.repo, "commit", "-qam", "fix it")
        self.assertNotEqual(self.rel("8.0.0", "--resume", check=False).returncode, 0, "another version")
        out = self.rel("9.9.9", "--resume").stdout
        self.assertIn("resuming at step 3", out)
        self.assertEqual(self.subject(), "Foreman 9.9.9")
        self.assertEqual(read_json(mk)["plugins"][0]["version"], "9.9.9")
        self.assertEqual(read_json(mk)["plugins"][1]["version"], "0.1.0", "the UI didn't change")
        design = os.path.join(self.repo, "plugins/foreman-design/.claude-plugin/plugin.json")  # T-1652: its skills
        self.assertEqual((read_json(mk)["plugins"][2]["version"], read_json(design)["version"]), ("9.9.9", "9.9.9"))
        self.assertEqual(sh(self.repo, "status", "--porcelain", "--", "plugins"), "", "committed with the release")
        self.assertIn("## 9.9.9 — ", read_text(os.path.join(self.repo, "CHANGELOG.md")))
        self.assertIn("round-1", sh(self.repo, "tag", "--list"))
        self.assertIn("already released", self.rel("9.9.9", "--resume").stdout)

    def test_a_resume_keeps_the_first_runs_flags(self):
        put(self.repo, self.UI + "/hooks/a.tsx", "b\n")
        sh(self.repo, "commit", "-qam", "ui change")
        r = self.rel("9.9.9", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--ui", r.stderr + r.stdout)
        self.rel("9.9.9", "--resume", "--ui", "0.2.0")
        mk = read_json(os.path.join(self.repo, ".claude-plugin/marketplace.json"))
        self.assertEqual(mk["plugins"][1]["version"], "0.2.0")
        self.assertEqual(read_json(os.path.join(self.repo, self.UI, ".claude-plugin/plugin.json"))["version"], "0.2.0")

    def test_a_ui_version_that_isnt_semver_is_refused(self):
        r = self.rel("9.9.9", "--ui", "0.2", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(sh(self.repo, "status", "--porcelain"), "")

    def test_the_first_runs_ui_is_reused_on_resume(self):
        self.rel("9.9.9", "--ui", "0.2.0", "--no-check")
        state = read_json(os.path.join(c.find_project(self.repo).dir, "release.json"))
        self.assertEqual((state["ui"], state["no_check"]), ("0.2.0", True))
        self.assertEqual(sorted(state["skipped"]), ["canary", "check", "release-check"])
        self.assertNotIn("check", state["done"])

    def test_push_without_the_publish_grant_refuses_before_any_change(self):
        r = self.rel("9.9.9", "--push", check=False)
        self.assertNotEqual(r.returncode, 0)
        out = r.stderr + r.stdout
        self.assertIn("fm ask ID publish", out)  # T-1322: both ways, the ask first
        self.assertIn("fm task set ID --allow publish", out)
        self.assertLess(out.index("fm ask ID publish"), out.index("--allow publish"))
        self.assertEqual(sh(self.repo, "status", "--porcelain"), "")
        self.assertEqual(self.subject(), "Foreman 1.0.0")

    def test_skipped_gates_cannot_push(self):
        self.origin()
        self.tid(grant=True)
        r = self.rel("9.9.9", "--push", "--no-check", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("skipped", r.stderr + r.stdout)
        self.assertEqual(self.subject(), "Foreman 1.0.0")

    def test_push_with_the_grant_merges_into_origin_main_and_spends_it(self):
        origin = self.origin()
        tid = self.tid(grant=True)
        self.rel("9.9.9", "--push")
        self.assertEqual(sh(origin, "log", "-1", "--format=%s", "main").strip(), "Foreman 9.9.9")
        self.assertEqual(sh(origin, "log", "-1", "--format=%p", "main").count(" "), 1, "a --no-ff merge")
        p = c.find_project(self.repo)
        self.assertNotIn("publish", c.find_brief(p, tid).meta.get("allow") or [])
        self.assertIn("grant_spent", read_text(os.path.join(p.dir, "ledger.jsonl")))
        self.assertEqual(sh(self.repo, "branch", "--show-current").strip(), "work")

    def fail_once(self, verb):
        d = os.path.join(self.tmp, "failonce")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, verb), "w").close()
        self.env["STUB_FAIL_ONCE"] = d

    def test_a_later_push_merges_the_release_commit_not_later_work(self):
        origin = self.origin()
        self.rel("9.9.9")
        put(self.repo, "later.txt", "x\n")
        sh(self.repo, "add", "later.txt")
        sh(self.repo, "commit", "-qm", "later, never gated")
        self.tid(grant=True)
        self.rel("9.9.9", "--push")
        self.assertNotIn("later, never gated", sh(origin, "log", "--format=%s", "main"))
        self.assertEqual(sh(origin, "log", "-1", "--format=%s", "main").strip(), "Foreman 9.9.9")

    def test_a_failed_gate_resumes_at_that_gate_and_a_plain_rerun_resumes_too(self):
        self.fail_once("check")
        self.assertNotEqual(self.rel("9.9.9", check=False).returncode, 0)
        self.assertEqual(self.subject(), "Foreman 1.0.0")
        self.assertIn("resuming at step 2", self.rel("9.9.9", "--resume").stdout)
        self.assertEqual(self.subject(), "Foreman 9.9.9")
        self.rel("--abandon")
        self.fail_once("release-check")  # stopped after the bump and commit
        self.assertNotEqual(self.rel("9.9.10", check=False).returncode, 0)
        self.assertIn("resuming at step 6", self.rel("9.9.10").stdout)  # not "9.9.10 isn't newer than 9.9.10"

    def test_stubbed_gates_never_push_to_a_real_remote(self):
        self.origin()
        sh(self.repo, "remote", "set-url", "origin", "https://example.invalid/foreman.git")
        self.tid(grant=True)
        r = self.rel("9.9.9", "--push", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FOREMAN_RELEASE_FM", r.stderr + r.stdout)
        self.assertEqual(self.subject(), "Foreman 1.0.0")

    def test_a_remote_that_looks_like_an_option_is_refused(self):
        self.tid(grant=True)
        r = self.rel("9.9.9", "--push", "--remote=--upload-pack=touch x", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.subject(), "Foreman 1.0.0")

    def test_abandon_with_dry_run_keeps_the_state_and_a_corrupt_state_can_be_abandoned(self):
        self.fail_once("check")
        self.rel("9.9.9", check=False)
        state = os.path.join(c.find_project(self.repo).dir, "release.json")
        self.rel("--abandon", "--dry-run")
        self.assertTrue(os.path.isfile(state))
        put(os.path.dirname(state), "release.json", "{torn")
        self.assertIn("abandon", self.rel("9.9.9", check=False).stderr)  # a clear error, not a traceback
        self.rel("--abandon")
        self.assertFalse(os.path.isfile(state))

    def test_the_commit_takes_only_the_release_files(self):
        self.fail_once("check")
        self.rel("9.9.9", check=False)  # stopped at step 2: the tree step, done, isn't rerun on resume
        put(self.repo, "staged.txt", "x\n")
        sh(self.repo, "add", "staged.txt")
        self.rel("9.9.9", "--resume")
        self.assertEqual(self.subject(), "Foreman 9.9.9")
        self.assertNotIn("staged.txt", sh(self.repo, "show", "--name-only", "--format=", "HEAD"))

    def test_a_release_without_push_can_be_pushed_later_and_only_pushes(self):
        origin = self.origin()
        self.rel("9.9.9")
        self.assertNotIn("Foreman 9.9.9", sh(origin, "log", "-3", "--format=%s", "main"))
        self.assertIn("already released", self.rel("9.9.9", "--resume").stdout, "no push asked, none left to do")
        self.tid(grant=True)
        out = self.rel("9.9.9", "--push").stdout
        self.assertNotIn("step 3/", out)
        self.assertIn("step 7/9 push", out)
        self.assertEqual(sh(origin, "log", "-1", "--format=%s", "main").strip(), "Foreman 9.9.9")
        self.assertEqual(sh(self.repo, "tag", "--list").split().count("round-1"), 1, "no second round")

    def canary_report(self, v):
        """The detached canary's report once it has finished."""
        path = os.path.join(c.find_project(self.repo).dir, "logs", f"canary-{v}.txt")
        for _ in range(100):
            if os.path.isfile(path) and "canary done" in read_text(path):
                return read_text(path)
            time.sleep(0.2)
        self.fail("the canary never finished")

    def test_the_canary_runs_detached_and_its_failure_lands_in_the_inbox_and_the_notify_hook(self):
        note = os.path.join(self.tmp, "note.txt")
        self.fm("notify", f'echo "$1" > {note}')
        self.env.update(STUB_SLEEP="3", STUB_FAIL="1", FOREMAN_NO_BACKGROUND="")  # detached for real; waited on below
        out = self.rel("9.9.9").stdout  # returns before the 3 s sentinel ends: the status below finds it unfinished
        self.assertIn("canary-9.9.9.txt", out)
        self.assertIn("--canary-status", out)
        self.assertNotIn("canary done", self.rel("--canary-status").stdout)
        report = self.canary_report("9.9.9")
        self.assertIn("app: 1 proof fails now", report)
        titles = [b.title for b in c.load_briefs(c.find_project(self.repo))]
        self.assertTrue(any("canary" in t.lower() and "app" in t for t in titles), titles)
        for _ in range(50):
            if os.path.isfile(note):
                break
            time.sleep(0.2)
        self.assertIn("app", read_text(note))
        self.assertIn("app: 1 proof fails now", self.rel("--canary-status").stdout)

    def test_canary_status_without_a_run_says_so(self):
        self.assertIn("no canary", self.rel("--canary-status").stdout)

    def test_the_canary_runs_for_real_and_is_never_fatal(self):
        self.rel("9.9.9")
        report = self.canary_report("9.9.9")
        self.assertIn("canary: fm sentinel in 1 project", report)
        self.assertIn("app: 1 proof fails now", report)
        put(self.tmp, "gone", "")
        self.env["STUB_ROOT"] = os.path.join(self.tmp, "no-such-dir")  # sentinel can't start there: OSError
        sh(self.repo, "checkout", "-q", "-b", "second")
        put(self.repo, "CHANGELOG.md", read_text(os.path.join(self.repo, "CHANGELOG.md")).replace("## Unreleased", "## Unreleased\n- more", 1))
        sh(self.repo, "commit", "-qam", "more")
        self.rel("9.9.10")
        self.assertIn("couldn't run", self.canary_report("9.9.10"))

    def test_the_canary_runs_sentinels_side_by_side_and_reports_in_project_order(self):
        log = os.path.join(self.tmp, "sentinels.log")
        self.env.update(STUB_N="4", STUB_LOG=log)
        self.rel("9.9.9")
        out = self.canary_report("9.9.9")
        self.assertGreaterEqual(read_text(log).split("end")[0].count("start"), 2, "two sentinels ran at once")
        lines = [ln.strip() for ln in out.splitlines() if ": 1 proof fails now" in ln]
        self.assertEqual([ln.split(":")[0][2:] for ln in lines], ["app0", "app1", "app2", "app3"])

    def test_an_unfinished_release_of_another_version_blocks_a_new_one_until_abandoned(self):
        put(self.repo, "CHANGELOG.md", "# Changelog\n")  # step 4 fails with the bump already written
        sh(self.repo, "commit", "-qam", "break it")
        self.assertNotEqual(self.rel("9.9.9", check=False).returncode, 0)
        r = self.rel("9.9.10", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("9.9.9", r.stderr)
        self.assertIn("--abandon", r.stderr)
        self.rel("--abandon")
        sh(self.repo, "checkout", "--", ".")
        put(self.repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n- a thing\n")
        sh(self.repo, "commit", "-qam", "fix it")
        self.rel("9.9.10")
        self.assertEqual(self.subject(), "Foreman 9.9.10")

    def test_a_plugin_json_version_that_isnt_numeric_is_a_clear_error(self):
        put(self.repo, "plugin/.claude-plugin/plugin.json", '{"name": "foreman", "version": "1.x"}\n')
        sh(self.repo, "commit", "-qam", "odd")
        r = self.rel("9.9.9", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MAJOR.MINOR.PATCH", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_outside_foremans_own_repo_it_takes_the_any_repo_ritual(self):  # T-2390: no longer a refusal
        os.remove(os.path.join(self.repo, "plugin/.claude-plugin/plugin.json"))
        r = self.rel("9.9.9", "--dry-run", check=False)
        self.assertNotIn("Foreman's own repo", r.stderr)
        self.assertIn("no manifest carries a version", r.stderr)  # this stub repo has none of the generic manifests


class AnyRepo(ForemanTestCase):
    """T-2390 (Mission 7, #10): fm release VERSION in a repo that isn't Foreman's: tree, check, bump the one manifest,
    changelog, fm prove --release, commit, local tag, and --push only behind that repo's task's publish grant."""
    replayed = True

    def setUp(self):
        super().setUp()
        self.env = stub_env(self.tmp, self.repo)

    def repo_with(self, files):
        for rel, text in files.items():
            put(self.repo, rel, text)
        put(self.repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n- a thing\n")
        self.fm("init")
        stub_corpus_replay(self.repo)
        sh(self.repo, "add", ".")
        sh(self.repo, "commit", "-qm", "start")

    def rel(self, *a, check=True, env=None):
        return self.fm("release", *a, check=check, env=dict(self.env, **(env or {})))

    def task(self, grant=False):
        out = self.fm("task", "new", "ship", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                      "--focus", "--json").stdout
        t = json.JSONDecoder().raw_decode(out)[0]["id"]  # --focus prints a second object
        if grant:
            p = c.find_project(self.repo)
            b = c.find_brief(p, t)
            b.meta["allow"] = ["publish"]
            c.save_brief(p, b)
        return t

    def test_any_repo_python_release_bumps_tags_and_a_red_proof_stops_before_the_commit(self):
        self.repo_with({"pyproject.toml": '[project]\nname = "x"\nversion = "0.1.0"\n'})
        dry = self.rel("0.2.0", "--dry-run")
        for step in ("bump", "changelog", "prove", "commit", "tag"):
            self.assertIn(step, dry.stdout)
        self.assertIn('version = "0.1.0"', read_text(os.path.join(self.repo, "pyproject.toml")))  # dry: nothing changed
        fail = os.path.join(self.tmp, "failonce")
        os.makedirs(fail)
        open(os.path.join(fail, "prove"), "w").close()
        r = self.rel("0.2.0", check=False, env={"STUB_FAIL_ONCE": fail})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("prove", r.stderr + r.stdout)
        self.assertEqual(sh(self.repo, "log", "-1", "--format=%s").strip(), "start")  # stopped before the commit
        self.rel("0.2.0", "--resume")
        self.assertEqual(sh(self.repo, "log", "-1", "--format=%s").strip(), "Release 0.2.0")
        self.assertIn('version = "0.2.0"', read_text(os.path.join(self.repo, "pyproject.toml")))
        self.assertRegex(read_text(os.path.join(self.repo, "CHANGELOG.md")), r"## Unreleased\n\n## 0\.2\.0 — ")
        self.assertEqual(sh(self.repo, "cat-file", "-t", "v0.2.0").strip(), "tag")  # annotated
        self.assertEqual(sh(self.repo, "status", "--porcelain").strip(), "")

    def test_any_repo_npm_release_and_no_manifest_names_what_it_looked_at(self):
        self.repo_with({"package.json": '{\n  "name": "y",\n  "version": "1.4.0"\n}\n'})
        self.rel("1.5.0")
        self.assertIn('"version": "1.5.0"', read_text(os.path.join(self.repo, "package.json")))
        os.remove(os.path.join(self.repo, "package.json"))
        sh(self.repo, "commit", "-qam", "drop the manifest")
        r = self.rel("1.6.0", check=False)
        self.assertNotEqual(r.returncode, 0)
        for name in ("pyproject.toml", "package.json", "Cargo.toml", "gemspec", "VERSION"):
            self.assertIn(name, r.stderr + r.stdout)

    def test_any_repo_push_needs_this_repos_publish_grant_and_spends_it(self):
        self.repo_with({"pyproject.toml": '[project]\nname = "x"\nversion = "0.1.0"\n'})
        origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
        sh(self.repo, "remote", "add", "origin", origin)
        self.task()
        r = self.rel("0.2.0", "--push")
        self.assertIn("publish grant", r.stdout)  # named for the final report
        self.assertEqual(sh(self.repo, "cat-file", "-t", "v0.2.0").strip(), "tag")  # the local steps ran
        self.assertEqual(subprocess.run(["git", "-C", origin, "tag"], capture_output=True, text=True).stdout, "")
        t = self.task(grant=True)
        self.rel("0.2.0", "--push")
        self.assertIn("v0.2.0", subprocess.run(["git", "-C", origin, "tag"], capture_output=True, text=True).stdout)
        p = c.find_project(self.repo)
        self.assertNotIn("publish", c.find_brief(p, t).meta.get("allow") or [])  # spent

    def test_any_repo_skill_runs_fm_release_for_any_repo(self):
        text = read_text(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "release",
                                      "SKILL.md"))
        any_part = text.split("## Foreman's own repo")[0]
        self.assertIn("sampo release VERSION", any_part)
        self.assertIn("--push", any_part)


class LandAfterShips(ReleaseCase):
    """T-2598: fm ship lands each commit on main as a cherry-pick, so by release time the branch and main share content
    but not history, and the --no-ff merge conflicts; 1.2.51 had to be landed by hand."""

    def shipped(self, origin, files, msg):  # what fm ship does: the same change as a new commit on origin's main
        clone = os.path.join(self.tmp, "clone")
        if not os.path.isdir(clone):
            subprocess.run(["git", "clone", "-q", origin, clone], check=True)
        sh(clone, "pull", "-q", "origin", "main")
        for path, text in files.items():
            put(clone, path, text)
        sh(clone, "add", "-A")
        sh(clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
        sh(clone, "push", "-q", "origin", "HEAD:main")

    def setup_ci(self):
        put(self.repo, ".github/workflows/test.yml", "on:\n  push:\n    paths-ignore:\n      - site/genesis/**\n")
        sh(self.repo, "add", ".")
        sh(self.repo, "commit", "-qm", "ci")
        origin = self.origin()
        put(self.repo, "a.txt", "one\n")
        sh(self.repo, "add", "a.txt")
        sh(self.repo, "commit", "-qm", "T-1: one")
        return origin

    def test_a_main_fed_by_cherry_picks_still_takes_the_release_and_the_branch_follows(self):
        origin = self.setup_ci()
        self.shipped(origin, {"a.txt": "one\n", "site/genesis/genesis.json": '{"tower": 2}\n'}, "T-1: one (shipped)")
        put(self.repo, "a.txt", "two\n")
        sh(self.repo, "add", "a.txt")
        sh(self.repo, "commit", "-qm", "T-2: two")
        self.tid(grant=True)
        r = self.rel("9.9.9", "--push", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sh(origin, "show", "main:a.txt"), "two\n")
        self.assertEqual(sh(origin, "show", "main:site/genesis/genesis.json"), '{"tower": 2}\n', "main's data kept")
        self.assertEqual(sh(origin, "log", "-1", "--format=%s", "main").strip(), "Foreman 9.9.9")
        self.assertEqual(sh(self.repo, "rev-parse", "HEAD").strip(), sh(origin, "rev-parse", "main").strip(),
                         "the branch fast-forwards to the landed release")

    def test_a_change_main_has_and_the_branch_never_had_still_refuses(self):
        origin = self.setup_ci()
        self.shipped(origin, {"a.txt": "three\n"}, "someone else's change")
        before = sh(origin, "rev-parse", "main").strip()
        self.tid(grant=True)
        r = self.rel("9.9.9", "--push", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a.txt", r.stdout + r.stderr)
        self.assertEqual(sh(origin, "rev-parse", "main").strip(), before)

    def test_an_abandoned_release_already_committed_continues(self):
        origin = self.origin()
        self.rel("9.9.9")
        self.rel("--abandon")
        self.tid(grant=True)
        r = self.rel("9.9.9", "--push", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sh(origin, "log", "-1", "--format=%s", "main").strip(), "Foreman 9.9.9")


class PushHold(ForemanTestCase):
    """T-4805: fm release --push merges the release onto origin main; a commit made straight on the trunk in what it
    merges is judged by the self-change gate against its parent first, as fm ship's is."""
    CASES = "plugin/tests/fixtures/guard_cases.jsonl"
    ALLOW = "plugin/tests/fixtures/guard_allow.jsonl"

    def setUp(self):
        super().setUp()
        self.fm("init")
        put(self.repo, "plugin/lib/fmguard.py", "x = 1\n")
        put(self.repo, self.CASES, '{"id": "c1", "cmd": "rm -rf ~", "cwd": ".", "should": "block"}\n')
        put(self.repo, self.ALLOW, '{"id": "a1", "cmd": "ls", "cwd": ".", "should": "allow"}\n')
        put(self.repo, "CHANGELOG.md", "# Changelog\n")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-qm", "base")
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        sh(self.repo, "remote", "add", "origin", self.origin)
        sh(self.repo, "push", "-q", "origin", "HEAD:main")
        sh(self.repo, "checkout", "-q", "-b", "work")
        self.p = c.find_project(self.repo)
        self.tid = json.loads(self.fm("task", "new", "ship", "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "step", self.tid, "add", "ship it")
        self.fm("task", "ac", self.tid, "add", "shipped", "--verify", "true")
        self.fm("focus", self.tid)
        b = c.find_brief(self.p, self.tid)
        b.meta["allow"] = ["publish"]
        c.save_brief(self.p, b)

    def commit(self, files, msg):
        for rel, text in files.items():
            put(self.repo, rel, text)
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-qm", msg)
        return sh(self.repo, "rev-parse", "HEAD").strip()

    def push(self):
        """(refusal text or '', guard replays): release._push in this process, the judge faked as test_lanes does."""
        from unittest import mock

        from sampo import cli, release, selfgate
        seen = []

        def verdicts(tree, cmds):
            seen.append(tree)
            return ["allow" if cmd == "ls" or (len(seen) % 2 == 0 and cmd.startswith("rm")) else "destructive"
                    for cmd, _ in cmds]
        for name in ("selfgate-base.json", "selfgate-base-push.json"):
            if os.path.exists(os.path.join(self.p.dir, name)):
                os.remove(os.path.join(self.p.dir, name))
        ctx = {"root": self.repo, "version": "9.9.9", "p": self.p, "remote": "origin",
               "state": {"sha": sh(self.repo, "rev-parse", "HEAD").strip()}}
        with mock.patch.object(selfgate, "_verdicts", verdicts), \
                mock.patch.object(selfgate, "_bench", lambda t: ({"Stop": {"p95": 100.0, "budget": 500}}, {"SessionStart": 2000})), \
                mock.patch.object(selfgate, "_corpus", lambda: []):
            try:
                release._push(ctx)
                return "", len(seen)
            except cli.UsageError as e:
                return str(e), len(seen)

    def test_direct_guard_weakening_in_what_release_merges_is_held_before_the_push(self):
        sha = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "loosen the guard")
        self.commit({"CHANGELOG.md": "# Changelog\n\n## 9.9.9\n"}, "Foreman 9.9.9")
        before = sh(self.origin, "rev-parse", "main").strip()
        why, _ = self.push()
        self.assertIn(sha[:8], why)
        self.assertIn("rm -rf ~", why)
        self.assertIn("nothing was pushed", why)
        self.assertEqual(sh(self.origin, "rev-parse", "main").strip(), before, "origin main did not move")
        self.assertIn("publish", c.find_brief(self.p, self.tid).meta.get("allow") or [], "the grant is kept")

    def test_a_release_whose_commits_a_lane_landed_or_touch_no_judged_file_runs_no_gate(self):
        base = sh(self.repo, "rev-parse", "HEAD").strip()
        lane = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "lane work")
        c.log_event(self.p, "lane_landed", task=self.tid, data={"base": base, "tip": lane, "tasks": [self.tid]})
        self.commit({"CHANGELOG.md": "# Changelog\n\n## 9.9.9\n"}, "Foreman 9.9.9")
        why, judged = self.push()
        self.assertEqual((why, judged), ("", 0))
        self.assertEqual(sh(self.origin, "log", "-1", "--format=%s", "main").strip(), "Foreman 9.9.9")

    def test_direct_guard_weakening_by_moving_the_guard_or_its_cases_away_is_held(self):
        """T-4805 review: a rename lists only the new path unless git is told --no-renames, so a moved guard judged nothing."""
        before = sh(self.origin, "rev-parse", "main").strip()
        for src, dst in (("plugin/lib/fmguard.py", "docs/old_guard.py"), (self.CASES, "docs/old_cases.jsonl")):
            os.makedirs(os.path.join(self.repo, "docs"), exist_ok=True)
            sh(self.repo, "mv", src, dst)
            sh(self.repo, "commit", "-qm", f"move {src} away")
            sha = sh(self.repo, "rev-parse", "HEAD").strip()
            self.commit({"CHANGELOG.md": f"# Changelog\n\n## {src}\n"}, "Foreman 9.9.9")
            why, _ = self.push()
            self.assertIn(sha[:8], why, src)
            self.assertEqual(sh(self.origin, "rev-parse", "main").strip(), before, "origin main did not move")
            sh(self.repo, "reset", "-q", "--hard", "HEAD~2")

    def test_a_listing_that_fails_or_a_commit_with_no_parent_is_a_hold_never_a_pass(self):
        """T-4805 review: git's failure read as an empty file list (skipped), and a root commit had no parent (skipped)."""
        from unittest import mock

        from sampo import cli, release
        b = c.find_brief(self.p, self.tid)
        root_sha = sh(self.repo, "rev-list", "--max-parents=0", "HEAD").strip()
        with self.assertRaises(cli.UsageError) as got:
            release._push_hold(self.p, b, self.repo, [root_sha], "fm ship")
        self.assertIn(root_sha[:8], str(got.exception))
        sha = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "loosen the guard")
        real = c._git

        def failing(root, *args, **kw):
            if "diff" in args:
                return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
            return real(root, *args, **kw)
        with mock.patch.object(c, "_git", failing), self.assertRaises(cli.UsageError) as got:
            release._push_hold(self.p, b, self.repo, [sha], "fm ship")
        self.assertIn(sha[:8], str(got.exception))
        self.assertIn("list", str(got.exception))

    def test_a_commit_a_later_ship_left_out_is_still_judged_but_a_picked_one_is_not(self):
        """T-4805 review: --not SHIPPED skipped the commits `fm ship <later commit>` left out, which the release's merge then
        pushes; only a commit whose patch origin main already has is skipped."""
        left = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "loosen the guard")
        later = self.commit({"README.md": "later\n"}, "a later commit")
        sh(self.repo, "update-ref", "refs/foreman/shipped", later)
        self.commit({"CHANGELOG.md": "# Changelog\n\n## 9.9.9\n"}, "Foreman 9.9.9")
        why, _ = self.push()
        self.assertIn(left[:8], why)

    def test_a_commit_whose_patch_origin_main_already_has_under_another_sha_is_not_judged(self):
        sh(self.repo, "checkout", "-q", "-b", "alt", "origin/main")
        self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "picked")
        sh(self.repo, "push", "-q", "origin", "alt:main")
        sh(self.repo, "checkout", "-q", "work")
        self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "the same patch, another sha")
        self.commit({"CHANGELOG.md": "# Changelog\n\n## 9.9.9\n"}, "Foreman 9.9.9")
        why, judged = self.push()
        self.assertEqual((why, judged), ("", 0))

    def test_lane_merge_records_the_range_from_the_merges_own_first_parent(self):
        """T-4805 review: pre_merge was read before the lock; a direct commit made in that window became the merge's first
        parent and the range pre_merge..merge covered it, so it skipped the judge. The range starts at merge^1."""
        from sampo import lanes
        sh(self.repo, "checkout", "-q", "-b", "lane")
        self.commit({"lane.txt": "lane\n"}, "lane commit")
        sh(self.repo, "checkout", "-q", "work")
        direct = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "loosen the guard")
        sh(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-m", "Merge T-1", "lane")
        merge = sh(self.repo, "rev-parse", "HEAD").strip()
        lanes.log_merged(self.p, self.tid, self.repo, merge)
        ev = c.ledger_events(self.p, ("lane_landed",), since="")
        self.assertEqual([(e["data"]["base"], e["data"]["tip"]) for e in ev], [(direct, merge)])
        self.commit({"CHANGELOG.md": "# Changelog\n\n## 9.9.9\n"}, "Foreman 9.9.9")
        why, _ = self.push()
        self.assertIn(direct[:8], why)
        self.assertNotIn(merge[:8], why)

    def test_a_pr_the_train_merged_is_recorded_from_the_merge_commit_github_made(self):
        """T-4805 review: the PR train ran the holds but logged nothing, so fm ship judged the merged PR again."""
        from sampo import pr
        before = sh(self.repo, "rev-parse", "HEAD").strip()
        sh(self.repo, "checkout", "-q", "-b", "prb")
        head = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "the PR's commit")
        sh(self.repo, "checkout", "-q", "work")
        sh(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-m", "Merge PR", "prb")
        merge = sh(self.repo, "rev-parse", "HEAD").strip()
        sh(self.repo, "push", "-q", "origin", "HEAD:main")
        pr._record_landing(self.p, c.find_brief(self.p, self.tid), "main", head)
        ev = c.ledger_events(self.p, ("lane_landed",), since="")
        self.assertEqual([(e["data"]["base"], e["data"]["tip"]) for e in ev], [(before, merge)])

    def test_every_landing_path_records_what_it_landed(self):
        """T-4805 review: each function that runs land_holds (the lane merge, the train, fm serve, the PR train) and then
        lands calls lanes.log_landed or log_merged, or says `T-4805 exempt:` (a hold with no landing of its own)."""
        import ast
        import glob
        lib = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "sampo")
        found = {}
        for path in sorted(glob.glob(os.path.join(lib, "*.py"))):
            with open(path, encoding="utf-8") as f:
                src = f.read()
            for fn in ast.walk(ast.parse(src)):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name != "land_holds":
                    text = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
                    if re.search(r"\bland_holds\(", text):
                        found[f"{os.path.basename(path)}:{fn.name}"] = bool(re.search(r"\blog_(landed|merged)\(|\b_record_landing\(|T-4805 exempt:", text))
        for must in ("lanes.py:merge", "serve.py", "pr.py"):
            self.assertTrue([k for k in found if k.startswith(must)], f"the scan no longer sees {must}: {sorted(found)}")
        self.assertEqual([k for k, ok in found.items() if not ok], [], "a landing that records nothing and has no exemption")

    def test_lane_landing_writes_the_lane_landed_record_the_hold_reads(self):
        """T-4805: lanes.log_landed writes one event for a moved trunk (none for an unmoved one), and both landing paths
        (`sampo lane merge` and the merge train) call it, so the hold never judges what a lane landed."""
        from sampo import lanes
        base = sh(self.repo, "rev-parse", "HEAD").strip()
        tip = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "lane work")
        lanes.log_landed(self.p, "T-0001,T-0002", base, base)
        lanes.log_landed(self.p, "T-0001,T-0002", base, tip)
        ev = c.ledger_events(self.p, ("lane_landed",), since="")
        self.assertEqual([(e["data"]["base"], e["data"]["tip"], e["data"]["tasks"]) for e in ev],
                         [(base, tip, ["T-0001", "T-0002"])])
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "sampo")
        for name in ("lanes.py", "train.py"):
            with open(os.path.join(here, name), encoding="utf-8") as f:
                self.assertIn("log_landed(p, ", f.read(), f"{name} no longer records its landing")

    def test_every_push_path_is_held_or_exempt(self):
        """T-4805: each function that pushes a ref to a remote's main (or merges a pull request) calls the push hold or
        land_holds, or says `T-4805 exempt:` and why. The sites are found in the source, so a new one can't pass unseen."""
        import ast
        import glob
        lib = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "sampo")
        site = re.compile(r'"push"[^\n]*(?:refs/heads/|\bmain\b|--force)|\b_push_main\(|"pr", "merge"')
        held = re.compile(r"\b_push_hold\(|\bland_holds\(|T-4805 exempt:")
        found = {}
        for path in sorted(glob.glob(os.path.join(lib, "*.py"))):
            with open(path, encoding="utf-8") as f:
                src = f.read()
            for node in ast.parse(src).body:
                if isinstance(node, ast.ClassDef):
                    funcs = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs = [node]
                else:
                    continue
                for fn in funcs:
                    text = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
                    if site.search(text):
                        found[f"{os.path.basename(path)}:{fn.name}"] = bool(held.search(text))
        for must in ("release.py:ship", "release.py:_push", "release.py:_push_main", "rune.py", "pr.py:train"):
            self.assertTrue([k for k in found if k.startswith(must)], f"the scan no longer sees {must}: {sorted(found)}")
        self.assertEqual([k for k, ok in found.items() if not ok], [], "a push path with no hold and no T-4805 exemption")
