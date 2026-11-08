"""fm doctor: each check passes on good fixtures and fails on bad ones; the command runs end to end."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock

from helpers import PLUGIN, ForemanTestCase, git_repo

from sampo import core as c
from sampo import doctor as d
from sampo import setup


class _TmpDir(unittest.TestCase):
    """A temporary folder and write(); no tests of its own, so a class built on it runs only its own (T-3814)."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.t = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        path = os.path.join(self.t, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path


class Checks(_TmpDir):
    def test_claude_code_features(self):
        # T-0785: a feature the installed Claude Code is too old for silently does nothing; only used features count
        self.write("pl/agents/a.md", "---\nname: a\nomitClaudeMd: true\n---\n")
        self.write("pl/hooks/hooks.json", '{"hooks": {"Stop": [{"hooks": [{"onFailure": "block"}]}]}}')
        pl = os.path.join(self.t, "pl")
        r = d.check_cc_features((2, 1, 100), pl)
        self.assertEqual(r.status, "WARN")
        self.assertIn("omitClaudeMd", r.detail)
        self.assertIn("onFailure", r.detail)
        self.assertNotIn("monitors", r.detail)
        self.assertNotIn("sessionTitle", r.detail)
        self.assertEqual(d.check_cc_features((2, 1, 299), pl).status, "PASS")
        self.assertEqual(d.check_cc_features((), pl).status, "PASS")  # unknown version: nothing to compare

    def test_upstream_issues(self):
        # T-1076: a recurring error that is a known Claude Code bug is named with the version that fixes it
        url = "https://github.com/anthropics/claude-code/issues/25990"
        issues = [{"id": "zsh-heredoc", "pattern": r"can't create temp file for here document", "issue_url": url,
                   "fixed_in": "2.1.47", "note": "sandboxed zsh heredocs"}]
        text = "(eval):1: can't create temp file for here document: read-only file system"
        before, after = "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z"
        update = c.parse_ts("2026-10-02T00:00:00Z").timestamp()  # the installed Claude Code arrived
        err = (after, text)
        r = d.check_upstream([err, (after, "other"), err], (2, 1, 40), issues, update)
        self.assertEqual(r.status, "WARN")
        self.assertIn(url, r.detail)
        self.assertIn("seen 2×", r.detail)
        self.assertIn("fixed in 2.1.47", r.detail)
        self.assertIn("claude update", r.detail)
        r = d.check_upstream([err, err], (2, 1, 50), issues, update)
        self.assertEqual(r.status, "WARN")
        self.assertIn("recurs", r.detail)
        self.assertNotIn("claude update", r.detail)
        # review: errors from before the update that fixed them don't recur; when the update time is unknown, say so
        self.assertEqual(d.check_upstream([(before, text)] * 3 + [err], (2, 1, 50), issues, update).status, "PASS")
        r = d.check_upstream([(before, text)] * 2, (2, 1, 50), issues, 0)
        self.assertEqual(r.status, "WARN")
        self.assertIn("seen 2× in 7 days (possibly before an update)", r.detail)
        r = d.check_upstream([err, err], (), issues, update)
        self.assertIn("installed version unknown", r.detail)
        self.assertNotIn("claude update", r.detail)
        self.assertEqual(d.check_upstream([err], (2, 1, 40), issues, update).status, "PASS")  # once is not a pattern
        self.assertEqual(d.check_upstream([], (2, 1, 40), issues, update).status, "PASS")
        self.assertEqual(d.check_upstream([err, err], (2, 1, 40), [], update).status, "PASS")
        r = d.check_upstream([(after, text.upper()), err], (2, 1, 40), [dict(issues[0], fixed_in=None)], update)
        self.assertEqual(r.status, "WARN")  # case doesn't hide it; an open issue has no fix to name
        self.assertIn("no fix released", r.detail)
        # review: a bad entry is a finding, never a crashed doctor run
        r = d.check_upstream([err, err], (2, 1, 40), [{"id": "broken", "pattern": "("}, "junk"] + issues, update)
        self.assertEqual(r.status, "WARN")
        self.assertIn("broken", r.detail)
        self.assertIn(url, r.detail)

    def test_the_installed_claude_code_arrived_when_its_binary_did(self):
        binary = self.write("versions/2.1.301", "#!/bin/sh\n")
        os.chmod(binary, 0o755)
        os.makedirs(os.path.join(self.t, "bin"))
        os.symlink(binary, os.path.join(self.t, "bin", "claude"))
        self.assertEqual(d._cc_installed_at(os.path.join(self.t, "bin")), os.stat(binary).st_ctime)
        self.assertEqual(d._cc_installed_at(os.path.join(self.t, "none")), 0)  # not found: unknown

    def test_the_shipped_upstream_list_is_well_formed(self):
        issues = json.load(open(os.path.join(PLUGIN, "lib", "upstream-issues.json"), encoding="utf-8"))
        self.assertIsInstance(issues, list)
        ids = [i["id"] for i in issues]
        self.assertEqual(len(ids), len(set(ids)))
        for i in issues:
            self.assertEqual(set(i), {"id", "pattern", "issue_url", "fixed_in", "note"}, i)
            re.compile(i["pattern"])
            self.assertRegex(i["issue_url"], r"^https://github\.com/anthropics/claude-code/issues/\d+$")
            self.assertTrue(i["fixed_in"] is None or re.fullmatch(r"\d+\.\d+\.\d+", i["fixed_in"]), i)

    def test_mod_release(self):
        # T-0191: every other session draws the installed foreman-ui; it lagged this repo (0.4.0 vs 0.5.0) unnoticed
        repo = os.path.join(self.t, "fhome")
        self.write("fhome/mods/foreman-ui/.claude-plugin/plugin.json", json.dumps({"name": "foreman-ui", "version": "0.5.0"}))
        self.write("fhome/mods/foreman-ui/hooks/register.tsx", "new panels\n")
        inst = os.path.join(self.t, "cache", "foreman-ui", "0.4.0")
        self.write("cache/foreman-ui/0.4.0/hooks/register.tsx", "old rows\n")
        listing = self.write("home/.claude/plugins/installed_plugins.json", json.dumps(
            {"plugins": {"foreman-ui@foreman": [{"installPath": inst, "version": "0.4.0"}]}}))
        r = d.check_mod_release(repo, listing)
        self.assertEqual(r.status, "WARN")
        self.assertIn("0.4.0", r.detail)
        self.assertIn("0.5.0", r.detail)
        self.write("cache/foreman-ui/0.4.0/hooks/register.tsx", "new panels\n")  # released: the same code
        self.assertEqual(d.check_mod_release(repo, listing).status, "PASS")
        self.assertEqual(d.check_mod_release(repo, os.path.join(self.t, "none.json")).status, "PASS")  # not installed
        # T-1451: a directory marketplace is read in place (claude plugin list: "Read from" the checkout), so the
        # stale cache record isn't what any session draws
        self.write("cache/foreman-ui/0.4.0/hooks/register.tsx", "old rows\n")
        self.write("home/.claude/plugins/known_marketplaces.json", json.dumps(
            {"foreman": {"source": {"source": "directory", "path": repo + "/"}}}))
        r = d.check_mod_release(repo, listing)
        self.assertEqual(r.status, "PASS", r.detail)
        self.assertIn("in place", r.detail)
        self.write("home/.claude/plugins/known_marketplaces.json", json.dumps(
            {"foreman": {"source": {"source": "github", "repo": "x/foreman"}}}))
        self.assertEqual(d.check_mod_release(repo, listing).status, "WARN")

    def test_low_disk_warns(self):
        # T-1450: the disk filled on 2026-10-22 and hooks failed writing state (ENOSPC) with no warning first
        import collections
        from sampo import core
        Usage = collections.namedtuple("Usage", "total used free")
        for n in range(3):
            self.write(f"volt/.claude/worktrees/agent-{n}/f", "x")
        self.write("gitter/.claude/worktrees/agent-0/f", "x")
        os.makedirs(os.path.join(self.t, "quiet"))
        projects = [(core.Project(name, os.path.join(self.t, name), ""), {}) for name in ("volt", "gitter", "quiet")]
        low = d.check_disk(self.t, lambda _: Usage(900e9, 893e9, 7e9), projects)
        self.assertEqual(low.status, "WARN")
        self.assertIn("7 GB free", low.detail)
        self.assertIn("volt 3", low.detail)
        self.assertIn("gitter 1", low.detail)
        self.assertNotIn("quiet", low.detail)
        self.assertEqual(d.check_disk(self.t, lambda _: Usage(900e9, 600e9, 300e9), projects).status, "PASS")
        self.assertEqual(d.check_disk(self.t, lambda _: Usage(100e9, 88e9, 12e9), projects).status, "PASS")
        self.assertEqual(d.check_disk(self.t, lambda _: Usage(2000e9, 1985e9, 15e9), projects).status, "WARN",
                         "under 5% free is low even with 15 GB left")

    def test_a_busy_machine_skips_hook_timing(self):
        # T-0367: at load 35 JARVIS spent 21 s on 136 timed hook runs, and slow numbers there say nothing of the hooks
        bench = {"Stop": {"p95": 900, "budget": 150, "ok": False, "exit_codes": [0]}}
        r = d.check_hook_latency(bench, busy="load 35.1 on 16 cores")
        self.assertEqual((r.status, "not measured" in r.detail), ("WARN", True))
        self.assertEqual(d.check_hook_latency(bench).status, "FAIL")
        self.assertEqual(d.bench_runs("load 35.1 on 16 cores"), 1)
        self.assertEqual(d.bench_runs(None), 5)

    def test_cpu_pressure_counts_as_busy_before_the_load_average_catches_up(self):
        # T-1219: 16 CPU burners left the 1-minute load at 13.8 on 16 cores while the bench ran slow and doctor failed
        from unittest import mock
        psi = os.path.join(self.t, "cpu")
        with open(psi, "w") as f:
            f.write("some avg10=28.72 avg60=14.95 avg300=16.51 total=17932752381\n")
        with mock.patch.object(d, "PSI_CPU", psi), mock.patch.object(d.os, "getloadavg", lambda: (3.0, 3.0, 3.0)), \
                mock.patch.object(d.os, "cpu_count", lambda: 16):
            self.assertIn("cpu pressure", d.busy() or "")
            with open(psi, "w") as f:
                f.write("some avg10=1.20 avg60=0.90 avg300=0.50 total=1\n")
            self.assertIsNone(d.busy())
        with mock.patch.object(d, "PSI_CPU", os.path.join(self.t, "absent")), \
                mock.patch.object(d.os, "getloadavg", lambda: (40.0, 40.0, 40.0)), \
                mock.patch.object(d.os, "cpu_count", lambda: 16):
            self.assertIn("load", d.busy() or "")  # no PSI (macOS): the load average still decides
        # T-3814: the SLO row's bar (0.5 per CPU): at 0.5-1.5 a hook's p95 runs 174-269 ms against 103-121 under it,
        # and a train's doctor gate failed on SessionStart p95 189 and 156 ms (budget 150) at that load, 2026-11-08
        for load, want in ((10.0, True), (7.0, False)):
            with mock.patch.object(d, "PSI_CPU", os.path.join(self.t, "absent")), \
                    mock.patch.object(d.os, "getloadavg", lambda load=load: (load, load, load)), \
                    mock.patch.object(d.os, "cpu_count", lambda: 16):
                self.assertEqual(bool(d.busy()), want, load)

    def test_python_below_the_floor_fails(self):
        for v, want in [((3, 12, 3), "FAIL"), ((3, 13, 0), "FAIL"), ((3, 12, 7), "PASS"), ((3, 14, 7), "PASS")]:
            self.assertEqual(d.check_python(v).status, want, v)
        self.assertIn("3.12.7", d.check_python((3, 10, 12)).detail)
        # T-0368: below the floor, Foreman runs under a supported interpreter it found, and says which
        r = d.check_python((3, 11, 2), refresh=lambda: "/u/.local/bin/python3.12")
        self.assertEqual((r.status, "/u/.local/bin/python3.12" in r.detail), ("WARN", True))
        r = d.check_python((3, 12, 14), child=True)
        self.assertEqual((r.status, "runs under" in r.detail), ("WARN", True))

    def test_fm_link_says_whether_fm_works_in_any_shell(self):
        # T-1324: an install from before T-0853 had no ~/.local/bin/fm, so fm worked only inside Claude Code
        bin_, target = os.path.join(self.t, "bin"), os.path.join(self.t, "plugin-bin-fm")
        os.makedirs(bin_)
        with open(target, "w") as f:
            f.write("#!/bin/sh\n")
        link = os.path.join(bin_, "fm")
        r = d.check_fm_link(link, target, bin_, shell=False)
        self.assertEqual(r.status, "WARN")
        self.assertIn("fm install-user", r.detail)
        os.symlink(target, link)
        self.assertEqual(d.check_fm_link(link, target, bin_, shell=False).status, "PASS")
        r = d.check_fm_link(link, target, "/usr/bin", shell=False)
        self.assertEqual(r.status, "WARN")
        self.assertIn("PATH", r.detail)
        os.unlink(link)
        os.symlink("/elsewhere/fm", link)
        r = d.check_fm_link(link, target, bin_, shell=False)
        self.assertEqual(r.status, "WARN")
        self.assertIn("/elsewhere/fm", r.detail)

    def test_a_login_shell_that_cant_find_fm_fails_with_the_profile_line(self):  # T-1333
        target = os.path.join(self.t, "plugin-bin-fm")
        with open(target, "w") as f:
            f.write("#!/bin/sh\n")
        link = os.path.join(self.t, "bin", "fm")
        os.makedirs(os.path.dirname(link))
        os.symlink(target, link)

        def shell(name, prints):
            path = os.path.join(self.t, name)
            with open(path, "w") as f:
                f.write(f"#!/bin/sh\n[ \"$PATH\" = /usr/bin:/bin ] || exit 9\n{'echo ' + prints if prints else 'exit 1'}\n")
            os.chmod(path, 0o755)
            return path
        r = d.check_fm_link(link, target, shell=shell("bash", None))
        self.assertEqual(r.status, "FAIL", r)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', r.detail)
        self.assertIn("~/.bash_profile", r.detail)
        self.assertIn("~/.zprofile", d.check_fm_link(link, target, shell=shell("zsh", None)).detail)
        self.assertEqual(d.check_fm_link(link, target, shell=shell("sh", link)).status, "PASS")
        r = d.check_fm_link(link, target, shell=shell("dash", "/usr/local/bin/fm"))
        self.assertEqual(r.status, "FAIL", "another fm comes first")
        self.assertIn("/usr/local/bin/fm", r.detail)

    def test_settings_json(self):
        good = self.write("good.json", '{"a": 1}')
        bad = self.write("bad.json", "{nope")
        self.assertEqual(d.check_settings_json([good]).status, "PASS")
        r = d.check_settings_json([good, bad, os.path.join(self.t, "missing.json")])
        self.assertEqual(r.status, "FAIL")
        self.assertIn("bad.json", r.detail)

    def test_footprint(self):
        rules = self.write("rules.md", "# r\n" + "x\n" * 60)
        md = self.write("CLAUDE.md", "# me\n<!-- foreman:begin -->\n# Foreman\nline\n<!-- foreman:end -->\n")
        self.assertEqual(d.check_footprint(rules, md).status, "PASS")
        long_rules = self.write("long.md", "# r\n" + "x\n" * 90)
        self.assertEqual(d.check_footprint(long_rules, md).status, "FAIL")

    def test_frontmatter(self):
        self.assertEqual(d.check_frontmatter(PLUGIN).status, "PASS")
        root = os.path.join(self.t, "plug")
        self.write("plug/skills/x/SKILL.md", "---\nname: x\ndescription: does things well enough\n---\nbody\n")
        self.write("plug/agents/a.md", "---\nname: a\ndescription: agent\ntools: Read, Write\n---\n400 words\n")
        r = d.check_frontmatter(root)
        self.assertEqual(r.status, "FAIL")
        self.assertIn("Write", r.detail)
        self.write("plug/skills/y/SKILL.md", "no frontmatter here\n")
        self.assertIn("y", d.check_frontmatter(root).detail)
        # T-0768: Claude Code ignores a mistyped effort, maxTurns or omitClaudeMd, so the saving silently never happens
        for field in ("effort: lo", "maxTurns: 0", "omitClaudeMd: yes", "autoCompactWindow: 150k",
                      "autoCompactWindow: ²"):  # isdigit() but int() raises
            self.write("plug/agents/a.md", f"---\nname: a\ndescription: agent\ntools: Read\n{field}\n---\nbody\n")
            self.assertIn(field.split(":")[0], d.check_frontmatter(root).detail, field)
        self.write("plug/agents/a.md", "---\nname: a\ndescription: agent\ntools: Read\neffort: low\nmaxTurns: 20\n"
                                       "omitClaudeMd: true\nautoCompactWindow: 150000\n---\nbody\n")
        self.assertNotIn("a.md", d.check_frontmatter(root).detail)

    def test_every_sonnet_agent_compacts_before_foremans_200k_window(self):
        # T-1797: from CC 2.1.312 a subagent's autoCompactWindow lowers CLAUDE_CODE_AUTO_COMPACT_WINDOW (200k, T-1796);
        # a 40-turn Sonnet reviewer otherwise re-reads up to 200k a reply. Haiku's 20-turn scout never gets there.
        folder = os.path.join(d.PLUGIN, "agents")
        for f in sorted(os.listdir(folder)):
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                head = fh.read().split("\n---", 1)[0]
            if "model: haiku" not in head:
                self.assertRegex(head, r"(?m)^autoCompactWindow: 150000$", f)
        self.assertIn("agent autoCompactWindow", [x[0] for x in d.CC_FEATURES if x[1] == (2, 1, 312)])

    def test_a_skill_that_breaks_the_agent_skills_spec_fails(self):
        # T-1396: name 1-64 lowercase letters, digits and single hyphens, equal to its folder; description 1-1024
        root = os.path.join(self.t, "plug")
        self.write("plug/skills/good/SKILL.md", "---\nname: good\ndescription: does one thing well\n---\nbody\n")
        self.assertEqual(d.check_frontmatter(root).status, "PASS")
        for folder, name, desc, why in (("Bad_Name", "Bad_Name", "fine", "breaks the spec"),
                                        ("x", "y", "fine", "doesn't match its folder"),
                                        ("a" * 65, "a" * 65, "fine", "over 64 characters"),
                                        ("long", "long", "d" * 1025, "description over 1,024"),
                                        ("greedy", "greedy", "Use for every task.", "over-broad description")):
            with self.subTest(why):
                self.write(f"plug/skills/{folder}/SKILL.md", f"---\nname: {name}\ndescription: {desc}\n---\nbody\n")
                r = d.check_frontmatter(root)
                self.assertEqual(r.status, "FAIL")
                self.assertIn(why, r.detail)
                shutil.rmtree(os.path.join(root, "skills", folder))

    def test_skill_descriptions_are_in_task_terms_and_distinct(self):
        # T-1698: the model sees only name and description, so Foreman jargon or two near-copies leave a skill unused
        root = os.path.join(self.t, "plug")
        pre = "---\nname: %s\ndescription: %s\n%s---\nbody\n"
        self.write("plug/skills/a/SKILL.md", pre % ("a", "Use whenever you are about to report where work stands", ""))
        self.assertEqual(d.check_frontmatter(root).status, "PASS")
        self.write("plug/skills/b/SKILL.md", pre % ("b", "Use when STATE says tidy is overdue", ""))
        r = d.check_frontmatter(root)
        self.assertEqual(r.status, "FAIL")
        self.assertIn("jargon", r.detail)
        self.write("plug/skills/b/SKILL.md", pre % ("b", "Use whenever you are about to report where all work stands", ""))
        self.assertIn("overlap", d.check_frontmatter(root).detail)
        # a user-only skill is never in the model's listing
        self.write("plug/skills/b/SKILL.md", pre % ("b", "Use when STATUS is asked", "disable-model-invocation: true\n"))
        self.assertEqual(d.check_frontmatter(root).status, "PASS")

    def test_named_but_never_loaded_skill_warns(self):
        # T-1703: a model-invocable skill named in many sessions and loaded in none is mis-described; a report only
        pre = "---\nname: %s\ndescription: Use whenever you %s\n%s---\nbody\n"
        for n, extra in (("a", ""), ("b", ""), ("next", ""), ("u", "disable-model-invocation: true\n")):
            self.write(f"pl/skills/{n}/SKILL.md", pre % (n, "do thing " + n, extra))
        pl = os.path.join(self.t, "pl")

        def ev(kind, sid, skills):
            return {"kind": kind, "session_id": sid, "skills": skills}
        named = [ev("skill_named", f"s{i}", [n]) for i in range(8) for n in ("a", "b", "next", "u")]
        named += [ev("skill_named", "s0", ["a"])] * 20  # one chatty session is one session, not twenty
        loaded = [ev("slash", "s1", ["b"])]
        r = d.check_unloaded_skills(named + loaded, plugin=pl, n=5)
        self.assertEqual(r.status, "WARN")
        self.assertIn("a ×8", r.detail)
        for quiet in ("b", "next", "u"):  # loaded once / the idle Next line names it every turn / user-only
            self.assertNotIn(quiet + " ×", r.detail)
        self.assertEqual(d.check_unloaded_skills(named[:5 * 4], plugin=pl, n=5).status, "PASS")  # 5 sessions: not more
        self.assertTrue(os.path.isfile(os.path.join(pl, "skills", "a", "SKILL.md")))

    def test_subagent_effort(self):
        # T-3384: each lane class's effort is shown; CLAUDE_CODE_SUBAGENT_EFFORT (2.1.316) would flatten them
        for n, e in (("builder", "high"), ("reviewer", "high"), ("scout", "low")):
            self.write(f"pl/agents/fm-{n}.md", f"---\nname: fm-{n}\neffort: {e}\n---\n")
        pl = os.path.join(self.t, "pl")
        v = (2, 1, 316)
        r = d.check_subagent_effort({}, plugin=pl, environ={}, version=v)
        self.assertEqual(r.status, "PASS")
        self.assertIn("builder=high", r.detail)
        self.assertIn("scout=low", r.detail)
        # a deliberate override is a PASS that says so, only where Claude Code reads it
        r = d.check_subagent_effort({"env": {"CLAUDE_CODE_SUBAGENT_EFFORT": "max"}}, plugin=pl, environ={}, version=v)
        self.assertEqual(r.status, "PASS")
        self.assertIn("overridden to max by CLAUDE_CODE_SUBAGENT_EFFORT; per-class efforts ignored", r.detail)
        self.assertNotIn("unset it", r.detail)
        r = d.check_subagent_effort({}, plugin=pl, environ={"CLAUDE_CODE_SUBAGENT_EFFORT": "low"}, version=v)
        self.assertIn("overridden to low", r.detail)
        old = d.check_subagent_effort({"env": {"CLAUDE_CODE_SUBAGENT_EFFORT": "max"}}, plugin=pl, environ={}, version=(2, 1, 315))
        self.assertNotIn("overridden", old.detail)
        # no effort line (inherits the session's), a quoted value, and an effort: line in the body only
        self.write("pl/agents/fm-builder.md", "---\nname: fm-builder\n---\neffort: high\n")
        self.write("pl/agents/fm-reviewer.md", '---\nname: fm-reviewer\neffort: "medium"\n---\n')
        r = d.check_subagent_effort({}, plugin=pl, environ={}, version=v)
        self.assertIn("builder=inherit", r.detail)
        self.assertIn("reviewer=medium", r.detail)

    def test_worktree_lifetime_gate(self):
        # T-3384: lanes rely on a subagent's worktree surviving its return (2.1.311)
        self.write("pl/agents/fm-builder.md", '---\ndescription: launched with isolation "worktree"\n---\n')
        pl = os.path.join(self.t, "pl")
        r = d.check_cc_features((2, 1, 310), pl)
        self.assertEqual(r.status, "WARN")
        self.assertIn("worktree lifetime", r.detail)
        self.assertEqual(d.check_cc_features((2, 1, 311), pl).status, "PASS")

    def test_worktree_base(self):
        # T-0803: builder lanes need worktree.baseRef head; the default makes them start from the remote's branch
        self.assertEqual(d.check_worktree_base({"worktree": {"baseRef": "head"}}).status, "PASS")
        for s in ({}, {"worktree": {"baseRef": "fresh"}}):
            r = d.check_worktree_base(s)
            self.assertEqual(r.status, "WARN")
            self.assertIn("baseRef", r.detail)

    def test_self_docs_match_the_code(self):
        self.assertEqual(d.check_self_docs().status, "PASS", d.check_self_docs().detail)

    def test_self_docs_hold_fm_help_to_every_command_once(self):
        # T-1011: T-0974 added fm approve-reads with no help tier; only the full suite's test_help noticed, after commit
        from sampo import cli
        from unittest import mock
        cli.build_parser()
        tiers = [(t, " ".join(w for w in n.split() if w != "approve-reads") + (" status teleport" if i == 0 else ""))
                 for i, (t, n) in enumerate(cli.HELP_TIERS)]
        real, saved = cli.build_parser, list(cli.HELP_TIERS)  # T-1378: the parser is built once, so put them back

        def broken():
            parser = real()
            cli.HELP_TIERS[:] = tiers
            return parser
        try:
            with mock.patch.object(cli, "build_parser", broken):
                r = d.check_self_docs()
        finally:
            cli.HELP_TIERS[:] = saved
        self.assertEqual(r.status, "FAIL")
        for needle in ("fm help's tiers lack approve-reads", "list status twice", "teleport isn't an fm command"):
            self.assertIn(needle, r.detail)

    def test_self_docs_flags_drift(self):
        home = os.path.join(self.t, "fh")
        self.write("fh/MASTER.md", "CLI: `fm state|queue`\nSkills: intake. Prose that says doctor and docs doesn't count.\n")
        self.write("fh/README.md", "Flags: `--no-plugins`, `--frobnicate`.\n")
        self.write("fh/install.sh", "#!/bin/bash\n#   --no-plugins     skip\n#   --no-wiring      skip wiring\nset -e\n")
        self.write("fh/plugin/rules/foreman.md", "Run `fm teleport now` then `fm task set T-1 x=y`.\n")
        self.write("fh/plugin/skills/intake/SKILL.md", "---\nname: intake\ndescription: x\n---\n")
        self.write("fh/plugin/skills/extra/SKILL.md", "---\nname: extra\ndescription: x\n---\n")
        r = d.check_self_docs(home, os.path.join(home, "plugin"))
        self.assertEqual(r.status, "FAIL")
        for needle in ("fm teleport", "MASTER.md lacks fm adopt, agents, approve-reads, ask", "doctor", "extra", "--frobnicate", "--no-wiring"):
            self.assertIn(needle, r.detail)

    def test_state_location(self):
        import unittest.mock as mock
        home = os.path.join(self.t, "fh")
        with mock.patch.object(d, "_fs_type", return_value="ext4"):  # T-2639: the test's /tmp may be tmpfs, which warns
            self.assertEqual(d.check_state_dir(home, os.path.join(home, "state")).status, "PASS")
        r = d.check_state_dir(home, os.path.join(self.t, "xdg", "foreman"))
        self.assertEqual(r.status, "WARN")
        self.assertIn("fallback", r.detail)
        self.assertIn("fm doctor --restore-state", r.detail)

    def test_hooks_json_registers_every_handler(self):
        self.assertEqual(d.check_hook_events().status, "PASS")
        self.assertEqual(d.check_hook_events(registered={"PreToolUse", "Stop"}).status, "FAIL")
        self.assertIn("PermissionRequest", d.check_hook_events(registered={"PreToolUse", "Stop"}).detail)

    def test_serve_units(self):
        self.assertEqual(d.check_serve({}).status, "PASS")
        self.assertEqual(d.check_serve({"app-1": "active"}).status, "PASS")
        r = d.check_serve({"app-1": "active", "api-2": "failed"})
        self.assertEqual(r.status, "WARN")
        self.assertIn("api-2 failed", r.detail)
        self.assertIn("fm serve status", r.detail)

    def test_env(self):
        both = dict(setup.ENV)
        self.assertEqual(d.check_env({"env": both}, {}).status, "PASS")
        self.assertEqual(d.check_env({"env": dict(both, CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="50")}, {}).status, "PASS",
                         "the user's own value counts")
        self.assertEqual(d.check_env({}, None).status, "PASS", "not wired (--no-wiring): nothing expected")
        r = d.check_env({"env": {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "60"}}, {})
        self.assertEqual(r.status, "WARN")
        self.assertIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", r.detail)
        self.assertIn("fm install-user", r.detail)

    def test_empty_tool_list_is_not_read_only(self):
        # Claude Code treats an empty or omitted tools list as "every tool" (sub-agents docs).
        for tools in ("[]", ""):
            with self.subTest(tools=tools):
                self.write("empty/agents/a.md", f"---\nname: a\ndescription: agent\ntools: {tools}\n---\n400 words\n")
                r = d.check_frontmatter(os.path.join(self.t, "empty"))
                self.assertEqual(r.status, "FAIL")
                self.assertIn("every tool", r.detail)

    def test_file_map(self):
        home = os.path.join(self.t, "fh")
        for rel in ("plugin/bin/fm", "plugin/lib/x.py", "README.md"):
            self.write(os.path.join("fh", rel), "x")
        master = self.write("fh/MASTER.md", "# M\n## 3. File map\n| Path | Purpose |\n|---|---|\n"
                                             "| `plugin/bin/` | cli |\n| `plugin/lib/` | code |\n| `README.md` | readme |\n"
                                             "| `state/logs/hooks.log` | hook errors (runtime) |\n## 4. Next\n")
        self.assertEqual(d.check_file_map(home, master).status, "PASS")
        self.write("fh/plugin/newdir/thing", "x")
        r = d.check_file_map(home, master)
        self.assertEqual(r.status, "FAIL")
        self.assertIn("plugin/newdir", r.detail)
        master2 = self.write("fh/MASTER2.md", "## 3. File map\n| `plugin/` | all |\n| `gone.md` | x |\n")
        self.assertIn("gone.md", d.check_file_map(home, master2).detail)
        self.assertEqual(d.check_file_map(home, os.path.join(home, "nope.md")).status, "FAIL")

    def test_running_code(self):
        # T-0391: a JARVIS session ran 1.2.3 from its Folder marketplace clone while 1.2.4 was synced into cache
        # folders it never loads; the root and version each session ran must be visible, and a mismatch flagged
        for root, ver in (("old", "1.2.3"), ("new", "1.2.4")):
            self.write(f"{root}/.claude-plugin/plugin.json", json.dumps({"name": "foreman", "version": ver}))
        installed = self.write("installed_plugins.json", json.dumps({"version": 2, "plugins": {"foreman@foreman": [
            {"installPath": os.path.join(self.t, "new"), "version": "1.2.4"}]}}))
        ledger = os.path.join(self.t, "ledger.jsonl")
        with open(ledger, "w") as f:
            for root in ("new", "old"):  # the newest session_start counts
                f.write(json.dumps({"event": "session_start", "data": {"root": os.path.join(self.t, root)}}) + "\n")
        r = d.check_running_code(ledger, installed, plugin=os.path.join(self.t, "new"))  # not this repo's own version
        self.assertEqual(r.status, "WARN")
        self.assertIn("1.2.3", r.detail)
        self.assertIn(os.path.join(self.t, "old"), r.detail)
        with open(ledger, "a") as f:
            f.write(json.dumps({"event": "session_start", "data": {"root": os.path.join(self.t, "new")}}) + "\n")
        self.assertEqual(d.check_running_code(ledger, installed, plugin=os.path.join(self.t, "new")).status, "PASS")
        self.assertEqual(d.check_running_code(os.path.join(self.t, "none.jsonl"), installed, plugin=os.path.join(self.t, "new")).status, "PASS")
        stale = self.write("stale.json", json.dumps({"plugins": {"foreman@foreman": [
            {"installPath": os.path.join(self.t, "old")}]}}))  # a dev checkout newer than the install record
        self.assertEqual(d.check_running_code(ledger, stale, plugin=os.path.join(self.t, "old")).status, "PASS")

    def test_git_hygiene(self):
        repo = git_repo(self.t, "r")
        self.write("r/.gitignore", "state/\n")
        subprocess.run(["git", "-C", repo, "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "ignore"], check=True)
        self.assertEqual(d.check_git_hygiene(repo).status, "PASS")
        self.write("r/state/x.json", "{}")
        subprocess.run(["git", "-C", repo, "add", "-f", "state/x.json"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "oops"], check=True)
        self.assertIn("state/x.json", d.check_git_hygiene(repo).detail)
        subprocess.run(["git", "-C", repo, "rm", "-q", "--cached", "state/x.json"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "fix"], check=True)
        self.write("r/config.py", "API_KEY = 'sk-ant-api03-abcdefghijklmnopqrstu'\n")  # pragma: allowlist secret
        subprocess.run(["git", "-C", repo, "add", "config.py"], check=True)
        r = d.check_git_hygiene(repo)
        self.assertEqual(r.status, "FAIL")
        self.assertIn("config.py", r.detail)
        subprocess.run(["git", "-C", repo, "rm", "-q", "--cached", "config.py"], check=True)
        self.write("r/notes.txt", "".join(f"line {i}\n" for i in range(40)))
        subprocess.run(["git", "-C", repo, "add", "notes.txt"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "notes"], check=True)
        subprocess.run(["git", "-C", repo, "mv", "notes.txt", "moved.txt"], check=True)
        with open(os.path.join(repo, "moved.txt"), "a") as f:  # T-0382: a rename that also adds a secret
            f.write("API_KEY = 'sk-ant-api03-abcdefghijklmnopqrstu'\n")  # pragma: allowlist secret
        subprocess.run(["git", "-C", repo, "add", "moved.txt"], check=True)
        self.assertIn("moved.txt", d.check_git_hygiene(repo).detail)

    def test_backup(self):
        home = os.path.join(self.t, "fh")
        self.assertEqual(d.check_backup(home).status, "WARN", "T-0844: a fresh install has none yet; not a failure")
        self.write("fh/backups/claude-20260101-000000.tgz", "x")
        self.assertEqual(d.check_backup(home).status, "FAIL")  # T-0574: an archive that doesn't open isn't a backup
        import tarfile
        with tarfile.open(os.path.join(home, "backups", "claude-20260102-000000.tgz"), "w:gz") as t:
            t.add(os.path.join(home, "backups", "claude-20260101-000000.tgz"), "x")
        self.assertEqual(d.check_backup(home).status, "PASS")

    def test_worktree_of_installed_repo_warns_not_fails(self):
        """T-3406: install wiring that points at the installed checkout, judged from its git worktree, is a WARN."""
        with tempfile.TemporaryDirectory() as t:
            main, wt = os.path.join(t, "main"), os.path.join(t, "wt")
            for cmd in (["git", "init", "-q", main], ["git", "-C", main, "-c", "user.name=x", "-c", "user.email=x@x",
                                                    "commit", "-q", "--allow-empty", "-m", "i"],
                        ["git", "-C", main, "worktree", "add", "-q", wt]):
                subprocess.run(cmd, check=True, capture_output=True)
            for r in (main, wt):
                os.makedirs(os.path.join(r, "plugin", "hooks"))
            inst, mine = os.path.join(main, "plugin", "hooks", "statusline"), os.path.join(wt, "plugin", "hooks", "statusline")
            for f in (inst, mine):
                open(f, "w").close()
            manifest = {"statusLine_original": {}}
            st =lambda cmd, wrapper: d.check_statusline({"statusLine": {"command": cmd}}, manifest, wrapper).status
            self.assertEqual(st(inst, mine), "WARN")
            self.assertEqual(st(inst + " --flag 'a b'", mine), "WARN")  # args after the path
            self.assertEqual(st(mine, inst), "FAIL")  # stale wiring into a worktree, judged from main
            self.assertEqual(st("/elsewhere/statusline", mine), "FAIL")
            self.assertTrue(d._sibling_worktree(os.path.join(main, "plugin"), os.path.join(wt, "plugin")))
            self.assertFalse(d._sibling_worktree(os.path.join(wt, "plugin"), os.path.join(main, "plugin")))
            self.assertFalse(d._sibling_worktree(os.path.join(t, "plugin"), os.path.join(wt, "plugin")))
            # the rules symlink, same pair both ways (temp HOME)
            home = os.path.join(t, "home")
            os.makedirs(os.path.join(home, ".claude", "rules"))
            for r in (main, wt):
                os.makedirs(os.path.join(r, "plugin", "rules"))
                open(os.path.join(r, "plugin", "rules", "foreman.md"), "w").close()
            link = os.path.join(home, ".claude", "rules", "foreman.md")
            with unittest.mock.patch.dict(os.environ, {"HOME": home}):
                for wired, running, want in ((main, wt, "WARN"), (wt, main, "FAIL")):
                    if os.path.lexists(link):
                        os.remove(link)
                    os.symlink(os.path.join(wired, "plugin", "rules", "foreman.md"), link)
                    self.assertEqual(d.check_rules_symlink(os.path.join(running, "plugin")).status, want)
            # a missing wired target, a bare command, an unbalanced quote, two unrelated repos: all FAIL
            self.assertEqual(st(os.path.join(main, "plugin", "hooks", "gone"), mine), "FAIL")
            self.assertEqual(st("claude-hud", mine), "FAIL")
            self.assertEqual(st("python3 " + inst, mine), "FAIL")
            self.assertEqual(d._cmd_path("a 'b"), "a 'b")  # shlex ValueError: the whole command
            other = os.path.join(t, "other")
            subprocess.run(["git", "init", "-q", other], check=True, capture_output=True)
            os.makedirs(os.path.join(other, "plugin"))
            self.assertFalse(d._sibling_worktree(os.path.join(other, "plugin"), os.path.join(wt, "plugin")))
            self.assertFalse(d._sibling_worktree("", os.path.join(wt, "plugin")))
            with unittest.mock.patch.dict(os.environ, {"HOME": home}):
                os.remove(link)
                os.symlink(os.path.join(main, "plugin", "rules", "gone.md"), link)  # dangling, dir still there
                self.assertEqual(d.check_rules_symlink(os.path.join(wt, "plugin")).status, "FAIL")
            with unittest.mock.patch.object(d, "_run", return_value=subprocess.CompletedProcess([], 127, "", "timeout")):
                self.assertFalse(d._sibling_worktree(main, wt))  # a git failure or timeout is never a WARN

    def test_deny_check_passes_after_the_rm_rules_are_pruned(self):
        # T-3704: the manifest no longer lists the retired rm rules once install-user prunes them
        from sampo import setup
        self.assertFalse([r for r in setup.DENY_RULES if "rm " in r])
        manifest = {"deny_added": list(setup.DENY_RULES)}
        settings = {"permissions": {"deny": list(setup.DENY_RULES) + ["Bash(rm -rf /data)"]}}
        self.assertEqual(d.check_deny_rules(settings, manifest).status, "PASS")

    def test_statusline_and_deny(self):
        wrapper = "/x/plugin/hooks/statusline"
        manifest = {"statusLine_original": {"command": "orig"}, "deny_added": ["Bash(mkfs *)"]}
        settings = {"statusLine": {"command": wrapper}, "permissions": {"deny": ["Bash(mkfs *)"]}}
        self.assertEqual(d.check_statusline(settings, manifest, wrapper).status, "PASS")
        self.assertEqual(d.check_statusline({"statusLine": {"command": "other"}}, manifest, wrapper).status, "FAIL")
        self.assertEqual(d.check_statusline({}, None, wrapper).status, "WARN")
        self.assertEqual(d.check_deny_rules(settings, manifest).status, "PASS")
        self.assertEqual(d.check_deny_rules({"permissions": {"deny": []}}, manifest).status, "FAIL")


class Briefs(ForemanTestCase):
    def test_briefs_and_state(self):
        self.fm("task", "new", "a", "--type", "FIX", "--tier", "S")
        p = c.find_project(self.repo)
        self.assertEqual(d.check_briefs(p).status, "PASS")
        with open(os.path.join(p.dir, "STATE.md"), "a") as f:
            f.write("tampered\n")
        self.assertEqual(d.check_briefs(p).status, "WARN")
        with open(os.path.join(p.dir, "tasks", "T-0009-broken.md"), "w") as f:
            f.write("no frontmatter")
        r = d.check_briefs(p)
        self.assertEqual(r.status, "FAIL")
        self.assertIn("T-0009", r.detail)


class RestoreState(ForemanTestCase):
    def setUp(self):
        super().setUp()
        # T-3222: run.py --sandbox turns the state fallback off for every class; this one is about the fallback
        was = os.environ.pop("FOREMAN_STATE_FALLBACK", None)
        if was is not None:
            self.addCleanup(os.environ.__setitem__, "FOREMAN_STATE_FALLBACK", was)

    def test_restore_state_moves_a_fallback_back_once_the_default_is_writable(self):
        self.fm("init")
        default = os.path.join(self.home, "state")
        os.chmod(self.home, 0o500)
        os.chmod(default, 0o500)
        try:
            self.assertNotEqual(c.state_dir(), default)
            p = self.fm("doctor", "--restore-state", check=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("still isn't writable", p.stderr)
        finally:
            os.chmod(self.home, 0o700)
            os.chmod(default, 0o700)
        self.assertIn(default, self.fm("doctor", "--restore-state").stdout)
        self.assertEqual(c.state_dir(), default)


class Command(ForemanTestCase):
    def test_doctor_json_runs_and_names_every_check(self):
        self.fm("init")
        p = self.fm("doctor", "--json", check=False, env={"HOME": self.tmp})
        self.assertIn(p.returncode, (0, 1))
        names = {r["name"] for r in json.loads(p.stdout)["results"]}
        for n in ("settings json", "hook scripts", "hook latency", "hook exit codes", "injection budgets", "footprint",
                  "frontmatter", "briefs", "file map", "backup", "plugin validate", "git hygiene", "statusline",
                  "deny rules", "rules symlink", "scripts", "name collisions", "hook state writers", "upstream issues"):
            self.assertIn(n, names)

    def test_plain_output_lists_only_problems_unless_verbose(self):
        # T-0844: a newcomer's first doctor run printed 40 lines of passes around the one line that mattered
        self.fm("init")
        quiet = self.fm("doctor", check=False, env={"HOME": self.tmp}).stdout
        loud = self.fm("doctor", "-v", check=False, env={"HOME": self.tmp}).stdout
        self.assertNotIn("PASS ", quiet)
        self.assertIn("PASS ", loud)
        self.assertIn("fm doctor:", quiet)
        self.assertIn("pass", quiet.splitlines()[-1], "the summary still counts them")
        rows = [x[:4] for x in quiet.splitlines() if x[:4] in ("WARN", "FAIL")]
        self.assertIn("FAIL", rows, "a fresh HOME has a failing check, so the order below is tested")
        self.assertEqual(rows, sorted(rows, key=lambda s: s == "FAIL"), "T-2567: failures print last, by the verdict")

    def test_the_tests_skip_the_hook_bench_and_doctor_says_so(self):
        # T-1379: the bench is ~100 hook runs (13 of doctor's 18 s at load 44, past 30 s at 60), and the tests that run
        # fm doctor for its other checks waited on it inside a 30 s limit; FOREMAN_DOCTOR_BENCH=0 (set for every test)
        self.fm("init")
        rs = {r["name"]: r for r in json.loads(self.fm("doctor", "--json", check=False, env={"HOME": self.tmp}).stdout)
              ["results"]}
        for n in ("hook latency", "hook exit codes", "injection budgets"):
            self.assertEqual(rs[n]["status"], "WARN", rs[n])
            self.assertIn("FOREMAN_DOCTOR_BENCH=0", rs[n]["detail"])


class CodeTreeDocs(ForemanTestCase):
    def test_self_docs_and_the_file_map_read_the_running_codes_tree_not_the_home(self):
        # T-3529: a lane (or a train's scratch tree) adding a verb with its docs failed `MASTER.md lacks fm <verb>`:
        # doctor read FOREMAN_HOME's (the main checkout's) docs; here the home is a test's empty folder
        fast = {k: (lambda *a, **kw: d.Result("fast", "PASS")) for k in dir(d)
                if k.startswith("check_") and k not in ("check_self_docs", "check_file_map")}
        with unittest.mock.patch.multiple(d, **fast), unittest.mock.patch.dict(os.environ, {"FOREMAN_DOCTOR_BENCH": "0"}):
            rs = {r.name: r for r in d.run_all()}
        self.assertFalse(os.path.exists(os.path.join(self.home, "MASTER.md")))
        for name in ("self docs", "file map"):
            self.assertEqual(rs[name].status, "PASS", rs[name].detail)


class Timing(ForemanTestCase):
    def test_a_slow_check_tops_the_slowest_list(self):  # T-2279
        import unittest.mock as mock

        def slow():
            time.sleep(0.3)
            return d.Result("disk", "PASS", "stub")
        # every other check is stubbed fast: the real ones read this machine's state and take seconds
        fast = {k: (lambda *a, **kw: d.Result("fast", "PASS")) for k in dir(d) if k.startswith("check_")}
        fast["check_disk"] = slow
        with mock.patch.multiple(d, **fast), mock.patch.dict(os.environ, {"FOREMAN_DOCTOR_BENCH": "0"}):
            rs = d.run_all()
        top = d.slowest(rs)
        self.assertEqual(len(top), 3)
        self.assertEqual(top[0]["name"], "disk")
        self.assertGreaterEqual(top[0]["ms"], 300)
        self.assertEqual(next(r for r in rs if r.name == "disk").ms, top[0]["ms"])
        self.assertEqual([t["ms"] for t in top], sorted((t["ms"] for t in top), reverse=True))

    def _run_stubbed(self, env, **extra):
        import unittest.mock as mock
        fast = {k: (lambda *a, **kw: d.Result("fast", "PASS")) for k in dir(d) if k.startswith("check_")}
        for k, name in (("check_hook_latency", "hook latency"), ("check_hook_exit_codes", "hook exit codes"),
                        ("check_injection_budgets", "injection budgets")):
            fast[k] = lambda *a, _n=name, **kw: d.Result(_n, "PASS")
        fast.update(extra)
        with mock.patch.multiple(d, **fast), mock.patch.dict(os.environ, env):
            return d.run_all()

    def test_a_slow_bench_is_charged_to_hook_latency(self):  # T-2279
        def slow_bench(load):
            time.sleep(0.3)
            return {}
        rs = self._run_stubbed({"FOREMAN_DOCTOR_BENCH": "1"}, bench_runs=slow_bench, busy=lambda: False,
                               _bench_and_injection=lambda b: ({}, {}))
        by = {r.name: r for r in rs}
        self.assertEqual(d.slowest(rs)[0]["name"], "hook latency")
        self.assertGreaterEqual(by["hook latency"].ms, 300)
        self.assertIsNone(by["hook exit codes"].ms)
        self.assertIsNone(by["injection budgets"].ms)

    def test_unmeasured_rows_are_none_and_briefs_and_views_are_timed(self):  # T-2279
        rs = {r.name: r for r in self._run_stubbed({"FOREMAN_DOCTOR_BENCH": "0"})}
        self.assertIsNone(rs["hook latency"].ms)
        self.assertIsNotNone(rs["briefs"].ms)
        self.assertIsNotNone(rs["views"].ms)
        self.assertNotIn("hook latency", [s["name"] for s in d.slowest(list(rs.values()))])

    def test_json_and_verbose_carry_the_timings(self):
        self.fm("init")
        out = json.loads(self.fm("doctor", "--json", check=False, env={"HOME": self.tmp}).stdout)
        self.assertEqual(len(out["slowest"]), 3)
        self.assertTrue(all("ms" in r for r in out["results"]))
        self.assertRegex(self.fm("doctor", "-v", check=False, env={"HOME": self.tmp}).stdout,
                         r"slowest: .+ \d+ ms, .+ \d+ ms, .+ \d+ ms")
        self.assertNotIn("slowest:", self.fm("doctor", check=False, env={"HOME": self.tmp}).stdout)


if __name__ == "__main__":
    unittest.main()


class HookErrors(ForemanTestCase):
    def test_the_latest_error_is_named(self):
        # round 5 (brainstorm, reliability): "2 hook errors" alone sends the user digging through the log
        log = os.path.join(self.home, "state", "logs", "hooks.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w") as f:
            f.write(f"{c.now()} Stop Traceback (most recent call last):\n  File \"x\", line 1\nKeyError: 'drive'\n"
                    f"{c.now()} PreToolUse Traceback (most recent call last):\n  File \"y\", line 2\n"
                    "NameError: name '_new_context_file' is not defined\n")
        r = d.check_hook_errors()
        self.assertEqual(r.status, "WARN")
        self.assertIn("2 hook error(s)", r.detail)
        self.assertIn("PreToolUse NameError: name '_new_context_file' is not defined", r.detail)
        with open(log, "a") as f:
            f.write(f"{c.now()} Stop\x1b]0;x\x07 ValueError: bad \x1b[2Jtitle\n")
        self.assertNotIn("\x1b", d.check_hook_errors().detail)

    def test_a_hot_or_wedged_busy_lock_warns_naming_its_holder(self):  # T-3535
        path = os.path.join(self.home, "state", "logs", "lock-busy.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        holder = "lock busy: ~/p (last known holder: pid 4242, took it 75.0 s ago: sampo focus T-1)"
        for by, held, warn in (({"Stop": 2}, 1.5, False), ({"Stop": 2}, 75.0, True),
                               ({"builder mark": 51}, None, True)):
            with open(path, "w") as f:
                json.dump({"since": c.now(), "last": c.now(), "by": by, "holder": holder, "held": held}, f)
            r = d.check_hook_errors()
            self.assertEqual(r.status, "WARN" if warn else "PASS", (by, held, r.detail))
            if warn:
                self.assertIn("pid 4242", r.detail)
                self.assertIn(f"{sum(by.values())} hook write(s) skipped", r.detail)

    def test_doctor_feeds_its_fails_and_hook_tracebacks_to_the_signal_to_fix_loop(self):  # T-2349
        self.fm("init")
        p = c.find_project(self.repo)
        log = os.path.join(self.home, "state", "logs", "hooks.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w") as f:  # three hook runs in three hours hit one traceback; a paused-line is not a traceback
            for i in range(3):
                f.write(f"{c.iso(time.time() - 7200 * (i + 1))} Stop Traceback (most recent call last):\n  File \"x\", line 1\n"
                        "KeyError: 'drive'\n")
            f.write(f"{c.now()} Stop paused for 10 min after 3 failures in a row (it runs again at 10:00)\n")
        results = [d.Result("hook latency", "FAIL", "PreToolUse p95 41ms > 25ms"),
                   d.Result("footprint", "WARN", "big"), d.Result("disk", "PASS", "fine")]
        for sid in ("s1", "s2", "s3"):
            made = d.feed_self_failures(p, results, sid)
        titles = [b.title for b in c.load_briefs(p)]
        self.assertEqual(len([t for t in titles if "hook latency" in t]), 1, titles)
        self.assertEqual(len([t for t in titles if "keyerror" in t]), 1, titles)
        self.assertFalse([t for t in titles if "footprint" in t or "paused" in t], "a WARN or a pause is not a failure")
        self.assertEqual(len(made), 1, "the third session filed the latency one (the tracebacks were filed at the first)")
        self.assertEqual(d.feed_self_failures(p, results, "s4"), [], "a fourth session files nothing more")

    def test_one_hours_tracebacks_of_one_bug_are_one_run_and_a_failing_feed_does_not_stop_the_rest(self):  # T-2349 review
        from unittest import mock
        from sampo import recall
        self.fm("init")
        p = c.find_project(self.repo)
        log = os.path.join(self.home, "state", "logs", "hooks.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w") as f:  # a storm: five tracebacks of one bug inside one hour is one run, not five
            for i in range(5):
                f.write(f"2099-01-01T00:0{i}:00Z Stop Traceback (most recent call last):\n  File \"x\", line 1\n"
                        "KeyError: 'drive'\n")
        d.feed_self_failures(p, [], "s1")
        self.assertEqual([b for b in c.load_briefs(p) if "keyerror" in b.title], [])
        open(log, "w").close()
        seen, real = [], recall.note_own

        def flaky(pp, text, who, *a, **k):
            seen.append(text)
            if len(seen) == 1:
                raise RuntimeError("lock gone")
            return real(pp, text, who, *a, **k)
        results = [d.Result("one", "FAIL", "a"), d.Result("two", "FAIL", "b")]
        with mock.patch.object(recall, "note_own", flaky):
            d.feed_self_failures(p, results, "s1")
        self.assertEqual(len(seen), 2, "the second feed ran after the first raised")

    def test_an_error_from_code_changed_since_is_stale(self):
        # T-1139: NameError BATCH_AT came from a half-written edit; the line it names no longer reads that way
        log = os.path.join(self.home, "state", "logs", "hooks.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        src = os.path.join(d.PLUGIN, "lib", "sampo", "doctor.py")
        with open(src) as f:
            first = f.readline().rstrip("\n")
        frame = f"  File \"{src}\", line 1, in <module>\n"
        with open(log, "w") as f:
            f.write(f"{c.now()} PostToolUse Traceback (most recent call last):\n{frame}    if n == BATCH_AT and x:\n"
                    "NameError: name 'BATCH_AT' is not defined\n")
        r = d.check_hook_errors()
        self.assertEqual(r.status, "PASS", r.detail)
        self.assertIn("changed since", r.detail)
        with open(log, "a") as f:  # the same line as the file has now: a live error
            f.write(f"{c.now()} PostToolUse Traceback (most recent call last):\n{frame}    {first.strip()}\n"
                    "ValueError: still broken\n")
        self.assertEqual(d.check_hook_errors().status, "WARN")

    def test_upstream_reads_a_week_of_hook_errors_and_failures(self):
        # T-1076: hooks.log entries and every project's failure signatures of the last 7 days, matched case-blind
        self.fm("init")
        p = c.find_project(self.repo)
        ago = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
        log = os.path.join(self.home, "state", "logs", "hooks.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w") as f:
            f.write(f"{ago(10)} SubagentStop failed: classifyHandoffIfNeeded is not defined\n"  # too old
                    f"{ago(3)} SubagentStop Traceback (most recent call last):\n"
                    "ReferenceError: classifyHandoffIfNeeded is not defined\n")
        with open(os.path.join(p.dir, "failures.jsonl"), "a") as f:
            rec = {"sig": "agent failed: classifyhandoffifneeded is not defined", "task": None, "at": c.now()}
            f.write(json.dumps(rec) + "\n" + json.dumps(dict(rec, hinted=True)) + "\n[1]\n")  # neither is a new error
        r = d.check_upstream(version=(2, 1, 40))
        self.assertEqual(r.status, "WARN")
        self.assertIn("issues/22087 (seen 2×)", r.detail)
        self.assertEqual(d.check_upstream(errors=[], version=(2, 1, 40)).status, "PASS")


class CoreIntegrity(unittest.TestCase):
    """Round 7: protected core that differs from the last commit is named (a tamper and half-edit check)."""

    def test_uncommitted_core_changes_are_named(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(d.check_core_integrity(t).status, "PASS", "not a git checkout: nothing to compare")
            repo = git_repo(t, "fh")
            os.makedirs(os.path.join(repo, "plugin", "lib"))
            path = os.path.join(repo, "plugin", "lib", "fmx.py")
            with open(path, "w") as f:
                f.write("x = 1\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                           check=True)
            self.assertEqual(d.check_core_integrity(repo).status, "PASS")
            with open(path, "w") as f:
                f.write("x = 2\n")
            r = d.check_core_integrity(repo)
            self.assertEqual(r.status, "WARN")
            self.assertIn("plugin/lib/fmx.py", r.detail)
            wt = os.path.join(t, "wt")  # a worktree's .git is a file, and it is still a checkout
            subprocess.run(["git", "-C", repo, "worktree", "add", "-q", "--detach", wt], check=True)
            with open(os.path.join(wt, "plugin", "lib", "fmx.py"), "w") as f:
                f.write("x = 3\n")
            self.assertEqual(d.check_core_integrity(wt).status, "WARN")

    def test_core_the_active_task_changes_in_scope_passes_and_names_it(self):
        # T-1998: every fm check of a core task warned "git checkout it back" about the task's own edits
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as t:
            repo = git_repo(t, "fh")
            os.makedirs(os.path.join(repo, "plugin", "lib"))
            for n in ("fmx.py", "fmy.py"):
                with open(os.path.join(repo, "plugin", "lib", n), "w") as f:
                    f.write("x = 1\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                           check=True)
            with open(os.path.join(repo, "plugin", "lib", "fmx.py"), "w") as f:
                f.write("x = 2\n")
            task = SimpleNamespace(id="T-1", meta={"scope": ["plugin/lib/fmx.py"]})
            r = d.check_core_integrity(repo, task)
            self.assertEqual((r.status, "T-1" in r.detail), ("PASS", True), r.detail)
            self.assertEqual(d.check_core_integrity(repo, SimpleNamespace(id="T-1", meta={})).status, "WARN")
            with open(os.path.join(repo, "plugin", "lib", "fmy.py"), "w") as f:
                f.write("y = 2\n")
            r = d.check_core_integrity(repo, task)
            self.assertEqual(r.status, "WARN")
            self.assertIn("plugin/lib/fmy.py", r.detail)
            self.assertNotIn("fmx.py", r.detail)

    def test_description_budget(self):
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, "skills", "big"))
            with open(os.path.join(t, "skills", "big", "SKILL.md"), "w") as f:
                f.write("---\nname: big\ndescription: " + "x" * 7000 + "\n---\n")
            r = d.check_footprint(os.path.join(t, "none.md"), os.path.join(t, "none.md"), plugin=t)
            self.assertEqual(r.status, "FAIL")
            self.assertIn("descriptions", r.detail)
            dense = os.path.join(t, "dense.md")  # few lines, many tokens: the line budget alone misses it
            with open(dense, "w") as f:
                f.write(("x" * 300 + "\n") * 40)
            r = d.check_footprint(dense, os.path.join(t, "none.md"), plugin=os.path.join(t, "none"))
            self.assertEqual(r.status, "FAIL")
            self.assertIn("chars", r.detail)
        self.assertEqual(d.check_footprint(os.path.join(PLUGIN, "rules", "foreman.md"),
                                           os.path.join(PLUGIN, "none.md")).status, "PASS")


class FootprintRatchet(unittest.TestCase):  # T-2281
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t)
        self.p = type("P", (), {"dir": self.t})()
        self.md = os.path.join(self.t, "none.md")
        self.plugin = os.path.join(self.t, "plugin")
        os.makedirs(self.plugin)

    def rules(self, n):
        path = os.path.join(self.t, "rules.md")
        with open(path, "w") as f:
            f.write("x" * (n - 1) + "\n")
        return path

    def decide(self, date, text):
        with open(os.path.join(self.t, "decisions.md"), "a") as f:
            f.write(f"| {date} | {text} | why |\n")

    def check(self, n):
        return d.check_footprint(self.rules(n), self.md, plugin=self.plugin, project=self.p)

    def test_no_record_passes_and_a_record_within_two_percent_passes(self):
        self.assertEqual(self.check(1000).status, "PASS")
        d.record_footprint(self.p, self.rules(1000), self.plugin)
        self.assertEqual(self.check(1020).status, "PASS")

    def test_a_zero_or_empty_record_is_ignored(self):
        for rec in ({"at": "2000-01-01", "metrics": {"rules chars": 0, "descriptions chars": 0}}, {"at": "x"}, {"metrics": {}}):
            with open(os.path.join(self.t, "footprint.json"), "w") as f:
                json.dump(rec, f)
            self.assertEqual(self.check(1000).status, "PASS")

    def test_growth_over_two_percent_warns_naming_the_metric(self):
        d.record_footprint(self.p, self.rules(1000), self.plugin)
        r = self.check(1021)
        self.assertEqual(r.status, "WARN")
        self.assertIn("rules chars", r.detail)
        self.assertIn("cut", r.detail)
        self.assertNotIn("fm decide", r.detail)

    def test_a_decision_naming_the_metric_no_longer_clears_it(self):  # T-2358: a session can't waive its own gate
        d.record_footprint(self.p, self.rules(1000), self.plugin)
        self.decide("2999-01-01", "footprint: rules chars +5% for the reply format")
        self.assertEqual(self.check(1050).status, "WARN")

    BENCH = {"PreToolUse:Bash": {"p95": 40.0, "cpu_p95": 30.0, "load": 0.2},
             "Stop": {"p95": 90.0, "cpu_p95": 60.0, "load": 2.5}}

    def test_measured_metrics_are_injected_chars_and_each_hooks_p95(self):  # T-2358: what gen-0 section 6 names
        got = d.measured(self.BENCH, {"SessionStart": 1200, "UserPromptSubmit": 0})
        self.assertEqual(got, {"SessionStart chars": 1200, "PreToolUse:Bash p95 ms": 40.0})
        # T-2358 review: a loaded run's timing is another basis and a 0 size is a hook that failed: neither is measured
        self.assertEqual(d.measured(None, None), {})

    def record(self, rules=1000, **m):
        d.record_footprint(self.p, self.rules(rules), self.plugin, extra=m)

    def checkm(self, rules=1000, **m):
        return d.check_footprint(self.rules(rules), self.md, plugin=self.plugin, project=self.p, extra=m)

    def test_a_hooks_p95_has_a_fifteen_percent_band_and_counts_only_when_measured(self):
        self.record(**{"Stop p95 ms": 40.0})
        self.assertEqual(self.checkm(**{"Stop p95 ms": 45.0}).status, "PASS")  # +12.5%: jitter
        r = self.checkm(**{"Stop p95 ms": 47.0})
        self.assertEqual(r.status, "WARN")
        self.assertIn("Stop p95 ms", r.detail)
        self.assertEqual(self.checkm().status, "PASS")  # not measured this time: not judged

    def test_injected_chars_ride_the_two_percent_band(self):
        self.record(**{"SessionStart chars": 2000})
        self.assertEqual(self.checkm(**{"SessionStart chars": 2040}).status, "PASS")
        self.assertIn("SessionStart chars", self.checkm(**{"SessionStart chars": 2041}).detail)

    def test_only_a_cut_of_at_least_the_growth_in_the_same_unit_clears_it(self):  # T-2358
        self.record(**{"SessionStart chars": 2000, "Stop p95 ms": 100.0})
        self.assertEqual(self.checkm(1050, **{"SessionStart chars": 1960}).status, "WARN")  # cut 40 < grew 50
        self.assertEqual(self.checkm(1050, **{"SessionStart chars": 1950}).status, "PASS")  # cut 50 pays for 50
        self.assertEqual(self.checkm(1050, **{"SessionStart chars": 2000, "Stop p95 ms": 10.0}).status,
                         "WARN")  # ms saved don't pay for chars added

    def test_a_hook_slower_is_never_paid_by_another_hook_or_a_skill_trim(self):  # T-2358 review
        self.record(**{"Stop p95 ms": 100.0, "PreToolUse:Bash p95 ms": 200.0})
        self.assertEqual(self.checkm(**{"Stop p95 ms": 150.0, "PreToolUse:Bash p95 ms": 100.0}).status, "WARN")
        rec = os.path.join(self.t, "footprint.json")
        with open(rec, "w") as f:
            json.dump({"at": "2000-01-01", "metrics": {"rules chars": 1000, "largest SKILL chars": 5000}}, f)
        # no SKILL in this plugin: largest SKILL reads 0, as a trimmed lazily loaded body would
        self.assertEqual(self.check(1050).status, "WARN")  # a SKILL body isn't always-on: its trim pays for nothing

    def test_the_record_is_a_ratchet_growth_with_no_cut_keeps_the_old_value(self):  # T-2358 review
        rec = os.path.join(self.t, "footprint.json")
        self.record(1000)
        self.record(1050)
        with open(rec) as f:
            self.assertEqual(json.load(f)["metrics"]["rules chars"], 1000)  # the next ship can't absorb it
        self.assertEqual(self.checkm(1050).status, "WARN")
        self.record(990)
        with open(rec) as f:
            self.assertEqual(json.load(f)["metrics"]["rules chars"], 990)

    def test_shrinking_passes_and_the_fixed_cap_still_fails(self):
        d.record_footprint(self.p, self.rules(1000), self.plugin)
        self.assertEqual(self.check(500).status, "PASS")
        self.assertEqual(self.check(d.RULES_CHARS_MAX + 1).status, "FAIL")
        self.assertEqual(d.RULES_CHARS_MAX, 6400)


class DoctorStateSize(ForemanTestCase):
    # T-0920: state grows quietly (ledger, research, outputs); past a budget doctor says so and names the fix
    def test_a_project_past_the_budget_warns_with_its_biggest_part_and_fm_tidy(self):
        self.fm("init")
        p = c.find_project(self.repo)
        os.makedirs(os.path.join(p.dir, "research"), exist_ok=True)
        with open(os.path.join(p.dir, "research", "big.md"), "w") as f:
            f.write("x" * 50_000)
        self.assertEqual(d.check_state_size([p], budget=1_000_000).status, "PASS")
        os.makedirs(os.path.join(p.dir, "backups"), exist_ok=True)  # round-7 review: tidy's own backups don't count
        with open(os.path.join(p.dir, "backups", "20260101T000000Z.tar.gz"), "w") as f:
            f.write("x" * 2_000_000)
        self.assertEqual(d.check_state_size([p], budget=1_000_000).status, "PASS")
        r = d.check_state_size([p], budget=20_000)
        self.assertEqual(r.status, "WARN")
        self.assertIn(p.slug, r.detail)
        self.assertIn("research", r.detail)
        self.assertIn("fm tidy", r.detail)

    def test_an_exam_heavy_project_is_told_tidy_doesnt_prune_exam_runs(self):  # T-2538
        self.fm("init")
        p = c.find_project(self.repo)
        os.makedirs(os.path.join(p.dir, "exam", "long", "l001"), exist_ok=True)
        with open(os.path.join(p.dir, "exam", "long", "l001", "work.tar"), "w") as f:
            f.write("x" * 60_000)
        r = d.check_state_size([p], budget=20_000)
        self.assertEqual(r.status, "WARN")
        self.assertIn("exam", r.detail)
        self.assertIn("fm tidy --apply prunes graded exam runs' old work copies", r.detail)
        self.assertNotIn("fm tidy --apply prunes and rolls it", r.detail)


class ScanCache(ForemanTestCase):
    # T-2298: usage burn and unloaded skills reuse their result while the files they read are unchanged
    def setUp(self):
        super().setUp()
        from sampo import cost
        self.fmcost = cost
        self.calls = {"burn": 0, "events": 0}
        self.cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(os.path.join(self.cfg, "projects", "-p"))
        self.transcript = os.path.join(self.cfg, "projects", "-p", "s.jsonl")
        with open(self.transcript, "w") as f:
            f.write("{}\n")
        self._saved = (cost.burn_findings, c.global_events, os.environ.get("CLAUDE_CONFIG_DIR"))
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg

        def burn(*a, **k):
            self.calls["burn"] += 1
            return [("slow", "fix")]

        def events(since):
            self.calls["events"] += 1
            return []
        cost.burn_findings, c.global_events = burn, events
        self.addCleanup(self.restore)

    def restore(self):
        self.fmcost.burn_findings, c.global_events = self._saved[:2]
        if self._saved[2] is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._saved[2]

    def mid_hour(self, *paths):
        t = time.time() // 3600 * 3600 + 1800
        for p in paths:
            os.utime(p, (t, t))
        return t

    def grow(self, path, seconds=2 * 3600):  # default: the mtime crosses an hour boundary
        t = os.stat(path).st_mtime + seconds  # T-2429: from the mtime before the append, which resets it to now (in
        with open(path, "a") as f:  # an hour's last minute, now + 60 s was the next hour: a cache miss)
            f.write("{}\n")
        os.utime(path, (t, t))

    def test_an_append_within_the_hour_still_hits(self):
        events = os.path.join(c.state_dir(), "events.jsonl")
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(events, "w") as f:
            f.write("{}\n")
        self.mid_hour(events, self.transcript)
        d.check_usage_burn()
        d.check_unloaded_skills()
        self.grow(events, 60)
        self.grow(self.transcript, 60)
        d.check_usage_burn()
        d.check_unloaded_skills()
        self.assertEqual(self.calls, {"burn": 1, "events": 1})

    def test_the_forecast_is_keyed_on_the_hours_it_shows(self):
        from sampo import budget as sbudget
        saved, n = sbudget.forecast, [0]

        def fc(now=None):
            n[0] += 1  # full_at moves with the clock; the hours before the reset shown don't
            return {"seven_day": {"resets_at": 1_000_000.0, "full_at": 1_000_000.0 - 7200 + n[0]}}
        sbudget.forecast = fc
        self.addCleanup(setattr, sbudget, "forecast", saved)
        d.check_usage_burn()
        d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 1)

    def test_an_edited_check_recomputes_the_same_day(self):  # T-2314
        d.check_usage_burn()
        saved = d._code_stamp
        d._code_stamp = lambda: saved() + "-edited"
        self.addCleanup(setattr, d, "_code_stamp", saved)
        d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 2)

    def test_an_entry_written_while_another_computes_is_kept(self):
        # T-3521: the model probe takes 20-70 s; a doctor in another lane writing meanwhile lost its entries to it
        def slow():
            d.check_unloaded_skills()  # another doctor's write, mid-compute
            return d.Result("probe", "PASS", "ok")
        d._cached("probe", [], slow)
        d.check_unloaded_skills()
        d._cached("probe", [], slow)
        self.assertEqual(self.calls["events"], 1, "unloaded skills' entry survived the probe's write")

    def test_the_code_stamp_follows_content_not_mtime(self):  # T-3521: a lane's checkout of main's doctor.py shares
        src = os.path.join(self.tmp, "doctor_copy.py")
        with open(src, "w") as f:
            f.write("x = 1\n")
        with unittest.mock.patch.object(d, "__file__", src):
            first = d._code_stamp()
            os.utime(src, (1, 1))
            self.assertEqual(d._code_stamp(), first, "the same code checked out again")
            with open(src, "a") as f:
                f.write("y = 2\n")
            self.assertNotEqual(d._code_stamp(), first, "an edited check")

    def test_another_checkout_of_the_same_code_reuses_the_cache(self):
        # T-3565: the key held doctor.py's mtime and the plugin's path, both different in a lane's checkout: a lane's
        # doctor and main's recomputed and rewrote doctor-cache.json for each other
        path = os.path.join(c.state_dir(), "doctor-cache.json")
        d.check_usage_burn()
        d.check_unloaded_skills()
        lane = os.path.join(self.tmp, "lane")  # the same files, a checkout's fresh mtimes
        shutil.copytree(os.path.join(d.PLUGIN, "skills"), os.path.join(lane, "skills"), copy_function=shutil.copyfile)
        code = os.path.join(lane, "doctor.py")
        shutil.copyfile(d.__file__, code)
        os.utime(code, ns=(1, 1))
        st = os.stat(path)
        with unittest.mock.patch.object(d, "__file__", code):
            d.check_usage_burn()
            d.check_unloaded_skills(plugin=lane)
        self.assertEqual(self.calls, {"burn": 1, "events": 1})
        self.assertEqual((os.stat(path).st_ino, os.stat(path).st_mtime_ns), (st.st_ino, st.st_mtime_ns))

    def test_an_odd_cache_file_recomputes(self):
        path = os.path.join(c.state_dir(), "doctor-cache.json")
        d.check_usage_burn()
        with open(path, "w") as f:
            f.write("[" * 100_000)  # T-1605 class: RecursionError, not ValueError
        d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 2)
        with open(path, "w") as f:
            f.write("[]")
        d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 3)
        with open(path) as f:
            good = json.load(f)["usage burn"]
        for result in (["usage burn", "BOGUS", "x"], ["usage burn", "PASS", 5], "text"):
            with open(path, "w") as f:
                json.dump({"usage burn": {"key": good["key"], "result": result}}, f)
            d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 6)

    def test_load_json_survives_a_deep_nested_file(self):  # T-1605 class
        path = os.path.join(self.tmp, "deep.json")
        with open(path, "w") as f:
            f.write("[" * 100_000)
        self.assertIsNone(d._load_json(path))

    def test_a_cached_detail_is_plain_and_short(self):
        d.check_usage_burn()
        path = os.path.join(c.state_dir(), "doctor-cache.json")
        with open(path) as f:
            cache = json.load(f)
        cache["usage burn"]["result"] = ["usage burn", "WARN", "a\x1b[31mb" + "x" * 5000]
        with open(path, "w") as f:
            json.dump(cache, f)
        r = d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 1)
        self.assertNotIn("\x1b", r.detail)
        self.assertLessEqual(len(r.detail), 2000)

    def test_usage_burn_reuses_its_result_until_a_transcript_changes(self):
        first = d.check_usage_burn()
        self.assertEqual(d.check_usage_burn(), first)
        self.assertEqual(self.calls["burn"], 1)
        self.grow(self.transcript)
        d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 2)

    def test_a_key_over_the_read_cap_still_hits_and_keeps_the_other_entries(self):
        # T-3521: usage burn's key named every transcript of 2 days (785 of them): the file passed the 64 KB read cap,
        # so it never parsed and every doctor run re-probed the model pins (20 s) and rescanned the transcripts
        for i in range(600):
            with open(os.path.join(self.cfg, "projects", "-p", f"{i:04d}-" + "x" * 120 + ".jsonl"), "w") as f:
                f.write("{}\n")
        d.check_unloaded_skills()
        d.check_usage_burn()
        self.assertLess(os.path.getsize(os.path.join(c.state_dir(), "doctor-cache.json")), 65536)
        d.check_usage_burn()
        d.check_unloaded_skills()
        self.assertEqual(self.calls, {"burn": 1, "events": 1})
        self.grow(self.transcript)
        d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 2, "a changed transcript still recomputes")

    def test_unloaded_skills_reuses_its_result_until_events_change(self):
        events = os.path.join(c.state_dir(), "events.jsonl")
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(events, "w") as f:
            f.write("{}\n")
        first = d.check_unloaded_skills()
        self.assertEqual(d.check_unloaded_skills(), first)
        self.assertEqual(self.calls["events"], 1)
        self.grow(events)
        d.check_unloaded_skills()
        self.assertEqual(self.calls["events"], 2)

    def test_the_cache_is_json_and_a_corrupt_one_recomputes(self):
        d.check_usage_burn()
        path = os.path.join(c.state_dir(), "doctor-cache.json")
        with open(path, encoding="utf-8") as f:
            self.assertIn("usage burn", json.load(f))
        with open(path, "w") as f:
            f.write("{not json")
        d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 2)
        os.remove(path)
        d.check_usage_burn()
        self.assertEqual(self.calls["burn"], 3)


