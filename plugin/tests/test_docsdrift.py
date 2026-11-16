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

    def test_id_a_pytest_id_names_its_file_and_an_existing_file_is_not_missing(self):
        # T-4148: `tests/test_app.py::App::test_it` read as a missing path; the file before :: is what is checked
        write(self.repo, {"tests/test_app.py": "x = 1\n"})
        self.append("README.md", "Run `tests/test_app.py::App::test_it` and `tests/test_app.py::test_other[param]`; "
                                 "not `tests/test_gone.py::Gone::test_it`.\n")
        details = [f["detail"] for f in docs.scan(self.repo) if f["file"] == "README.md"]
        self.assertNotIn("tests/test_app.py", " ".join(details))
        self.assertIn("tests/test_gone.py::Gone::test_it", details, "a test file that is gone is still drift, as written")
        self.assertEqual(len([d for d in details if "test_gone" in d]), 1, details)
        blockers, _ = docs.task_docs(self.repo, "README.md")
        self.assertEqual([b for b in blockers if "tests/test_app.py" in b], [])
        self.assertEqual(len([b for b in blockers if "tests/test_gone.py" in b]), 1, blockers)

    def test_id_a_bare_doc_name_that_one_tracked_doc_has_is_that_doc(self):
        # T-4148: "docs impact names verification.md, which doesn't exist" (11 refusals) for docs/verification.md
        write(self.repo, {"docs/verification.md": "# Verifying\nSee `src/app.py`.\n"})
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        self.append("docs/verification.md", "And `src/never-written.py`.\n")
        blockers, notes = docs.task_docs(self.repo, "verification.md")
        self.assertEqual(len(blockers), 1, blockers)
        self.assertIn("src/never-written.py", blockers[0], "its drift is checked as docs/verification.md's")
        self.assertFalse([b for b in blockers if "doesn't exist" in b], blockers)
        self.assertTrue(any("docs/verification.md" in n for n in notes), notes)  # says which doc it took it for

    def test_id_a_bare_doc_name_two_docs_share_is_still_missing_and_names_both(self):
        write(self.repo, {"docs/verification.md": "# A\n", "ops/verification.md": "# B\n"})
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        blockers, _ = docs.task_docs(self.repo, "verification.md")
        self.assertEqual(blockers, ["docs impact names verification.md, which doesn't exist "
                                    "(one of docs/verification.md, ops/verification.md?)"])

    def commit_all(self):
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
                       check=True)

    def test_id_a_bare_name_of_a_doc_the_task_never_touched_stays_missing(self):
        # T-4155: vendor/x/CHANGELOG.md is tracked and unchanged: "CHANGELOG.md" named in a docs impact is not it
        write(self.repo, {"vendor/x/CHANGELOG.md": "# Vendored\n"})
        self.commit_all()
        blockers, notes = docs.task_docs(self.repo, "CHANGELOG.md")
        self.assertEqual(blockers, ["docs impact names CHANGELOG.md, which doesn't exist (vendor/x/CHANGELOG.md?)"])
        self.assertFalse([n for n in notes if "taken as" in n], notes)
        self.append("vendor/x/CHANGELOG.md", "more\n")  # the task changed it: now it is that doc
        blockers, notes = docs.task_docs(self.repo, "CHANGELOG.md")
        self.assertEqual(blockers, [])
        self.assertTrue(any("taken as vendor/x/CHANGELOG.md" in n for n in notes), notes)

    def test_id_a_bare_name_of_a_doc_removed_from_disk_stays_missing(self):
        # T-4155: git ls-files still lists a tracked doc that was deleted on disk; there is no doc to have updated
        write(self.repo, {"docs/verification.md": "# Verifying\n"})
        self.commit_all()
        os.unlink(os.path.join(self.repo, "docs", "verification.md"))
        blockers, notes = docs.task_docs(self.repo, "verification.md")
        self.assertEqual(blockers, ["docs impact names verification.md, which doesn't exist (docs/verification.md?)"])
        self.assertFalse([n for n in notes if "taken as" in n], notes)

    def test_id_an_ignore_entry_matches_the_file_of_a_pytest_id(self):
        # T-4155: findings are named by the file part of path::Class::test; git's ignore rules must see that file too
        self.append(".gitignore", "src/gen_*.py\n")
        self.append("README.md", "Run `src/gen_x.py::T::test_it`, not `src/other_gone.py::T::test_it`.\n")
        details = [f["detail"] for f in docs.scan(self.repo) if f["file"] == "README.md"]
        self.assertFalse([d for d in details if "gen_x" in d], details)
        self.assertIn("src/other_gone.py::T::test_it", details)

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
