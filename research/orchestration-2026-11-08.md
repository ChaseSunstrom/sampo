# Orchestration architecture for Sampo (T-3933), 2026-11-08

Question: would it be good to have orchestrators managing orchestrators, agents messaging each other, small models and
Claude checking each other, and decisions from several viewpoints?

**Answer: partly. Most of the parts exist and are barely used; the evidence says wire and measure two cheap ones (small-model
tripwires on replies, a peer note channel between lanes) before any layer of orchestrators. A hierarchy of orchestrators is the
one to not build now.** Numbers below are plain reads of the project ledger (65,707 events since 2026-11-01), the task
files (2,741) and the bus; counts are for this project only.

## 1. Capability map (what exists, with evidence)

| Part | Where | State of use |
|---|---|---|
| Bus: `send`, `unread`, `wake`, `watch` | plugin/lib/sampo/bus.py:53,74,124,287 | 30 messages in 23 days: 29 `note`, 1 `steer`; 15 went to one lane id. No lane-to-lane traffic by design: `send(to, text, kind, sender)` allows it but nothing prompts it |
| Conductor: `live`, `rows`, `steer`, `remote` | bus.py:351,675,746 (`cmd_conductor`); remotes bus.py:690-745 | `steer` broadcasts to all live sessions plus the user's own remotes over ssh (one remote, `devbox`). It is a broadcast, not an orchestrator: it holds no plan and assigns no work |
| Leases | bus.py:399-545 (`preclaim`, `conflict`, `release`), `cmd_lease` bus.py:597 | Active: claims keyed file::symbol, per task; this is the blackboard's lock half and works |
| Second: `plan`, `cheapest`, `rival` | plugin/lib/sampo/second.py:72,104,164 | `rival` ran 140 times |
| Stances (`ROLES`: pre-mortem, naive, prosecutor, devil, defender) | second.py:19-35 | one model, five prompts; no data of use found beyond the tasks that ran them |
| Dissent: `task_dissent add|resolve` | second.py:233-270 | 1,148 resolve events; each objection must be answered in the brief before the plan gate passes |
| Debate: confirm/refute a saved review | second.py:273 (`debate`) | 8 `second_session` events; debate itself is rarely invoked |
| Protocol panels by tier | plugin/protocols.json (S none, M rival when race-bar, L `second plan` before approval) | enforced by the plan gate (T-2341) |
| Andon | lanes.py:181-187 (`lane andon`), core.py:4880 (reads open ANDON.md) | 22 task files mention an andon out of 2,741 (0.8%); 149 lanes created |
| Kalevala towers | plugin/lib/sampo/kalevala.py (manifest, per-tower gen-N, restart) | 26 towers; 31 `kalevala_act` events (restarts of stalled towers); a Sonnet critic gates generations: 595 verdicts, 527 pass, 65 fail (11%), 3 Haiku pass |
| Hollow-return tripwire | hooks.py:5389-5396 | a char-count check on builder and reviewer returns: 153 of 1,933 subagent returns (7.9%) came back under the size bar. It is a cheap check on a reply, not a model |
| Builders in lanes | `lane_builder` 252, `lane_new` 149 | the real multi-agent path: main thread dispatches, lanes isolate by worktree, results come back through the brief |

Reading: Sampo already is a two-level hierarchy (main thread, lane builders) plus a plan panel (second) plus a critic gate
(Kalevala). What it lacks is any agent-to-agent channel that is used, and any measurement of whether the panel changes outcomes.

## 2. Measurements from Sampo's own data

- **Rival read changes plans.** 143 tasks carry answered rival items; 106 (74%) adopted at least one. Item level: 429 adopted,
  107 taken, 14 Taken (about 550) against 277 rejected and 96 ruled-out, 13 not-taken, 6 deferred (about 57% adopted of ~960).
  Cost: one Sonnet call per task, 140 runs. Caveat: "adopted" means the plan text changed, not that the outcome improved.
- **Dissent is answered, not ignored:** 1,147 resolves to 1 add (the review panels add through `rival`/`second plan`, which write
  the items; humans rarely add).
- **Reply tripwire catches:** 153 hollow returns of 1,933 subagent returns (7.9%), from a character count alone.
- **Critic on generated work:** Sonnet fails 11% of tower outputs (65 of 592). That is a second-model check already paying off.
- **Peer messaging today:** 30 bus messages in 23 days, almost all main-to-lane. Demand for lane-to-lane is not yet measured
  because no verb makes it easy (the A/B in slate item 2 creates the measure).
