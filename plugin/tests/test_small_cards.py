"""T-2909: small-model cards part 1 — the write-time admit, the grid read thin-first by grow on a fake local profile
with two workers, and coverage by cells. Every model call is mocked."""
# map-links: plugin/lib/sampo/smallcards.py plugin/lib/sampo/moonscale.py plugin/lib/sampo/playbooks.py
import json
import os
import threading
import unittest
from unittest import mock

from helpers import ForemanTestCase, headless_chrome


def good(**kw):
    card = {"title": "Fix a failing pytest test in a Python repo",
            "when": "A pytest test that passed before now fails and you must find out why.",
            "steps": [{"do": "Run only the failing test and read the last error line.", "cmd": "python3 -m pytest -x -q"},
                      {"do": "Find the file the traceback names last and open it.", "cmd": "grep -n def tests/test_a.py"},
                      {"do": "Change one thing, then run the same test again.", "cmd": "python3 -m pytest -x -q"},
                      {"do": "Run the whole suite and read the summary line.", "cmd": "sampo check --affected"}],
            "example": "Test test_sum fails with 3 != 4. Open sum.py, see range(3), change to range(4), rerun: passes.",
            "check": "The failing test passes and the suite summary shows no failures.",
            "traps": ["Editing the test so it passes instead of the code.", "Changing two things before rerunning."]}
    card.update(kw)
    return card


def reply(n):
    return "Cards:\n```json\n" + "\n".join(json.dumps(good(title=f"Card number {i}")) for i in range(n)) + "\n```\n"