class DoctorDualInstall(ForemanTestCase):
    # T-0923: Foreman from a GitHub marketplace beside install.sh's copy in its home: sessions run either, and drift
    def setUp(self):
        super().setUp()
        self.t = tempfile.mkdtemp(dir=self.tmp)
        self.fmhome = os.path.join(self.t, "foreman")
        for root, v in ((os.path.join(self.fmhome, "plugin"), "1.2.28"), (os.path.join(self.t, "cache", "1.2.20"), "1.2.20")):
            os.makedirs(os.path.join(root, ".claude-plugin"))
            with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w") as f:
                json.dump({"name": "foreman", "version": v}, f)
        self.installed = os.path.join(self.t, "installed.json")
        with open(self.installed, "w") as f:
            json.dump({"plugins": {"foreman@foreman": [{"version": "1.2.20", "installPath": os.path.join(self.t, "cache", "1.2.20")}]}}, f)
        self.known = os.path.join(self.t, "known.json")

    def known_as(self, source):
        with open(self.known, "w") as f:
            json.dump({"foreman": {"source": source}}, f)

    def test_a_github_install_beside_this_home_at_another_version_warns(self):
        self.known_as({"source": "github", "repo": "ChaseSunstrom/sampo"})
        r = d.check_dual_install(self.fmhome, self.known, self.installed)
        self.assertEqual(r.status, "WARN")
        self.assertIn("1.2.20", r.detail)
        self.assertIn("1.2.28", r.detail)

    def test_a_half_moved_install_warns_with_the_fix(self):  # T-2879 review: both ids enabled loads Sampo twice
        both = {"enabledPlugins": {"foreman@foreman": True, "sampo@sampo": True, "sampo-ui@sampo": True}}
        r = d.check_half_moved(both)
        self.assertEqual(r.status, "WARN")
        self.assertIn("twice", r.detail)
        self.assertIn("foreman@foreman", r.detail)
        self.assertNotIn("foreman-ui@foreman", r.detail)
        self.assertIn("sampo migrate plugins", r.detail)  # T-3707: the move drops the old id in the same write
        for one in ({"enabledPlugins": {"sampo@sampo": True}}, {"enabledPlugins": {"foreman@foreman": False, "sampo@sampo": True}},
                    {}):
            self.assertEqual(d.check_half_moved(one).status, "PASS", one)

    def test_a_machine_still_on_an_old_id_is_told_the_migrate_command(self):  # T-3707
        for old in ({"foreman@foreman": True, "foreman-ui@foreman": True}, {"sampo@foreman": True}):
            r = d.check_half_moved({"enabledPlugins": old})
            self.assertEqual(r.status, "WARN", old)
            self.assertIn("sampo migrate plugins", r.detail)
            self.assertIn("sampo@sampo", r.detail)
            self.assertIn(next(iter(old)), r.detail)
            self.assertNotIn("twice", r.detail)

    def test_an_install_recorded_under_either_id_counts(self):  # T-2879: sampo@foreman once moved, foreman@foreman before
        self.known_as({"source": "github", "repo": "ChaseSunstrom/sampo"})
        with open(self.installed) as f:
            data = json.load(f)
        data["plugins"] = {"sampo@foreman": data["plugins"].pop("foreman@foreman")}
        with open(self.installed, "w") as f:
            json.dump(data, f)
        r = d.check_dual_install(self.fmhome, self.known, self.installed)
        self.assertEqual(r.status, "WARN")
        self.assertIn("claude plugin update sampo@foreman", r.detail)

    def test_the_marketplace_being_this_home_is_one_install(self):
        self.known_as({"source": "directory", "path": self.fmhome})
        self.assertEqual(d.check_dual_install(self.fmhome, self.known, self.installed).status, "PASS")
        os.remove(self.installed)
        self.assertEqual(d.check_dual_install(self.fmhome, self.known, self.installed).status, "PASS")


