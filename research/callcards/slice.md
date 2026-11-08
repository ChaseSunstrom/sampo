# Calling cards, the thin slice: the pilot (T-3160) and the harness-driven pilot (T-3181)

## Part 1b: the harness drives the cards (T-3181)

The T-3160 pilot below found the model rarely calls a card (1 run of 10). T-3181 moves the decision to fm code:
before turn 1, on a task whose verify is red, fm code itself runs `failing-frame` on the file the verify runs, runs
the outline card on the files the frame names (at most 3), and puts the facts in the first prompt; when the failure's
exception and message match a fix card's `signature` (`py-none-guard`: TypeError or AttributeError with
"'NoneType'"), fm code applies that card to the frame's function (never in a test file or the file a verify runs), the model filling only the card's one open slot (the
parameter to guard) under a JSON schema. A passing verify, a read-only run or a failed step changes nothing. The
early-done refusal (T-3180) was already in.

### Verdict

Not proven. Thirty paired scenarios on `jarvis-nothink`: cards off passed 3 of 30, cards on 4 of 30. That is 2 wins
and 1 loss; the exact two-sided sign test gives p = 1.0. The brief asks for 40 pairs before "proven" and the exam runs
on this box hold 30 python `bug`, `vuln` and `none` scenarios in all, so the 40-pair bar could not be met; at 30 the
result would not have been proven either (p = 1.0, and 2 wins against 1 loss).

### How it was measured

- Commands: `sampo card bench RUN --model jarvis-nothink --kind none,bug,vuln --limit 11 --out
  research/callcards/t3181-pilot.jsonl` for RUN in s70-010, s21-013, s0-001, s31-006, s4-018, s5-019 and s7-004
  (the rows are in `t3181-pilot.jsonl`; `sampo card bench --out` on it prints the summary). Same arms as T-3160:
  `SAMPO_CARDS=off` against `on`, the same model, each in a throwaway copy, the first arm alternating. Pass is the
  exam's hidden checks.
- The pairs are not independent of the T-3160 pilot: its 10 scenarios were the first 10 python scenarios of s70-010,
  and the s70-010 pairs here are the same ones, run again with the harness. They also share a model and a host, so
  the sign test's independence assumption is only roughly met.
- Because this is not proven, the harness is opt-in: fm code runs it only with `SAMPO_CARDS=on` set explicitly (the
  card bench sets it per arm). A default run offers the cards but runs no harness step. After a fix card applies, the
  harness verifies again and keeps the edit only when strictly fewer criteria fail; else it puts the files back.
- The 30 pairs ran before the revert, the dotted-name and the test-file rules (review fixes), so the numbers describe
  the first harness, not this one.
- One pair (s5-019-m01, cards on) ran twice because two bench processes overlapped; the newest row counts and both
  failed.
- `card_calls` now counts the harness's own calls before turn 1 as well as the model's.
- Host load was 80 to 170 over 16 CPUs, so `seconds` says little; Jarvis did the model work.

### Numbers

| | cards off | cards on |
|---|---|---|
| passed | 3 / 30 (10%) | 4 / 30 (13%) |
| tokens per scenario, mean (median) | 65,710 (5,904) | 85,560 (20,994) |
| seconds per scenario, mean (median) | 166 (23) | 154 (58) |
| runs that called a card (calls) | 0 (0) | 13 (24) |
| runs that edited or wrote a file | 6 | 9 |
| runs that ended with done accepted | 5 | 12 |

Wins (only on passed) 2: bug-python-1a097f and bug-python-1e5fc6. Losses (only off passed) 1: s5-019-m04. Ties 27:
two passed in both arms (bug-python-88e0e5, bug-python-020e5a; both start with a green verify, so there was nothing
for a card to do), 25 failed in both.

### What failed

- The gain is two scenarios, both `bug` with a red verify at the start: the harness ran `failing-frame`, the model
  edited and the checks passed (39,955 and 41,893 tokens, against 4,054 and 2,966 for plain runs that read, then
  called done). That is the shape the change was built for, but two of thirty is a count that chance alone gives.
- The runs that ended early now end less often (12 accepted dones with cards against 5 plain, 9 runs that edited
  against 6), yet most still failed the hidden checks: the model finds a spot, changes it and is wrong. The cards
  give it facts about the failing frame, not the fix.