class _Cards(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        from sampo import core as c
        from sampo import smallcards
        self.sc = smallcards
        self.p = c.find_project(self.repo)
        patch = mock.patch.dict(os.environ, {"SAMPO_CARDS": os.path.join(self.tmp, "cards")})
        patch.start()
        self.addCleanup(patch.stop)


class Admit(_Cards):
    def test_a_complete_card_is_kept(self):
        self.assertEqual(self.sc.admit(good()), (True, ""))

    def test_a_small_models_shapes_are_parsed_leniently_and_checked_strictly(self):  # T-2984: 400 calls, 0 kept
        steps = good()["steps"]
        loose = good(traps="Models may forget the dialect flag. They might not stop on the first error.",
                     steps=[f"{s['do']} `{s['cmd']}`" if s["cmd"] else s["do"] for s in steps])
        self.assertEqual(self.sc.admit(loose), (True, ""))
        self.assertEqual(len(self.sc._clean(loose)["traps"]), 2)
        ok, why = self.sc.admit(good(traps="Too short."))  # the strict checks still hold on the parsed parts
        self.assertFalse(ok)
        self.assertTrue(why.startswith("traps"), why)

    def test_each_missing_part_is_rejected_with_its_reason(self):
        cases = {
            "steps": good(steps=[]),
            "steps ": good(steps=good()["steps"][:2]),
            "example": good(example=""),
            "check": good(check=""),
            "traps": good(traps=[]),
            "when": good(when=""),
            "command": good(steps=[{"do": "Run it and read the output.", "cmd": ""}] * 4),
        }
        for want, card in cases.items():
            with self.subTest(want):
                ok, why = self.sc.admit(card)
                self.assertFalse(ok)
                self.assertTrue(why.startswith(want.strip() + ":"), why)

    def test_a_command_that_does_not_exist_is_rejected(self):
        for cmd in ("frobnicate --all", "sampo nosuchverb now", "python3 -m pytest; rm -rf /"):
            with self.subTest(cmd):
                steps = [dict(s) for s in good()["steps"]]
                steps[1]["cmd"] = cmd
                ok, why = self.sc.admit(good(steps=steps))
                self.assertFalse(ok)
                self.assertTrue(why.startswith("command:"), why)

    def test_a_quoted_pattern_is_no_redirect_or_pipe_but_a_real_one_still_is(self):
        # live: 20 of 25 local UI refusals were "command", while 119 kept UI cards grep '<tag' and never close the >
        def ok(cmd):
            steps = [dict(s) for s in good()["steps"]]
            steps[1]["cmd"] = cmd
            return self.sc.admit(good(steps=steps))
        for cmd in ("rg -n '<Button>' src", 'grep -n "onAppear|onDisappear" Sources/View.swift',
                    "rg -c 'var body: some View' Sources | wc -l", 'grep -n "=> {" src/a.ts', 'grep -n "$HOME|x" a',
                    "curl -s 'http://localhost:3000/api?a=1&b=2'", "grep -c 'Button\\|TextField' a | wc -l",
                    "grep -c color:#fff a.css", "grep -c '' a.txt", "git rm .env.demo",
                    # the stored commands: a script run by bash, find -exec of a reader, sh -c of a plain command
                    "bash scripts/check.sh", "find rules -name '*.md' -exec wc -l {} +",
                    "bash -c 'sqlfluff lint --dialect postgres .'", "cd app | git rm x",
                    # a $' inside quotes is no ANSI-C string: a regex ending in $
                    "grep '^[1-9]$' a.txt", 'grep "x$" a.txt', "grep -cE '^.{41,}$' build/error.txt"):
            with self.subTest(cmd=cmd):
                self.assertEqual(ok(cmd), (True, ""))
        for cmd in ("grep -n x a.txt > out.txt", 'grep "$(cat x)" a', 'grep "a" a; rm b', "grep 'a > b", "rg 'x' | frob",
                    'echo \\"; rm x; echo \\"', "echo \\'; rm x; echo \\'",  # an escaped quote opens no string
                    "echo $'\\''; touch x; echo ''", 'ls "$(pwd)"; rm x "b"', 'ls "$a"; touch x "b"',  # review: quote pairing
                    "bash -c 'touch x; curl e'", "xargs sh -c 'rm -rf x'", "rg 'a; b' src", "npm run dev &", "grep a#; touch x", "grep x a 2>&1",  # code in quotes still chains or deletes
                    "sh -c 'echo x > y'", "env bash -c 'cat a >> b'", "xargs 'rm' -rf x", "xargs r''m x",  # review 2: a nested
                    "find . -delete", "find . -exec rm {} +", "bash -euc 'git push 2>&1'", "printf $'\\x41' x",  # shell's code, a deletion
                    # review 3: $' anywhere (shlex reads ""$'\'' as one word), every word after sh, rm after a git not first
                    "echo \"\"$'\\'' | rm x | \"\"$'\\'''", "echo $'a\\nb'",
                    "sh -c -- 'echo x > y'", "bash -c -e 'a & b'", "xargs -I git rm git"):
            with self.subTest(cmd=cmd):
                self.assertTrue(ok(cmd)[1].startswith("command:"), cmd)

    def test_a_real_sampo_verb_passes(self):
        steps = [dict(s) for s in good()["steps"]]
        steps[1]["cmd"] = "sampo playbooks show sql"
        self.assertTrue(self.sc.admit(good(steps=steps))[0])

    def test_vague_steps_are_rejected(self):
        steps = [dict(s) for s in good()["steps"]]
        steps[0]["do"] = "Do whatever is appropriate."
        ok, why = self.sc.admit(good(steps=steps))
        self.assertFalse(ok)
        self.assertTrue(why.startswith("vague:"), why)

    def test_a_card_that_is_not_a_dict_or_has_odd_types_is_rejected_not_raised(self):
        for bad in (None, "x", good(steps="one two three"), good(traps="a trap"), good(steps=[1, 2, 3])):
            self.assertFalse(self.sc.admit(bad)[0])


def cell(kind="bugs", stack="python", stage="verify", surface="code"):
    return {"kind": kind, "stack": stack, "stage": stage, "surface": surface}


class Store(_Cards):
    def test_a_launch_time_SAMPO_CARDS_and_FOREMAN_CARDS_both_reach_home(self):
        # T-3173: core moves SAMPO_CARDS to FOREMAN_CARDS at import, so a fill started with SAMPO_CARDS wrote elsewhere
        import subprocess
        import sys
        lib = os.path.dirname(os.path.dirname(self.sc.__file__))
        code = f"import sys; sys.path.insert(0, {lib!r}); from sampo import smallcards; print(smallcards.home())"
        for name in ("SAMPO_CARDS", "FOREMAN_CARDS"):
            env = {k: v for k, v in os.environ.items() if not k.endswith("_CARDS")}
            env[name] = os.path.join(self.tmp, "c")
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), os.path.join(self.tmp, "c"), name + r.stderr[-200:])

    def test_add_stores_only_admitted_cards_and_skips_a_repeat(self):
        out = self.sc.add(self.p, [good(), good(example=""), good()], cell(), "gpu-qwen9", 1)
        self.assertEqual([v for _, v, _ in out], ["kept", "rejected", "dup"])
        rows = self.sc.rows()
        self.assertEqual([r["id"] for r in rows], ["K-000001"])
        self.assertEqual((rows[0]["model"], rows[0]["cell"]["stack"]), ("gpu-qwen9", "python"))

    def test_text_is_stored_without_secrets_or_newlines_in_titles(self):
        key = "sk-ant-api03-" + "a" * 40
        self.sc.add(self.p, [good(title="Line one\nline two", example=f"Set the key {key} in the shell, then run the tests again.")], cell(), "m", 1)
        r = self.sc.rows()[0]
        self.assertNotIn("\n", r["title"])
        self.assertNotIn(key, json.dumps(r))


