---
name: intake
description: Use whenever a new request arrives (FIX:/FEATURE: lines or plain words): makes tasks.
---

# Sampo intake

Run this for every new request, tagged or plain. A typo gets a three-line brief, never zero thought. State changes go through `sampo`.

## 1. Capture
- Tagged block → `sampo intake <<'EOF'` … `EOF` (stdin). It creates one **captured** brief per item, attaches the block's CONTEXT/CONSTRAINT/DONE-WHEN/SKIP lines, and prints the canonical order. Language details: `references/language.md`.
- Plain request → classify it (type + tier) and state that in one line, then `sampo capture "<verbatim request>" --type T --tier X`. A small, obvious S task can be briefed and started in one command: `sampo task new "<title>" --type T --tier S --ac "<done when>" --step "<step>" --focus`.
- The urgent FIX path (`FIX!:`): when the request reports live harm (an outage, wrong data being written, errors users see now), don't capture it as text: find a command that is red while the harm lasts and run `sampo capture --incident '<impact>' --signal CMD`. It refuses a signal that passes, opens the urgent FIX with that red run as its first evidence and a UTC timeline, and closes only on the same signal green twice plus one follow-up (the ops/incident playbook, `/sampo:playbooks`).
- While another task is active, a new request is only captured (focus lock in the rules): reply with its id and continue the active task.

## 2. Expand, ground, anticipate, compile (per item, in canonical order)
Promote each captured item: `sampo task new "<imperative title>" --type T --tier X --from T-NNNN --scope <glob>...`.
Then fill the brief with `sampo task set ID --section "<Name>" --text "…"` and `sampo task ac` / `sampo task step`:
1. **R1 Expand:** Interpretation, Assumptions (with confidence), testable acceptance criteria (`sampo task ac ID add "<criterion>" --verify "<cmd>"`), scope paths, Non-goals. A wish about a Python module's behaviour can start its red test from `sampo spec "<wish>" --module <module>`: a tool-less child's example assertions, only safe ones kept, as a unittest that should fail today.
2. **R2 Ground:** start from what Sampo already knows: `sampo map` (gates, layout, entry points, hot files, test links), `sampo impact <path>` (likely tests, dependents) and `sampo recall "<request>"` (related past briefs, decisions, research, lessons), `sampo why FILE:LINE` (the task and lesson behind existing code), `sampo outline PATH` for big files, `sampo lib` (stack cards for this repo: exact Verify commands for the criteria, the failing-test recipe, pitfalls; `sampo lib search WORDS` also finds installed plugins' skills). Then read the real code, run the real commands, correct R1 against reality. For M/L, weigh every approach worth weighing (three or more, one unconventional) in "Approach (options → choice → why)", and for a FEATURE add a capability sweep (`references/planning.md`). Library/API questions: context7. Large unfamiliar areas: `sampo:fm-recon` (`references/delegate.md`).
3. **R3 Anticipate:** pre-mortem — "if I ship this, what are the three most likely follow-ups or complaints?" Implied requirements (error handling, edge cases, tests, docs/config, migration notes) go in scope. Adjacent ideas are captured (`sampo capture --source followup`), not built.
4. **R4 Compile:** write the **Execution prompt** section: a block a fresh session with zero history could execute (goal, context, files, steps, acceptance checks, verification commands, constraints). Add steps with `sampo task step ID add "…"`.
Tier depth, the brief layout and the rubric: `references/planning.md`.

## 3. Self-critique (M/L, and before any approval gate)
Every criterion has a verification command? Could a fresh session run the Execution prompt without asking? Anything in scope the user didn't ask for and wouldn't expect → INBOX. Anything obviously expected missing → add. Simplest adequate approach? Rollback clear? Revise at most twice.

## 4. Autonomy gate
Check the level with `sampo autonomy`.
- **Standard** — L or a bigger tier (XL, HUGE, GIANT), `?` items, and anything destructive or irreversible (data deletion, force-push, migrations on real data, major dependency bumps, public API changes): present a ≤ 15-line plan summary plus batched questions, each with your default, then stop. After approval: `sampo task set ID approved=true` (a FEATURE L first gets its "Build vs reuse" section: what it builds on, or why nothing fits).
- **Full** — ask nothing. After the self-critique, record the plan choice with `sampo decide`, `sampo task set ID approved=true` with a log line "self-approved (full autonomy)", and continue. What only the user can grant (`core`, destructive guard categories, merging Sampo changes): `sampo ask`, then carry on with other work and list it in the final report.
- S/M: proceed after self-review.

## 5. Baseline (once per queue)
Confirm the build and tests run; record baseline metrics for PERF items; note git state. `sampo log baseline '{"tests":"<result>","commit":"<sha>"}'`. A FIX that breaks the baseline is hoisted first.

## 6. Execute
`sampo focus ID` (it refuses a brief that isn't planned for its tier and lists what's missing; `sampo next` always names the one next required action and its procedure), then follow `references/execute.md` — one step at a time, fresh evidence per step, checkpoints, commits, the 3-attempt loop cap and end-of-task triage. Stage procedures: `/sampo:playbooks`. FIX work: `references/debugging.md` then `references/regression-test.md`. Before any completion claim: `references/verification.md`; before `sampo task done`: the audits in `references/audit.md`.
