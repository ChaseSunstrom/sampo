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


class PushHoldBase(ForemanTestCase):
    """T-4805: the repo, origin and task a push hold is judged in; no tests of its own (PushHold and PushHoldTiming)."""
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

    def hold(self, shas, gate=None):
        """release._push_hold over these commits with the guard judge faked to agree (a plain pass); (the commits the
        gate was called for, once each in call order, the refusal text or '')."""
        from unittest import mock

        from sampo import cli, release, selfgate
        seen = []
        real = gate or selfgate.gate

        def counted(p, b, main, ref, files, *a, **kw):
            seen.append(ref)
            return real(p, b, main, ref, files, *a, **kw)
        b = c.find_brief(self.p, self.tid)
        with mock.patch.object(selfgate, "_verdicts", lambda tree, cmds: ["allow" if cmd == "ls" else "destructive" for cmd, _ in cmds]), \
                mock.patch.object(selfgate, "_bench", lambda t: ({"Stop": {"p95": 100.0, "budget": 500}}, {"SessionStart": 2000})), \
                mock.patch.object(selfgate, "_corpus", lambda: []), mock.patch.object(selfgate, "gate", counted):
            try:
                release._push_hold(self.p, b, self.repo, shas, "fm ship")
                return list(dict.fromkeys(seen)), ""  # T-6048: the range's timing gate calls the last commit again
            except cli.UsageError as e:
                return list(dict.fromkeys(seen)), str(e)


