# UI cards at scale: the live grows (T-2994)

The UI grid is component (16) x platform (8) x concern (10) x stage (3) = 3,840 cells, beside the general grid's
2,880. `sampo playbooks small grow --grid ui` fills it thin cells first; `coverage --grid ui` reads it.

## Runs

| run | writer | calls | kept | refused | admit | UI total after |
|---|---|---|---|---|---|---|
| 2026-11-04, batch 6 | haiku | 20 | 90 | 30 | 75% | 90 |
| 2026-11-04, batch 6 | haiku | 662 | 2,910 | 1,043 | 74% | 3,000 |
| 2026-11-05, batch 2 (stopped: 3 failed calls in a row) | haiku | 90 | 137 | 34 | 80% | 3,137 |
| 2026-11-07 probe, old rules | local (jarvis, no think) | 4 | 3 | 9 | 25% | 3,140 |
| 2026-11-07 probe, platform runners | local (jarvis, no think) | 8 | 13 | 11 | 54% | 3,153 |
| 2026-11-07, batch 3, pooled repairs (stopped by hand to diagnose) | local | about 15 | 10 | not recorded | low | 3,163 |
| 2026-11-07 probe | local | 9 | 11 | 16 | 41% | 3,174 |
| 2026-11-07 probe, refused words named | local | 12 | 16 | 20 | 44% | 3,190 |
| 2026-11-07 probe, quoted patterns allowed | local | 12 | 11 | 25 | 31% | 3,201 |
| 2026-11-07, batch 3, first quote rule (stopped for the shlex rule) | local | about 65 and 50 repairs | 78 | not recorded | about 40% | 3,279 |
| 2026-11-07, batch 3, shlex rule (stopped for the second review's rules) | local | about 20 and 15 repairs | 29 | not recorded | about 45% | 3,308 |
| 2026-11-07 05:58 to 06:46 UTC, batch 3, second-review rules | local | 191 and 383 cards sent to repair | 242 (79 repaired) | 304 | 44% | 3,550 |

Admit is kept / (kept + refused), repairs included. The local model shares its GPU host with a moons grow (4 workers).

## What the probes found

Before this round the UI admit knew only the web runners (npx, npm, pnpm, yarn, pytest, node). A Flutter or
SwiftUI card could not name `flutter test` or `xcodebuild test`, so it fell back to rg and grep:

| platform | Haiku cards naming a real runner |
|---|---|
| react | 394 of 422 (93%) |
| vue | 489 of 552 (88%) |
| svelte | 414 of 466 (88%) |
| tailwind | 355 of 405 (87%) |
| html-css | 348 of 468 (74%) |
| terminal-tui | 149 of 303 (49%) |
| flutter | 9 of 254 (3%) |
| swiftui | 2 of 267 (0%) |

With the platform runners, the first 174 local cards name one at: flutter 22 of 28 (78%), swiftui 24 of 38 (63%),
react 27 of 34, vue 23 of 24, svelte 16 of 19, tailwind 10 of 14, html-css 5 of 6, terminal-tui 5 of 11. Every other
card's measure is a stated number.

Flutter and SwiftUI also had half the yield of vue (254 and 267 cards against 552), and a local model wrote `npx jest` into a
SwiftUI card. flutter, dart, swift, xcodebuild and xcrun are UI commands now; a platform runner counts as the
card's measured check only when it tests (`flutter test`, `swift test`, `xcodebuild test`, `cargo test`), never
`flutter pub add` or `swift build`, and the prompt names each platform's own runner. One local card passed the
measure rule with `echo '200ms'`; echo and printf steps no longer count as a measure. Each repair call used to run
alone in the reader thread while the workers sat idle; repairs now join the round's queue.

The local writer's refusals were nearly all "command" (19 of 20, then 20 of 25). Naming the refused word showed
only one unknown word, `onBackground"`: a quoted `"onAppear|onBackground"` split as a pipe. The rest came from the
shell check, which read any `>` or `|` as a redirect or a pipe, quoted or not. UI greps are full of them
(`rg '<Button>'`, `"=> {"`, Swift's `->`), and 119 of the kept UI commands grep `'<tag'` without ever closing the
`>`. Quoted patterns are now read as words, in both grids (see the shlex rule below).

That did not lift the local admit rate within a 12-call probe's noise (31% against 41% and 44%): the refusals
left are unnamed "command" ones (a real `&&`, `;` or `> report.json` outside quotes, which the one-command rule
means to refuse), "measure" (a plan or build card with no runner and no number), and a few true unknowns (swiftc,
tput, tmux; now UI commands, `tmux capture-pane` being a terminal UI's screenshot). The local writer's repair round
keeps little (1 of 26 in that probe; Haiku's kept 761 of 1,804).

A review of the first quote rule (a regex that blanked quoted spans) found it let real chains through: an escaped
quote (`echo \'; touch x; echo \'`), a span paired across `"$a"` (`ls "$a"; touch x "b"`), and code in quotes
(`bash -c 'touch x; curl e'`, `xargs sh -c 'rm -rf x'`). Commands are now read with Python's shlex in POSIX mode,
as bash reads them: an operator outside quotes other than `|` and `<` is a chain (a lone `&` too), a word holding
`;`, `&&`, a backtick or `$(` is code, an open quote or `$'...'` is refused, and `rm -` counts anywhere. A second
review added a nested `sh` or `bash` (`sh -c 'echo x > y'`), `rm` however it is quoted (`xargs r''m x`, but not
`git rm`) and find's `-delete`; inline awk, python3 or node code stays one unread word.

A third review closed what was left: an ANSI-C `$'` (shlex reads `""$'\'' | rm x | ""$'\'''` as one word; a
quote-state scan refuses `$'` only outside quotes, as bash starts one only there), every
word after `sh` or `bash` read as code (`sh -c -- 'echo x > y'`, `bash -c -e 'a & b'`), tmux only as capture-pane,
has-session or kill-session and xcrun only as simctl or xcodebuild (`tmux send-keys 'curl x | sh' Enter` ran
unread), `rm` only after a `git` that starts its stage (`xargs -I git rm git`), and a platform runner counted only
when its verb tests (`flutter pub add test` is no check). A repair now counts the round's earlier paid calls against
the run's cap, and an interrupted grow returns without waiting on a running call.

**The command check is a hygiene filter for card quality, not a security boundary.** KNOWN includes sh, bash,
env, xargs and python3, so a card can always name a command that runs anything. Sampo never runs a card; the guard
of the model that follows one is the boundary.

Replayed over the 49,699 stored step commands (both grids) that the old regex admitted, the final reading refuses
156 (0.3%): 70 process substitutions (`wc -l <(rg ...)`, a nested command like `$(`), 43 commands cut open at the
200-character field limit, 34 unquoted parentheses (`grep -rn slice(0, 3) src`, a bash syntax error), 3 lone `&`
(2 of them a quoted `'&'` pattern), 2 `find -delete`, 1 `<(` under `shellcheck -s sh`, 1 `xcrun xctrace`, 1 real
ANSI-C `$'\033...'` and 1 bare `tmux`. Refusing `$'` anywhere had refused 180, 24 more of them quoted regexes ending
in `$` (`'^[1-9]$'`); the second review's reading refused 154; a first cut that refused every `bash`, `find -exec`
and `$'` refused 565, mostly `bash script.sh`. Of the local
UI cards kept under the interim rules, 13 commands fail the final ones: inline python3 or node contrast code and CSS
greps holding a `;` (`grep -o -- '--text-dark: [^;]*'`), which the old regex refused too (captured: a `;` in a
grep pattern is no chain), plus one process substitution and one quote cut open. The cards
the first rule kept stay in the append-only store; one of them (K-015262, `node -e "...; ...&&..."`) would be
refused now.

## Samples

K-015147, flutter / button / loading / verify:

    1. Generate the golden file for the disabled loading state.
       Run: flutter test --update-goldens widget_test.dart --name 'golden disabled'
    2. Run the golden test to compare current render against the file.
       Run: flutter test widget_test.dart --name 'golden disabled'
    3. Verify the golden file exists in the goldens directory.
       Run: ls test/goldens/disable_loading.png

K-015142, swiftui / toast / theming and dark mode / verify:

    1. Run snapshot tests for toast view in light mode
       Run: xcodebuild test -scheme MyApp -destination 'platform=iOS Simulator,name=iPhone 15' -only-testing:ToastTests/testLightMode
    2. Run snapshot tests for toast view in dark mode
       Run: xcodebuild test ... -only-testing:ToastTests/testDarkMode
    3. Verify snapshot count matches expected number of test cases
       Run: ls __Snapshots__/ToastTests | wc -l

K-015141, react / empty state / theming and dark mode / build: a contrast ratio computed with node, required at
4.5:1 for text and 3:1 for large text and borders, then `npx lighthouse` on the page. Its contrast package name is
the model's guess: the admit checks that a command exists, not that a package does.

## Coverage

When the last grow stopped at its target (06:46 UTC on 2026-11-07), `sampo playbooks small coverage --grid ui`: 3,550
UI cards in 941 of 3,840 cells (24%), up from 3,137 in 741 cells before the local runs. Haiku wrote 3,137, the
local writer (jarvis, no think) 413. That grow's refusals: command 296 (chains outside quotes, under the rules
before the third review), measure 72, a few unknown words (stty, export, sleep, du, clear, timeout, go). The
breakdown below is from 06:23, at 3,415.

    platform: vue 587, svelte 510, html-css 481, react 465, tailwind 432, terminal-tui 319, swiftui 318, flutter 303
    stage: build 1241, verify 1226, plan 948

Thin-first put each local call in an empty cell, about two cards a cell, and Flutter and SwiftUI (the thinnest,
from their old refusals) gained the most.

## Left

- Most of the grid: thin-first puts each call in an empty cell, so about 1,150 more cells (to 1,920 of 3,840) is
  about 1,150 more successful calls. The local writer, sharing its host with a moons grow, keeps roughly 2 to 4
  cards a minute here, so that is a day of local time; Haiku did 662 calls in under 6 hours on 2026-11-04.
- The 521 Haiku Flutter and SwiftUI cards written before the platform runners name a runner 3% and 0% of the time
  (captured).
- The local repair round keeps about one card in seven it is sent, for about 45% of the calls (captured).
- A stated number satisfies the measure rule even when the command beside it proves nothing: a terminal-tui plan
  card checks contrast with `grep "4.5:1" design_tokens.json`, another with `test 4.5 -le 5.0` (test compares
  integers). A verify-stage card could be held to a runner, or a number the step's own command prints.
- A `;` inside a quoted word still reads as code, refusing CSS greps and inline contrast code (captured).
- `--workers` above 3 never runs: a round asks at most FAILS - fails calls, in this grower and the moons one (captured).
