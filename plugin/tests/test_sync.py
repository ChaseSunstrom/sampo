"""fm sync: an opt-in, committed mirror of the project's Sampo state (.sampo/) that another clone imports (T-0022).
Authorization never travels: grants are stripped on export and ignored on import."""
import os
import re
import subprocess

from helpers import ForemanTestCase, read_text

from sampo import core as c
import fmguard as g


class Sync(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = self.fm_json("task", "new", "Add export", "--type", "FEATURE", "--tier", "S", "--step", "build it",
                                "--ac", "exports")["id"]
        self.fm("task", "set", self.tid, "--allow", "publish")
        self.fm("task", "set", self.tid, "approved=true")
        self.fm("decide", "CSV over XLSX (no new dependency)", "--task", self.tid)
        self.fm("research", "add", "csv-notes", input="# CSV\nRFC 4180 quoting.\n")
        self.mirror = os.path.join(self.repo, ".sampo")

    def exported(self):
        path = next(os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.mirror, "tasks")) for f in fs
                    if f.startswith(self.tid))
        return read_text(path)

    def test_export_mirrors_briefs_decisions_and_research_without_authorization(self):
        self.fm("sync", "on")
        text = self.exported()
        self.assertIn("Add export", text)
        self.assertNotRegex(text, r"(?m)^allow:|^approved:")
        self.assertIn("CSV over XLSX", read_text(os.path.join(self.mirror, "decisions.md")))
        self.assertIn("RFC 4180", read_text(os.path.join(self.mirror, "research", "csv-notes.md")))
        self.assertIn("`sampo sync`", read_text(os.path.join(self.mirror, "README.md")))

    def test_an_unchanged_export_reads_nothing(self):
        # T-3349: every state write re-read, re-parsed and re-redacted every mirrored file under the project lock
        from unittest import mock
        from sampo import sync
        self.fm("sync", "on")
        p = c.find_project(self.repo)
        reads, real = [], sync._read
        with mock.patch.object(sync, "_read", side_effect=lambda path: reads.append(path) or real(path)):
            self.assertEqual(sync.export(p), 0)
        self.assertEqual(reads, [])
        self.fm("task", "log", self.tid, "picked the csv module")  # a changed brief is exported again
        self.assertIn("picked the csv module", self.exported())
        path = os.path.join(self.mirror, "decisions.md")  # a pulled change is still left for the import
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n- theirs\n")
        sync.export(p)
        self.assertIn("- theirs", read_text(path))

    def test_the_mirror_follows_every_change_and_stops_when_off(self):
        self.fm("sync", "on")
        self.fm("task", "log", self.tid, "picked the csv module")
        self.assertIn("picked the csv module", self.exported())
        self.fm("sync", "off")
        self.fm("task", "log", self.tid, "after sync off")
        self.assertNotIn("after sync off", self.exported())
        self.fm("sync", "off", "--remove")
        self.assertFalse(os.path.exists(self.mirror))

    def test_another_machine_imports_it_without_grants(self):
        self.fm("sync", "on")
        other = os.path.join(self.tmp, "other-home")
        os.makedirs(other)
        self.hook("SessionStart", {"source": "startup"}, env={"FOREMAN_HOME": other})
        os.environ["FOREMAN_HOME"] = other
        p = c.find_project(self.repo)
        b = c.find_brief(p, self.tid)
        self.assertEqual(b.title, "Add export")
        self.assertEqual((b.meta.get("allow") or [], b.meta.get("approved")), ([], None))
        self.assertTrue(c.read_meta(p).get("sync"), "a repo with .sampo/ keeps syncing on the new machine")
        self.assertIn("CSV over XLSX", read_text(os.path.join(p.dir, "decisions.md")))

    def test_a_newer_pulled_brief_updates_the_local_one_but_never_its_grants(self):
        self.fm("sync", "on")
        path = next(os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.mirror, "tasks")) for f in fs)
        text = read_text(path)
        text = re.sub(r"(?m)^updated: .*$", "updated: 2999-01-01T00:00:00Z", text)  # a collaborator's later change
        text = text.replace("---\n#", "allow: [core, plugin]\nplugin_pin: [evil@m, 0000, 9999999999]\n---\n#", 1) \
            + "- 2999-01-01T00:00:00Z pulled note\n"
        with open(path, "w") as f:
            f.write(text)
        out = self.fm("sync", "import").stdout
        self.assertIn("1 updated", out)
        b = c.find_brief(c.find_project(self.repo), self.tid)
        self.assertIn("pulled note", b.render())
        self.assertEqual(b.meta.get("allow"), ["publish"], "local grants stay; the repo's are ignored")
        self.assertNotIn("plugin_pin", b.meta, "a pinned plugin yes (T-0036) never comes from the repo either")

    def pulled(self, text_fn, rel=None):
        """Change the mirror the way a git pull would."""
        path = rel and os.path.join(self.mirror, rel) or next(
            os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.mirror, "tasks")) for f in fs)
        text = text_fn(read_text(path) if os.path.exists(path) else "")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_marks_audits_and_focus_from_elsewhere_do_not_count_here(self):
        # round-3 adversary audit: a pulled brief can't satisfy this machine's run, audit or plan gates
        self.fm("sync", "on")
        self.pulled(lambda t: t.replace("status: planned", "status: active") + "\n- (step 1) `pytest` → exit 0 · ok"
                    " [ran] [tree deadbeef0000] (2999-01-01T00:00:00Z)\n- (audit intent) `x` → ok [tree deadbeef0000]"
                    " (2999-01-01T00:00:00Z)\n")
        self.fm("sync", "import")
        b = c.find_brief(c.find_project(self.repo), self.tid)
        self.assertIn("exit 0 · ok", b.render(), "the pulled evidence arrived")
        self.assertIn("(audit intent, imported)", b.render())
        self.assertEqual(b.audits(), [])
        self.assertNotIn("[ran]", b.render())
        self.assertNotIn("[tree", b.render())
        self.assertEqual(b.status, "planned", "focus is per machine")

    def test_a_local_audit_survives_an_import_and_a_foreign_one_is_still_marked(self):
        # T-3395: an import relabelled this machine's own audit rows as imported, so they never counted at close
        self.fm("sync", "on")
        self.fm("task", "audit", self.tid, "intent", "checked", "ok")
        self.pulled(lambda t: t + "- 2999-01-01T00:00:00Z pulled note\n- (audit edge) `x` → ok (2999-01-01T00:00:00Z)\n")
        self.fm("sync", "import")
        b = c.find_brief(c.find_project(self.repo), self.tid)
        self.assertIn("pulled note", b.render())
        self.assertEqual([a[0] for a in b.audits()], ["intent"], "the local row counts")
        self.assertIn("(audit edge, imported)", b.render())

    def test_a_local_audit_survives_an_import_when_the_local_brief_is_archived(self):
        # T-3395 review: the local copy in archive/ and the mirror's in tasks/ (or the reverse) must still match
        self.fm("sync", "on")
        self.fm("task", "audit", self.tid, "intent", "checked", "ok")
        p = c.find_project(self.repo)
        old = c.find_brief(p, self.tid).path
        new = os.path.join(p.dir, "archive", os.path.basename(old))
        os.makedirs(os.path.dirname(new), exist_ok=True)
        os.replace(old, new)
        self.pulled(lambda t: t + "- 2999-01-01T00:00:00Z pulled note\n")
        self.fm("sync", "import")
        b = c.find_brief(p, self.tid)
        self.assertIn("pulled note", b.render())
        self.assertEqual([a[0] for a in b.audits()], ["intent"])

    def test_invisible_characters_inside_marks_dont_save_them(self):
        # T-1665 review: plain_lines ran last, so a zero-width, word-joiner or bidi mark inside a mark hid it from the subs
        self.fm("sync", "on")
        for ch in ("​", "⁠", "‏"):
            with self.subTest(ch=hex(ord(ch))):
                self.pulled(lambda t: t + f"\n- (step 1) `x` → exit 0 · ok [ra{ch}n] [tr{ch}ee deadbeef0000] "
                            f"(2999-01-01T00:00:00Z)\n- (audit self{ch}) `x` → ok (2999-01-01T00:00:00Z)\n")
                self.fm("sync", "import")
                b = c.find_brief(c.find_project(self.repo), self.tid)
                self.assertEqual(b.audits(), [])
                self.assertNotIn("[ran]", b.render())
                self.assertNotIn("[tree", b.render())

    def test_a_pull_is_never_overwritten_by_the_next_export(self):
        # round-3 edge audit: exports run on every fm change; a pulled change must wait for the import, not be lost
        self.fm("sync", "on")
        self.pulled(lambda t: t + "- 2026-09-26T00:00:00Z from the other machine\n")
        self.fm("task", "log", self.tid, "local note")  # the next fm command takes the pull in first, then works on it
        here = c.find_brief(c.find_project(self.repo), self.tid).render()
        self.assertIn("from the other machine", here)
        self.assertIn("local note", here)
        self.assertIn("from the other machine", self.exported())
        self.assertIn("local note", self.exported())

    def test_both_sides_changed_keeps_local_and_saves_theirs(self):
        self.fm("sync", "on")
        self.fm("sync", "off")
        self.fm("task", "log", self.tid, "local only change")
        self.pulled(lambda t: t + "- 2026-09-26T00:00:00Z their change\n")
        out = self.fm("sync", "import").stdout
        self.assertIn("conflict", out)
        p = c.find_project(self.repo)
        self.assertIn("local only change", c.find_brief(p, self.tid).render())
        saved = os.path.join(p.dir, "sync-conflicts")
        self.assertTrue(any("their change" in read_text(os.path.join(saved, f)) for f in os.listdir(saved)))

    def test_unresolved_merges_symlinks_and_odd_names_are_refused(self):
        self.fm("sync", "on")
        self.pulled(lambda t: t + "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> branch\n")
        secret = os.path.join(self.tmp, "secret.txt")
        with open(secret, "w") as f:
            f.write("TOP SECRET\n")
        os.symlink(secret, os.path.join(self.mirror, "research", "leak.md"))
        out = self.fm("sync", "import").stdout
        self.assertIn("merge conflict", out)
        p = c.find_project(self.repo)
        self.assertFalse(os.path.exists(os.path.join(p.dir, "research", "leak.md")))
        with open(os.path.join(p.dir, "tasks", "notes.md"), "w") as f:
            f.write("not a brief\n")
        self.fm("task", "log", self.tid, "still works")  # export skips names that aren't briefs
        os.mkfifo(os.path.join(self.mirror, "research", "pipe.md"))  # a reader would block on it forever
        self.fm("sync", "import")
        self.fm("task", "log", self.tid, "no hang")

    def test_code_in_the_mirror_folder_still_counts_for_audits(self):
        self.fm("sync", "on")
        before = c.worktree_id(self.repo)
        with open(os.path.join(self.mirror, "payload.py"), "w") as f:
            f.write("import os\n")
        self.assertNotEqual(c.worktree_id(self.repo), before)

    def test_explicit_off_is_respected_and_checkpoints_ignore_the_mirror(self):
        self.fm("sync", "on")
        self.fm("focus", self.tid)
        self.fm("checkpoint", "--note", "x")
        self.assertNotIn(".foreman", c.find_brief(c.find_project(self.repo), self.tid).section("Resume here"))
        other = os.path.join(self.tmp, "other-home")
        os.makedirs(other)
        env = {"FOREMAN_HOME": other}
        self.fm("init", env=env)
        self.fm("sync", "off", env=env)
        self.hook("SessionStart", {"source": "startup"}, env=env)
        os.environ["FOREMAN_HOME"] = other
        self.assertFalse(c.read_meta(c.find_project(self.repo)).get("sync"))

    def test_on_fails_cleanly_when_the_mirror_cannot_be_written_and_warns_when_ignored(self):
        with open(self.mirror, "w") as f:
            f.write("a file where the folder would go\n")
        p = self.fm("sync", "on", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse(c.read_meta(c.find_project(self.repo)).get("sync"))
        os.remove(self.mirror)
        with open(os.path.join(self.repo, ".gitignore"), "a") as f:
            f.write(".*\n")
        self.assertIn("ignored", self.fm("sync", "on").stdout)

    def test_a_new_pulled_brief_waits_for_the_users_yes_and_carries_no_lease(self):  # T-1749
        self.fm("sync", "on")
        rel = "tasks/T-0099-planted.md"
        planted = re.sub(r"(?m)^id: .*$", "id: T-0099", self.exported()).replace("# Add export", "# Planted work")
        planted = planted.replace("status: planned", "status: active\nlane: /x\nbuilder: b\ncost_cap: 99\n"
                                  "priority: urgent", 1)
        self.pulled(lambda _: planted, rel)
        self.fm("sync", "import")
        b = c.find_brief(c.find_project(self.repo), "T-0099")
        self.assertEqual(b.title, "Planted work")
        self.assertEqual(b.status, "planned")
        self.assertTrue(b.meta.get("confirm"), "a task that arrived from the repo waits for the user's yes")
        for k in ("lane", "builder", "cost_cap"):
            self.assertNotIn(k, b.meta)
        mine = c.find_brief(c.find_project(self.repo), self.tid)
        self.assertFalse(mine.meta.get("confirm"), "a task this machine already had keeps its own confirm")

    def test_pulled_titles_cannot_carry_terminal_sequences(self):
        # round-4 adversary audit: fm queue printed titles straight from disk
        self.fm("sync", "on")
        self.pulled(lambda t: t.replace("# Add export", "# Add\x1b]0;pwned\x07 export‮", 1))
        self.fm("sync", "import")
        self.assertNotIn("\x1b", c.find_brief(c.find_project(self.repo), self.tid).title)
        self.assertNotIn("\x1b]0;pwned", self.fm("queue").stdout)

    def test_exports_are_redacted_and_huge_imports_skipped(self):
        # round 4 (brainstorm, security): the mirror is committed, so no secret may reach it from any older note
        p = c.find_project(self.repo)
        with open(os.path.join(p.dir, "research", "old-note.md"), "w") as f:
            f.write("deploy with token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB\n")  # pragma: allowlist secret
        self.fm("sync", "on")
        text = read_text(os.path.join(self.mirror, "research", "old-note.md"))
        self.assertNotIn("ghp_", text)
        self.assertIn("[REDACTED]", text)
        with open(os.path.join(self.mirror, "research", "huge.md"), "w") as f:
            f.write("x" * 2_000_001)
        self.assertIn("too large", self.fm("sync", "import").stdout)
        self.assertFalse(os.path.exists(os.path.join(p.dir, "research", "huge.md")))

    def test_exports_show_home_folders_as_tilde_and_private_hosts_as_host(self):
        # T-3508: the mirror is public: no login, home folder or private address of this machine may reach it, and
        # the redacted copies still read as unchanged on import (no false conflict, no duplicated decision line)
        import json
        from unittest import mock
        from sampo import sync
        p, home = c.find_project(self.repo), "/srv/u/alice"
        with open(os.path.join(p.dir, "research", "hosts.md"), "w") as f:
            f.write("cd /srv/u/alice/proj and /home/bob/.ssh/config, /home/carol.\n"
                    "ssh alice@100.101.102.103 'tmux ls'; api at 192.168.1.20:8080, 10.1.2.3, 172.20.0.5, "
                    "dev@172.31.9.9\n"
                    "public 8.8.8.8, 172.32.0.1, 100.128.0.1, 10.1.2.3.4, v1.2.3; /mnt/home/x and /srv/u/alicex stay\n"
                    # T-3547: after a $VAR, and Claude's project folder form (the path's slashes as dashes)
                    "echo x > $NOPE/home/bob/.claude and $X/srv/u/alice/y; /tmp/claude-1000/-home-bob-projects-v, "
                    "/tmp/claude-1000/-home-dave/x; /tmp/drill-q1/home/state and lib/home/views stay\n")
        self.fm("task", "log", self.tid, "ran ssh alice@100.101.102.103 from /home/bob/src")
        self.fm("task", "set", self.tid, "scope+=/srv/u/alice/proj/**")
        self.fm("decide", "keep builds in /home/bob/cache", "--task", self.tid)
        meta = c.read_meta(p)
        meta["sync"] = True
        c.write_meta(p, meta)
        with mock.patch.dict(os.environ, {"HOME": home}):
            sync.export(p)
            mirrored = "".join(read_text(os.path.join(d, f)) for d, _, fs in os.walk(self.mirror) for f in fs)
            for leak in (home + "/", "/home/bob", "/home/carol", "alice@", "100.101.102.103", "192.168.1.20",
                         "10.1.2.3,", "172.20.0.5", "172.31.9.9", "home-bob", "home-dave"):
                self.assertNotIn(leak, mirrored)
            self.assertEqual(read_text(os.path.join(self.mirror, "research", "hosts.md")).splitlines(), [
                "cd ~/proj and ~/.ssh/config, ~.",
                "ssh <host> 'tmux ls'; api at <host>:8080, <host>, <host>, <host>",
                "public 8.8.8.8, 172.32.0.1, 100.128.0.1, 10.1.2.3.4, v1.2.3; /mnt/home/x and /srv/u/alicex stay",
                "echo x > $NOPE~/.claude and $X~/y; /tmp/claude-1000/-home-~-projects-v, /tmp/claude-1000/-home-~/x; "
                "/tmp/drill-q1/home/state and lib/home/views stay"])
            self.assertIn("ran ssh <host> from ~/src", self.exported())
            self.assertIn("~/proj/**", self.exported())
            self.pulled(lambda t: re.sub(r"(?m)^updated: .*$", "updated: 2999-01-01T00:00:00Z", t)
                                + "- 2999-01-01T00:00:00Z pulled note\n")
            self.pulled(lambda t: t + "- theirs\n", rel="decisions.md")
            res = sync.import_(p)
            self.assertEqual(res["conflicts"], [])
            self.assertEqual(res["updated"], 2)
            b = c.find_brief(p, self.tid)
            self.assertIn("pulled note", b.render())
            self.assertEqual(b.meta.get("scope"), [home + "/proj/**"], "a scope is this machine's path again")
            decisions = read_text(os.path.join(p.dir, "decisions.md"))
            self.assertEqual((decisions.count("keep builds in"), decisions.count("- theirs")), (1, 1))
            self.assertEqual(sync.export(p), 0, "taken in, nothing to write back")
        self.assertIn("v", json.loads(read_text(os.path.join(p.dir, sync.STAMPS))))

    def test_a_repo_copy_left_for_the_import_is_scrubbed_and_its_lane_never_taken(self):
        # T-3547: export leaves a copy the repo changed (an older export's commit) for the import, but never with this
        # machine's home in it; the import still takes the scrubbed copy and keeps this machine's own lane, not its ~-path
        from unittest import mock
        from sampo import sync
        self.fm("sync", "on")
        p, home = c.find_project(self.repo), "/srv/u/alice"
        b = c.find_brief(p, self.tid)
        b.meta["lane"] = home + "/.claude/worktrees/agent-a1"
        c.save_brief(p, b)
        with mock.patch.dict(os.environ, {"HOME": home}):
            sync.export(p)
            self.assertIn("lane: ~/.claude/worktrees/agent-a1\n", self.exported())
            self.pulled(lambda t: re.sub(r"(?m)^updated: .*$", "updated: 2999-01-01T00:00:00Z", t).replace(
                "lane: ~/", "lane: /home/bob/") + "- 2999-01-01T00:00:00Z pulled from /home/bob/src\n")
            sync.export(p)
            self.assertNotIn("/home/bob", self.exported())
            self.assertIn("pulled from ~/src", self.exported(), "the pull itself waits for the import")
            self.assertEqual(sync.import_(p)["conflicts"], [])
            b = c.find_brief(p, self.tid)
            self.assertIn("pulled from ~/src", b.render())
            self.assertEqual(b.meta.get("lane"), home + "/.claude/worktrees/agent-a1", "this machine's lane, not ~")

    def test_a_brief_named_after_the_home_is_mirrored_under_a_redacted_name(self):
        # T-3574: a brief's file name is a slug of its title made at creation, so a title holding the home path named
        # it home-<login>-…; the mirror redacted the text but kept the name. An older export's copy goes too, and the
        # import still finds the brief by its id and keeps this machine's own file name
        from unittest import mock
        from sampo import sync
        self.fm("sync", "on")
        p = c.find_project(self.repo)
        local = os.path.join(p.dir, "tasks", f"{self.tid}-home-alice-claude-foreman-state.md")
        os.rename(c.find_brief(p, self.tid).path, local)
        note = os.path.join(p.dir, "research", "notes-home-alice.md")  # a research note's name, slugged from free text
        with open(note, "w") as f:
            f.write("# notes\n")
        names = lambda: sorted(f for _, _, fs in os.walk(self.mirror) for f in fs  # noqa: E731
                               if f.startswith((self.tid, "notes-")))
        with mock.patch.dict(os.environ, {"HOME": "/srv/u/bob"}):
            sync.export(p)  # an export from before the fix: the names as they are
        self.assertEqual(names(), [os.path.basename(local), "notes-home-alice.md"])
        with mock.patch.dict(os.environ, {"HOME": "/home/alice"}):
            sync.export(p)
            self.assertEqual(names(), [f"{self.tid}-home-user-claude-foreman-state.md", "notes-home-user.md"])
            self.pulled(lambda t: t + "- 2999-01-01T00:00:00Z pulled note\n")
            self.assertEqual(sync.import_(p), {"added": 0, "updated": 1, "conflicts": []})
            self.assertIn("pulled note", read_text(local))
            self.assertEqual([os.path.basename(b.path) for b in c.load_briefs(p, include_archive=True)
                              if b.id == self.tid], [os.path.basename(local)])
            self.assertEqual(sync.export(p), 0, "taken in, nothing to write back")
            self.assertEqual(names(), [f"{self.tid}-home-user-claude-foreman-state.md", "notes-home-user.md"])
            self.pulled(lambda t: t + "theirs\n", rel="research/notes-home-user.md")
            res = sync.import_(p)  # mapped back to the local note: a conflict on it, never a second copy
            self.assertEqual((res["added"], len(res["conflicts"])), (0, 1))
            self.assertTrue(res["conflicts"][0].startswith("research/notes-home-user.md: differs here"))
            self.assertEqual(sorted(os.listdir(os.path.dirname(note))), ["csv-notes.md", "notes-home-alice.md"])
            self.fm("private", "add", "*.lan.example-private.test")
            self.fm("private", "add", "wombatcorp")
            name = sync._namer()
            for rel, want in (  # names cut short, innocent home-* words, the terms in kebab form (a host glob's with
                    # the label before it), research notes; dates and short cuts stay
                    ("archive/2026-10/T-0002-docs-at-home-ali.md", "archive/2026-10/T-0002-docs-at-home-user.md"),
                    ("tasks/T-0003-fm-code-home-by-need-users-alice.md", "tasks/T-0003-fm-code-home-by-need-home-user.md"),
                    ("tasks/T-0004-ssh-devbox-lan-example-private-test-for-wombatcorp.md",
                     "tasks/T-0004-ssh-private-for-private.md"),
                    ("tasks/T-0006-mail-from-wombat.md", "tasks/T-0006-mail-from-private.md"),
                    ("tasks/T-0007-ssh-devbox-lan-example-pri.md", "tasks/T-0007-ssh-private.md"),
                    ("research/home-alice.md", "research/home-user.md"),
                    ("research/forge-skill-at-home-alice/SKILL.md", "research/forge-skill-at-home-user/SKILL.md"),
                    ("tasks/T-0005-shutdown-2026-10-22-08-37-alicex.md", None), ("tasks/T-0008-from-wom.md", None),
                    ("research/csv-notes.md", None), ("decisions.md", None)):
                self.assertEqual(name(rel), want or rel, rel)

    def test_the_next_export_cleans_a_mirror_written_before_redaction(self):
        # T-3508: files an older export mirrored unredacted (and its `fm sync` README) are rewritten, though the
        # export stamps (T-3349) say neither side moved
        import json
        from sampo import sync
        self.fm("sync", "on")
        p = c.find_project(self.repo)
        rel, raw = "research/csv-notes.md", "notes from /home/bob/x via alice@100.101.102.103\n"
        for path in (os.path.join(p.dir, rel), os.path.join(self.mirror, rel)):
            with open(path, "w") as f:
                f.write(raw)
        readme = os.path.join(self.mirror, "README.md")
        with open(readme, "w") as f:
            f.write(sync.README.replace("`sampo sync", "`fm sync"))
        meta = c.read_meta(p)
        meta["sync_base"][rel] = sync._sha(raw)
        c.write_meta(p, meta)
        old = {r: [sync._stamp(os.path.join(p.dir, r)), sync._stamp(os.path.join(self.mirror, r))]
               for r in sync._local(p)}  # an older export's stamps: no version
        c.write_atomic(os.path.join(p.dir, sync.STAMPS), json.dumps(old))
        sync.export(p)
        self.assertEqual(read_text(os.path.join(self.mirror, rel)), "notes from ~/x via <host>\n")
        self.assertIn("`sampo sync`", read_text(readme))
        self.assertNotIn("fm sync", read_text(readme))

    def test_a_private_term_added_later_rewrites_the_mirrored_copies_once(self):
        # T-3529: the user's private domain (a host glob) and words never reach the public mirror; a term added after
        # files were mirrored rewrites them on the next export, though the export stamps say neither side moved
        from sampo import sync
        self.fm("sync", "on")
        p = c.find_project(self.repo)
        rel = "research/csv-notes.md"
        with open(os.path.join(p.dir, rel), "w") as f:
            f.write("endpoint http://llm.home.example-private.test/v1 for Wombatcorp\n")
        sync.export(p)
        self.assertIn("example-private", read_text(os.path.join(self.mirror, rel)), "no term yet")
        self.fm("private", "add", "*.home.example-private.test")
        self.fm("private", "add", "wombatcorp")
        sync.export(p)
        self.assertEqual(read_text(os.path.join(self.mirror, rel)), "endpoint http://<host>/v1 for <private>\n")
        self.assertEqual(sync.export(p), 0, "rewritten once")
        self.assertEqual(sync.import_(p)["conflicts"], [], "the redacted copy reads as unchanged")

    def test_the_mirror_does_not_change_the_worktree_id(self):
        before = c.worktree_id(self.repo)
        self.fm("sync", "on")
        self.assertEqual(c.worktree_id(self.repo), before, "exports must not stale audits")

    def test_writing_the_mirror_by_hand_is_state_direct(self):
        ctx = g.Ctx(cwd=self.repo, project_root=self.repo, home=self.tmp, foreman_home=self.home)
        r = g.check("Write", {"file_path": os.path.join(self.mirror, "tasks", "T-0001-x.md"), "content": "x"}, ctx)
        self.assertEqual(r.category, "state-direct")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True).stdout.strip()

    def legacy(self, tracked=True):
        """T-3342: a repo whose mirror is still the old .foreman/ folder."""
        self.fm("sync", "on")
        os.rename(self.mirror, os.path.join(self.repo, ".foreman"))
        if tracked:
            self.git("add", "-A")
            self.git("commit", "-qm", "legacy mirror")
        return os.path.join(self.repo, ".foreman")

    def test_fresh_sync_writes_sampo_and_status_and_readme_name_it(self):
        self.fm("sync", "on")
        self.assertTrue(os.path.isdir(self.mirror))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".foreman")))
        self.assertIn("# .sampo", read_text(os.path.join(self.mirror, "README.md")))
        self.assertIn(".sampo", self.fm_json("sync", "status")["dir"])

    def test_a_tracked_foreman_becomes_one_logged_rename_commit(self):
        old = self.legacy()
        self.assertEqual(self.fm_json("sync", "status")["dir"], old, "read in place until the move")
        before = self.git("rev-list", "--count", "HEAD")
        self.fm("sync", "export")
        self.assertFalse(os.path.exists(old))
        self.assertIn("# .sampo", read_text(os.path.join(self.mirror, "README.md")))
        self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), int(before) + 1)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Move fm sync's mirror .foreman/ to .sampo/")
        self.assertEqual(self.git("ls-files", ".foreman"), "")
        self.assertTrue(self.git("ls-files", ".sampo"))
        self.assertEqual(self.git("status", "--porcelain", "--", ".sampo", ".foreman"), "")
        moved = [e for e in c.ledger_tail(c.find_project(self.repo), 50) if e.get("event") == "sync_moved"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["data"]["sha"], self.git("rev-parse", "--short", "HEAD"))
        self.fm("sync", "export")  # a second run moves nothing
        self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), int(before) + 1)

    def test_an_ignored_foreman_is_renamed_without_a_commit(self):
        with open(os.path.join(self.repo, ".gitignore"), "a") as f:
            f.write(".foreman/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore")
        old = self.legacy(tracked=False)
        head = self.git("rev-parse", "HEAD")
        self.fm("sync", "export")
        self.assertTrue(os.path.isdir(self.mirror))
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("check-ignore", ".sampo/README.md"), ".sampo/README.md")

    def test_both_folders_fold_into_sampo_and_commit(self):
        old = self.legacy()
        self.fm("sync", "export")  # moved
        os.makedirs(os.path.join(old, "research"))  # a pull brings the old folder back
        with open(os.path.join(old, "research", "theirs.md"), "w") as f:
            f.write("# theirs\n")
        with open(os.path.join(old, "decisions.md"), "w") as f:
            f.write("old copy\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "pulled old mirror")
        mine = read_text(os.path.join(self.mirror, "decisions.md"))
        self.fm("sync", "export")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(read_text(os.path.join(self.mirror, "research", "theirs.md")), "# theirs\n")
        text = read_text(os.path.join(self.mirror, "decisions.md"))
        self.assertIn(mine.strip(), text, "a file .sampo has is kept")
        self.assertIn("old copy", text)
        self.assertEqual(self.git("ls-files", ".foreman"), "")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_an_untracked_newer_foreman_beside_a_tracked_sampo_merges_in_without_loss(self):
        old = self.legacy()
        self.fm("sync", "export")  # moved and committed
        os.makedirs(os.path.join(old, "research"))  # old code keeps writing an untracked .foreman/
        with open(os.path.join(old, "research", "new.md"), "w") as f:
            f.write("# new\n")
        with open(os.path.join(old, "decisions.md"), "w") as f:
            f.write("- only in the old folder\n")
        with open(os.path.join(old, "research", "csv-notes.md"), "w") as f:
            f.write("# CSV\nnewer text\n")
        self.fm("sync", "export")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(read_text(os.path.join(self.mirror, "research", "new.md")), "# new\n")
        self.assertNotIn("newer text", read_text(os.path.join(self.mirror, "research", "csv-notes.md")), "never by mtime")
        self.assertIn("newer text", self.aside_text())  # the other copy is set aside, not lost
        text = read_text(os.path.join(self.mirror, "decisions.md"))
        self.assertIn("CSV over XLSX", text)
        self.assertIn("only in the old folder", text)

    def aside_text(self):
        saved = os.path.join(c.find_project(self.repo).dir, "sync-conflicts")
        return "".join(read_text(os.path.join(saved, f)) for f in os.listdir(saved)) if os.path.isdir(saved) else ""

    def brief_path(self, base):
        return next(os.path.join(d, f) for d, _, fs in os.walk(os.path.join(base, "tasks")) for f in fs
                    if f.startswith(self.tid))

    def test_a_stale_old_brief_never_beats_a_newer_sampo_brief(self):  # the clock is `updated`, never mtime
        old = self.legacy()
        self.fm("sync", "export")
        mine = read_text(self.brief_path(self.mirror))
        stale = re.sub(r"(?m)^(updated:).*$", r"\1 2000-01-01T00:00:00Z", mine).replace("Add export", "STALE TITLE")
        self.assertNotEqual(stale, mine)
        os.makedirs(os.path.join(old, "tasks"))
        with open(os.path.join(old, "tasks", os.path.basename(self.brief_path(self.mirror))), "w") as f:
            f.write(stale)  # written just now: the newest mtime
        self.fm("sync", "export")
        self.assertFalse(os.path.exists(old))
        self.assertNotIn("STALE TITLE", read_text(self.brief_path(self.mirror)))
        self.assertIn("STALE TITLE", self.aside_text())

    def test_non_markdown_files_in_the_old_folder_travel_or_are_set_aside(self):
        old = self.legacy()
        self.fm("sync", "export")
        os.makedirs(old)
        for rel, text in (("policy.toml", "a = 1\n"), ("research/data.csv", "x,y\n")):
            os.makedirs(os.path.dirname(os.path.join(old, rel)), exist_ok=True)
            with open(os.path.join(old, rel), "w") as f:
                f.write(text)
        with open(os.path.join(self.mirror, "research", "data.csv"), "w") as f:
            f.write("mine\n")
        self.fm("sync", "export")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(read_text(os.path.join(self.mirror, "policy.toml")), "a = 1\n")
        self.assertEqual(read_text(os.path.join(self.mirror, "research", "data.csv")), "mine\n")
        self.assertIn("x,y", self.aside_text())

    def test_a_failed_move_commit_keeps_uncommitted_mirror_edits(self):
        old = self.legacy()
        hook = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        note = os.path.join(old, "research", "csv-notes.md")
        with open(note, "a") as f:
            f.write("uncommitted edit\n")
        self.fm("sync", "export")
        self.assertIn("uncommitted edit", read_text(note))
        # both folders: the same failure leaves the old folder, with its edits, in place
        os.remove(hook)
        self.fm("sync", "export")
        self.assertIn("uncommitted edit", read_text(os.path.join(self.mirror, "research", "csv-notes.md")))
        os.makedirs(old)
        with open(os.path.join(old, "extra.md"), "w") as f:
            f.write("extra edit\n")
        self.git("add", "-f", "-A")
        self.git("commit", "-qm", "pulled old")
        with open(os.path.join(old, "extra.md"), "a") as f:
            f.write("more\n")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        self.fm("sync", "export")
        self.assertIn("more", read_text(os.path.join(old, "extra.md")))
        self.assertEqual(self.git("ls-files", ".foreman"), ".foreman/extra.md", "the index is put back too")

    def test_a_users_own_sampo_folder_is_never_merged_moved_or_removed(self):
        old = self.legacy()
        os.makedirs(self.mirror)
        with open(os.path.join(self.mirror, "notes.txt"), "w") as f:
            f.write("mine\n")
        self.fm("sync", "export")
        self.assertTrue(os.path.isdir(old), "not moved into someone else's folder")
        self.assertEqual(os.listdir(self.mirror), ["notes.txt"])
        self.assertFalse(g.classify_write(os.path.join(self.mirror, "notes.txt"), g.Ctx(
            cwd=self.repo, project_root=self.repo, home=self.tmp, foreman_home=self.tmp)))

    def test_a_tracked_sampo_is_not_added_to_the_exclude_file(self):
        old = self.legacy(tracked=False)
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write("/.foreman/\n")
        os.makedirs(self.mirror)
        with open(os.path.join(self.mirror, "README.md"), "w") as f:
            f.write("# .sampo\n")
        self.git("add", "-f", ".sampo")
        self.git("commit", "-qm", "a tracked .sampo")
        self.fm("sync", "export")
        self.assertFalse(os.path.exists(old))
        self.assertNotIn("/.sampo/", read_text(os.path.join(self.repo, ".git", "info", "exclude")))

    def test_a_lane_worktree_never_moves_the_main_checkouts_mirror(self):
        old = self.legacy()
        head = self.git("rev-parse", "HEAD")
        wt = os.path.join(self.tmp, "lane-wt")
        self.git("worktree", "add", "-q", "-b", "lane-x", wt)
        self.fm("sync", "export", cwd=wt)
        self.fm("task", "log", self.tid, "from the lane", cwd=wt)
        self.assertTrue(os.path.isdir(old))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        sub = os.path.join(self.repo, "sub")
        os.makedirs(sub)
        self.fm("sync", "export", cwd=sub)  # the repo's own checkout, from a subfolder: its own toplevel
        self.assertFalse(os.path.exists(old))

    def test_nothing_moves_mid_merge_or_on_a_detached_head(self):
        old = self.legacy()
        with open(os.path.join(self.repo, ".git", "MERGE_HEAD"), "w") as f:
            f.write(self.git("rev-parse", "HEAD") + "\n")
        self.fm("sync", "export")
        self.assertTrue(os.path.isdir(old))
        os.remove(os.path.join(self.repo, ".git", "MERGE_HEAD"))
        self.git("checkout", "-q", "--detach")
        self.fm("sync", "export")
        self.assertTrue(os.path.isdir(old))
        self.assertFalse(os.path.exists(self.mirror))

    def test_a_failed_commit_puts_it_back_and_only_export_retries(self):
        old = self.legacy()
        hook = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        head = self.git("rev-parse", "HEAD")
        self.fm("sync", "export")
        self.assertTrue(os.path.isdir(old))
        self.assertFalse(os.path.exists(self.mirror))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain", "--", ".foreman"), "")
        self.assertIn("move", self.fm_json("sync", "status").get("move_error", ""))
        os.remove(hook)
        self.fm("task", "log", self.tid, "any other command")
        self.assertTrue(os.path.isdir(old), "only sync export retries")
        self.fm("sync", "export")
        self.assertTrue(os.path.isdir(self.mirror))
        self.assertFalse(os.path.exists(old))
        self.assertNotIn("move_error", self.fm_json("sync", "status"))

    def test_status(self):
        self.assertIn("off", self.fm("sync", "status").stdout)
        self.fm("sync", "on")
        st = self.fm_json("sync", "status")
        self.assertEqual((st["on"], st["briefs"]), (True, 1))
