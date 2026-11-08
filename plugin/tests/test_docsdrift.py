"""fm docs: markdown that drifted from the repo (missing paths, npm/make commands that don't exist)."""
import json
import os
import subprocess

from helpers import ForemanTestCase

from test_prove import cli, write

from sampo import docs


class _Repo(ForemanTestCase):
    """The fixture repo with drift in it, and kinds(); no tests of its own (T-3815), so TaskDocsDrift shares it without
    rerunning DocsDrift's tests."""
    def setUp(self):
        super().setUp()
        files = {
            "src/app.py": "x = 1\n",
            "package.json": json.dumps({"scripts": {"test": "jest"}}),
            "Makefile": "test:\n\tpytest\n",
            ".gitignore": "local/\nnode_modules/\n",
            "README.md": "Run `src/app.py` or `src/gone.py`; build with `npm run build`, test with `npm run test`.\n"
                         "```\nsrc/in-a-code-block.py\n```\n`local/notes.md` is machine-specific.\n",
            "docs/guide.md": "See `../README.md`, `docs/setup.md`, `./install.md`, `STATE.md`, `org/repo`, "
                             "`foreman/build` and `make deploy`.\n",
            "node_modules/pkg/README.md": "`missing/everywhere.js`\n",
        }
        for rel, text in files.items():
            path = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

    def kinds(self, findings):
        return sorted((f["file"], f["kind"], f["detail"]) for f in findings)


class DocsDrift(_Repo):
    def test_scan_reports_drift_only(self):
        found = self.kinds(docs.scan(self.repo))
        self.assertEqual(found, [
            ("README.md", "command", "npm run build"),
            ("README.md", "path", "src/gone.py"),
            ("docs/guide.md", "command", "make deploy"),
            ("docs/guide.md", "path", "./install.md"),
            ("docs/guide.md", "path", "docs/setup.md"),
        ])  # bare names, other repos and branch names aren't repo paths: never reported

    def test_a_folder_holding_only_ignored_files_is_not_a_repo_path(self):
        # Claude Code drops .claude/scheduled_tasks.lock (ignored) into repos; `.claude/…` in docs means the user's
        os.makedirs(os.path.join(self.repo, ".claude"))
        open(os.path.join(self.repo, ".claude", "scheduled_tasks.lock"), "w").close()
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write("**/.claude/scheduled_tasks.lock\n")
        with open(os.path.join(self.repo, "SETUP.md"), "w") as f:
            f.write("Put overrides in `.claude/settings.local.json`; the app is `src/app.py`, not `src/old.py`.\n")
        found = [f["detail"] for f in docs.scan(self.repo) if f["file"] == "SETUP.md"]
        self.assertEqual(found, ["src/old.py"])

    def test_ignored_markdown_is_not_scanned(self):
        # T-0030: point-in-time notes in ignored folders (Foreman's state/, local/) aren't the repo's docs
        os.makedirs(os.path.join(self.repo, "local"), exist_ok=True)
        with open(os.path.join(self.repo, "local", "notes.md"), "w") as f:
            f.write("Audit note: `src/gone-long-ago.py:12` was the bug.\n")
        self.assertNotIn("local/notes.md", {f["file"] for f in docs.scan(self.repo)})

    def test_stack_cards_and_changelogs_name_other_trees(self):
        # T-1817: a card's paths are the detected project's (./gradlew); a changelog's, files as they were then
        os.makedirs(os.path.join(self.repo, "cards"), exist_ok=True)
        with open(os.path.join(self.repo, "cards", "gradle.md"), "w") as f:
            f.write("---\nname: gradle\ndetect: [file:gradlew]\n---\nRun `./gradlew build`; see `src/gone.py`.\n")
        with open(os.path.join(self.repo, "CHANGELOG.md"), "w") as f:
            f.write("## 1.0\n- `src/removed.py` is gone; `./gradlew` too.\n")
        found = {f["file"] for f in docs.scan(self.repo)}
        self.assertFalse(found & {"cards/gradle.md", "CHANGELOG.md"}, found)
        self.assertIn("README.md", found)  # drift elsewhere still reports

    def test_cli(self):
        p = self.fm("docs", "--json", cwd=self.repo)
        self.assertEqual(len(json.loads(p.stdout)["findings"]), 5)
        self.assertEqual(self.fm("docs", "--strict", cwd=self.repo, check=False).returncode, 1)
        for rel in ("README.md", "docs/guide.md"):
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write("Nothing to check.\n")
        self.assertEqual(self.fm("docs", "--strict", cwd=self.repo).returncode, 0)


