---
name: reflect
description: Use whenever a task, phase or block ends, or for a retro: records decisions, lessons and ideas.
---

# Sampo: reflect

Short retro on the last task or phase. Evidence over opinion.
1. What happened: the brief's Log, evidence, guard blocks and stop-gate events (`sampo watch --once`, the ledger via `sampo resume --no-check`); across tasks, `sampo friction` (the self-improvement digest since the last pass).
2. Ask: what caused back-and-forth? What did the user correct (`sampo recall --corrections`; a repeated one is a durable preference: save it)? What did the Ward block that the user then allowed? What took more than one verification attempt? What would a fresh session have needed to know?
3. Record, each in its own home:
   - Decisions (what, why, rejected alternatives) → `sampo decide "<decision>" --why "…" --rejected "…" --task ID`.
   - Durable, project-specific learnings and gotchas → auto memory for this project (one line in MEMORY.md, detail in a topic file). No task status there.
   - Stable conventions the project should follow → propose a line for the project CLAUDE.md (≤ 150 lines; apply per autonomy).
   - Ideas to improve Sampo itself → `sampo capture --self --source self --type <T> "<idea> — evidence: <what happened>"`. Never edit Sampo directly from here; `/sampo:improve` handles those.
4. Report in ≤ 8 lines.
