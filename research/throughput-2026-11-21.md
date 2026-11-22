# Throughput, tokens, test CPU and failures: measured 2026-11-21 (T-5488)

The user's words, 2026-11-21: "how can we improve it even more? so shit isn't constantly just running, like running as
few as possible, also massively increasing output for tokens, reducing failures, etc. and just getting shit done
quicker, because stuff still takes forever from when I request it to it being done" and "should we create a lot more
types of subagents? … why are subagents still containing "fm" in the name? … and also "general-purpose"?"

All numbers come from read-only scripts over the ledger, the run-cache records and the subagent transcripts
(scripts in the operator's ops folder: latency.py, agent_tokens.py, agent_jobs.py, run_stats.py, cache_shape.py,
docstring_churn.py, queue_guard.py).

## Request to done (210 tasks closed in 7 days)

| group | n | wait to start (median / p90 h) | work | land to close | total |
|---|---|---|---|---|---|
| source user | 79 | 0.0 / 245 | 2.0 / 65 | 0.3 / 64 | 9.8 / 284 |
| source discovered | 89 | 2.7 / 103 | 5.4 / 292 | 0.4 / 2.7 | 14.1 / 294 |
| priority urgent | 22 | 0.5 / 26 | 4.5 / 24 | 0.2 / 3.3 | 7.8 / 33 |
| priority normal | 188 | 1.2 / 182 | 5.4 / 279 | 0.6 / 2.8 | 21.5 / 295 |

- A builder's own run is 40 minutes (median), yet "work" is 2 to 5 hours: the time goes to hand-offs that wait for the
  session's next turn (launch, review, review fixes, recover, land, close).
- The queue holds 324 tasks, 317 marked source user: every intake line counts as the user's, including tasks Sampo
  derived itself, so the user's own asks can't be told apart or put first.
- p90 of 10 to 12 days: a normal task behind 300 others waits for days unless marked urgent.

## Tokens (4 days of subagent transcripts)

| agent type | runs | median turns | minutes | cache read | cache write | output | per run |
|---|---|---|---|---|---|---|---|
| fm-builder | 158 | 80 | 40 | 3.4M | 324k | 50k | 37 Bash, 12 test runs, 17 Edit, 12 Read |
| fm-reviewer | 190 | 32 | 2 | 1.2M | 154k | 37k | 13 Grep, 10 Read |
| general-purpose | 362 | 14 | 2 | 656k | 49k | 5k | 331 were workflow stages, the rest ad-hoc planning, triage and fix jobs |

- general-purpose is Claude Code's built-in type: those 362 runs ran on the session's model (Opus) with no Sampo rules.
- A builder's 80 turns each re-read its whole context; exploration (Read, Grep, Bash) is about half the turns.

## Tests

- The behind verify ran the whole suite 7 times in 24 hours, 4 uncached (fixed: T-5482); the hourly land ran its own
  uncached suite behind it (fixed: T-5483).
- 12% of the 2,517 cached classes read the whole tree ("*") and rerun on every change.
- 56% of changed module files change the module body hash, which reruns every class that imported the module.
  Docstrings are not the cause (56% without them too); counting each undecorated def as its own unit instead of part
  of the module body brings it to 35%.

## Failures

- 237 guard blocks in 7 days (system 79, brief 71, core 60): each costs at least one turn and a retry.
- Recovered builders re-authored their lanes (fixed: T-5475); map-file conflicts parked lanes (fixed: T-5472); builders
  stopped each other on shared files (T-5479).

## Plan, ranked by measured cost

1. The user's own asks first: a task carries whether the user said it (verbatim) or Sampo derived it; the queue puts
   the user's asks ahead, oldest wait first.
2. An event-driven lane: a builder's return starts its review, review fixes go back to the same builder (T-5477), a
   clean review lands it, the land closes it; no step waits for the session's next turn.
3. Builders write their CHANGELOG line and close notes in the branch, so a land closes at once (land-to-close p90 64 h).
4. Every agent Sampo or a workflow starts names a Sampo type and a model fit for the job (Haiku lookups and close prep,
   Sonnet building and review, Opus only for art and UI); a workflow stage with no model is flagged before it runs.
5. Builder briefs carry the code they need (the functions to change, the nearest test to copy, the one command to run
   while iterating), so builders explore less; reviewers get the diff and touched functions inline.
6. Small related tasks go to one builder (T-5478).
7. Test cache: undecorated defs are their own units (56% → 35% of module edits rerun every importer); the whole-tree
   readers narrowed (T-5484); one machine test budget (T-5485); shared Rust builds (T-5486).
8. Guard blocks become fixes where the fix is safe and known (the top three categories), so a block costs no turn.
9. Agents renamed without the old Foreman prefix: sampo:builder, sampo:reviewer, sampo:recon, sampo:scout,
   sampo:debugger, sampo:reproducer, the old names kept working for running sessions.
10. New agent types only where a job is frequent, narrow and cheaper on a smaller model, each measured against what it
    replaces: a planner (brief drafts), a closer (CHANGELOG, audits, close prep on Haiku), a conflict resolver for
    parked lanes, per-lens reviewers (edge and adversary on Sonnet, maintainer and docs on Haiku).
