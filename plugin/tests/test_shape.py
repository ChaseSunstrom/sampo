"""sampo shape FILE (T-3275, moon C-000723): the companions a new file is owed (a test module, an fm alias, a MASTER.md
line), learned from how earlier files of the same shape were added, and their drafts. Nothing is written without
--write, and never over a file that exists."""
import os
import shutil
import subprocess

from helpers import SampoTestCase

from sampo import shape

NAMES = ("alpha", "bravo", "charlie", "delta")
NEWEST = "delta"


class History(SampoTestCase):  # the fixture: four past add-commits, no tests of its own
    def setUp(self):
        super().setUp()
        for name in NAMES:  # src/NAME.py with its test module, a MASTER.md line, and NOTES.md rewritten in place
            self.add(f"src/{name}.py", f"def {name}(): pass\n")
            self.add(f"tests/test_{name}.py", self.module_text(name))
            self.add("MASTER.md", self.master_line(name), mode="a")
            self.add("NOTES.md", f"{name}\n")
            if name in ("alpha", "bravo"):  # a companion in 2 of 4 adds: below the 3-commit bar, owed nothing
                self.add(f"docs/{name}.md", f"# {name}\n")
            self.commit(f"add {name}")
        self.add("README.md", "x\n")  # a commit that adds no same-shaped file says nothing about companions
        self.commit("readme")

    def module_text(self, name):
        note = "  # newest" if name == NEWEST else ""  # the newest module differs: a draft from an older one shows it
        return f"from src import {name}{note}\n\n\ndef test_{name}():\n    assert {name}\n"

    def master_line(self, name):
        if name == NEWEST:  # the newest add's shared line names no companion: a draft must not paste it
            return "- an unrelated entry\n"
        return f"- `fm{name}` (`sampo {name}`: the {name} module)\n"

    def add(self, rel, text, mode="w"):
        os.makedirs(os.path.join(self.repo, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(self.repo, rel), mode) as f:
            f.write(text)

    def commit(self, msg):
        for args in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg]):
            subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)


class Shape(History):
    def test_owed_learns_the_test_module_and_the_master_line_from_past_adds(self):
        owed = {o["template"]: o for o in shape.owed(self.repo, "src/foo.py")}
        self.assertEqual(set(owed), {"tests/test_<stem>.py", "MASTER.md"}, "docs/<stem>.md, seen twice, is not owed")
        self.assertEqual((owed["tests/test_<stem>.py"]["count"], owed["tests/test_<stem>.py"]["of"]), (4, 4))
        self.assertEqual(owed["MASTER.md"]["share"], 1.0)
        self.assertNotIn("src/<stem>.py", owed, "the subject itself is never its own companion")

    def test_a_file_rewritten_in_place_is_not_a_companion_even_when_it_changes_in_every_add(self):
        owed = {o["template"] for o in shape.owed(self.repo, "src/foo.py")}
        self.assertNotIn("NOTES.md", owed, "only the first add appended to it: the other three rewrote it")

    def test_no_same_shaped_history_owes_nothing(self):
        self.assertEqual(shape.owed(self.repo, "lib/foo.py"), [])
        nogit = os.path.join(self.tmp, "nogit")
        os.makedirs(nogit)
        self.assertIsNone(shape.owed(nogit, "src/foo.py"), "no git: unknown, not nothing owed")

    def test_draft_copies_the_newest_sibling_with_the_stem_swapped_and_writes_nothing_without_write(self):
        stubs = {s["path"]: s for s in shape.draft(self.repo, "src/foo.py")}
        self.assertEqual(stubs["tests/test_foo.py"]["text"],
                         "from src import foo  # newest\n\n\ndef test_foo():\n    assert foo\n")
        self.assertEqual(stubs["MASTER.md"]["text"], "- `fmfoo` (`sampo foo`: the foo module)",
                         "the newest add's line names no stem, so the one before it is the draft")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "tests/test_foo.py")), "a draft writes nothing")
        self.assertNotIn("fmfoo", open(os.path.join(self.repo, "MASTER.md")).read())

    def test_write_creates_missing_stubs_and_never_overwrites_one(self):
        self.add("tests/test_foo.py", "keep me\n")
        shape.draft(self.repo, "src/foo.py", write=True)
        self.assertEqual(open(os.path.join(self.repo, "tests/test_foo.py")).read(), "keep me\n")
        self.assertNotIn("fmfoo", open(os.path.join(self.repo, "MASTER.md")).read(), "a shared file gets the line by hand")
        os.remove(os.path.join(self.repo, "tests/test_foo.py"))
        self.fm("shape", "src/foo.py", "--write")
        self.assertIn("from src import foo", open(os.path.join(self.repo, "tests/test_foo.py")).read())

    def test_a_folder_that_links_out_of_the_repo_is_neither_read_nor_written(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        for name in NAMES:  # siblings out there: a link followed would copy one and write beside it
            with open(os.path.join(outside, f"test_{name}.py"), "w") as f:
                f.write(f"outside {name}\n")
        shutil.rmtree(os.path.join(self.repo, "tests"))
        os.symlink(outside, os.path.join(self.repo, "tests"))
        stub = {s["path"]: s for s in shape.draft(self.repo, "src/foo.py", write=True)}["tests/test_foo.py"]
        self.assertFalse(os.path.exists(os.path.join(outside, "test_foo.py")), "nothing is written out of the repo")
        self.assertNotEqual(stub["state"], "wrote")
        self.assertIsNone(stub["text"], "no sibling is read out of the repo either")

    def test_the_cli_prints_one_line_per_owed_file_and_says_nothing_owed_without_a_pattern(self):
        lines = [x for x in self.fm("shape", "src/foo.py").stdout.splitlines() if x.strip()]
        self.assertEqual(len(lines), 2, lines)
        by = {x.split()[0]: x for x in lines}
        self.assertEqual(set(by), {"tests/test_foo.py", "MASTER.md"}, lines)
        self.assertIn("4 of 4", by["tests/test_foo.py"])
        self.assertIn("- `fmfoo` (`sampo foo`: the foo module)", by["MASTER.md"])
        self.assertIn("nothing owed", self.fm("shape", "lib/foo.py").stdout)

    def test_an_absolute_path_names_the_same_file_as_a_repo_relative_one(self):
        rel = self.fm("shape", "src/foo.py").stdout
        self.assertEqual(self.fm("shape", os.path.join(self.repo, "src/foo.py")).stdout, rel)

    def test_a_new_file_named_from_inside_its_folder_is_read_relative_to_the_cwd(self):
        old = os.getcwd()
        os.chdir(os.path.join(self.repo, "src"))
        try:
            self.assertEqual(shape._rel(self.repo, "new.py"), "src/new.py")
        finally:
            os.chdir(old)

    def test_control_characters_from_history_print_as_question_marks(self):
        self.assertEqual(shape.plain("a\x1b[2Jb\n"), "a?[2Jb?")

    def test_a_stem_that_is_not_a_plain_name_owes_nothing_and_writes_nothing(self):
        self.assertEqual(shape.owed(self.repo, "src/.py"), [])
        self.assertEqual(shape.draft(self.repo, "src/.py", write=True), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "tests", "test_.py")))


class SharedLines(History):  # every shared line names no stem: the draft is left for the user to adapt
    def master_line(self, name):
        return "- a shared entry\n"

    def test_a_shared_line_that_names_no_stem_is_adapted_by_hand_not_copied(self):
        m = {s["path"]: s for s in shape.draft(self.repo, "src/foo.py")}["MASTER.md"]
        self.assertEqual((m["state"], m["text"]), ("adapt", None))