class BoardDoctor(unittest.TestCase):  # T-0954
    def test_invisible_text_in_instruction_files_warns_with_file_and_line(self):
        with tempfile.TemporaryDirectory() as t:
            bad, clean = os.path.join(t, "CLAUDE.md"), os.path.join(t, "rules.md")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("# Rules\nbe nice​\nok ‮evil‬\nhidden \U000E0041 tag\n")
            with open(clean, "w", encoding="utf-8") as f:
                f.write("# Rules\nnothing hidden — even em dashes and émojis 🎉\n")
            r = d.check_invisible_text([bad, clean, os.path.join(t, "missing.md")])
            self.assertEqual(r.status, "WARN")
            for line in (2, 3, 4):
                self.assertIn(f"CLAUDE.md:{line}", r.detail)
            self.assertNotIn("rules.md", r.detail)
            self.assertEqual(d.check_invisible_text([clean]).status, "PASS")
            for hidden in ("joined⁠", "﻿mid-file", "soft­hyphen", "vs︁", "é‍‍é",
                           "a️", "sup\U000e0101"):
                with open(bad, "w", encoding="utf-8") as f:
                    f.write("ok\n" + hidden + "\n")
                self.assertEqual(d.check_invisible_text([bad]).status, "WARN", repr(hidden))
            with open(clean, "w", encoding="utf-8") as f:
                f.write("﻿# BOM first\nfamily 👨‍👩‍👧 and ❤\n")
            self.assertEqual(d.check_invisible_text([clean]).status, "PASS")
            from sampo import core  # T-1687: VS15/16 are invisible wherever they sit; fmcore.plain drops them, doctor flags them
            for hidden in ("❤️", "é︎", "☺︎ plain"):
                self.assertNotEqual(core.plain(hidden), hidden)
                with open(bad, "w", encoding="utf-8") as f:
                    f.write("ok\n" + hidden + "\n")
                self.assertEqual(d.check_invisible_text([bad]).status, "WARN", repr(hidden))

    def test_the_shipped_instruction_files_are_clean(self):
        files = [f for f in d.instruction_files() if f.startswith(PLUGIN)]
        self.assertTrue(any(f.endswith(os.path.join("output-styles", "foreman.md")) for f in files))
        self.assertTrue(any(os.sep + "references" + os.sep in f for f in files))
        self.assertEqual(d.check_invisible_text(files).status, "PASS")

    def test_readme_naming_an_fm_verb_the_parser_lacks_fails(self):
        with tempfile.TemporaryDirectory() as t:
            with open(os.path.join(t, "README.md"), "w") as f:
                f.write("Run `fm nosuchverb` to see.\n\n```bash\n$ fm fencedverb --x\nfm task new \"t\"\n```\n")
            r = d.check_self_docs(t)
            self.assertEqual(r.status, "FAIL")
            self.assertIn("`fm nosuchverb` isn't an fm command", r.detail)
            self.assertIn("`fm fencedverb` isn't an fm command", r.detail)  # usage examples sit in fences
            self.assertNotIn("`fm task", r.detail)