class PushHoldTiming(PushHoldBase):
    """T-6048: the push hold judges each direct commit without the hook timing bench and times their range once on the last
    commit's tree; a range that holds is timed commit by commit to name the one that slowed a hook."""
    SLOW = "hook Stop p95 100 -> 900 ms (+800%)"

    def timed(self, shas, slow_marker="SLOW", due=True):
        """(the benches run, why, the self_change_passed shas in the ledger at the range's bench): release._push_hold over
        shas with _paired counted; a tree holding slow_marker the base lacks is the slow one; due False makes the saved
        hook map say no changed file is one a hook loads."""
        from unittest import mock

        from sampo import selfgate
        runs = []

        def paired(base, new, cache=None, excused=None):
            def has(tree):
                with open(os.path.join(tree, "lib", "fmguard.py"), encoding="utf-8") as f:
                    return slow_marker in f.read()
            runs.append(len([e for e in c.ledger_events(self.p, ("self_change_passed",))]))
            return ({"Stop": self.SLOW} if has(new) and not has(base) else {}), {}, {}
        patches = [mock.patch.object(selfgate, "_paired", paired)]
        if not due:
            patches.append(mock.patch.object(selfgate, "cached_hook_map", lambda p, main, base: {"Stop": ["plugin/lib/sampo/core.py"]}))
        for pt in patches:
            pt.start()
        try:
            seen, why = self.hold(shas)
        finally:
            for pt in patches:
                pt.stop()
        return runs, why

    def passed(self):
        return sorted(e["data"]["sha"] for e in c.ledger_events(self.p, ("self_change_passed",)))

    def test_n_direct_commits_run_one_timing_bench_not_n(self):
        shas = [self.commit({"plugin/lib/fmguard.py": f"x = {i + 2}\n"}, f"commit {i}") for i in range(4)]
        runs, why = self.timed(shas)
        self.assertEqual((len(runs), why), (1, ""), "one bench for the four commits")
        self.assertEqual(runs, [0], "no commit's pass was recorded before the range's timing passed")
        self.assertEqual(self.passed(), sorted(shas), "each is recorded after it")

    def test_a_slower_hook_in_one_commit_holds_the_push_and_the_fallback_names_it(self):
        a = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "fast one")
        slow = self.commit({"plugin/lib/fmguard.py": "x = 3  # SLOW\n"}, "slows a hook")
        z = self.commit({"plugin/lib/other.py": "y = 1\n"}, "fast two")
        runs, why = self.timed([a, slow, z])
        self.assertEqual(len(runs), 1 + 3, "the range's bench, then each commit's own")
        self.assertEqual(runs[0], 0, "nothing recorded before the range's timing")
        self.assertIn(slow[:8], why)
        self.assertNotIn(a[:8], why)
        self.assertNotIn(z[:8], why)
        self.assertIn("hook Stop", why)
        self.assertEqual(self.passed(), sorted([a, z]), "the slow one is never recorded as passed")

    def test_a_slower_hook_in_the_first_of_several_is_still_held(self):
        slow = self.commit({"plugin/lib/fmguard.py": "x = 3  # SLOW\n"}, "slows a hook")
        later = [self.commit({"plugin/lib/other.py": f"y = {i}\n"}, f"later {i}") for i in range(3)]
        runs, why = self.timed([slow] + later)
        self.assertIn(slow[:8], why)
        self.assertEqual(self.passed(), sorted(later))

    def test_a_range_no_hook_loads_runs_no_bench_and_records_the_passes(self):
        shas = [self.commit({"plugin/lib/sampo/unused.py": f"y = {i}\n"}, f"commit {i}") for i in range(3)]
        runs, why = self.timed(shas, due=False)
        self.assertEqual((runs, why), ([], ""))
        self.assertEqual(self.passed(), sorted(shas))

    def test_a_range_of_docs_and_tests_only_changes_runs_no_bench(self):
        """Only a judged file is a direct commit's concern; nothing here is benched or judged."""
        shas = [self.commit({"docs/note.md": f"{i}\n", "plugin/tests/t.py": f"{i}\n"}, f"commit {i}") for i in range(3)]
        runs, why = self.timed(shas)
        self.assertEqual((runs, why, self.passed()), ([], "", []))

    def test_a_standing_yes_that_passes_the_timing_rows_is_recorded_as_standing(self):
        """The range timing passed only through the standing yes: each commit's record is revocable, not a plain pass."""
        from sampo import selfgate
        c.update_meta(self.p, standing={"core": {"at": c.now(), "task": self.tid, "why": "test"}})
        shas = [self.commit({"plugin/lib/fmguard.py": "x = 3  # SLOW\n"}, "slows a hook"),
                self.commit({"plugin/lib/other.py": "y = 1\n"}, "fast")]
        runs, why = self.timed(shas)
        self.assertEqual(why, "")
        keys = {e["data"]["sha"]: e["data"]["key"] for e in c.ledger_events(self.p, ("self_change_passed",))}
        self.assertEqual(set(keys), set(shas))
        self.assertTrue(all(k == selfgate.gate_key() + ":standing" for k in keys.values()), keys)

    def test_commits_that_are_not_one_chain_are_timed_each(self):
        """The range needs a last commit that holds every other; two branches are timed commit by commit, as before."""
        base = sh(self.repo, "rev-parse", "HEAD").strip()
        left = self.commit({"plugin/lib/other.py": "y = 1\n"}, "left")
        sh(self.repo, "checkout", "-q", "-b", "side", base)
        right = self.commit({"plugin/lib/third.py": "z = 1\n"}, "right")
        sh(self.repo, "checkout", "-q", "work")
        runs, why = self.timed([left, right])
        self.assertEqual((len(runs), why), (2, ""))
        self.assertEqual(self.passed(), sorted([left, right]))

    def test_one_direct_commit_is_judged_and_timed_once_as_before(self):
        sha = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "one")
        runs, why = self.timed([sha])
        self.assertEqual((len(runs), why, self.passed()), (1, "", [sha]))


