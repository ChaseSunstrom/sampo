---
name: brainstorm
description: Use whenever a request is open-ended ("super improve it", "what next?"): scored ideas become tasks.
---

# Sampo: brainstorm

Open-ended requests get ideas from several independent angles before any planning, so the work isn't limited to whatever comes to mind first. State changes go through `sampo`.

The full steps, with every command and rule, are in `references/procedure.md`. Read it before any step, and read it again after a compaction: this stub keeps only the step headings.

1. Classify and capture: say you are brainstorming first; `sampo capture` as RESEARCH, M tier, then `sampo focus`; a standing request also sets a mandate.
2. Context pack: `sampo mission --request` writes it under `research/`; add what it cannot know (at most 1,200 words).
3. Fan out: `sampo ideas --pack` runs one sub-agent per lens, in parallel; choose the lenses, the grid lens for exhaustive requests, `--deepen` and super mode as the procedure says.
4. Merge, cluster, ground: dedupe, cluster into capabilities, ground every cluster against MASTER.md then the code, with a canary before any "new".
5. Score each grounded cluster (value, effort, risk, confidence) and mark it build or skip; the slate is the build marks.
6. Record: `sampo research add brainstorm-<date>` with the whole catalogue; the slate becomes one `sampo intake` block, the rest go to the inbox.
7. Show the catalogue, then go: one yes/no for the slate when standard; `sampo decide` and the queue when full autonomy.
8. Close the brainstorm task with its Decision, a `self` audit and `sampo task done`.
