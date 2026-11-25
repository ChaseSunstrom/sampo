---
name: intake
description: Use whenever a new request arrives (FIX:/FEATURE: lines or plain words): makes tasks.
---

# Sampo intake

Every request becomes a brief before any code changes. The full steps, with every command and rule, are in `references/procedure.md`. Read it before any step, and read it again after a compaction: this stub keeps only the step headings.

1. Capture: a tagged block goes to `sampo intake` on stdin; plain words go to `sampo capture`. Language details: `references/language.md`.
2. Expand, ground, anticipate, compile (per item, in canonical order): `sampo map`, `sampo impact`, `sampo recall`, `sampo why`, `sampo lib`; for M and L, weigh at least three approaches. Planning tiers: `references/planning.md`.
3. Self-critique (M/L, and before any approval gate): the lenses in `references/audit.md`.
4. Autonomy gate: approval only when the autonomy setting asks for it.
5. Baseline (once per queue): record the red and green commands before the first change.
6. Execute: `sampo focus ID`, then `references/execute.md`, one step at a time, with fresh evidence per step.

The urgent FIX path (`FIX!:`): when the request reports live harm (an outage, wrong data being written, errors users see now), don't capture it as text. Find a command that is red while the harm lasts and run `sampo capture --incident '<impact>' --signal CMD`, which opens the urgent FIX with that red run as its first evidence. Procedure: `references/debugging.md`, `references/verification.md`, and the ops/incident playbook.