class Grow(_Cards):
    def test_grow_fills_thin_cells_first_with_two_workers(self):
        ms, gate, seen = self.sc.ms, threading.Barrier(2, timeout=5), []

        def fake(p, system, prompt, cheap=True, paid=0, feature=None):
            gate.wait()  # raises unless the second call is in flight
            seen.append(prompt)
            return reply(3), "gpu-qwen9"
        small = {"KINDS": ("bugs", "docs"), "STACKS": ("python", "rust"), "STAGES": ("verify",), "SURFACES": ("code",)}
        with mock.patch.multiple(self.sc, **small):
            self.sc.add(self.p, [good(title="Seed card here")], cell("bugs", "python", "verify", "code"), "m", 1)
            with mock.patch.object(ms, "_ask", side_effect=fake):
                r = self.sc.grow(self.p, target=7, batch=3, workers=2)
            cov = self.sc.coverage()
        self.assertEqual((r["calls"], r["workers"], r["stopped"]), (2, 2, "target"))
        self.assertEqual(r["kept"], 6, "3 cards in each of the two empty cells")
        self.assertEqual(len(seen), 2)
        self.assertFalse(any("kind=bugs stack=python" in x for x in seen), "the filled cell was not asked first")
        self.assertEqual(cov["cells"]["bugs/python/verify/code"], 1)
        self.assertEqual(cov["total"], 7)

    def test_grow_stops_after_repeated_empty_replies(self):
        with mock.patch.object(self.sc.ms, "_ask", return_value=("no cards here", "m")):
            r = self.sc.grow(self.p, target=50, batch=3, workers=1)
        self.assertEqual((r["stopped"], r["added"]), ("failed", 0))

    def test_a_refused_card_is_sent_back_once_with_its_reason_and_kept_if_it_passes(self):  # T-2985
        steps = [dict(s) for s in good()["steps"]]
        steps[1]["cmd"] = "grep -n def tests/test_a.py && echo done"
        chained = good(title="Chained command card", steps=steps)
        hopeless = good(title="Hopeless card here", example="")
        fixed = good(title="Chained command card")
        calls = []

        def fake(p, system, prompt, cheap=True, paid=0, feature=None):
            calls.append(prompt)
            cards = [chained, hopeless] if len(calls) == 1 else [fixed, hopeless]
            return "\n".join(json.dumps(x) for x in cards), "gpu-qwen9"
        with mock.patch.object(self.sc.ms, "_ask", side_effect=fake):
            r = self.sc.grow(self.p, target=1, batch=2, workers=1)
        self.assertEqual(len(calls), 2, "one repair call, never a loop")
        self.assertIn("command: step 2", calls[1])
        self.assertIn("example: 8 words at least", calls[1])
        self.assertEqual([x["title"] for x in self.sc.rows()], ["Chained command card"])
        self.assertEqual((r["kept"], r["rejected"], r["repair_asked"], r["repair_kept"]), (1, 1, 2, 1))
        self.assertIn("repaired 1 of 2", self.sc.grow_line(r))

    def test_two_repairs_run_at_once_in_the_pool_not_one_by_one(self):
        # live: a local writer refused half its cards, and each repair call ran alone while the workers sat idle
        steps = [dict(s) for s in good()["steps"]]
        steps[1]["cmd"] = "grep -n def tests/test_a.py && echo done"
        gate, seen = threading.Barrier(2, timeout=5), []

        def fake(p, system, prompt, cheap=True, paid=0, feature=None):
            seen.append(prompt)
            if prompt.startswith("These cards were refused"):
                gate.wait()  # raises unless the other repair is in flight
                return json.dumps(good(title="Chained command card")), "gpu-qwen9"
            return "\n".join(json.dumps(x) for x in (good(title=f"Fine card {len(seen)}"), good(title="Chained command card", steps=steps))), "gpu-qwen9"
        with mock.patch.object(self.sc.ms, "_ask", side_effect=fake):
            r = self.sc.grow(self.p, target=50, batch=2, workers=2, max_calls=2)
        self.assertEqual((r["calls"], r["repair_asked"], r["repair_kept"], r["kept"], r["stopped"]), (2, 2, 2, 4, "max_calls"))
        self.assertEqual(len(self.sc.rows()), 4)

    def test_a_repair_counts_the_rounds_paid_calls_before_it(self):  # review 3: a stale paid passed the run's cap
        steps = [dict(s) for s in good()["steps"]]
        steps[1]["cmd"] = "grep -n def tests/test_a.py && echo done"
        paid = []

        def fake(p, system, prompt, cheap=True, paid=0, feature=None):
            if prompt.startswith("These cards were refused"):
                return json.dumps(good(title="Chained command card")), "haiku"
            return json.dumps(good(title="Chained command card", steps=steps)), "haiku"
        with mock.patch.object(self.sc.ms, "_ask", side_effect=lambda *a, **kw: paid.append((a[2][:9], kw["paid"])) or fake(*a, **kw)):
            self.sc.grow(self.p, target=50, batch=1, workers=2, max_calls=2)
        repairs = [n for head, n in paid if head == "These car"]
        self.assertEqual(len(repairs), 2)
        self.assertGreaterEqual(min(repairs), 2, f"each repair counts both paid calls already asked: {paid}")

    def test_an_interrupted_grow_returns_without_waiting_on_a_running_call(self):  # review 3
        import time
        release, first = threading.Event(), self.sc._pick(__import__("collections").Counter(), 2, 0)[0]
        line = "kind={kind} stack={stack} stage={stage} surface={surface}".format(**first)

        def fake(p, system, prompt, cheap=True, paid=0, feature=None):
            if line not in prompt:
                release.wait(20)  # the other worker's call is still running when the reader is interrupted
            return reply(1), "m"
        t0 = time.monotonic()
        try:
            with mock.patch.object(self.sc.ms, "_ask", side_effect=fake), \
                    mock.patch.object(self.sc, "add", side_effect=RuntimeError("interrupted")):
                with self.assertRaises(RuntimeError):
                    self.sc.grow(self.p, target=50, batch=1, workers=2, max_calls=2)
            self.assertLess(time.monotonic() - t0, 10, "shutdown waited on the running call")
        finally:
            release.set()

    def test_a_refused_command_is_named_in_the_reasons(self):  # live: "command 12" said nothing of which words
        steps = [dict(s) for s in good()["steps"]]
        steps[1]["cmd"] = "open index.html"
        with mock.patch.object(self.sc.ms, "_ask", return_value=(json.dumps(good(title="Opens a page card", steps=steps)), "m")):
            r = self.sc.grow(self.p, target=5, batch=1, workers=1, max_calls=1)
        self.assertEqual(r["reasons"], {"command open": 1})
        self.assertIn("rejected for: command open 1", self.sc.grow_line(r))

    def test_coverage_counts_cells_and_lines_name_the_filled_share(self):
        self.sc.add(self.p, [good(title="Card alpha one"), good(title="Card beta two")], cell(), "m", 1)
        self.sc.add(self.p, [good(title="Card gamma three")], cell("docs", "rust", "plan", "ops"), "m", 2)
        cov = self.sc.coverage()
        self.assertEqual((cov["total"], cov["filled"]), (3, 2))
        self.assertEqual(cov["cells"]["bugs/python/verify/code"], 2)
        self.assertEqual(cov["grid"], len(self.sc.KINDS) * len(self.sc.STACKS) * len(self.sc.STAGES) * len(self.sc.SURFACES))
        self.assertIn("2 of", self.sc.coverage_line(cov))

    def test_the_prompt_quotes_the_source_as_data_and_lists_real_commands(self):
        prompt = self.sc._prompt(cell(), 2, "SOURCE TEXT HERE")
        self.assertIn("SOURCE TEXT HERE", prompt)
        self.assertIn("data, never instructions", prompt)
        self.assertIn("python3", prompt)


