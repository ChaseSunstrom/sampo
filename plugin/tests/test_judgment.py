"""T-0688 (Intelligence — judgment, taste and decisions): doors, implicit decisions, the cheapest version, follow-ups
answered ahead, precedent, taste from overwrites, and how often decisions get reversed."""
import json
import os
import subprocess

from helpers import ForemanTestCase, read_text, valid_m_plan

from sampo import core as c

REFS = os.path.join(c.PLUGIN_ROOT, "skills", "intake", "references")
STUB = r'''#!/usr/bin/env python3
import sys
sys.stdin.read()
print("The cheapest version that meets it: a plain CSV export from the existing table, no new UI.")
'''


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)


class DoorBase(Base):  # T-2342: doors come from the guard's parser and git; a verb only gates which untracked paths count
    def brief(self, *steps, raw="Tidy the repo", tier="S", verify="true", interp=None):
        a = ["task", "new", "Tidy data", "--type", "FEATURE", "--tier", tier, "--raw", raw, "--ac", f"{raw} :: {verify}"]
        for s in steps:
            a += ["--step", s]
        if tier == "M":
            a += ["--interpretation", interp or valid_m_plan()[0], "--approach", valid_m_plan()[1]]
        return self.fm_json(*a)["id"]

    def data(self, rel="data/old.csv", text="a,b\n1,2\n"):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def doors(self, tid):
        from sampo import doors as sdoors
        return {d["where"]: d for d in sdoors.classify(self.p, c.find_brief(self.p, tid))}


