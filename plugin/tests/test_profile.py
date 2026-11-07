"""T-3672: the user's machine-local profile (sampo profile): built from Claude's memories and a seed, kept in the state
folder (git-ignored, never in a repo or a public CLAUDE.md), injected where an agent starts. Made-up values only."""
import json
import os
import subprocess
import sys

from helpers import FM, HOOK, SampoTestCase, git_repo

SEED = ("# notes\n<!-- sampo-profile:begin -->\n# The user\n- Full autonomy, standing:\n  - Never ask; decide and report.\n"
        "- Visual taste: crafted, animated, real data.\n<!-- sampo-profile:end -->\n")


class Profile(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.cc = os.path.join(self.tmp, "cc")
        self.env = {"CLAUDE_CONFIG_DIR": self.cc, "LOGNAME": "alice", "USER": "alice"}

    def plant(self, name, kind, desc, folder="-home-alice-app", body="Body line."):
        d = os.path.join(self.cc, "projects", folder, "memory")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: {kind}\n---\n\n{body}\n")

    def seed(self):
        os.makedirs(self.cc, exist_ok=True)
        with open(os.path.join(self.cc, "CLAUDE.md"), "w") as f:
            f.write(SEED)

    def built(self):
        self.seed()
        self.plant("replies", "feedback", "Keep replies short and quiet; spell out codes")
        self.plant("niced", "user", "Runs heavy jobs niced on a shared 16-core box")
        self.plant("oneproject", "project", "The importer ships Friday")  # one project's fact: not the user
        self.plant("ssh", "reference", "Push over port 443", folder="-home-alice-app")
        self.plant("ssh", "reference", "Push over port 443", folder="-home-alice-site")  # recurs: kept
        self.plant("box", "feedback", "Deliver to alice@jarvis.home.example.test (10.0.0.7) under /home/alice/work")
        self.plant("taste", "user", '"Cartoonish reads as \\"goofy\\" to them"')  # a quoted YAML scalar
        self.fm("profile", "rebuild", env=self.env)

    def profile_text(self):
        with open(os.path.join(self.home, "state", "profile", "profile.md")) as f:
            return f.read()

    def context(self, p):
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout.strip() else ""

    def test_a_folder_sampo_never_set_up_starts_with_the_profile(self):
        self.built()
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        ctx = self.context(self.hook("SessionStart", {"cwd": plain, "source": "startup"}))
        self.assertIn("Keep replies short and quiet", ctx)
        self.assertIn("Full autonomy, standing", ctx)
        self.assertIn("new to Sampo", ctx)
        self.assertIn("sampo init", ctx)

    def test_a_project_session_carries_it_within_the_budget(self):
        from sampo import hooks, profile
        self.built()
        ctx = self.context(self.hook("SessionStart", {"source": "startup"}))
        self.assertIn("Keep replies short and quiet", ctx)
        self.assertLessEqual(len(ctx), hooks.CTX_BUDGET + profile.DIGEST_MAX + profile.LIVE_MAX + 200)

    def test_a_fresh_install_injects_nothing(self):
        self.plant("replies", "feedback", "Keep replies short and quiet")  # memories alone build nothing
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(self.hook("SessionStart", {"cwd": plain}, env=self.env).stdout.strip(), "")
        self.assertNotIn("Keep replies", self.context(self.hook("SessionStart", {}, env=self.env)))
        self.assertEqual(self.hook("SubagentStartContext", {"agent_type": "x"}, env=self.env).stdout.strip(), "")
        self.assertIn("no profile", self.fm("profile", "show", env=self.env).stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "profile", "profile.md")))

    def test_rebuild_keeps_the_user_and_scrubs_the_machine(self):
        self.built()
        text = self.profile_text()
        for kept in ("Keep replies short", "Runs heavy jobs niced", "Push over port 443", "Full autonomy",
                     '- Cartoonish reads as "goofy" to them\n'):
            self.assertIn(kept, text)
        self.assertNotIn("The importer ships Friday", text)
        for leak in ("jarvis.home.example.test", "10.0.0.7", "alice", "/home/"):
            self.assertNotIn(leak, text)
        self.assertEqual(text.count("Push over port 443"), 1)
        self.assertEqual(oct(os.stat(os.path.join(self.home, "state", "profile", "profile.md")).st_mode & 0o777),
                         "0o600")
        self.fm("profile", "rebuild", env=self.env)
        self.assertEqual(self.profile_text(), text)  # a second run writes the same profile
        self.assertEqual(self.fm("profile", "check", env=self.env).returncode, 0)

    def test_the_privacy_check_refuses_a_planted_ip_or_domain(self):
        self.built()
        path = os.path.join(self.home, "state", "profile", "profile.md")
        for value, kind in (("10.0.0.7", "ip"), ("jarvis.home.example.test", "domain"), ("alice", "login")):
            with open(path, "a") as f:
                f.write(f"- Box {value} here\n")
            p = self.fm("profile", "check", env=self.env, check=False)
            self.assertNotEqual(p.returncode, 0, value)
            self.assertIn(kind, p.stdout + p.stderr)
            self.assertNotIn(value, p.stdout + p.stderr)  # names the kind, never the value
            self.assertNotIn("Keep replies", self.context(self.hook("SessionStart", {}, env=self.env)))
            self.fm("profile", "rebuild", env=self.env)

    def test_a_subagent_gets_a_shorter_digest_under_its_budget(self):
        from sampo import profile
        self.built()
        out = self.hook("SubagentStartContext", {"agent_type": "general-purpose", "agent_id": "a1"})
        ctx = self.context(out)
        self.assertIn("Full autonomy", ctx)
        self.assertLessEqual(len(ctx.encode()), profile.BRIEF_MAX)
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["hookEventName"], "SubagentStart")

    def test_the_mcp_resource_and_tool_carry_the_profile(self):
        self.built()
        self.fm("init")
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "sampo://profile"}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "profile", "arguments": {}}}]
        p = subprocess.run([sys.executable, FM, "mcp"], input="\n".join(map(json.dumps, msgs)) + "\n",
                           capture_output=True, text=True, cwd=self.repo, timeout=120,
                           env=dict(os.environ, FOREMAN_HOME=self.home))
        got = {r["id"]: r for r in map(json.loads, p.stdout.splitlines())}
        self.assertEqual([r["uri"] for r in got[1]["result"]["resources"]], ["sampo://profile"])
        self.assertIn("Keep replies short", got[2]["result"]["contents"][0]["text"])
        self.assertIn("Keep replies short", got[3]["result"]["content"][0]["text"])

    def test_the_builder_brief_carries_the_profile(self):
        self.built()
        tid = json.loads(self.fm("task", "new", "Router", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)
        tid = (tid[0] if isinstance(tid, list) else tid)["id"]
        with open(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"]) as f:
            brief = f.read()
        self.assertIn("## The user (machine-local profile)", brief)
        self.assertIn("Keep replies short", brief)

    def test_codex_gets_it_only_when_linked_and_loses_it_when_unlinked(self):
        self.built()
        codex = os.path.join(self.tmp, "codex")
        os.makedirs(codex)
        rules = os.path.join(codex, "AGENTS.md")
        with open(rules, "w") as f:
            f.write("# mine\n")
        env = dict(self.env, CODEX_HOME=codex)
        self.fm("profile", "rebuild", env=env)
        with open(rules) as f:
            self.assertEqual(f.read(), "# mine\n")  # off by default: a rebuild never writes a tool's file unasked
        self.fm("profile", "link", "codex", env=env)
        with open(rules) as f:
            self.assertIn("Keep replies short", f.read())
        self.fm("profile", "link", "codex", "--off", env=env)
        with open(rules) as f:
            self.assertEqual(f.read(), "# mine\n")
        self.assertNotEqual(self.fm("profile", "link", "vim", env=env, check=False).returncode, 0)

    def test_opencode_session_start_carries_the_digest(self):
        self.built()
        repo = git_repo(self.tmp, "oc")
        p = subprocess.run([sys.executable, HOOK, "--agent", "opencode", "SessionStart"],
                           input=json.dumps({"session_id": "oc-1", "cwd": repo}), capture_output=True, text=True,
                           timeout=30, cwd=repo, env=dict(os.environ, FOREMAN_HOME=self.home))
        self.assertIn("Keep replies short", json.loads(p.stdout)["context"])

    def test_live_line_names_other_projects_but_never_a_sensitive_one(self):
        from sampo import profile
        self.built()
        self.fm("init")
        other = git_repo(self.tmp, "secretapp")
        self.fm("init", cwd=other)
        self.fm("sensitive", "on", cwd=other)
        sessions = os.path.join(self.home, "state", "sessions")
        os.makedirs(sessions, exist_ok=True)
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        slugs = {}
        for sid, root in (("s-a", self.repo), ("s-b", other)):
            from sampo import core as c
            slugs[sid] = c.find_project(root).slug
            with open(os.path.join(sessions, f"{sid}.json"), "w") as f:
                json.dump({"session_id": sid, "project": slugs[sid], "cwd": root, "ts": now}, f)
        line = profile.live_line("me")
        self.assertIn(slugs["s-a"], line)
        self.assertNotIn(slugs["s-b"], line)
        self.assertLessEqual(len(line), profile.LIVE_MAX)
