<!-- Sampo's own index (written for Sampo, not ported). -->
# Context playbooks: writing for readers

Paths are relative to the playbooks skill. The stage's other playbooks (context budget, living docs, compaction) are
listed in SKILL.md.

- `references/context/docs.md` — Task-first pages: one kind per page, numbered steps with results, examples run in CI by a self-checked run_examples.py.
- `references/context/write-plainly.md` — Plain writing for every reader: the answer first, lists before paragraphs, word limits by kind, checked by `sampo readme check --prose`.
- `references/context/decks-and-reports.md` — Decks, reports and status updates: answer and ask first, sentence titles, sourced numbers, built from source.
- `references/context/instruction-files.md` — CLAUDE.md, rules and skill descriptions that cost little: count what loads, checkable rules, short descriptions.
- `references/context/context-budget.md` — Audit what fills the context window (agents, skills, MCP, rules) and cut it.
- `references/context/living-docs.md` — Keep a long-lived project's documentation from rotting by assigning existing docs clear owners.
- `references/context/strategic-compact.md` — Compact the context at logical phase breaks, to keep each phase's state.