- **Andon:** 22 of 2,741 tasks; the rare path, working as an exception.
- **Conductor steer:** 1 `steer` message in the bus log, 172 `steer_expand` events (those are the user's steers, expanded, a different path).

## 3. Architectures

Each: when it helps, cost per task, failure mode, evidence.

1. **Hierarchy of conductors (orchestrator of orchestrators).** A top conductor splits an L goal and assigns sub-conductors, each
   running its own lanes. Helps when a goal splits into independent, wide subtrees (a repo-wide migration) and one context can't
   hold them. Cost: an extra planner call per level plus the sub-summaries re-read upward; Anthropic reports multi-agent
   research uses about 15x chat tokens (4x for a single agent) ("How we built our multi-agent research system", 2025).
   Failure: lost context at each hand-off and conflicting edits; Cemri et al. 2025 ("Why do multi-agent LLM systems fail?", MAST)
   put coordination and verification failures at the top; Cognition's "Don't build multi-agents" (2025) makes the same case.
   Sampo number: the main thread already runs 149 lanes with one level and 7.9% hollow returns; a second level would multiply that
   exposure. Verdict: do not build; the lease and lane train already cover the parallel-edit risk.
2. **Peer messaging between lanes.** A lane asks a sibling a question or announces an interface it changed. Helps when two
   lanes touch a shared seam (the lease conflict is the signal). Cost: a bus line (about 100 tokens) plus a read at the next
   tool call, `bus.unread` already does that. Failure: chatter and a lane that obeys a sibling's wrong note; mitigation: kind
   `note` is data, never an instruction, and only sent on a lease conflict. Evidence: Claude Code's agent-teams docs (teammates
   message each other directly, experimental) and Sampo's 30 notes, all one-way. Verdict: build small, measure.
3. **Calibrated mixed-model council.** Several models (Opus, Sonnet, Haiku, Jarvis local) vote, weighted by their measured
   hit rate per question kind. Helps on judgment calls with no proof (picking an approach). Cost: N calls; PoLL (Verga et al.
   2024, "Replacing judges with a panel of LLM evaluators") reports a panel of smaller models from different families beat one
   large judge at about a seventh of the cost. Failure: correlated errors when members share a family, and a vote with no
   ground truth. Sampo number: `rival` adopted at 57% shows panels change plans but no record ties that to the outcome; the
   council needs the outcome label from `closes.jsonl`. Verdict: after item 1, because it needs the calibration it produces.
4. **Small-model tripwires on frontier replies.** A Haiku or local model reads each builder/reviewer return (and each
   Claude reply on a risky step) for named failures: claims with no evidence, a criterion not mentioned, "should work" wording,
   files outside scope. Helps because the cheap check runs on every return, not on a sample. Cost: one Haiku call, about 1/10th
   of the Sonnet price of the work it checks; an exact figure waits for `sampo cost`. Failure: false alarms train the reader to
   skip it (so a flag only informs; "models advise, proof decides"), and small models are poor judges of subtle reasoning
   (Huang et al. 2023: LLMs cannot self-correct reasoning without external feedback). Sampo number: the char-count tripwire
   already flags 7.9% of returns; the Kalevala critic fails 11% of generations. Verdict: build first, cheapest.
