"""T-2742 (SAMPO.md): the rename's automatic check. Counts the lines on user-visible surfaces that still say Foreman,
leaving out history and the names kept as aliases. A ratchet: the count may only go down, and the rename is done at 0.
Lower BASELINE in the same commit that removes mentions; never raise it."""
import os
import re
import subprocess
import unittest

import helpers  # noqa: F401  (plugin/lib on sys.path, for ReadersTakeBoth)

# map-links: plugin/lib/sampo/cost.py plugin/lib/sampo/repeats.py plugin/lib/sampo/friction.py plugin/lib/sampo/bench.py plugin/lib/sampo/long.py plugin/lib/sampo/smithtui.py plugin/lib/sampo/mandate.py plugin/lib/sampo/rune.py plugin/lib/sampo/fixes.py plugin/lib/sampo/cli.py plugin/lib/sampo/louhi.py plugin/lib/fmguard.py

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASELINE = 16  # 2026-11-04 (T-2946: surfaces widened to looks.html, hooks.json, READMEs, fmguard, hook, bin/fm; 74 found, the guard now says Ward:/Sampo:, 16 left are readers of the old text); 2026-11-03 (T-2818: widened to install.sh, workflows, templates, site/*.py and site/genesis; their prose renamed, Foreman Desktop and the release-subject grep kept, count unchanged at 19):876 before the rename, 720 after the site and README (T-2753, T-2760), 475 after the skills, commands and output style (T-2768), 402 after the library strings and the mod (T-2779; the rest are protocol: the release subject, fmguard's refusal and posture text, which older transcripts and the guard still carry), 61 after MASTER.md (T-2810: MASTER's lines, and the lowercase-id forms KEPT now keeps on every surface), 42 after the mod (T-2807: the code-matched forms it keeps are listed in KEPT), 23 after the agents, listing, output style and install.sh (T-2808), 19 after the rules (T-2809); the goal is 0
SURFACES = ("README.md", "MASTER.md", "INSTALL.md", "site/*.html", "site/*.js", "plugin/skills/**/*.md",
            "plugin/commands/*.md", "plugin/agents/*.md", ".claude-plugin/marketplace.json",
            "plugin/.claude-plugin/plugin.json", "plugin/output-styles/*", "mods/foreman-ui/**", "plugin/rules/*.md",
            "install.sh", ".github/workflows/*.yml", "plugin/templates/**", "site/*.py",
            "site/genesis/*.html", "site/genesis/*.js", "site/genesis/*.css",
            # T-2946: the cold review found the guard, the looks page, the hooks and the READMEs outside the scan
            # (fmguard.py, hook and bin/fm are code: library_strings counts their string literals, not their comments)
            "plugin/lib/looks.html", "plugin/hooks/hooks.json", "plugin/README.md", "plugin/evals/README.md", "plugin/observability/README.md", "bench/*/README.md")
# names kept on purpose: old plugin ids, commands, agents, env, trailer and path, each an alias of its Sampo name, and
# the one line that says what Sampo was called. T-2807: also the strings code matches (the mod's pane id 'foreman', the
# skill namespace foreman:next, the install folder /foreman/, the resume prompt the plugin and the mod both read, the
# guard's refusal and ask prefixes); each moves in the task that changes its reader (T-2809 for the guard and prompt)
# T-2879: sampo@foreman is the id the renames map gives an install whose marketplace was added as foreman; T-3707 made
# the marketplace sampo (sampo@sampo), and sampo migrate plugins moves an install that still has an old id
KEPT = re.compile(r"(?:sampo|foreman)?(?:-ui|-design)?@foreman\b|foreman-ui|/foreman:|(?-i:foreman:(?=[a-z]))|foreman:fm-|FOREMAN_|Foreman-Task|"
                  r"\.claude/foreman|(?-i:/foreman/)|'foreman'|(?-i:Continue the Foreman drive)|"
                  r"(?-i:Foreman guard:|Foreman asks |Foreman\(\?: guard\)\?:)|"  # the last: register.tsx's guard regex source
                  r"formerly Foreman|was called Foreman|renamed from Foreman|"
                  # T-2810: lowercase ids a page names as code (.foreman/, refs/foreman/, foreman-ui, foreman.md, a `foreman` arm,
                  # `claude mcp add foreman`): a folder, ref, file, plugin or arm name, kept like the plugin id; a capital F is prose
                  r"(?-i:[`|/.$~-]foreman|foreman[`|/.:=-]|mcp add foreman)|"
                  # strings the code still prints or matches, which a page quotes until the code is renamed (eco.py, bench.py,
                  # release.py, cost.py, the mod's drive prompt): the quote must keep matching what runs
                  # T-2818: the desktop app and its .desktop entry keep the name until its outward rename
                  r"Foreman Desktop|Name=Foreman|Foreman\.app|\^Foreman \[0-9\]|"
                  r"\"Foreman\"|\(Foreman ID\)|false done: Foreman|Foreman X\.Y\.Z|Continue the Foreman drive", re.I)