- The harness had something to read in few runs. In the on-arm, the first verify was red (exit 1 or 5) in 9 of 30 runs,
  and every one of those had a card call; 2 of the 9 passed. It was green at the start in 9 (the visible tests don't
  catch the defect), where the harness reads no frame; and in 11 (5 `vuln`, 5 `bug`, 1 `none`) no verify ran at all, so the harness stood idle and the arm
  differs from the plain one only by the card lines (one of them still called a card itself).
- The fix card was not what won. Each of the two wins shows one card call, the failing frame; the trail does not
  record a fix card applied, and the exam's seeded slips are mostly not None errors, so the signature (TypeError or
  AttributeError with "NoneType") matches few failures.
- Cost: the on-arm's mean tokens are 30% higher; the medians are 3.6 times higher. The long runs of s5-019 and s4-018
  (300k to 500k tokens, both arms) dominate the means.

### What would have to change

A pool over 40 pairs (other kinds such as untested or slow, or a second model) and fix cards for the shapes the exam
seeds (an off-by-one in a bound, a wrong comparison), with the frame's operands as the evidence. A card that locates the
line is worth less than one that proposes the one-line change the failing test then checks. For 40 pairs on this pool
the box needs about 10 more scenarios of python `bug`, `vuln` or `none` kind in the exam runs.

## Part 1: the pilot (T-3160)

The question: does a small local model fix more exam defects when fm code offers it calling cards (verified
generators it calls by name with typed inputs) than when it works with the plain actions alone? The brief's step 3
says to run a cheap pilot first and stop if it shows nothing. The user's words: "If step 3's pilot shows no gain,
stop there and report the numbers — that is a valid result."

## Verdict

Not proven. Ten paired scenarios on `jarvis-nothink`: cards off passed 1 of 10, cards on 0 of 10. That is 0 wins
and 1 loss, and the sign test gives p = 1.0. The model called a card in one run of ten (`py-outline`, twice, both
answered) and that run failed too. Per the brief's step 3 and the user's rule, the work stops here. Steps 4 to 6
were not built.

## How it was measured

- Command: `sampo card bench s70-010 --kind none,bug,vuln --limit 10 --out research/callcards/pilot.jsonl`.
  The rows are in `pilot.jsonl` beside this file; `sampo card bench --out research/callcards/pilot.jsonl` prints the
  summary again.
- Model: `jarvis-nothink` (Jarvis, 27B, thinking off). It answered the first call, so the fallbacks
  (`gpu-gemma12-32k`, `gpu-mimo9`) never ran. Both arms ran on the same model.
- Arms: fm code with `SAMPO_CARDS=off` (no card tool, no card lines in the prompt) and with `SAMPO_CARDS=on`. Each
  arm runs in its own throwaway copy of the scenario, with louhi's smith-arm isolation (its own Smith home and no user
  state), at most 40 steps. The arm that goes first alternates from one scenario to the next.
- Scenarios: the first 10 python scenarios of exam run s70-010 whose kind is `none` (a logic bug a user would hit),
  `bug` or `vuln`. These are the same scenarios and requests the exam itself uses.
- Pass: the exam's hidden checks (`louhi.passes`) on the tree the run left. A run that changes nothing can't pass
  on a guard check alone.
- Cards: `py-outline`, `py-callers`, `failing-frame` and `py-none-guard`. Each passes its known-bad-input test and
  its plausible-but-wrong-output test (`sampo card test`).
- Caveats on the rows: `offered` is the cards' lexical rank of the request alone, worked out by the bench as the
  pilot started; fm code's own pick also reads the plan's title, and it now records its offer as a `note` event
  that later bench rows read instead. `seconds` is wall-clock time on a machine shared with other sessions' test
  runs (load 20 to 40 over 16 CPUs); the model ran on Jarvis, so the load mostly slows the harness, not the model.
- Statistics: the pairs where only one arm passed go into an exact two-sided sign test. A gain counts as "proven"
  only at p < 0.05 with more wins than losses.

## Numbers

| | cards off | cards on |
|---|---|---|
| passed | 1 / 10 (10%) | 0 / 10 (0%) |
| tokens per scenario, mean (median) | 35,742 (8,968) | 20,448 (7,637) |
| seconds per scenario, mean (median) | 77.0 (19.6) | 39.1 (23.7) |
| card calls | 0 | 2 (1 run) |
| runs that edited or wrote a file | 4 | 3 |

