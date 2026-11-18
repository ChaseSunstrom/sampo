# Mission 1 — exhaustive thinking (gen 4, #2): width that costs nothing

Task: T-2910 (brief 1).

Parent: gen-4/seed.md row 1 → gen-0.md #2: "Exhaustive thinking. Before acting on anything non-trivial: list the
readings of the request, every approach (including "use what exists" and "do less"), the likely outcomes and failure
modes of each, edge cases, and blast radius (`fm graph blast`). Then generate several candidate fixes or designs, test
them in parallel lanes, and choose by evidence, not first instinct. Scale the depth to the risk, so an S task doesn't
pay an L task's cost." gen-0's goal, MUSTs and NEVERs are quoted in the seed and apply here unchanged.

## Capability
Width without a price. #2 asks Sampo to cover the whole space of a request, and gen-0 says the depth scales with risk
because width costs money. The machine already has a free source of width: the local models. In the Moons funnel, the
local 27B's candidates survive the cheap-first filters far more often than Haiku's (`sampo moons coverage`, read
2026-11-04: kept by model "haiku 545 of 1499, a large local model+nothink 2753 of 3316, a large local model+think 64 of 64"). Yet the verb
every brainstorm goes through can only call the cloud. This generation's own procedure hit it: it asked for local idea
volume "when a command lets you", and `sampo ideas` has no way to do that.

## Grounding (each read in the code on 2026-11-04)
- plugin/lib/sampo/ideas.py:78 `child_cmd` builds every child as `claude -p --model <model>`: there is no other path.
  ideas.py:253 `run_child` (falsify, crossbreed and the deepen rounds) and ideas.py:281 `run_children` (the lens round,
  called at ideas.py:797 with `child_cmd(args.model, …)` per lens) both go through it.
- plugin/lib/sampo/cli.py:8070: `sampo ideas --model` defaults to sonnet; nothing reads a local profile.
- plugin/lib/sampo/moonscale.py:364 `_generative`, :379 `_local` and :397 `_ask` already pick the first own or loopback
  generative profile that answers (a sensitive project's only on loopback) and fall back to Haiku. They are the
  routing to reuse, not to copy.
- models.json: the `a large local model` profile is qwen3.8-27b, context 256000, `slots: 2`, `own: true`, so a ten-lens round can
  run two at a time with every seen list in context.
- Live use: the ledger holds 16 `ideas` events since 2026-11-01, and each gen-4 Mission row names a run of
  `--rounds 4 --deepen 3 --falsify --crossbreed` (about 35 children each).
- The open L task T-2878 lists this as its step 2 ("volume: parallel local calls for moons grow and the ideas
  lenses"). T-2910 is that step's brainstorm half, cut as its own M so it can land tonight. Moons grow already has its
  local routing.

## Gaps this Mission targets (coverage: #2)
- find × headless/server: a brainstorm that runs with no cloud call, so a night job or `sampo run` can widen a plan
  for free.
- plan × fm code: Smith's plans and brainstorms get the same local arm through the one verb they call.

## Ideas (from the brainstorm, grounded)
The brainstorm for this seed row ran before this file (brainstorm-20261102-184150: 542 distinct ideas, 4 rounds, 3
deepened categories, cross-bred and kill-tested; decided on 2026-11-04 to reuse it, since the seed hasn't changed
since). Grounded against the code:
- "Keep a local-only brainstorm mode with a pack-egress diff" (round 2): new. Nothing in ideas.py calls a local model.
  It is brief 1, and its egress half is the sensitive rule moonscale already enforces.
- "Price the width at intake and reveal readings only on disagreement" (cross-bred) and "Run the Mission at three
  widths and let outcomes pick": partial. `sampo mission` sets lenses and rounds, but width has a price that only a
  local arm takes to zero. Both wait on brief 1's head-to-head: if the local arm holds up, a width dial has nothing to
  charge for.
- "Show the other readings only when two cold readers disagree": built near T-2229 (`fm genesis put` shows a cold
  reader's FAIL quotes). Not taken.
The kill test the brainstorm wrote for the width idea ("ask the user once … if the answer is always no, drop it")
needs the user present; the head-to-head below replaces it with a measurement that doesn't.

## Approaches
1. A `local` model name in `run_child` and `run_children`, routed through `moonscale._local` and `models.complete` in a
   pool sized by the profile's slots, Haiku for any child the local model can't answer, the answering model booked
   per child. Likely outcome: brainstorms at no cost, and a number that decides the default. Fails if: the local
   replies miss the lens format and parse to few ideas (the test plants one; the head-to-head counts parsed ideas).
2. use what exists: call `moonscale._ask` per child. Fails if: it is serial and books spend under the moons feature.
3. do less: keep local volume in `moons grow` only. Fails if: every Mission brainstorm keeps paying for sonnet while
   the local model idles.
4. A separate `ideas-local` verb. Fails if: it splits the brainstorm skill's one entry point and doubles the parser.

Choice: 1 (75%). The doubt is reply format, measured, not assumed.

## Exam (an outcome measure, with a live reading)
- Outcome measure: the head-to-head on one real gen-4 Mission pack (Mission 2's), same lenses, same `--seen` set, one
  round each, local vs sonnet. Per arm: distinct ideas past `--seen`, and a blind cold judge (`sampo second`, arm
  labels removed) marking each of 40 sampled ideas per arm "grounded in this repo and new" or not. The local arm earns
  the default if its judged-good count reaches 80% of sonnet's. Under 50%, it stays opt-in and the gap is the next
  #2 Mission's lead. Filed as research/ideas-local-vs-cloud.
- Live reading before close: one `sampo ideas --model local --rounds 1` on this project, its manifest showing the
  local profile on every child.
- Exam kinds: #2's scenarios are unchanged. Their session arms run at the end-of-run exam (rule 6).

## Long-horizon goal (end of run)
On a held-out long goal, the plan's readings and options come from a brainstorm that cost nothing, and the session
that used them closes more checks than one that didn't.

## Risks
- Format drift: the parser drops malformed bullets silently. Mitigation: a planted malformed reply in the test, and
  parsed-per-child counts in the head-to-head.
- GPU contention with `moons grow` and the bench: one pool sized by `slots`, never more children than the profile
  allows.
- Spend booked wrong: local children book $0 under the `ideas` feature, and a Haiku fallback books its real cost.
- Sensitive projects: loopback only, failing closed, never the cloud.

## Lean
No new verb, one model name, one branch in the child runner. No hook change.

## Tier share
Brief 1 is tier 3: a brainstorm whose width is free changes what "scale the depth to the risk" can afford.

## Hands to the next generation
The head-to-head number. If the local arm holds up: the width dial (rounds and lenses set by risk, at no cost), and
`sampo mission` defaulting to local. If it doesn't: the failure classes the judge found go to T-2878's control step
(Smith's small-model mode) as its first cases.

## Done when
T-2910's three criteria are green, the research note is filed, and MASTER.md (sampo ideas) and CHANGELOG say it.
