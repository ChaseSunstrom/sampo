---
name: write-docs
description: Use whenever you write or rework docs, a README or a how-to guide: task-first, examples that run.
---

# Sampo: write documentation

A page a person reads to get something done. No brief yet → `/foreman:intake` first; a docs-only change is a FEATURE or CLEAN task like any other.

1. **Read the code, not your memory.** `sampo map` for the layout and entry points; `sampo sym PATH:NAME` for a function's real behaviour. Flags come from `--help`, not from the old page.
2. **Write the page** by `../playbooks/references/context/docs.md`: the reader and outcome first, one kind of page per page, the common path first, numbered steps that each end in what you should see, plain words.
3. **Show it.** A README, docs or landing page gets real figures (app screenshots, architecture and flow diagrams, dark and light): load: Skill foreman:readme-visuals (`sampo readme plan`).
4. **Run every example.** Copy each command and code block out and run it; paste what really printed. A block you can't run is marked as such.
5. **Keep the repo's doc set fresh** (README, CHANGELOG, guides, MASTER): `../playbooks/references/context/living-docs.md`. A project instruction file (CLAUDE.md, rules) → `../playbooks/references/context/instruction-files.md`.
6. **Check links and names:** grep the repo for each renamed command or path, and open each relative link.
7. **Say it in the close:** `sampo task finish ID --docs "<the pages changed>"` records the docs impact; `sampo check --affected --evidence ID` runs the repo's doc tests.
