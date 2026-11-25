---
name: genesis
description: Use whenever the user wants Rune (Genesis): self-seeding generations toward one goal they wrote, gen-0 to a final report, driven by sampo rune step.
disable-model-invocation: true
---

# Rune: Genesis

A tower of generations toward one goal the user wrote (gen-0). Tower files change only through `sampo rune put`, every choice goes through `sampo decide`, and anything that leaves the machine needs its grant. The full rules, every check and every row of the sweep are in `references/procedure.md`. Read it before any step, and read it again after a compaction: this stub keeps only the step headings.

## Start (no gen-0 yet)

1. The user's own words are gen-0: `sampo rune draft "<line>"` when given only a line, then `sampo rune init --file -` to seal it.
2. `sampo rune spec` maps the capabilities to an exam, a third held out for grading.
3. `sampo rune exam` grades Genesis itself on the held-out third.

## Drive

Repeat until the kind is done or stopped. `sampo rune step --json` names the one next action; do it.

1. Stages in order: spec, seed (at most 8 Missions), mission, intake, build, critique, select, drift, review, gap map, sweep, then the next seed or the report.
2. From gen 2 the seed's 8th Mission is the moonshot: `sampo rune frontier N`, then `sampo rune novelty N`, then the spiked pick.
3. After a tower edit, `sampo rune check`; a hand-edited tower file gets `sampo rune put` again.

Kinds: blocked (work the rest of the queue), fatal (fix what the action names), stopped (three reverted generations in a row: read lineage.md, then write the next seed by hand or file the report), done (report.md is filed).

## See it

`sampo rune status` prints the tower; `sampo rune live on` (a publish grant) keeps the site's genesis page current.