class PushHold(PushHoldBase):
    """T-4805: fm release --push merges the release onto origin main; a commit made straight on the trunk in what it
    merges is judged by the self-change gate against its parent first, as fm ship's is."""

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

    def test_a_commit_the_gate_passed_is_not_judged_again_until_the_gate_changes(self):
        """T-6031: every ship judged the same direct commits again (66 minutes of hook bench): a pass is a self_change_passed
        record keyed by the sha and the gate's inputs, and a later hold skips it."""
        from unittest import mock

        from sampo import selfgate
        a = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "one")
        z = self.commit({"plugin/lib/fmguard.py": "x = 3\n"}, "two")
        seen, why = self.hold([a, z])
        self.assertEqual((sorted(seen), why), (sorted([a, z]), ""))
        done = [e["data"] for e in c.ledger_events(self.p, ("self_change_passed",))]
        self.assertEqual(sorted(d["sha"] for d in done), sorted([a, z]))
        self.assertEqual({d["key"] for d in done}, {selfgate.gate_key()})
        seen, why = self.hold([a, z])
        self.assertEqual((seen, why), ([], ""), "the second hold judged nothing")
        ran = [e["data"] for e in c.ledger_events(self.p, ("push_hold_judged",))]
        self.assertEqual([(d["judged"], d["skipped"]) for d in ran], [(2, 0), (0, 2)])
        self.assertTrue(all(isinstance(d["seconds"], (int, float)) for d in ran))
        with mock.patch.object(selfgate, "gate_key", lambda: "another gate"):
            seen, why = self.hold([a, z])
        self.assertEqual((sorted(seen), why), (sorted([a, z]), ""), "a changed gate judges them again")

    def test_a_refused_commit_is_judged_again_by_every_hold(self):
        """T-6031: only a pass is remembered; a refusal is held each time until the user's yes."""
        from unittest import mock

        from sampo import cli, release, selfgate
        sha = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "loosen the guard")

        def loose(tree, cmds):  # the branch's tree allows what the base's blocks (the base half may come from its cache)
            loosened = "x = 2" in read_text(os.path.join(tree, "lib", "fmguard.py"))
            return ["allow" if cmd == "ls" or loosened else "destructive" for cmd, _ in cmds]
        for _ in range(2):
            with mock.patch.object(selfgate, "_verdicts", loose), \
                    mock.patch.object(selfgate, "_bench", lambda t: ({"Stop": {"p95": 100.0, "budget": 500}}, {})), \
                    mock.patch.object(selfgate, "_corpus", lambda: []):
                with self.assertRaises(cli.UsageError) as got:
                    release._push_hold(self.p, c.find_brief(self.p, self.tid), self.repo, [sha], "fm ship")
            self.assertIn("rm -rf ~", str(got.exception))
        self.assertEqual(list(c.ledger_events(self.p, ("self_change_passed",))), [])

    def test_the_hold_judges_four_commits_at_once_each_in_its_own_worktree(self):
        """T-6031: a stub gate that waits for three others (a serial hold times the barrier out and is held)."""
        import threading

        shas = [self.commit({"plugin/lib/fmguard.py": f"x = {i + 2}\n"}, f"commit {i}") for i in range(6)]
        lock, state = threading.Lock(), {"active": 0, "peak": 0, "calls": 0, "trees": [], "caches": [], "bad": []}
        barrier = threading.Barrier(4, timeout=20)

        def gate(p, b, main, ref, files, cache_name="", timing=True, **kw):
            with lock:
                state["calls"] += 1
                first = state["calls"] <= 4
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state["trees"].append(main)
                state["caches"].append(cache_name)
            try:
                if first:
                    barrier.wait()
                head = sh(main, "rev-parse", "HEAD").strip()
                if timing is False and head != sh(main, "rev-parse", f"{ref}^1").strip():  # T-6048: not the range's timing call
                    state["bad"].append(ref)
                return []
            finally:
                with lock:
                    state["active"] -= 1
        seen, why = self.hold(shas, gate)
        self.assertEqual((sorted(seen), why), (sorted(shas), ""))
        self.assertEqual(state["peak"], 4, "four at once, never more")
        self.assertEqual(state["bad"], [], "each tree stood at its commit's parent while it was judged")
        self.assertEqual(len(set(state["trees"][:4])), 4, "four trees at the same time")
        import glob

        from sampo import release
        self.assertEqual(set(state["caches"]), {release.PUSH_CACHE}, "one base cache file, not a file a worker")
        self.assertEqual(sorted(os.path.basename(f) for f in glob.glob(os.path.join(self.p.dir, "selfgate-base*"))
                                ), [], "the stub gate saved nothing, and no worker left a file behind")

    def test_the_trains_own_knob_sets_how_many_commits_are_judged_at_once(self):
        """T-6031: SAMPO_HOLD_JOBS (train.hold_jobs), not a second knob of the push hold's."""
        import threading
        from unittest import mock

        shas = [self.commit({"plugin/lib/fmguard.py": f"x = {i + 2}\n"}, f"commit {i}") for i in range(5)]
        lock, state = threading.Lock(), {"active": 0, "peak": 0}

        def gate(p, b, main, ref, files, *a, **kw):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.2)
            with lock:
                state["active"] -= 1
            return []
        with mock.patch.dict(os.environ, {"SAMPO_HOLD_JOBS": "2"}):
            seen, why = self.hold(shas, gate)
        self.assertEqual((sorted(seen), why, state["peak"]), (sorted(shas), "", 2))

    def test_no_replay_runs_while_a_hook_bench_is_timing(self):
        """T-6031 review: four workers judge at once, but a bench's timings are skewed by another worker's replay on the same
        CPUs: the real gate over five commits never replays (in any worker) while any bench is running."""
        import threading
        from unittest import mock

        from sampo import cli, release, selfgate
        shas = [self.commit({"plugin/lib/fmguard.py": f"x = {i + 2}\n"}, f"commit {i}") for i in range(5)]
        lock, state = threading.Lock(), {"replays": 0, "benches": 0, "overlap": 0, "ran": 0, "peak": 0}

        def enter(kind, other):
            with lock:
                state[kind] += 1
                state["ran"] += kind == "benches"
                state["peak"] = max(state["peak"], state["replays"] + state["benches"])
                if state[other]:
                    state["overlap"] += 1

        def leave(kind):
            with lock:
                state[kind] -= 1

        def verdicts(tree, cmds):
            enter("replays", "benches")
            time.sleep(0.05)
            leave("replays")
            return ["allow" if cmd == "ls" else "destructive" for cmd, _ in cmds]

        def bench(tree):
            enter("benches", "replays")
            time.sleep(0.05)
            leave("benches")
            return {"Stop": {"p95": 100.0, "budget": 500}}, {"SessionStart": 2000}
        with mock.patch.object(selfgate, "_verdicts", verdicts), mock.patch.object(selfgate, "_bench", bench), \
                mock.patch.object(selfgate, "_corpus", lambda: []):
            release._push_hold(self.p, c.find_brief(self.p, self.tid), self.repo, shas, "fm ship")
        self.assertGreater(state["ran"], 0, "the benches ran")
        self.assertEqual(state["overlap"], 0, "a replay and a bench ran at the same time")

    def test_a_pass_the_user_or_the_standing_yes_gave_is_remembered_and_revoking_standing_judges_again(self):
        """T-6031: a commit a yes passed was judged by every ship (the standing yes passes it each time): its pass is
        recorded with the kind of yes, and a standing pass is only read while the standing yes stands."""
        sha = self.commit({"plugin/lib/sampo/cli.py": 'add("brandnew")\n'}, "a verb with none removed")
        seen, why = self.hold([sha])
        self.assertIn("verbs", why, "the quality row holds without a standing yes")
        self.assertEqual(list(c.ledger_events(self.p, ("self_change_passed",))), [])
        c.update_meta(self.p, standing={"core": {"at": c.now(), "task": self.tid, "why": "test"}})
        self.assertEqual(self.hold([sha]), ([sha], ""), "the standing yes passes it")
        self.assertEqual(self.hold([sha]), ([], ""), "and the next hold does not judge it again")
        c.update_meta(self.p, standing={})
        seen, why = self.hold([sha])
        self.assertEqual(seen, [sha], "revoked: judged again")
        self.assertIn("verbs", why)
        c.log_event(self.p, "approval_granted", task=self.tid, data={"allow": ["core"], "via": "prompt"})
        self.assertEqual(self.hold([sha]), ([sha], ""), "the user's yes after the refusal passes it")
        c.update_meta(self.p, standing={})
        self.assertEqual(self.hold([sha]), ([], ""), "a yes is kept when no standing yes is set")

    def test_a_pass_line_with_no_valid_signature_is_not_read(self):
        """T-6031: a line an agent appends to the ledger (or edits to name another commit, key or task) skips nothing."""
        from sampo import selfgate
        sha = self.commit({"plugin/lib/fmguard.py": "x = 2\n"}, "one")
        b = c.find_brief(self.p, self.tid)
        c.log_event(self.p, "self_change_passed", task=self.tid, data={"sha": sha, "key": selfgate.gate_key()})
        c.log_event(self.p, "self_change_passed", task=self.tid, data={"sha": sha, "key": selfgate.gate_key(), "mac": "0" * 64})
        self.assertEqual(self.hold([sha])[0], [sha], "unsigned and wrongly signed lines skip nothing")
        selfgate._log_pass(self.p, b, sha)
        self.assertEqual(self.hold([sha])[0], [], "the signed one does")
        sealed = [e for e in c.ledger_events(self.p, ("self_change_passed",)) if len(e["data"].get("mac", "")) == 64
                  and e["data"]["mac"] != "0" * 64][-1]
        other = self.commit({"plugin/lib/fmguard.py": "x = 3\n"}, "two")
        forged = json.dumps(dict(sealed, data=dict(sealed["data"], sha=other)))  # the signature of another commit's pass
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a", encoding="utf-8") as f:
            f.write(forged + "\n")
        self.assertEqual(self.hold([other])[0], [other], "a signature does not carry to another sha")

    def test_the_gate_key_names_every_module_the_gate_imports_or_says_why_not(self):
        """T-6031: a gate input left out of the key lets a changed judge's old passes stand. The imports of selfgate (any
        depth) and doctor (module level) are each in KEY_MODULES or KEY_EXEMPT, and a change to a keyed module's file
        changes the key."""
        import ast
        from unittest import mock

        from sampo import selfgate
        lib = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "sampo")
        found = set()
        for name, top in (("selfgate", False), ("doctor", True)):
            with open(os.path.join(lib, name + ".py"), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in (tree.body if top else ast.walk(tree)):
                if isinstance(node, ast.ImportFrom) and node.module == "sampo":
                    found |= {a.name for a in node.names}
        self.assertIn("replay", found, "the scan sees selfgate's lazy import")
        self.assertIn("flake_forecast", found, "and doctor's")
        left = found - set(selfgate.KEY_MODULES) - set(selfgate.KEY_EXEMPT) - {"selfgate", "doctor"}
        self.assertEqual(left, set(), "a module the gate imports is in neither KEY_MODULES nor KEY_EXEMPT")
        self.assertEqual(set(selfgate.KEY_MODULES) & set(selfgate.KEY_EXEMPT), set())
        base = selfgate.gate_key()
        self.assertEqual(selfgate.gate_key(), base)
        real = selfgate._module_bytes
        for name in selfgate.KEY_MODULES:
            with mock.patch.object(selfgate, "_module_bytes", lambda n, name=name: (real(n) or b"") + b"\n# edit" if n == name else real(n)):
                self.assertNotEqual(selfgate.gate_key(), base, f"a change to {name} leaves the key alone")
        with mock.patch.object(selfgate, "_module_bytes", lambda n: None):
            self.assertNotEqual(selfgate.gate_key(), selfgate.gate_key(), "an unreadable module proves nothing")

    def test_a_refusal_among_concurrent_judgements_is_still_held_with_its_rows(self):
        shas = [self.commit({"plugin/lib/fmguard.py": f"x = {i + 2}\n"}, f"commit {i}") for i in range(5)]

        def gate(p, b, main, ref, files, *a, **kw):
            return ["guard: rm -rf ~: base destructive, branch allow"] if ref == shas[3] else []
        seen, why = self.hold(shas, gate)
        self.assertIn(shas[3][:8], why)
        self.assertNotIn(shas[2][:8], why)
        self.assertIn("nothing was pushed", why)

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


SUITE_STUB = '''import os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "runs.log"), "a") as f:
    f.write(("only " + " ".join(sys.argv[1:]) if sys.argv[1:] else "full") + "\\n")
if not sys.argv[1:] and os.path.exists(os.path.join(here, "cut")):  # run.py's closing words for a run cut at its deadline
    print("INCOMPLETE (stopped at its deadline)\\nnot run (deadline): 2\\n  not run class: test_x.A\\n  not run class: test_y.B")
    sys.exit(124)
'''


class ShipRecordsCase(ForemanTestCase):
    """T-6097: fm ship in a temp repo with a stub suite that counts its runs (no real suite, no real push): the commits are
    a, b, c, d on a branch off main; the post-merge verify's records are written by hand."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        sh(self.repo, "remote", "add", "origin", self.origin)
        sh(self.repo, "push", "-q", "origin", "HEAD:main")
        sh(self.repo, "checkout", "-q", "-b", "work")
        for name in ("a", "b", "c", "d"):
            self.commit(name)
        self.tid = json.loads(self.fm("task", "new", "land", "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "step", self.tid, "add", "land it")
        self.fm("task", "ac", self.tid, "add", "landed", "--verify", "true")
        self.fm("focus", self.tid)
        p = c.find_project(self.repo)
        b = c.find_brief(p, self.tid)
        b.meta["allow"] = ["publish"]
        c.save_brief(p, b)
        self.stub = os.path.join(self.tmp, "suite.py")
        with open(self.stub, "w") as f:
            f.write(SUITE_STUB)
        self.suite = f"python3 {self.stub}"
        self.env = {"FM_POSTMERGE_CMD": self.suite, "FM_POSTMERGE_RERUN": f"python3 {self.stub} {{classes}}"}

    def commit(self, name):
        with open(os.path.join(self.repo, f"{name}.txt"), "w") as f:
            f.write(name + "\n")
        sh(self.repo, "add", f"{name}.txt")
        sh(self.repo, "commit", "-qm", f"add {name}")

    def rev(self, rev):
        return sh(self.repo, "rev-parse", rev).strip()

    def rec(self, rev, state="green", **kv):
        """The post-merge verify's record on rev, as lanes.pm_record writes it, in `state`."""
        sha = self.rev(rev)
        d = os.path.join(c.find_project(self.repo).dir, "postmerge")
        os.makedirs(d, exist_ok=True)
        c.write_atomic(os.path.join(d, f"{sha}.json"), json.dumps(
            {"sha": sha, "task": self.tid, "state": state, "run_at": sha, "n": time.time_ns(), "at": c.now(), **kv}))
        return sha

    def runs(self):
        try:
            with open(os.path.join(self.tmp, "runs.log")) as f:
                return f.read().split("\n")[:-1]
        except OSError:
            return []

    def main_log(self):
        return sh(self.origin, "log", "--format=%s", "main").split("\n")

    def ship(self, *a):
        return self.fm("ship", "--remote", "origin", "--test", self.suite, *a, env=self.env, check=False, timeout=60)


class ShipFromRecords(ShipRecordsCase):
    """T-6097: the ship pushes the newest commit the verify records say passed the whole suite, running none itself."""

    def test_a_green_record_on_a_commit_ships_it_with_no_suite_run(self):
        sha = self.rec("HEAD~1")  # c: green; d, the head, has no record
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:3], ["add c", "add b", "add a"])
        self.assertNotIn("add d", self.main_log())
        self.assertEqual(self.runs(), [], "the stub suite never ran")
        self.assertIn(f"shipping {sha[:9]}", r.stdout)
        self.assertIn(f"record on {sha[:9]}", r.stdout, "the ship log names the record it shipped on")
        self.assertIn("1 newer commit", r.stdout)

    def test_a_red_record_holds_its_merge_and_the_commits_after_it(self):
        good = self.rec("HEAD~2")  # b: green
        self.rec("HEAD~1", "red")  # c: red
        self.rec("HEAD")  # d: green on its own tree, but after the red one
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:2], ["add b", "add a"])
        self.assertNotIn("add c", self.main_log())
        self.assertEqual(self.runs(), [])
        self.assertIn(f"record on {good[:9]}", r.stdout)

    def test_a_red_record_with_no_green_before_it_runs_the_suite_once_below_it(self):
        self.rec("HEAD~1", "red")  # c: red; nothing proven below it
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:2], ["add b", "add a"], "the ship stops under the red merge")
        self.assertNotIn("add c", self.main_log())
        self.assertEqual(self.runs(), ["full"])

    def test_a_ship_with_no_record_runs_the_suite_once_and_that_run_becomes_the_record(self):
        from sampo import lanes
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.runs(), ["full"])
        self.assertEqual(self.main_log()[:4], ["add d", "add c", "add b", "add a"])
        passes = lanes.pm_full_passes(c.find_project(self.repo))
        self.assertEqual([x["sha"] for x in passes], [self.rev("HEAD")], "the ship's own green run is a full pass on the head")

    def test_a_partial_a_narrowed_or_a_reverted_green_is_no_proof(self):
        self.rec("HEAD", partial=True)
        self.rec("HEAD~1", narrowed=True)
        self.rec("HEAD~2", after_reverts=True)
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.runs(), ["full"], "no record proves a whole run: the suite ran")
        self.assertEqual(self.main_log()[:4], ["add d", "add c", "add b", "add a"])

    def test_a_merge_covered_by_a_later_full_run_proves_that_commit(self):
        self.rec("HEAD~2", covered_by=self.rev("HEAD~1"), narrowed=False, partial=False)  # b verified on c's tree
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:3], ["add c", "add b", "add a"])
        self.assertEqual(self.runs(), [])

    def test_another_suite_command_is_no_proof(self):
        self.rec("HEAD~1")
        r = self.fm("ship", "--remote", "origin", "--test", self.suite, env={**self.env, "FM_POSTMERGE_CMD": "true"},
                    check=False, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.runs(), ["full"], "the records ran another suite than this ship's: it runs its own")