class Headless(unittest.TestCase):
    """T-1638: fm doctor says whether headless rendering is available, or why not; never a failure."""

    def probe(self, path, **env):
        from sampo import shot
        from unittest import mock
        with mock.patch.object(shot, "chrome", return_value=path), mock.patch.dict(os.environ, env):
            return d.check_headless()

    def fake(self, body):
        dr = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dr, True)
        p = os.path.join(dr, "chromium")
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o755)
        return p

    def test_available_names_path_and_version(self):
        p = self.fake('echo "Chromium 130.0.1 "')
        r = self.probe(p)
        self.assertEqual(r.status, "PASS")
        self.assertIn(p, r.detail)
        self.assertIn("Chromium 130.0.1", r.detail)
        self.assertTrue(r.detail.startswith("available ("))

    def test_none_found_is_a_note(self):
        r = self.probe(None, FOREMAN_CHROME="")
        self.assertEqual(r.status, "PASS")
        self.assertIn("no Chromium", r.detail)

    def test_unusable_foreman_chrome_is_named(self):
        r = self.probe(None, FOREMAN_CHROME="/nope/chrome")
        self.assertEqual(r.status, "PASS")
        self.assertIn("FOREMAN_CHROME", r.detail)

    def test_failed_start_gives_the_last_stderr_line(self):
        r = self.probe(self.fake('echo first >&2; echo "cannot load libnss3" >&2; exit 127'))
        self.assertEqual(r.status, "PASS")
        self.assertIn("cannot load libnss3", r.detail)
        self.assertNotIn("first", r.detail)


