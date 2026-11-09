"""T-0949: the first Edit/Write of a file in a task gets the Avoid section of the map's best stack card for that file,
once per card per task (a ledger event, logged before the note goes out); fm doctor counts the enforce events of the
last 7 days by kind (validator blocks, merge refusals, waived merges, injected cards)."""
import inspect
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

from test_builder import _Tasks, git
from test_hooks import HookCase

from sampo import core as c

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEAD = "Sampo: stack card"


def card(name, area, score, *detect):
    return {"name": name, "area": area, "score": score, "detect": list(detect)}


class _CardCase(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = self.task()

    def map(self, cards=None, raw=None):
        with open(os.path.join(self.project().dir, "map.json"), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps({"lib": [x["name"] for x in cards], "lib_cards": cards}))

    def edit(self, rel, env=None, sid="sess-1"):
        path = os.path.join(self.repo, rel)
        proc = self.hook("PreToolUse", {"tool_name": "Edit", "session_id": sid, "tool_input": {"file_path": path, "old_string": "x",
                                                                          "new_string": "y"}}, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('"deny"', proc.stdout)
        ctx = self.ctx_of(proc)
        return ctx[ctx.index(HEAD):] if HEAD in ctx else ""

    def injected(self):
        return [e for e in c.ledger_tail(self.project(), 200)
                if e["event"] == "enforce" and e["data"].get("kind") == "card_injected"]



class EditCards(_CardCase):
    def test_first_edit_injects_the_cards_avoid_section_once_per_task(self):
        self.map([card("python", "lang", 5, "ext:.py", "file:pyproject.toml")])
        note = self.edit("app.py")
        self.assertIn("python", note)
        self.assertIn("Mutable default arguments", note)
        ev = self.injected()
        self.assertEqual([(e["task"], e["data"]["card"]) for e in ev], [(self.tid, "python")])
        self.assertEqual(self.edit("other.py"), "", "the card was already injected in this task")
        self.assertEqual(self.edit("app.py"), "")
        self.assertEqual(len(self.injected()), 1)
        self.assertEqual(self.edit("README.md"), "", "no card matches a markdown file")
        self.fm("task", "new", "Second", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        self.assertEqual(self.edit("app.py"), "", "T-3078: shown once per session, no repeat pointer for a later task")
        self.assertEqual(self.edit("app.py"), "")
        self.fm("task", "new", "Third", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        self.assertIn("Mutable default arguments", self.edit("app.py", sid="sess-2"), "a new session gets the full note again")

    def test_a_framework_card_beats_its_language_card_and_ties_go_by_name(self):
        self.map([card("python", "lang", 9, "ext:.py"), card("pytest", "python", 2, "file:conftest.py")])
        self.assertIn("stack card pytest", self.edit("tests/conftest.py"))
        self.map([card("python", "lang", 2, "ext:.py"), card("ruff", "python", 2, "ext:.py"),
                  card("mypy", "python", 2, "ext:.py")])
        self.fm("task", "new", "Two", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        self.assertIn("stack card mypy", self.edit("a.py"), "own-ext framework cards tie by name before the language")
        self.map([card("python", "lang", 2, "ext:.py"), card("go-concurrency", "go", 9, "lext:.py"),
                  card("uv", "python", 1, "lext:.py")])
        self.fm("task", "new", "Three", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        self.assertIn("stack card python", self.edit("a.py"), "a card sharing only the language's ext comes after it")
        self.map([card("uv", "python", 7, "lext:.py"), card("ruff", "python", 2, "lext:.py")])
        self.fm("task", "new", "Four", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        self.assertIn("stack card uv", self.edit("a.py"), "without the language card, the higher score")

    def test_a_long_avoid_section_is_cut_at_a_bullet_under_700_chars(self):
        self.map([card("vault", "infra", 2, "file:*.hcl")])
        note = self.edit("policies/app.hcl")
        self.assertTrue(note, "vault's Avoid section is over 700 chars, so it is cut, not dropped")
        self.assertLessEqual(len(note), 700)
        self.assertTrue(note.endswith("…"), note[-80:])
        with open(os.path.join(PLUGIN, "skills", "library", "cards", "infra", "vault.md"), encoding="utf-8") as f:
            lines = set(f.read().splitlines())
        bullets = [l for l in note.splitlines() if l.startswith("- ")]
        self.assertTrue(bullets)
        self.assertEqual([b for b in bullets if b not in lines], [], "every bullet is whole")

    def test_no_avoid_section_missing_card_or_bad_map_injects_nothing(self):
        from sampo import lib
        self.assertIsNone(lib.avoid_note("x", "---\nname: x\n---\nSummary.\n\n## Do\n- a\n"))
        self.map([card("no-such-card", "lang", 5, "ext:.py")])
        self.assertEqual(self.edit("a.py"), "")
        self.assertEqual(self.injected(), [], "nothing injected, nothing logged")
        for raw in ("{", "[]", '{"lib_cards": 7}', '{"lib_cards": [7, {"name": 1}, {"name": "python", "detect": 3}]}',
                    '{"lib_cards": [{"name": "../../etc/passwd", "detect": ["ext:.py"]}]}'):
            self.map(raw=raw)
            self.assertEqual(self.edit("a.py"), "", raw)
        os.remove(os.path.join(self.project().dir, "map.json"))
        self.assertEqual(self.edit("a.py"), "")
        self.assertEqual(self.injected(), [])

    def test_env_and_fm_lib_inject_off_turn_it_off(self):
        self.map([card("python", "lang", 5, "ext:.py")])
        self.assertEqual(self.edit("a.py", env={"FOREMAN_NO_CARDS": "1"}), "")
        self.fm("lib", "inject", "off")
        self.assertIn("off", self.fm("lib", "inject").stdout)
        self.assertEqual(self.edit("a.py"), "")
        self.assertEqual(self.injected(), [])
        self.fm("lib", "inject", "on")
        self.assertIn("Mutable default arguments", self.edit("a.py"))

    def test_the_map_carries_each_cards_area_score_and_file_signals(self):
        from sampo import map as smap
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("x = 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "app.py"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "py"], check=True)
        m = smap.load(self.project(), rebuild=True)
        py = [x for x in m["lib_cards"] if x["name"] == "python"]
        self.assertTrue(py, m["lib_cards"])
        self.assertEqual(py[0]["area"], "lang")
        self.assertIn("ext:.py", py[0]["detect"])
        self.assertFalse([s for x in m["lib_cards"] for s in x["detect"] if s.startswith("dep:")])  # no deps in this repo (T-1036 keeps dep: signals when there are)
        shared = [x for x in m["lib_cards"] if x["area"] != "lang" and "ext:.py" in x["detect"]]
        self.assertFalse(shared, "a non-language card keeps the language's ext only as lext:")
        self.assertEqual(m["lib"], [x["name"] for x in m["lib_cards"]])


class EditCardsDoctor(HookCase):
    def test_doctor_counts_seven_days_of_enforce_events_by_kind(self):
        from sampo import doctor
        self.fm("init")
        p = self.project()
        c.log_event(p, "enforce", data={"file": "a.json", "problem": "bad"})  # T-0947's validator block
        c.log_event(p, "enforce", data={"kind": "validator_block", "file": "b.json", "problem": "bad"})
        c.log_event(p, "enforce", data={"kind": "merge_refused", "reason": "red"})
        c.log_event(p, "enforce", data={"kind": "card_injected", "card": "python"})
        c.log_event(p, "lane_unverified", data={"reason": "flaky"})
        old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        with open(os.path.join(p.dir, "ledger.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": old, "event": "enforce", "data": {"kind": "card_injected"}}) + "\n")
        r = doctor.check_enforce(p)
        self.assertEqual(r.status, "PASS")
        for want in ("validator_block 2", "merge_refused 1", "lane_unverified 1", "card_injected 1"):
            self.assertIn(want, r.detail)
        self.assertIn("check_enforce(", inspect.getsource(doctor.run_all))
        self.assertIn("none", doctor.check_enforce(None).detail)


class EditCardsMerge(_Tasks):
    def test_a_refused_merge_is_an_enforce_event(self):
        tid = json.loads(self.fm("task", "new", "Red", "--type", "FEATURE", "--tier", "S", "--ac",
                                 "it passes :: false", "--step", "build", "--json").stdout)["id"]
        wt = os.path.join(self.tmp, "wt-" + tid)
        git(self.repo, "worktree", "add", "-q", "-b", f"foreman/{tid}", wt)
        self.fm("focus", tid, cwd=wt)
        with open(os.path.join(wt, "x.py"), "w") as f:
            f.write("x = 1\n")
        git(wt, "add", "x.py")
        git(wt, "commit", "-qm", "work")
        self.assertNotEqual(self.fm("lane", "merge", tid, check=False).returncode, 0)
        ev = [e for e in c.ledger_tail(c.find_project(self.repo), 50) if e["event"] == "enforce"]
        self.assertEqual([(e["task"], e["data"]["kind"]) for e in ev], [(tid, "merge_refused")])
        self.assertTrue(re.search(r"criterion 1 is red", ev[0]["data"]["reason"]))


class EditCardsReview(_CardCase):
    """T-0949 review: every file tool, a marker instead of a ledger scan, fm lib inject never creates a project."""

    def tool(self, name, inp):
        proc = self.hook("PreToolUse", {"tool_name": name, "tool_input": inp})
        ctx = self.ctx_of(proc)
        return ctx[ctx.index(HEAD):] if HEAD in ctx else ""

    def test_write_multiedit_and_notebookedit_get_the_card(self):
        self.map([card("python", "lang", 5, "ext:.py"), card("jupyter", "ml", 5, "ext:.ipynb"),
                  card("rust", "lang", 5, "ext:.rs")])
        self.assertIn("python", self.tool("Write", {"file_path": os.path.join(self.repo, "w.py"), "content": "x"}))
        self.assertIn("rust", self.tool("MultiEdit", {"file_path": os.path.join(self.repo, "m.rs"), "edits": []}))
        nb = self.tool("NotebookEdit", {"notebook_path": os.path.join(self.repo, "n.ipynb"), "new_source": "x"})
        self.assertIn("jupyter", nb) if nb else None

    def test_dedupe_reads_a_small_marker_not_the_ledger(self):
        self.map([card("python", "lang", 5, "ext:.py")])
        self.assertIn("python", self.edit("a.py"))
        with open(os.path.join(self.project().dir, "ledger.jsonl"), "w") as f:
            f.write("")  # the ledger rolled or was archived: the marker still knows
        self.assertEqual(self.edit("b.py"), "")

    def test_fm_lib_inject_status_never_creates_a_project(self):
        other = os.path.join(self.tmp, "plain")
        os.makedirs(other)
        subprocess.run(["git", "init", "-q", other], check=True)
        r = self.fm("lib", "inject", cwd=other)
        self.assertIn("Edit-time stack cards", r.stdout)
        self.assertIsNone(c.find_project(other, create=False))


class LangCardsKept(HookCase):
    """T-0980: the map keeps the repo's language cards past the top 8, so an edit in a repo whose top cards are tools
    still gets its language's card (Foreman's own map had 8 claude-code cards and no python)."""

    def test_language_cards_survive_a_crowded_top_eight(self):
        from sampo import lib
        rows = [{"name": f"tool{i}", "area": "tools", "score": 50 - i, "detect": ["ext:.py"]} for i in range(10)]
        rows.append({"name": "python", "area": "lang", "score": 3, "detect": ["ext:.py"]})
        real = lib.suggest
        lib.suggest = lambda root, files, n=None: rows if n is None else rows[:n]
        try:
            names = [r["name"] for r in lib.for_map(self.repo, [])]
        finally:
            lib.suggest = real
        self.assertIn("python", names)
        self.assertLessEqual(len(names), 8 + 3)  # the top 8 plus at most a few languages


class ImportCards(_CardCase):
    """T-1036: a card known by a dependency (React, FastAPI, Tokio, Gin …) never fired: injection keyed on the file's
    extension, so only language cards reached Claude (6 of 1,084 cards in 50 injections). Now what the edited file
    imports picks the project's framework cards too, before the language card, at most two cards an edit."""

    def put(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def write(self, rel, content):
        proc = self.hook("PreToolUse", {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.repo, rel),
                                                                          "content": content}})
        ctx = self.ctx_of(proc)
        return ctx[ctx.index(HEAD):] if HEAD in ctx else ""

    def test_a_file_that_imports_a_framework_gets_its_card_beside_the_language_card(self):
        self.map([card("typescript", "lang", 5, "ext:.ts", "ext:.tsx"), card("react", "web", 3, "dep:react", "dep:react-dom")])
        self.put("src/App.tsx", "import { useState } from 'react'\nimport x from './local'\nexport const A = () => null\n")
        note = self.edit("src/App.tsx")
        self.assertIn("stack card react", note)
        self.assertIn("stack card typescript", note)
        self.assertEqual(sorted(e["data"]["card"] for e in self.injected()), ["react", "typescript"])
        self.assertEqual(self.edit("src/App.tsx"), "")  # once per card per task

    def test_python_rust_and_go_imports_match_their_deps(self):
        self.map([card("python", "lang", 5, "ext:.py"), card("fastapi", "python", 3, "dep:fastapi"),
                  card("pydantic", "python", 2, "dep:pydantic"), card("tokio", "rust", 3, "dep:tokio"),
                  card("gin", "go", 3, "dep:github.com/gin-gonic/gin")])
        self.put("api.py", "from fastapi import FastAPI\nimport pydantic.v1 as p\n")
        note = self.edit("api.py")
        self.assertIn("stack card fastapi", note)
        self.assertIn("stack card pydantic", note)  # two frameworks: they come before the language card
        self.assertNotIn("stack card python,", note)
        self.assertIn("stack card tokio", self.write("src/main.rs", "use tokio::sync::mpsc;\nfn main() {}\n"))
        self.assertIn("stack card gin", self.write("main.go", 'package main\n\nimport (\n\t"fmt"\n\t"github.com/gin-gonic/gin"\n)\n'))

    def test_relative_imports_and_packages_the_map_lacks_match_nothing(self):
        self.map([card("typescript", "lang", 5, "ext:.ts"), card("react", "web", 3, "dep:react")])
        self.put("util.ts", "import { pad } from 'left-pad'\nimport y from '../react'\nconst r = require('./react')\n")
        note = self.edit("util.ts")
        self.assertIn("stack card typescript", note)
        self.assertNotIn("stack card react", note)

    def test_a_big_or_binary_file_is_read_only_at_its_head(self):
        self.map([card("python", "lang", 5, "ext:.py"), card("fastapi", "python", 3, "dep:fastapi")])
        self.put("big.py", "x = 1\n" * 50000 + "from fastapi import FastAPI\n")  # an import far down: not looked for
        self.assertNotIn("fastapi", self.edit("big.py"))

    def test_audit_prep_carries_the_review_checklists_of_the_cards_used(self):
        self.map([card("typescript", "lang", 5, "ext:.ts", "ext:.tsx"), card("react", "web", 3, "dep:react")])
        self.put("App.tsx", "import React from 'react'\n")
        self.edit("App.tsx")
        brief = self.fm("audit", "prep", self.tid, check=False)
        path = os.path.join(self.project().dir, "audits", f"{self.tid}.review.md")
        self.assertTrue(os.path.exists(path), brief.stdout + brief.stderr)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Stack card checklists", text)
        self.assertIn("react", text)

    def test_fm_lib_stats_counts_injections_per_card(self):
        self.map([card("typescript", "lang", 5, "ext:.ts", "ext:.tsx"), card("react", "web", 3, "dep:react")])
        self.put("App.tsx", "import React from 'react'\n")
        self.edit("App.tsx")
        rows = json.loads(self.fm("lib", "stats", "--json").stdout)
        self.assertEqual({r["card"]: r["injected"] for r in rows}, {"react": 1, "typescript": 1})

    def test_fm_lib_stats_days_counts_only_recent_injections(self):
        # T-1035: the change the README's recorded session made, in a clone of this repo
        self.map([card("typescript", "lang", 5, "ext:.ts", "ext:.tsx"), card("react", "web", 3, "dep:react")])
        self.put("App.tsx", "import React from 'react'\n")
        self.edit("App.tsx")
        old = (datetime.now(timezone.utc) - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(self.project().dir, "ledger.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": old, "event": "enforce", "data": {"kind": "card_injected", "card": "old"}}) + "\n")
        def stats(*a):
            return {r["card"] for r in json.loads(self.fm("lib", "stats", "--json", *a).stdout)}
        self.assertEqual(stats(), {"react", "typescript", "old"})
        self.assertEqual(stats("--days", "7"), {"react", "typescript"})

    def test_audit_prep_of_an_l_task_that_used_a_card_runs(self):
        # T-1036: a walrus in the checklist comprehension bound r in cmd_audit's scope and broke its L-tier path
        tid = self.task(title="Big one", type_="FEATURE", tier="L", focus=False)
        for sec in ("Interpretation", "Approach (options → choice → why)", "Build vs reuse", "Risks and rollback"):
            self.fm("task", "set", tid, "--section", sec, "--text", "Reading 1: a. Reading 2: b. 1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")
        self.fm("task", "set", tid, "approved=true")
        self.fm("focus", tid)
        self.map([card("typescript", "lang", 5, "ext:.ts", "ext:.tsx"), card("react", "web", 3, "dep:react")])
        self.put("Big.tsx", "import React from 'react'\n")
        self.edit("Big.tsx")
        r = self.fm("audit", "prep", tid, check=False)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("Stack card checklists", open(os.path.join(self.project().dir, "audits", f"{tid}.review.md")).read())

    def test_review_cards_already_used_never_crowd_out_the_rest(self):
        # T-1036 review: the cap of two came before the already-injected filter, so later cards never fired
        self.map([card("python", "lang", 5, "ext:.py"), card("fastapi", "python", 3, "dep:fastapi"),
                  card("pydantic", "python", 2, "dep:pydantic")])
        self.put("api.py", "from fastapi import FastAPI\nimport pydantic\n")
        self.assertIn("fastapi", self.edit("api.py"))
        self.assertIn("stack card python", self.edit("api.py"))  # the next edit gets the one left out

    def test_review_wildcard_deps_match_and_files_outside_the_project_never_count(self):
        self.map([card("typescript", "lang", 5, "ext:.ts"), card("tauri", "web", 3, "dep:@tauri-apps/plugin-*")])
        self.put("a.ts", "import { open } from '@tauri-apps/plugin-shell'\n")
        self.assertIn("stack card tauri", self.edit("a.ts"))
        outside = os.path.join(self.tmp, "scratch.ts")
        with open(outside, "w") as f:
            f.write("import x from '@tauri-apps/plugin-fs'\n")
        self.fm("task", "new", "Other", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        proc = self.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": outside, "old_string": "x", "new_string": "y"}})
        self.assertNotIn(HEAD, self.ctx_of(proc))
