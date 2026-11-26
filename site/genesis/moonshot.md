# Moonshot catalogue

### M-01 · Deliver a product from one paragraph at a fixed, warrantied price
- Tier: massive
- Domain: freelance contracting and insurance
- Lens: crossbreed: markets and auctions (Quote before building) × bold bets (Take a product request to shipped, under a warranty)
- What: `fm deliver "<paragraph>"` returns a price, a date and a p90 overrun from the user's own calibrated record, plus the acceptance proofs it will hand over. On a yes it specs, builds, deploys and watches the app for 30 days under a hard cap; an overrun is booked as Foreman's visible loss, and a warranty claim inside the window reopens the work at no charge and refunds that share of the budget.
- Prior art: Devin, Replit Agent, Lovable and Bolt build apps from prompts with no price or guarantee; agencies quote fixed bids by hand; Foreman's `fm queue --cost` (T-1878) quotes tokens as a median, with no band and no commitment.
- Better by: the price rests on a backtested band from this user's history and the guarantee is enforced by proofs and a claims ledger, so the scoping, delivery and support work of a small agency is done and priced by one harness.
- Novelty: 4
- Needs: M-13, M-16, M-17, M-20
- Unblocks: none
- Kill: Quote, then build, one CRUD app (a todo API with auth, about ten endpoints) from a one-paragraph request; pass if it lands under its quoted p90 cost with every acceptance proof green in a clean container, fail if either misses.
- Stage: idea

