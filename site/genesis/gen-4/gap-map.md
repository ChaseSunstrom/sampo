# Genesis gap map — generation 4 (2026-11-04)

Drawn at: 192dab33

Redrawn against the code at 192dab33 by three read-only recon passes (capabilities 1–4, 5–9, 10–13), each reading gen 3's map, gen 4's seed, Missions, critiques, review (fable: 23 findings, filed as T-3057 to T-3079), select and lineage, and the code and tests the Missions' tasks changed. Every later idea is checked against this map: an idea that names something under **Built** is a duplicate unless it extends it. Tiers: 3 capability, 4 paradigm.

Source limit: the held-out exam on the production model still hasn't run (the user's testing-cadence steer keeps it for the end of the run), so no capability below counts as passing. Gen 4 was kept on one Haiku smoke run (s32-015: 3 closed against gen 3's 2), which has flapped 3, 2, 3 (review #5). Signals Sampo didn't author: that run, the long tier (foreman 0/6 true done, 6 false dones), the gen-4 review, and the user's steers since gen 3 (moons kept at 10,000, Kalevala, the modern live page).

## The map (one row per capability; tier of its largest open gap)

| Area | Tier | Evidence | Missing |
|---|---|---|---|
| 1. Initiative | 3 | No gen-4 Mission; find ladder, leads and exam kinds (`none`, `clean`, `rank`) unchanged from earlier generations | Held-out initiative score; s21-013 still has no foreman arm; 56 empty coverage cells (seed.md:109, not measured) |
| 2. Exhaustive thinking | 3 | `sampo ideas --model local` routes lens, deepen, falsify and crossbreed children through moonscale, Haiku fallback (T-2910) | Head-to-head never ran (T-2921 captured, no criteria); procedure still picks sonnet; moons kept-rate premise flipped |
| 3. Anticipation | 3 | Local 27B arm `pred_local` booked, scored, replayed, night-wired (T-2912, T-2950); shown rows scored by arm | Local arm 0 scored; replay note not filed (T-2924); predictor ties naive (review #13, T-3069); draft-ahead 0 of 1 |
| 4. Goal to finish | 4 | `goal plan` reads the cited document: 13 capability items plus DONE-WHEN clauses, deduped, per-clause checks (T-2913) | Live verdict still "no items" (`--apply` never run); foreman false done 6 of 6 (review #1, #20; T-3057, T-3076) |
| 5. Getting the tools it lacks | 3 | `playbooks eval --real` and `small variant` on a local profile, kept only on lift; Smith's local prompt carries the variant (T-2917) | No lift measured: T-2949 (circular keep rule) then T-2932 open; 400 calls, 0 kept (review #18, T-3074) |
| 6. Verifying anything | 4 | The plan gate names user clauses with no criterion; the rollup splits meaning into user and notes, 244 + 355 = 599 (T-2914) | The after-share is not measured (53 of 302 before); no meaning row closed by proof; provenance lines in Raw request |
| 7. Self-repair and self-improvement | 3 | No gen-4 Mission; the review filed repair tasks (T-3057 to T-3079); T-2988 warns on state paths in verify commands | Failed tool calls 0.6 → 1.3 per 100 (review #2, T-3058); the top guard rule blocked builders 18 times (review #21, T-3077) |
| 8. Calibrated judgment | 3 | No gen-4 Mission; the model bench shows jeff under baseline (mission 5) and d3:4b at 0.73 vs 0.595 on intake type | The headline rates are read on under 10% of closes (review #11); the 0.77 and 0.12 baselines are wrong (#12, #15) |
| 9. Orchestration | 3 | Builder lanes ran the tonight briefs; exam run lock (T-3029); keys from a private secrets file (T-3036); machine-wide live page (T-3022) | Isolation refuses builders' commands (review #2, T-3058); the live steering has a stated hole and no exam line (review #7, T-3063) |
| 10. Beyond code | 3 | No gen-4 Mission. The only adjacent work is `exam gen --from-repo`, which mints code-bug scenarios (T-2978, T-2987), and a docs-claim narrowing carried from gen 3. | A held-out score for docs, infra and data work, and the 421 docs claims still unproven (seed.md:86). The seed ranks this cell 56 and below the coverage Missions (seed.md:109). |
| 11. Self-triggering | 3 | Night adds `moons grow` and `moons funnel` at $0, local-only, behind a gate (T-2916, T-2953). Also a $0 next-request job (T-2912) and a nightly timer plus an hourly tick. | No real night has run the new jobs. The gate shuts `grow` at 10,007 rows; T-3042 would raise the target. The `night` trigger has fired once, and paid court takes $1.53 of the $2.00. |
| 12. Staying lean and safe | 3 | MASTER.md is an index (82,232 bytes) plus 7 `docs/map` shards, one reader (`core.master_text`) and size tests (T-2915). Repeat-failure firewall (T-2992). Hook SLO judged at normal load (T-2798). | Live hook p95 is 315 ms against a gate that reads ~36 ms (review #14). SessionStart injected 142k characters over 95 sessions (#22). The guard blocked its builders 18 times (#21). |
| 13. Genesis as a feature | 3 | Gen 4 kept on s32-015: 3 closed against gen 3's 2 (lineage.md:23; select.md). Moons at scale: 10,007 candidates, 292 kept. `moons regen` (T-2918), three ecology moons M-59 to M-61, a lineage line per generation. | The grade is still provisional and rests on a Haiku smoke run (lineage.md:24), and s32-015 flaps (review #5). The M-02 slice never ran (T-2954). Kalevala (T-3050; part 1 T-3053) is not built. |

## Cross-cutting findings
- **The measure waits on the clock, and the generation closes first.** Self-triggering, anticipation and lean all have first readings that are due later (2026-11-11 for Read failures). No night has run the new jobs. Their critiques say planned, not moved.
- **Hard criteria are split out and then drift.** T-2918's run became T-2954. The head-to-head became T-2921 and the replay became T-2924. All three are still captured with no result, so the claim goes unproven (moonshot and anticipation critiques).
- **A busy GPU is the shared blocker, and nothing arbitrates it.** The slice, the head-to-head and the replay each moved out because the endpoint was busy with moons growth. The new $0 night jobs add load. A GPU lock held ahead of other local jobs was proposed but is not built.
- **A gate sized to a moving number.** The night's `grow` gate used 10,000. T-2865 took the store to 10,007 by hand and switched the feature off.
- **Lean was measured by size, not by cost.** MASTER.md shrank but the total grew. Live p95 is 315 ms against a gate reading ~36 ms (review #14). SessionStart injected 142k characters, and the guard blocked its own builders (review #21, #22).
- **Provisional trust.** Kept on one Haiku smoke run that flaps (review #5). The false-done problem (review #1, #20) is untouched, and every Mission here is measured on that same unproven base.
- **Machinery over results.** Gen 4 landed 136k lines (review #6) while the held-out exam still has not run on the production model.

## 1. Initiative
- Built: no gen-4 Mission and no gen-4 task on `find` appears in the CHANGELOG entries I read. The earlier machinery stands: `/foreman:initiative` runs map, find, prove, fix and receipt (CHANGELOG.md:476). The `find` MCP tool is built (CHANGELOG.md:517). The exam kinds `none`, `clean` and `rank` exist (CHANGELOG.md:520).
- Built: gen 4 changed Initiative only indirectly, through the goal plan's cited-document reader (T-2913), which can now list the initiative item.
- Measured: no gen-4 exam line on Initiative that I read. The seed carries "the s21-013 run still carries no foreman arm at all" (seed.md:72). gen-4/review.md has no Initiative finding.
- Missing: a foreman arm on s21-013. It would show closed when an exam line for it appears in `sampo exam scorecard`.
- Missing: recall reported per language and per bug type, with child-authored scenarios (seed.md:51). It would show closed when the scorecard prints that split.
- Missing: Initiative has 56 open coverage cells (seed.md:109). It would show closed when `genesis check` counts them as filled or n/a.
- Missing: nothing was measured on the production model. The held-out exam is deferred to the end of the run.

## 2. Exhaustive thinking
- Built: `ideas.py:779-825` sends every child through `_local_round` when `--model local`. The routing is `moonscale._local` and `_ask`, reused rather than copied (critique-exhaustive-thinking.md:6). The model that answered each child is booked in `manifest.json` (ideas.py:976). `cli.py:8139` documents the flag. test_ideas passes 30 tests (critique).
- Measured: nothing on width. The critique reads the post-merge `ideas` run (14:18Z, 235 ideas) as `"model": "sonnet"`. The moons kept-rate moved from "a large local model+nothink 2753 of 3316" to "157 of 8444", so the Mission's premise no longer holds.
- Missing: the head-to-head (local vs sonnet, 40 blind-judged ideas per arm). It sits in T-2921, which is captured with no criteria and has produced no research note. It would show closed when the judged-good count is filed against the 80% and 50% thresholds.
- Missing: use in practice. The brainstorm skill has no step that tries `--model local`, so the flag goes unused (critique-exhaustive-thinking.md:27). It would show closed when a new `ideas` manifest names the local profile on its children.
- Missing: the live one-round run the Mission demanded before close. It is not in the task evidence.
- Missing: no review finding names #2 (not measured).

## 3. Anticipation
- Built: `foresight.local_tick` (foresight.py:419) books a write-once `kind: local` row. The kind `next-request-local` is scored beside the count and the naive baseline (foresight.py:361). `leading` picks the arm for the session-start line and draft-ahead (foresight.py:246, draft.py:158). `replay_forecast(local=True)` adds the local arm (louhi.py:813, :4172). The night job calls it (night.py:74). T-2950 caps the replay at 30 asks and scores shown rows by arm (louhi.py:80, foresight.py:511).
- Measured: `outcomes --predictions` reads "next-request-local: 0 scored". The existing arm reads 81 scored, 41% vs 31%, shown 39 scored at 38% (critique). The replay tie is 259/376 vs naive 256, paths 0.088 vs 0.086 (review #13).
- Missing: one scored local row, then the held-out replay note (T-2924, captured; no research note exists). It would show closed when the note gives the type-hit gap in points against the 10-point rule.
- Missing: the shown rows' baseline is not printed, so the 38% can't be judged.
- Missing: draft-ahead is at 0 of 1 adopted (the kill test wants 3).
- Missing: the `genesis-anticipation` trigger has 23 fired, 0 done, precision 0.00.
- Missing: a lead over noise, not three rows (T-3069).

## 4. Goal to finish
- Built: `mandate.doc_items` (mandate.py:517) cuts one item per numbered entry and per DONE-WHEN clause from the document the goal cites. `_cited` (mandate.py:469) realpath-checks the path inside the repo or project root. Items are deduped by text and target (mandate.py:450). `plan` calls it (mandate.py:587). A clause carries its own check on apply (mandate.py:626). test_goal passes 44 tests (critique).
- Measured: live `sampo goal plan` now lists the 13 capabilities and 3 DONE-WHEN clauses, with no repeated "web UI" lines. Live `sampo goal verdict` still reads "stopped: the goal has no items". Long tier: foreman 0/6 true done, 6 false dones, plain 1/6 (review #1).
- Missing: `goal plan --apply` run on the live mandate, with the verdict recorded before and after. It would show closed when the verdict names 13 unmet items.
- Missing: a check linking each capability item to its exam row or task.
- Missing: the planner's proposals still name `fm` and verbs that don't exist.
- Missing: a replay where the done claim is refused while a spec entry is unmet. None exists. It would show closed when the "planned but not built" class (9 of 31 failed checks) falls.
- Missing: false done is the first user complaint (review #20; T-3057, T-3076).

## 5. Getting the tools it lacks
- Built: `playbooks eval NAME --real --model PROFILE` answers through an own or loopback profile, never `claude -p` (T-2917; `playbooks.py:1377`, `real_model`). `smallcards.variant` (`smallcards.py:566`) drafts held-out briefs from the original, writes a variant, and keeps it only if its lift is at least the original's and above zero (`smallcards.py:596`).
- Built: `playbooks show --model` serves a kept variant (`playbooks.py:1240`). The coverage line counts variants (`playbooks.py:1278`). Smith's local prompt appends the variant (`smith.py:1009`).
- Built: the exam run lock and the keyed models (T-3029, T-3036) let the 27B writer be reached outside a login shell. I did not read any run that used them.
- Missing: the keep rule is circular as merged. `_variant_prompt` (`smallcards.py:551-557`) still passes the writer the eval briefs' "has" phrases. T-2949 is captured. It closes when the writer's prompt carries no check phrases and a test holds that.
- Missing: no lift table. Per the critique, `playbook-evals.jsonl` has 2 rows, both on the scripted "literal" model. T-2932 is captured and depends on T-2949. It closes when research/small-playbooks lists lifts for 10 or more playbooks on two profiles. Review #18 (T-3074) reads 400 calls, 0 kept.
- Missing: the find and install half of gen-0 #5 has no brief. Friction-driven aliases for unknown verbs were a lead; the critique recorded no alias or capture made, and I did not check further.
- Missing: the failure taxonomy (40% out of steps, 20% plan refused) is not what a brief-following variant addresses. Not measured past n=15.

## 6. Verifying anything
- Built: `plan_gaps` (`core.py:3180-3186`) lists clauses of a user's M or L request that no criterion checks, each with the `sampo task ac add` line that closes it. It uses `recall.unchecked_clauses` (`recall.py:723`). A Non-goals echo counts as accounted for (T-2914).
- Built: the rollup's meaning kind splits into meaning-user and meaning-notes, summing to the old count. The task log reads 244 + 355 = 599, and `test_proof.py:477` holds the sum.
- Missing: the after-share is not measured. The log says "user M/L share not recomputed"; the before is 53 of 302. It is due 2026-11-11 and 2026-12-04. It closes when a dated command prints the share for closes after T-2914.
- Missing: nothing was proven. The 244 user rows stay until each gets a criterion that can fail or a Non-goals line. The false-bind rate of the two-shared-words rule is not measured.
- Missing: 355 of 599 meaning rows (59%) are Sampo's provenance lines such as "(noticed by an agent;". They should not enter Raw request. The critique assigns this to #7's capture format; no task id is filed that I could confirm.
- Missing: three-valued verdicts and model-drafted checks are held for T-2878. Intent proof beyond a label is still tier 4.

## 7. Self-repair and self-improvement
- Built: no gen-4 Mission. T-2988 warns on a verify that names repo-relative `state/` (CHANGELOG:38). The review filed fix tasks T-3057 to T-3079, all captured (T-3074 and T-3058 read as captured).
- Missing: failed tool calls per 100 doubled, 0.6 → 1.3, mostly worktree isolation refusing builders (review #2, T-3058). It closes when that rate falls in the next window.
- Missing: the top-firing guard rule (`interpreter-code-driving-foreman`, 11 hits) blocked builders 18 times, and T-2261 sat open all generation (review #21, T-3077).
- Missing: a defect escaped the repeat-failure firewall's own close (review #4, T-3060). Recur and correction rates have no held-out score (not measured).

## 8. Calibrated judgment
- Built: no gen-4 Mission. It reuses gen 3's windowed reads.
- Missing: outcomes quote "49 of 50 held" while 1769 of 1960 closes are inside the window, so the record is read on under 10% of closes (review #11, T-3067).
- Missing: the 0.77 retrieval figure drops abstentions from the numerator but not from the baseline (review #12, T-3068).
- Missing: the next-request predictor ties naive (259 vs 256) yet ships as a "lead" (review #13, T-3069).
- Missing: the Jeff tier baseline of 0.12 is impossible (review #15, T-3071). The close-holds verifier ships on AUC 0.55 (review #17, T-3073).

## 9. Orchestration
- Built: builder lanes ran the gen-4 briefs. Fixes landed for shared leases and the long-subagent wait (CHANGELOG:66-69, T-2870, T-2872 to T-2874). A lane re-runs its own plugin (T-2843).
- Built: `sampo exam run` takes a lock per run and agent (T-3029; `louhi.py:3872`, test at `test_exam.py:1017`). It closes the mixed-answers hazard.
- Built: `models.py:873` reads the key from `~/.config/sampo/secrets.env` (T-3036). The gen-3 gap "the model layer waits on the user's key" is closed in code. It is used live only if the file exists, which I did not check.
- Built: the live page manages every session on the machine (T-3022 to T-3027). It steers by POST (T-3026), 1,704 lines in `plugin/web/live/live.js` (review #7).
- Missing: worktree isolation still refuses builders' commands (review #2, T-3058). Evidence and lease failures inside worktrees from gen 3 (review #23) were not re-measured.
- Missing: the steering wall has a stated hole (port forwards read as your uid) and no exam line (review #7, T-3063).

## 10. Beyond code
- Built: nothing new this generation. `/gen-4/seed.md:109` lists this cell among those that wait. The nearest gen-4 work is exam scenarios minted from real repos, one planted bug each (CHANGELOG.md:43, T-2978, T-2987). That covers code only.
- Built: the gen-2 incident ritual and the docs, infra and data proof kinds stand. Gen 3 narrowed docs proof to 421 claims (T-2616, review #19). Gen 4 did not recount it, and I found no gen-4 task that proves docs claims. That is an inference from the CHANGELOG, not a measured line.
- Missing: any held-out reading for docs, infra, data or package work. It would be closed by an exam kind (or a `fm prove --docs` run) with a score on the production model. None exists, and `fm exam scorecard` stays at 0/13 until the end-of-run exam.
- Missing: the 421 unproven `--flag` claims (seed.md:86), closed when a prove run checks them and the count falls by proof, not by narrowing.
- Missing: infra and data work have no scenario at all. Louhi's minted scenarios plant code bugs only (CHANGELOG.md:43). Closed by one non-code scenario family on the train split.

## 11. Self-triggering
- Built: `night.jobs` lists `moons grow` (250 calls) and `moons funnel` (400 calls) at $0 with `--local-only` (night.py:67-73). `moonscale.night_ready` gates them on a non-sensitive project and an answering local model (moonscale.py:461-472). A call no local model answers raises `LocalOnly` instead of reaching Haiku (moonscale.py:499-500).
- Built: a $0 next-request job (night.py:74, T-2912) and the funnel's parallel kill stage (T-2953). Timers fire nightly at 02:30 and hourly.
- Measured: none of it ran. The critique's 2026-11-04 dry run planned 11 jobs, $1.98 of $2.00, with neither Moons job listed. The store sat at 10,007, so the gate shut `grow`. That is the critique's reading of the gate, not a run.
- Missing: one real night's ledger event showing which jobs ran and why each stopped. T-3042 (10,000 kept) is the gate fix, and `grow` should gate on rows waiting.
- Missing: a second kind of $0 night work (T-2886 bench rerun, a graded morning card) and `sampo triggers` showing `night` above one firing.
- Missing: the paid half. Court takes $1.53 of $2.00 and the queue is skipped every night.

## 12. Staying lean and safe
- Built: MASTER.md went from 462,922 bytes to 82,232, with no line over 2,000 characters and shards of at most 76,047 bytes. Tests hold both limits, and a word-multiset test holds that no text was lost (T-2915).
- Built: `sampo check --affected` always runs the standing-rule tests, and the self-change gate benches interleaved pairs (T-2992, CHANGELOG.md:44).
- Missing: the Read outcome. Failures were 7 against 1 success before the change and are not re-counted (due 2026-11-11). The shards total 518,072 bytes against 462,922 before, so total weight rose. No real Read has been run on the file.
- Missing: the injected-context trim. SessionStart injected 142,399 characters over 95 sessions (review #22), and `sampo burden` reads 77 against 40 characters a turn. Nothing in the diff trims either. T-3078 files the SessionStart trim.
- Missing: an honest latency read. p95 is 315 ms live (review #14), and the gate's ~36 ms excludes loaded runs.
- Missing: guard friction. The top rule fired 11 times and blocked builders 18 times (review #21). T-2261 sat open all generation.
- Missing: the 2.9 GB of `exam/` state, left untouched on purpose.

## 13. Genesis as a feature
- Built: gen 4 closed 3 train scenarios against gen 3's 2, with none reopened, and select.md says keep. Lineage marks it provisional, resting on s32-015 only.
- Built: Moons at scale (CHANGELOG.md:9). The candidate store holds 10,007 rows, 292 kept in 280 families. The local 27B's keep rate is 83% against Haiku's 36%.
- Built: `sampo moons regen PATH` (T-2918) and three ecology moons, M-59 (xhigh), M-60 and M-61 (massive), each with a kill line. The catalogue went from 58 to 61 of 100.
- Missing: the M-02 slice result. `m02-slice-result.json` reads `"ran": false`, so there is no Advanced line, 0 of 20 modules were run, and 1 of 20 extreme or massive moons is kill-tested. T-2954 closes it, with the GPU lock held first.
- Missing: a production-model held-out grade. The same Haiku smoke run has now flapped 3, 2, 3 (review #5, T-3061).
- Missing: 10,000 kept (T-3042) and Kalevala (T-3050), many towers overseen at once. Part 1 (T-3053) is in a lane. Neither was built at HEAD. I could not find these task ids in the files, so they come from the brief.
- Missing: a wild score for M-59 to M-61. The critique could not quote one.