LIB_KEPT = re.compile(r"Foreman(?=\)-Task|-Task|\(\?)|user\.name=Foreman|Foreman@|;Foreman-|foreman:Foreman|\(\?:Foreman\||Foreman guard\)|Foreman \{v\}|\(\?:Sampo\|Foreman\)|\(Sampo\|Foreman\)|Foreman\) snapshot")
# kept: trailer, git identity, the output style id, and the old name as a regex alternative (older transcripts say it)


def library_strings():
    """T-2779: the (path, text) pairs for string literals in the sampo library and the compact hook that say Foreman
    outside the kept forms (comments are not user-visible; docstrings are counted)."""
    import glob
    import io
    import tokenize
    mid = getattr(tokenize, "FSTRING_MIDDLE", -1)
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "plugin/lib/sampo/*.py"))) + [os.path.join(ROOT, p) for p in (
            "plugin/hooks/compact", "plugin/lib/fmguard.py", "plugin/hooks/hook", "plugin/bin/fm")]:
        with open(f, encoding="utf-8") as fh:
            src = fh.read()
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            if t.type in (tokenize.STRING, mid) and re.search(r"(?<![\w/.@-])Foreman\b", LIB_KEPT.sub("", t.string)):
                rows.append((os.path.relpath(f, ROOT), t.string.strip()[:100]))
    return rows


# T-2811: the repo is ChaseSunstrom/sampo now; its old address (and the Pages one, which 404s) counts wherever it appears.
# KEPT can't hide it: its lowercase-id forms keep any /foreman. (foreman-desktop is a separate repo, so `-` is not a match)
OLD_ADDR = re.compile(r"sunstrom(?:\.github\.io)?/foreman(?!-)", re.I)


FM_ONLY_FILES = ("plugin/lib/sampo/hooks.py", "plugin/lib/sampo/core.py", "plugin/lib/fmguard.py")
_BARE_FM = re.compile(r"(?:(?<![\w./-])|(?<=\\b))fm(?=\\s|\\b| |\))")  # fm as a word (fm-reviewer, fm_x, plugin/bin/fm are not)
_REGEXY = re.compile(r"\\[sbdwS]|\(\?|\[\^|\\\.|\|")


# T-2845: fm on purpose, not a typed command: the MCP server's key in a runner's config, the program name the alias line
# tests, a literal cli builds and compares in one function, a module name (fmcli) and the guard's own normalized name
FM_ONLY_OK = (("agents.py", "servers.get('fm')"), ("cli.py", "_prog() != 'fm'"), ("cli.py", "cmd == 'fm prove --infra'"),
              ("recall.py", "w.startswith('fm')"), ("fmguard.py", "fm.startswith('fm')"), ("fmguard.py", "name == 'fm'"))