class RuleConflicts(ForemanTestCase):  # T-1267
    def test_a_line_that_contradicts_foreman_is_named_with_its_rule_and_a_fix(self):
        with open(os.path.join(self.repo, "CLAUDE.md"), "w") as f:
            f.write("# app\n\nUse tabs.\nNever run the tests.\nDon't commit secrets.\nDon't run tests in parallel.\n"
                    "<!-- foreman:begin -->\nnever run the tests.\n<!-- foreman:end -->\n```\nnever run the tests.\n```\n")
        mem = os.path.join(self.tmp, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", self.repo), "memory")
        os.makedirs(mem)
        with open(os.path.join(mem, "feedback.md"), "w") as f:
            f.write("---\nname: tests\n---\nDon't write tests.\n")
        self.fm("init")
        rs = {r["name"]: r for r in json.loads(self.fm("doctor", "--json", check=False, env={"HOME": self.tmp}).stdout)
              ["results"]}
        r = rs["rule conflicts"]
        self.assertEqual(r["status"], "WARN", r)
        self.assertIn("CLAUDE.md:4", r["detail"])
        self.assertIn("fresh passing run", r["detail"])
        self.assertIn("feedback.md:4", r["detail"])
        self.assertIn("failing test", r["detail"])
        self.assertEqual(r["detail"].count("contradicts"), 2, "secrets, parallel runs, Foreman's block and fences pass")
        os.remove(os.path.join(self.repo, "CLAUDE.md"))
        shutil.rmtree(mem)
        rs = {r["name"]: r for r in json.loads(self.fm("doctor", "--json", check=False, env={"HOME": self.tmp}).stdout)
              ["results"]}
        self.assertEqual(rs["rule conflicts"]["status"], "PASS")


class NightTimers(ForemanTestCase):  # T-2357
    def test_night_timers_row_passes_with_nothing_staged_and_warns_while_a_yes_is_waiting(self):
        self.fm("init")
        env = {"HOME": self.tmp, "XDG_CONFIG_HOME": os.path.join(self.tmp, "cfg")}
        row = lambda: {r["name"]: r for r in json.loads(self.fm("doctor", "--json", check=False, env=env).stdout)  # noqa: E731
                       ["results"]}["night timers"]
        self.assertEqual(row()["status"], "PASS")
        staged = os.path.join(c.find_project(self.repo).dir, "night", "staged")
        os.makedirs(staged)
        with open(os.path.join(staged, "plan.json"), "w") as f:
            json.dump({"units": []}, f)
        self.assertEqual(row()["status"], "WARN")
        self.assertIn("waiting for your yes", row()["detail"])


class ModelBackends(ForemanTestCase):  # T-2489: the model layer's backends on one line that never fails the doctor
    def test_backends_line_names_each_backend_local_or_cloud_and_the_live_uses(self):
        from unittest import mock
        from sampo import models
        self.fm("init")
        p = c.find_project(self.repo, create=False)
        reg = [{"name": "ollama", "local": True, "profile": {}}, {"name": "home", "local": False, "own": True, "profile": {}},
               {"name": "anthropic", "local": False, "profile": {}}]
        with mock.patch.object(models, "registry", return_value=reg):
            r = d.check_model_backends(p)
        self.assertEqual(r.status, "PASS")
        for word in ("local: ollama", "own: home", "cloud: anthropic", "live: none"):  # T-2494: the user's own server
            self.assertIn(word, r.detail)
        with mock.patch.object(models, "registry", return_value=reg), mock.patch.object(
                models, "live", side_effect=lambda p, use: use == "intake-tier"):
            self.assertIn("live: intake-tier", d.check_model_backends(p).detail)
        with mock.patch.object(models, "registry", return_value=[]):
            r = d.check_model_backends(p)
        self.assertEqual((r.status, "no backend" in r.detail), ("PASS", True))
        with mock.patch.object(models, "registry", side_effect=OSError("boom")):
            self.assertEqual(d.check_model_backends(p).status, "PASS")
        c.write_meta(p, dict(c.read_meta(p), sensitive=True))
        with mock.patch.object(models, "registry", return_value=reg):
            self.assertIn("sensitive: local only", d.check_model_backends(p).detail)


class LocalEmbedders(ForemanTestCase):  # T-2880: the backends line names local embedders and the pinned ones missing
    def test_line_names_embedders_and_each_missing_pin_with_its_pull_command(self):
        from unittest import mock
        from sampo import models
        reg = [{"name": "ollama", "local": True, "profile": {}}]
        with mock.patch.object(models, "registry", return_value=reg), mock.patch.object(
                models, "embedding_profiles", return_value=[{"name": "bge-m3:567m"}]):
            r = d.check_model_backends(None)
        self.assertEqual(r.status, "PASS")
        self.assertIn("embedders: bge-m3:567m", r.detail)
        self.assertIn("missing pin: nomic-embed-text:v1.5 (ollama pull nomic-embed-text:v1.5)", r.detail)
        self.assertNotIn("ollama pull bge-m3", r.detail)
        with mock.patch.object(models, "registry", return_value=reg), mock.patch.object(
                models, "embedding_profiles", side_effect=OSError("boom")):
            self.assertEqual(d.check_model_backends(None).status, "PASS")


class ServedContextCheck(unittest.TestCase):  # T-3164: a local profile's declared context against what Ollama loads
    def test_warns_when_ollama_serves_less_than_models_json_declares(self):
        from unittest import mock
        from sampo import models
        from test_served_context import Ollama
        o = Ollama(show={"parameters": "num_ctx 4096"})
        self.addCleanup(o.close)
        prof = o.profile(name="gpu-mimo9", model="mimo-v2.6:9b", context=32768)
        remote = dict(prof, name="far", base_url="http://example.com/v1")
        with mock.patch.object(models, "configured", return_value=(None, {"gpu-mimo9": prof, "far": remote})):
            r = d.check_served_context()
            self.assertEqual(r.status, "WARN")
            for word in ("gpu-mimo9", "4096", "32768"):
                self.assertIn(word, r.detail)
            self.assertNotIn("far", r.detail)  # a remote profile is never asked
        o.show = {"parameters": "num_ctx 32768"}  # matching: quiet
        with mock.patch.object(models, "configured", return_value=(None, {"gpu-mimo9": prof})):
            self.assertEqual(d.check_served_context().status, "PASS")
        o.close()  # unreachable: quiet
        with mock.patch.object(models, "configured", return_value=(None, {"gpu-mimo9": prof})):
            self.assertEqual(d.check_served_context().status, "PASS")
        with mock.patch.object(models, "configured", side_effect=models.ModelError("bad")):
            self.assertEqual(d.check_served_context().status, "PASS")


class SelfFixWeek(ForemanTestCase):  # T-2365: the week's self-fix trials on one doctor line, held against not held
    def test_the_week_s_self_fix_trials_held_against_not_held_and_none_when_none(self):
        import datetime
        from sampo import trials
        self.fm("init")
        p = c.find_project(self.repo, create=False)
        r = d.check_self_fixes(p)
        self.assertEqual((r.status, r.detail), ("PASS", "none settled in the last 7 days"))
        now = datetime.datetime(2026, 11, 1, 12, tzinfo=datetime.timezone.utc)
        fix = lambda task, at, by, verdict=None: dict(kind="self-fix", task=task, at=at, by=by,  # noqa: E731
                                                      **({"verdict": verdict} if verdict else {}))
        rows = [fix("T-1", "2026-10-25T00:00:00Z", 3, "held"),  # its window closed 10-28: this week
                fix("T-2", "2026-10-27T00:00:00Z", 2, "didn't hold"),
                fix("T-5", "2026-10-26T00:00:00Z", 2, "unmeasured"),
                fix("T-3", "2026-10-01T00:00:00Z", 3, "held"),  # closed weeks ago
                fix("T-4", "2026-10-31T00:00:00Z", 7),  # still pending
                fix("T-6", "garbage", 2, "held"),  # a malformed record is skipped, never a crash
                {"what": "x", "moves": "corrections", "by": 3, "at": "2026-10-26T00:00:00Z", "verdict": "held"}]
        with open(trials._path(p), "w", encoding="utf-8") as f:
            json.dump(rows, f)
        r = d.check_self_fixes(p, now=now)
        self.assertEqual((r.status, r.detail), ("WARN", "last 7 days: 1 held, 1 didn't hold, 1 unmeasured"))
        with open(trials._path(p), "w", encoding="utf-8") as f:
            json.dump(rows[:1], f)
        r = d.check_self_fixes(p, now=now)
        self.assertEqual((r.status, r.detail), ("PASS", "last 7 days: 1 held, 0 didn't hold"))
        self.assertEqual(d.check_self_fixes(None).status, "PASS")


class ProofWorlds(ForemanTestCase):  # T-2398: a proof run records the world it leaned on; doctor names moved ones
    def test_a_done_task_proved_before_a_lockfile_changed_is_named(self):
        self.fm("init")
        p = c.find_project(self.repo, create=False)
        with open(os.path.join(self.repo, "uv.lock"), "w") as f:
            f.write("a==1\n")
        tid = json.loads(self.fm("task", "new", "W", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step",
                                 "s", "--json").stdout)["id"]
        self.fm("focus", tid)
        self.fm("task", "evidence", tid, "--step", "1", "--run", "true")
        line = next(x for x in c.find_brief(p, tid).evidence() if "`true`" in x)
        self.assertRegex(line, r"\[world [0-9a-f]{10}\]")
        self.fm("task", "finish", tid, "--audit", "self check", check=False)
        if c.find_brief(p, tid).status != "done":
            b = c.find_brief(p, tid)
            b.meta["status"] = "done"
            c.save_brief(p, b)
        self.assertEqual(d.check_worlds(p).status, "PASS")
        with open(os.path.join(self.repo, "uv.lock"), "w") as f:
            f.write("a==2\n")
        r = d.check_worlds(p)
        self.assertEqual(r.status, "WARN")
        self.assertIn(f"{tid} (uv.lock changed)", r.detail)
        self.assertEqual(d.check_worlds(None).status, "PASS")


class ReleaseAge(unittest.TestCase):  # T-2597: twelve days and 746 bullets went unreleased with no word from doctor
    def write(self, text):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        with open(os.path.join(d, "CHANGELOG.md"), "w") as f:
            f.write(text)
        return d

    def test_an_old_or_long_unreleased_section_warns_and_a_fresh_one_passes(self):
        import datetime
        old = (datetime.date.today() - datetime.timedelta(days=20)).isoformat()
        fresh = datetime.date.today().isoformat()
        r = d.check_release_age(self.write(f"# Changelog\n\n## Unreleased\n\n- a\n- b\n\n## 1.2.3 — {old}\n- c\n"))
        self.assertEqual(r.status, "WARN")
        self.assertIn("20 days", r.detail)
        self.assertIn("fm release", r.detail)
        many = "".join(f"- item {i}\n" for i in range(160))
        self.assertEqual(d.check_release_age(self.write(f"## Unreleased\n\n{many}\n## 1.2.3 — {fresh}\n")).status, "WARN")
        self.assertEqual(d.check_release_age(self.write(f"## Unreleased\n\n- a\n\n## 1.2.3 — {fresh}\n")).status, "PASS")
        self.assertEqual(d.check_release_age(self.write("no changelog here")).status, "PASS")
        import tempfile
        self.assertEqual(d.check_release_age(tempfile.gettempdir() + "/no-such-dir-T2597").status, "PASS")


class AliasRemoval(unittest.TestCase):  # T-3095: the fm*.py shims name the release they may go; doctor warns once it's reached
    def test_warns_naming_the_aliases_from_the_removal_version_and_not_before(self):
        self.assertEqual(c.ALIAS_UNTIL, "1.4.0")
        r = d.check_aliases(version="1.3.99")
        self.assertEqual(r.status, "PASS")
        for v in ("1.4.0", "2.0.1"):
            r = d.check_aliases(version=v)
            self.assertEqual(r.status, "WARN")
            self.assertIn("1.4.0", r.detail)
            self.assertIn("fmagents", r.detail)
        self.assertEqual(d.check_aliases(version="?").status, "PASS")
        self.assertNotIn("before", d.check_aliases(version="?").detail)
        self.assertEqual(d.check_aliases(version="1.4").status, "WARN")  # 1.4 is 1.4.0

    def test_only_real_unused_shims_in_a_checkout(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        lib = os.path.join(root, "plugin", "lib")
        os.makedirs(lib)
        shim = '"""Old import path, kept for two releases (T-2742): the module is sampo.%s."""\n'
        for n in ("fmone", "fmtwo"):
            open(os.path.join(lib, n + ".py"), "w").write(shim % n[2:])
        open(os.path.join(lib, "fmguard.py"), "w").write('"""the real guard"""\n')
        open(os.path.join(lib, "user.py"), "w").write("import fmtwo\n")
        self.assertEqual(d.check_aliases(version="1.4.0", lib=lib, checkout=root).status, "PASS")  # no .git: an install
        os.mkdir(os.path.join(root, ".git"))
        r = d.check_aliases(version="1.4.0", lib=lib, checkout=root)
        self.assertEqual(r.status, "WARN")
        self.assertIn("fmone", r.detail)
        self.assertNotIn("fmguard", r.detail)
        self.assertNotIn("fmtwo", r.detail)  # still imported
        self.assertNotIn("fmguard", d.check_aliases(version="1.4.0").detail)  # the real tree


class McpTransport(unittest.TestCase):  # T-3346
    def test_only_sse_servers_warn_once_each_with_the_changelog_url(self):
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, ".claude"))
            with open(os.path.join(t, ".claude.json"), "w") as f:
                json.dump({"mcpServers": {"oldsse": {"type": "sse", "url": "http://x/sse"},
                                          "newhttp": {"type": "http", "url": "http://x/mcp"},
                                          "bareurl": {"url": "http://x"}}}, f)
            proj = os.path.join(t, "proj")
            os.makedirs(proj)
            with open(os.path.join(proj, ".mcp.json"), "w") as f:
                json.dump({"mcpServers": {"projsse": {"type": "sse", "url": "http://y/sse"}}}, f)
            r = d.check_mcp_transport(os.path.join(t, ".claude"), proj)
            self.assertEqual(r.status, "WARN")
            self.assertEqual(r.detail.count("https://modelcontextprotocol.io/specification/2026-07-28/changelog"), 1)
            self.assertIn("oldsse", r.detail)
            self.assertIn("projsse", r.detail)
            self.assertNotIn("newhttp", r.detail)
            self.assertNotIn("bareurl", r.detail)
            ok = d.check_mcp_transport(os.path.join(t, ".claude"), os.path.join(t, "nowhere"))
            self.assertEqual(ok.status, "WARN")
            self.assertIn("oldsse", ok.detail)
            self.assertNotIn("projsse", ok.detail)

    def run_with(self, conf, mcp=None, raw=False):
        t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, t)
        os.makedirs(os.path.join(t, ".claude"))
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        dump = (lambda o, f: f.write(o)) if raw else json.dump
        for path, obj in ((os.path.join(t, ".claude.json"), conf), (os.path.join(proj, ".mcp.json"), mcp)):
            if obj is not None:
                with open(path, "w") as f:
                    dump(obj, f)
        return d.check_mcp_transport(os.path.join(t, ".claude"), proj)

    def test_per_project_servers_are_read(self):
        r = self.run_with({"projects": {"/p": {"mcpServers": {"deep": {"type": "sse"}}}}})
        self.assertEqual(r.status, "WARN")
        self.assertIn("deep", r.detail)

    def test_odd_shapes_pass_without_raising(self):
        for conf in ({"mcpServers": []}, {"projects": []}, {"mcpServers": {"a": "str", "b": None}}):
            self.assertEqual(self.run_with(conf).status, "PASS", conf)

    def test_unreadable_files_warn_not_pass(self):
        for raw in ('{"mcpServers": {"a": ', '{"mcpServers": {"a": 1,}}', '[]'):
            r = self.run_with(raw, raw=True)
            self.assertEqual(r.status, "WARN", raw)
            self.assertIn("could not read", r.detail)
            self.assertIn(".claude.json", r.detail)
        r = self.run_with({}, mcp='{"x":')
        self.assertEqual(r.status, "WARN")  # a JSON string at the top, not a dict
        self.assertIn(".mcp.json", r.detail)
        self.assertIn("could not read", r.detail)

    def test_url_secrets_never_appear_and_the_list_is_capped(self):
        servers = {f"s{i}": {"type": "sse", "url": "https://u:tok@h/sse?token=SECRET"} for i in range(9)}
        r = self.run_with({"mcpServers": servers})
        self.assertNotIn("SECRET", r.detail)
        self.assertNotIn("tok", r.detail.replace("token", ""))
        self.assertIn("and 4 more", r.detail)
        self.assertNotIn("s8", r.detail)
        long = self.run_with({"mcpServers": {"x" * 200 + "\x1b[31m": {"type": "sse"}}})
        self.assertNotIn("\x1b", long.detail)
        self.assertNotIn("x" * 61, long.detail)