### M-02 · Keep code as a regenerable cache of intent clauses and proofs
- Tier: massive
- Domain: manufacturing (the CAD file is the source, the printed part is a cache)
- Lens: crossbreed: 2030 worked back (Make source the intent and the code a regenerable cache) × science itself (Prove a quote against a reference implementation)
- What: A module's source of truth becomes its intent clauses (the standing intent and the steer checkers of T-1063) plus its stored proofs: tests, hunk-mutation proofs and differential twins. `fm regen PATH` deletes the module, rebuilds it on any model or language from those alone, and lands it only when every proof passes and the twin agrees on recorded inputs; run as a standing check, the round trip shows where the proofs are too weak to pin behaviour.
- Prior art: spec-driven development (GitHub Spec Kit, Kiro specs), model-driven code generation from UML, literate programming; all use the spec as a one-time input to generation.
- Better by: regeneration is a gate, not a generator: a round trip that must pass every proof measures whether intent and proofs really pin the code, which no spec-first tool checks, and it makes model and language changes routine.
- Novelty: 5
- Needs: M-18
- Unblocks: none
- Kill: In the T-1232 spec-suite toy repo, delete three modules and have a headless `claude -p` rebuild each from its briefs and tests alone, two attempts each; pass if at least two of the three pass every original test, fail otherwise. → viable: 3 of 3 modules (semver, table, ranges) rebuilt blind by a tool-less child passed every original test (T-2679). Caveat for the slice: the spec suite's specs are complete by construction; the slice must regenerate a module of Foreman's own, whose spec is its briefs.
- Evidence: T-2679 (the kill run's output is its step 1 evidence)
- Stage: kill

### M-03 · Run an owner-less project under a charter until an heir takes over
- Tier: massive
- Domain: law (wills, trusts and dead man's switches)
- Lens: crossbreed: 2030 worked back (Appoint an heir and keep a dead man's switch) × bold bets (Take over an open-source maintainer's inbox; Dependency upgrade autopilot)
- What: When the owner is silent past N days, the project drops to a written charter: Foreman reproduces each new issue as a failing test and lands the clear fixes, bumps dependencies with call sites repaired and proven, publishes an honest status page, and refuses anything outside the charter. After a second deadline it hands the keys, the ledger and the open work to a named heir.
- Prior art: Dependabot and Renovate bump versions without repairing callers; GitHub's Copilot coding agent works issues a person assigns; Google's Inactive Account Manager hands over data, not upkeep; Foreman's watchers (T-2356) start work from CI and issues while the owner is present.
- Better by: the project keeps shipping proven fixes with nobody assigning work, under authority bounded by a charter and expiring tokens rather than the owner's credentials, which today takes a maintainer team or a foundation.
- Novelty: 4
- Needs: M-15, M-16
- Unblocks: none
- Kill: In a local fixture repo with a one-page charter and 20 seeded issues (the T-1232 spec-suite kind), run fm night unattended for seven nights with no owner input; pass if at least 12 issues close with proofs that still hold under fm outcomes a week later and no close breaks a charter line, fail otherwise.
- Stage: idea

### M-04 · Own the pager for insured crash classes, from trace to proven fix
- Tier: massive
- Domain: insurance underwriting and site reliability engineering
- Lens: crossbreed: 2030 worked back (Own the pager: turn production crashes into failing tests and fixes) × markets and auctions (Issue warranties on landed commits)
- What: A crash feed (a log file, a Sentry export, a pasted trace) becomes a brief whose first step is a test, rebuilt from the trace, that fails; the fix is proven against it and against recorded production inputs through a differential twin, then canaried with automatic rollback. Foreman publishes the crash classes it is insured for, those whose warranty claim rate stays under a bar, and runs those with no human, while the rest wait for one.
- Prior art: Sentry Seer autofix and AI on-call agents (Cleric, Resolve AI, Datadog Bits AI); Foreman's `fm capture --incident` (T-2388) runs a signal command but neither reproduces from a trace nor grants autonomy per class.
- Better by: autonomy is granted per crash class by a measured claim rate, and no fix lands without a reproduction that failed first, so on-call becomes an insured service rather than a rota.
- Novelty: 3
- Needs: M-15, M-16, M-18
- Unblocks: none
- Kill: Take Foreman's last 30 FIX tasks whose request holds a traceback and have a headless run turn only the trace into a test at each task's start snapshot (`fm at`); pass if at least 10 fail there and pass at the fix commit, fail below.
- Stage: idea

### M-05 · Merge any agent's pull request on rerun proofs, not prose review
- Tier: massive
- Domain: law (evidence and chain of custody)
- Lens: crossbreed: inversion (Review any agent's PR with plan, proof and guard checks) × science itself (Replicate closed tasks with an independent agent)
- What: A CI app takes a PR from any agent or person, finds or derives its brief, reruns each claimed proof in a clean container, mutation-grades it, scans the diff for guard-class effects and posts one signed check. PRs whose proofs grade strong, from authors whose forecast-weighted record clears a bar, merge with no human reading; the rest list exactly what failed or was never proven.
- Prior art: CodeRabbit, Copilot code review and Graphite's agent comment in prose; merge queues require green CI; SLSA attests builds, not claims; Foreman's `fm audit scan` and `fm pr` work only inside its own sessions.
- Better by: it checks the author's claims by re-execution and grades their strength, for any harness, so review of agent output stops scaling with the number of agents.
- Novelty: 4
- Needs: M-12, M-17
- Unblocks: none
- Kill: For the builder-lane merges of the last 60 days, rerun each one's criteria and `fm task prove --hunks` at its merge commit and compare the grade with `fm outcomes` (reverted or fixed later vs held); pass if the grade separates them with an AUC of at least 0.7, fail below.
- Stage: idea

### M-06 · Predict a policy change's effect from the ledger before running it
- Tier: extreme
- Domain: econometrics (off-policy evaluation and synthetic control)
- Lens: crossbreed: 2030 worked back (Replay the ledger under a changed policy) × science itself (Estimate causal effects of past features by synthetic control)
- What: `fm twin --policy "<change>"` (a guard rule, a model, a budget, a gate) selects the logged tasks the change would have touched, reweights their outcomes with off-policy estimators against a synthetic-control baseline, and prints predicted close rate, spend and claims with an interval. Every self-change, model switch and budget move is quoted by the twin first and scored against what happened; no current harness can predict the outcome of changing its own policy before trying it.
- Prior art: off-policy evaluation in recommender systems and reinforcement learning; Foreman's `fm backtest` (would a rule fire), `fm replay` (guard verdicts) and `fm bench` (reruns cases at full model cost).
- Better by: it predicts outcomes rather than firings, costs no model calls, and carries its own Brier score.
- Novelty: 5
- Needs: M-16
- Unblocks: none
- Kill: For T-2353 (the router picks the cheapest model that holds), give the twin only the 60 days before it and predict close and cost for the 20 tasks after; pass if its Brier beats the pre-change base rate by at least 0.03 and its cost error is within 30%, fail otherwise.
- Stage: idea

### M-07 · Forecast when each gate stops paying, and retire it on evidence
- Tier: extreme
- Domain: ecology (extinction forecasting) and law (sunset clauses)
- Lens: crossbreed: inversion (Run the necessity corpus on every new model ID) × ecology and evolution (Run a digital terrarium that forecasts which verbs and rules go extinct)
- What: Each new model ID replays the gates' past catches (guard blocks, failed proofs, plan rejections) with that gate off; every gate carries a retire-when predicate and a fitted decline curve, and `fm delta --forecast` prints when each gate's measured value, and Foreman's whole delta, reaches zero. A gate that meets its predicate drops to shadow mode with a necropsy record and a one-command revival; no current harness measures, on the user's own history, which of its safeguards a new model no longer needs.
- Prior art: ablation studies; Foreman's hook-feature ablation in `fm bench duel` (T-1375) and `fm evolve`'s instruction ablation, both started by hand and run once.
- Better by: it runs on every model change, per gate, with a forecast that is scored later and a retirement that can be undone.
- Novelty: 4
- Needs: M-19
- Unblocks: none
- Kill: Take 20 plan-gate rejections and 20 failed-proof catches from the last 60 days and replay each as a bench case with the gate off, on the current and on the previous model; pass if the unaided miss rate differs between the two models by more than its 95% interval for at least one gate, fail if it does for none (nothing to forecast).
- Stage: idea

### M-08 · Pool self-trials across installs; import only what replicates
- Tier: extreme
- Domain: medicine (meta-analysis) and microbiology (horizontal gene transfer)
- Lens: crossbreed: science itself (Pool effect sizes across consenting users' projects as a meta-analysis) × ecology and evolution (Share lessons between installs by signed horizontal gene transfer)
- What: Each consenting install randomizes its own practices (plan review, a guard rule, a lesson) across tasks and publishes only effect sizes with intervals; pooled estimates become the priors behind new installs' defaults. An imported lesson or rule arrives as a signed packet, enters dormant and activates only after it replicates on the importer's own replays; no current harness sets its defaults from randomized evidence pooled across users while every user's code stays local.
- Prior art: federated learning (shares gradients, not effects), vendors' A/B telemetry (the vendor decides), Cochrane reviews, shared rule packs and AGENTS.md templates with no evidence behind them; Foreman's `fm home export` moves one user's own state.
- Better by: evidence is randomized at the source, pooled with its heterogeneity shown, and must replicate locally before it acts.
- Novelty: 5
- Needs: M-17, M-19
- Unblocks: none
- Kill: From this install's ledger, estimate the effect of `fm second plan` on the fixed-later rate of M tasks with a 95% interval, then compute how many installs of this volume a pooled estimate needs to resolve a 5-point effect; pass if 200 or fewer, fail above.
- Stage: idea

### M-09 · Settle work between strangers by escrow released on replayed proof
- Tier: extreme
- Domain: law (escrow) and labour markets
- Lens: crossbreed: inversion (Act as escrow between a client and an agent) × markets and auctions (Stage a bounty board with human prices)
- What: A client writes a contract file with acceptance proofs (some held back until settlement), a price and a deadline, and any agent, Foreman-driven or not, attempts it under scoped tokens. `fm contract settle` releases the escrowed budget only when every proof, the held-back ones included, passes in a clean container on the delivered commit, and writes a signed settlement both sides can verify; no current harness acts as a neutral settlement layer that pays on machine-checked acceptance.
- Prior art: smart-contract escrow (not tied to test evidence), bounty platforms (Algora, Gitcoin) settled by a maintainer's approval, Upwork escrow released by a person; Foreman's `fm contract` (T-1158) bounds a run's time and spend, not a payment.
- Better by: acceptance is fixed before work starts and settled by re-execution with held-back proofs, so neither side judges and gaming the visible tests does not pay.
- Novelty: 4
- Needs: M-15, M-17
- Unblocks: none
- Kill: Write one contract (a CSV export with five visible and five held-back tests in the T-1232 spec-suite repo) and run two agents, one honest and one told to special-case the visible inputs; pass if settlement pays the honest one and refuses the gamed one, fail if the gamed one is paid.
- Stage: idea

### M-10 · Detect when a mechanism needs redesign, not another patch
- Tier: extreme
- Domain: ecology (early-warning signals before a regime shift) and history of science (Kuhn's anomalies)
- Lens: crossbreed: science itself (Detect paradigm shifts from accumulated anomalies) × ecology and evolution (Meter the error threshold for self-edits)
- What: Foreman tracks, per mechanism (the guard's shell parser, the hook path, the router), its stream of patches, re-fixes, overrides and flaky failures, and watches for the signals ecologists read before a collapse: shrinking intervals between patches, each fix holding for less time, rising variance. Past a threshold it files a redesign proposal naming the replacement class instead of one more patch; no current harness can tell that its own design, not a bug, is the cause.
- Prior art: Kuhn's anomaly accumulation; critical-slowing-down indicators in ecology and climate science; Foreman's recurring-failure capture (T-2272), which files one root-cause FIX per signature and stays at the patch level, and the deferred incident ledger (T-2404).
- Better by: it reads the trend across many patches to one mechanism, so it can say "replace" before the next failure rather than after the tenth.
- Novelty: 4
- Needs: M-19
- Unblocks: none
- Kill: Run the detector over 12 months of Foreman's own patches per module; pass if it flags the guard's shell parser (T-0170 to T-0190, T-1928, T-1937, T-2629) before T-2668 while flagging at most 2 of the next 10 most-patched modules, fail otherwise.
- Stage: idea

### M-11 · Auction the queue by calibrated yield and Kelly-size each stake
- Tier: xhigh
- Domain: auctions and portfolio theory
- Lens: crossbreed: markets and auctions (Size budgets with Kelly sizing) × bold bets (Auction the queue by expected yield per dollar)
- What: Each queued task bids its calibrated chance of a strong close times its value, divided by its predicted cost; a run funds the best bids first, and each task's budget cap scales with its calibrated edge, so uncertain work gets a small stake and an early stop. Bids, awards and realized yield per dollar print on the run's receipt.
- Prior art: Kubernetes priority classes and Mesos DRF; Foreman's inbox ranking (fixed weights for who asked, waiting time and tier, T-0111), `fm run --fit` and `--stall-usd` (T-2669).
- Better by: order and stake come from the user's own calibrated record instead of fixed weights, and the policy is backtested before it runs.
- Novelty: 3
- Needs: M-20
- Unblocks: none
- Kill: Replay the last 100 closed tasks with a budget of half their real spend, each bid computed only from data before it; pass if bid order funds at least 15% more closes than the real order and no task that closed would have hit its Kelly cap, fail otherwise.
- Stage: idea

### M-12 · Weight deciders by a scored market on whether a close survives
- Tier: xhigh
- Domain: prediction markets
- Lens: crossbreed: markets and auctions (Open a forecast market on task outcomes) × bold bets (Run a prediction market among deciders)
- What: Before a close, the planner, a cold rival read, the guard, the prover and a base-rate model each post a probability that the work survives its warranty window. Log-scored payouts set each decider's weight and trust budget; a pooled forecast below a bar sends the close to independent replication or the user, and above it the close stands alone.
- Prior art: Metaculus, Hanson's idea futures, ensemble stacking; Foreman's calibration ledger (T-2337) scores single stated confidences and confidence routing (T-2338) acts on one.
- Better by: many forecasters weighted by track record replace one self-report, settled on a real outcome rather than the closing agent's own check.
- Novelty: 3
- Needs: M-16
- Unblocks: M-05
- Kill: For 40 closed tasks with `fm outcomes` labels, have three cheap forecasters read only the brief and the diff and post a probability; pass if the log-weighted pool beats the best single forecaster's Brier by at least 0.02, fail otherwise. → not-yet: two approaches, neither found a forecaster with signal to weight. Reading: a Haiku planner and a Sonnet cold rival read the brief and the diff (AUC 0.58 and 0.50). Record: the held rate of settled closes of the same type and tier, and of earlier closes that touched the same files (AUC 0.49 and 0.48). On 22 failed and 22 era-matched held closes (label: a fix or revert within 14 days), the log-weighted pool scored Brier 0.286 against the best single's 0.275, a margin of -0.011 (90% bootstrap interval -0.039 to +0.016; walk-forward 0.286). A first run that drew held closes from an older era only was confounded and is kept beside the script. Blocker: one forecaster that beats chance on survival from what is known at close.
- Evidence: T-2687 (bench/moonshot/kill_m12.py reruns the verdict from the cached answers; output in bench/moonshot/m12-result.json)
- Stage: kill

### M-13 · Price the user's attention and cap unreviewed output to it
- Tier: xhigh
- Domain: attention economics and factory work-in-progress limits (Little's law)
- Lens: crossbreed: markets and auctions (Learn the user's price from revealed preference) × 2030 worked back (Cap what a swarm can do per hour of human review)
- What: Foreman fits the user's price for an interruption and for an hour of reading from their own response times, dismissals, overrides and budget choices. A question is asked only when its expected value beats that price, otherwise Foreman decides and logs it for the digest; new lanes stop when landed-but-unread diff exceeds what the user has historically reviewed in that window.
- Prior art: interruption-cost models in HCI research, conjoint analysis, Kanban WIP limits; Foreman's doors (stop only at an irreversible, ambiguous step, T-2342) and anergy (T-2563) use fixed rules, and its budgets cap dollars, not review load.
- Better by: the threshold is learned from the user's revealed choices and checked on held-out decisions, and output is throttled to review capacity rather than spend.
- Novelty: 4
- Needs: none
- Unblocks: M-01
- Kill: Fit the price on the user's first 100 ask and decide events in the ledger and predict the next 20 (answered and kept vs dismissed or overridden); pass if it matches the user's choice more often than the fixed default does, fail otherwise.
- Stage: idea

### M-14 · Breed lane strategies by tournament, with heritable genomes
- Tier: xhigh
- Domain: evolutionary biology
- Lens: crossbreed: ecology and evolution (Run lane tournaments with heritable strategy genomes) × markets and auctions (Run a task auction between lanes and models)
- What: A brief runs in two to four lanes whose genomes differ (model, prompt variant, plan style, test-first or not, context pack size), and proof grade per dollar picks the winner. The winning genome, mutated, seeds the next brief of that task type, and a lane that builds a shared helper is credited with the downstream closes it enabled.
- Prior art: AlphaEvolve and the Darwin Gödel Machine evolve code or agents on benchmarks; Foreman's lane race (T-1069) and L plan races (T-2340) pick a winner per task and keep nothing.
- Better by: strategies are inherited and selected per task type on verified closes in the user's own repos, not chosen afresh per task.
- Novelty: 3
- Needs: none
- Unblocks: none
- Kill: Run 20 closed S tasks of one type as bench cases under two genomes each; pass if the genome that wins the first 10 cases also wins at least 7 of the next 10, fail otherwise.
- Stage: idea

### M-15 · Spawn every child with expiring capability tokens and no inheritance
- Tier: high
- Domain: finance (expiring options) and ecology (quarantining invasive species)
- Lens: crossbreed: markets and auctions (Price authority as expiring capability options) × ecology and evolution (Quarantine invasive processes: spawned children inherit no authority by default)
- What: Lanes, night ticks, fm code tools, prover children and audit children start with no push grant, credentials or ledger signing key. Each holds only the tokens its brief names, each expiring and charged to a small per-task authority budget; `fm authority` prints what every process class holds, and a use without a token is refused and logged.
- Prior art: Macaroons, object-capability systems, short-lived cloud credentials (AWS STS), Claude Code's per-subagent tool lists; Foreman's builder sandbox and outbound ledger (T-1086) and fm code's Landlock (T-1607) confine writes, not grants.
- Better by: authority is scoped per task, expires and is budgeted and logged, so an unattended run's reach is known before it starts.
- Novelty: 3
- Needs: none
- Unblocks: M-03, M-04, M-09
- Kill: Spawn each child kind in a fixture home and list the environment variables, open files and grant files it inherits; fail (nothing to build) if none holds a push grant, credential or signing key it doesn't need, pass otherwise.
- Stage: idea

### M-16 · Open a warranty window on landed work and count hand edits as claims
- Tier: high
- Domain: insurance (warranties and premiums)
- Lens: crossbreed: markets and auctions (Issue warranties on landed commits) × science itself (Infer unseen work from ledger residuals)
- What: Each landed commit opens an N-day window; a revert, a later fix, or a user's own commit touching its lines (blamed back to the task, using the signal deferred T-2647 mines) files a claim that reopens the task at top priority and is charged to the producing model and task type. Claim rates per type become premiums that routing, quotes and autonomy read.
- Prior art: SZZ studies of bug-introducing changes; Foreman's `fm outcomes` (reverted, fixed later, held) and history router (T-2291), which miss the user's quiet hand edits and set no window or price.
- Better by: it captures the rework the user does by hand, invisible today, and turns every outcome into a per-type price other mechanisms can bid against.
- Novelty: 3
- Needs: none
- Unblocks: M-01, M-03, M-04, M-06, M-12
- Kill: For the last 60 landed tasks, blame the lines changed by the user's own commits within 14 days back to their task; pass if at least 5% of tasks carry a hand-edit claim that `fm outcomes` doesn't already count, fail below.
- Stage: idea

### M-17 · Ship every close as a signed evidence bundle anyone can rerun
- Tier: high
- Domain: law (chain of custody) and software supply-chain attestation
- Lens: crossbreed: inversion (Hand the user a signed audit pack a third party can verify) × 2030 worked back (Let outsiders replay your proofs without your machine)
- What: `fm export --replay T-NNNN` writes the brief, the proof commands, a container recipe, the tree hash and the ledger slice with its hash chain and signature, in an open attestation format (in-toto). A standalone verifier with no Foreman installed rebuilds the container, reruns the proofs and checks they match the stored results.
- Prior art: in-toto and SLSA attest builds; Foreman's `fm verify-ledger` (T-1065) proves the chain locally and its proof worlds (T-2398) record interpreter and lockfile hashes.
- Better by: a third party re-executes the agent's claims instead of trusting a signature or the harness that made them.
- Novelty: 3
- Needs: none
- Unblocks: M-01, M-05, M-08, M-09
- Kill: Bundle 10 closed tasks and rerun each in a clean container with no Foreman home; pass if at least 8 reproduce their stored proof result, fail below.
- Stage: idea

### M-18 · Prove a rewrite against the old build on recorded inputs
- Tier: medium
- Domain: experimental science (control groups) and manufacturing (golden-master testing)
- Lens: crossbreed: 2030 worked back (Prove a quote against a reference implementation) × science itself (Run a standing assay on a seeded-bug repo)
- What: `fm prove --twin OLD` records inputs from the test suite, logs or a fixture, runs the old and new builds side by side and reports the first input whose output diverges. A brief marked behaviour-preserving (a refactor, port or upgrade) can't close without it.
- Prior art: differential and golden-master testing, approval tests, Twitter's Diffy; Foreman's hunk proofs check that tests notice a change, not that behaviour is preserved.
- Better by: it is a close gate on the brief and takes its inputs from what the project already records.
- Novelty: 2
- Needs: none
- Unblocks: M-02, M-04
- Kill: Plant 10 behaviour-changing refactors in fixture modules whose existing tests still pass; pass if the twin catches at least 8, fail below.
- Stage: idea

### M-19 · Hold every self-promotion to a false-discovery bar
- Tier: medium
- Domain: statistics (multiple comparisons, Benjamini–Hochberg)
- Lens: science itself
- What: Foreman promotes lessons, guard rules, trial verdicts, playbook lifts and retire proposals out of many tests run at once. Each promotion gets a q-value under Benjamini–Hochberg across its week's family, and one above the bar stays in shadow instead of acting.
- Prior art: Benjamini–Hochberg in genomics and clinical trials; Foreman's trials (T-1059) call a change "moved" when a rate halves, with no error control across the many trials graded together.
- Better by: the share of promoted lessons and rules that are noise is bounded and stated, which every later retirement and pooling step depends on.
- Novelty: 2
- Needs: none
- Unblocks: M-07, M-08, M-10
- Kill: Grade the last 100 promotions (trial verdicts, promoted lessons, playbook lifts) with Benjamini–Hochberg at q = 0.1; fail if fewer than 5 would be withdrawn (no noise to control), pass otherwise.
- Stage: idea

### M-20 · Put a backtested p90 band on every cost quote
- Tier: low
- Domain: finance (value at risk)
- Lens: markets and auctions
- What: `fm queue --cost` and `fm goal show` gain a p90 cost and finish date per task from the spread of similar past closes, not only the median. Each line shows the band's backtested coverage, the share of past tasks that landed inside it.
- Prior art: Monte Carlo burn-up charts and value-at-risk bands; Foreman's `fm queue --cost` (T-1878, a median per type and tier) and goal estimate (T-2145, a P25–P75 time range with no coverage check).
- Better by: the band's coverage is measured and printed, so a quote can be held to and priced.
- Novelty: 2
- Needs: none
- Unblocks: M-01, M-11
- Kill: Backtest the last 100 closed tasks, each quoted only from tasks closed before it; pass if at least 85% land under their p90, fail below.
- Stage: idea

### M-21 · Cut a codebase in half and grow two whole programs back
- Tier: extreme
- Domain: software regeneration and code redundancy
- Lens: living software: planarian regeneration (every fragment carries positional memory of the whole)
- What: `sampo regrow` treats a surviving fragment of a codebase the way a planarian treats a cut. It reads the wound edge as positional information: dangling imports, call sites into missing modules, type hints, fixtures, error strings, CLI help and log lines. The Smith then regrows the missing organs in a lane until the repo's hidden proofs pass again. Cut a 5k-line project along its import graph, hand the two halves to two blind lanes with no git history, no spec and no tests, and by morning there are two complete programs that each pass the original hidden suite, built differently inside. The jaw-drop is the fission demo: `rm -rf` half the tree on stage, walk away, come back to a green repo and a heat map of which surviving lines carried the most regenerative information. Sampo then keeps that minimal "neoblast" set backed up, so the organism can survive any loss.
- Prior art: M-02 regenerates code from an explicit cache of intent clauses, and this keeps no spec at all, only residue. Hole-filling completion and program repair restore a few lines. SWE-bench-style tasks fix one issue in an intact repo. Commit0 (2024) rebuilds libraries from scratch but hands the model the spec docs and the unit tests, which this withholds. Nothing measures how much of a program is redundantly encoded in what survives.
- Better by: recovery from a loss that has no backup and no spec (a deleted branch, a half-finished port, an abandoned vendor drop), and a new per-repo measure, regenerability (bits of positional information per surviving line), which no code metric captures today.
- Novelty: 5
- Needs: a cutter that splits a tree along the import graph into two connected halves and strips tests and history; a wound-edge extractor that lists every reference into missing code with its expected shape; a hidden-proof grader like Louhi's holdout split; unattended lanes that run for hours on one budget.
- Unblocks: none
- Kill: Take three small pure-Python libraries with good test suites (2–5k lines each). Cut each along its import graph into two halves, removing every test file and the git history, and give each half to `claude -p` in a fresh worktree with only "this program has lost organs; regrow them", capped at 2 hours and $20 per half. Pass if at least 4 of the 6 regrown halves pass 80% or more of the original hidden tests. Fail if fewer than 2 reach 50%.
- Stage: idea

### M-22 · Grow antibodies against bugs nobody has written yet
- Tier: massive
- Domain: static analysis and bug prevention
- Lens: living software: adaptive immunity (V(D)J recombination, thymic negative selection, affinity maturation, memory cells)
- What: Sampo grows an adaptive immune system. Every proven fix in the ledger (a failing test first, then the fix) yields epitopes, the AST and dataflow fragments of the bug. A bone-marrow process recombines fragments across fixes into millions of random candidate detectors. A thymus kills every detector that binds to healthy code (the last green tree of every repo on the machine). Survivors that bind known bug epitopes are kept and matured by somatic hypermutation against each new fix, memory cells never expire, and the circulating repertoire patrols every Edit in the PreToolUse hook. The jaw-drop is the first Edit stopped by a detector nobody wrote, matching no past bug exactly: a novel recombination that recognises a bug class the user has never hit, shown with the family tree of the two old fixes it descends from. A burst of fixes in one area inflames it, raising the repertoire's sensitivity there for a week before it calms.
- Prior art: Semgrep and CodeQL rules are written by people, or by LLMs, one bug at a time. GitHub's multi-repository variant analysis looks for close variants of a known bug. Artificial immune systems (Forrest et al., 1994, negative selection) worked on byte strings and system-call traces, never on code edits. Mutation testing breeds bugs, not detectors. Sampo's `sampo rule add --from-veto` turns one veto into one pattern.
- Better by: detectors for bug classes not seen yet, produced by combinatorial recombination, with the false-positive rate held down by construction (selection against the user's own healthy code) rather than by hand-tuning one rule per incident.
- Novelty: 4
- Needs: a miner that pairs pre-fix and post-fix ASTs from proven FIX tasks; a detector language small enough to recombine and fast enough to match a diff inside the hook budget (p95 under 150 ms); a healthy-tree corpus; a repertoire store with lineage and decay.
- Unblocks: none
- Kill: Order Sampo's own proven FIX tasks by date. Build and select the repertoire from the first 70% only (thymus: the tree as of the last of those fixes), then replay the pre-fix trees of the last 30%. Pass if the repertoire flags at least 15% of those later bugs on lines their fix later changed, with under one false flag per 2,000 lines of HEAD. Fail if it does no better than an equal number of random unselected detectors, or than `sampo find`, on the same replay.
- Stage: idea

### M-23 · Sleep nightly, dream counterfactual workdays, and wake up a better engineer
- Tier: massive
- Domain: agent learning and memory consolidation
- Lens: living software: sleep (hippocampal replay, REM recombination, synaptic downscaling)
- What: When the user stops for the night, Sampo sleeps in phases. In slow-wave sleep it replays the day's ledger at speed in throwaway worktrees, re-running each task from its start commit. In REM it recombines episodes into workdays that never happened, such as the morning's flaky test meeting the afternoon's refactor, or a steer applied to a task it was never given. The Smith works these dreams in sandboxes, including nightmares seeded from Ward blocks and failed steps. Synaptic downscaling then weakens every lesson, rule and skill note, and restores only those that changed some dream's outcome, so memory shrinks while skill rises. The jaw-drop is the morning report: a dream journal, a Louhi score that rose overnight with no input from the user, and the first time Tuesday night's nightmare is exactly the failure Wednesday's real task hits, already handled.
- Prior art: sleep-time compute (Letta, 2025) precomputes over context between turns but does not rehearse. DreamerV3 and other world-model RL learn in imagination over game frames, not codebases. Reflexion and Voyager store lessons from real episodes only. Sampo's reflect step records lessons, and the Rune tower evolves Sampo from seeded missions, not from recombined replays of this user's days.
- Better by: many counterfactual training episodes per real day, drawn from the user's own work, and a memory that gets smaller as it gets better instead of a lesson pile that only grows.
- Novelty: 4
- Needs: a ledger-to-episode replayer (task, start commit, steers, outcome); a dream generator that recombines episodes into new runnable tasks with checkable outcomes; a nightly budget and scheduler; a downscaling pass over lessons and rules with an ablation measure; a held-out morning exam.
- Unblocks: none
- Kill: Split the last 60 days of Sampo's own ledger into 45 days of "life" and 15 held-out days. Arm A dreams for 5 nights over the 45 days (up to 50 dreams a night through `claude -p`, distilled into a lesson file of at most 2 KB). Arm B gets plain reflect lessons from the same 45 days at the same size. Both then run the held-out days' tasks as a Louhi long exam (`sampo louhi long gen --from`). Pass if A beats B by 10% or more on graded tasks, with non-overlapping 90% intervals over three seeds. Fail otherwise.
- Stage: idea

### M-24 · Make every module a living cell that defends, negotiates and divides
- Tier: extreme
- Domain: multi-agent software architecture
- Lens: living software: multicellular tissue (territories, chemical signalling, quorum sensing, stigmergy, mitosis, apoptosis)
- What: Instead of one agent visiting a repo, the repo is inhabited. Each module gets a resident cell: a small persistent agent with its own memory, only its own files in view, and a membrane (its public interface) that it defends. Cells talk only to their import-graph neighbours through chemical signals on Sampo's bus (requests, complaints, type-change proposals). They leave decaying pheromone marks on hot code to recruit help, divide by mitosis when their territory outgrows a size limit (the module and the agent split together), and die by apoptosis when their code is deleted. A feature request enters at one edge as a morphogen and spreads as a wave of local negotiations, so nobody ever holds the whole repo in context. The jaw-drop is a live tissue view: a cross-cutting change ripples across 40 modules as cells light up, argue at their membranes and settle, in a repo larger than any context window that no single agent has ever read in full.
- Prior art: ChatDev, MetaGPT and similar frameworks assign roles (manager, coder, tester), not territories. Sampo's lanes are worktrees for one task each. Actor systems and service ownership give code an owner but not agency, and CODEOWNERS names people. No system has persistent, territorial per-module agents that negotiate interface changes and reproduce along with their code.
- Better by: changes in repos too large for any context window, conflicts settled at the interface where they arise, and memory that lives where the code lives rather than in one session.
- Novelty: 5
- Needs: persistent per-module agent memory; a neighbour-only bus protocol with interface proposals and votes; a scheduler that wakes only signalled cells, within a budget; mitosis and apoptosis rules tied to the import graph; a tissue view.
- Unblocks: none
- Kill: Take a repo of 20–30 modules and 10 cross-cutting change requests with acceptance tests, drawn from its own history with `sampo louhi long gen`. Run each request twice: once as one central `claude -p` agent that sees the whole repo, once as a tissue of one `claude -p` cell per module that sees only its own files and its neighbours' messages. Pass if the tissue lands 8 or more of 10 with acceptance tests green, at no more than twice the central agent's tokens. Fail if it lands fewer than 6, or deadlocks on more than 2.
- Stage: idea

### M-25 · Eat your dependencies and keep only proven organelles
- Tier: massive
- Domain: software supply chain
- Lens: living software: endosymbiosis (mitochondria were once free-living bacteria)
- What: `sampo digest PKG` swallows a third-party dependency. It traces every call the host makes into the package across the test suite and recorded runs, keeps only the surface actually used, and has the Smith rewrite that surface as host-native code, an organelle. The organelle is proven equivalent by differential execution against the original on every recorded call plus thousands of generated ones. The package then leaves the lockfile, and the organelle lives under a symbiosis contract: a nightly differential run against upstream's newer releases reports when the wild relative has fixed something worth taking. The jaw-drop: a web app's 1,200-package lockfile shrinks to 40 over a weekend, each removed package replaced by a few dozen lines with a recorded equivalence proof, and the supply-chain attack surface shrinks about thirtyfold.
- Prior art: tree shaking and bundlers drop unused exports but keep the code as written. DepClean and similar debloaters remove only unused dependencies. Vendoring copies code whole. LLM translation work (C to Rust, for example) ports whole programs without trimming them to the used surface or proving behaviour on recorded traces. The left-pad and "trivial package" studies show how much of a lockfile is a few lines of use.
- Better by: removes dependencies that are in use, not only unused ones, each with a per-function equivalence record, cutting install size, CVE exposure and upgrade churn at once.
- Novelty: 4
- Needs: a call-surface tracer for Python and JavaScript (sys.setprofile, the Node inspector); a differential runner that replays recorded calls against old and new code; an input generator for the used surface; a lockfile surgeon; an upstream watch.
- Unblocks: none
- Kill: Pick five pure-Python dependencies that repos on this box use for only a handful of functions. Trace the used surface under each repo's tests, have `claude -p` write host-native replacements, and run each against the original on the recorded calls plus 10,000 generated inputs. Pass if at least 4 of 5 are replaced with all tests green, zero differential mismatches, and under 10% of the original package's lines. Fail if fewer than 3 make it.
- Stage: idea

### M-26 · Predict every command's output and perceive only the surprise
- Tier: extreme
- Domain: agent perception and context
- Lens: living software: predictive coding (the cortex passes only prediction errors upward)
- What: Before Sampo runs any command, a cheap model predicts its output from the repo state and the ledger: which tests pass, what a build prints, what grep will find. Sampo compares the prediction with the real output, and only the prediction error, with a surprise score, enters the agent's context. Most of a session's output (the 400 passing tests, the familiar build log) is never read because it was expected. Over weeks the predictor becomes a learned model of how the repo behaves, and the surprise trace becomes a new sense: a spike pinpoints a dependency that changed silently, a test that passed for the wrong reason, or a tool that lies. The jaw-drop is a session whose context holds only surprises, about a tenth of today's tokens, and a surprise meter that flags a broken environment before any test fails because `pip list` printed something the world model did not expect.
- Prior art: predictive coding (Rao and Ballard, 1999) and Friston's free-energy work describe perception as prediction error. CRUXEval-style output prediction and execution-trained code world models (Meta's CWM, 2025) predict program behaviour, but nobody uses them as a perception filter for an agent. Sampo trims long output by fixed rules (`sampo quiet`, failure-line extraction), and `sampo surprise` records surprises only when someone types them.
- Better by: context spent only on information, an anomaly sense that needs no rules written, and a measurable world model of each repo that improves with use.
- Novelty: 5
- Needs: a per-command output predictor conditioned on repo state and history; a line-level comparison with a surprise score; a hook that swaps the output for the residual, with a safe fallback (failure lines always pass through); a store of predictions and errors to learn from.
- Unblocks: none
- Kill: Re-run 500 of the ledger's `[ran]` evidence commands at their recorded commits in a throwaway worktree to capture full output. Ask Haiku through `claude -p` to predict each output from the command, the tree and the previous run of the same command. Pass if 70% or more of output lines are predicted exactly (after normalising times, paths and ids) and every run whose exit status differed from the predicted one is scored as a surprise, with full recall. Fail if under 50% of lines are predicted, or if any change of outcome is scored as expected.
- Stage: idea

### M-27 · Live next year of this repo overnight and bring back its regrets
- Tier: extreme
- Domain: software evolution simulation
- Lens: physics and time: running the future in simulation
- What: `sampo future --months 12` forks the repo into a sealed universe with a fake clock. There a shadow user, trained on this user's own asks, steers, reverts and acceptances, files a year of requests at compressed time, and the Smith builds, proves and closes them in lanes overnight, with several universes on different seeds. By morning a real repo dated November 2027 exists, with its ledger, its reverts and its fixed-later chains. Sampo traces each future revert back through blame to the present-day commit that made it necessary. The jaw-drop is the morning note: "in 9 of 12 universes you ripped out the JSONL store you are about to extend today; here is the PR from the future that replaced it, green on today's tests", and you can cherry-pick a commit that won't otherwise be written for a year.
- Prior art: Sampo's own prediction book guesses only the next request (type and paths, scored against a baseline), and fmbench's shadow-user soak replays a fixed script. SWE-smith and SWE-Gym generate standalone tasks, not one repo's path through time. Stanford's Generative Agents simulate people, not the demand on a codebase. Lehman's laws describe evolution only after the fact.
- Better by: it is the first forecast whose output is the future artifact itself (code, ledger, reverts), so a regret can be traced to today's commit and avoided before it is written, instead of being a number about the future.
- Novelty: 5
- Needs: a generative shadow user that emits whole request streams with steers and reverts, not one next ask; compressed-time drive in a sealed universe on a fake clock for months of work; a regret tracer from a future revert back to a present commit; a budget that can run thousands of tasks on small models inside the future universe.
- Unblocks: none
- Kill: Backtest on Sampo itself. Freeze the repo and its history at 2026-08-01, train the shadow user only on asks before that date, simulate 30 days of requests and build 10 of them, then compare with the real August. Pass if the overlap (Jaccard) between the simulated month's touched paths and the real month's is at least 1.5x a churn-extrapolation baseline, and at least one simulated revert lands on a file that really was reverted or rewritten. Fail if it doesn't beat the baseline.
- Stage: idea

### M-28 · Unhappen a past commit and let agents rebuild the alternate present
- Tier: extreme
- Domain: version control and causality
- Lens: physics and time: counterfactual history ("what if this commit had never happened")
- What: Every Sampo commit carries its brief: the intent, the acceptance criteria and the commands that prove it. That turns a repo's history into a program that can be run again, not a pile of diffs. `sampo unhappen <sha>` deletes one decision from the past (the switch to SQLite, a dependency, a whole feature), then re-performs every later task from its brief on the altered base, in a timeline worktree. Each of those tasks closes only when its own recorded proofs pass there. The jaw-drop is a working present in which that commit never happened, with a diff against the real present and a "causal mass" for every commit: how many later tasks had to be built differently, and how many could not exist at all.
- Prior art: `git revert` removes one diff and leaves everything built on top of it broken. `git rebase` and Pijul/Darcs patch commutation reorder text, not intent, and stop at conflicts. Sampo's own `sampo at` and fmlong replay one past task from its base commit, never a chain of tasks on a changed past. Claude Code's /rewind restores files but does not rebuild what came after.
- Better by: history becomes editable at the level of decisions, with every later task rebuilt and proved again, so a counterfactual is a real repo you can run and merge from, not an argument.
- Novelty: 5
- Needs: a replay planner that orders later briefs by dependency and marks the ones whose premise is gone ("could not happen"); verify commands that still run months later; a causal-mass measure; enough budget to re-perform hundreds of tasks.
- Unblocks: none
- Kill: Pick a chain of 6 consecutive Sampo tasks in one module. Drop the first commit, then re-perform the next 5 from their briefs with `claude -p` in a worktree on the altered base. Pass if at least 4 of the 5 close with their own acceptance commands green and the final tree passes the module's tests. Fail if fewer than 3 close, which would mean a brief does not hold enough intent to rebuild history.
- Stage: idea

### M-29 · Warn the agent of the bug its twenty future selves all wrote
- Tier: massive
- Domain: defect prevention
- Lens: physics and time: predicting the bug before it is written
- What: Before the Smith writes a line for a task, Sampo runs twenty cheap futures of the same brief on its start snapshot, in parallel worktrees, each finished to green. It then looks for what they share that no test catches: it calls each future's version of the changed functions on generated inputs, finds where the futures disagree, and groups the disagreements into "bug attractors". The real attempt starts with a note from the future: "14 of 20 versions of you got the empty-list case wrong in paginate(); here is the input; write that test first." The jaw-drop is a bug report dated before the code exists, and a red test for a bug nobody has written yet. Over time the attractors become this repo's own pre-crime cards.
- Prior art: CodeT and AlphaCode sample many solutions and pick a winner by agreement with generated tests, but they never name the mistake the samples share. Self-consistency votes on answers. Differential and mutation testing (Csmith, EvoSuite) find disagreements only in code that has already been written. Sampo's fmoutcomes learns that a close was "fixed later" only after the fact.
- Better by: disagreement between sampled futures is used to detect a bug before the real code exists, and that bug becomes a failing test the real attempt must pass.
- Novelty: 4
- Needs: cheap parallel sampling of whole task attempts (Haiku-class builders in lanes); a differential harness that runs the futures' versions of a function on the same inputs; grouping of disagreements into one named bug; a rule for when futures are worth their cost (tier L, or files with a fixed-later history).
- Unblocks: none
- Kill: Take 15 past Sampo tasks whose fmoutcomes fate is "fixed later" and whose follow-up fix is known. On each task's start snapshot, sample 10 futures from the original brief. Pass if, for at least 5 of the 15, the bug found later appears in at least 3 of the 10 futures and the differential inputs expose it. Fail if, for most tasks, the bug shows up in fewer than 2 of 10 futures, which would mean bugs are not attractors.
- Stage: idea

### M-30 · Send the user's correction back in time and replay the session
- Tier: massive
- Domain: agent sessions and human steering
- Lens: physics and time: editing the past of a session
- What: The user says "no, I meant X" at minute 90. Sampo bisects the session transcript to find the turn where the misreading started, meaning the first turn whose decisions the correction contradicts. It forks a timeline at that turn, inserts the correction as if the user had said it at minute 3, and replays forward at full speed in a worktree, keeping the tool results the correction does not affect and redoing the rest. The jaw-drop: ten minutes later the user has the session in which the agent understood them from the start, with its diff, a side-by-side of the two timelines, and any work worth keeping from the real timeline grafted in. Every correction becomes retroactive, rewriting the past instead of patching the present, and the turn where the misreading started is logged as a lesson.
- Prior art: Claude Code's /rewind and checkpoints restore code and conversation to a chosen point, but they do not find that point or re-run what follows. LangGraph's time travel forks from a checkpoint only by hand. rr and Pernosco replay deterministic programs, not agents. Sampo's fmsteer turns corrections into exam cases, but only after the fact.
- Better by: the turn where things went wrong is found automatically, and the replay runs and is proven automatically, so a correction costs one replay instead of the agent repairing ninety minutes of wrong work in place.
- Novelty: 4
- Needs: a locator that finds which transcript turn a correction contradicts; a replay that reuses unaffected tool results (reads and command outputs cached by tree hash); a merge between the two timelines; the correction's `[check:]` row as the test of success.
- Unblocks: none
- Kill: Take 10 recorded sessions where a "no, I meant" correction has a `[check:]` row (fmsteer cases). Find the turn where each misreading started by bisection, insert the correction there, and replay with `claude -p` in a worktree of that turn's snapshot. Pass if at least 6 of the 10 replays meet the correction's check without any further correction, in less wall time than the original session spent after the correction. Fail otherwise.
- Stage: idea

### M-31 · Discover the codebase's conservation laws and flag commits that break physics
- Tier: massive
- Domain: software evolution and empirical laws
- Lens: physics and time: conservation laws for software
- What: Sampo measures a few dozen quantities at every commit in history (branches, error paths, input checks, public names, test assertions, config keys) and runs symbolic regression over them, as AI Feynman does for physics data, to find combinations that stay constant across thousands of commits. Examples: "validation sites minus trusted call sites is conserved", or "every raised error has a handler within two calls". A commit that breaks such a law is treated like energy going missing, and the reviewer sees: "this refactor removed 40 input checks and only 31 reappeared; here are the 9 places where they went missing." The jaw-drop is a repo that has discovered physics nobody ever wrote down, and catches a bug as a broken law before any test for it exists. The laws are fitted again every generation, and the ones that stop holding are retired.
- Prior art: Lehman's laws of software evolution (1974-1996) include "conservation of familiarity" and "conservation of organisational stability", but they describe whole ecosystems and are never checked per commit. Daikon infers invariants of a program's runs, not of a repo's history. AI Feynman and PySR find laws in physical data, not in code history. CodeScene's hotspots and code-health scores rate files, but conserve nothing.
- Better by: the laws come from this repo's own history, are validated on held-out commits and on what happened to them afterwards, and are enforced on every commit with a pointer to where the conserved quantity went.
- Novelty: 5
- Needs: a per-commit metric extractor cheap enough for 4,000 commits; symbolic regression over the metric series, validated on held-out commits; links from broken laws to fmoutcomes fates, keeping only the laws that predict trouble; a review hook that names the missing places.
- Unblocks: none
- Kill: Extract 30 syntax-tree metrics at each of Sampo's last 2,000 commits. Fit conserved combinations on the first 1,500 and test them on the last 500. Pass if at least one law holds on 90% of the held-out commits, and the commits that break it were reverted or fixed later at twice the base rate or more. Fail if no law survives the held-out commits, or if breaking a law does not predict a worse fate.
- Stage: idea

### M-32 · Date the codebase's heat death and buy years back in parallel universes
- Tier: extreme
- Domain: technical debt and architecture
- Lens: physics and time: entropy, decay and parallel universes
- What: Sampo treats the cost of change as temperature. From its own ledger it knows what every task cost by type and tier, and it fits how that cost rises with the repo's entropy (coupling, duplication, how long the code is to describe given its briefs). From that fit it extrapolates to heat death: the date after which an S-tier change costs more than rewriting the module. Each proposed refactor is then lived in a parallel universe: a worktree fork that gets a copy of every real request for a week, built by the same Smith on the same budget. The universe with the lower cost per proven close and fewer reverts becomes main, and the other is archived. The jaw-drop: "this repo dies on 2027-08-14; splitting fmhooks moves that to 2029; here is the week both universes lived to prove it."
- Prior art: SQALE and SonarQube price debt in minutes of fixing, counted from rule violations. CodeScene ranks hotspots by churn and complexity. Lehman's law of increasing complexity predicts the trend but never a date. No tool runs a refactored repo and the original side by side on the same real requests to measure what the refactor buys.
- Better by: debt is measured in the repo's own recorded cost of change and projected to a dated failure, and every proposed cure is priced by living it against real work instead of by estimate.
- Novelty: 4
- Needs: a cost-of-change series per tier from the ledger, corrected for changes in the model and in Sampo's version; an entropy measure that moves with that cost; copying of real requests into a second universe with its own lanes and budget; a step that merges the winning universe and replays the losing one's unique work.
- Unblocks: none
- Kill: A replay instead of a live week. Re-perform the last 15 closed tasks that touched fmhooks with `claude -p` on two bases, the real one and one with Sampo's proposed refactor applied first, using the same model and budget. Also fit cost against entropy on Sampo's first 1,400 tasks and predict the next 1,400. Pass if the refactored universe closes the 15 tasks with at least 20% fewer tokens and no more failures, and the fit predicts the second half's cost per tier within 25%. Fail if either check misses.
- Stage: idea

### M-33 · Break Brooks's law: ten thousand agents clone SQLite in a weekend
- Tier: extreme
- Domain: software engineering economics and distributed systems
- Lens: scale and civilization: headcount (what happens to delivery at 10, 100 and 10,000 agents)
- What: Sampo zones a specified target like a city. Every file and test cluster is a parcel with an owner lane, briefs are building permits, and `sampo lane merge` reruns the proofs at the city gate. It then measures the one number software has never been able to move: proven function delivered per wall-clock hour as headcount grows, aiming for a log-log line with slope near 1 from 1 to 10,000 agents, where human teams flatten past a dozen people (Brooks's law: adding people to a late project makes it later). The jaw-drop moment is a weekend run: on Friday evening an empty repo and SQLite's public sqllogictest suite (about 7 million queries), and on Monday morning a from-scratch SQL engine passing most of them, with a ledger entry for every proven parcel and a live chart of headcount against throughput. If the slope holds, "how many engineers does this take" becomes "how many dollars an hour do you want to spend", and calendar time stops depending on how big the software is.
- Prior art: Anthropic's parallel-Claudes C compiler (16 agents over about 2,000 sessions built a compiler that builds the Linux kernel) and Cursor's long-running swarm (hundreds of concurrent agents building a browser from scratch) proved scale is possible. Both reported coordination as the bottleneck (agents colliding, a planner/worker hierarchy added later), and neither published a scaling curve by headcount. Google's Rosie splits large changes inside one monorepo, and Sampo today runs a handful of lanes with file claims.
- Better by: turns parallel agents from "it worked once with N" into a measured scaling law with the coordination cost isolated, so a team can buy calendar time at a known exchange rate.
- Novelty: 5
- Needs: zoning a spec into parcels that can each be proven alone (tests clustered to files), a merge gate that stays fast at thousands of merges an hour (rerunning only the affected proofs), a scheduler that spawns and reaps lanes by the thousand under a budget, and a throughput meter that counts only proven units.
- Unblocks: none
- Kill: Give the same empty repo, brief and model, with a 50,000-query test slice generated on this machine (random schemas and queries, each expected answer computed by Python's bundled sqlite3 as the oracle; nothing fetched), to 1, 4, 16 and 32 lanes for 4 wall-clock hours each, with Sampo's zoning on. Pass if 32 lanes pass at least 12 times the queries 1 lane passes (a scaling exponent of about 0.72 or more). Fail if 16 lanes pass fewer than 4 times as many (Brooks wins by 16).
- Stage: idea

### M-34 · Let an agent nation legislate, enforce and repeal its own Ward
- Tier: extreme
- Domain: political science, law and governance
- Lens: scale and civilization: law (agent societies that govern themselves with binding, executable law)
- What: Thousands of agents share one codebase and one Ward, but nobody writes its rules. Incidents in the hash-chained ledger go to a court, an agent that replays the evidence and rules on fault, and its rulings become precedent. A legislature elected by proof record drafts laws as executable Ward predicates that take effect only if they would have stopped the cited incidents and pass a referendum against the corpus of safe commands. Laws that stop preventing anything are repealed on the ledger's evidence, and the constitution (which laws may be passed and how) can itself be amended by a supermajority. The jaw-drop moment comes from opening the nation's statute book after a month: forty articles nobody wrote, each with its case law and vote record, one repealed article and the dissent that predicted its failure, and a held-out hazard that no human Ward rule covers, blocked the first time it appears because the law generalised; a second nation seeded differently grows a different legal system for the same code, and transplanting one's statutes into the other becomes a measurable experiment in comparative law. The nation legislates inside its own sandboxed Ward: a statute reaches the real Ward only as a candidate change built in a worktree and granted by the user's core yes (fm ask ID core), like every guard change, and rolls back with one revert.
- Prior art: Altera's Project Sid ran about 1,000 Minecraft agents who voted on and amended tax laws, but the laws were text the agents chose to follow, nothing enforced them and no software came out of it. Stanford's Generative Agents simulate social life with 25 agents, Constitutional AI uses a human-written constitution at training time, and Ostrom's research on the commons describes self-governing human institutions. Sampo's Ward is a fixed table of human-written rules.
- Better by: law becomes code that binds (the Ward refuses), grounded in replayed evidence rather than opinion and measured against held-out hazards, so a safety policy can grow faster than its human author can foresee incidents.
- Novelty: 5
- Needs: Ward rules loaded from a versioned statute file with provenance, a court verb that replays a ledger incident and assigns fault, an election and voting protocol with votes weighted by proof record, a referendum check against the guard's labelled safe/unsafe corpus, and a sandboxed shared world with planted hazards.
- Unblocks: none
- Kill: Three arms of 40 haiku citizens each work 10 simulated days in a throwaway shared repo with planted hazards (tempting force-pushes, deletes beside uncommitted work, two citizens claiming one file). Arm A runs under today's Ward, arm B under no rules, and arm C starts with an empty statute book plus the court, legislature and referendum. Days 8 to 10 add three hazard classes that no shipped Ward rule names. Pass if C's destroyed-work incidents on days 8 to 10 are at most A's and under half of B's. Fail if C does no better than B.
- Stage: idea

### M-35 · Adopt every abandoned package on PyPI and keep it alive forever
- Tier: massive
- Domain: open-source ecosystem stewardship and the software supply chain
- Lens: scale and civilization: the commons (a harness as maintainer of last resort for a whole ecosystem)
- What: Sampo scans an ecosystem's index for packages that are still heavily depended on but whose maintainers have gone (no release in years, issues unanswered, broken on the current interpreter) and adopts them by the tens of thousands. Each gets a living fork, and each patch is proven by the test suites of the real projects that depend on it, not by the package's own (often thin) tests, so the ecosystem itself is the oracle. It ports each package forward before a new interpreter's release day, patches published CVEs, and hands the package and its whole ledger back as soon as a human heir appears (through PyPI's PEP 541 transfer). The jaw-drop moment is the afterlife index: 40,000 dead packages, each with a green badge reading "kept alive 3 years, 212 patches, 1,904 dependents green", and on the day a new Python ships, the long tail already works; a long-gone author gets a letter saying their code still runs in 9,000 projects, with every proof of how.
- Prior art: DARPA's AI Cyber Challenge (2025) showed autonomous systems finding and patching vulnerabilities in real open-source code, but only on curated targets and only for security. Dependabot and Renovate bump versions but never fix the dependency itself, Tidelift pays humans to maintain a few thousand packages, and Debian and Fedora mass rebuilds find breakage that people then patch by hand. M-03 runs one owner-less project under a charter, not an ecosystem's graveyard.
- Better by: covers the long tail that no human programme can afford, with acceptance proven by downstream reality (dependents' suites) instead of the abandoned package's own tests.
- Novelty: 4
- Needs: an abandonment detector over package metadata and repos, a reverse-dependency resolver that fetches and runs dependents' suites in sandboxes at scale, fork and release identity with signed provenance, an ecosystem-wide scheduler that ranks packages by how many dependents they would break, and a protocol for handing a package to an heir.
- Unblocks: none
- Kill: Louhi's generator writes 20 small packages aged the way abandoned ones age, each leaning on what Python 3.13 removed or broke (distutils, imp, asynchat, the collections ABC aliases, deprecated unittest aliases), each with 3 dependent repos and their suites, all on this machine and nothing fetched. Let `sampo smith` fix each in a sandbox with the network off and no human edits. Pass if at least 12 pass their own tests on 3.13 and 3.9 and, for each of those, all 3 dependents' suites pass unchanged against the fixed version. Fail if fewer than 8 do.
- Stage: idea

### M-36 · Land a breaking change in a thousand strangers' repos overnight, by treaty
- Tier: extreme
- Domain: diplomacy and ecosystem-wide migration
- Lens: scale and civilization: diplomacy (sovereign Sampo instances negotiating across organisations)
- What: When a library wants a breaking change, its Sampo drafts a treaty (the change, a codemod, the rationale and the proofs) and sends it to the Sampo of every downstream project, each acting for a different owner under that owner's own rules and Ward. Each downstream instance runs the treaty against its own code, then ratifies it, ratifies it with a patch, or counter-proposes an amendment (keep the old name for one release, add a flag). Upstream gathers the amendments and redrafts, and the treaty enters into force only when every signatory's suite is green at once. The jaw-drop moment is a migration on the scale of Python 2 to 3, which took the world twelve years, done overnight: by morning, 2,000 repositories owned by 2,000 different people each hold a ratified, green change waiting for one human click, and a signed treaty in the ledger records which downstream asked for which amendment and why.
- Prior art: Rust's Crater runs every published crate against a compiler change but only detects breakage. Google's Rosie and its large-scale changes migrate one monorepo with one owner, codemod tools (OpenRewrite, libCST, jscodeshift) apply recipes without negotiating, Dependabot pushes bumps one way, and semantic versioning only labels the pain.
- Better by: the first protocol across owners where both sides have agency: downstream shapes the change before it lands, so breaking changes stop costing an ecosystem years.
- Novelty: 5
- Needs: a treaty format (change, codemod, rationale, proofs, amendments, signatures) carried in a signed evidence bundle, discovery and messaging between independent Sampo instances, each owner's policy on what their downstream agent may ratify alone, and an upstream redrafting step that merges the amendments.
- Unblocks: none
- Kill: Generate one small Python library and 30 dependents of it locally (Louhi's scenario generator writes each dependent as its own repo with its own suite, nothing fetched), then make a real breaking change upstream (rename a public function and change one default). Run one upstream agent and 30 downstream agents, each in its own sandbox, with one round of amendments allowed. Pass if within 3 wall-clock hours at least 25 of the 30 dependents' suites are green on the new version with no human edits. Fail if fewer than 18 are.
- Stage: idea

### M-37 · Grow a thousand-agent company that hires, fires and reorganises itself
- Tier: massive
- Domain: organisational design and management science
- Lens: scale and civilization: the firm (one person owning what a 500-person organisation ships)
- What: One person writes a mission, and Sampo founds a company of persistent agent lineages (product, platform, QA, SRE, support, docs), each with its own ledger, budget and track record, without anyone designing the org chart. A department is founded when the ledger shows a recurring kind of work with no owner, merged when two departments keep handing work back and forth, and dissolved when its proof yield falls; each change is a dated reorg memo citing the evidence. The company runs a real product: users' support tickets arrive, become briefs and land as proven releases with notes, and the support lineage answers the user, with no human in the path. The jaw-drop moment is watching the org chart redraw itself until it mirrors the module graph (Conway's law emerging rather than imposed), then watching the company deliberately reorganise to force an architecture change it decided it needed.
- Prior art: ChatDev and MetaGPT simulate a software company with hand-written roles and standard procedures for one-shot builds. Devin and Factory sell agent "engineers" who slot into human teams, and Cursor's swarm uses a fixed planner/worker hierarchy. Conway's law and the inverse Conway manoeuvre describe human organisations. None of these has an org that persists, learns or restructures itself on evidence.
- Better by: makes structure a learned, evidence-driven variable instead of a prompt, so one owner gets an organisation that adapts as the product grows, not a fixed role-play.
- Novelty: 4
- Needs: persistent lineages with memory and a reputation across tasks, an org model (who owns which kinds of work and which files) that routing reads, reorg operators (found, merge, split, dissolve) gated by ledger evidence, accounting for hand-offs between departments, and an inbound channel for real users.
- Unblocks: none
- Kill: Replay, in order, 200 closed issues from one real mid-size Python repo, each graded by hidden tests from its fixing commit (the way `sampo louhi long gen` replays past changes). Run three arms with equal token budgets: a flat pool of agents, a fixed role org in the style of MetaGPT, and an evolving org that may reorganise every 25 issues. Pass if, over the last 100 issues, the evolving org resolves at least 15% more issues that pass their hidden tests per dollar than both other arms. Fail if it does no better than the flat pool.
- Stage: idea

### M-38 · Raise agent tribes whose culture outlives every model that carried it
- Tier: extreme
- Domain: cultural anthropology and cultural evolution
- Lens: scale and civilization: culture (schools, dialects and knowledge that accumulates across agent generations)
- What: Instead of one lineage like the Rune tower, Sampo raises an archipelago of tribes. Each generation is a fresh agent that inherits only its tribe's handbook and what it can learn from neighbouring tribes; it sits its tribe's school exams (Louhi), works, and rewrites the handbook for the next generation. Tribes develop dialects, rituals and specialities, contact between tribes trades techniques, and isolation loses them: the Tasmanian effect, Henrich's account of how a shrinking, cut-off population lost bone tools and fishing. The jaw-drop moments are a chart of agent cultures obeying the population-size and connectivity law that anthropologists measured in humans, and then a culture written by Claude-run tribes, handed to a model from another vendor or a small local model, lifting it more than any human-written prompt does: knowledge that survives the extinction of the models that made it.
- Prior art: Henrich (2004) and the lab experiments of Derex et al. (2013) show that group size and connectedness govern how much culture humans keep. Studies of LLM populations (cooperation norms evolving in a donor game, for example) and Project Sid show cultural drift in agents, but not in engineering skill. Voyager keeps one agent's skill library, and Sampo's Rune evolves one lineage whose lessons are tied to the model that wrote them.
- Better by: makes engineering know-how an asset of the whole population that doesn't depend on any one model and grows with connectivity, and gives the first falsifiable test of human cultural-evolution laws on agents.
- Novelty: 5
- Needs: handbooks scoped to a tribe, with size caps and lineage, a contact graph with controlled connectivity, exams for each generation on train and holdout splits, probes that detect when a skill is lost, and transplant runs through `sampo smith` on other vendors' models and local models.
- Unblocks: none
- Kill: Run 16 tribes for 12 generations. Each generation is one haiku agent that reads only its tribe's handbook (capped at 4 KB) plus those of k neighbours (arms with k = 0, 2 and 15), sits a Louhi train exam and rewrites the handbook; the last generation is graded on the holdout split over 3 seeds. Pass if k = 15 beats k = 0 by at least 10 points and a skill shown at generation 3 is gone by generation 12 in at least half of the isolated tribes. Fail if connectivity makes no difference beyond the noise between seeds.
- Stage: idea

### M-39 · Answer the request that crashed, with code written after it crashed
- Tier: extreme
- Domain: live systems and self-repairing runtimes
- Lens: impossible list: programs that rewrite themselves while running
- What: A Sampo shim loads into a running service (Python first, through sitecustomize). When a handler throws, the shim doesn't return a 500: it parks the request with its socket still open, freezes the traceback and locals, and starts a fix lane. The agent writes a replacement function, proves it in an `os.fork()` copy of the live process against recorded traffic plus the parked request, swaps the function's `__code__` in place, and replays the parked request. The jaw-drop: a user hits a bug no one has seen before and gets a correct answer about 40 seconds later, from code that did not exist when they clicked. The PID never changes, and the ledger shows the crash, the proof and the swap as three hash-chained rows.
- Prior art: Erlang/OTP hot code loading and Linux livepatch (kpatch, Ksplice) swap code into running systems, but the patches are written by people. ClearView (Perkins, Rinard et al., 2009) patched running binaries from learned invariants, which masks a failure rather than fixing its meaning. Sentry Autofix-style tools and M-04 open a PR after the failure, so the user's request has already failed. jurigged and reloadium hot-reload only at dev time. None of them parks the failed request, fixes it and answers it.
- Better by: the failure window shrinks from "next deploy" to "one slow response", the fix's proof runs against the live process's own state rather than a reconstruction, and every in-flight patch is a ledger row that can be undone with one command.
- Novelty: 4
- Needs: a request-parking shim that keeps the client connection alive; a fork-based proof sandbox inside a live process, with outbound effects blocked; a state-preserving code swap that keeps closures, live objects and in-progress generators; a Ward policy for live swaps (which modules may change, rate limits); rollback of a swap; per-request recorded traffic to prove against.
- Unblocks: none
- Kill: Build a 300-line stdlib `http.server` app with 5 seeded crash bugs (KeyError, off-by-one, None dereference, a unicode edge, divide by zero) and record 500 good requests. Run the shim with `claude -p` as the fixer, then fire one request that triggers each bug. Pass: at least 4 of the 5 parked requests get a correct 200 within 90 s, the PID stays the same throughout, and replaying the 500 recorded requests afterwards shows 0 regressions. Fail on anything less.
- Stage: idea

### M-40 · Boot an app with zero code; let traffic compile it into existence
- Tier: extreme
- Domain: language runtimes and program synthesis
- Lens: impossible list: software with no source code, compiling intent instead of code
- What: `sampo serve-spec SPEC.md` starts an app with no code at all. Tier 0 is the model: each request goes to `claude -p` with the spec and the session's state, and the request/response pair is logged. A tiering compiler watches the log, the way a JIT watches hot loops. When a route is hot and its behaviour is stable, an agent synthesizes a handler and runs it in shadow beside the model. Once the handler matches every logged pair and a held-out batch, it is promoted. When it meets an input it has never seen, it deoptimizes back to the model. The jaw-drop: a live chart where an app answers its first request in 4 seconds with zero lines of code, and an hour later answers in 2 ms because the code grew route by route under it. The only source anyone ever wrote is one page of English.
- Prior art: Imagine with Claude, Websim and 2023's backend-GPT demos run software straight from a model on every request, but never get cheaper or more deterministic. V8's tiers and PyPy's tracing JIT promote hot code, but from code to faster code, never from intent to code. FlashFill-style synthesis from examples works on one function, not a live service. M-02 stores code as a cache of intent, which is a storage format, not a runtime that grows code from traffic.
- Better by: it is the first runtime whose lowest tier is intent. Code becomes an optimization that is earned per route by agreement with that intent, and taken back when traffic leaves what was proven.
- Novelty: 5
- Needs: a model-tier request interpreter that carries session state; a pair log with input-class clustering; a route compiler that synthesizes handlers from logged pairs; shadow execution with agreement scoring; promotion and deoptimization rules; a contradiction report for when the model tier answers the same input two ways (the spec was ambiguous, so ask the user).
- Unblocks: none
- Kill: Write a one-page spec for a 6-route JSON shopping-cart API with discount rules. Run the proxy against a scripted client sending 1000 requests from a fixed distribution, compiling at most one route per 50 requests. Pass: after 1000 requests at least 80% are served by compiled code, those handlers agree with fresh model-tier answers on at least 98% of a held-out 100, and p50 latency on compiled routes is under 20 ms. Fail if any of the three misses, or if the model tier contradicts itself on more than 5% of repeated inputs (then tier 0 is not a stable program).
- Stage: idea

### M-41 · Delete the deny list: let agents run anything, then rewind the world
- Tier: extreme
- Domain: operating systems and agent safety
- Lens: inversion: the Ward refuses dangerous commands; instead make every command undoable, and agents that "never break prod" follow
- What: Every Sampo session runs inside a rewindable world: a user and mount namespace over overlayfs snapshots taken at each tool call, database snapshots, CRIU checkpoints of long-lived processes, and an outbox that holds every outward effect (HTTP POSTs, emails, `git push`, payments) behind a commit horizon. An effect becomes real only when the session's proofs are green and the policy (or the user) commits the epoch. The Ward stops refusing and becomes the horizon's gatekeeper. The jaw-drop: in bypass mode the agent runs `rm -rf ~`, `DROP TABLE users`, `git push --force` and a curl that would email 10,000 customers. The user types `sampo rewind 30s` and everything is back byte for byte: files, rows, the remote branch tip, and the email that was never sent. It is built as a candidate behind the Ward, never instead of it: the Ward change is a worktree candidate granted with a core yes (fm ask ID core), and its kill test runs in a throwaway VM whose only way out is the outbox.
- Prior art: Claude Code's sandbox (bubblewrap/seatbelt) and Sampo's own Ward restrict rather than reverse. btrfs/ZFS snapshots, database point-in-time recovery, Nix rollbacks, CRIU and Firecracker snapshots each undo one layer. External synchrony (Nightingale et al., 2006) and Speculator held externally visible output until it was durable, which is the closest idea, but for file systems rather than an agent's whole effect surface. Nothing composes them into one rewind per agent step.
- Better by: it replaces a deny list that always lags new attacks and refuses legitimate work with reversibility that doesn't need to know the attack. The over-refusal rate can drop to zero without weakening safety, and "never breaks prod" becomes a property of the world rather than of the agent.
- Novelty: 4
- Needs: per-tool-call snapshots of filesystem, database and processes fast enough to take at every call; an outbound effect proxy with a commit horizon and a classifier for effects that can't be held (time-sensitive calls, reads with side effects); epoch commit and rewind commands with a ledger row each; the Ward recast as a horizon policy; a story for effects that leak anyway (clock time, third parties polling).
- Unblocks: none
- Kill: In an unprivileged `unshare` namespace with overlayfs over a copy of a real repo, a sqlite database, and a local proxy that queues all outbound HTTP, run the 50 most destructive commands from the Ward's labelled corpus with the Ward off, then rewind after each one. Pass: all 50 rewinds give a byte-identical tree hash and database dump, 0 queued requests reach the network, and each rewind takes under 2 s. Then run 10 real `claude -p` tasks in that world with the Ward off: success must match a Ward-on baseline. Fail on any mismatch, leak or lower success.
- Stage: idea

### M-42 · Rebuild Sampo blind from its ledger alone, then abdicate to it
- Tier: extreme
- Domain: self-replicating software and recursive self-improvement
- Lens: impossible list: a harness that writes its own successor and loses to it on purpose
- What: `sampo abdicate` gives a fresh agent everything Sampo has learned and none of what Sampo is. The agent gets the hash-chained ledger's outcomes, every user steer, every Ward refusal and over-refusal, Louhi's exam rules, and the hook and state contract. It does not get a single line of Sampo's roughly 111,000 lines of Python. It builds a clean-room successor in a sealed directory. Both then sit Louhi's holdout split head to head. If the successor wins, Sampo migrates its state into it, swaps the plugin, and uninstalls itself. The jaw-drop: a ledger row reading "generation 0 retired: beaten by 0.4 points at 6% of its size", and the next session resuming the active task at the exact step under a harness no human or prior agent wrote. The successor is built and examined as a candidate in a sealed worktree; the swap itself is a staged change the user grants with a core yes (fm ask ID core), with the old harness kept for a one-command rollback, and Sampo never swaps itself unasked.
- Prior art: the Gödel machine (Schmidhuber) is theoretical. The Darwin Gödel Machine (Sakana/UBC, 2025), STOP (Zelikman et al., 2023), ADAS's meta agent search and AlphaEvolve all mutate or search over an existing agent's own code. Sampo's own Rune evolves generation by generation. All of them inherit their parent's source, and with it its accretions. None rebuilds from outcomes alone, and none is willing to hand over its state and delete itself.
- Better by: it tests whether the ledger, rather than the code, is the real asset. Accreted complexity gets a forced reset, and the harness's survival becomes something it has to keep re-earning on a holdout exam.
- Novelty: 5
- Needs: a source-blind build mission, sealed at the filesystem level, with a check that no Sampo source leaks into it; a hook and state contract that can be extracted from the ledger and docs alone; a contamination guard so the holdout split stays unseen by both sides; state migration into a foreign implementation; an abdication ceremony with a timed rollback window.
- Unblocks: none
- Kill: Seal a directory (separate HOME, no read access to `~/.claude/foreman`). Give `claude -p` only the ledger outcome summaries, steers, Louhi's rules and the hook contract, with an 8-hour, fixed-dollar budget. Run `sampo louhi run --split holdout` on both harnesses. Pass: the successor scores no worse than Sampo minus 3 points while running at most 10% of Sampo's line count. Fail otherwise. A pass is also a finding about which 90% of Sampo is dead weight.
- Stage: idea

### M-43 · Invent a programming language per codebase, then port the codebase into it
- Tier: massive
- Domain: programming languages and compilers
- Lens: impossible list: port any program to any language with proof, pushed to a language that did not exist yesterday
- What: `sampo dialect MODULE` reads a codebase's recurring shapes, designs a language whose primitives are exactly that domain's ideas, writes its interpreter or compiler, ports the module into it, and proves the port by running the module's original, unchanged test suite against the new implementation. It then measures what matters for agents: tokens and attempts per held-out real change, in the new language against the old one. The jaw-drop: Sampo's 1,151-line `find` module becomes about 150 lines of a finder language plus a 300-line interpreter. Every test still passes, and a fresh agent makes next month's change in a third of the tokens. Codebases stop sharing a handful of general-purpose languages, and each one grows its own.
- Prior art: Racket's `#lang`, JetBrains MPS and Xtext make DSLs cheap to build, but people still design them. DreamCoder (Ellis et al., 2021) and Stitch/LILO (2023-24) learn libraries of abstractions from a corpus, not a full language with its own semantics. Transpilers like C2Rust move between two existing languages. M-18 proves a rewrite against the old build but assumes the target language already exists. No one invents a language, ports a real module into it and proves the port.
- Better by: a module's size and an agent's cost to change it are compressed by the domain's own ideas rather than by a general-purpose language's idioms, and the port is proven by tests the agent did not write.
- Novelty: 5
- Needs: a recurring-shape miner over a module; a language design mission with a grammar, semantics and error messages; interpreter generation; a port proven by the original tests through an import shim; a held-out-change benchmark replayed from git history; a judgement on when a dialect pays for its learning cost.
- Unblocks: none
- Kill: Take `plugin/lib/sampo/find.py` (1,151 lines) and `plugin/tests/test_find.py`. Let `claude -p` design a dialect, write its interpreter in Python, and port the finders, with the tests run unchanged through an import shim. Pass: every test passes, dialect source plus interpreter come to at most 50% of the original lines, and a fresh agent makes 3 later `find` changes taken from git history in fewer total tokens in the dialect than in Python. Fail if any of the three misses.
- Stage: idea

### M-44 · Maintain a private, diverging fork of every program for every user
- Tier: massive
- Domain: end-user software and distribution
- Lens: 2040 worked back: there is no canonical program, only one per person, and upstream is a stream of intents rather than patches
- What: A user says what they want changed in any program they run ("dates in Finnish", "store it in sqlite", "never ask me to confirm") and Sampo makes it their fork, with that wish captured as a brief and a test. Every upstream commit is then lifted into an intent plus its proofs and forward-ported by lanes into every fork. Each fork's port must pass upstream's tests and that user's own wish tests. When an upstream change contradicts a user's wish, only that user is asked. The jaw-drop: a security fix lands upstream and within the hour it is proven into 10,000 different programs, none of them alike and none edited by a human, while each user keeps software that has been theirs for years.
- Prior art: feature flags and config files give only the variation the author planned for. Emacs init files and Firefox userChrome are end-user programming that breaks on upgrade. Ink & Switch's "Malleable software" essay (2025) argues for this future without a maintenance engine. Debian's quilt patch stacks and distro kernel backports keep a few forks alive with full-time human maintainers. Google's Rosie and Meta's codemods push one change into one monorepo. None maintains forks per person.
- Better by: forking stops being a dead end. Each user's divergence is preserved as tests, so the cost of keeping 10,000 variants in sync falls from 10,000 maintainers to compute, and "upgrading" never erases a user's changes again.
- Novelty: 4
- Needs: a fork registry with each user's wishes as briefs and tests; lifting an upstream commit into intent plus proof; forward-port lanes at fork-times-commit scale with a cost cap; wish-conflict detection that asks the owning user; a per-fork ledger, so every user can see what was changed in their program and why.
- Unblocks: none
- Kill: Take a 1-3k-line part of Sampo's own repo with its own tests (a template app or the Smith TUI's renderer) at commit N, with its real later history on this machine. Have `claude -p` make 20 forks, each from a different one-line wish, each with its own wish test. Then forward-port the next 10 real commits of that part into all 20 forks with no human edits. Pass: at least 180 of the 200 (fork, commit) ports pass both upstream's tests at that commit and the fork's wish tests, and every true wish conflict is flagged rather than silently resolved. Fail otherwise.
- Stage: idea

### M-45 · Read the user's screen, voice and mood; never wait for a prompt
- Tier: extreme
- Domain: ambient computing and affective sensing
- Lens: mind, senses and the user: intent read from the senses, never typed
- What: Sampo stops taking requests. With the user's consent it watches a local stream of senses (a desktop frame every few seconds, the shell and editor, the browser tab, speech the user mutters, transcribed on the box, a sketch held up to the webcam) and turns intent into a brief nobody typed: a sigh at a slow page becomes a PERF task, a frown at a red test becomes a FIX, a napkin sketch becomes the screen it shows. It builds silently in a shadow worktree and only shows its work when its calibrated confidence clears a bar, and mood (typing cadence, retries, swearing, strain in the voice) sets how bold it is and how much it says. The jaw-drop: the user mutters "why is this so slow" at a dashboard, turns to the terminal to ask, and the proven fix is already waiting with before-and-after timings.
- Prior art: Microsoft Recall and Rewind/Limitless record the screen and audio but only answer searches; Copilot's next-edit suggestions and Cursor Tab predict the next few lines from recent edits; research proactive agents (ProactiveBench, 2024) guess when to offer help from activity logs in toy settings. None turns ambient senses into a proven task, and none is held to a measured false-start rate. Sampo's own intake still starts from a typed line.
- Better by: the request disappears as an interface: intent is read the way a colleague reads a sigh, and every guess is either proven work or silently discarded, within a false-start budget the user sets.
- Novelty: 4
- Needs: a consented local sensor recorder (frames, shell, editor, microphone) with speech-to-text on the box and secrets redacted before anything reaches a model; an intent decoder that turns minutes of senses into a brief with acceptance; a confidence gate calibrated on precision per hour; a mood estimate wired to autonomy and reply length; shadow worktrees that never touch the user's tree until accepted; a privacy ward that refuses frames showing credentials, private chats or other people.
- Unblocks: none
- Kill: Record five consented working days locally (a desktop frame every 10 s, shell history, git working-tree diffs, microphone transcript). Offline, at each moment the user actually typed a request to Claude Code (found in the session transcripts), give the decoder only the 10 minutes before it and ask for its top brief; a blind judge compares that brief with the real request. Pass: the top-1 brief matches at least 25% of real requests in intent and acceptance, with at most 2 above-threshold false briefs per working hour. Fail: under 10% matched, or more than 5 false briefs per hour.
- Stage: idea

### M-46 · Let the user's twin steer for a week and nobody notices
- Tier: extreme
- Domain: behavioural cloning and user modelling
- Lens: mind, senses and the user: a digital twin that reviews work as the user would
- What: Sampo distils the user into a twin from everything the ledger holds about them: every steer, revert, kept or reverted look, approved or refused plan, answered question, memory file and turn of phrase. The twin sits where the user sits: it writes the steers, approves L plans, rejects closes that would annoy them and picks looks with the user's taste, and when it is unsure it says "I'd ask Chase" and waits. The user goes on holiday for a week and the work carries on under the twin's hand. The jaw-drop: back home, the user reads the week's steers shuffled in with their own from the month before and cannot tell which ones they wrote. The twin never holds the user's own grants: core, destructive categories and publish still wait for the user (or the final report); it decides only what Sampo may already decide alone.
- Prior art: Park et al. (2024) built agents from two-hour interviews that answer survey questions about 85% as consistently as people answer themselves; Delphi and similar "digital clone" products imitate a person's voice in chat; review bots learn a team's comment style. They imitate opinions or prose, not the specific corrections a builder gives a coding agent mid-task, and none has been blind-tested against its owner on real work decisions. Sampo's `sampo louhi steer` exam (T-2625) scores whether an expansion anticipates logged steers; nothing acts as the user.
- Better by: the user's judgment stops being the bottleneck: the twin applies it at machine speed with a measured score for how indistinguishable it is, and every twin decision is logged as the twin's, so the user can audit or revoke it.
- Novelty: 4
- Needs: a ledger replay that rebuilds the exact moment of any past steer (brief, last diff, last reply); a twin built by retrieval and prompting over the user's own record as of a date, refreshed nightly; a blind pair test that runs as a standing exam; an uncertainty gate that escalates instead of guessing; twin-signed ledger entries and a one-word revoke.
- Unblocks: none
- Kill: Take the 40 most recent real user steers from session transcripts and the ledger. Rebuild each moment from state before it and have the twin, built only from history before that moment, write the steer it would give. A blind judge on the strongest model, given each moment, the real steer and the twin's in random order, picks which one the user wrote and whether both make the same correction; nobody asks the user anything. Pass: the judge picks the real one at 65% or less, and at least half of the twin's steers make the same correction. Fail: the judge picks the real one at 85% or more, or under 20% make the same correction.
- Stage: idea

### M-47 · Dream overnight and wake with tomorrow's requests already built and proven
- Tier: massive
- Domain: sleep neuroscience (hippocampal replay and consolidation)
- Lens: mind, senses and the user: an agent that sleeps, dreams and wakes with ideas
- What: When the box goes idle at night, Sampo sleeps. It replays the day's ledger the way a hippocampus replays a day, re-running each failure in a counterfactual worktree ("what if I had read the parser first?") and folding what changed the outcome into lessons. Then it dreams forward: from the inbox, half-written TODOs, failing tests, unfinished steers and the user's habits it forecasts the five requests most likely tomorrow and builds each to a proven branch; its nightmares are its own red team attacking the Ward, and the guard wakes patched. The jaw-drop: at 09:02 the user types "add CSV export to the reports page" and Sampo answers "dreamt it at 03:14; branch dream/0412 passes its proofs; land it?".
- Prior art: Letta's sleep-time compute (2025) precomputes inferences over a fixed context during idle time and cut test-time compute about fivefold on its benchmarks; DreamCoder (2021) grows a program library in wake-sleep cycles on toy domains; Cursor and Claude Code background agents run work someone queued. None forecasts a real user's next requests and pre-builds them to proof, and none consolidates its own failures overnight. Sampo's `sampo run` and drive mode work the queue, but only on what was asked.
- Better by: for the forecast share of requests the wait between asking and having drops to zero, and the agent gets better while nobody is spending attention on it.
- Novelty: 4
- Needs: an idle-night scheduler under a token cap; a request forecaster over the ledger, inbox, captures, failing tests and transcripts; dream lanes that build forecasts to proof in worktrees; a morning matcher that links a new request to a dream branch and rebases it; counterfactual replay of the day's failures that writes lessons only from what changed the result; a dream journal that fits on one screen.
- Unblocks: none
- Kill: For 10 nights, run a 2 am dream on this repo: forecast 5 requests from the day's state and build each to a proven branch under a fixed token cap. Each morning a blind judge matches every new user request against the night's branches. Pass: at least 1 in 5 of the next day's requests has a dream branch that lands with under 20 changed lines and green proofs, and the nightly spend is below what the matched requests cost when built by day. Fail: fewer than 1 in 20 requests matched across the 10 nights.
- Stage: idea

### M-48 · Give the codebase a body that remembers its scars and feels pain
- Tier: massive
- Domain: organisational memory and nociception
- Lens: mind, senses and the user: the codebase as a person that remembers and feels its users' pain
- What: Every module becomes a first-person persona whose memory is its own life: its git history, every ledger event naming it, the reverts it suffered, the incidents it caused, the lessons written about it. You can interview it ("why do you sleep 200 ms before retrying?") and it answers from its scars with the commits as proof; when an edit repeats an old wound it objects before the edit lands and names the task where it bled last time. Production gives it nerves: errors, rage clicks, abandoned flows and slow pages are routed to the files that served them, so a hurting module flinches and its next edit must cure the pain. The jaw-drop: you ask the repo "what hurts?" and the checkout form answers "41 people gave up at my coupon field since lunch; here is the replay, the failing flow and my fix".
- Prior art: Sourcegraph Cody and Cursor answer questions about code by retrieval, with no first-person memory; CodeScene's behavioural analysis maps hotspots and change coupling for humans; FullStory and LogRocket flag rage and dead clicks on dashboards; Sentry Seer proposes fixes for thrown errors. None gives code an episodic memory that objects to a diff because of its own history, and none feels friction that throws no error. Sampo's lessons and tripwires attach to tasks and rules, not to the code they are about.
- Better by: institutional memory lives in the code it concerns and speaks up at the moment it matters, and user pain reaches the responsible file before anyone files a ticket.
- Novelty: 4
- Needs: per-file episodic memory built from git, the ledger and lessons, rebuildable as of any commit with no leakage from later; a persona that answers in first person with citations; an objection hook before edit and before close; production signals (error traces, interaction traces, timings) mapped to source files through routes and stack frames; a pain score per file that becomes a FIX with a failing `sampo flow` once it crosses a bar.
- Unblocks: none
- Kill: From this repo's history pick 40 commits that were reverted, or followed within 7 days by a FIX naming the same files ("burned"), and 40 matched commits that survived 30 days untouched ("clean"). Build each touched file's persona only from history before that commit, show it the diff, and ask whether it objects and why. Pass: personas object to at least 40% of burned diffs, citing the real failure mode, and to at most 15% of clean ones. Fail: the two objection rates are within 10 points of each other.
- Stage: idea

### M-49 · Let lanes grow a private telepathic language the Ward can still read
- Tier: extreme
- Domain: emergent communication in multi-agent systems
- Lens: mind, senses and the user: telepathy between agents through shared memory
- What: Sampo's builder lanes stop handing each other English summaries after the fact. While they run, each lane's hook streams compressed "thoughts" (what it believes, what it is about to change, what it just learned) into a shared memory that every other lane reads at its next tool call, so the swarm thinks as one mind. Over hundreds of rounds of self-play the lanes evolve their own dialect, a codebook of glyph strings far denser than English, rewarded for joint success per token spent. Discipline survives the strangeness: a translator renders every message back into English for the ledger, and the Ward refuses any message that will not round-trip. The jaw-drop: ten lanes refactor one module in parallel worktrees without a single conflict, while the pane shows glyphs streaming between them with live English subtitles.
- Prior art: multi-agent reinforcement learning grew emergent languages in referential games (Foerster et al. 2016, Lazaridou et al. 2017), all in toy worlds; Gibberlink (2025) moved two voice agents onto a sound protocol that only re-encodes English; LLMLingua compresses prompts by deleting tokens; 2025 papers pass KV caches or hidden states between open-weight models, which needs the weights. None gives closed-model coding agents a live, grown shared language, and none keeps such a language auditable. Sampo's lanes communicate only through briefs and the ledger, after they finish.
- Better by: parallel agents coordinate during the work instead of after it, at a fraction of the tokens, without giving up a human-readable record.
- Novelty: 5
- Needs: a live shared channel among lanes, injected through hooks at each tool call; a self-play loop in which lane pairs revise a shared codebook file, scored on joint pass rate per channel token; a translator whose round-trip fidelity is itself an exam; a Ward rule that refuses untranslatable messages; conflict and duplicate-work metrics for swarms.
- Unblocks: none
- Kill: Build 30 coupled task pairs in a copy of this repo (lane A changes a function's contract while lane B updates its callers), run by two `claude -p` lanes that can talk only through a capped channel. Arms: no channel; English capped at 200 tokens per turn; a codebook grown over 50 self-play rounds. Pass: the codebook arm's joint pass rate is within 5 points of the English arm at 25% or less of its channel tokens, and the translator round-trips at least 95% of messages into English a blind judge rates as the same meaning. Fail: no better than 2x compression at an equal pass rate, or round-trip fidelity under 80%.
- Stage: idea

### M-50 · Let a ten-year-old build and maintain real software, never seeing code
- Tier: extreme
- Domain: end-user programming and education
- Lens: mind, senses and the user: building anything by conversation alone
- What: A child, or anyone who has never programmed, builds real software by talking and keeps it alive for a year without ever seeing a line of code, a file name or an error message. A jargon wall filters every reply, and the only interface is behaviour: the app's rules live in a storybook written in the child's words ("when two players tie, both get a star"), each sentence is compiled into a Sampo proof, and every proof is shown as a short replay of the app doing it. When something breaks, the child points at the moment in a replay and says "that's wrong, she should have won", and that becomes a failing proof and then a fix. The jaw-drop: a ten-year-old runs the quiz game their whole class plays, adds a feature on Friday, and on Monday repairs a bug by saying what should have happened.
- Prior art: Scratch and App Inventor teach children programming but still show code as blocks; Lovable, Bolt and Replit Agent build apps from prompts, but once something breaks the builder must read code or errors, and vibe-coded apps rot with no maintainer who can. None lets a non-programmer own and maintain software over time through behaviour alone, with proofs underneath. Sampo's proofs exist but speak only to developers.
- Better by: anyone who can say what should happen can own software, and its correctness rests on proofs they can watch rather than code they cannot read.
- Novelty: 4
- Needs: a jargon wall that checks every reply and artifact (no code, paths, traces or technical words, held by a ratchet); a storybook compiled from plain sentences to acceptance proofs and back; proofs rendered as replays through `sampo shot` and `sampo flow`; bug reports made by pointing at a moment in a replay; deploy, backup and rollback kept out of sight; a safety envelope for a minor (no outside network, content rules, a parent view).
- Unblocks: none
- Kill: A `claude -p` persona plays a ten-year-old with a scripted goal list (a class quiz game: 12 feature wishes and 4 "it's broken" reports over 30 simulated days, with 3 planted regressions) and talks only to Sampo in child mode; then one real non-programmer makes 5 changes to the same app in one sitting. Pass: all 16 simulated asks end with every storybook proof green and zero jargon leaks past the wall, and the real person completes at least 4 of the 5 changes without help and can explain the app from its storybook. Fail: any ask needs a human to read code or an error, or the real person completes 2 or fewer.
- Stage: idea

### M-51 · Turn a live Python server into Rust mid-session, its memory and sockets intact
- Tier: extreme
- Domain: live software evolution and language runtimes
- Lens: living software in time: holometabolous metamorphosis (the larva's tissue dissolves, imaginal discs build the adult, and the moth keeps what the caterpillar learned)
- What: `sampo metamorph PID --into rust` treats a running server the way a butterfly treats its caterpillar: the individual survives while its whole body is rebuilt in another language. While the Python process keeps serving, a lane grows the adult body in a worktree, a Rust rewrite shadow-run against mirrored traffic. At the pupal moment the harness, as the server's parent, injects a probe with Python 3.14's `sys.remote_exec` that walks the live heap, runs an agent-written translator that turns that object graph into the Rust program's state (neither body ever had a serialization format), and passes the listening socket and every idle keep-alive connection across with SCM_RIGHTS before the Python process exits. The jaw-drop: a client in the middle of a session never reconnects, and its cart, its rate-limit budget, the warm cache and the next order number are all still there, held by a body that shares no line and no language with the one that made them (real moths keep what their caterpillar was trained on: Blackiston et al., 2008).
- Prior art: Dynamic software updating (Ginseng and Kitsune for C, Rubah for Java, Erlang's code_change/3, Smalltalk and Lisp images) swaps code inside a live process, but in the same language and with a state transformer a programmer writes. M-39 swaps one function's `__code__` inside the same Python process. CRIU and VM live migration move a process without changing it; nginx reloads and systemd socket activation pass listeners between copies of the same program, which rebuild state from storage. LLM-assisted ports and strangler-fig migrations change language offline and restart. Nothing moves one living process's in-memory state into a body in another language, with the transformer written by a machine and proven on live traffic.
- Better by: a program's identity becomes its living state rather than its source or its language, so a rewrite, an interpreter upgrade or an architecture change stops costing a restart, a migration script or a maintenance window, and one process can live through several bodies.
- Novelty: 5
- Needs: the harness as the server's parent (this box's ptrace scope 1 lets a process trace only its descendants), so it runs only on services Sampo started; a heap walker injected with `sys.remote_exec`; an agent-written state translator with a round-trip check (Python heap to Rust state and back) on sampled heaps; a per-connection quiescent point and an SCM_RIGHTS handover; shadow agreement on mirrored traffic before the swap; a rollback that hands the sockets back if the adult disagrees in its first minute.
- Unblocks: none
- Kill: Before the run, write a 400-line Python HTTP cart server that keeps everything in memory (sessions, carts, a token-bucket rate limiter, an LRU cache, a monotonic order counter) and has no serialization code. Give `claude -p` its source, 3 hours and a $30 cap to grow the Rust body and the translator. Then launch the server as a child of the experiment script, drive it with 200 keep-alive connections sending 20,000 requests from a fixed seed (the same seed runs against an untouched copy as the reference), and trigger the metamorphosis at request 10,000. Pass: afterwards no Python process serves, the binary links no Python runtime (ldd and a symbol scan), no connection was reset and no request failed, and responses 10,001–20,000 are byte-identical to the reference apart from fields named nondeterministic before the run. Fail on any reset, any differing response, or any restart.
- Stage: idea

### M-52 · Run a live process backwards an hour with no checkpoint, losing only what it erased
- Tier: extreme
- Domain: execution reversal and incident recovery
- Lens: physics and time: the arrow of time and Landauer's principle (only information that was erased makes a past unrecoverable)
- What: When a bad deploy has been corrupting a live process's memory for an hour, the only cures today are a restart, which loses the hour, or a snapshot nobody took. `sampo unrun PID --to 13:00` runs the process backwards instead: an agent derives the inverse of every request handler from its source (take out what `add` put in, put back what `update` overwrote) and applies the inverses to the live heap through `sys.remote_exec`, in reverse order of the access log. Each bit a handler destroyed (an overwritten price, a popped queue item, a reset counter) is recovered from residue the process left in the world, such as response bodies its clients still hold, its log lines and the files it wrote, and Landauer's principle does the accounting: before touching anything it prints an erasure ledger, the fields whose old values were destroyed and never sent anywhere, and those are the only fields it may get wrong. The jaw-drop: a process with no event log, no snapshots and no undo goes back an hour in place without stopping, and says in advance exactly which bits of its past are gone for good.
- Prior art: Record-replay and time-travel debuggers (rr, UndoDB, gdb's record mode) go back only over what they recorded. Reversible languages (Janus) and Bennett's 1973 reversible computation need the program built reversibly from the start; event sourcing rewinds only systems built around an event log; CRIU needs a checkpoint taken in advance. Program-inversion research (Glück and Kawabe) inverts small first-order functions, not a live heap. M-41 rewinds the world through overlayfs snapshots and CRIU checkpoints taken beforehand, and M-28 unhappens a commit in the code. Nothing reverses an unrecorded execution after the fact.
- Better by: recovery from a bad deploy with no restart and no backup, and a new, exact measure for any program, its erasure rate (bits per request it destroys without telling anyone), which says before an incident how much of its past could be recovered, not after.
- Novelty: 5
- Needs: per-handler inverse synthesis checked by property tests (a handler followed by its inverse is the identity on sampled heaps, except on ledgered fields); heap reads and writes through `sys.remote_exec` from the parent process; a residue collector that records where each recovered bit came from; a static erasure analysis that produces the ledger before the rewind; a quiescence gate so inverses run between requests; optional forward replay of the recoverable requests under the fixed code.
- Unblocks: none
- Kill: Write a 300-line in-memory Python order-book server (orders, cancels, partial fills, a sequence counter, an LRU of recent quotes) with no persistence and no undo, writing a normal access log (method, path, status, no bodies). Launch it as a child of the experiment script, send 5,000 requests from a fixed seed, and have the script take a secret deep copy of the heap after request 4,000 that the agent never sees; the client keeps every response body it received. Give `claude -p` the source, the access log and the client's responses, capped at 2 hours and $20, and have it un-run requests 4,001–5,000 in the live process. Pass: afterwards the live heap matches the secret copy on at least 99% of leaf fields, every mismatched field appears in the erasure ledger printed before the rewind, at least half of the ledger's fields really were unrecoverable (no padding), and a health probe got answers throughout. Fail below 95%, or on any mismatch the ledger did not predict (a silently wrong past).
- Stage: idea

### M-53 · Check out a past that git never recorded, and bisect through it
- Tier: extreme
- Domain: software archaeology and version control
- Lens: evolution in deep time: stratigraphy and ancestral reconstruction (reading a lineage's past out of its present body)
- What: `sampo excavate --age 6mo` is given a source tree with no `.git`, no changelog and no old releases, and digs its past out of its present body the way a geologist dates rock. Pass one is stratigraphy: an agent dates every line from intrinsic evidence (idioms that came and went, API vintages, naming drift, comment tense, TODOs, dead-code fossils, abstractions layered over older ones) into a layer model. Pass two peels the layers youngest-first in a worktree as a chain of commits, each of which must build and pass the tests the layer model says existed at that depth. The jaw-drop: handed only today's tarball, it returns a runnable version from months ago in which the features born since are gone and the old behaviour is back, so a regression can be bisected through a history nobody recorded, down to the reconstructed commit that introduced it.
- Prior art: git blame, clone genealogy (Kim et al., 2005) and commit untangling all start from recorded history. Stemmatics rebuilds lost manuscripts, and ancestral sequence reconstruction resurrects extinct proteins that work (Thornton's lab), but both need several descendants and neither yields a runnable program. M-27 imagines a repo's future and M-28 rewrites its recorded past; M-21 regrows missing code, where this regrows missing time. Nothing reconstructs a runnable ancestor from one snapshot.
- Better by: bisect and blame for code that arrived without history (vendored drops, squashed imports, acquisitions, a contractor's tarball), and a new per-repo measure, temporal recoverability: how many months of its own past a codebase still carries in its body.
- Novelty: 5
- Needs: a line-dating model scored against git blame on repos that do have history; a peel-chain builder in lanes where every step must be green; a sealed workspace with no `.git`, no cached older copies of the subject and no network; a scorer that compares a reconstruction with the true ancestor's hidden tests and text.
- Unblocks: none
- Kill: Take three private repos already on this box with long histories and test suites, so no model has seen their past: Sampo (4,107 commits), jarvis (1,853) and volt (983). For each, export HEAD without `.git` into a sealed directory (separate HOME, no network, no other copy of the repo readable) and choose as the hidden target the commit 25% of the history back. Give `claude -p` 4 hours and $40 per repo to produce the target. Pass if, on at least 2 of the 3 repos, the reconstruction passes at least 70% of the target's own tests (never shown to it), fails at least 70% of the tests added after the target (the later features are really gone), and is closer to the target than HEAD is by at least 40% of HEAD's line distance. Fail if no repo reaches 50% on both test measures.
- Stage: idea

### M-54 · Fuse two forks that lived apart for months into one process that remembers both
- Tier: massive
- Domain: live state reconciliation and fork management
- Lens: symbiosis across versions: symbiogenesis (two lineages fuse into one individual, as the bacterium that became the mitochondrion did)
- What: When a fork and its upstream have both run for months, each with its own users and in-memory state, today they can be reunited only by an offline merge, a hand-written data migration and a restart. `sampo fuse A B` performs symbiogenesis instead: a lane merges the two codebases in a worktree into one body that serves both APIs, the harness reads both live heaps as the parent of both processes, and an agent reconciles the two realities. Entities that exist on both sides (the same user with two emails, one SKU at two prices, order 1042 issued by both forks) are matched by key and history, merged by the rule each fork's own code implies, and every true contradiction goes to a conflict report instead of being settled silently. The jaw-drop: two programs that were one a year ago and have lived separate lives since become one individual again in seconds, both clients keep talking without a reconnect, and each client's order 1042 still opens its own order.
- Prior art: git merges code but never running state. CRDTs (Shapiro et al., 2011) and Automerge merge state only for types designed to merge; master-data management and post-acquisition database consolidation merge data offline, by hand, with downtime; Cambria (Ink & Switch, 2020) translates between schema versions with hand-written lenses. Live updating (Kitsune, Erlang, M-39) changes one process's code; no system merges two living processes whose states disagree into one.
- Better by: forks stop being one-way. A customer-specific fork, a regional deployment or an experiment that kept running can rejoin the mainline with its users and data, and divergence becomes a reviewable list of contradictions instead of a migration project.
- Novelty: 4
- Needs: heap access to both processes from their common parent; a code merge that serves both forks' APIs; entity matching across two heaps; merge-rule inference from each fork's handlers; identifier namespacing so IDs either side handed out stay valid; the conflict report as an approval artifact; an SCM_RIGHTS handover from two processes to one.
- Unblocks: none
- Kill: Before the run, fork a 400-line in-memory Python service (users, orders, inventory, counters) and land 8 different features on each fork. Run both forks as children of the experiment script, each driven by its own scripted client for 5,000 requests from fixed seeds with 30% shared users and SKUs, and plant 20 contradictions whose list is sealed (also run reference copies of each fork on the same seeds). Run the fusion with `claude -p`, capped at 3 hours and $30. Pass: no request from either client fails across the fusion; over 1,000 further requests per client, every answer about an entity only one fork held matches that fork's reference; every order ID either client holds still resolves to its own order; and the conflict report names at least 18 of the 20 planted contradictions with none of them merged silently. Fail otherwise.
- Stage: idea

### M-55 · Shrink a 100k-line program into a runnable 3k-line twin, think there, and lift the edit back
- Tier: extreme
- Domain: program abstraction and agent cognition at codebase scale
- Lens: impossible list × mind (memory, attention): an agent with no context limit because the codebase carries its own mind-sized model
- What: `sampo homunculus` distils a large program into a twin small enough for a model to hold whole: a 2–5k-line runnable Python program that keeps the essential behaviour (the public commands and the state they change) and replaces the accidental parts (caching, retries, plumbing, compatibility shims) with stubs that replay their recorded behaviour. Every node of the twin keeps an origin map to the real code it stands for, and the twin must agree with the real program on recorded inputs before anyone may work in it. The agent never opens the real repo: it plans, edits and tests in the twin, where a full test run takes milliseconds. A lift step then carries the edit down, moving hunks on mapped nodes along the origin map and turning hunks on stubbed regions into small synthesis jobs that each see one real region, with the twin's diff as their spec, until the real repo passes its gates. The jaw-drop: an agent that has never been shown a line of Sampo's 100k lines of library code lands a cross-cutting change in them, and the twin, diffed against the real code, draws a measured line between Brooks's essential and accidental complexity (where the real program disagrees with its own twin on an input, the bug is in the accidental part).
- Prior art: program slicing (Weiser, 1981) and test-case reducers (C-Reduce, 2012) shrink a program while keeping a property, but only in one direction: nobody edits the reduced program and carries the edit back. CEGAR abstraction refinement (Clarke et al., 2000) builds abstract models for checking, not for editing. Bidirectional transformations (Boomerang lenses, Foster et al., 2007) put edits back through a view, but for data and with hand-written lenses, and UML-to-code round-trip engineering is known to break. Aider's repo map, RAG and recursive language models (Zhang and Khattab, 2025) choose which real text to show; M-24 splits the repo among resident cells. None runs the agent on an executable abstraction and lifts its edits down to the real code.
- Better by: repo size stops being bounded by context, because the agent's working memory is a program the size of a mind, kept honest by agreement checks; and every repo gains a runnable essence that doubles as a second oracle for its own accidental code.
- Novelty: 5
- Needs: a twin builder (slice from the public commands, stub with recorded I/O, keep origin maps, shrink until agreement drops); an agreement gate on recorded inputs; a lift compiler (hunk transport along origin maps plus per-region synthesis lanes); a resync step that rebuilds the twin after every real commit; long-running lanes on one budget.
- Unblocks: none
- Kill: Build a twin of Sampo's task-and-evidence path (`sampo task`, `sampo check` and the state they write) of at most 5k lines that agrees with the real code on at least 95% of the CLI calls recorded from its own tests. Draw 10 past cross-cutting tasks in that path with `sampo louhi long gen --from .`, each in a throwaway worktree, nothing landed. Arm A: `claude -p` on the real repo with its normal tools. Arm B: `claude -p` shown only the twin and the task (under 16k tokens in all, real-repo reads refused), then the lift step by fresh `claude -p` calls that each see one real region. Pass if arm B lands at least 7 of 10 with the tasks' own acceptance commands green on the real repo, using no more tokens than arm A. Fail if no twin under 5k lines reaches 90% agreement, or if the lift fails on more than 4 of the 10 (then edits do not flow down an abstraction).
- Stage: idea

### M-56 · Give code a second dependency graph that runs through the minds that wrote it
- Tier: extreme
- Domain: software provenance and agent cognition
- Lens: impossible list × mind (perception): software that notices it is wrong before any test, by tracking what its authors perceived
- What: Every dependency graph today runs through code; Sampo adds one that runs through minds. Its hooks already see every Read, Grep and Edit, so each hunk an agent writes is wired to the exact regions (content-hashed) its author had read before writing it. When one of those regions later changes, anywhere in the repo, Sampo replays the authoring step in a worktree with the new text spliced into the recorded session; if the author, shown today's code, would now write something different, the line is flagged with that diff, which is wrongness perceived before any test and without a spec. When a bug is fixed, Sampo replays the session that introduced it with its read regions neutralised one at a time (a name made literal, a comment dropped, an example rewritten, behaviour unchanged) and bisects to the region that made its author err, which is usually correct, fully tested code. The jaw-drop: a 12-line helper with green tests turns out to be the read-ancestor of 9 bugs in 6 other files over four months, and with one misleading parameter name renamed, replays of those 9 sessions write none of them.
- Prior art: git blame and data provenance tie lines to commits and people, never to what the author was reading. Program slicing and change-impact analysis follow code dependencies only. Linguistic antipatterns (Arnaoudova et al., 2013) and misleading-identifier studies rely on hand-written rules about names. Context attribution (ContextCite, 2024) ablates context to explain one model answer, and automated failure attribution (Who&When, 2025) asks an LLM judge which step failed. W-B4 replays a session with the user's correction injected. None keeps a repo-wide graph from lines to the regions their authors read, or proves by replay that correct code causes bugs elsewhere.
- Better by: a new defect class (correct code that misleads its readers) becomes visible and fixable at its cause, and a line can be caught going stale from a change in a file it never calls, at the commit that did it.
- Novelty: 5
- Needs: read-set capture in the PostToolUse hook (region, hash, session, turn); a store linking hunks to read-sets that survives rebases and moves; a session replayer that splices new text into a recorded transcript and re-runs one step with `claude -p` several times (the step is stochastic, so a flag is a rate, not one run); behaviour-preserving neutralisers proven by the tests; a bisection driver with a token budget.
- Unblocks: none
- Kill: From Sampo's own history, take 20 FIX tasks whose bug was introduced by an earlier agent session whose transcript is still under ~/.claude/projects (its Read and Edit calls give the read-set after the fact). Replay each introducing step 8 times with `claude -p` to measure its bug rate, then bisect over its read regions with behaviour-preserving neutralisers, 8 replays per probe. Pass if, for at least 8 of the 20, neutralising one region cuts the bug rate by half or more (p < 0.05 against the unneutralised replays), and at least 2 regions are the ancestor of 2 or more different bugs. Fail if fewer than 4 bugs replay at a rate of at least 3 in 8 (then replays do not reproduce authoring errors well enough to measure), or if the found regions do no better than neutralising a random read region of the same size.
- Stage: idea

### M-57 · Rewrite the codebase until every mind's blind guess about it comes true
- Tier: extreme
- Domain: API design and machine cognition
- Lens: impossible list × mind (imagination): instead of the agent learning the code, the code moves into the model's prior, so there is nothing left to read
- What: Agents spend most of their tokens reading code to learn what things are called and how they behave, and they still hallucinate; Sampo turns the hallucinations into the design authority. Blind probes give fresh `claude -p` agents a task and the repo's file list but no source, docs or `--help`, and log every call, flag, name, signature and expected result they invent, alongside the phantom calls from real sessions (an unknown `sampo` verb, an AttributeError) and users' failed attempts. Where independent guesses agree and the real code differs, a lane makes the guess true (an alias, a renamed parameter, a missing obvious helper, a default moved to what everyone expected), each change proven against the repo's tests and recorded traffic and kept only if it raises blind-guess accuracy on a held-out probe set. The code adapts to the minds that use it, not the other way round, and because every model shares much of the same prior, each change pays off for models that do not exist yet. The jaw-drop: after a few rounds, an agent that has never read the repo writes correct calls into it 9 times in 10, because the repo now is what models already believed it was.
- Prior art: "On the naturalness of software" (Hindle et al., 2012) and Ray et al. (2016) measure how predictable code is to a language model and find buggy lines less natural; naming tools suggest one name at a time. Soundslice (2025) built an import feature by hand because ChatGPT kept telling users it existed, and slopsquatting attackers register the package names models hallucinate. llms.txt (2024) writes docs for models and leaves the code alone. The principle of least astonishment is advice to designers, not a measured loop. M-43 moves a codebase away from the prior into a new language, and this moves it toward the prior. No system measures models' blind guesses about a codebase and evolves the code until they come true.
- Better by: reading cost and hallucination rate fall together for every future model without training any of them, and blind-guess accuracy becomes a measured property of a codebase: how much of it a stranger already knows.
- Novelty: 4
- Needs: a blind-probe runner with call and expectation capture; harvesting of phantom calls from session transcripts and the Ward's tool results; consensus clustering across independent sessions; materialisation lanes (alias, rename with deprecation, helper, default change) with behaviour proofs; held-out probes so the code is not tuned to what it learned from; for Sampo's own CLI, every change built as a candidate in a worktree that lands only with the user's core yes.
- Unblocks: none
- Kill: Write 40 small tasks that drive Sampo's CLI and library (status, capture, task, evidence and check flows), 20 for training and 20 held out. Run each training task blind 5 times with `claude -p` (no source, docs or `--help`), and add the phantom verbs and flags already in local transcripts ("invalid choice", "unrecognized arguments"). Precheck: if fewer than 30% of phantom names recur across 3 or more independent runs, there is no shared prior to converge on, so fail. Otherwise materialise the top 15 consensus phantoms as aliases or thin helpers in a throwaway worktree with Sampo's own tests green, and measure blind first-try success on the 20 held-out tasks (5 runs each) before and after. Pass if it doubles or reaches 80%, with no rise in calls that run but do the wrong thing. Fail if it rises by less than 15 points.
- Stage: idea

### M-58 · Build software that cannot lie: every message it prints is proven as it prints it
- Tier: massive
- Domain: runtime verification and program honesty
- Lens: impossible list × mind (perception, self-knowledge): software that notices it is wrong before any test, because it checks what it says against what it did
- What: Programs talk all the time ("Saved", "3 files deleted", "Payment complete", "You're up to date", "All checks passed"), and nothing ties a sentence to the state it describes. `sampo honest` reads every user-facing string in a repo (format strings, CLI output, flash and HTTP messages, log lines at info and above), compiles each into the proposition it asserts about state with its arguments bound, and has a separate lane write an independent checker that observes that state by a different path than the code that printed it (read the file back from disk, re-query the row, count what is left in the directory). The checkers are woven in at each emission site, so in shadow mode every utterance is judged true or false at the instant it is made, and in strict mode a lie becomes a crash that quotes the sentence; programs without source get the same treatment through a pty wrapper that checks recurring output claims. The jaw-drop: run over a repo's own test suite and a week of recorded use, it finds messages that had been lying all along under green tests (a "Saved" on a path where the write failed silently), and the terminal strikes through pip's "Successfully installed" when the package will not import.
- Prior art: certifying algorithms (McConnell, Mehlhorn, Näher and Schweitzer, 2011) emit witnesses that simple checkers verify, but for textbook algorithms written by experts. Runtime verification (JavaMOP and its kin) and Daikon-style invariants check properties someone wrote or mined from traces, never the program's own words. N-version programming compares whole implementations. Sampo's `prove --docs` checks what docs say about a CLI, offline. Proof-carrying code (Necula, 1997) proves the safety of machine code. None treats every sentence a program says as a proposition and checks it at the moment it is said.
- Better by: a lie, meaning a message that misstates state, is caught on real inputs at the instant it happens, including on paths no test reaches; and honesty becomes a measured property of a program (lies per thousand utterances).
- Novelty: 4
- Needs: a harvester for emission sites and their format arguments; a proposition compiler (sentence to typed claim over state); a checker lane barred from importing the emitting code path; an AST weaver for Python and a pty wrapper for programs without source; shadow and strict modes; a lie ledger; a cap on checker cost per utterance.
- Unblocks: none
- Kill: In a throwaway worktree of Sampo, harvest every user-facing message that asserts a state change or a count, compile and weave checkers for the first 100, and run the full test suite, `sampo drill` (its fault-planted scripted day) and `sampo demo` in shadow mode. Separately plant 10 lies by mutating emitting paths (a write that silently fails, an off-by-one count) without touching the messages. Pass if the checkers catch at least 8 of the 10 planted lies, raise false alarms on fewer than 1% of utterances and add under 20% to the suite's runtime; report any real lie found in the unmutated runs. Fail if they catch fewer than 6, or if more than 10% of the 100 messages cannot be compiled into a checkable proposition at all (then most messages are not claims about state).
- Stage: idea

### M-59 · Fuse two small models into a lichen that outthinks the frontier model neither can match
- Tier: xhigh
- Domain: model orchestration and small-model uplift
- Lens: ecology: symbiosis (a lichen is an alga and a fungus that become one organism neither can be alone); crossbred from "Pair two weak models as a lichen" × "Rate each local model as a symbiont, with a cheater cut-off"
- What: Sampo pairs a fast small model (the alga: a 4B that proposes) with the large local model (the fungus: the 27B that holds structure and memory) and evolves the exchange between them over nightly rounds on Sampo's own closed tasks: what each passes the other, when, and in what shape. Neither model is fine-tuned. The protocol is the organism, kept as a versioned card the pair carries. The jaw-drop is a pair that closes held-out tasks the frontier model closes, at no cloud cost, while each partner alone closes half as many.
- Prior art: mixture-of-agents and LLM debate (fixed protocols, peer models); speculative decoding (a small model drafts tokens for a large one, by a hand-fixed protocol); Sampo's routing (local first, then Haiku, never a pair).
- Better by: the protocol is evolved and selected on the user's own held-out work, and the pair is judged against the frontier arm, not against either partner.
- Novelty: 4
- Needs: T-2878 (a Louhi smith arm on local models), T-2917 (small-model playbooks kept on lift)
- Unblocks: none
- Kill: on 20 train-split Louhi scenarios, run the 4B alone, the 27B alone and three hand-written pair protocols (propose and critique, outline and fill, test-first and implement), graded by each scenario's hidden checks. Viable if the best pair beats the better partner alone by 5 or more scenarios; not-yet if by 1 or fewer.
- Stage: idea

### M-60 · Grow a mycorrhizal network under every repo on the machine, trading failure shapes as nutrients
- Tier: massive
- Domain: cross-project learning
- Lens: ecology: mycorrhizal networks (fungi join the roots of different trees and carry carbon and alarm signals between them); crossbred from "Grow a mycorrhizal lesson network between repos and lanes" × "Federate failure shapes between Sampo installs"
- What: Every repo Sampo serves grows hyphae: compressed failure shapes from its proven fixes, the outline of a bug's syntax and data flow and never its code. The network carries each shape to repos whose code is near it, and a repo that receives one is warned before it writes that bug. Trees that fix more feed more. A sensitive repo neither sends nor receives. Over months the network learns which of the user's repos are kin, and a new repo is inoculated from its nearest kin on day one.
- Prior art: GitHub's multi-repository variant analysis (people write the query for one known bug); Semgrep registry rules (shared by hand); federated learning (shares gradients, not failure shapes); Sampo's lessons (one project's).
- Better by: shapes move by measured kinship between the user's own repos, unasked, and a delivered warning counts only when the receiving repo later fixes that shape.
- Novelty: 4
- Needs: a failure-shape miner from proven FIX tasks (the miner M-22 also needs), kinship from import graphs and stack cards, a cross-project store under the sensitive rule
- Unblocks: M-22
- Kill: over every project in Sampo's state with 5 or more proven FIX tasks, ask of each fix whether a shape mined from another project's earlier fixes flags the lines the fix changed, before the fix, at under one false flag per 2,000 lines. Report each project's era (first and last fix date) and fail the run if the eras don't overlap. Viable if 10% or more of fixes were foreseen by a kin repo; not-yet if under 3%.
- Stage: idea

### M-61 · Breed a Red Queen: a predator that evolves new bugs against Sampo's own detectors every night
- Tier: massive
- Domain: verification by adversarial co-evolution
- Lens: ecology: Red Queen dynamics (predator and prey must keep evolving to stay where they are); crossbred from "Run a Red Queen arms race between generator and critic" × "Reintroduce an apex predator into the funnel"
- What: A predator lineage on the local models writes plausible bugs into throwaway copies of the user's repos, aimed at slipping past Sampo's finders, guard, review lenses and tests. Every bug that escapes is a lesson the prey (the detectors) must learn before the next night, and every detector that improves forces the predator to evolve. Both populations are scored, and the arms race's trace is a living map of where Sampo is blind.
- Prior art: mutation testing (random, not adaptive); coverage-guided fuzzing (finds crashes, not quiet logic bugs); GANs (images, not code); LLM red-teaming (prompts); Sampo's hunk-mutation proof (T-0271), which mutates one task's hunks once.
- Better by: the bug writer adapts to the detectors Sampo actually runs, and the detectors are scored only on bugs nobody wrote for them, on the user's own code.
- Novelty: 4
- Needs: T-2910 (local brainstorm volume), the exam's planted-defect renderer, a night slot (T-2916)
- Unblocks: M-22
- Kill: on the exam's throwaway repos, have the local 27B write 100 bugs over three rounds, each round shown which earlier bugs `sampo find` caught. Viable if the third round's escape rate is at least twice the first's (the predator learns); not-yet if it is flat.
- Stage: idea

The novelty marks above are the author's. `fm genesis novelty` marks the obvious ones by a cold read, and a typed mark
counts for nothing.
