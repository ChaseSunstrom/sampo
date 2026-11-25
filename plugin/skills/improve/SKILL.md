---
name: improve
description: Use whenever asked to improve Sampo itself, or its self-inbox has items and the queue's empty: eval-gated changes.
---

# Sampo: improve (bounded, eval-gated)

Sampo improves its own skills, rules, hooks and scripts, never the model, and never unreviewed. The full steps, with every command and rule, are in `references/procedure.md`. Read it before any step, and read it again after a compaction: this stub keeps only the step headings.

1. Collect: the self-inbox (`sampo state`, items with `source: self`), plus `sampo doctor`, `sampo tidy` and hook latency.
2. Propose at most 5 changes, each with its evidence; ask one yes/no question.
3. Isolate: build candidates in a worktree only; never edit the live plugin.
4. Protected core (Ward, deny rules, the evidence rules, the eval suite): the user's yes, through `sampo ask`.
5. Referee: the eval suite on both arms, then `sampo bench run`, `sampo bench compare`, `sampo bench gate` and `sampo bench court`.
5b. Automated generations: `sampo evolve --target <path>` for one instruction file.
6. Ask for the merge with a table of change, evidence, eval delta and budgets.
7. After the user's yes: merge, bump the plugin version, tag; roll back with `git revert`.