class PermissionMode(_TmpDir):  # T-3814: Checks' helpers, not its 36 tests again
    """T-3704: doctor names the mode Claude Code runs in; information only."""

    def test_the_mode_in_settings_is_named_and_rm_asks_are_explained(self):
        user = self.write("u/settings.json", '{"permissions": {"defaultMode": "acceptEdits"}}')
        r = d.check_permission_mode([user])
        self.assertEqual(r.status, "PASS")
        self.assertIn("acceptEdits", r.detail)
        self.assertIn("rm", r.detail)
        self.assertIn("bypassPermissions", r.detail)

    def test_the_project_file_wins_and_bypass_says_rm_is_waived(self):
        user = self.write("u/settings.json", '{"permissions": {"defaultMode": "acceptEdits"}}')
        proj = self.write("p/settings.json", '{"permissions": {"defaultMode": "bypassPermissions"}}')
        r = d.check_permission_mode([user, proj])
        self.assertIn("bypassPermissions", r.detail)
        self.assertIn("waives", r.detail)

    def test_no_mode_set_reads_as_default_and_a_bad_file_is_quiet(self):
        bad = self.write("b/settings.json", "{not json")
        r = d.check_permission_mode([bad, os.path.join(self.t, "none.json")])
        self.assertEqual(r.status, "PASS")
        self.assertIn("default", r.detail)