def fm_only_regexes():
    """T-2778: the (path, line, text) string literals in the hooks that read as a regex naming the command fm and never
    sampo. A doc may only teach sampo spellings once every pattern that matches an fm command matches sampo too."""
    import ast
    import glob
    rows = []
    # T-2845: every library module, not three; a pattern is judged by what it matches, not by the word sampo in it
    files = sorted(os.path.relpath(f, ROOT) for f in glob.glob(os.path.join(ROOT, "plugin/lib/sampo/*.py")))
    for rel in dict.fromkeys(files + list(FM_ONLY_FILES)):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
        up = {id(k): p for p in ast.walk(tree) for k in ast.iter_child_nodes(p)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                s = n.value
                if _BARE_FM.search(s) and _REGEXY.search(s) and fm_only_pattern(s):
                    rows.append((rel, n.lineno, s[:80]))
            elif isinstance(n, (ast.Compare, ast.Call)) and _fm_only_compare(n, up):
                text = ast.unparse(n)
                if not any(rel.endswith(f) and text.startswith(t) for f, t in FM_ONLY_OK):
                    rows.append((rel, n.lineno, text[:80]))
    return rows


def _command_samples(words):
    """The fm spellings a pattern might be written for: fm alone, then fm with each word the pattern itself names, bare
    and after a shell prefix (the caller swaps the command word for sampo)."""
    tails = ["", " x"] + [f" {w} x" for w in words] + [f" {w}" for w in words]
    heads = ["", "cd a && ", "a; ", "FOO=1 ", "/usr/bin/", "sudo "]
    return [h + "fm" + t for h in heads for t in tails]


def fm_only_pattern(src):
    """True when the regex `src` matches some typed fm command but not the same command spelled sampo."""
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # a string that only looks like a regex may warn on set syntax
            rx = re.compile(src)
    except re.error:
        return False
    words = set(re.findall(r"-{0,2}[a-z][a-z-]*", re.sub(r"\\[a-zA-Z]", " ", src))) - {"fm", "sampo"}
    for s in _command_samples(sorted(words)):
        if rx.search(s) and not rx.search(s.replace("fm", "sampo", 1)):  # the heads hold no fm: the first is the command
            return True
    return False


def _fm_only_compare(n, up):
    """A comparison or startswith that names the command fm ("fm", "fm ", ["fm"]) with no sampo beside it (or in the
    `a or b` it is one side of)."""
    import ast
    if isinstance(n, ast.Call) and not (isinstance(n.func, ast.Attribute) and n.func.attr in ("startswith", "endswith")):
        return False
    strs = lambda x: [c.value for c in ast.walk(x) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    own, around = strs(n), n
    while isinstance(up.get(id(around)), ast.BoolOp):
        around = up[id(around)]
    return any(s == "fm" or s.startswith("fm ") for s in own) and not any("sampo" in s for s in strs(around))


# T-2778: the surfaces that teach commands, and the command spelling they stopped teaching (`fm task new`, `fm check`,
# `fm -p DIR`, `fm <command>`; not fm-reviewer, plugin/bin/fm, `fm` as the alias's name or the fm* module names)
TEACH_SURFACES = ("README.md", "MASTER.md", "INSTALL.md", "plugin/skills/**/*.md", "plugin/commands/*.md",
                  "plugin/agents/*.md", "plugin/output-styles/*", "plugin/rules/*.md")
FM_SPELLING = r"(?<![\w./:@-])fm(?= +(?:[a-z][a-z-]*|<|-p\b))"
FM_SPELLING_BASELINE = 0  # 2026-11-03 (T-2778): 2254 before the rewrite; lower it when a line goes, never raise it


def fm_spellings():
    out = subprocess.run(["git", "-C", ROOT, "grep", "-nP", FM_SPELLING, "--", *TEACH_SURFACES], capture_output=True,
                         text=True).stdout
    return [ln[:100] for ln in out.splitlines()]


def leftover():
    """The (path, line) pairs on a surface that name Foreman outside the kept forms."""
    out = subprocess.run(["git", "-C", ROOT, "grep", "-niw", "foreman", "--", *SURFACES], capture_output=True,
                         text=True).stdout
    rows = []
    for ln in out.splitlines():
        path, _, rest = ln.partition(":")
        text = rest.partition(":")[2]
        if OLD_ADDR.search(text) or re.search(r"\bforeman\b", KEPT.sub("", text), re.I):
            rows.append((path, text.strip()[:100]))
    return rows


class RenameScan(unittest.TestCase):
    def test_user_visible_foreman_only_goes_down(self):
        rows = leftover() + library_strings()
        self.assertLessEqual(len(rows), BASELINE, f"{len(rows)} lines still say Foreman (baseline {BASELINE}); "
                                                  f"first: {rows[:3]}")

    def test_no_fm_only_command_regex(self):  # T-2778
        self.assertEqual(fm_only_regexes(), [])

    def test_docs_teach_sampo_spellings_only_goes_down(self):  # T-2778
        rows = fm_spellings()
        self.assertLessEqual(len(rows), FM_SPELLING_BASELINE, f"{len(rows)} lines teach an fm command (baseline "
                                                              f"{FM_SPELLING_BASELINE}); first: {rows[:3]}")

    def test_fm_spelling_pattern(self):
        for taught in ("run `fm task new`", "fm check", "(fm -p DIR capture)", "`fm <command> -h`"):
            self.assertTrue(re.search(FM_SPELLING, taught), taught)
        for fine in ("foreman:fm-reviewer", "plugin/bin/fm check", "the `fm` alias", "fmguard", "~/.local/bin/fm"):
            self.assertFalse(re.search(FM_SPELLING, fine), fine)

    def test_fm_only_pattern_judges_by_matching(self):  # T-2845
        for fm_only in (r"\bfm\s+ask\b", r"^\s*fm edit\b", r"\s*(?:\S*/)?fm\s", r"fm (?:task|focus)\b",
                        r"(?:^|[;&|(]\s*|\s)fm\s+([a-z][a-z-]*)", r"^(?:fm quiet -- )?(?:fm|sampo)x? check|\bfm [a-z-]+"):
            self.assertTrue(fm_only_pattern(fm_only), fm_only)
        for both in (r"\b(?:fm|sampo)\s+ask\b", r"^\s*(?:fm|sampo) edit\b", r"(?:fm|sampo)\s+(?:quiet\s+)?([\w-]+)",
                     r"(?:sampo|fm) task", r"\bsed\s+-i|\b(?:fm|sampo)\s+(?:edit|sub)\b", r"plugin/bin/fm\b", r"fm-reviewer"):
            self.assertFalse(fm_only_pattern(both), both)

    def test_fm_only_compare_sees_the_forms(self):  # T-2845
        import ast
        def flagged(src):
            tree = ast.parse(src)
            up = {id(k): p for p in ast.walk(tree) for k in ast.iter_child_nodes(p)}
            return any(_fm_only_compare(n, up) for n in ast.walk(tree) if isinstance(n, (ast.Compare, ast.Call)))
        for bad in ("w == 'fm'", "t.startswith('fm ')", "x in ([], ['fm'])", "'fm ask' in out"):
            self.assertTrue(flagged(bad), bad)
        for good in ("w in ('fm', 'sampo')", "t.startswith(('fm ', 'sampo '))", "'fm ask' in out or 'sampo ask' in out",
                     "w == 'git'"):
            self.assertFalse(flagged(good), good)

    def test_fm_only_scan_sees_the_forms(self):
        for fm_only in (r"^fm quiet (?:-- )?", r"(?:\S*/)?fm\s+", r"\bfm code\b", r"(?<!fm )ui|ux"):
            self.assertTrue(_BARE_FM.search(fm_only) and _REGEXY.search(fm_only), fm_only)
        for fine in (r"(?:fm|sampo)\s+check", r"(?:^|:)fm-reviewer$", r"plugin/bin/fm\b", r"\bfm_x\b"):
            self.assertFalse(_BARE_FM.search(fine) and "sampo" not in fine, fine)

    def test_kept_forms_are_not_counted(self):
        self.assertFalse(re.search(r"\bforeman\b", KEPT.sub("", "enable foreman@foreman or run /foreman:next"), re.I))
        self.assertTrue(re.search(r"\bforeman\b", KEPT.sub("", "Foreman plans before it edits"), re.I))
        self.assertFalse(re.search(r"\bforeman\b", KEPT.sub("", "pane 'foreman', skill foreman:next, /h/foreman/state"), re.I))
        self.assertFalse(re.search(r"\bforeman\b", KEPT.sub("", "Continue the Foreman drive: x"), re.I))
        self.assertFalse(re.search(r"\bforeman\b", KEPT.sub("", "Foreman guard: blocked core"), re.I))
        self.assertTrue(re.search(r"\bforeman\b", KEPT.sub("", "Foreman: open the pane"), re.I))  # display, not a kept form

    def test_old_repo_address_counts(self):  # T-2811
        for old in ("https://github.com/ChaseSunstrom/foreman/blob/main/INSTALL.md", "https://chasesunstrom.github.io/foreman/",
                    "raw.githubusercontent.com/ChaseSunstrom/foreman/main/install.sh", "gh repo clone ChaseSunstrom/foreman"):
            self.assertTrue(OLD_ADDR.search(old), old)
        self.assertFalse(OLD_ADDR.search("github.com/ChaseSunstrom/foreman-desktop"))
        self.assertFalse(OLD_ADDR.search("https://chasesunstrom.github.io/sampo/"))

    def test_lowercase_ids_are_kept_but_prose_is_not(self):  # T-2810
        for kept in ("the `foreman` arm", "writes .foreman/ in the repo", "refs/foreman/shipped", "foreman-ui mod", "rules/foreman.md",
                     "foreman.shiptest", "claude mcp add foreman -- fm mcp", "oracle|empty|foreman|plain"):
            self.assertFalse(re.search(r"\bforeman\b", KEPT.sub("", kept), re.I), kept)
        for prose in ("is foreman ok?", "Foreman.", "ask Foreman-owned work", "the foreman arm", "Foreman's ui"):
            self.assertTrue(re.search(r"\bforeman\b", KEPT.sub("", prose), re.I), prose)

    def test_no_library_code_hardcodes_the_old_mirror_name(self):  # T-3342: one constant (core.MIRROR/MIRRORS) names the mirror
        import ast
        lib = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
        allowed = (("core.py", 'MIRRORS = (MIRROR, ".foreman")'),  # the constant
                   ("fmguard.py", '(".foreman", ".sampo")'),  # the stdlib-only guard can't import it
                   ("cli.py", r"\.sampo|\.foreman|\.claude"))  # the cache-path regex, both names
        hits = []
        for root, _, names in os.walk(lib):
            for n in names:
                if not n.endswith(".py"):
                    continue
                path = os.path.join(root, n)
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                lines = src.splitlines()
                tree = ast.parse(src)
                docs = {id(b[0].value) for node in ast.walk(tree) if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                        ast.AsyncFunctionDef)) for b in [node.body] if b and isinstance(b[0], ast.Expr)}
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs \
                            and re.search(r"\.foreman(?![\w-])", node.value) \
                            and not any(n == f and frag in " ".join(lines[node.lineno - 1:node.end_lineno]) for f, frag in allowed):
                        hits.append(f"{n}:{node.lineno}")
        self.assertEqual(hits, [])


