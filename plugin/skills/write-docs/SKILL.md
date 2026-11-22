---
name: write-docs
description: Use whenever you write or rework docs, a README or a how-to guide: task-first, examples that run.
---

# Sampo: write documentation

A page a person reads to get something done. No brief yet → `/sampo:intake` first; a docs-only change is a FEATURE or CLEAN task like any other.

1. **Read the code, not your memory.** `sampo map` for the layout and entry points; `sampo sym PATH:NAME` for a function's real behaviour. Flags come from `--help`, not from the old page.
2. **Write the page** by `../playbooks/references/context/docs.md`: the reader and outcome first, one kind of page per page, the common path first, numbered steps that each end in what you should see, plain words.
   **A README is scanned, never a wall of text.** Top: the name, one line on what it is, a figure, then install and
   the first command in ten lines or fewer; short sections under task headings below, depth on linked pages under
   docs/. The rules and limits for any prose (README, report, research note, CHANGELOG, close) are in
   `../playbooks/references/context/write-plainly.md`; `sampo readme check` names each wall of text by line: fix
   every one before the close.
3. **Show it.** A README, docs or landing page gets real figures (app screenshots, architecture and flow diagrams, dark and light): load: Skill sampo:readme-visuals (`sampo readme plan`); a diagram or dashboard of the repo's parts: `../board-panels/SKILL.md`.
4. **Run every example.** Copy each command and code block out and run it; paste what really printed. A block you can't run is marked as such.
5. **Keep the repo's doc set fresh** (README, CHANGELOG, guides, MASTER): `../playbooks/references/context/living-docs.md`. A project instruction file (CLAUDE.md, rules) → `../playbooks/references/context/instruction-files.md`.
6. **Check links, names and shape:** grep the repo for each renamed command or path, open each relative link, and
   run `sampo readme check` on each README you touched (figures and walls of text; exit 0 before the close).
7. **Say it in the close:** `sampo task finish ID --docs "<the pages changed>"` records the docs impact; `sampo check --affected --evidence ID` runs the repo's doc tests.