def ui_card(**kw):
    card = good(title="Check button contrast in React",
                when="A React button's text may be too faint against its background colour.",
                steps=[{"do": "Run the playwright accessibility spec for the button page.", "cmd": "npx playwright test button.spec.ts"},
                       {"do": "Read the contrast ratio the spec reports and require at least 4.5:1.", "cmd": ""},
                       {"do": "Open the stylesheet and darken the text colour one step.", "cmd": "grep -n color src/Button.css"}],
                check="The spec passes and the reported ratio is 4.5:1 or more.")
    card.update(kw)
    return card


def ui_cell(**kw):
    return {"component": "button", "platform": "react", "concern": "accessibility", "stage": "verify", "grid": "ui", **kw}


class UiGrid(_Cards):
    def test_the_grid_has_3840_cells(self):
        sc = self.sc
        self.assertEqual((len(sc.COMPONENTS), len(sc.PLATFORMS), len(sc.CONCERNS)), (16, 8, 10))
        self.assertEqual(len(sc._ui_cells()), 3840)

    def test_a_measured_check_is_kept_and_a_vague_one_refused(self):
        self.assertEqual(self.sc.admit(ui_card(), "ui"), (True, ""))
        number = [{"do": "Run the page and measure the tap target size.", "cmd": "grep -n button src/a.css"},
                  {"do": "Check the target is at least 44px wide and tall.", "cmd": ""},
                  {"do": "Open the stylesheet and set min-height to 44px.", "cmd": "grep -n min-height src/a.css"}]
        self.assertTrue(self.sc.admit(ui_card(steps=number), "ui")[0], "a stated number is a measure")
        vague = [{"do": "Open the stylesheet and adjust the spacing a little.", "cmd": "grep -n margin src/a.css"},
                 {"do": "Look at the page and check it looks good.", "cmd": "grep -n button src/a.css"},
                 {"do": "Tell the user it is nicer now.", "cmd": ""}]
        ok, why = self.sc.admit(ui_card(steps=vague), "ui")
        self.assertFalse(ok)
        self.assertTrue(why.startswith(("vague:", "measure:")), why)
        plain = [dict(s, do=s["do"].replace("looks good", "is fine").replace("nicer", "different")) for s in vague]  # no vague phrase, still no measure
        ok, why = self.sc.admit(ui_card(steps=plain), "ui")
        self.assertTrue(not ok and why.startswith("measure:"), why)
        self.assertTrue(self.sc.admit(good())[0], "the general grid is unchanged")

    def test_pnpm_and_yarn_count_as_commands_in_the_ui_grid_only(self):
        steps = ui_card()["steps"]
        steps[2] = {"do": "Run the lint rule for the button's aria labels.", "cmd": "pnpm exec eslint src"}
        self.assertTrue(self.sc.admit(ui_card(steps=steps), "ui")[0])
        self.assertFalse(self.sc.admit(ui_card(steps=steps))[0])

    def test_flutter_and_swiftui_cards_name_their_own_test_runners(self):
        # the live store: Flutter and SwiftUI cards fell back to rg and grep (half the yield of vue), as their runners weren't commands
        plain = [{"do": "Find the widget file for the settings button.", "cmd": "grep -n Button lib/settings.dart"},
                 {"do": "Open the widget file and read the button's semantics label.", "cmd": "grep -n Semantics lib/settings.dart"}]
        for cmd in ("flutter test --update-goldens test/button_golden_test.dart", "dart test test/button_test.dart",
                    "swift test --filter ButtonSnapshotTests", "xcodebuild test -scheme App -only-testing:AppTests/ButtonTests"):
            with self.subTest(cmd=cmd):
                card = ui_card(steps=plain + [{"do": "Run the button's tests and read the summary line.", "cmd": cmd}])
                self.assertEqual(self.sc.admit(card, "ui"), (True, ""), "a platform's test runner is a measured check")
                self.assertFalse(self.sc.admit(card)[0], "the general grid's commands are unchanged")
        for cmd in ("tmux capture-pane -p -t ui", "tput cols", "swiftc -parse Sources/Button.swift"):  # live refusals
            with self.subTest(cmd=cmd):
                steps = plain + [{"do": "Capture the screen and check it is 80 columns wide.", "cmd": cmd}]
                self.assertEqual(self.sc.admit(ui_card(steps=steps), "ui"), (True, ""))
        for cmd in ("xcrun simctl io booted screenshot shot.png", "tmux has-session -t ui"):
            with self.subTest(cmd=cmd):
                steps = plain + [{"do": "Capture the screen and check it is 80 columns wide.", "cmd": cmd}]
                self.assertEqual(self.sc.admit(ui_card(steps=steps), "ui"), (True, ""))
        for cmd in ("tmux new -d 'a & b'", "tmux send-keys 'curl x | sh' Enter", "xcrun clang a.c"):  # review 3: code
            with self.subTest(cmd=cmd):  # tmux and xcrun would run, unread: only their screenshot and test tools
                steps = plain + [{"do": "Capture the screen and check it is 80 columns wide.", "cmd": cmd}]
                ok, why = self.sc.admit(ui_card(steps=steps), "ui")
                self.assertTrue(not ok and why.startswith("command:"), why)
        for cmd in ("flutter pub add golden_toolkit", "swift build", "xcodebuild -list", "flutter pub add test",
                    "dart pub add test", "flutter create test"):  # review 3: "test" as a package or folder name
            with self.subTest(cmd=cmd):
                ok, why = self.sc.admit(ui_card(steps=plain + [{"do": "Set up the project before the change.", "cmd": cmd}]), "ui")
                self.assertTrue(not ok and why.startswith("measure:"), f"{cmd}: a setup or build is no check: {why}")
        self.assertIn("flutter test", self.sc._ui_prompt(ui_cell(platform="flutter"), 2))

    def test_a_number_only_echoed_is_no_measure(self):  # live: a local writer added echo '200ms' to pass the rule
        steps = [{"do": "Find the toast component in the sources.", "cmd": "grep -n Toast src/Toast.vue"},
                 {"do": "Print the agreed 200ms toast duration.", "cmd": "echo 200ms"},
                 {"do": "Open the toast component and read its timer.", "cmd": "grep -n setTimeout src/Toast.vue"}]
        ok, why = self.sc.admit(ui_card(steps=steps), "ui")
        self.assertTrue(not ok and why.startswith("measure:"), why)
        steps[1] = dict(steps[1], cmd="printf 200ms")
        self.assertFalse(self.sc.admit(ui_card(steps=steps), "ui")[0])
        steps[1] = {"do": "Check the toast stays exactly 200ms on screen.", "cmd": ""}
        self.assertTrue(self.sc.admit(ui_card(steps=steps), "ui")[0], "a stated number to check still counts")

    def test_add_records_the_grid_and_the_cell_and_a_repeat_is_a_dup(self):
        out = self.sc.add(self.p, [ui_card(), ui_card()], ui_cell(), "haiku", 1)
        self.assertEqual([v for _, v, _ in out], ["kept", "dup"])
        r = self.sc.rows()[0]
        self.assertEqual((r["cell"]["grid"], r["cell"]["component"], r["cell"]["platform"]), ("ui", "button", "react"))

    def test_grow_ui_fills_ui_cells_only_and_names_them_in_the_prompt(self):
        seen = []

        def fake(p, system, prompt, cheap=True, paid=0, feature=None, writer=None):
            seen.append((prompt, writer))
            return "\n".join(json.dumps(ui_card(title=f"Button contrast card {len(seen)} {i}")) for i in range(2)), "haiku"
        self.sc.add(self.p, [good(title="A general card here")], cell(), "m", 1)
        with mock.patch.object(self.sc.ms, "_ask", side_effect=fake):
            r = self.sc.grow(self.p, target=4, batch=2, workers=1, grid="ui", writer="haiku")
        self.assertEqual((r["kept"], r["stopped"]), (4, "target"))
        self.assertTrue(all(w == "haiku" for _, w in seen))
        for word in ("component=", "platform=", "concern=", "stage="):
            self.assertIn(word, seen[0][0])
        cov = self.sc.coverage("ui")
        self.assertEqual((cov["total"], cov["grid"]), (4, 3840))
        self.assertEqual(self.sc.coverage()["total"], 1, "the general grid counts its own cards only")
        self.assertIn("UI cards: 4 in 2 of 3840 cells", self.sc.coverage_text(self.sc.coverage()))
        self.assertIn("4 in 2 of 3840", self.sc.coverage_line(cov))

    def test_writer_routing_in_ask_never_calls_a_real_model(self):
        ms = self.sc.ms
        with mock.patch.object(ms, "_locals", return_value=[{"name": "loc"}]), \
                mock.patch.object(ms.models, "complete", return_value={"text": "x"}) as comp, \
                mock.patch.object(ms.ideas, "run_child", return_value="paid") as run, \
                mock.patch.object(ms.budget, "check"):
            self.assertEqual(ms._ask(self.p, "s", "p", writer="sonnet"), ("paid", "sonnet"))
            self.assertEqual(ms._ask(self.p, "s", "p", writer="haiku")[1], "haiku")
            comp.assert_not_called()
            self.assertEqual(ms._ask(self.p, "s", "p", writer="local")[1], "loc")
        with mock.patch.object(ms, "_locals", return_value=[]), self.assertRaises(ValueError):
            ms._ask(self.p, "s", "p", writer="local")
        with self.assertRaises(ValueError):
            ms._ask(self.p, "s", "p", writer="gpt")


