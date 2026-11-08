{{goal}}

RESEARCH!: Genesis. Build {{project}} into its complete, best-in-class version: Sampo finds, thinks through and does the work on its own, and seeds itself to get there
{{context}}
CONTEXT: This message is Generation 0. It writes a Seed. The Seed writes one Mission per capability gap. Missions write and build the briefs, then critique themselves. Each generation is selected on the exam and the next one is written from the critiques.
MUST: full autonomy. Every decision through fm decide. Never ask the user anything mid-run.
MUST: no spend cap on building and proving. Tokens go into {{project}}'s code and its tests, not into sweeping the outside world. Waiting out a usage limit is fine.
MUST: everything finishes completely. A capability is done only when it has its tests, its exam scenarios passing (§4), docs ({{docs}} where each belongs), a page if it's user-facing, a status surface if it has state, and it works on every surface it belongs on.
MUST: unprompted actions stay local and reversible (a lane, a branch, a spike, a throwaway folder). Anything that leaves the machine needs its grant, so it goes into the final report.
{{musts}}
NEVER: clone or sweep outside repos to brainstorm or to benchmark. Outside material is only allowed in the targeted form in §5.
NEVER: grant yourself anything or lower your own guard. Protected files get a built, evaluated candidate in a worktree plus `fm ask ID core` at the end. Merging stays the user's call.
{{nevers}}
{{done}}
DONE-WHEN: every capability in §2 passes its held-out exam, the completeness sweep (§7) is clean, and two generations in a row find no remaining tier 3–4 gap. It ends with one report (§8).

STANDING INTENT: the user's, under this gen-0 and over every later steer.
{{intent}}

---

## 0. Set up

```
fm autonomy full
fm drive on
fm loop on
fm lane max 6
fm mandate set "Genesis: build {{project}} per research/genesis/gen-0.md §2" --until <YYYY-MM-DD>
```
Save this message verbatim with `fm genesis init` (never edited), capture it as the Genesis task, and record the setup with `fm decide`. `fm genesis step` names each next action from here on.

## 1. Gap map first (generation 1 only, no brainstorm yet)

For each capability in §2, map what {{project}} already has with file and command evidence, what's partial, and what's missing, using `sampo:fm-recon` per capability in parallel. Save it with `fm genesis put gap-map.md`. Every later idea is checked against it. Nothing is built that already exists.

## 2. The capability spec: what "complete and best-in-class" means for {{project}}

Each item has a test. A capability counts only when its exam scenarios pass (§4).

{{caps}}

## 3. The tower (each generation)

| Level | Written by | File |
|---|---|---|
| L0 Genesis | the user, or `fm genesis draft` | `gen-0.md` (fixed) |
| L1 Seed | gen 1: Genesis plus the gap map; later: the last generation's critiques | `gen-N/seed.md`: which gaps this generation closes, lenses, order |
| L2 Missions | the Seed, ≤ 8, one per gap | `gen-N/mission-<capability>.md`: a self-contained prompt a fresh `fm run` session can execute |
| L3 Briefs | each Mission | `fm intake` blocks, grounded, tiered, built |

1. The Seed picks the biggest open gaps from the gap map and the exam results.
2. Each Mission runs `fm mission` and `fm ideas --rounds 4 --deepen 3 --falsify --crossbreed --seen <earlier ideas.md>`, with lenses fitted to its capability, plus **inversion** (how this capability fails or gets abused), **sci-fi** (the version five years out, worked back to what can be built now), **self-modification** (how the work improves the tower itself) and at least two lenses it invents.
3. It grounds every idea against the code (nothing dropped silently) and tags it with an **ambition tier**: 1 tweak, 2 feature, 3 capability, 4 paradigm. At least 70% of what gets built is tier 3–4. Tier 1 goes to the inbox.
4. It builds (§6) until its capability's exam passes.
5. It writes `gen-N/critique-<capability>.md`: what in its Mission or the Seed was wrong or too timid, and a proposed rewrite of each.
6. **Selection:** the next Seed is written from the critiques. A rewrite stays only if its generation closed more exam scenarios than its parent did; otherwise the parent text comes back (`fm genesis select N`, train scenarios only). Lineage goes in `lineage.md`. From generation 2 on, `fm genesis seed N` writes the Seed. Every 3rd generation, the Seed and mission templates go through `fm evolve --runs 3`.
7. **Drift check:** an `fm second` read compares each new Seed to gen-0's goals, MUSTs and NEVERs, and undoes anything that drifted.
8. **Structure caps:** at most 4 levels, at most 8 Missions per generation, one generation at a time, each finished before the next starts.

## 4. The exam: local, cheap, held out

There are no outside repos. Build the exam in gen 1 as a permanent suite (`fm exam gen` in {{langs}}):

- **Generated scenarios:** throwaway repos made locally from scripts, with planted defects matching §2 (bugs, vulnerabilities, missing tests, slow paths, docs drift, a flaky test, a broken dep), plus vague goals and ambiguous requests.
- **The user's own history:** `fm bench build` cases from finished tasks, `fm backtest` on real sessions, and `fm bench court` on past corrections, so taste is measured too.
- **Held out:** a third of the scenarios are never shown to any Mission or used for selection. They're only used to grade a capability as done, so the tower can't overfit to the exam.
- **Scored:** what it found or did against the answer key, false alarms, fixes that pass with a failing-test-first proof, tokens per verified result. Results go to `research/genesis/exam.jsonl`.

## 5. Outside ideas: targeted only

When a Mission is stuck on a specific gap, it may do one `fm research ask` on how others solve exactly that, and search the already indexed sources (`fm lib search`). At most a couple of pages per gap, never a sweep.

## 6. Building rules

- Tier 4 or uncertain → `fm lane new ID --spike` first, then promote it or kill it with the reason. "Too ambitious" is never a reason on its own; a failed spike gets two different approaches before it goes to `not-yet.md` with its blocker.
- Two credible designs → `fm lane race`, then `fm lane pick`.
- A normal brief: red-first test, M/L approaches listed first, `fm task finish` with its audits.
- Instruction files change only through `fm evolve`, kept only past `fm bench gate`.
- **Subtract as you add:** each generation removes, merges or slims at least as much as it adds. Hook p95 (`fm doctor`), rules size and injected context may not grow.
- **Retry limit:** a blocked task gets two new approaches. If both fail, record the diagnosis with `fm decide` and move on.

## 7. Completeness sweep (the end of every generation)

Run `fm state`, `fm queue`, `fm lane list`, `fm docs`, `fm doctor`, the full gates (`{{gates}}`), the exam re-run and `fm genesis check`. Every task is closed, or blocked with a diagnosis after its two retries. Every spike has a verdict. Every finished lane is merged (or queued for the user if it's protected). File it as `gen-N/sweep.md`.

## 8. Final report (one, at the end)

- The capability scorecard: each §2 item with its held-out exam score per generation, what was built for it, and a short real example of it working
- The catalogue: every idea with its capability, tier, outcome and reason
- The prompt lineage, and which rewrites won
- What was removed to stay lean, with the hook latency and context numbers before and after
- What's waiting on the user: protected candidates with their eval deltas, installs and timers, outward actions, merges, `fm decide --review` items
- The last completeness sweep's output, and `fm cost` for the record
