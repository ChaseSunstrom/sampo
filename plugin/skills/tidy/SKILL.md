---
name: tidy
description: Use whenever the user asks to clean up Sampo or memory, or tidying is overdue: archives, flags stale items, lints; dry-run first.
disable-model-invocation: true
---

# Sampo: tidy

0. When the disk is the worry, `sampo tidy --report` (read-only; `--json`, `--budget SECONDS`) lists each Sampo-owned kind per project and machine-wide: size, count, newest use, owner, what its cap would free, and anything it ran out of time to size.
1. `sampo tidy` (dry run; `--all` covers every registered project). Read every finding.
2. Safe to apply without asking (archives, never deletes): old done/dropped briefs to `archive/YYYY-MM/`, ledger and event rotation, regenerated STATE/INBOX. Run `sampo tidy --apply`.
3. Needs the user's decision: stale inbox items (keep or drop — never auto-delete user requests), CLAUDE.md or rules cleanup, auto memory edits (dedupe, dead paths, contradictions), unclassified or unused plugins. Present them as one batched question with your default for each.
4. Apply approved memory/CLAUDE.md edits yourself (they are not Sampo state), then run `sampo tidy` again and report what changed in ≤ 10 lines.