# T-3102: a UI card's live example, written by a local model and kept only when its render check passes
SAFE = ('<div class="row"><button class="primary" type="button">Save draft</button>'
        '<a href="#">Cancel</a></div>')


def gate(name, ok, got="", bar=""):
    return {"gate": name, "ok": ok, "got": got, "bar": bar}


class Examples(_Cards):
    def setUp(self):
        super().setUp()
        from sampo import works
        self.works = works
        cards = [ui_card(title=f"Check button contrast in React, case {i}") for i in range(3)]
        self.ids = [i for i, v, _ in self.sc.add(self.p, cards, ui_cell(), "haiku", 1) if v == "kept"]
        self.assertEqual(len(self.ids), 3)

    def test_the_sanitiser_drops_scripts_handlers_and_every_outside_url(self):
        dirty = ('<div onclick="steal()" style="color:red"><script>alert(1)</script>'
                 '<a href="https://evil.example/x">go</a><a href="&#106;avascript:alert(1)">js</a>'
                 '<img src="http://evil.example/p.png" alt="p"><img src="data:image/png;base64,AAAA" alt="ok">'
                 '<style>@import url(http://evil.example/a.css); .x{color:red}</style>'
                 '<p style="background:url(http://evil.example/a)">hi</p><iframe src="/x"></iframe>'
                 '<form action="https://evil.example"><input formaction="//evil.example" value="v"></form>'
                 '<svg viewBox="0 0 4 4"><use href="https://evil.example/s.svg#a"/><rect width="4" height="4"/></svg>'
                 '<style>a{}</style ><script>x()</script></div>')
        clean, offences = self.works.sanitise_html(dirty)
        for bad in ("script", "onclick", "evil", "javascript", "iframe", "@import", "<use", "http"):
            self.assertNotIn(bad, clean.lower(), clean)
        for kept in ("hi", "go", "js", "data:image/png;base64,AAAA", "<rect", 'style="color:red"', 'value="v"'):
            self.assertIn(kept, clean)
        self.assertGreaterEqual(len(offences), 9, offences)
        self.assertTrue(any("onclick" in o for o in offences), offences)
        self.assertEqual(self.works.sanitise_html(SAFE), (SAFE, []), "a clean fragment passes unchanged")
        clean, offences = self.works.sanitise_html("<p>1 &lt; 2 &amp; <b>bold</b></p>")
        self.assertEqual((clean, offences), ("<p>1 &lt; 2 &amp; <b>bold</b></p>", []), "text stays text")

    def fake_ask(self, replies, seen):
        def ask(p, system, prompt, cheap=True, paid=0, feature=None, writer=None):
            seen.append((prompt, writer))
            title = next(t for t in replies if t in prompt)
            return replies[title], "gemma-local"
        return ask

    def run_examples(self, replies, verdict, **kw):
        seen = []

        def check(path, docs):
            return [{"gates": verdict(d["light"]), "h": 120} for d in docs]
        with mock.patch.object(self.sc.ms, "_ask", side_effect=self.fake_ask(replies, seen)), \
                mock.patch.object(self.works, "check_html", side_effect=check), \
                mock.patch("sampo.shot.find_browser", return_value=("/usr/bin/chromium", "chromium")):
            r = self.sc.write_examples(self.p, **kw)
        return r, seen

    def test_kept_only_when_every_gate_passes_and_each_rejection_says_why(self):
        titles = [r["title"] for r in self.sc.rows()]
        replies = {titles[0]: "```html\n" + SAFE + "\n```", titles[1]: SAFE.replace("Save draft", "LOW contrast"),
                   titles[2]: SAFE + '<script>fetch("https://evil.example")</script>'}

        def verdict(doc):
            low = "LOW contrast" in doc
            return [gate("blank", True), gate("overflow", True),
                    gate("contrast", not low, "3.1:1" if low else "7.2:1", "bar 4.5:1")]
        r, seen = self.run_examples(replies, verdict, max_calls=5, workers=1, writer="local")
        self.assertEqual((r["calls"], r["kept"], r["rejected"]), (3, 1, 2), r)
        self.assertEqual(r["stopped"], "done")
        self.assertTrue(all(w == "local" for _, w in seen), "a local model writes them")
        for word in ("button", "react", "4.5:1", "390", "12 KB"):
            self.assertIn(word, seen[0][0])
        got = self.sc.examples()
        self.assertEqual(list(got), [self.ids[0]])
        self.assertEqual(got[self.ids[0]]["html"], SAFE, "the fence is stripped, the html kept as checked")
        self.assertEqual(got[self.ids[0]]["h"], 120)
        rows = list(self.sc.ms._read(os.path.join(self.sc.home(), "examples.jsonl")))
        why = {x["card"]: x.get("why", "") for x in rows if not x["kept"]}
        self.assertIn("contrast: 3.1:1 (bar 4.5:1)", why[self.ids[1]])
        self.assertIn("safe: script", why[self.ids[2]], "an unsafe one is refused before any render")
        self.assertEqual(r["reasons"], {"contrast": 1, "safe": 1})
        again, seen = self.run_examples(replies, verdict, max_calls=5, workers=1, writer="local")
        self.assertNotIn(self.ids[0], " ".join(p for p, _ in seen), "a card with a kept example isn't asked again")
        self.assertIn("1 kept", self.sc.examples_line(r))

    def test_over_12_kb_is_refused_unrendered(self):
        big = '<div class="row">' + "<p>Row of text in a list</p>" * 600 + "</div>"
        titles = [r["title"] for r in self.sc.rows()]
        r, _ = self.run_examples({t: big for t in titles}, lambda d: self.fail("rendered"), max_calls=1, workers=1,
                                 writer="local")
        self.assertEqual((r["kept"], r["reasons"]), (0, {"size": 1}))

    def test_no_browser_means_no_model_calls(self):
        with mock.patch.object(self.sc.ms, "_ask") as ask, \
                mock.patch("sampo.shot.find_browser", return_value=(None, "no browser here")), \
                self.assertRaises(ValueError):
            self.sc.write_examples(self.p, max_calls=2, writer="local")
        ask.assert_not_called()

    def test_a_stored_example_is_re_sanitised_on_read(self):
        os.makedirs(self.sc.home(), exist_ok=True)
        with open(os.path.join(self.sc.home(), "examples.jsonl"), "w", encoding="utf-8") as f:
            for card, html in ((self.ids[0], SAFE + "<script>x()</script>"), (self.ids[1], SAFE)):
                f.write(json.dumps({"card": card, "kept": True, "html": html, "h": 90}) + "\n")
        self.assertEqual(list(self.sc.examples()), [self.ids[1]], "an edited store can't smuggle a script out")

    def test_the_cli_verb_needs_the_ui_grid(self):
        import types
        from sampo import cli
        args = types.SimpleNamespace(name="examples", rest=[], grid=None, writer=None, max_calls=2, workers=None,
                                     project=None)
        with self.assertRaises(cli.UsageError):
            self.sc.cmd(args)
        args.grid = "ui"
        with mock.patch.object(self.sc, "write_examples", return_value={"calls": 0, "kept": 0, "rejected": 0,
                                                                         "reasons": {}, "stopped": "done", "error": "",
                                                                         "models": {}}) as w, \
                mock.patch.object(cli, "out"):
            self.sc.cmd(args)
        self.assertEqual(w.call_args.kwargs.get("writer"), "local", "the local model is the default writer")


