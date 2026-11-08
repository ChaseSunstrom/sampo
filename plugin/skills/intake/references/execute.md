# Execute loop (reference)

## Per step
0. M/L FEATURE or FIX, before reading the code for the tests: `sampo oracle ID` (T-0226) writes behaviour examples and ambiguities from the request alone into the brief's Oracle section. Write the tests from those examples, so they check what was asked rather than mirror what you build; settle each ambiguity with `sampo decide` first.
1. `sampo resume` if you are not sure where you are. Work only on the CURRENT step.
2. Do the work. Stay in scope; out-of-scope edits → widen scope (`sampo task set ID scope=…`) or log why (`sampo task log ID "scope: <why>"`; `sampo task done` asks for one), or `sampo capture`. Big file: `sampo outline PATH`, then Read only the range you need.
3. Verify with a fresh command (tests, build, lint, a reproduction). Read the whole output. When the LSP tool is available, check its diagnostics for each file you edited first: a type error is cheaper to fix before the tests run.
4. Record: `sampo task step ID done N --evidence "<cmd>" "<result>"` (or `sampo task evidence` first). sampo refuses without evidence.
5. Acceptance criteria: `sampo task ac ID check N --evidence "<cmd>" "<result>"`.

## Batches (T-0257)
Small related requests (the same type, nearby files) are faster as one: `sampo batch ID ID …` makes one host task with every member's criteria and one step each. Plan its verify commands, work the steps, run `sampo check` once for the whole batch, one review pass, one commit; the members close done in the host. `sampo next` suggests it when three or more small items of one type lead the inbox.

## Checkpoints and git
- First focus prints a preflight: scope paths that don't exist, uncommitted files outside the scope, and whether the gates already ran green on this tree (T-0633). The same failing command a third time on a step gets a nudge and is logged as tried (T-0632). `sampo audit prep` maps each changed file to the steps that name it (T-0610). At the close, debug scaffolding in added lines warns (T-0650).
- Stuck: from 2 failed runs on a step, `sampo next` climbs a ladder and names the stall. Environment means fix the setup first. The same error again means the fix isn't reaching the cause, so re-read the spec. Changing errors mean progress, so take the smallest next step (T-0593). Keep the case file in the brief with `sampo task note ID fact|question "…"`; checkpoint and resume print it (T-0630).
- `sampo checkpoint --note "<exact resume point>"` before switching tasks, before risky or destructive-but-authorized steps, and at natural pauses. PreCompact does an automatic checkpoint; a note is better.
- Commit in small conventional commits on the task branch. Before a destructive-but-authorized step, commit or stash (`git stash push -m foreman/T-NNNN`) so git and rewind both cover it.

## Loop cap
3 failed verification attempts on the same step → stop. Write a diagnosis in the brief (what was tried, what was observed, hypotheses) with `sampo task set ID --section "Resume here" --text …`, `sampo task block ID "<reason>"`, move to the next unblocked task, and report.

## Preemption and pause
- `NOW:` / `TAG!:` / emergency → `sampo checkpoint --note …`, `sampo capture` or intake the new item, `sampo focus NEW`, and offer to resume the old one afterwards.
- PAUSE → `sampo checkpoint --note …` and wait (drive pauses automatically).

## End of task
1. FINAL checks for this task: full test suite / build, not just the new test. Record evidence.
2. Audit (`audit.md`): S = self checklist; M = intent + the riskiest other lens; L = all five lenses via `sampo:fm-reviewer`. Verify every finding, fix or capture it, `sampo task audit ID <lens> "<how>" "<result>"`.
3. `sampo task done ID` (sampo lists anything missing, including audits older than the last edit).
4. `/sampo:reflect` for M/L tasks or anything that caused back-and-forth. Anything you did by hand again (`sampo repeats` lists what recurs across tasks) → capture an S task to make it a project tool (below).
5. Triage the inbox: expand captured items into briefs (intake §2), fold them into the queue.
6. Re-plan checkpoint after each phase group or structural change: re-read remaining briefs, update scope, approach and Execution prompts, re-order if dependencies changed (`sampo queue --replan`), log what changed in each brief.
7. Show the queue in ≤ 10 lines and continue per autonomy (`/sampo:next`).

## Project tools (for work that repeats)
`sampo repeats` reports commands run 3+ times in 2+ tasks and steps that recur in 3+ tasks, each with a suggestion. Make the smallest tool that removes the repetition, in the project repo, as its own S task, then `sampo repeats dismiss "<shape or step>"` (also for one that isn't worth a tool). A new skill, agent or command file is always-on context, so creating one needs the user's yes (`sampo ask ID plugin`; editing an existing one doesn't):
- A gate (tests, lint, build, a benchmark with a budget) → `sampo check add "<cmd>"`; `sampo check` then runs every gate at once.
- A multi-command sequence → a script in the repo (`scripts/<name>`), then use it in evidence (`--run`) or as a gate.
- A procedure with judgement (how this repo does releases, migrations, fixture updates) → a project skill, `.claude/skills/<name>/SKILL.md`: frontmatter `name` and a one-line `description` saying when to use it (it is always-on context in this repo, so keep it short), then the steps, commands and pitfalls. Sessions and subagents in the repo load it when it applies.
- The same delegation brief sent again and again → a project agent, `.claude/agents/<name>.md`, with only the tools it needs.

## Evidence quality
Evidence is a command plus its actual result. "Tests pass" needs the test command's output with 0 failures; "bug fixed" needs the original reproduction passing; "regression test works" needs the red-green cycle: `sampo task prove ID --run "<test cmd>"` runs it on the start tree with only your test files (must fail) and on the current tree (must pass), recording both. `sampo task prove ID --hunks --run "<check>"` goes further: it removes each code hunk of your change alone and runs the check — a hunk it doesn't notice is untested (recorded inconclusive), so write the test that catches it or say why not. PERFORMANCE tasks record numbers (`sampo task finish ID --numbers "<metric>: <before> → <after>"`, or `--section "Measurements"`); CLEAN tasks run the tests before their first edit (behaviour lock). `sampo task done` prints a verification grade (strong: every check ran through fm, plus red→green or an independent lens). `sampo sentinel` re-runs the checks recent finished tasks passed (FINAL VERIFY). See `verification.md`.
- A check that couldn't have failed (no test selected, a grep that matches either way, output you can't read) is inconclusive: record it with `--inconclusive` (it never counts) and write a sharper check.
- A fact the plan rests on goes in as `sampo task assume ID add "<fact>"`; check it before building on it (`sampo task assume ID verify N --run CMD`, or `--evidence "<file:line read>"`). A false one changes the plan.
- When the code wasn't what you expected, `sampo surprise "<expected> → <observed>"`: friction and recall bring it back to the next related task.

## Forks and precedent
- Doors (T-2342): `sampo focus` classifies each step and verify command with the Ward's parser and git (`fmdoors.py`); a Ward category stays the Ward's stop, an untracked-data step gets an Undo line or parks, every other fork is two-way: decide it, `sampo decide … --confidence N`, never ask.
- When sources disagree, the order is: the user's words > vetoes > decisions > lessons > your defaults (T-0654). Say which one a decision rests on: `sampo decide "<it>" --cites "<the user's message, veto or decision>"`; `sampo recall` finds the candidates.
