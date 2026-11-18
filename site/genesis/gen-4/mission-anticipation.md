# Mission 2 — anticipation (gen 4, #3): a guess that reads the user's words

Task: T-2912 (brief 1).

Parent: gen-4/seed.md row 2 → gen-0.md #3: "Anticipation. A model of me, my projects and my machines (requests,
corrections, habits, what breaks). It predicts the next request and drafts it on spec in a lane, and flags problems
before they happen (grow `fmforesight`)." gen-0's goal, MUSTs and NEVERs are quoted in the seed and apply here
unchanged.

## Capability
The prediction is the root of #3: the session-start line, draft-ahead and the user model all hang off it. Today the
guess is a count of type transitions plus the paths of the last ask of that type. It reads no words. The live book
(predictions.jsonl, read 2026-11-04) holds 57 scored guesses: 25 type hits against the naive baseline's 21 (44% vs
37%), path overlap 0.172 against 0.166. Draft-ahead, which runs only while the guess beats the baseline, has drafted
once and been adopted never. This Mission gives the guess a second arm that reads what a person would read: the
user's recent asks in their own words, the open queue and the standing steers. It runs on the local 27B at no cost,
and it is shown only after a held-out replay says it leads.

## Grounding (each read in the code on 2026-11-04)
- plugin/lib/sampo/foresight.py:332 `predict(reqs)`: the type from transitions out of the last type, the paths from
  the newest ask of that type. T-2449's note in its docstring: changing what it reads made it trail the baseline.
- foresight.py:347 `prediction_tick` books one next-request row per user ask ({pred, base}) and scores the pending one
  with `type_hit`, `base_type_hit`, `overlap` and `base_overlap`. It is called from the hourly tick at
  plugin/lib/sampo/night.py:389 (the timer foreman-tick-foreman-4a056f is installed and fired at 00:00).
- foresight.py:388 `prediction_kinds` scores each kind against its baseline and mutes one that trails it over
  `MUTE_MIN` (foresight.py:267, 20) rows. foresight.py:245 `_next_line` shows a guess only while its kind leads.
  plugin/lib/sampo/draft.py:154 (`ahead`) drafts only on the same condition.
- plugin/lib/sampo/louhi.py:781 `replay_forecast` replays the user's own stream with the last third held out (the
  exam's foresight arm calls it at louhi.py:3537). This is the zero-token harness the new arm joins.
- Callers of the shared read, found by grep (the calibrated-judgment critique's rule): `foresight._requests` is read
  by louhi.py:787, draft.py:163 and foresight.py itself; `prediction_kinds` by outcomes.py:200 and :207,
  triggers.py:285 and draft.py:154. The new kind is added beside next-request, so none of them changes meaning.
- The surface is in use (the anticipation critique's rule): 60 next-request rows and 24 `shown` rows in the book; the
  session-start line printed "next-request predictions 44% vs baseline 37%" in `sampo triggers` today.
- The local model: models.json `a large local model` (qwen3.8-27b, own, 2 slots), routed by plugin/lib/sampo/moonscale.py:379
  `_local`, the same rule T-2910 reuses.

## Gaps this Mission targets (coverage: #3)
- understand × fm code: the user model reads the asks' words, not only their types.
- plan × headless/server: the guess is made on the tick with no cloud call and no session open.

## Ideas (from the brainstorm, grounded)
fm mission, then fm ideas --rounds 4 --falsify --crossbreed --seen-latest 6 (research/brainstorm-20261104-002824:
366 distinct ideas; its --deepen rounds were dropped by today's risk budget, recorded with sampo decide). Grounded:
- "Settle predictions by text similarity" (round 1) and the cross-bred "Close the prediction loop with edits,
  pushbacks and text-similarity settling": partial. Score rows hold type and path overlap only. Brief 1 adds a word
  overlap for the arm that predicts words. Its kill test ("agreement under 80% with a hand-judged 20") needs a hand
  judge; the replay's type and path numbers decide instead.
- "Draft the predicted ask speculatively and verify it cheaply": built near T-2407 (draft-ahead). Its kill test
  ("count accepted drafts; if under 3, the spend is waste") already reads 0 of 1. The draft waits on a better guess,
  which is brief 1.
- "Wild: Hold a nightly ghost user that predicts a handful of asks and is scored by the morning": new. It builds on
  brief 1's arm and goes to the next #3 Mission if the replay shows the arm leads.
- "Escalate to frontier only when local models disagree": new, held for T-2878's control step.

## Approaches
1. A local predictor beside `predict`, booked as `pred_local` in the same row, scored the same way plus a word
   overlap, listed as its own kind, replayed on the same held-out third. Likely outcome: a guess that knows what the
   user is in the middle of. Fails if: it doesn't beat the count on held-out rows; it then stays in shadow.
2. use what exists: tune `predict()`. Fails if: T-2449 found tuning made it trail; a count can't read words.
3. do less: settle by word similarity only. Fails if: the guess stays at 44% and draft-ahead stays idle.
4. A 4B decision model for the type. Fails if: the type is the weakest half; paths and words need context.

Choice: 1 (60%). The doubt is that the user's asks come from new ideas history can't predict. The replay measures it.

## Exam (an outcome measure, with a live reading)
- Outcome measure: `replay_forecast` on this project's own stream, held-out third, zero cloud tokens. The local arm
  leads if its type hit beats the count's by 10 points or more, or its path overlap by 0.10 or more. A lead earns the
  session-start line and draft-ahead. Anything less keeps it in shadow, with the numbers filed in
  research/next-request-local. The rule for a windowed signal holds: a guess counts only once the ask after it exists.
- Live reading before close: the book's next scored rows carry `local_type_hit`, and `sampo triggers` prints both arms.
- Exam kinds: #3's forecast scenarios are unchanged; the end-of-run exam reads the replay's new fields.

## Long-horizon goal (end of run)
The user opens a session and the first line names the ask they were about to type, often enough that a drafted plan
for it is adopted.

## Risks
- Leakage: the replay must give each guess only the asks before it (`reqs[:i]`), never the held-out ask's own text.
  A test checks that.
- The book's integrity: rows stay write-once; a new field, never a rewritten row.
- Privacy: a sensitive project predicts nothing (the existing rule), and the local prompt goes to own or loopback
  profiles only.
- Load: one call per user ask on the tick, two slots, a timeout. A down model books nothing and the tick goes on.

## Lean
No new verb, no new hook, no new file. One arm in an existing book.

## Tier share
Brief 1 is tier 3: the only #3 predictor that reads meaning.

## Hands to the next generation
The replay's numbers per arm. If the local arm leads: the ghost-user night run and word-level drafts. If it doesn't:
the failure classes (wrong type, wrong area, new idea) as cases for T-2878's small-model control step.

## Done when
T-2912's three criteria are green, the research note is filed, and MASTER.md (fmforesight) and CHANGELOG say it.