class ShipResume(ShipRecordsCase):
    """T-6097: a ship cut at its deadline keeps the classes it never ran, and the next ship on the same tree runs only
    those; a verify running on the tree the ship is about to test is waited for and reused."""

    def cut(self):
        open(os.path.join(self.tmp, "cut"), "w").close()

    def test_a_deadline_cut_ship_records_what_it_left_and_the_next_ship_runs_only_that(self):
        self.cut()
        r = self.ship()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("add a", self.main_log(), "a cut run pushes nothing")
        keep = c.read_meta(c.find_project(self.repo)).get("ship_resume") or {}
        self.assertEqual(keep.get("not_run"), ["test_x.A", "test_y.B"])
        self.assertEqual(keep.get("target"), self.rev("HEAD"))
        os.remove(os.path.join(self.tmp, "cut"))
        self.commit("e")  # the head moved on: the next ship stays on the pinned commit
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.runs(), ["full", "only test_x.A test_y.B"], "the second ship ran only the classes not run")
        self.assertEqual(self.main_log()[:4], ["add d", "add c", "add b", "add a"])
        self.assertNotIn("add e", self.main_log())
        self.assertIn("resuming", r.stdout)
        self.assertFalse(c.read_meta(c.find_project(self.repo)).get("ship_resume"), "spent with the push")

    def test_a_resume_on_another_tree_runs_the_whole_suite(self):
        self.cut()
        self.assertNotEqual(self.ship().returncode, 0)
        os.remove(os.path.join(self.tmp, "cut"))
        p = c.find_project(self.repo)
        keep = c.read_meta(p)["ship_resume"]
        c.update_meta(p, ship_resume={**keep, "tree": "0" * 40})  # what was left belongs to another tree
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.runs(), ["full", "full"])

    def test_a_concurrent_verify_on_the_tree_is_waited_for_and_reused(self):
        import threading
        sleeper = subprocess.Popen(["sleep", "60"])  # a real process: the record's pid answers while it runs
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        sha = self.rec("HEAD", "running", pid=sleeper.pid, ticks=c.proc_ticks(sleeper.pid))

        def finish():
            from sampo import lanes
            p = c.find_project(self.repo)
            end = time.time() + 40
            while not c.read_meta(p).get("ship_testing") and time.time() < end:  # the ship reached its suite phase
                time.sleep(0.1)
            time.sleep(1.5)  # long enough for its wait to start
            lanes.pm_set(p, sha, state="green")
            sleeper.terminate()
        t = threading.Thread(target=finish, daemon=True)
        t.start()
        r = self.ship()
        t.join()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("waiting for", r.stdout + r.stderr)
        self.assertEqual(self.runs(), [], "the verify's result is reused: the ship ran nothing")
        self.assertEqual(self.main_log()[:4], ["add d", "add c", "add b", "add a"])