class DoorGate(DoorBase):
    def test_push_publish_and_rm_outside_steps_are_the_guards(self):
        tid = self.brief("run `git push --force origin main`", "run `npm publish`", "run `rm -rf /var/lib/appdata`",
                         "run `sed -i s/a/b/ README.md`")
        d = self.doors(tid)
        self.assertEqual([d[f"step {n}"]["door"] for n in (1, 2, 3, 4)], ["guard", "guard", "guard", "two-way"])
        self.assertTrue(d["step 3"]["cats"], "the guard's own category names it")

    def test_a_verify_command_the_guard_stops_is_the_guards_too(self):
        tid = self.brief("edit it", verify="git push --force origin main")
        self.assertEqual(self.doors(tid)["verify 1"]["door"], "guard")

    def test_a_command_the_guard_cannot_classify_fails_closed(self):
        from unittest import mock
        tid = self.brief("run `make deploy`")
        with mock.patch("fmguard.check_bash", side_effect=RuntimeError("no parse")):
            self.assertEqual(self.doors(tid)["step 1"]["door"], "guard")

    def test_the_gate_asks_the_guard_only_about_commands_that_name_a_path(self):
        # T-3677: focus put every step's and verify's command through the guard (~9 ms of its patterns compiled, a
        # call) though the gate acts only on a local door, which needs a path; the guard's own doors stay its to stop
        import fmguard
        from unittest import mock
        from sampo import doors as sdoors
        self.data()
        plain = self.brief("run `git push --force origin main`", "edit it", verify="python3 -c pass")
        named = self.brief("run `rm -rf /var/lib/appdata`", "delete data/old.csv", verify="rm -f data/old.csv")
        with mock.patch.object(fmguard, "check_bash", wraps=fmguard.check_bash) as asked:
            self.assertIsNone(sdoors.gate(self.p, plain))
            self.assertEqual(asked.call_count, 0, "no path named: no local door, nothing to ask")
            sdoors.gate(self.p, named)
            self.assertEqual(asked.call_count, 2, "a command naming a path still goes through the guard")
        self.assertEqual(c.door_marked(c.find_brief(self.p, named), "door_acs"), [1], "the verify is still a door")
        self.assertEqual(self.doors(plain)["step 1"]["door"], "guard", "classify still names the guard's doors")

    def test_a_verify_that_only_creates_a_new_file_outside_the_repo_is_two_way(self):  # T-2435
        marker = os.path.join(self.tmp, "ran")
        tid = self.brief("edit it", verify=f"touch {marker}; echo x > {marker}.2; true")
        self.assertEqual(self.doors(tid)["verify 1"]["door"], "two-way")
        self.data("test_calc.py", "print('ok')\n")
        import subprocess
        subprocess.run(["git", "-C", self.repo, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "t"], check=True, capture_output=True)
        tid = self.brief("fix calc.add", verify=f"touch {marker}; python3 test_calc.py")  # test_code's TaskRun fixture
        self.assertEqual(self.doors(tid)["verify 1"]["door"], "two-way", self.doors(tid)["verify 1"])
        open(marker, "w").close()  # once it exists, overwriting it outside the repo is one-way again
        tid = self.brief("edit it", verify=f"echo x > {marker}")
        self.assertEqual(self.doors(tid)["verify 1"]["door"], "local")
        tid = self.brief("edit it", verify=f"touch {marker}; mkdir -p {marker}.d; python3 test_calc.py")
        self.assertEqual(self.doors(tid)["verify 1"]["door"], "two-way")  # T-2435: a touch of an existing file loses nothing

    def test_a_lone_slash_between_two_words_of_a_step_is_prose_not_the_root(self):
        # T-4370 (exam s8-047 goal-ruby-f44b59): 'Write the "Releasing / Decision" section into README.md' named '/',
        # the filesystem root, which no snapshot covers: sampo focus refused the plan for a missing Undo line
        from sampo import doors as sdoors
        tid = self.brief('Write the "Releasing / Decision" section into the readme only', "update the intro and / or the summary",
                         "edit a / b / c in the table")
        d = self.doors(tid)
        self.assertEqual([d[f"step {n}"]["door"] for n in (1, 2, 3)], ["two-way"] * 3, d)
        self.assertIsNone(sdoors.gate(self.p, tid))
        self.assertEqual(c.door_marked(c.find_brief(self.p, tid)), [])

    def test_a_step_that_names_the_root_as_a_place_is_still_a_door_no_copy_covers(self):  # T-4370: fail closed
        tid = self.brief("delete / and start over", "wipe everything under /", "clean up, then delete /.",
                         "move everything from / to the backup", "remove /etc/hosts")
        d = self.doors(tid)
        self.assertEqual([(d[f"step {n}"]["door"], d[f"step {n}"]["root"]) for n in (1, 2, 3, 4)], [("local", True)] * 4, d)
        self.assertEqual((d["step 5"]["door"], d["step 5"]["named"]), ("local", ["/etc/hosts"]))

    def test_an_untracked_data_path_is_a_local_one_way_step(self):
        self.data()
        tid = self.brief("delete data/old.csv")
        d = self.doors(tid)["step 1"]
        self.assertEqual((d["door"], d["paths"]), ("local", ["data/old.csv"]))

    def test_tracked_regenerable_and_missing_paths_are_two_way_but_an_ignored_secret_is_local(self):
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("build/\n.env\nlocal.db\n")
        self.data("build/out.bin")
        self.data(".env", "KEY=1\n")
        self.data("local.db", "rows")
        tid = self.brief("delete README.md", "delete build/out.bin", "delete data/none.csv", "delete .env", "delete local.db")
        d = self.doors(tid)
        self.assertEqual([d[f"step {n}"]["door"] for n in (1, 2, 3, 4, 5)],
                         ["two-way", "two-way", "two-way", "local", "local"])

    def test_globs_vars_tilde_and_bare_words_name_the_paths_they_expand_to(self):
        from unittest import mock
        self.data("data/a.csv")
        self.data("data/b.csv")
        outside = os.path.join(self.tmp, "door-dump.txt")
        open(outside, "w").close()
        tid = self.brief("delete data/*.csv", "run `rm -rf data`", "delete $DOOR_DIR/a.csv", "wipe data", "delete ~/door-dump.txt")
        with mock.patch.dict(os.environ, {"DOOR_DIR": "data", "HOME": self.tmp}):
            d = self.doors(tid)
        self.assertEqual(sorted(d["step 1"]["paths"]), ["data/a.csv", "data/b.csv"])
        self.assertEqual(d["step 2"]["paths"], ["data"])
        self.assertEqual(d["step 3"]["paths"], ["data/a.csv"])
        self.assertEqual(d["step 4"]["paths"], ["data"])
        self.assertEqual(d["step 5"]["door"], "local")

    def test_each_way_to_change_an_untracked_file_is_a_door(self):
        self.data()
        self.data("new.txt")
        for step in ("run `sed -i s/a/b/ data/old.csv`", "run `echo x > data/old.csv`", "run `tee data/old.csv`",
                     "run `dd of=data/old.csv`", "run `cp new.txt data/old.csv`", "run `mv new.txt data/old.csv`",
                     "run `gzip data/old.csv`", "run `shred data/old.csv`", "run `truncate -s 0 data/old.csv`",
                     "edit data/old.csv", "update data/old.csv", "modify data/old.csv", "write data/old.csv"):
            with self.subTest(step=step):
                self.assertEqual(self.doors(self.brief(step))["step 1"]["door"], "local")

    def test_a_destructive_verify_command_is_a_door(self):
        self.data()
        tid = self.brief("edit it", verify="rm -f data/old.csv && test ! -e data/old.csv")
        d = self.doors(tid)["verify 1"]
        self.assertEqual((d["door"], d["paths"]), ("local", ["data/old.csv"]))

    def test_the_repo_itself_is_a_local_door_flagged_as_root(self):
        tid = self.brief("delete ./")
        d = self.doors(tid)["step 1"]
        self.assertEqual((d["door"], d.get("root")), ("local", True))

    def test_an_undo_line_must_be_a_real_restore_naming_an_existing_source(self):
        tid = self.brief("delete data/old.csv")
        b = c.find_brief(self.p, tid)
        b.meta["doors"] = [1]
        keep = os.path.join(self.tmp, "keep")
        os.makedirs(keep)
        for undo, ok in (("none", False), ("n/a", False), ("", False), ("none, the user said go", False),
                         ("cp -a /no/such/dir/. /repo/", False), (f"cp -a {keep}/. /repo/", True)):
            with self.subTest(undo=undo):
                b.edit_step(1, f"delete data/old.csv. Undo: {undo}")
                self.assertEqual(c.door_gaps(b) == [], ok)
                self.assertEqual(c.door_park(b) is None, ok)

    def test_a_request_names_a_path_only_by_whole_token_with_a_destroy_verb_and_no_negation(self):
        from sampo import doors as sdoors
        for raw, tok, want in (("update the data model", "src/data", False), ("Don't delete data/old.csv", "data/old.csv", False),
                               ("Delete data/old.csv, it is stale", "data/old.csv", True),
                               ("delete data/old.csv.bak", "data/old.csv", False), ("keep data/old.csv", "data/old.csv", False),
                               ("Tidy up. Then delete ./data/old.csv", "data/old.csv", True),
                               ("delete data/*.csv now", "data/*.csv", True)):
            with self.subTest(raw=raw, tok=tok):
                self.assertEqual(sdoors._named(raw, tok), want)

    def test_a_local_one_way_step_needs_an_undo_line(self):
        tid = self.brief("delete data/old.csv")
        b = c.find_brief(self.p, tid)
        b.meta["doors"] = [1]  # what fm focus records for a local one-way step
        self.assertTrue([g for g in c.door_gaps(b) if "Undo" in g and "step 1" in g])
        snap = os.path.join(self.tmp, "snap")  # T-2438: the undo source must exist; /snap is a snapd dir, not on every host
        os.makedirs(snap)
        b.edit_step(1, f"delete data/old.csv. Undo: cp -a {snap}/. /repo/")
        self.assertEqual(c.door_gaps(b), [])

    def test_a_local_one_way_step_needs_the_rival_read(self):
        os.environ["FOREMAN_NO_RIVAL"] = "0"
        self.data()
        tid = self.brief("overwrite data/old.csv")  # no HARD_RE verb: only the door marks it
        b = c.find_brief(self.p, tid)
        self.assertEqual(c.rival_gap(b), [])
        b.meta["doors"] = [1]
        self.assertTrue(c.rival_gap(b), "a one-way door the request didn't name asks for a cold rival")

    def test_a_reply_about_a_guard_command_is_one_way_and_a_plain_fork_two_way(self):
        from sampo import doors as sdoors
        self.assertEqual(sdoors.reply_door("Should I run `git push --force origin main`?", self.p), "one-way")
        self.assertEqual(sdoors.reply_door("Which parser should I keep, the old or the new one?", self.p), "two-way")