# T-2879/T-3707: the plugin ids moved to Sampo: sampo@sampo (sampo-ui, sampo-design), /sampo:next, sampo:fm-builder. OLD_NS
# is every old spelling of the namespace or an id (the CLAUDE.md block's <!-- foreman:begin/end --> markers are none).
OLD_NS = r"(?<![\w-])(?:(?:sampo|foreman)(?:-ui|-design)?@foreman\b|foreman:(?=[a-z{<(\[])(?!begin\b|end\b))"
# The only paths that may still write one, each with why. Readers take both names for good (c.NAMESPACES,
# c.plugin_ids), so they need no entry; any other file that writes an old spelling is a producer and fails the scan.
OLD_NS_ALLOWED = {
    "CHANGELOG.md": "history",
    ".sampo": "task, research and decision records: history",
    "site/genesis": "Genesis generation records: history",
    "SEED_PROMPT.md": "the user's Genesis seed message, verbatim",
    "SAMPO.md": "the rename's spec names the ids it renames",
    "site/data/stats.json": "the homepage's record, quoting decision history (site/stats.py remakes it from state)",
    "site/cards": "published calling cards, made from the card store's past runs",
    "reset-claude.sh": "keeps the plugin installed under either id (a reader of both)",
    "plugin/hooks/hook": "the broken-library fallback names every id itself (a reader; core.plugin_ids can't load)",
    "scripts/probe-plugin-rename.sh": "installs the old ids in a scratch config to test the move",
    "demo/demo.cast": "a recorded session", "demo/demo.svg": "a recorded session",
    "demo/live.cast": "a recorded session", "demo/live.svg": "a recorded session",
    "plugin/tests/test_rename_scan.py": "this scan and its old-id reader cases",
    "plugin/tests/test_doctor.py": "old-id reader cases (an install record under foreman@foreman)",
    "plugin/tests/test_migrate.py": "the plugin move's cases: a config holding the old ids it moves",
    "plugin/tests/test_cost.py": "old-id reader cases (a transcript's foreman: listings)",
    "plugin/tests/test_hooks.py": "old-id reader cases (a foreman:fm-builder start, a typed /foreman:next)",
    "plugin/tests/test_setup.py": "old-id reader cases (an install record install.sh leaves to move itself)",
    "mods/foreman-ui/tests/foreman-ui.test.tsx": "the mod's old-id reader cases",
}


