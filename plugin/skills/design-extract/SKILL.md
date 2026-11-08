---
name: design-extract
description: DESIGN.md's token table from a repo's CSS custom properties, its contrast proved, or a light/dark palette from one seed colour (usage /sampo:design-extract [ui root | seed colour]).
argument-hint: "[ui root | seed colour]"
disable-model-invocation: true
---

# Sampo: design tokens

Three `sampo design` verbs, stdlib only, the same from Claude Code and a Smith (`sampo smith`) session. User-started (the skill
listing has no room for another skill Claude starts itself); Claude reaches the verbs through the directions skill
and the playbook `../playbooks/references/ui/type-and-colour.md`, which say when to run them.

- `sampo design extract PATHS` reads the CSS custom properties set in `:root`, `html`, `:host` and `@theme` (dark values
  from a `.dark`, `[data-theme]` or `prefers-color-scheme: dark` block, or `light-dark()`), plus the literal colours
  of a `tailwind.config`, and writes them as a table into the `## Tokens` section of `DESIGN.md` (at the repo root, or
  `docs/DESIGN.md` when that is the one there; `--out FILE`, `--out -` prints). It replaces only that section, and adds
  it after `## Knobs` when it is missing. Each colour gets its sRGB hex, and the known role pairs (text, muted text,
  border and accent on each surface; text on the accent and subtle fills) their WCAG contrast.
- `sampo design contrast TOKENS.css [fg/bg[:need[,dark need]] ...]` proves pairs in light and dark on the hex the
  browser paints, warns on values outside sRGB and exits 1 on any failure. Without pairs it checks the known role
  pairs, body text 7:1 in dark. A CI without Sampo copies `plugin/lib/sampo/design.py` (stdlib only, Python 3.11 or
  later) and runs `python3 design.py contrast …` the same way.
- `sampo design palette SEED [--format css|dtcg|tailwind]` makes those roles in both themes from one colour (hex, `rgb()`,
  `hsl()`, `oklch()`): neutrals tinted with its hue, the accent keeping its hue, each text role moved in lightness until
  its pairs pass. The export goes to stdout and the proof to stderr, so `> tokens/colour.css` keeps only the tokens.

## Procedure

1. **A brief.** Writing `DESIGN.md` or a token file is an edit: work inside a focused task, or start one with
   `sampo task new "Design tokens for <product>" --type FEATURE --tier S --ac "DESIGN.md has the token table :: grep -q
   '^## Tokens' DESIGN.md" --focus`.
2. **Read `$ARGUMENTS`.** A colour is a seed: go to step 4. A path is the UI root; none means the repo: find the
   folder holding the token file (`grep -rlE -- '--[a-z][\w-]*:' --include=*.css .`, built and vendored folders aside).
3. **Extract.** `sampo design extract <ui root>`, then read the table back. Tokens the components use but the table lacks
   live somewhere it doesn't read (CSS-in-JS, a JS theme object): name them in the report. A pair marked FAIL is a
   finding: fix the token test-first, or capture it.
4. **No tokens yet, or a new palette asked for.** `sampo design palette '<seed>'` with the seed from `DESIGN.md`'s accent
   (else the brand colour, else ask once), into the token file the playbook names (`tokens/colour.css`), `--format
   tailwind` when the project uses Tailwind v4, `dtcg` for design tools. Then step 3. The palette is a start, not a
   direction: status colours, hover states and the type scale come from `type-and-colour.md`.
5. **Prove it in CI.** Add `sampo design contrast <token file>` (or the copied script) to the checks the project runs,
   with the pairs its components actually use, and record it as the step's evidence.
6. **No direction yet** (`DESIGN.md` has no `## Direction` section; a Tokens section alone is a palette): offer
   `/sampo:directions`, which keeps the Tokens section and says per card whether it keeps this palette.