5. **Verifier-picked best-of-N with an escalation ladder.** Run the cheap model first; escalate to a stronger one only when a
   verifier (the task's own tests or `sampo check`) fails; pick the best of N when tests can rank. Helps where proof exists:
   FrugalGPT (Chen et al. 2023) reports matching GPT-4 with up to 98% lower cost; self-consistency (Wang et al. 2022) improves
   reasoning with sampled paths; RouteLLM (Ong et al. 2024) routes with a learned judge. Cost: expected 1.3 to 2 cheap calls,
   with a strong call on the fraction that escalates. Failure: a weak verifier lets a wrong answer through; N copies of the same
   mistake. Sampo number: `models` and routing exist (route.json, model-routing rule); no escalation record joins a first try's
   failure to the retry's model. Verdict: measure on the Louhi exam, where the hidden checks are the verifier.
6. **Blackboard on the brief.** All agents read and write one shared artifact (the brief's sections, Dissent, Log) rather than
   messaging. Helps because it is auditable and survives a crash; the brief already is one (Dissent, Evidence, Log). Cost: near
   zero. Failure: write races on one file (110 race/lock defects reviewers found in 30 days, per the builder brief) and
   unread notes. Sampo number: 1,147 dissent resolves written through `cli.mutate` atomically show the path works. Verdict: it is
   the default already; the gap is a lane-visible Notes section sibling lanes can read.
7. **Debate between stances (multi-agent debate).** Two or more instances argue, then revise. Du et al. 2023 (multiagent
   debate) report gains in factuality and arithmetic reasoning. Helps on factual or reasoning claims with a hard answer.
   Cost: N x rounds calls. Failure: the group converges on a confident wrong answer; Sampo's own `debate` exists but ran in few
   sessions (8 `second_session` events). Verdict: keep `rival` (cheaper, 74% of tasks adopt something); try debate only on L
   reviews, A/B against a single reviewer.
8. **Tower-as-peer (Kalevala generates, a council critiques).** Already running: 26 towers, 595 verdicts. Its lesson is that a
   different, cheaper critic gates a generator at 11% rejection. Extend the pattern to builds, which is architecture 4.

## 4. Answering the question

- Orchestrators managing orchestrators: not now. Evidence is against more levels (coordination failure, cost multiplier) and
  Sampo's lane-train and leases already handle the one problem a second level would solve.
- Agents messaging each other: yes, small and only on a lease conflict, as data notes.
- Models checking each other: yes; Sampo's own 11% critic fail rate and 7.9% hollow returns show the check finds things.
  Constraint: advise only; proof decides.
- Several viewpoints: already present as stances and `rival`; adopt-rate 57% says they change plans. What is missing is the
  join to outcomes, so the first slate item adds it.

## 5. Slate (cheapest first), each with an A/B

1. **Reply tripwire reader on builder/reviewer returns** (Haiku, advisory finding). A/B: over the next 100 returns, compare the
   share of builder returns later reopened or fixed (closes.jsonl) between flagged and unflagged; also precision of flags by a
   Sonnet re-read of 20.
2. **Lane-to-lane note on a lease conflict.** A conflict blocks today; instead send a `note` to the holder lane and surface it
   in its next tool call. A/B: on real tasks, lane blocks resolved without the main thread, and time from block to resolution,
   versus the current baseline from `guard_block`/lease events.
3. **Outcome join for panels.** Record which rival/second items were adopted and whether the task's close needed a fix later;
   gives the calibration the council needs. A/B: fix-after-close rate for tasks with adopted rival items versus tasks that
   skipped the panel (S tier). Zero model cost.
4. **Escalation ladder on the Louhi exam.** Haiku first, Sonnet on failed hidden checks, Opus on a second failure; compare to
   all-Sonnet on the held-out third: pass rate and tokens per pass.
5. (Hold) Calibrated council and an orchestrator-of-orchestrators wait for the data of 1 and 3.

## 6. Weaknesses of this note
- Adoption counts text change, not benefit. Cited figures are from my memory of the papers and docs, not re-fetched this run.
- Counts are for foreman-4a056f; other projects were not read.

```intake
FEATURE: Reply tripwire reader: a Haiku pass over every fm-builder and fm-reviewer return, logging a finding with kind tripwire (advisory, never gates) for claims without evidence, a criterion not mentioned, or files outside scope. MUST: advise only, proof decides. DONE-WHEN: a test plants a hollow return and a no-evidence return and both are flagged, a clean one is not; the A/B (reopen rate of flagged vs unflagged over 100 returns) is written by a verb. @plugin/lib/sampo/hooks.py
FEATURE: Lane-to-lane note on lease conflict: when preclaim/conflict names a holder lane, send it a bus note (data, rate-limited to one per pair per 10 min) and show it in its next unread. DONE-WHEN: a test shows two lanes, a conflict, the holder reads the note once, the sender can see it was read; A/B measure: lane blocks resolved without the main thread, and minutes to resolve, from guard and lease events. @plugin/lib/sampo/bus.py
FEATURE: Panel outcome join: log, per task, rival/second items adopted and whether its close was later fixed or reverted; a verb prints fix-after-close rate for panel vs no-panel tasks. DONE-WHEN: a test over a fixture ledger prints both rates and the n of each; no model call. @plugin/lib/sampo/second.py
RESEARCH: Escalation ladder on the Louhi exam: Haiku, then Sonnet on a failed hidden check, then Opus; compare with all-Sonnet on the held-out third. DONE-WHEN: a note reports pass rate and tokens per pass for both arms with n per arm, and a verdict on routing. @plugin/lib/sampo/louhi.py
```

Intake block above was briefed as T-3949, T-3950, T-3951 and T-3952.
