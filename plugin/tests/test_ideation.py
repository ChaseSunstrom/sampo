"""T-0684 (Intelligence — brainstorming and ideation): know what's covered and what's thin, kill ideas fast, learn which
lenses the user keeps, reframe and provoke, and cross-breed the best."""
import glob
import json
import os

from helpers import ForemanTestCase, read_text

from sampo import core as c
from sampo import ideas as sideas

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
if "--append-system-prompt-file" in args:  # T-1265: the pack comes in the shared system text
    prompt = open(args[args.index("--append-system-prompt-file") + 1]).read() + "\n" + prompt
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps({"stdin": prompt}) + "\n")
if "KILL IT FAST" in prompt:
    print("1: nobody retries a flaky gate twice in a week")
    print("2: the mascot is muted by most users")
elif "CROSS-BREED" in prompt:
    print("- **Retrying mascot** — the mascot shows each retry — category: delight")
else:
    print("- **Retry flaky gates** — rerun a gate once before failing — category: reliability")
    print("- **Gate history** — keep pass rates per gate — category: reliability")
    print("- **Quarantine flakes** — park a flaky test — category: reliability")
    print("- **Animated mascot** — it dances on done — category: delight")
'''


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log}
        self.fm("init")
        self.pack = os.path.join(self.tmp, "pack.md")
        with open(self.pack, "w") as f:
            f.write("Make the gates calmer.\n")

    def ideas(self, *extra):
        self.fm("ideas", "--pack", self.pack, "--lens", "user value", *extra, env=self.env)
        out = sorted(glob.glob(os.path.join(self.home, "state", "projects", "*", "research", "brainstorm-*")))[-1]
        return out, read_text(os.path.join(out, "ideas.md"))

    def prompts(self):
        with open(self.log) as f:
            return [json.loads(x)["stdin"] for x in f]


class Coverage(Base):
    def test_the_index_maps_coverage_and_the_next_round_is_told_the_holes(self):
        _, md = self.ideas("--rounds", "2", "--dry", "0")
        self.assertIn("## Coverage", md)
        self.assertIn("reliability 3", md)
        self.assertRegex(md, r"Thin.*security")
        self.assertIn("Thin so far", self.prompts()[-1])


class Falsify(Base):
    def test_each_idea_gets_a_one_line_kill_test(self):
        _, md = self.ideas("--falsify")
        self.assertIn("## Kill it fast", md)
        self.assertIn("Retry flaky gates — nobody retries a flaky gate twice in a week", md)


class KeepRates(Base):
    def test_taste_shows_which_lenses_ideas_were_built(self):
        out, _ = self.ideas()
        with open(os.path.join(out, "ideas.json")) as f:
            self.assertIn("Animated mascot", json.load(f)["lenses"]["user value"])
        self.fm("task", "new", "Animated mascot on the pane", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                "--step", "s", "--focus")
        self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true")
        self.fm("capture", "Retry flaky gates once")
        self.fm("task", "drop", "T-0002", "not now")
        line = next(l for l in self.fm("taste").stdout.splitlines() if "user value" in l)
        self.assertIn("1/4 built", line)
        self.assertIn("1 dropped", line)


class Lenses(Base):
    def test_reframing_and_wild_lenses_exist_with_their_notes(self):
        for lens in ("reframe", "flip assumptions", "oblique provocation", "devil's idea", "worst-bugs persona"):
            self.assertIn(lens, sideas.LENSES)
            self.assertIn(sideas.LENS_NOTES[lens], sideas.lens_prompt(lens))
        self.assertIn("flip", read_text(os.path.join(c.PLUGIN_ROOT, "skills", "intake", "references",
                                                     "planning.md")).lower())


class Crossbreed(Base):
    def test_a_final_child_combines_the_top_ideas(self):
        _, md = self.ideas("--crossbreed")
        self.assertIn("## Cross-bred", md)
        self.assertIn("Retrying mascot", md)


class ChildFolder(ForemanTestCase):
    def test_each_child_runs_in_its_own_fresh_private_folder_that_is_removed(self):
        # T-5706 round 6: no shared persistent folder (T-0856's) and no /tmp fallback: a shared folder is a place a plant
        # survives in; the cost, a ~/.claude/projects shell per run, is a fair price for no state between runs
        import stat
        from unittest import mock
        root = os.path.join(self.tmp, "root")
        os.makedirs(root, mode=0o700)
        with mock.patch.object(sideas, "safe_root", return_value=root):
            with sideas.fresh_cwd("ideas") as a, sideas.fresh_cwd("ideas") as b:
                self.assertNotEqual(a, b)
                for d in (a, b):
                    st = os.lstat(d)
                    self.assertTrue(stat.S_ISDIR(st.st_mode))
                    self.assertEqual((st.st_uid, stat.S_IMODE(st.st_mode), os.path.dirname(d)), (os.getuid(), 0o700, root))
            self.assertEqual(os.listdir(root), [], "removed after the run")
        self.assertFalse(hasattr(sideas, "_child_dir"), "the shared folder is gone")