class DoorPark(DoorBase):
    def waits(self, tid):
        return c.waits_on_user(c.find_brief(self.p, tid), [])

    def test_a_small_untracked_step_is_snapshotted_and_its_undo_restores_it(self):
        path = self.data(text="keep,me\n")
        tid = self.brief("delete data/old.csv")
        self.fm("focus", tid)
        step = c.find_brief(self.p, tid).steps()[0].text
        self.assertIn("Undo: cp -a", step)
        os.remove(path)
        undo = step.split("Undo: ", 1)[1]
        subprocess.run(undo, shell=True, check=True)
        self.assertEqual(read_text(path), "keep,me\n")
        rows = read_text(os.path.join(self.p.dir, "decisions.md"))
        self.assertRegex(rows, r"\[confidence: \d+%\].*data/old\.csv")
        self.assertIsNone(self.waits(tid))

    def test_the_finish_receipt_tallies_each_one_way_step_and_how_it_was_undone_or_parked(self):
        # T-2346 (Mission 5 inbox): one receipt line, only when the plan had a local one-way step
        from sampo import doors as sdoors
        self.data(text="keep,me\n")
        tid = self.brief("delete data/old.csv", "write the summary")
        self.fm("focus", tid)
        out = self.fm("task", "finish", tid, "--audit", "self", "--run", "true").stdout
        self.assertRegex(out, r"Doors: 1 one-way step: step 1 undone \(cp -a ")
        plain = self.brief("write the summary")
        self.fm("focus", plain)
        self.assertNotIn("Doors:", self.fm("task", "finish", plain, "--audit", "self", "--run", "true").stdout)
        b = c.find_brief(self.p, plain)
        b.meta["doors"] = ["1"]  # a marked step with no way back: parked
        self.assertEqual(sdoors.tally(b), "Doors: 1 one-way step: step 1 parked")

    def test_the_final_report_prices_each_parked_door_by_days_and_what_waits_behind_it(self):
        # T-2347 (Mission 5 inbox): fm decide --review names each parked task, how long it waited, what it blocks
        tid = self.brief("write the summary")
        later = self.fm_json("task", "new", "Ship the summary", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                             "--step", "s", "--depends", tid)["id"]

        def park(b):
            b.meta["doors"] = ["1"]  # a marked one-way step with no Undo line: parked
        from sampo import cli
        cli.mutate(self.p, tid, park, "door_gate")
        c.log_event(self.p, "door_park", task=tid, data={"door": "one-way", "step": "write the summary"})
        out = self.fm("decide", "--review").stdout
        self.assertRegex(out, rf"Parked at a one-way door: {tid} \(step 1: write the summary\), 0 days, blocks {later}")
        self.assertEqual([d["id"] for d in self.fm_json("decide", "--review")["doors_parked"]], [tid])
        cli.mutate(self.p, tid, lambda b: b.meta.update(doors=[]), "door_gate")
        self.assertNotIn("Parked at a one-way door", self.fm("decide", "--review").stdout)

    def test_a_snapshot_that_does_not_verify_is_not_an_undo(self):
        from unittest import mock
        from sampo import doors as sdoors
        self.data()
        tid = self.brief("delete data/old.csv")
        with mock.patch.object(sdoors, "_same", return_value=False):
            parked = sdoors.gate(self.p, tid)
        self.assertEqual(parked, "one-way and ambiguous: step 1: delete data/old.csv")
        self.assertNotIn("Undo:", c.find_brief(self.p, tid).steps()[0].text)

    def test_over_the_cap_and_unasked_parks_the_task_for_the_final_report(self):
        self.data(text="x" * 400)
        tid = self.brief("delete data/old.csv", raw="Tidy the repo")
        r = self.fm("focus", tid, check=False, env={"FM_DOOR_CAP": "100"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.waits(tid), "one-way and ambiguous: step 1: delete data/old.csv")
        review = self.fm("decide", "--review").stdout
        self.assertIn("delete data/old.csv", review)
        self.assertIn("[costly]", review)
        self.assertNotEqual(self.fm("focus", tid, check=False, env={"FM_DOOR_CAP": "100"}).returncode, 0, "still parked")

    def test_a_parked_task_the_gate_can_now_snapshot_focuses(self):
        # T-3583: focus refuses on the peek; the door gate re-marks the doors plan_gaps reads, so it runs first
        self.data(text="x" * 400)
        tid = self.brief("delete data/old.csv")
        self.assertNotEqual(self.fm("focus", tid, check=False, env={"FM_DOOR_CAP": "100"}).returncode, 0)
        self.assertEqual(self.waits(tid), "one-way and ambiguous: step 1: delete data/old.csv")
        self.fm("focus", tid)  # the default cap holds the file: snapshotted, an Undo line, no longer parked
        self.assertEqual(c.find_brief(self.p, tid).status, "active")
        self.assertIn("Undo: cp -a", c.find_brief(self.p, tid).steps()[0].text)

    def test_an_ambiguous_brief_parks_even_when_the_request_names_the_path(self):
        self.data(text="x" * 400)
        tid = self.brief("delete data/old.csv", raw="Delete data/old.csv", tier="M")
        self.fm("focus", tid, check=False, env={"FM_DOOR_CAP": "100"})
        self.assertEqual(self.waits(tid), "one-way and ambiguous: step 1: delete data/old.csv")
        self.assertIn("Reading 1", self.fm("decide", "--review").stdout, "both readings go to the final report")

    def test_the_requests_own_words_let_an_unambiguous_step_run_as_a_costly_decision(self):
        self.data(text="x" * 400)
        tid = self.brief("delete data/old.csv", raw="Delete data/old.csv, it is stale")
        self.fm("focus", tid, env={"FM_DOOR_CAP": "100"})
        self.assertIsNone(self.waits(tid))
        self.assertEqual(c.find_brief(self.p, tid).status, "active")
        self.assertIn("[costly]", self.fm("decide", "--review").stdout)

    def test_a_rival_split_makes_a_named_step_ambiguous_so_it_parks(self):  # T-2424: brief 8 reads rival_split
        self.data(text="x" * 400)
        one = ("Reading 1: delete the stale data/old.csv. There is no other honest reading, because the request names "
               "the file.")
        plain = self.brief("delete data/old.csv", raw="Delete data/old.csv, it is stale", tier="M", interp=one)
        self.fm("focus", plain, env={"FM_DOOR_CAP": "100"})
        self.assertIsNone(self.waits(plain), "one written reading, no split: it runs as a costly decision")
        self.fm("task", "drop", plain, "next", "--force")
        self.data(text="x" * 400)
        tid = self.brief("delete data/old.csv", raw="Delete data/old.csv, it is stale", tier="M", interp=one)
        b = c.find_brief(self.p, tid)
        b.meta["rival_split"] = True
        c.save_brief(self.p, b)
        self.fm("focus", tid, check=False, env={"FM_DOOR_CAP": "100"})
        self.assertEqual(self.waits(tid), "one-way and ambiguous: step 1: delete data/old.csv")

    def test_a_verify_deleting_a_file_outside_the_repo_gets_a_copy_and_the_task_starts(self):
        # T-4377, live (exam s8-048 goal-c-0b1e05): the planner's verify was 'gcc … -o /tmp/app_test && /tmp/app_test
        # && rm -f /tmp/app_test'; sampo code's probe ran it, the tests failed before the rm, the binary stayed, and
        # focus refused the plan for 'a verify that doesn't delete untracked data': the snapshot said 'outside the
        # repo' without looking at the file
        out = os.path.join(self.tmp, "app_test")
        with open(out, "wb") as f:
            f.write(b"\x7fELF left by a run whose tests failed")
        tid = self.brief("edit it", verify=f"test -s {out} && rm -f {out}")
        self.assertEqual(self.doors(tid)["verify 1"]["door"], "local", "still a door: the file is there and not git's")
        self.fm("focus", tid)
        self.assertIsNone(self.waits(tid))
        b = c.find_brief(self.p, tid)
        self.assertEqual(b.status, "active")
        self.assertEqual(c.door_marked(b, "door_acs"), [1], "marked, and covered by its copy")
        kept = os.path.join(self.p.dir, "door-snapshots", f"{tid}-verify1.out", out.lstrip("/"))
        with open(kept, "rb") as f:
            self.assertEqual(f.read(), b"\x7fELF left by a run whose tests failed")
        self.assertIn(out, read_text(os.path.join(self.p.dir, "decisions.md")))

    def test_the_undo_puts_an_outside_path_back_in_its_own_place(self):  # T-4377
        outside = os.path.join(self.tmp, "elsewhere.dat")
        folder = os.path.join(self.tmp, "elsewhere.d")
        os.makedirs(os.path.join(folder, "sub"))
        for path, text in ((outside, "x\x00y"), (os.path.join(folder, "sub", "a.txt"), "keep me")):
            with open(path, "w") as f:
                f.write(text)
        inside = self.data(text="in,side\n")
        tmp_mode = os.stat(self.tmp).st_mode & 0o7777
        tid = self.brief(f"delete {outside}", f"delete {folder}", f"delete data/old.csv and {outside}")
        self.fm("focus", tid)
        steps = [s.text for s in c.find_brief(self.p, tid).steps()]
        for s in steps:
            self.assertIn("Undo: cp -a", s)
            self.assertTrue(c.undo_ok(s), s)
        self.assertIsNone(self.waits(tid))
        before = sorted(os.path.join(dp, n) for dp, _, fn in os.walk(self.repo) if ".git" not in dp for n in fn)
        import shutil
        os.remove(outside)
        shutil.rmtree(folder)
        subprocess.run(steps[1].split("Undo: ", 1)[1], shell=True, check=True, cwd=self.repo)  # the folder
        self.assertEqual(read_text(os.path.join(folder, "sub", "a.txt")), "keep me")
        self.assertEqual(os.listdir(folder), ["sub"], "put back as itself, not nested into itself")
        with open(os.path.join(folder, "sub", "new.txt"), "w") as f:
            f.write("made since")
        subprocess.run(steps[1].split("Undo: ", 1)[1], shell=True, check=True, cwd=self.repo)  # over a folder that is there
        self.assertEqual(sorted(os.listdir(os.path.join(folder, "sub"))), ["a.txt", "new.txt"])
        self.assertEqual(os.listdir(folder), ["sub"])
        os.remove(inside)
        subprocess.run(steps[2].split("Undo: ", 1)[1], shell=True, check=True, cwd=os.path.dirname(self.repo))  # from any folder
        self.assertEqual(read_text(outside), "x\x00y")
        self.assertEqual(read_text(inside), "in,side\n")
        after = sorted(os.path.join(dp, n) for dp, _, fn in os.walk(self.repo) if ".git" not in dp for n in fn)
        self.assertEqual(after, before, "nothing of an outside copy lands in the repo")
        self.assertEqual(os.stat(self.tmp).st_mode & 0o7777, tmp_mode, "a parent folder's mode is left alone")

    def test_an_outside_still_refused_path_says_why(self):  # T-4377: what a copy can't cover parks as before
        big = os.path.join(self.tmp, "big.dat")
        with open(big, "w") as f:
            f.write("x" * 400)
        tid = self.brief(f"delete {big}")
        self.assertNotEqual(self.fm("focus", tid, check=False, env={"FM_DOOR_CAP": "100"}).returncode, 0)
        self.assertEqual(self.waits(tid), f"one-way and ambiguous: step 1: delete {big}")
        self.assertIn("over the snapshot cap", read_text(os.path.join(self.p.dir, "decisions.md")))
        pipe = os.path.join(self.tmp, "pipe")
        os.mkfifo(pipe)
        tid = self.brief(f"delete {pipe}")
        self.fm("focus", tid, check=False, timeout=10)
        self.assertEqual(self.waits(tid), f"one-way and ambiguous: step 1: delete {pipe}")
        self.assertIn("FIFO", read_text(os.path.join(self.p.dir, "decisions.md")))
        above = os.path.dirname(self.repo)
        tid = self.brief(f"delete {above}", raw=f"Delete {above} and start over")
        self.assertNotEqual(self.fm("focus", tid, check=False).returncode, 0)
        self.assertEqual(self.waits(tid), f"one-way and ambiguous: step 1: delete {above}")
        from sampo import doors as sdoors
        odd = os.path.join(self.tmp, "kept.d", "..")  # a name that is not the path's own: no place to keep its copy by
        os.makedirs(os.path.join(self.tmp, "kept.d"))
        undo, why = sdoors._snapshot(self.p, "T-0001", "step9", [odd])
        self.assertIsNone(undo)
        self.assertIn("not a plain path", why)
        if os.geteuid():  # root reads anything
            locked = os.path.join(self.tmp, "locked.dat")
            with open(locked, "w") as f:
                f.write("x")
            os.chmod(locked, 0)
            tid = self.brief(f"delete {locked}")
            self.fm("focus", tid, check=False)
            self.assertEqual(self.waits(tid), f"one-way and ambiguous: step 1: delete {locked}")
            self.assertFalse(os.path.lexists(os.path.join(self.p.dir, "door-snapshots", f"{tid}-step1.out")),
                             "a copy that failed leaves nothing behind")

    def test_an_undo_line_the_user_adds_lifts_the_park(self):
        self.data(text="x" * 400)
        tid = self.brief("delete data/old.csv")
        self.fm("focus", tid, check=False, env={"FM_DOOR_CAP": "100"})
        for fake in ("none, the user said go", "n/a", ""):
            self.fm("task", "step", tid, "edit", "1", f"delete data/old.csv. Undo: {fake}")
            self.assertIsNotNone(self.waits(tid), f"Undo: {fake!r} is not a way back")
        keep = os.path.join(self.tmp, "keep")
        os.makedirs(keep)
        self.fm("task", "step", tid, "edit", "1", f"delete data/old.csv. Undo: cp -a {keep}/. ./")
        self.assertIsNone(self.waits(tid))

    def test_a_destructive_verify_command_is_snapshotted_or_parks(self):
        self.data(text="x" * 400)
        tid = self.brief("edit it", verify="rm -f data/old.csv && test ! -e data/old.csv")
        self.fm("focus", tid, check=False, env={"FM_DOOR_CAP": "100"})
        self.assertEqual(self.waits(tid), "one-way and ambiguous: verify 1: rm -f data/old.csv && test ! -e data/old.csv")
        self.fm("focus", tid)
        self.assertIsNone(self.waits(tid))
        self.assertTrue(os.path.exists(os.path.join(self.p.dir, "door-snapshots", f"{tid}-verify1", "data", "old.csv")))

    def test_a_step_added_to_an_active_task_meets_the_gate_too(self):
        self.data(text="x" * 400)
        tid = self.brief("read it")
        self.fm("focus", tid)
        r = self.fm("task", "step", tid, "add", "delete data/old.csv", check=False, env={"FM_DOOR_CAP": "100"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Undo", r.stderr)
        self.assertIsNotNone(self.waits(tid))
        self.fm("task", "step", tid, "add", "delete data/old.csv")  # small cap not set: the gate snapshots it
        self.assertIn("Undo: cp -a", c.find_brief(self.p, tid).steps()[-1].text)

    def test_the_repo_itself_parks_even_when_the_request_names_it(self):
        tid = self.brief("delete ./", raw="Delete ./ and start over")
        self.assertNotEqual(self.fm("focus", tid, check=False).returncode, 0)
        self.assertEqual(self.waits(tid), "one-way and ambiguous: step 1: delete ./")

    def test_a_fifo_is_not_read_and_the_step_parks(self):
        os.makedirs(os.path.join(self.repo, "data"))
        os.mkfifo(os.path.join(self.repo, "data", "pipe"))
        tid = self.brief("delete data/pipe")
        self.fm("focus", tid, check=False, timeout=10)
        self.assertEqual(self.waits(tid), "one-way and ambiguous: step 1: delete data/pipe")
        self.assertIn("FIFO", read_text(os.path.join(self.p.dir, "decisions.md")))

    def test_an_error_in_the_gate_is_a_policy_error_naming_the_cause(self):
        from unittest import mock
        from sampo import doors as sdoors
        tid = self.brief("delete data/old.csv")
        with mock.patch.object(sdoors, "gate", side_effect=RuntimeError("git exploded")):
            with self.assertRaisesRegex(c.PolicyError, "RuntimeError: git exploded"):
                sdoors.check(self.p, tid)

    def test_step_done_warns_when_the_data_differs_from_its_snapshot(self):
        path = self.data(text="keep,me\n")
        tid = self.brief("delete data/old.csv")
        self.fm("focus", tid)
        with open(path, "w") as f:
            f.write("changed after the snapshot\n")
        r = self.fm("task", "step", tid, "done", "1", "--evidence", "true", "ok")
        self.assertIn("data/old.csv differs from its snapshot", r.stderr)

    def test_a_snapshot_is_not_dropped_by_a_step_edit_that_changes_nothing(self):
        self.data()
        tid = self.brief("delete data/old.csv")
        self.fm("focus", tid)
        n = len(read_text(os.path.join(self.p.dir, "decisions.md")).splitlines())
        self.fm("task", "step", tid, "edit", "1", c.find_brief(self.p, tid).steps()[0].text)
        self.assertEqual(len(read_text(os.path.join(self.p.dir, "decisions.md")).splitlines()), n)

    def test_a_second_focus_does_not_snapshot_again(self):
        self.data()
        tid = self.brief("delete data/old.csv")
        self.fm("focus", tid)
        n = len(read_text(os.path.join(self.p.dir, "decisions.md")).splitlines())
        self.fm("focus", tid)
        self.assertEqual(len(read_text(os.path.join(self.p.dir, "decisions.md")).splitlines()), n)

    def test_the_digest_counts_parks_and_asks_by_door(self):
        self.data(text="x" * 400)
        tid = self.brief("delete data/old.csv")
        self.fm("focus", tid, check=False, env={"FM_DOOR_CAP": "100"})
        self.assertRegex(self.fm("digest").stdout, r"Doors: .*parks: 1 one-way")


class Implicit(Base):
    def test_reviews_ask_for_the_decisions_taken_by_default(self):
        self.assertIn("implicit decisions", read_text(os.path.join(REFS, "audit.md")).lower())
        self.assertIn("implicit decisions", read_text(os.path.join(c.PLUGIN_ROOT, "agents", "fm-reviewer.md")).lower())


class Cheapest(Base):
    def test_a_child_argues_the_cheapest_version_into_the_brief(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.fm("task", "new", "Export reports", "--type", "FEATURE", "--tier", "M", "--ac", "ok :: true", "--step", "s")
        self.fm("second", "cheapest", "T-0001", env={"PATH": bindir + os.pathsep + os.environ["PATH"]})
        self.assertIn("plain CSV export", c.find_brief(self.p, "T-0001").section("Cheapest version"))


class DecisionFileClosed(Base):  # T-2426: writing a decision closes the decisions.md it read
    def test_two_decisions_leave_no_unclosed_file(self):
        import gc
        import warnings
        from sampo import cli
        with warnings.catch_warnings(record=True) as got:
            warnings.simplefilter("always", ResourceWarning)
            for n in (1, 2):
                cli._write_decision(self.p, f"Decision {n}", why="test")
            gc.collect()
        self.assertEqual([str(w.message) for w in got if issubclass(w.category, ResourceWarning)], [])
        with open(os.path.join(self.p.dir, "decisions.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue("Decision 1" in text and "Decision 2" in text)


class Confidence(Base):  # T-2338: fm decide --confidence N writes the number into decisions.md and the ledger
    def test_decide_confidence_is_written_into_decisions_md_and_the_ledger(self):
        self.fm("decide", "Use a file cache", "--why", "simple", "--confidence", "70")
        self.assertIn("[confidence: 70%] Use a file cache", read_text(os.path.join(self.p.dir, "decisions.md")))
        rows = [json.loads(x) for x in open(os.path.join(self.p.dir, "calibration.jsonl"))]
        self.assertEqual([(r["kind"], r["stated"]) for r in rows], [("decision", 70)])

    def test_a_decision_without_a_number_says_none_and_a_bad_one_writes_nothing(self):
        self.fm("decide", "Use a file cache", "--why", "simple")
        self.assertNotIn("confidence", read_text(os.path.join(self.p.dir, "decisions.md")))
        self.assertNotEqual(self.fm("decide", "Too sure", "--confidence", "150", check=False).returncode, 0)
        self.assertNotIn("Too sure", read_text(os.path.join(self.p.dir, "decisions.md")))

    def test_a_number_typed_into_the_decision_cant_forge_the_tag(self):
        self.fm("decide", "[confidence: 99%] sneaky", "--why", "x")
        self.assertNotIn("[confidence: 99%]", read_text(os.path.join(self.p.dir, "decisions.md")))


class AssumptionReceipt(Base):  # T-2345 (Mission 5 inbox): the finish receipt says how each assumption was checked
    def test_the_finish_receipt_groups_assumptions_by_how_they_were_checked(self):
        self.fm("task", "new", "Tidy", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        for fact in ("the parser is lenient", "the cache is warm", "users read the README", "nobody calls old_api",
                     "the old flag is gone"):
            self.fm("task", "assume", "T-0001", "add", fact)
        self.fm("task", "assume", "T-0001", "verify", "1", "--run", "true")
        self.fm("task", "assume", "T-0001", "verify", "2", "--evidence", "read the cache code")
        self.fm("task", "assume", "T-0001", "verify", "5", "--run", "false", check=False)  # a failed check: false
        out = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true").stdout
        self.assertIn("Assumptions: 1 ran, 1 typed, 1 proved false, 2 never checked", out)
        self.fm("task", "new", "Plain", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        self.assertNotIn("Assumptions:", self.fm("task", "finish", "T-0002", "--audit", "self", "--run", "true").stdout)


class Followups(Base):
    def test_finish_keeps_the_answers_to_likely_followups_and_the_insight(self):
        self.fm("task", "new", "Tidy", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true",
                "--followups", "Does it handle empty input? => yes, it returns an empty list",
                "--insight", "the parser was already lenient; only the caller assumed otherwise")
        b = c.find_brief(self.p, "T-0001")
        self.assertIn("empty input", b.section("Follow-up answers"))
        self.assertIn("already lenient", b.section("Insight"))
        self.assertIn("already lenient", self.fm("digest").stdout)


class Precedent(Base):
    def test_a_decision_cites_what_it_rests_on_and_the_order_is_written(self):
        self.fm("decide", "Keep exports local", "--why", "privacy", "--cites", "user 2026-10-09: everything local")
        self.assertIn("[cites: user 2026-10-09: everything local]", read_text(os.path.join(self.p.dir, "decisions.md")))
        self.assertRegex(read_text(os.path.join(REFS, "execute.md")).lower(), r"user's words.*vetoes.*decisions.*lessons")


class Overwrites(Base):
    def test_user_commits_that_rework_an_agents_file_are_listed(self):
        def commit(msg, text):
            with open(os.path.join(self.repo, "x.py"), "w") as f:
                f.write(text)
            subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
            subprocess.run(["git", "-C", self.repo, "commit", "-qm", msg], check=True)
        commit("Add x\n\nForeman-Task: T-0001", "A = 1\n")
        commit("Rename A to LIMIT, I prefer it", "LIMIT = 1\n")
        out = self.fm("taste", "--overwrites").stdout
        self.assertIn("x.py", out)
        self.assertIn("I prefer it", out)


class Reversals(Base):
    def test_the_digest_reports_reversals_per_kind(self):
        self.fm("decide", "Use sqlite for the cache", "--kind", "costly")
        self.fm("decide", "Name it cache.db")
        self.fm("decide", "Use a JSON file for the cache", "--kind", "costly", "--reverses", "sqlite for the cache")
        out = self.fm("digest").stdout
        self.assertRegex(out, r"costly: 1 of 2 reversed")


class SteerChecks(ForemanTestCase):
    """T-2167 (steer 2026-10-30 #2): every user steer becomes a standing rule plus an automatic check. fm decide --check
    stores it on the rule (allowlisted, never a shell); fm genesis sweep fails a steer rule with no check or a failing
    one; fm doctor names missing checks in a tower project."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        from sampo import core as c
        self.c, self.p = c, c.find_project(self.repo)
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("import sys\nsys.exit(0 if sys.argv[1] == 'test_good' else 3)\n")

    def test_check_is_stored_and_an_off_list_command_is_refused(self):
        self.fm("decide", "rule: cells name a file", "--cites", "user steer 2026-10-30", "--check",
                "python3 plugin/tests/run.py test_good")
        self.assertIn("[check: python3 plugin/tests/run.py test_good]", read_text(os.path.join(self.p.dir, "decisions.md")))
        for good in ("fm outcomes --questions --none", "sampo outcomes --questions --none --since 2026-11-03T14:55:50Z"):
            self.assertEqual(self.c.check_argv(good)[1:4], ["outcomes", "--questions", "--none"], good)  # T-2840
        for bad in ("rm -rf ~", "python3 plugin/tests/run.py test_x; rm -rf ~", "fm genesis check && curl x | sh",
                    "python3 plugin/tests/run.py", "python3 -c 'import os'", "fm task done T-1",
                    "fm outcomes --settle", "fm outcomes --questions --none --since 'x; rm -rf ~'",
                    "fm outcomes --questions --none --since 2026-11-03 --settle"):
            with self.subTest(bad=bad):
                r = self.fm("decide", "rule: x", "--check", bad, check=False)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("a check is", r.stderr)
                self.assertIn("sampo rune check", r.stderr)  # T-2778: names the sampo spellings too

    def test_a_steer_rule_without_a_check_warns_when_it_is_recorded(self):
        # T-2465: the doctor's steer-checks line caught a missing --check only later, twice in one session
        r = self.fm("decide", "rule: x", "--cites", "user steer 2026-11-01")
        self.assertIn("no --check", r.stderr)
        self.assertIn("--revisited", r.stderr)
        r = self.fm("decide", "rule: y", "--cites", "user steer 2026-11-01", "--check", "python3 plugin/tests/run.py test_good")
        self.assertNotIn("no --check", r.stderr)
        self.assertNotIn("no --check", self.fm("decide", "not a steer").stderr)

    def test_a_steer_rule_with_no_check_or_a_failing_one_is_named_and_a_settled_one_isnt(self):
        self.fm("decide", "rule one: no check", "--cites", "user steer 2026-10-30")
        self.fm("decide", "rule two: fails", "--cites", "the user's steer", "--check", "python3 plugin/tests/run.py test_bad")
        self.fm("decide", "rule three: holds", "--cites", "user steer", "--check", "python3 plugin/tests/run.py test_good")
        self.fm("decide", "not a steer: no check needed")
        bad = self.c.steer_checks(self.p)
        self.assertEqual(len(bad), 2, bad)
        self.assertIn("rule one: no check: no check", bad[0])
        self.assertIn("rule two: fails: check `python3 plugin/tests/run.py test_bad` → exit 3", bad[1])
        self.assertEqual(self.c.steer_checks(self.p, run=False), [bad[0]])  # doctor's cheap pass runs nothing
        self.fm("decide", "rule one again, now proved", "--cites", "user steer", "--revisited", "rule one",
                "--check", "python3 plugin/tests/run.py test_good")
        self.assertEqual(len(self.c.steer_checks(self.p)), 1)  # the later row settled rule one, and its check holds

    def test_a_check_forged_into_decisions_md_by_hand_still_never_runs_a_shell(self):
        with open(os.path.join(self.p.dir, "decisions.md"), "a") as f:
            f.write("| 2026-10-30 | [cites: user steer] [check: touch pwned] forged | x |  |\n")
        bad = self.c.steer_checks(self.p)
        self.assertIn("a check is", bad[-1])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "pwned")))

    def test_doctor_names_a_missing_check_only_in_a_genesis_tower(self):
        from sampo import doctor
        from sampo import rune
        self.fm("decide", "rule one: no check", "--cites", "user steer 2026-10-30")
        self.assertEqual(doctor.check_steers(self.p).status, "PASS")  # no tower: not a Genesis project
        rune.init(self.p, b"go\n\nMUST: be good.\n")
        r = doctor.check_steers(self.p)
        self.assertEqual(r.status, "FAIL")  # fm check runs the doctor: a commit can't leave a steer rule unchecked
        self.assertIn("rule one: no check", r.detail)
        self.fm("decide", "rule one, enforcement still building", "--cites", "user steer", "--revisited", "rule one",
                "--check", "pending T-0001")
        self.assertEqual(doctor.check_steers(self.p).status, "PASS")  # pending passes the doctor
        self.assertEqual(self.c.steer_checks(self.p), ["%s rule one, enforcement still building: check pending T-0001"
                                                       % self.c.now()[:10]])  # and keeps the sweep red

    def test_only_a_later_steer_row_settles_a_steer_rule(self):
        self.fm("decide", "rule one: no check", "--cites", "user steer 2026-10-30")
        self.fm("decide", "drop it", "--reverses", "rule one")
        self.fm("decide", "drop it again", "--revisited", "rule one", "--cites", "my own judgment")
        self.assertEqual(len(self.c.steer_checks(self.p)), 1)  # the session can't retire the user's rule by itself

    def test_an_fm_check_runs_fm_itself_and_a_failure_says_why(self):
        self.fm("decide", "rule: the tower is whole", "--cites", "user steer", "--check", "fm genesis check")
        bad = self.c.steer_checks(self.p)
        self.assertEqual(len(bad), 1, bad)
        self.assertRegex(bad[0], r"check `fm genesis check` → exit [1-9]\d*: \S")  # fm's own words, no tower here
        self.assertNotIn("can't open file", bad[0])

    def test_cites_that_almost_name_a_steer_warn(self):
        r = self.fm("decide", "rule: x", "--cites", "steer 2026-10-30 #2")
        self.assertIn("doesn't read as a user steer", r.stderr)
        self.assertNotIn("doesn't read as a user steer",  # T-2465: it reads as one (and warns only that it has no check)
                         self.fm("decide", "rule: y", "--cites", "the user's steer 2026-10-30").stderr)


class ShipBets(Base):  # T-2496: a ship bet is scored in hours by CI's blocking jobs, apart from the bars
    def test_a_ship_bet_is_written_once_per_commit_and_settled_by_ci_s_verdict(self):
        from sampo import outcomes as o
        tid = json.loads(self.fm("task", "new", "Ship it", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        green, red = "a" * 40, "b" * 40
        row = o.ship_bet(self.p, 90, tid, green)
        self.assertEqual((row["kind"], row["stated"], row["key"], row["base"]), ("ship", 90, green[:12], None))
        self.assertIsNone(o.ship_bet(self.p, 70, tid, green))  # write-once per commit: a restated number is ignored
        self.assertEqual([(r["kind"], r["outcome"]) for r in o.scored(self.p)], [("ship", None)])
        c.log_event(self.p, "ci_verdict", data={"commit": green, "green": True})
        self.assertEqual(o.maintain(self.p), 1)  # T-2520: it says how many outcome rows it wrote
        self.assertEqual(o.maintain(self.p), 0)
        (r,) = o.scored(self.p)
        self.assertEqual((r["outcome"], r["src"]), ("held", "ci"))
        self.assertEqual(o.bar_rows(o.scored(self.p)), [])  # CI scores the kind, never moves a bar
        self.assertEqual(o.ship_bet(self.p, 80, tid, red)["base"], 100)  # the held share of earlier settled ships
        c.log_event(self.p, "ci_verdict", data={"commit": red, "green": False})
        self.assertEqual({r["key"]: r["outcome"] for r in o.scored(self.p)}, {green[:12]: "held", red[:12]: "missed"})
        with self.assertRaises(ValueError):
            o.ship_bet(self.p, 150, tid, "c" * 40)
        kinds = o.ledger_data(self.p)["kinds"]  # T-2519: the scored ship bets show in the buckets the user reads
        self.assertEqual([(b["lo"], b["n"], b["held"]) for b in kinds.get("ship", [])], [(80, 2, 1)])
        self.assertEqual(o.bar_rows(o.scored(self.p)), [], "still no bar moves on them")


class BaseRateHeld(Base):  # T-2423: the base rate counts held as the bet scorer does
    def test_a_correction_or_a_smoke_failure_is_no_miss_in_the_base_rate(self):
        from sampo import outcomes as o
        outs = {f"T-{i}": {"type": "FIX", "tier": "S", "fate": f}
                for i, f in enumerate(("held", "smoke failed after", "corrected", "fixed later"))}
        self.assertEqual(o.base_rate(outs, {}, "FIX", "S"), 75)
        self.assertEqual(o.base_rate(outs, {"T-0": False}, "FIX", "S"), 50, "a failed first finish still misses")


class BaseRateNote(Base):  # T-2497 (Mission 2, #8): a plan's percent against its base rate, said while it is stated
    def plan(self, pct):
        tid = self.fm_json("task", "new", "Fix the parser", "--type", "FIX", "--tier", "S", "--step", "x",
                           "--ac", "fixed :: true")["id"]
        return tid, self.fm("task", "set", tid, "--section", "Approach (options → choice → why)", "--text",
                            f"1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z "
                            f"Choice 1 ({pct}%): it holds").stdout

    def test_a_choice_far_from_its_base_rate_is_named_with_both_numbers(self):
        from sampo import outcomes
        from unittest import mock
        tid, _ = self.plan(75)
        b = c.find_brief(self.p, tid)
        with mock.patch.object(outcomes, "base_rate", return_value=96):
            note = outcomes.base_note(self.p, b)
        self.assertIn("75%", note)
        self.assertIn("96%", note)
        self.assertIn("21 points under", note)
        with mock.patch.object(outcomes, "base_rate", return_value=85):
            self.assertEqual(outcomes.base_note(self.p, b), "")  # within 20 points
        with mock.patch.object(outcomes, "base_rate", return_value=None):
            self.assertEqual(outcomes.base_note(self.p, b), "")  # no history

    def test_the_note_is_kept_at_focus_and_shown_by_fm_next(self):
        tid, _ = self.plan(75)
        self.fm("focus", tid)  # focus computes the note (none here: no history) and keeps it in the brief
        b = c.find_brief(self.p, tid)
        b.meta["base_note"] = "Calibration: this plan states Choice 75% where FIX S closes held 96%"
        c.save_brief(self.p, b)
        self.assertIn("Choice 75% where FIX S closes held 96%", self.fm("next").stdout)


class ShipNote(Base):  # T-2611 (Mission 2, #8): the ship record meets the stated chance where it is typed
    def settled(self, n, held):
        """n ship bets settled by CI's verdict, `held` of them green."""
        from sampo import outcomes as o
        tid = json.loads(self.fm("task", "new", "Ship them", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        for i in range(n):
            sha = f"{i:012x}" + "0" * 28  # a bet is keyed by the commit's first 12 characters
            o.ship_bet(self.p, 85, tid, sha)
            c.log_event(self.p, "ci_verdict", data={"commit": sha, "green": i < held})
        return tid

    def test_a_chance_far_from_twenty_settled_ship_bets_names_the_record(self):
        from sampo import outcomes as o
        self.settled(21, 21)
        self.assertEqual(o.ship_note(self.p, 85), "Ship record: 21 of 21 ship bets held (100%); 85% sits 15 points "
                                                  "under it. State what you believe; fm ci-watch scores it.")
        self.assertEqual(o.ship_note(self.p, 90), "", "within 15 points: nothing to say")

    def test_under_twenty_settled_says_nothing(self):
        from sampo import outcomes as o
        self.settled(19, 19)
        self.assertEqual(o.ship_note(self.p, 50), "")

    def test_an_overconfident_chance_reads_over(self):
        from sampo import outcomes as o
        self.settled(20, 10)
        self.assertIn("95% sits 45 points over it", o.ship_note(self.p, 95))