def old_ns_rows():
    out = subprocess.run(["git", "-C", ROOT, "grep", "-nIP", OLD_NS, "--", ".",
                          *(f":(exclude){p}" for p in OLD_NS_ALLOWED)], capture_output=True, text=True).stdout
    return [ln[:160] for ln in out.splitlines()]


class PluginIds(unittest.TestCase):
    def test_no_producer_writes_the_old_namespace(self):
        rows = old_ns_rows()
        self.assertEqual(rows, [], f"{len(rows)} lines write an old plugin namespace or id (sampo: and sampo@sampo "
                                   f"now; a reader takes both via c.NAMESPACES): {rows[:8]}")

    def test_old_ns_sees_the_forms(self):
        for old in ("/foreman:next", "load: Skill foreman:intake", "`foreman:fm-builder`", "claude plugin disable "
                    "foreman@foreman", "foreman-ui@foreman", "(foreman-design@foreman)", "'/foreman:next'", "sampo@foreman", "sampo-ui@foreman",
                    'f"(you type /foreman:{name})"', "/foreman:<name>", r'r"Skill foreman:([\w-]+)"'):
            self.assertTrue(re.search(OLD_NS, old), old)
        for fine in ("<!-- foreman:begin -->", "<!-- foreman:end -->", "sampo@sampo", "sampo-ui@sampo", "/sampo:next",
                     "mods/foreman-ui/", "foreman:Sampo", "user.email=foreman@localhost", "(?:sampo|foreman):x",
                     '("sampo@", "foreman@")', "rules/foreman.md", "foreman-ui:economy"):
            self.assertFalse(re.search(OLD_NS, fine), fine)

    def test_allowed_paths_exist(self):  # a stale entry would hide nothing but reads as a licence
        for p in OLD_NS_ALLOWED:
            self.assertTrue(os.path.exists(os.path.join(ROOT, p)), p)

    def test_manifests_name_sampo_and_map_every_old_name(self):
        # the renames map moves foreman@foreman installs to sampo@foreman by themselves (probe T-2879-probe): a
        # configured marketplace keeps the name it was added under, so only sampo migrate plugins reaches sampo@sampo
        import json
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json"), encoding="utf-8") as f:
            mk = json.load(f)
        self.assertEqual(mk["name"], "sampo")  # T-3707: sampo@sampo; sampo migrate plugins re-adds it on a machine
        sources = {e["name"]: e["source"] for e in mk["plugins"]}
        self.assertEqual(sources, {"sampo": "./plugin", "sampo-ui": "./mods/foreman-ui", "sampo-design": "./plugins/foreman-design"})
        self.assertEqual(mk.get("renames"), {"foreman": "sampo", "foreman-ui": "sampo-ui", "foreman-design": "sampo-design"})
        for name, src in sources.items():
            with open(os.path.join(ROOT, src, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["name"], name, src)


class ReadersTakeBoth(unittest.TestCase):
    """T-2879: an install, setting, log or habit from before the move says foreman; every reader still takes it."""

    def test_core_names_every_id(self):
        from sampo import core as c
        self.assertEqual(c.plugin_ids(), ("sampo@sampo", "sampo@foreman", "foreman@foreman"))
        self.assertEqual(c.plugin_ids("ui"), ("sampo-ui@sampo", "sampo-ui@foreman", "foreman-ui@foreman"))
        self.assertEqual(c.old_plugin_ids(), ("sampo@foreman", "foreman@foreman"))
        self.assertEqual(c.ns_names("fm-builder"), ("sampo:fm-builder", "foreman:fm-builder"))

    def test_hooks_see_a_skill_named_either_way(self):
        from sampo import hooks
        text = "load: Skill sampo:intake; /foreman:next, then Skill foreman:ui-design and /sampo:review"
        self.assertEqual(sorted(set(hooks._NAMED.findall(text))), ["intake", "next", "review", "ui-design"])
        for typed in ("/sampo:status now", "/foreman:status now"):
            self.assertEqual(hooks._TYPED.match(typed).group(1), "status", typed)

    def test_playbook_stage_either_way(self):
        from sampo import playbooks
        for ns in ("sampo", "foreman"):
            self.assertEqual(playbooks._STAGE.search(f"load: Skill {ns}:playbooks (clean)").group(1), "clean")

    def test_settings_read_the_mod_under_either_id(self):
        from sampo import settings
        for pid in ("sampo-ui@sampo", "sampo-ui@foreman", "foreman-ui@foreman"):
            data = {"enabledPlugins": {pid: True}, "pluginConfigs": {pid: {"options": {"mascot": "blue"}}}}
            self.assertEqual(settings.plugin_id(data), pid)
            self.assertEqual(settings.plugin_values(data), {"mascot": "blue"})
        self.assertEqual(settings.plugin_id({}), "sampo-ui@sampo")  # nothing recorded: the id Claude Code uses now

    def test_output_style_ids_both(self):
        from sampo import setup
        for sid in ("sampo:Sampo", "foreman:Sampo", "foreman:Foreman"):
            self.assertIn(sid, setup.OUTPUT_STYLES)

    def test_a_hook_knows_the_name_its_session_loaded(self):  # review: Claude Code's data folder is <name>-<market>
        from unittest import mock
        from sampo import core as c
        for data, want in (("/c/plugins/data/foreman-foreman", "foreman"), ("/c/plugins/data/sampo-foreman/", "sampo"),
                           ("/c/plugins/data/sampo-sampo", "sampo"), ("/c/plugins/data/sampo-ui-sampo", None),
                           ("/c/plugins/data/sampo-ui-foreman", None), ("", None)):
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": data}):
                self.assertEqual(c.loaded_ns(), want, data)

    def test_text_for_an_old_session_names_what_it_loaded(self):
        from sampo import core as c
        text = "load: Skill sampo:intake; /sampo:next; Agent sampo:fm-builder; claude plugin disable sampo@foreman; sampo: ok"
        self.assertEqual(c.as_loaded(text, "foreman"), "load: Skill foreman:intake; /foreman:next; Agent foreman:fm-builder; "
                                                       "claude plugin disable sampo@foreman; sampo: ok")
        self.assertEqual(c.as_loaded(text, "sampo"), text)

    def test_installed_ids_read_claude_codes_record(self):  # review: a disable hint names the id that is there
        import json
        import tempfile
        from unittest import mock
        from sampo import core as c
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, "plugins"))
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": t}):
                self.assertEqual(c.installed_ids(), ())
                with open(os.path.join(t, "plugins", "installed_plugins.json"), "w") as f:
                    json.dump({"plugins": {"foreman@foreman": [{}], "foreman-ui@foreman": [{}]}}, f)
                self.assertEqual(c.installed_ids(), ("foreman@foreman",))
                self.assertEqual(c.installed_ids("ui"), ("foreman-ui@foreman",))

    def test_settings_merge_the_mods_options_across_its_ids(self):  # review: never drop a saved option
        import json
        import tempfile
        from sampo import settings
        data = {"pluginConfigs": {"foreman-ui@foreman": {"options": {"mascot": "blue", "motion": "off"}},
                                  "sampo-ui@foreman": {"options": {"mascot": "green"}}}}
        self.assertEqual(settings.plugin_values(data), {"mascot": "green", "motion": "off"})
        with tempfile.TemporaryDirectory() as t:
            real = os.path.join(t, "settings.json")
            settings._write_plugin(data, "sampo-ui@foreman", "palette", "cb", real, "")
            with open(real) as f:
                self.assertEqual(json.load(f)["pluginConfigs"]["sampo-ui@foreman"]["options"],
                                 {"mascot": "green", "motion": "off", "palette": "cb"})
            settings._write_plugin(data, "sampo-ui@foreman", "motion", None, real, "")  # an unset holds on both
            self.assertNotIn("motion", settings.plugin_values(data))


if __name__ == "__main__":
    unittest.main()