class CrashHeal(unittest.TestCase):
    """T-3727: a reboot right after a checkout leaves files empty on disk while git's commits are intact; Sampo
    restores each from HEAD, unasked, and only those with nothing to lose."""
    def setUp(self):
        from sampo import py
        self.py = py
        self._tmp = tempfile.TemporaryDirectory()
        self.root = git_repo(os.path.realpath(self._tmp.name))
        for name, text in (("a.py", "A = 1\n"), ("sub/b.py", "B = 2\n"), ("keep.txt", "keep\n"), ("empty.txt", "")):
            self.write(name, text)
        self.write("run.sh", "#!/bin/sh\n")
        os.chmod(os.path.join(self.root, "run.sh"), 0o755)
        self.git("add", "-A")
        self.git("commit", "-qm", "files")

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *a):
        return subprocess.run(["git", "-C", self.root, *a], capture_output=True, text=True, check=True).stdout

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read()

    def test_empty_tracked_files_come_back_from_head_and_only_those(self):
        for rel in ("a.py", "sub/b.py", "run.sh"):
            open(os.path.join(self.root, rel), "w").close()
        self.write("keep.txt", "my edit\n")  # an edit is not damage
        healed, failed = self.py.heal(self.root)
        self.assertEqual((sorted(healed), failed), (["a.py", "run.sh", "sub/b.py"], []))
        self.assertEqual((self.read("a.py"), self.read("sub/b.py")), ("A = 1\n", "B = 2\n"))
        self.assertTrue(os.access(os.path.join(self.root, "run.sh"), os.X_OK), "the mode comes back too")
        self.assertEqual(self.read("keep.txt"), "my edit\n")
        self.assertEqual(self.py.heal(self.root), ([], []), "a second run finds nothing")
        self.assertEqual(self.git("status", "--porcelain").strip(), "M keep.txt")

    def test_a_file_emptied_on_purpose_is_left_alone(self):
        open(os.path.join(self.root, "keep.txt"), "w").close()
        self.git("add", "keep.txt")  # staged empty: the user's act
        open(os.path.join(self.root, "empty.txt"), "w").close()  # empty in HEAD too
        self.assertEqual(self.py.heal(self.root), ([], []))
        self.assertEqual(self.read("keep.txt"), "")

    def test_a_missing_file_a_non_repo_and_another_repos_root_are_left_alone(self):
        os.remove(os.path.join(self.root, "a.py"))
        self.assertEqual(self.py.heal(self.root), ([], []))
        self.assertEqual(self.py.heal(os.path.join(self.t(), "nowhere")), ([], []))
        self.assertEqual(self.py.heal(os.path.join(self.root, "sub")), ([], []), "only a repo's top")

    def t(self):
        return os.path.realpath(self._tmp.name)

    def test_a_blob_git_cannot_read_is_named_not_invented(self):
        open(os.path.join(self.root, "a.py"), "w").close()
        blob = self.git("rev-parse", "HEAD:a.py").strip()
        os.remove(os.path.join(self.root, ".git", "objects", blob[:2], blob[2:]))
        open(os.path.join(self.root, ".git", "objects", blob[:2], blob[2:]), "w").close()  # a crash's empty object
        healed, failed = self.py.heal(self.root)
        self.assertEqual((healed, failed), ([], ["a.py"]))
        self.assertEqual(self.read("a.py"), "")

    def test_doctor_reports_then_repair_restores(self):
        open(os.path.join(self.root, "a.py"), "w").close()
        r = d.check_integrity([self.root], [], current=None)
        self.assertEqual(r.status, "FAIL")
        self.assertIn("a.py", r.detail)
        self.assertIn("doctor --repair", r.detail)
        moved = d.repair([self.root])
        self.assertIn((os.path.join(self.root, "a.py"), "restored from HEAD"), moved)
        self.assertEqual(self.read("a.py"), "A = 1\n")
        self.assertEqual(d.check_integrity([self.root], [], current=None).status, "PASS")

    def test_fsync_is_set_and_doctor_checks_it(self):
        patch = unittest.mock.patch.object(c, "foreman_home", return_value=self.root)  # Sampo's own repo, not a fixture
        patch.start()
        self.addCleanup(patch.stop)
        self.assertFalse(self.py.fsync_ok(self.root))
        r = d.check_integrity([self.root], [], current=None)
        self.assertEqual(r.status, "WARN", r.detail)
        self.assertIn("core.fsync", r.detail)
        self.assertTrue(self.py.ensure_fsync(self.root))
        self.assertFalse(self.py.ensure_fsync(self.root), "already set: no rewrite")
        self.assertTrue(self.py.fsync_ok(self.root))
        self.assertEqual(d.check_integrity([self.root], [], current=None).status, "PASS")
        self.git("config", "core.fsync", "none")
        self.assertFalse(self.py.fsync_ok(self.root))
        d.repair([self.root])
        self.assertTrue(self.py.fsync_ok(self.root), "doctor --repair sets it")