Wins (only on passed) 0, losses (only off passed) 1, ties 9; exact two-sided sign test p = 1.0.

The means are swayed by three long off-arm runs (68k to 128k tokens, 111 to 271 s); the medians are close. No
claim about cost either way follows from ten pairs.

Each pair (passed, tokens, seconds):

| scenario | cards off | cards on | card calls |
|---|---|---|---|
| bug-python-f5ce6e | fail, 4527, 12.3 | fail, 8719, 22.0 | 0 |
| bug-python-1a097f | fail, 691, 6.6 | fail, 2475, 10.8 | 0 |
| bug-python-1e5fc6 | pass, 128358, 270.6 | fail, 80746, 136.8 | 0 |
| vuln-python-798082 | fail, 113649, 265.6 | fail, 5701, 25.4 | 0 |
| vuln-python-932756 | fail, 68455, 111.0 | fail, 44847, 70.6 | 0 |
| none-python-6aec03 | fail, 16260, 36.3 | fail, 5803, 14.1 | 0 |
| none-python-57a947 | fail, 1570, 9.1 | fail, 15030, 26.3 | 0 |
| none-python-77cc63 | fail, 8996, 18.9 | fail, 2264, 7.3 | 0 |
| none-python-f480ab | fail, 8939, 19.0 | fail, 6554, 19.2 | 0 |
| none-python-0098c3 | fail, 5973, 20.2 | fail, 32341, 58.2 | 2 |

Before the pilot, two probe pairs (s21-013 and one s70-010 scenario, kind `none`) failed in both arms with no card
calls; they are not counted here.

## What failed

- The model almost never called a card. One on-arm run of ten called one: `py-outline`, by its own name (fm code
  takes a card's name as an action), twice with the same inputs, both answered. It then read, wrote a file and
  failed the hidden checks. Every other on-arm run used read, search, edit, write, run and done only, even with the
  card tool in its tool list and the cards taught in the system prompt. A pilot where the cards are hardly called
  measures mostly what offering them costs.
- The cards fit few of the defects. The exam's `bug`, `vuln` and `none` scenarios are seeded slips whose fix is a
  small edit the model must first find. Of the four cards only `py-none-guard` changes code, and it makes one fix
  shape, a None check. The three read cards (outline, callers, failing frame) only help find the spot, which the
  model did with read and search instead. The offer is lexical (the words of the request against each card's
  purpose), so a request that says "None" or "callers" gets that card whether or not it fits, and a request
  with none of the cards' words gets no card at all. By the bench's rank of the requests, the five `none` scenarios
  were offered nothing; the one card call shows fm code's own pick (which adds the plan's title) offered
  `py-outline` to one of them. For the rest, both arms likely ran the same prompt, and their differences are the
  model's own variance.
- The baseline is near the floor. On these scenarios fm code on Jarvis often reads a file or two, then calls done
  with nothing changed (several runs ended after 3 to 7 actions). The exam's own smith runs on `jarvis-nothink` show
  the same: 0 or 1 of 3 on s5-019 and s4-018. A card can't help a run that stops before it would edit.
- Offering cards costs tokens on every turn: the card lines sit in the system prompt. The two short bug pairs
  (bug-python-f5ce6e, bug-python-1a097f) spent 2 to 4 times the off arm's tokens on the on arm, though with a few
  more actions too, so the prompt's share can't be separated out here. Over all ten pairs the long off-arm runs
  outweigh it.
- The variance is large. One scenario passed off and failed on with 128k and 81k tokens spent: these long runs
  wander (edit, run, done, read again) and one pass or fail is close to chance at this size.

What would have to change before a second pilot is worth its time: cards that fit the defects small models miss
(locate the failing assertion's line and its operands; apply a one-line replacement checked by the failing test),
and an offer the model takes up (the plan's step names the card to call, or the harness calls a read card such as
`failing-frame` itself before the first turn and puts its facts in the prompt).

## What was not done

Steps 4 to 6 were not built, because the pilot showed no gain: seed cards 5 to 10, the qwen3-embedding ranker with
its 30-task recall test, and the 40-pair run. What the pilot needed from step 5 was built: fm code offers the picked
cards as a native `card` tool and a JSON `card` action, with the lexical pick standing in for the embedding ranker.
Criterion 5's 40-pair run is still owed if the cards are redesigned.
