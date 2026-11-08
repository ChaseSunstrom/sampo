"""T-2746: fm migrate moves the Foreman home and its siblings to sampo names once, leaves symlinks at the old paths,
backs up the record, writes a manifest with hashes, reruns safely, resumes a half-done move and undoes exactly."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helpers import FM, ForemanTestCase

from sampo import homemove as hm

REAL_SNAPSHOTS = os.path.expanduser("~/.claude/foreman/state/snapshots")  # read only, captured before any HOME patch
SIBLINGS = ("draft", "evolve", "lanes")


class Migrate(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.h = os.path.join(self.tmp, "h")
        self.cl = os.path.join(self.h, ".claude")
        self.old = os.path.join(self.cl, "foreman")
        self.new = os.path.join(self.cl, "sampo")
        os.makedirs(self.old)
        for s in SIBLINGS:
            d = os.path.join(self.cl, f"foreman.{s}", "sub")
            os.makedirs(d)
            with open(os.path.join(d, "f.txt"), "w") as f:
                f.write(s)
            os.symlink("sub/f.txt", os.path.join(self.cl, f"foreman.{s}", "link"))
        env = {"HOME": self.h, "FOREMAN_HOME": self.old}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.slug = self._lay_record()

    def _lay_record(self):
        """The newest real upgrade snapshot when this machine has one, else a project made through fm."""
        from sampo import rollback
        snaps = sorted(s for s in os.listdir(REAL_SNAPSHOTS) if s.endswith(".tar.gz") and not s.startswith(rollback.PRE)) \
            if os.path.isdir(REAL_SNAPSHOTS) else []
        if snaps:
            import shutil
            stage = os.path.join(self.tmp, "stage")
            try:
                slugs = rollback._stage(os.path.join(REAL_SNAPSHOTS, snaps[-1]), stage, True)
                for s in slugs:
                    shutil.copytree(os.path.join(stage, s), os.path.join(self.old, "state", "projects", s))
                if slugs:
                    return sorted(slugs)[0]
            except Exception:  # a snapshot that won't unpack here: fall through to the synthetic record
                shutil.rmtree(os.path.join(self.old, "state"), ignore_errors=True)
        self.fm("task", "new", "t", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s",
                env={"HOME": self.h, "FOREMAN_HOME": self.old})
        return sorted(os.listdir(os.path.join(self.old, "state", "projects")))[0]

    def _checks(self):
        """fmdrill.RESTORE_CHECKS through the new path."""
        from sampo import drill
        e = dict(os.environ, HOME=self.h, FOREMAN_HOME=self.new, FOREMAN_INHIBITED="1", FOREMAN_NO_BACKGROUND="1",
                 FOREMAN_NO_LINK="1")
        for label, verb in drill.RESTORE_CHECKS:
            r = subprocess.run([sys.executable, FM, verb, "-p", self.slug], cwd=self.repo, env=e,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, f"{verb}: {r.stdout}{r.stderr}")
            self.assertNotIn("Traceback", r.stderr)

    def test_moves_links_backs_up_and_the_record_reads_back(self):
        r = hm.migrate()
        self.assertEqual(r["status"], "done", r)
        for s in ("",) + tuple("." + x for x in SIBLINGS):
            o, n = os.path.join(self.cl, "foreman" + s), os.path.join(self.cl, "sampo" + s)
            self.assertTrue(os.path.isdir(n) and not os.path.islink(n))
            self.assertEqual(os.readlink(o), n)
        man = json.load(open(os.path.join(self.cl, "sampo-migrate.json")))
        self.assertEqual(man["status"], "done")
        self.assertEqual(len(man["folders"]), 4)
        self.assertTrue(all(len(f["hash"]) == 64 for f in man["folders"]))
        self.assertTrue(os.path.getsize(os.path.join(self.cl, "sampo-migrate-backup.tar.gz")) > 0)
        self._checks()

    def test_a_second_run_changes_nothing(self):
        hm.migrate()
        man = open(os.path.join(self.cl, "sampo-migrate.json")).read()
        self.assertEqual(hm.migrate()["status"], "done")
        self.assertEqual(open(os.path.join(self.cl, "sampo-migrate.json")).read(), man)

    def test_a_run_killed_after_the_move_resumes(self):
        with mock.patch.object(hm, "_link", side_effect=RuntimeError("killed")):
            with self.assertRaises(RuntimeError):
                hm.migrate()
        self.assertFalse(os.path.lexists(self.old))
        self.assertTrue(os.path.isdir(self.new))
        self.assertEqual(hm.migrate()["status"], "done")
        self.assertEqual(os.readlink(self.old), self.new)
        self._checks()

    def test_undo_restores_the_old_layout_byte_for_byte(self):
        before = {n: hm.tree_hash(os.path.join(self.cl, n)) for n in os.listdir(self.cl)}
        hm.migrate()
        r = hm.migrate(undo=True)
        self.assertEqual(r["status"], "undone", r)
        # the backup and the manifest stay: they are the record of what happened
        self.assertEqual([n for n in sorted(os.listdir(self.cl)) if not n.startswith("sampo-migrate")], sorted(before))
        for n, h in before.items():
            self.assertFalse(os.path.islink(os.path.join(self.cl, n)))
            self.assertEqual(hm.tree_hash(os.path.join(self.cl, n)), h)

    def test_core_holds_through_both_paths_after_the_move(self):
        # T-2967: the old path is a symlink to the new; a write through either is core, and a standing yes covers it
        import fmguard as g
        hm.migrate()
        os.makedirs(os.path.join(self.new, "plugin", "lib"))
        for fh in (self.old, self.new):  # the home the guard was told: the old name, or the new one
            for root in (self.old, self.new):  # the path the write names
                def ctx(**kw):
                    return g.Ctx(cwd=self.h, project_root=None, home=self.h, foreman_home=fh, scratch=["/tmp"],
                                 task_id="T-0007", **kw)
                lib = os.path.join(root, "plugin", "lib")
                for tool, inp in (("Edit", {"file_path": lib + "/fmcore.py", "old_string": "a", "new_string": "b"}),
                                  ("Bash", {"command": f"echo x > {lib}/fmcore.py"})):
                    with self.subTest(fh=fh, root=root, tool=tool):
                        r = g.check(tool, inp, ctx())
                        self.assertTrue(r and r.category == "core", r)
                        self.assertIsNone(g.check(tool, inp, ctx(standing={"core"})), "the standing yes covers it")
                guard = {"command": f"echo x > {lib}/fmguard.py"}
                self.assertEqual(g.check("Bash", guard, ctx(standing={"core"})).category, "core")
                tree = g.check("Bash", {"command": f"cp -r /tmp/a {root}/plugin"}, ctx(standing={"core"}))
                self.assertTrue(tree and tree.category == "core", tree)

    def test_undo_after_the_state_changed_restores_warns_and_records(self):
        hm.migrate()
        with open(os.path.join(self.new, "late.txt"), "w") as f:
            f.write("written after the move")
        r = hm.migrate(undo=True)
        self.assertEqual(r["status"], "undone", r)
        self.assertIn(self.old, r["warning"])
        self.assertTrue(os.path.isdir(self.old) and not os.path.islink(self.old))
        self.assertTrue(os.path.isfile(os.path.join(self.old, "late.txt")))
        with self.assertRaisesRegex(ValueError, "nothing to undo"):
            hm.migrate(undo=True)

    def test_undo_with_nothing_migrated_is_a_clear_refusal(self):
        with self.assertRaises(ValueError):
            hm.migrate(undo=True)

    def test_both_paths_real_is_refused_and_nothing_moves(self):
        os.makedirs(self.new)
        with self.assertRaises(ValueError):
            hm.migrate()
        self.assertTrue(os.path.isdir(self.old) and not os.path.islink(self.old))

    def test_a_dry_run_writes_nothing(self):
        r = hm.migrate(dry=True)
        self.assertEqual(r["status"], "plan")
        self.assertEqual(sorted(os.listdir(self.cl)), ["foreman", "foreman.draft", "foreman.evolve", "foreman.lanes"])

    def test_a_sensitive_project_stays_sensitive(self):
        e = {"HOME": self.h, "FOREMAN_HOME": self.old}
        self.fm("sensitive", "on", env=e)
        def sens(home):
            pd = os.path.join(home, "state", "projects")
            return sorted(s for s in os.listdir(pd) if os.path.isfile(os.path.join(pd, s, "meta.json"))
                          and json.load(open(os.path.join(pd, s, "meta.json"))).get("sensitive"))
        before = sens(self.old)
        self.assertTrue(before)  # the scratch repo's project
        hm.migrate()
        self.assertEqual(sens(self.new), before)
        self.assertEqual(sens(self.old), before)  # through the symlink


FILES = ("settings.json", "plugins/installed_plugins.json", "plugins/known_marketplaces.json")


class MigratePlugins(ForemanTestCase):
    """T-3707: sampo migrate plugins moves Claude Code's record of Sampo from foreman@foreman (and sampo@foreman, the
    renames map's id) to sampo@sampo, re-adding the marketplace as sampo, one write per file, with an exact undo. The
    config is laid out the way this machine's is, with made-up values; the real ~/.claude is never read."""

    def setUp(self):
        super().setUp()
        self.h = os.path.join(self.tmp, "h")
        self.cl = os.path.join(self.h, ".claude")
        self.mk = os.path.join(self.h, "src", "sampo")  # the checkout the directory marketplace points at
        for rel, data in ((".claude-plugin/marketplace.json", {"name": "sampo", "plugins": [
                              {"name": "sampo", "source": "./plugin"}, {"name": "sampo-ui", "source": "./mods/foreman-ui"},
                              {"name": "sampo-design", "source": "./plugins/foreman-design"}]}),
                          ("plugin/.claude-plugin/plugin.json", {"name": "sampo", "version": "9.1.0"}),
                          ("mods/foreman-ui/.claude-plugin/plugin.json", {"name": "sampo-ui", "version": "0.9.0"})):
            self.write(os.path.join(self.mk, rel), data)
        old = {"source": {"source": "directory", "path": self.mk}}
        self.write(os.path.join(self.cl, "settings.json"), {
            "env": {"SOME_FLAG": "1"}, "permissions": {"allow": ["Bash(ls:*)"], "defaultMode": "bypassPermissions"},
            "model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x/stop.sh"}]}]},
            "statusLine": {"type": "command", "command": os.path.join(self.mk, "plugin", "hooks", "statusline")},
            "enabledPlugins": {"tool-a@official": True, "tool-b@official": False, "foreman@foreman": True,
                               "design@synced": False, "foreman-ui@foreman": True},
            "extraKnownMarketplaces": {"official": {"source": {"source": "github", "repo": "org/official"}},
                                       "foreman": old},
            "pluginConfigs": {"tool-c@tools": {"options": {"x": 1}}, "foreman-ui@foreman": {"options": {"motion": "off"}}},
            "theme": "dark"}, newline=True)
        rec = lambda name, v: [{"scope": "user", "installPath": os.path.join(self.cl, "plugins", "cache", "foreman", name, v),  # noqa: E731
                                "version": v, "installedAt": "2026-09-26T04:24:30.268Z",
                                "lastUpdated": "2026-09-26T04:24:30.268Z", "gitCommitSha": "0" * 40}]
        self.write(os.path.join(self.cl, "plugins", "installed_plugins.json"), {"version": 2, "plugins": {
            "tool-a@official": rec("tool-a", "1.0.0"), "foreman@foreman": rec("foreman", "0.1.0"),
            "foreman-ui@foreman": rec("foreman-ui", "0.7.0")}})
        self.write(os.path.join(self.cl, "plugins", "known_marketplaces.json"), {
            "official": {"source": {"source": "github", "repo": "org/official"},
                         "installLocation": os.path.join(self.cl, "plugins", "marketplaces", "official"),
                         "lastUpdated": "2026-11-07T19:33:50.805Z"},
            "foreman": dict(old, installLocation=self.mk, lastUpdated="2026-10-15T19:48:08.766Z")})
        p = mock.patch.dict(os.environ, {"HOME": self.h})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.before = self.bytes()

    def write(self, path, data, newline=False):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps(data, indent=2) + ("\n" if newline else ""))

    def bytes(self):
        return {f: open(os.path.join(self.cl, f), "rb").read() for f in FILES}

    def read(self, f):
        with open(os.path.join(self.cl, f)) as fh:
            return json.load(fh)

    def migrate(self, **kw):
        from sampo import pluginmove
        return pluginmove.migrate(**kw)

    def test_a_dry_run_lists_the_swap_and_changes_nothing(self):
        r = self.migrate(dry=True)
        self.assertEqual(r["status"], "plan")
        self.assertEqual(self.bytes(), self.before)
        self.assertFalse(os.path.exists(os.path.join(self.cl, "sampo-migrate-plugins")))
        from sampo import pluginmove
        text = pluginmove.render(r)
        for words in ("foreman@foreman -> sampo@sampo", "foreman-ui@foreman -> sampo-ui@sampo", "+ sampo", "- foreman",
                      "dry run"):
            self.assertIn(words, text)

    def test_the_move_swaps_every_id_and_keeps_the_rest(self):
        r = self.migrate()
        self.assertEqual(r["status"], "done")
        s = self.read("settings.json")
        self.assertEqual(s["enabledPlugins"], {"tool-a@official": True, "tool-b@official": False, "sampo@sampo": True,
                                               "design@synced": False, "sampo-ui@sampo": True})
        self.assertEqual(list(s["enabledPlugins"]), ["tool-a@official", "tool-b@official", "sampo@sampo",
                                                     "design@synced", "sampo-ui@sampo"])  # each in its old place
        self.assertEqual(s["extraKnownMarketplaces"], {"official": {"source": {"source": "github", "repo": "org/official"}},
                                                       "sampo": {"source": {"source": "directory", "path": self.mk}}})
        self.assertEqual(s["pluginConfigs"], {"tool-c@tools": {"options": {"x": 1}},
                                              "sampo-ui@sampo": {"options": {"motion": "off"}}})
        for k in ("env", "permissions", "model", "hooks", "statusLine", "theme"):
            self.assertEqual(s[k], json.loads(self.before["settings.json"])[k], k)
        self.assertTrue(self.before["settings.json"].endswith(b"}\n"))
        self.assertTrue(open(os.path.join(self.cl, "settings.json"), "rb").read().endswith(b"}\n"))
        inst = self.read("plugins/installed_plugins.json")
        self.assertEqual(sorted(inst["plugins"]), ["sampo-ui@sampo", "sampo@sampo", "tool-a@official"])
        self.assertEqual(inst["version"], 2)
        e = inst["plugins"]["sampo@sampo"][0]
        self.assertEqual((e["scope"], e["installPath"], e["version"]), ("user", os.path.join(self.mk, "plugin"), "9.1.0"))
        self.assertEqual(inst["plugins"]["sampo-ui@sampo"][0]["installPath"], os.path.join(self.mk, "mods", "foreman-ui"))
        known = self.read("plugins/known_marketplaces.json")
        self.assertEqual(sorted(known), ["official", "sampo"])
        self.assertEqual(known["sampo"]["source"], {"source": "directory", "path": self.mk})
        self.assertEqual(known["sampo"]["installLocation"], self.mk)
        self.assertEqual(known["official"], json.loads(self.before["plugins/known_marketplaces.json"])["official"])

    def test_never_both_ids_enabled_in_any_write(self):
        from sampo import pluginmove
        seen = []
        real = pluginmove.c.write_atomic

        def spy(path, text):
            seen.append((os.path.relpath(path, self.cl), json.loads(text)))
            return real(path, text)
        with mock.patch.object(pluginmove.c, "write_atomic", spy):
            self.migrate()
        settings = [d for f, d in seen if f == "settings.json"]
        self.assertEqual(len(settings), 1, "one write swaps the ids")
        on = {k for k, v in settings[0]["enabledPlugins"].items() if v}
        self.assertTrue({"sampo@sampo", "sampo-ui@sampo"} <= on)
        self.assertFalse(on & {"foreman@foreman", "foreman-ui@foreman", "sampo@foreman", "sampo-ui@foreman"})
        self.assertEqual(sorted(f for f, _ in seen if f in FILES), sorted(FILES), "each file written once")

    def test_a_rerun_changes_nothing(self):
        self.migrate()
        after = self.bytes()
        r = self.migrate()
        self.assertEqual(r["status"], "done")
        self.assertFalse(r["changed"])
        self.assertEqual(self.bytes(), after)

    def test_undo_restores_every_file_byte_for_byte(self):
        self.migrate()
        self.assertNotEqual(self.bytes(), self.before)
        r = self.migrate(undo=True)
        self.assertEqual(r["status"], "undone")
        self.assertEqual(self.bytes(), self.before)
        self.assertFalse(r.get("warning"))
        with self.assertRaises(ValueError):
            self.migrate(undo=True)  # nothing left to undo
        self.assertEqual(self.migrate()["status"], "done")  # and it moves again after an undo
        self.assertEqual(self.migrate(undo=True)["status"], "undone")
        self.assertEqual(self.bytes(), self.before)

    def test_undo_after_claude_code_rewrote_a_file_keeps_that_copy_and_warns(self):
        self.migrate()
        k = os.path.join(self.cl, "plugins", "known_marketplaces.json")
        with open(k, "a") as f:
            f.write(" ")
        r = self.migrate(undo=True)
        self.assertEqual(self.bytes(), self.before)
        self.assertIn("known_marketplaces.json", r["warning"])
        kept = [n for n in os.listdir(os.path.join(self.cl, "sampo-migrate-plugins")) if "known_marketplaces" in n
                and n.endswith(".replaced")]
        self.assertEqual(len(kept), 1)

    def test_a_half_moved_config_ends_on_one_id(self):
        # the renames map moved the plugin to sampo@foreman while foreman-ui@foreman and a stale foreman@foreman stayed
        s = self.read("settings.json")
        s["enabledPlugins"]["sampo@foreman"] = True
        s["enabledPlugins"]["foreman-ui@foreman"] = False
        self.write(os.path.join(self.cl, "settings.json"), s, newline=True)
        self.migrate()
        on = self.read("settings.json")["enabledPlugins"]
        self.assertEqual({k: v for k, v in on.items() if "sampo" in k or "foreman" in k},
                         {"sampo@sampo": True, "sampo-ui@sampo": False})  # a disabled mod stays disabled

    def test_claude_config_dir_is_followed(self):
        cfg = os.path.join(self.tmp, "cfg")
        os.rename(self.cl, cfg)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
            self.assertEqual(self.migrate()["status"], "done")
        with open(os.path.join(cfg, "settings.json")) as f:
            self.assertIn("sampo@sampo", json.load(f)["enabledPlugins"])

    def test_a_checkout_that_still_says_foreman_is_refused(self):
        self.write(os.path.join(self.mk, ".claude-plugin", "marketplace.json"), {"name": "foreman", "plugins": []})
        with self.assertRaises(ValueError) as e:
            self.migrate()
        self.assertIn("foreman", str(e.exception))
        self.assertEqual(self.bytes(), self.before)

    def test_a_marketplace_not_in_a_folder_is_refused_with_the_steps(self):
        k = self.read("plugins/known_marketplaces.json")
        k["foreman"] = {"source": {"source": "github", "repo": "ChaseSunstrom/sampo"}, "installLocation": "/x"}
        self.write(os.path.join(self.cl, "plugins", "known_marketplaces.json"), k)
        before = self.bytes()
        with self.assertRaises(ValueError) as e:
            self.migrate()
        self.assertIn("claude plugin marketplace add ChaseSunstrom/sampo", str(e.exception))
        self.assertEqual(self.bytes(), before)

    def test_a_sampo_marketplace_already_added_is_kept_if_it_is_this_folder(self):
        # install.sh's claude plugin marketplace add may have run first: same folder (through a link) is the one to keep
        link = os.path.join(self.tmp, "link")
        os.symlink(self.mk, link)
        k = self.read("plugins/known_marketplaces.json")
        k["sampo"] = {"source": {"source": "directory", "path": link}, "installLocation": link, "lastUpdated": "x"}
        self.write(os.path.join(self.cl, "plugins", "known_marketplaces.json"), k)
        self.migrate()
        self.assertEqual(self.read("plugins/known_marketplaces.json")["sampo"], k["sampo"])
        k["sampo"] = {"source": {"source": "github", "repo": "someone/else"}}
        self.migrate(undo=True)
        self.write(os.path.join(self.cl, "plugins", "known_marketplaces.json"), k)
        before = self.bytes()
        with self.assertRaises(ValueError) as e:
            self.migrate()
        self.assertIn("someone/else", str(e.exception))
        self.assertEqual(self.bytes(), before)

    def test_nothing_to_move_says_so(self):
        self.migrate()
        os.remove(os.path.join(self.cl, "sampo-migrate-plugins", "manifest.json"))
        r = self.migrate()
        self.assertFalse(r["changed"])

    def test_the_cli_dry_run_and_undo(self):
        e = {"HOME": self.h}
        p = self.fm("migrate", "plugins", "--dry-run", env=e)
        self.assertIn("foreman@foreman -> sampo@sampo", p.stdout)
        self.assertEqual(self.bytes(), self.before)
        self.assertIn("sampo@sampo", self.fm("migrate", "plugins", env=e).stdout)
        self.fm("migrate", "plugins", "--undo", env=e)
        self.assertEqual(self.bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
