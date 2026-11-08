# Whole-repo cleanup sweep (Sampo's own, T-0364)

Use when asked to "clean up the repo / docs / code / briefs / dead code / unused or replaced functionality". A dead-code tool that
finds nothing is one row of evidence, never the verdict: a sweep is done when every area has been checked by a stated
method, not when a gate stays green.

## Plan it as what it is
- Tier L (M only for a small repo). One step per area below; a request folded into this task keeps its own words in
  Raw request, and each part of it gets a step (UI, settings, environment are areas too).
- Inventory first: `git ls-files | cut -d/ -f1-2 | sort | uniq -c` → the brief's **Coverage** section, one row per
  area: `area · files · method · found · fixed · captured`. Before/after numbers: files, lines, tool findings.
- Big repos: one `sampo:fm-recon` per area in parallel (≤ 3), each returning candidates with `path:line` and the
  evidence; the main thread verifies and removes.

## Areas and how each is checked
1. **Dead code** — the language's tools (vulture, knip/ts-prune, `cargo +nightly udeps`, `deadcode`, `-Wunused`) and,
   for every exported or public symbol they can't see across modules, a caller search (`rg -w NAME`): only tests or
   nothing call it → candidate.
2. **Replaced functionality** — two things doing one job: old/new, v1/v2, legacy, compat, fallback, deprecated, "TODO
   remove", flags that are always on or off, a module whose callers all moved. `git log --diff-filter=A -S` and commit
   messages with replace/instead of/supersede point at what was left behind. Move the last callers, then delete the old
   path and its tests.
3. **Docs** — every doc file against the code it describes: paths exist, commands run, options and defaults match,
   screenshots and feature lists are current; duplicates merged; dead links (`rg -o '\]\(([^)#]+)' docs`) fixed.
4. **Settings, config, environment** — every setting and env var is read somewhere and documented once; unused ones
   go, undocumented ones get a line.
5. **UI** — unused components, styles, assets and routes (no import, no reference); one style for one thing.
6. **Stray files** — committed build output, logs, scratch, old scripts, files `.gitignore` should cover.
7. **Sampo state** — `/sampo:tidy`: archive finished briefs, drop stale captured ones with a reason, merge
   duplicates.
8. **Needs improving** — duplication and smells found on the way: fix the small ones here, capture the rest
   (`sampo capture --source discovered`) with the evidence.

## Done when
Every Coverage row names its method and result (zero found is fine, with the method that found zero), behaviour is
unchanged (the full gate before and after), and the before/after numbers are in the final report.
