# Mission 6 — self-triggering (gen 4, #11): the night shift runs on the local models

Task: T-2916 (brief 1).

Parent: gen-4/seed.md row 6 → gen-0.md #11: "Self-triggering. Schedules, file and event triggers, CI and issue
watchers, so work starts without me. Installs (timers, units) get queued for one yes." gen-0's goal, MUSTs and NEVERs
are quoted in the seed and apply here unchanged.

## Capability
Work that starts by itself and costs nothing. The schedule is there: `systemctl --user list-timers` (2026-11-04)
shows foreman-night-foreman-4a056f firing nightly at 02:30 and foreman-tick-foreman-4a056f hourly. Both were
installed through the one-yes queue. What the night does is the gap. `sampo night --dry-run` plans 10 jobs tonight,
about $1.80 of a $2.00 limit: eight free chores with no model (scout, dream, cold files, guard cases, steer checks,
compost, consolidate, sentinel) and two paid calls (landscape, queue). It skips the court for money. The local models
sit idle all night, though the user asked for them to be used "without limit" for volume (standing steers a and b).
The work they are best at is already built and routes local first: Moons grow and the Moons funnel. The catalogue
stands at 4,879 candidates against the 10,000 target (`sampo moons coverage`), and it grows only when a session runs
it by hand.

## Grounding (each read in the code on 2026-11-04)
- plugin/lib/sampo/night.py:51 `jobs(p)` lists tonight's (name, argv, USD) jobs. No job calls a local model; the only
  model work is paid (`second session`, `landscape`, `model lesion`, `pre-plan`, `research debt`, `court`, `evolve
  candidate`, `queue`, `draft`).
- night.py:553-585: `cmd_night` (night.py:483) fits jobs under the limit, then runs them one at a time as `fm`
  subprocesses with the breaker (`serve.FAILS_MAX`, night.py:575), the usage check (night.py:569), the panic check and
  a heartbeat. Each job gets `JOB_TIMEOUT` (night.py:25, 3 hours). A $0 job always fits.
- plugin/lib/sampo/moonscale.py:428 `grow` runs `workers` calls at once (default: the local profile's slots) and stops
  at its target, its call cap, the budget or a run of failures. moonscale.py:834 `funnel` runs the exact, embed, kill,
  tier and novelty stages. All of them ask through moonscale.py:397 `_ask`, which tries the local profile first and
  falls back to Haiku with no way to refuse the fallback. That fallback is what a night must not take silently.
- moonscale.py:379 `_local`: own or loopback profiles only, loopback only for a sensitive project. Night already
  refuses to run anything in a sensitive project ("sensitive project: nothing runs at night").
- `sampo moons coverage` (2026-11-04): "Kept by model: haiku 545 of 1499, a large local model+nothink 2753 of 3316, a large local model+think
  64 of 64". The local model's candidates survive the write-time rules far more often than Haiku's, so local-only
  costs no quality here.
- The trigger table (`sampo triggers`): the `night` trigger has fired once. The schedule exists, and almost nothing
  rides it.

## Gaps this Mission targets (coverage: #11)
- operate × headless/server: a nightly job that uses the machine's own models with no session and no spend.
- build × headless/server: the Moons catalogue grows toward its target without a person starting it.

## Ideas (from the brainstorm, grounded)
fm mission, then fm ideas --rounds 4 --falsify --crossbreed --seen-latest 6 (research/brainstorm-20261104-003735: 349
distinct ideas; --deepen dropped by today's risk budget, recorded). Grounded:
- "Route background volume to the local model first" (round 1): new for the night run, built for Moons. Brief 1 joins
  them.
- "Stop the night on spend and disk, not only on usage limits": built near T-1476. The night stops at its spend limit
  and on usage (night.py:563-571). Disk is not a stop condition. Held.
- "Wild: Yield the GPU the moment the user shows up" and "Start and stop the night on logind lock and unlock": new.
  The night runs at 02:30, so a user at the machine is rare. Held for the next #11 Mission, with "Mark every
  local-made result unwitnessed until verified" (the funnel's verdicts already carry their model).
- "Write a sealed night plan and a morning card graded against it" (round 4) and the cross-bred "Supervise the night
  from outside with typed stops and catch-up": partial. The night ledger event records ran, skipped and stopped. A
  graded morning card is the next step once the night does real work.

## Approaches
1. Two $0 jobs, `moons grow` and `moons funnel` with call caps and `--local-only`, listed only while a local model
   answers and the store is under target. Likely outcome: about two thousand candidates a night, funnelled, at $0.
   Fails if: the local server drops mid-run; local-only stops the job with its reason, still at $0.
2. use what exists: run `moons grow` by hand. Fails if: it needs a session, which #11 exists to remove.
3. do less: the grow job only. Fails if: unfunnelled rows pile up as filler.
4. A separate local-model timer. Fails if: it is a new install waiting on a yes, and it duplicates the night's
   safeties.

Choice: 1 (75%).

## Exam (an outcome measure, with a live reading)
- Outcome measure: the catalogue's size and funnel counts in the morning, read from `sampo moons coverage`, against
  4,879 candidates and 3,362 kept today. It is read after a night has run (the window rule), and paid spend on the
  `moons` and `moons-funnel` features that night is $0 (`sampo cost`).
- Live reading at close: `sampo night --dry-run` lists both jobs at $0.00 with the local model up, and a run with the
  model stopped lists neither.
- Exam kinds: #11's trigger scenarios are unchanged. A machine's state (a model up or down) is a unit test, not an
  exam variant (the self-triggering critique's rule).

## Long-horizon goal (end of run)
The user wakes to measured work the machine did on its own models: more candidates, funnelled, with the night's
ledger saying what ran and why it stopped.

## Risks
- Spend leak through the fallback: the reason for `--local-only`. A test plants a dead local profile and asserts no
  Haiku call.
- GPU contention with the bench or Smith: night jobs run one at a time, each with a call cap.
- Catalogue quality: the funnel job runs after grow in the same night, and the write-time rules still reject
  internal-only and untestable rows.
- Privacy: a sensitive project runs nothing at night (existing), and local means own or loopback only.

## Lean
No new verb, no new unit, no hook. Two job rows and one flag.

## Tier share
Brief 1 is tier 3: self-triggered work on the machine's own models, the user's standing ask.

## Hands to the next generation
The night's growth per run and its stop reasons. Next on the same rail: T-2910's local brainstorm as a night job for
the next Mission's ideas, the model bench rerun nightly (T-2886), and the graded morning card.

## Done when
T-2916's three criteria are green, one real night has run both jobs (its ledger event read in the critique), and
MASTER.md (fmnight, Moonscale) and CHANGELOG say it.