class TaskDocsDrift(_Repo):
    """T-0035: a doc's old drift must not hold up the task that touched it; drift it added must."""

    def setUp(self):
        super().setUp()
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "docs with old drift"], check=True)

    def append(self, rel, text):
        with open(os.path.join(self.repo, rel), "a") as f:
            f.write(text)

    def test_task_docs_old_drift_is_a_note_not_a_blocker(self):
        self.append("README.md", "The app entry point is `src/app.py`.\n")
        blockers, notes = docs.task_docs(self.repo, "README.md")
        self.assertEqual(blockers, [])
        self.assertTrue(any("src/gone.py" in n for n in notes))

    def test_task_docs_drift_the_task_added_still_blocks(self):
        self.append("README.md", "Run `src/never-written.py` next.\n")
        blockers, _ = docs.task_docs(self.repo, "README.md")
        self.assertEqual(len(blockers), 1)
        self.assertIn("src/never-written.py", blockers[0])

    def test_a_missing_named_doc_suggests_the_tracked_path_it_meant(self):
        # T-1995: "rules/foreman.md" for plugin/rules/foreman.md was refused with no hint
        os.makedirs(os.path.join(self.repo, "plugin", "rules"))
        with open(os.path.join(self.repo, "plugin", "rules", "foreman.md"), "w") as f:
            f.write("# Rules\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        blockers, _ = docs.task_docs(self.repo, "rules/foreman.md and gone/NOPE.md")
        self.assertEqual(blockers, ["docs impact names gone/NOPE.md, which doesn't exist",
                                    "docs impact names rules/foreman.md, which doesn't exist (plugin/rules/foreman.md?)"])
        os.makedirs(os.path.join(self.repo, "mods", "rules"))  # T-2000: several matches are all suggested
        with open(os.path.join(self.repo, "mods", "rules", "foreman.md"), "w") as f:
            f.write("# Rules\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        blockers, _ = docs.task_docs(self.repo, "rules/foreman.md")
        self.assertEqual(blockers, ["docs impact names rules/foreman.md, which doesn't exist "
                                    "(one of mods/rules/foreman.md, plugin/rules/foreman.md?)"])

    def test_task_docs_docs_proof_a_renamed_flag_blocks_and_prose_checks_nothing(self):
        # T-2381: the static rung of fm prove --docs runs on the lines the task added to the docs it names
        write(self.repo, {"bin/mycli": cli("/dev/null"), "CHANGELOG.md": "# Changes\n"})
        os.chmod(os.path.join(self.repo, "bin", "mycli"), 0o755)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "a cli"], check=True)
        self.append("README.md", "Build with `mycli build --out dist`.\n")
        self.assertEqual(docs.task_docs(self.repo, "README.md")[0], [])
        self.append("README.md", "Or `mycli build --renamed-flag`.\n")
        blockers, _ = docs.task_docs(self.repo, "README.md")
        self.assertEqual(len(blockers), 1, blockers)
        self.assertIn("--renamed-flag", blockers[0])
        subprocess.run(["git", "-C", self.repo, "checkout", "README.md"], check=True)
        self.append("CHANGELOG.md", "- the mycli --renamed-flag option is gone.\n")
        self.append("README.md", "A prose line, no command.\n")
        self.assertEqual(docs.task_docs(self.repo, "CHANGELOG.md and README.md")[0], [])

    def test_task_docs_a_new_doc_is_all_added(self):
        self.append("NEW.md", "See `src/missing.py`.\n")
        blockers, _ = docs.task_docs(self.repo, "NEW.md")
        self.assertTrue(any("src/missing.py" in b for b in blockers))
