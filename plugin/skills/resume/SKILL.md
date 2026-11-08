---
name: resume
description: Use whenever you start after /compact, /clear or a crash, or "where were we" is asked: restores task context.
---

# Sampo: resume

1. `sampo resume` — active task, current step, the "Resume here" notes and the brief path, the commands already tried on the step, and the last green check re-run so drift shows before you build on it (`--no-check` skips it). (SessionStart already injected a summary; this gives the full picture.)
2. Read the brief's Execution prompt and the current step. Compare "Resume here" with reality: `git status`, `git log --oneline -5`, the files it names.
3. State the resume point in one line: "Resuming T-0012 [FIX] at step 3/5: <step>."
4. If reality disagrees with the brief (uncommitted work missing, files changed), record what you found (`sampo checkpoint --note …`) before continuing.
5. Continue with what `sampo next` names (the harness derives it from the brief). If there is no active task, run `/sampo:next`.