@unittest.skipUnless(headless_chrome(), "no headless Chromium here")
class ExamplesRendered(_Cards):
    """The render check on a real browser: its verdicts, and the 16-component kit in both themes."""

    def check(self, *fragments):
        from sampo import works
        docs = [{t: self.sc.doc(f, t) for t in works.THEMES} for f in fragments]
        return [{g["gate"]: g for g in r["gates"]} | {"h": r["h"]} for r in works.check_html(headless_chrome(), docs)]

    def test_blank_overflow_and_faint_text_fail_and_a_good_one_passes(self):
        good, blank, wide, faint = self.check(
            SAFE, "<div></div>", '<div style="width:640px;background:var(--surface)">Too wide for a phone</div>',
            '<p style="color:#c8c8c8">Faint grey text on white</p>')
        self.assertTrue(all(good[g]["ok"] for g in ("blank", "overflow", "contrast", "height")), good)
        self.assertLess(good["h"], 200)
        self.assertFalse(blank["blank"]["ok"])
        self.assertFalse(wide["overflow"]["ok"])
        self.assertIn("640", wide["overflow"]["got"])
        self.assertFalse(faint["contrast"]["ok"], faint["contrast"])

    def test_every_component_has_a_kit_demo_that_passes_in_both_themes(self):
        kit = self.sc.kit()
        self.assertEqual(set(kit), set(self.sc.COMPONENTS))
        from sampo import works
        for comp, got in zip(kit, self.check(*(k["html"] for k in kit.values()))):
            with self.subTest(comp):
                self.assertEqual(works.sanitise_html(kit[comp]["html"])[1], [], "the kit is clean as written")
                self.assertEqual([g for g, v in got.items() if g != "h" and not v["ok"]], [], got)
                self.assertLessEqual(got["h"], kit[comp]["h"], "its declared height holds it")
                self.assertLessEqual(kit[comp]["h"] - got["h"], 40, "and isn't far taller")
