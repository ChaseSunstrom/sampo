"""fm ideas: tool-less brainstorm children (claude -p with no tools and no MCP), one per lens, in parallel."""
import json
import os
import time

from helpers import ForemanTestCase, read_text

from sampo import core as c

STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
lens = next(l for l in args if l.startswith("Lens: ")).split("\n")[0][6:] if any(a.startswith("Lens: ") for a in args) else "?"
stdin = sys.stdin.read()
lens = next((l for l in stdin.splitlines() if l.startswith("Lens: ")), "Lens: ?")[6:]
system = open(args[args.index("--append-system-prompt-file") + 1]).read() if "--append-system-prompt-file" in args else ""
stdin = system + "\n" + stdin  # T-1265: what the child reads in all, pack included
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "stdin": stdin, "system": system, "cwd": os.getcwd(), "t": time.time()}) + "\n")
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
if lens == os.environ.get("STUB_FAIL_LENS"):
    sys.exit(3)
if lens == os.environ.get("STUB_CHAT_LENS"):
    print("Could you tell me more about the project first?")
    sys.exit(0)
phrases = ["apple banana cherry", "delta echo foxtrot", "golf hotel india", "juliet kilo lima", "mike november oscar"]
vary = " " + phrases[stdin.count(chr(10) + '- ') // 2 % 5] if os.environ.get("STUB_VARY") else ""
cat = "speed" if "user" in lens or "deepen" in lens else "looks"
idea = f"- **Idea for {lens}{vary}** — FEATURE — value 4 — effort S — risk low — category: {cat}"
if os.environ.get("STUB_JSON"):  # T-1265: claude -p's json, with the prompt cache's token counts
    print(json.dumps({"type": "result", "result": idea, "total_cost_usd": 0.01,
                      "usage": {"cache_read_input_tokens": 200, "cache_creation_input_tokens": 100}}))
else:
    print(idea)
if os.environ.get("STUB_QUESTION"):  # T-2076: the question this lens says the next brainstorm should ask
    print("Next question: " + os.environ["STUB_QUESTION"])
sys.exit(2 if lens == os.environ.get("STUB_PARTIAL_LENS") else 0)
'''


class Stubbed(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log}
        self.pack = os.path.join(self.tmp, "pack.md")
        with open(self.pack, "w") as f:
            f.write("Project: a tiny calculator app.\nUser: super improve it\n")
        self.fm("init")

    def calls(self):
        return [json.loads(l) for l in read_text(self.log).splitlines()]


class Ideas(Stubbed):

    def test_super_brainstorm_rounds_build_on_earlier_ideas_until_dry(self):
        # T-0071: each round sees every idea so far and is asked only for new ones; a round with nothing new ends it
        two = ["--lens", "user value", "--lens", "bold bets"]
        res = json.loads(self.fm("ideas", "--pack", self.pack, *two, "--rounds", "4", "--json", env=self.env).stdout)
        self.assertEqual(res["rounds"], 2, "round 2 repeated round 1: dry")
        later = [x for x in self.calls() if "Ideas so far" in x["stdin"]]
        self.assertTrue(later and "Idea for user value" in later[0]["stdin"])
        self.assertIn("Idea for bold bets", read_text(os.path.join(res["dir"], "ideas.md")))
        res = json.loads(self.fm("ideas", "--pack", self.pack, *two, "--rounds", "3", "--dry", "1", "--json",
                                 env=dict(self.env, STUB_VARY="1")).stdout)
        self.assertEqual(res["rounds"], 3)
        index = read_text(os.path.join(res["dir"], "ideas.md"))
        self.assertEqual(index.split("## New ideas per lens")[0].count("- "), 6)
        self.assertEqual(res["yield"], {"user value": 3, "bold bets": 3})

    def test_one_toolless_child_per_lens_in_parallel(self):
        env = dict(self.env, STUB_SLEEP="1", FOREMAN_IDEAS_WARM="0")
        start = time.time()
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "reliability", "--lens", "bold bets",
                                 "--json", env=env).stdout)
        self.assertLess(time.time() - start, 1.9, "children run concurrently")  # wall clock: children sleep, and run in parallel
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        for call in calls:
            a = call["args"]
            self.assertEqual(a[a.index("--tools") + 1], "")
            self.assertIn("--strict-mcp-config", a)
            self.assertEqual(a[a.index("--setting-sources") + 1], "project,local", "no user plugins/hooks leak in")
            self.assertEqual(json.loads(a[a.index("--mcp-config") + 1]), {"mcpServers": {}})
            self.assertIn("-p", a)
            self.assertIsNone(c.find_project(call["cwd"]), "children run outside any Foreman project")
        self.assertEqual(sorted(r["lens"] for r in res["results"]), ["bold bets", "reliability"])
        for r in res["results"]:
            self.assertIn(f"Idea for {r['lens']}", read_text(r["file"]))
        self.assertIn("super improve it", calls[0]["system"], "the pack goes in a file (no argv size limit)")
        self.assertNotIn("super improve it", " ".join(calls[0]["args"]))

    def test_the_grid_lens_sends_its_note_to_the_child(self):  # T-2672
        from sampo import ideas
        self.assertIn("one idea per cell", ideas.LENS_NOTES["grid"])
        self.fm("ideas", "--pack", self.pack, "--lens", "grid", env=self.env)
        stdin = self.calls()[0]["stdin"]
        self.assertIn("Lens: grid", stdin)
        self.assertIn(ideas.LENS_NOTES["grid"], stdin)

    def test_the_pack_is_one_cached_system_prompt_and_the_first_child_warms_it(self):  # T-1265
        env = dict(self.env, STUB_SLEEP="1", FOREMAN_IDEAS_WARM="5", STUB_JSON="1")
        three = ["--lens", "reliability", "--lens", "bold bets", "--lens", "delight"]
        out = self.fm("ideas", "--pack", self.pack, *three, env=env).stdout
        calls = sorted(self.calls(), key=lambda x: x["t"])
        self.assertEqual(len({x["system"] for x in calls}), 1, "one system text: the cache's shared prefix")
        self.assertIn("super improve it", calls[0]["system"])
        for x in calls:
            self.assertNotIn("super improve it", x["stdin"][len(x["system"]):], "the message is the lens alone")
            self.assertTrue(x["stdin"][len(x["system"]):].strip().startswith("Lens: "))
        self.assertGreater(calls[1]["t"] - calls[0]["t"], 0.9, "the rest wait for the first child to write the cache")
        self.assertLess(calls[1]["t"] - calls[0]["t"], 4, "it ended before the warm time: no longer wait")
        self.assertLess(calls[2]["t"] - calls[1]["t"], 0.8, "then the rest run together")
        self.assertIn("prompt cache: 600 tokens read, 300 written", out)
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "delight", "--json", env=env).stdout)
        manifest = json.loads(read_text(os.path.join(res["dir"], "manifest.json")))
        self.assertEqual(manifest["cache"], {"read": 200, "written": 100})

    def test_a_pack_that_already_carries_the_users_words_gets_them_once(self):  # T-2249
        from sampo import core as c
        from sampo import ideas
        self.fm("task", "new", "make the totals add up", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                "--step", "s")
        voice = ideas.user_voice(c.find_project(self.repo))
        self.assertIn("make the totals add up", voice)
        with open(self.pack, "a") as f:  # what fm mission writes
            f.write(voice)
        self.fm("ideas", "--pack", self.pack, "--lens", "delight", env=self.env)
        head = "## The user's own words"
        self.assertEqual(self.calls()[0]["system"].count(head), 1)
        os.remove(self.log)
        with open(self.pack, "w") as f:  # a plain pack still gets them
            f.write("Project: a tiny calculator app.\n")
        self.fm("ideas", "--pack", self.pack, "--lens", "delight", env=self.env)
        self.assertEqual(self.calls()[0]["system"].count(head), 1)

    def test_missing_claude_is_a_clear_error(self):
        empty = os.path.join(self.tmp, "empty-bin")
        os.makedirs(empty)
        env = dict(self.env, PATH=empty)  # fm itself runs via sys.executable; no `claude` anywhere on PATH
        p = self.fm("ideas", "--pack", self.pack, "--lens", "reliability", env=env, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("claude", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_lenses_with_the_same_slug_get_separate_files(self):
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "ünïcode", "--lens", "日本",
                                 "--json", env=self.env).stdout)
        self.assertEqual(len({r["file"] for r in res["results"]}), 2)

    def test_failed_lens_is_reported_and_others_kept(self):
        env = dict(self.env, STUB_FAIL_LENS="performance")
        p = self.fm("ideas", "--pack", self.pack, "--lens", "performance", "--lens", "simplicity", env=env, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("performance", p.stderr)
        out = os.path.join(c.find_project(self.repo).dir, "research")
        files = [f for _, _, fs in os.walk(out) for f in fs]
        self.assertTrue(any("simplicity" in f for f in files))

    def test_an_answer_without_ideas_counts_as_a_failed_lens(self):
        env = dict(self.env, STUB_CHAT_LENS="simplicity")
        p = self.fm("ideas", "--pack", self.pack, "--lens", "simplicity", "--lens", "reliability", env=env, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("simplicity", p.stderr)
        self.assertIn("no ideas in the required format", p.stdout)

    def test_seen_ideas_seed_the_pack_and_the_dedupe(self):
        old = os.path.join(self.tmp, "old-ideas.md")
        with open(old, "w") as f:
            f.write("# Ideas by round\n\n## Round 1\n- Idea for user value\n- Cache the parser\n\n"
                    "## New ideas per lens\n- user value: 2\n")
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "user value", "--lens", "bold bets",
                                 "--seen", old, "--json", env=self.env).stdout)
        self.assertEqual(res["ideas"], 1, "the user-value idea was already seen; only bold bets is new")
        stdin = self.calls()[0]["stdin"]
        self.assertIn("- Cache the parser", stdin)
        self.assertNotIn("- user value: 2", stdin, "the per-lens tally isn't an idea")

    def test_seen_latest_reads_the_newest_earlier_runs_without_typed_paths(self):  # T-2559
        p = c.find_project(self.repo, create=True)
        idea = "# Ideas by round\n\n## Round 1\n- {}\n"
        for stamp, text in (("20260101-000000", "An old idea nobody repeats"), ("20260102-000000", "Idea for user value"),
                            ("20260103-000000", None)):  # the newest run has no ideas.md (it failed): skipped
            d = os.path.join(p.dir, "research", f"brainstorm-{stamp}")
            os.makedirs(d, exist_ok=True)
            if text:
                with open(os.path.join(d, "ideas.md"), "w") as f:
                    f.write(idea.format(text))
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "user value", "--lens", "bold bets",
                                 "--seen-latest", "1", "--json", env=self.env).stdout)
        self.assertEqual(res["ideas"], 1, "the newest run with ideas (user value) was seen; only bold bets is new")
        stdin = self.calls()[0]["stdin"]
        self.assertIn("Idea for user value", stdin)
        self.assertNotIn("An old idea nobody repeats", stdin, "only the newest 1")

    def test_ideas_name_the_finished_task_they_resemble(self):
        # T-0208: most ideas in a dry run were already built; say which before the main thread greps for it
        tid = json.loads(self.fm("task", "new", "Idea for user value", "--type", "FEATURE", "--tier", "S", "--ac",
                                 "works :: true", "--step", "do it", "--json").stdout)["id"]
        self.fm("focus", tid)
        self.fm("task", "step", tid, "done", "1", "--evidence", "true", "ok")
        self.fm("task", "ac", tid, "check", "1", "--evidence", "true", "ok")
        self.fm("task", "audit", tid, "self", "x", "ok")
        self.fm("task", "set", tid, "--section", "Regression test", "--text", "none: fixture")
        self.fm("task", "done", tid)
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "user value", "--lens", "bold bets",
                                 "--json", env=self.env).stdout)
        index = read_text(os.path.join(res["dir"], "ideas.md"))
        self.assertIn(f"- Idea for user value — near {tid} (done)", index)
        self.assertRegex(index, r"(?m)^- Idea for bold bets$", "no finished task shares its words")
        self.env["STUB_VARY"] = ""
        again = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "user value", "--seen",
                                   os.path.join(res["dir"], "ideas.md"), "--json", env=self.env).stdout)
        self.assertIn("- Idea for user value\n", self.calls()[-1]["stdin"], "the note isn't part of the title")
        self.assertEqual(again["ideas"], 0)

    def test_brainstorms_learn_from_the_asks_they_missed(self):
        # T-2076: the user had to ask for skills, playbooks and a site refresh that 104 brainstorms never surfaced; the
        # asks no idea predicted feed the next brainstorm, with a lens that turns each into the question that would have
        # caught it, and each run leaves questions for the next ("prompt yourself on how to prompt yourself")
        import re
        from sampo import ideas
        p = c.find_project(self.repo)
        d = os.path.join(p.dir, "research", "brainstorm-20250101-000000")
        os.makedirs(d)
        with open(os.path.join(d, "ideas.json"), "w") as f:
            json.dump({"lenses": {"user value": ["Add a skills catalogue Claude reaches for"]},
                       "questions": ["What does a user meet first?"], "created": "2025-01-01T00:00:00Z"}, f)
        cap = lambda text, src="user": re.search(r"T-\d+", self.fm("capture", text, "--source", src).stdout).group(0)
        old = cap("Refresh the onboarding tour")
        import glob  # before any brainstorm: nothing could predict it
        path = glob.glob(os.path.join(p.dir, "tasks", old + "-*.md"))[0]
        with open(path) as f:
            text = re.sub(r"(?m)^created: .*$", "created: 2020-01-01T00:00:00Z", f.read(), count=1)
        with open(path, "w") as f:
            f.write(text)
        cap("Add a skills catalogue that Claude reaches for")  # predicted
        miss = cap("Refresh the website docs for the code UI")
        cap("Clean the cache folder", "discovered")  # not the user's ask
        self.assertEqual(ideas.misses(p), [(miss, "Refresh the website docs for the code UI")])
        from sampo import mission  # fm mission passes its own lenses, so it adds the blind-spots one itself
        self.assertIn("blind spots", mission.compose(p)["lenses"])
        self.env["STUB_QUESTION"] = "Which screen did nobody open?"
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--json", env=self.env).stdout)
        lenses = {next(l for l in x["stdin"].splitlines() if l.startswith("Lens: "))[6:].split(" — ")[0]
                  for x in self.calls()}
        self.assertIn("blind spots", lenses)
        stdin = self.calls()[0]["stdin"]
        missed = stdin.split("## What earlier brainstorms missed", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"- {miss}: Refresh the website docs for the code UI", missed)
        self.assertNotIn("onboarding", missed)
        self.assertNotIn("skills catalogue", missed)
        self.assertIn("- What does a user meet first?", stdin.split("## Questions earlier brainstorms left", 1)[1])
        with open(os.path.join(res["dir"], "ideas.json")) as f:
            self.assertEqual(json.load(f)["questions"], ["Which screen did nobody open?"])
        self.fm("ideas", "--pack", self.pack, "--lens", "user value", env=self.env)
        last = self.calls()[-1]["stdin"]
        self.assertIn("- Which screen did nobody open?", last)
        self.assertNotIn("Lens: blind spots", last, "a caller's own lenses stay as given")
        prompt = read_text(ideas.PROMPT)
        self.assertIn("Next question:", prompt)
        self.assertIn('"blind spots"', prompt)

    def test_default_lenses(self):
        self.fm("ideas", "--pack", self.pack, env=self.env)
        # T-0061 four; T-0099 adds unspoken needs and delight; T-0364 capability map and approaches
        self.assertEqual(len(self.calls()), 8)

    def test_default_lenses_cover_capabilities_and_approaches(self):
        # T-0364: "it doesn't think of every possible solution/capability": no lens looked at the whole space of what
        # a complete version has, or at every way to solve the problem; 8-12 ideas per lens capped the breadth
        self.fm("ideas", "--pack", self.pack, env=self.env)
        lenses = {next(l for l in x["stdin"].splitlines() if l.startswith("Lens: "))[6:] for x in self.calls()}
        self.assertTrue({"capability map", "approaches"} <= lenses, lenses)
        self.assertIn("10-15 ideas", self.calls()[0]["stdin"])
        from sampo import ideas
        prompt = read_text(ideas.PROMPT)
        self.assertIn('"capability map"', prompt)
        self.assertIn('"approaches"', prompt)

    def test_default_lenses_look_for_unspoken_needs_and_delight(self):
        # T-0099: the user wanted ideas they can't put into words, and more creative ones
        self.fm("ideas", "--pack", self.pack, env=self.env)
        lenses = {next(l for l in x["stdin"].splitlines() if l.startswith("Lens: "))[6:] for x in self.calls()}
        self.assertTrue({"unspoken needs", "delight", "user value", "bold bets"} <= lenses, lenses)

    def test_every_pack_carries_the_users_own_words(self):
        self.fm("capture", "make the dashboard feel alive")
        self.hook("UserPromptSubmit", {"prompt": "no, don't stop after one task"})
        self.fm("ideas", "--pack", self.pack, "--lens", "user value", env=self.env)
        stdin = self.calls()[0]["stdin"]
        self.assertIn("The user's own words", stdin)
        self.assertIn("make the dashboard feel alive", stdin)
        self.assertIn("don't stop after one task", stdin)

    def test_deepen_builds_off_the_biggest_categories(self):
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "user value", "--lens", "user value 2",
                                 "--lens", "delight", "--deepen", "1", "--json", env=self.env).stdout)
        deep = [x for x in self.calls() if "Lens: deepen: speed" in x["stdin"]]
        self.assertEqual(len(deep), 1, "one yes-and round for the top category (speed: 2 ideas)")
        self.assertIn("Idea for user value", deep[0]["stdin"])
        self.assertIn("## Deepened: speed", read_text(os.path.join(res["dir"], "ideas.md")))


class ChildCommand(ForemanTestCase):
    def test_children_carry_the_run_cap(self):
        """T-0776: a runaway child stops at fm budget's per-command cap."""
        from sampo import budget
        from sampo import ideas
        from sampo import research
        cap = f"{budget.effective()[0]['run']:g}"
        for argv in (ideas.child_cmd("haiku", "s"), research._child("haiku", "WebFetch", "s")):
            self.assertEqual(argv[argv.index("--max-budget-usd") + 1], cap)


class BoardIdeas(Stubbed):
    """T-0959: a child's partial answer kept, a run manifest, cache-bust causes, friction minutes, unused verbs."""

    def test_a_child_that_fails_after_printing_keeps_its_text_flagged_partial(self):
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "reliability", "--lens", "bold bets", "--json",
                                 env=dict(self.env, STUB_PARTIAL_LENS="reliability")).stdout)
        r = next(r for r in res["results"] if r["lens"] == "reliability")
        self.assertTrue(r["ok"] and r["partial"])
        self.assertEqual(r["titles"], ["Idea for reliability"])
        self.assertIn("exit 2", r["error"])
        self.assertIn("PARTIAL", read_text(r["file"]))
        self.assertIn("Idea for reliability", read_text(r["file"]))
        self.assertFalse(next(r for r in res["results"] if r["lens"] == "bold bets")["partial"])

    def test_ideas_writes_a_run_manifest_beside_ideas_json(self):
        import hashlib
        seen = os.path.join(self.tmp, "old.md")
        with open(seen, "w") as f:
            f.write("- An older idea\n")
        res = json.loads(self.fm("ideas", "--pack", self.pack, "--lens", "reliability", "--seen", seen,
                                 "--model", "haiku", "--json", env=self.env).stdout)
        self.assertTrue(os.path.isfile(os.path.join(res["dir"], "ideas.json")))
        m = json.loads(read_text(os.path.join(res["dir"], "manifest.json")))
        self.assertEqual(m["pack_sha256"], hashlib.sha256(read_text(self.pack).encode()).hexdigest())
        self.assertEqual((m["model"], m["lenses"], m["seen"]), ("haiku", ["reliability"], [seen]))
        self.assertTrue(m["created"].startswith("20"))

    def test_cost_labels_each_cache_bust_ttl_or_context_edit(self):
        import datetime
        import re
        folder = os.path.join(self.tmp, "cfg", "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.repo)))
        os.makedirs(folder)
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = ((20, 0, 40000), (19, 40000, 2000), (18, 1000, 45000), (8, 1000, 46000))  # 1 min gap, then 10 min
        with open(os.path.join(folder, "sess-a.jsonl"), "w") as f:
            for n, (ago, r, w) in enumerate(rows):
                ts = (now - datetime.timedelta(minutes=ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(json.dumps({"type": "assistant", "timestamp": ts, "sessionId": "sess-a", "message": {
                    "id": f"m{n}", "role": "assistant", "content": [], "usage": {
                        "cache_read_input_tokens": r, "cache_creation_input_tokens": w, "input_tokens": 0,
                        "output_tokens": 500}}}) + "\n")
        env = {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "cfg")}
        s = json.loads(self.fm("cost", "--sessions", "--json", env=env).stdout)["sessions"][0]
        self.assertEqual((s["busts"], s["bust_causes"]), (2, {"context edit": 1, "ttl": 1}))
        self.assertIn("2 busts (1 context edit, 1 ttl)", self.fm("cost", "--sessions", env=env).stdout)

    def events(self, *rows):
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_friction_minutes_estimates_minutes_lost_per_week(self):
        now, old = c.now(), c.iso(time.time() - 10 * 86400)
        self.events({"ts": now, "kind": "guard_block", "category": "x"}, {"ts": now, "kind": "guard_block"},
                    {"ts": old, "kind": "guard_block"}, {"ts": now, "kind": "tool_fail", "tool": "Bash", "ms": 60000},
                    {"ts": now, "kind": "hook_ms", "ms": 30000}, {"ts": now, "kind": "hook_ms", "ms": 30000})
        d = json.loads(self.fm("friction", "--minutes", "--json").stdout)
        self.assertEqual(d["minutes"], 3.5)  # 2 blocks x 30 s + (60 s + 30 s retry) + 60 s of hooks
        self.assertEqual(d["by_kind"], {"guard blocks": 1.0, "failed tool calls": 1.5, "hook time": 1.0})
        self.assertIn("3.5 min lost per week", self.fm("friction", "--minutes").stdout)

    def test_burden_by_verb_lists_fm_verbs_never_called(self):
        self.events({"ts": c.now(), "kind": "tool", "tool": "Bash", "target": "fm queue"})
        d = json.loads(self.fm("burden", "--by-verb", "--json").stdout)
        self.assertIn("orders", d["unused"])
        self.assertNotIn("queue", d["unused"])

    def test_rotated_event_logs_count_too(self):
        logs = os.path.join(c.state_dir(), "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "events-20990101-000000.jsonl"), "w") as f:  # fm tidy rotated it today
            f.write(json.dumps({"ts": c.now(), "kind": "tool", "tool": "Bash", "target": "fm orders"}) + "\n")
            f.write(json.dumps({"ts": c.now(), "kind": "guard_block"}) + "\n")
        self.events({"ts": c.now(), "kind": "guard_block"})
        self.assertNotIn("orders", json.loads(self.fm("burden", "--by-verb", "--json").stdout)["unused"])
        self.assertEqual(json.loads(self.fm("friction", "--minutes", "--json").stdout)["by_kind"]["guard blocks"], 1.0)


class SeenIndex(ForemanTestCase):
    INDEX = ("# Ideas by round\nIdeas: 3 distinct\n\n## Round 1\n- Cache the parser\n- Quote shell paths — near T-0004 (done): x\n"
             "\n## Deepened: speed\n- Warm the cache at start\n\n## New ideas per lens\n- bold bets: 40\n"
             "\n## Coverage (ideas per category)\n- capability map 23\nThin (0-1 ideas): none\n"
             "\n## Kill it fast (the quickest test that would show it isn't worth building)\n- Cache the parser — time it\n")

    def test_seen_reads_only_the_idea_sections(self):
        # T-1056: coverage and kill bullets were read back as idea titles
        from sampo import ideas
        self.assertEqual(ideas.seen_titles(self.INDEX), ["Cache the parser", "Quote shell paths", "Warm the cache at start"])


class IdeasLocal(Stubbed):  # T-2910: --model local answers every child through moonscale's routing
    PROFILE = {"name": "jarvis", "kind": "openai", "base_url": "https://127.0.0.1:9/v1", "model": "m",
               "key_env": "MOONS_TEST_KEY", "context": 8192, "tools": False, "slots": 2}

    def run_local(self, up=True, sensitive=False, host="127.0.0.1"):
        import argparse
        from unittest import mock
        from sampo import ideas, models, moonscale
        if sensitive:
            self.fm("sensitive", "on")
        args = argparse.Namespace(pack=self.pack, lens=["reliability", "bold bets"], model="local", rounds=1, dry=3,
                                  seen=None, seen_latest=None, deepen=1, falsify=True, crossbreed=True, timeout=30,
                                  project=c.find_project(self.repo).slug, json=False)
        asked = []

        def complete(prof, msgs, **k):
            asked.append(msgs[-1]["content"])
            if msgs[-1]["content"].startswith("KILL"):
                return {"text": "1: run it once"}
            return {"text": "- **Local idea %d** — FEATURE — value 4 — effort S — risk low — category: speed" % len(asked)}
        with mock.patch.dict(os.environ, {"MOONS_LOCAL": "jarvis", "MOONS_TEST_KEY": "k", **self.env}), \
                mock.patch.object(models, "resolve", return_value=dict(self.PROFILE, base_url=f"https://{host}:9/v1", own=True)), \
                mock.patch.object(moonscale, "_up", return_value=up), \
                mock.patch.object(models, "complete", side_effect=complete) as comp, \
                mock.patch("sys.stdout") as out:
            try:
                ideas.cmd_ideas(args)
                err = None
            except Exception as e:
                err = e
        return comp, "".join(x.args[0] for x in out.write.call_args_list), err

    def manifest(self):
        import glob
        m = sorted(glob.glob(os.path.join(c.find_project(self.repo).dir, "research", "brainstorm-*", "manifest.json")))[-1]
        return json.loads(read_text(m))

    def test_every_child_goes_to_the_local_profile_and_is_booked(self):
        comp, _, err = self.run_local()
        self.assertIsNone(err, err)
        self.assertGreaterEqual(comp.call_count, 5, "2 lenses, a deepen, a falsify and a crossbreed")
        self.assertFalse(os.path.exists(self.log), "no claude -p child ran")
        kids = self.manifest()["children"]
        self.assertEqual(len(kids), comp.call_count)
        self.assertEqual({k["model"] for k in kids}, {"jarvis"})

    def test_no_local_model_falls_back_to_haiku_and_says_so(self):
        _, said, err = self.run_local(up=False)
        self.assertIsNone(err, err)
        self.assertTrue(self.calls(), "claude -p answered")
        self.assertIn("Haiku", said)
        self.assertEqual({k["model"] for k in self.manifest()["children"]}, {"haiku"})

    def test_follow_ups_workers_timeout_paid_and_oracle(self):  # T-2952
        import argparse
        import concurrent.futures as cf
        from unittest import mock
        from sampo import cli, ideas, moonscale
        pools, asks = [], []
        real_pool, real_ask = cf.ThreadPoolExecutor, moonscale._ask

        def pool(n, *a, **k):
            pools.append(n)
            return real_pool(n, *a, **k)

        def ask(*a, **k):
            asks.append(k)
            return real_ask(*a, **k)
        with mock.patch.object(cf, "ThreadPoolExecutor", side_effect=pool), \
                mock.patch.object(moonscale, "_ask", side_effect=ask):
            self.run_local(up=False)
        self.assertEqual(pools[0], 2, "no local profile: one worker per lens, not one thread")
        self.assertTrue(all(k.get("timeout") == 30 for k in asks), "args.timeout reaches every child")
        self.assertGreater(max(k.get("paid", 0) for k in asks), 0, "the Haiku children so far count to the cap")
        # cmd_oracle takes --model local
        p = c.find_project(self.repo)
        b = mock.Mock(title="t", type="FIX")
        b.section.return_value = "text"
        b.id = "T-1"
        args = argparse.Namespace(id="T-1", model="local", timeout=7)
        with mock.patch.object(cli, "resolve", return_value=p), mock.patch.object(cli, "need_brief", return_value=b), \
                mock.patch.object(moonscale, "_ask", return_value=("## Examples\n- GIVEN a WHEN b THEN c", "jarvis")) as m, \
                mock.patch.object(ideas, "run_child") as rc, mock.patch.object(cli, "mutate") as mut, \
                mock.patch.object(cli, "out"):
            ideas.cmd_oracle(args)
        m.assert_called_once()
        self.assertEqual((m.call_args.kwargs["feature"], m.call_args.kwargs["timeout"]), ("oracle", 7))
        rc.assert_not_called()
        mut.assert_called_once()  # the section was saved after the routed call

    def test_file_map_hint_names_the_shard(self):  # T-2952
        self.assertNotIn("add a MASTER.md File map row", read_text(os.path.join(os.path.dirname(__file__), "..", "lib", "sampo", "cli.py")))

    def test_a_sensitive_project_fails_closed_with_no_loopback(self):
        comp, said, err = self.run_local(sensitive=True, host="llm.example.com")  # an own profile, but not loopback
        comp.assert_not_called()
        self.assertFalse(os.path.exists(self.log), "no cloud child")
        self.assertIn("sensitive", said + str(err))
